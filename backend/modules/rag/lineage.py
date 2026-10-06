from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from bson import ObjectId

from core.config import settings
from core.database import get_rag_db, mongo_transaction
from modules.documents.repository import object_id
from modules.documents.store import get_document_repository
from modules.questions.citations import count_versions_citing_chunks
from modules.rag.chromadb_engine import get_collection


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CandidateLineage:
    document_id: str
    ocr_job_id: str
    chunk_set_id: str
    vector_collection_id: str

    def as_object_ids(self) -> dict[str, ObjectId]:
        return {
            "document_id": object_id(self.document_id, "document_id"),
            "ocr_job_id": object_id(self.ocr_job_id, "ocr_job_id"),
            "chunk_set_id": object_id(self.chunk_set_id, "chunk_set_id"),
            "vector_collection_id": object_id(self.vector_collection_id, "vector_collection_id"),
        }


def _snapshot(value: dict) -> dict:
    return {
        "ocr_job_id": value.get("ocr_job_id"),
        "chunk_set_id": value.get("chunk_set_id"),
        "vector_collection_id": value.get("vector_collection_id"),
    }


def _match_snapshot(document_id: ObjectId, snapshot: dict) -> dict:
    query = {"_id": document_id, "archived_at": None}
    for key, value in snapshot.items():
        query[f"current_processing.{key}"] = value
    return query


def _summarize_values(values: list[object], label: str, *, limit: int = 5) -> str:
    sample = ", ".join(str(value) for value in values[:limit])
    suffix = " ..." if len(values) > limit else ""
    return f"{len(values)} {label}; sample: {sample}{suffix}"


class LineageValidator:
    def __init__(self, db=None, collection_getter: Callable[[str], object] = get_collection):
        self.db = db if db is not None else get_rag_db()
        self.collection_getter = collection_getter

    def validate(self, candidate: CandidateLineage, *, smoke_queries: list[str]) -> dict:
        ids = candidate.as_object_ids()
        errors: list[str] = []
        warnings: list[str] = []
        document_repository = get_document_repository(self.db)
        document = document_repository.find_by_id(ids["document_id"])
        ocr_job = document_repository.find_job(ids["ocr_job_id"])
        chunk_set = self.db.chunk_sets.find_one({"_id": ids["chunk_set_id"]})
        vector = self.db.vector_collections.find_one({"_id": ids["vector_collection_id"]})
        ownership_mismatch = False
        if not document:
            errors.append("document missing or archived")
        if not ocr_job or ocr_job.get("job_type") != "OCR" or ocr_job.get("status") != "COMPLETED":
            errors.append("OCR job is not COMPLETED")
        elif ocr_job.get("document_id") != ids["document_id"]:
            errors.append("OCR job belongs to another document")
            ownership_mismatch = True
        if not chunk_set or chunk_set.get("status") != "COMPLETED":
            errors.append("chunk set is not COMPLETED")
        elif chunk_set.get("document_id") != ids["document_id"]:
            errors.append("chunk set belongs to another document")
            ownership_mismatch = True
        elif chunk_set.get("source_ocr_job_id") != ids["ocr_job_id"]:
            errors.append("chunk set does not descend from candidate OCR job")
        if ocr_job and chunk_set and ocr_job.get("document_version") != chunk_set.get("document_version"):
            errors.append("OCR job and chunk set document versions differ")
        if document and ocr_job and ocr_job.get("document_version") != document.get("current_version"):
            errors.append("candidate document version is not current")
        if not vector or not vector.get("is_active"):
            errors.append("vector collection metadata missing or inactive")
        candidate_snapshot = {key: value for key, value in ids.items() if key != "document_id"}
        if document:
            current_snapshot = _snapshot(document.get("current_processing") or {})
            pending_snapshot = _snapshot(document.get("pending_processing") or {})
            if candidate_snapshot == current_snapshot:
                warnings.append("candidate is already the active lineage")
            elif candidate_snapshot != pending_snapshot:
                errors.append("candidate is neither the registered pending lineage nor the active lineage")

        if ownership_mismatch:
            return {
                "status": "failed",
                "errors": errors,
                "warnings": sorted(set(warnings)),
                "metrics": {
                    "pages": 0,
                    "chunks": 0,
                    "embeddings": 0,
                    "chroma_vectors": 0,
                    "retrieval_smoke_passed": 0,
                    "retrieval_smoke_total": len(smoke_queries),
                },
            }

        pages = document_repository.list_pages_for_job(ids["document_id"], ids["ocr_job_id"])
        if not pages:
            errors.append("candidate has no persisted pages")
        pages_without_blocks: list[object] = []
        for page in pages:
            page_label = page.get("page_number") or page.get("unit_number")
            if (page.get("quality") or {}).get("status") == "quality_failed":
                errors.append(f"page {page_label} failed extraction quality")
            if (page.get("cleaned_text") or "").strip() and not (page.get("content_blocks") or []):
                pages_without_blocks.append(page_label)
            for block in page.get("content_blocks") or []:
                provenance = block.get("provenance") or {}
                if str(provenance.get("document_id")) != candidate.document_id:
                    errors.append(f"block {block.get('block_id')} has wrong document provenance")
                if block.get("validation_status") == "failed":
                    errors.append(f"block {block.get('block_id')} failed typed validation")
                elif block.get("validation_status") == "needs_review":
                    warnings.append(f"block {block.get('block_id')} needs review")
                if block.get("block_type") == "table" and not (block.get("structured_content") or {}).get("rows"):
                    errors.append(f"table {block.get('block_id')} has no cell grid")
                if block.get("block_type") == "formula" and not any(
                    (block.get("structured_content") or {}).get(key)
                    for key in ("raw", "latex", "mathml", "omml")
                ):
                    errors.append(f"formula {block.get('block_id')} has no raw representation")
            for asset in page.get("assets") or []:
                provenance = asset.get("provenance") or {}
                if not asset.get("content_sha256"):
                    warnings.append(f"asset {asset.get('asset_id')} has no content hash")
                if not (asset.get("storage_uri") or provenance.get("raw_ref")):
                    errors.append(f"asset {asset.get('asset_id')} has no original/crop reference")
                if asset.get("validation_status") == "failed":
                    errors.append(f"asset {asset.get('asset_id')} failed typed validation")
                elif asset.get("validation_status") == "needs_review":
                    warnings.append(f"asset {asset.get('asset_id')} needs review")
        if pages_without_blocks:
            errors.append(_summarize_values(pages_without_blocks, "page(s) have text but no source blocks"))

        chunks = list(self.db.document_chunks.find({"document_id": ids["document_id"], "chunk_set_id": ids["chunk_set_id"]}))
        if not chunks:
            errors.append("candidate has no Mongo chunks")
        if chunk_set and int(chunk_set.get("total_chunks") or 0) != len(chunks):
            errors.append("chunk set total does not match Mongo chunk count")
        chunk_by_id = {chunk["_id"]: chunk for chunk in chunks}
        chunks_without_provenance: list[object] = []
        chunks_without_location: list[object] = []
        for chunk in chunks:
            source = chunk.get("source") or {}
            if not source.get("source_uri") or not source.get("block_ids"):
                chunks_without_provenance.append(chunk["_id"])
            if not (chunk.get("page_range") or {}).get("pages") and not source.get("source_locations"):
                chunks_without_location.append(chunk["_id"])
            validation = chunk.get("validation") or {}
            if "failed" in (validation.get("statuses") or []):
                errors.append(f"chunk {chunk['_id']} contains failed source content")
            elif validation.get("requires_review"):
                warnings.append(f"chunk {chunk['_id']} contains content needing review")
        if chunks_without_provenance:
            errors.append(_summarize_values(chunks_without_provenance, "chunk(s) are missing provenance"))
        if chunks_without_location:
            errors.append(_summarize_values(chunks_without_location, "chunk(s) have no page/source location"))

        embeddings = list(
            self.db.chunk_embeddings.find(
                {"chunk_set_id": ids["chunk_set_id"], "vector_collection_id": ids["vector_collection_id"]}
            )
        )
        if len(embeddings) != len(chunks):
            errors.append("Mongo embedding count does not match chunk count")
        embeddings_without_chunk: list[object] = []
        for embedding in embeddings:
            chunk = chunk_by_id.get(embedding.get("chunk_id"))
            if not chunk:
                embeddings_without_chunk.append(embedding.get("_id"))
            elif embedding.get("status") != "INDEXED":
                errors.append(f"embedding {embedding.get('_id')} is not INDEXED")
            elif embedding.get("embedding_content_hash") != chunk.get("content_hash"):
                errors.append(f"embedding {embedding.get('_id')} content hash is stale")
        if embeddings_without_chunk:
            errors.append(_summarize_values(embeddings_without_chunk, "embedding(s) point to a missing chunk"))

        chroma_count = 0
        retrieval_hits = 0
        if vector:
            try:
                collection = self.collection_getter(vector["collection_name"])
                where = {
                    "$and": [
                        {"document_id": candidate.document_id},
                        {"chunk_set_id": candidate.chunk_set_id},
                    ]
                }
                chroma = collection.get(where=where, include=["metadatas", "documents"])
                chroma_ids = set(chroma.get("ids") or [])
                chroma_count = len(chroma_ids)
                expected_ids = {embedding.get("external_vector_id") for embedding in embeddings}
                if chroma_ids != expected_ids:
                    errors.append("Chroma IDs do not match Mongo embedding IDs")
                for metadata in chroma.get("metadatas") or []:
                    chunk = chunk_by_id.get(object_id(metadata.get("chunk_id"), "chunk_id"))
                    if not chunk or metadata.get("content_hash") != chunk.get("content_hash"):
                        errors.append("Chroma metadata/content hash mismatch")
                        break
                if not smoke_queries:
                    errors.append("at least one retrieval smoke query is required")
                for query in smoke_queries:
                    result = collection.query(
                        query_texts=[query], where=where, n_results=min(5, max(chroma_count, 1)), include=["metadatas"]
                    )
                    metas = (result.get("metadatas") or [[]])[0] or []
                    if not metas or any(
                        meta.get("document_id") != candidate.document_id
                        or meta.get("chunk_set_id") != candidate.chunk_set_id
                        for meta in metas
                    ):
                        errors.append(f"retrieval smoke failed: {query}")
                    elif any(meta.get("requires_review") for meta in metas):
                        errors.append(f"retrieval smoke returned content needing review: {query}")
                    else:
                        retrieval_hits += 1
            except Exception as exc:
                errors.append(f"Chroma validation failed: {type(exc).__name__}: {exc}")

        status = "failed" if errors else "needs_review" if warnings else "passed"
        return {
            "status": status,
            "errors": errors,
            "warnings": sorted(set(warnings)),
            "metrics": {
                "pages": len(pages),
                "chunks": len(chunks),
                "embeddings": len(embeddings),
                "chroma_vectors": chroma_count,
                "retrieval_smoke_passed": retrieval_hits,
                "retrieval_smoke_total": len(smoke_queries),
            },
        }


class LineagePromotionService:
    def __init__(self, db=None, validator: LineageValidator | None = None):
        self.db = db if db is not None else get_rag_db()
        self.validator = validator or LineageValidator(self.db)

    @staticmethod
    def confirmation_token(candidate: CandidateLineage) -> str:
        return f"PROMOTE:{candidate.document_id}:{candidate.chunk_set_id}"

    @staticmethod
    def _require_audit(actor: str, reason: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and reason are required for lineage mutations")

    def dry_run(self, candidate: CandidateLineage, *, smoke_queries: list[str]) -> dict:
        validation = self.validator.validate(candidate, smoke_queries=smoke_queries)
        document = get_document_repository(self.db).find_by_id(candidate.document_id) or {}
        return {
            "operation": "dry-run",
            "validation": validation,
            "from_snapshot": _snapshot(document.get("current_processing") or {}),
            "to_snapshot": {key: value for key, value in candidate.as_object_ids().items() if key != "document_id"},
            "confirmation_token": self.confirmation_token(candidate) if validation["status"] == "passed" else None,
        }

    def promote(
        self,
        candidate: CandidateLineage,
        *,
        smoke_queries: list[str],
        actor: str,
        reason: str,
        confirmation: str,
    ) -> dict:
        self._require_audit(actor, reason)
        if confirmation != self.confirmation_token(candidate):
            raise PermissionError("invalid promotion confirmation token")
        expected_document = (get_document_repository(self.db).find_by_id(candidate.document_id)
                             if settings.document_store == "postgres" else None)
        if settings.document_store == "postgres" and not expected_document:
            raise LookupError("document missing or archived")
        validation = self.validator.validate(candidate, smoke_queries=smoke_queries)
        if validation["status"] != "passed":
            raise ValueError("candidate lineage did not pass validation")
        ids = candidate.as_object_ids()
        if settings.document_store == "postgres":
            return get_document_repository(self.db).promote_lineage(
                candidate.document_id,
                {key: value for key, value in ids.items() if key != "document_id"},
                operation_id=str(uuid.uuid4()), validation=validation,
                expected_current=_snapshot(expected_document.get("current_processing") or {}),
                expected_version=expected_document["current_version"],
                actor=actor, reason=reason,
            )
        document = self.db.documents.find_one({"_id": ids["document_id"], "archived_at": None})
        if not document:
            raise LookupError("document missing or archived")
        before = _snapshot(document.get("current_processing") or {})
        after = {key: value for key, value in ids.items() if key != "document_id"}
        pending = _snapshot(document.get("pending_processing") or {})
        if pending != after:
            raise ValueError("candidate is no longer the registered pending lineage")
        operation_id = str(uuid.uuid4())
        event = {
            "schema_version": 2,
            "operation_id": operation_id,
            "event_type": "PROMOTE",
            "document_id": ids["document_id"],
            "actor": actor,
            "reason": reason,
            "from_snapshot": before,
            "to_snapshot": after,
            "validation": validation,
            "rollback_available": True,
            "created_at": utc_now(),
        }
        changed_without_transaction = False
        try:
            with mongo_transaction() as session:
                result = self.db.documents.update_one(
                    {
                        **_match_snapshot(ids["document_id"], before),
                        **{f"pending_processing.{key}": value for key, value in after.items()},
                    },
                    {
                        "$set": {
                            **{f"current_processing.{key}": value for key, value in after.items()},
                            "status": "READY",
                            "pipeline_summary.ocr_status": "COMPLETED",
                            "pipeline_summary.chunk_status": "COMPLETED",
                            "pipeline_summary.index_status": "COMPLETED",
                            "updated_at": utc_now(),
                        },
                        "$unset": {"pending_processing": ""},
                    },
                    session=session,
                )
                if not result.modified_count:
                    raise RuntimeError("active lineage changed concurrently")
                changed_without_transaction = session is None
                self.db.pipeline_lineage_events.insert_one(event, session=session)
        except Exception:
            if changed_without_transaction:
                self.db.documents.update_one(
                    _match_snapshot(ids["document_id"], after),
                    {"$set": {f"current_processing.{key}": value for key, value in before.items()}},
                )
            raise
        return {"operation_id": operation_id, "from_snapshot": before, "to_snapshot": after}

    def rollback(self, operation_id: str, *, actor: str, reason: str) -> dict:
        self._require_audit(actor, reason)
        if settings.document_store == "postgres":
            repository = get_document_repository(self.db)
            promoted = repository.promotion_event(operation_id)
            if not promoted:
                raise LookupError("promotion is missing or no longer rollbackable")
            before = promoted["from_snapshot"]
            ocr_job = repository.find_job(before.get("ocr_job_id")) if before.get("ocr_job_id") else None
            target_checks = {
                "ocr_job": bool(ocr_job and ocr_job.get("status") == "COMPLETED"),
                "chunk_set": bool(self.db.chunk_sets.find_one(
                    {"_id": before.get("chunk_set_id"), "status": "COMPLETED"})),
                "vector_collection": bool(self.db.vector_collections.find_one(
                    {"_id": before.get("vector_collection_id"), "is_active": True})),
            }
            if not all(target_checks.values()):
                raise ValueError("rollback target lineage is incomplete")
            return repository.rollback_lineage(
                operation_id, rollback_id=str(uuid.uuid4()), actor=actor, reason=reason,
            )
        promoted = self.db.pipeline_lineage_events.find_one(
            {"operation_id": operation_id, "event_type": "PROMOTE", "rollback_available": True}
        )
        if not promoted:
            raise LookupError("promotion is missing or no longer rollbackable")
        document_id = promoted["document_id"]
        before, after = promoted["from_snapshot"], promoted["to_snapshot"]
        target_checks = {
            "ocr_job": bool(self.db.document_jobs.find_one({"_id": before.get("ocr_job_id"), "status": "COMPLETED"})),
            "chunk_set": bool(self.db.chunk_sets.find_one({"_id": before.get("chunk_set_id"), "status": "COMPLETED"})),
            "vector_collection": bool(
                self.db.vector_collections.find_one({"_id": before.get("vector_collection_id"), "is_active": True})
            ),
        }
        if not all(target_checks.values()):
            raise ValueError("rollback target lineage is incomplete")
        rollback_id = str(uuid.uuid4())
        changed_without_transaction = False
        try:
            with mongo_transaction() as session:
                result = self.db.documents.update_one(
                    _match_snapshot(document_id, after),
                    {
                        "$set": {
                            **{f"current_processing.{key}": value for key, value in before.items()},
                            "updated_at": utc_now(),
                        }
                    },
                    session=session,
                )
                if not result.modified_count:
                    raise RuntimeError("rollback target is not the active lineage")
                changed_without_transaction = session is None
                self.db.pipeline_lineage_events.insert_one(
                    {
                        "schema_version": 2,
                        "operation_id": rollback_id,
                        "event_type": "ROLLBACK",
                        "document_id": document_id,
                        "actor": actor,
                        "reason": reason,
                        "from_snapshot": after,
                        "to_snapshot": before,
                        "promotion_operation_id": operation_id,
                        "validation": {"status": "passed", "rollback_target_checks": target_checks},
                        "created_at": utc_now(),
                    },
                    session=session,
                )
                self.db.pipeline_lineage_events.update_one(
                    {"operation_id": operation_id}, {"$set": {"rollback_available": False}}, session=session
                )
        except Exception:
            if changed_without_transaction:
                self.db.documents.update_one(
                    _match_snapshot(document_id, before),
                    {"$set": {f"current_processing.{key}": value for key, value in after.items()}},
                )
            raise
        return {"operation_id": rollback_id, "restored_snapshot": before}

    def archive(self, candidate: CandidateLineage, *, actor: str, reason: str) -> dict:
        self._require_audit(actor, reason)
        ids = candidate.as_object_ids()
        if settings.document_store == "postgres":
            chunk_set = self.db.chunk_sets.find_one({
                "_id": ids["chunk_set_id"], "document_id": ids["document_id"],
                "source_ocr_job_id": ids["ocr_job_id"], "archived_at": None,
            })
            if not chunk_set:
                raise ValueError("lineage missing or already archived")
            return get_document_repository(self.db).queue_archive_lineage(
                candidate.document_id,
                {key: value for key, value in ids.items() if key != "document_id"},
                operation_id=str(uuid.uuid4()), actor=actor, reason=reason,
            )
        document = get_document_repository(self.db).find_by_id(ids["document_id"]) or {}
        referenced = {
            *((document.get("current_processing") or {}).values()),
            *((document.get("pending_processing") or {}).values()),
        }
        rollback_ref = self.db.pipeline_lineage_events.find_one(
            {
                "document_id": ids["document_id"],
                "rollback_available": True,
                "$or": [
                    {"from_snapshot.chunk_set_id": ids["chunk_set_id"]},
                    {"to_snapshot.chunk_set_id": ids["chunk_set_id"]},
                ],
            }
        )
        if ids["chunk_set_id"] in referenced or ids["ocr_job_id"] in referenced or rollback_ref:
            raise ValueError("active, pending, or rollback lineage cannot be archived")
        now = utc_now()
        operation_id = str(uuid.uuid4())
        changed_without_transaction = False
        try:
            with mongo_transaction() as session:
                archived = self.db.chunk_sets.update_one(
                    {"_id": ids["chunk_set_id"], "archived_at": None},
                    {"$set": {"archived_at": now, "archived_by": actor, "archive_reason": reason}},
                    session=session,
                )
                if not archived.modified_count:
                    raise RuntimeError("lineage was already archived or changed concurrently")
                changed_without_transaction = session is None
                self.db.pipeline_lineage_events.insert_one(
                    {
                        "schema_version": 2,
                        "operation_id": operation_id,
                        "event_type": "ARCHIVE",
                        "document_id": ids["document_id"],
                        "actor": actor,
                        "reason": reason,
                        "from_snapshot": {key: value for key, value in ids.items() if key != "document_id"},
                        "to_snapshot": {"archived_at": now},
                        "validation": {
                            "status": "passed",
                            "guards": ["not_active", "not_pending", "not_rollback"],
                        },
                        "created_at": now,
                    },
                    session=session,
                )
        except Exception:
            if changed_without_transaction:
                self.db.chunk_sets.update_one(
                    {"_id": ids["chunk_set_id"], "archived_at": now},
                    {"$unset": {"archived_at": "", "archived_by": "", "archive_reason": ""}},
                )
            raise
        return {"operation_id": operation_id, "archived_at": now}

    def _ensure_not_cited(self, chunk_set_id) -> None:
        """Question versions are immutable evidence records; their cited chunks must outlive them."""
        chunk_ids = self.db.document_chunks.distinct("_id", {"chunk_set_id": chunk_set_id})
        cited = count_versions_citing_chunks(self.db, chunk_ids)
        if cited:
            raise ValueError(f"lineage is cited by {cited} question version(s) and cannot be deleted")

    def request_permanent_delete(
        self, candidate: CandidateLineage, *, actor: str, reason: str, confirmation: str
    ) -> dict:
        self._require_audit(actor, reason)
        expected = f"DELETE:{candidate.document_id}:{candidate.chunk_set_id}"
        if confirmation != expected:
            raise PermissionError("invalid permanent-delete confirmation token")
        ids = candidate.as_object_ids()
        chunk_set = self.db.chunk_sets.find_one({"_id": ids["chunk_set_id"], "archived_at": {"$ne": None}})
        if not chunk_set:
            raise ValueError("candidate must be archived before permanent deletion")
        self._ensure_not_cited(ids["chunk_set_id"])
        if settings.document_store == "postgres":
            if chunk_set.get("document_id") != ids["document_id"]:
                raise ValueError("chunk set belongs to another document")
            return get_document_repository(self.db).request_permanent_delete(
                candidate.document_id,
                {key: value for key, value in ids.items() if key != "document_id"},
                operation_id=str(uuid.uuid4()), actor=actor, reason=reason,
            )
        operation_id = str(uuid.uuid4())
        self.db.pipeline_lineage_events.insert_one(
            {
                "schema_version": 2,
                "operation_id": operation_id,
                "event_type": "PERMANENT_DELETE_REQUESTED",
                "document_id": ids["document_id"],
                "actor": actor,
                "reason": reason,
                "to_snapshot": {key: value for key, value in ids.items() if key != "document_id"},
                "status": "AWAITING_OFFLINE_BACKUP_AND_EXECUTION",
                "validation": {"status": "passed", "guards": ["archived_first"]},
                "created_at": utc_now(),
            }
        )
        return {
            "operation_id": operation_id,
            "status": "AWAITING_OFFLINE_BACKUP_AND_EXECUTION",
            "deleted": False,
        }

    def execute_permanent_delete(self, request_operation_id: str, *, confirmation: str) -> dict:
        expected = f"EXECUTE_DELETE:{request_operation_id}"
        if confirmation != expected:
            raise PermissionError("invalid execution confirmation token")
        if settings.document_store == "postgres":
            return get_document_repository(self.db).queue_permanent_delete(
                request_operation_id,
            )
        request = self.db.pipeline_lineage_events.find_one(
            {
                "operation_id": request_operation_id,
                "event_type": "PERMANENT_DELETE_REQUESTED",
                "status": "AWAITING_OFFLINE_BACKUP_AND_EXECUTION",
            }
        )
        if not request:
            raise LookupError("delete request missing or already executed")
        target = request["to_snapshot"]
        document_id = request["document_id"]
        document = self.db.documents.find_one({"_id": document_id}) or {}
        referenced = {
            *((document.get("current_processing") or {}).values()),
            *((document.get("pending_processing") or {}).values()),
        }
        if target["chunk_set_id"] in referenced or target["ocr_job_id"] in referenced:
            raise ValueError("active or pending lineage cannot be deleted")
        if self.db.pipeline_lineage_events.find_one(
            {
                "document_id": document_id,
                "rollback_available": True,
                "$or": [
                    {"from_snapshot.chunk_set_id": target["chunk_set_id"]},
                    {"to_snapshot.chunk_set_id": target["chunk_set_id"]},
                ],
            }
        ):
            raise ValueError("rollback lineage cannot be deleted")
        self._ensure_not_cited(target["chunk_set_id"])
        vector = self.db.vector_collections.find_one({"_id": target["vector_collection_id"]})
        embeddings = list(
            self.db.chunk_embeddings.find(
                {
                    "chunk_set_id": target["chunk_set_id"],
                    "vector_collection_id": target["vector_collection_id"],
                }
            )
        )
        external_ids = [item["external_vector_id"] for item in embeddings]
        collection = get_collection(vector["collection_name"])
        backup = collection.get(
            ids=external_ids,
            include=["documents", "metadatas", "embeddings"],
        ) if external_ids else {"ids": []}
        try:
            if external_ids:
                collection.delete(ids=external_ids)
            with mongo_transaction() as session:
                self.db.chunk_embeddings.delete_many(
                    {"chunk_set_id": target["chunk_set_id"]}, session=session
                )
                self.db.document_chunks.delete_many(
                    {"chunk_set_id": target["chunk_set_id"]}, session=session
                )
                self.db.document_pages.delete_many(
                    {"document_id": document_id, "ocr_job_id": target["ocr_job_id"]}, session=session
                )
                self.db.chunk_sets.delete_one({"_id": target["chunk_set_id"]}, session=session)
                self.db.document_jobs.delete_many(
                    {"_id": {"$in": [target["ocr_job_id"]]}}, session=session
                )
                self.db.pipeline_lineage_events.update_one(
                    {"operation_id": request_operation_id},
                    {"$set": {"status": "DELETED", "executed_at": utc_now()}},
                    session=session,
                )
        except Exception:
            if backup.get("ids"):
                collection.upsert(
                    ids=backup["ids"],
                    documents=backup.get("documents"),
                    metadatas=backup.get("metadatas"),
                    embeddings=backup.get("embeddings"),
                )
            raise
        return {"operation_id": request_operation_id, "status": "DELETED", "deleted": True}


def _auto_smoke_queries(db, chunk_set_id: ObjectId) -> list[str]:
    queries: list[str] = []
    for chunk in db.document_chunks.find({"chunk_set_id": chunk_set_id}).sort("chunk_no", 1).limit(20):
        if (chunk.get("validation") or {}).get("requires_review"):
            continue
        words = (chunk.get("content") or "").split()
        if len(words) >= 5:
            queries.append(" ".join(words[:16]))
        if len(queries) >= 2:
            break
    return queries


def auto_promote_pending_lineage(document_id: str, *, actor: str, reason: str, db=None) -> dict:
    """Validate and promote the registered pending lineage; failures leave it pending for manual review."""
    repository = get_document_repository(db)
    document = repository.find_by_id(document_id)
    pending = (document or {}).get("pending_processing") or {}
    if not all(pending.get(key) for key in ("ocr_job_id", "chunk_set_id", "vector_collection_id")):
        return {"status": "SKIPPED", "reason": "no pending lineage"}
    candidate = CandidateLineage(
        document_id=str(document_id),
        ocr_job_id=str(pending["ocr_job_id"]),
        chunk_set_id=str(pending["chunk_set_id"]),
        vector_collection_id=str(pending["vector_collection_id"]),
    )
    service = LineagePromotionService(db)
    smoke_queries = _auto_smoke_queries(service.db, candidate.as_object_ids()["chunk_set_id"])
    outcome: dict
    try:
        preview = service.dry_run(candidate, smoke_queries=smoke_queries)
        validation = preview["validation"]
        if validation["status"] != "passed":
            outcome = {
                "status": "FAILED",
                "reason": "; ".join((validation.get("errors") or validation.get("warnings") or [])[:5])
                or "candidate lineage did not pass validation",
            }
        else:
            result = service.promote(
                candidate,
                smoke_queries=smoke_queries,
                actor=actor,
                reason=reason,
                confirmation=preview["confirmation_token"],
            )
            outcome = {"status": "PROMOTED", "operation_id": result.get("operation_id")}
    except Exception as exc:
        outcome = {"status": "FAILED", "reason": str(exc) or type(exc).__name__}
    outcome["at"] = utc_now().isoformat()
    latest = repository.find_by_id(document_id)
    if latest:
        summary = dict(latest.get("pipeline_summary") or {})
        summary["auto_promotion"] = outcome
        repository.update(document_id, {"pipeline_summary": summary})
    return outcome
