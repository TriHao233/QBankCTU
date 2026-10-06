"""PostgreSQL document metadata repository; not routed until OCR/RAG cutover."""

from __future__ import annotations

from datetime import datetime

from bson import ObjectId
from psycopg import sql
from psycopg.types.json import Jsonb

from core.config import settings
from core.database import get_database
from core.postgres import postgres_connection
from db.bson_json import restore as _restore
from db.copy_business_data import normalized, projected_rows, upsert
from core.postgres_audit import write_postgres_audit_event
from modules.documents.repository import (
    ACTIVE_DOCUMENT_JOB_STATUSES, DOCUMENT_BUSY_MESSAGE, DUPLICATE_IGNORED_STATUSES, DocumentBusyError,
    MongoDocumentRepository, compact_raw_extraction, json_safe, object_id, utc_now,
)


def _lineage_snapshot(value: dict) -> dict:
    return {key: value.get(key) for key in (
        "ocr_job_id", "chunk_set_id", "vector_collection_id",
    )}


def _artifact_id(value: str):
    """Artifact chép từ MongoDB không có _id mang id tổng hợp "<document>:artifact:<n>"."""
    return ObjectId(value) if ObjectId.is_valid(value) else value


class PostgresDocumentRepository:
    def __init__(self):
        if settings.user_store != "postgres" or settings.catalog_store != "postgres":
            raise RuntimeError("DOCUMENT_STORE=postgres requires USER_STORE and CATALOG_STORE=postgres")
        self.validator = MongoDocumentRepository(get_database())

    @staticmethod
    def _load(conn, row: dict | None) -> dict | None:
        if row is None:
            return None
        record = _restore(row["payload"] or {})
        record.update({
            "_id": ObjectId(row["id"]), "title": row["title"],
            "original_filename": row["original_filename"],
            "subject_id": ObjectId(row["subject_id"]) if row["subject_id"] else None,
            "uploaded_by_user_id": (ObjectId(row["uploaded_by_user_id"])
                                    if row["uploaded_by_user_id"] else None),
            "status": row["status"], "current_version": row["current_version"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
        subjects = conn.execute(
            "SELECT subject_id FROM document_subjects WHERE document_id=%s ORDER BY position_no",
            (row["id"],),
        ).fetchall()
        record["subject_ids"] = [ObjectId(item["subject_id"]) for item in subjects]
        artifacts = conn.execute(
            "SELECT * FROM document_artifacts WHERE document_id=%s ORDER BY created_at, id",
            (row["id"],),
        ).fetchall()
        record["artifacts"] = []
        for artifact in artifacts:
            item = _restore(artifact["payload"] or {})
            item.update({
                "_id": _artifact_id(artifact["id"]), "type": artifact["artifact_type"],
                "document_version": artifact["version"],
                "storage": {
                    **(item.get("storage") or {}),
                    "provider": artifact["storage_provider"],
                    "uri": artifact["storage_key"],
                },
                "sha256": artifact["checksum_sha256"],
                "mime_type": artifact["mime_type"], "size_bytes": artifact["size_bytes"],
                "created_at": artifact["created_at"],
            })
            record["artifacts"].append(item)
        current = record.setdefault("current_processing", {})
        current["chunk_set_id"] = (ObjectId(row["active_chunk_set_id"])
                                   if row["active_chunk_set_id"] else None)
        return record

    @staticmethod
    def _save(conn, record: dict) -> None:
        document_id = str(record["_id"])
        conn.execute("DELETE FROM document_subjects WHERE document_id=%s", (document_id,))
        for table, row in projected_rows("documents", record):
            upsert(conn, table, row)

    def create(self, data: dict, uploaded_by_user_id: ObjectId | None) -> dict:
        record = self.validator.build_record(data, uploaded_by_user_id)
        with postgres_connection() as conn:
            self._save(conn, record)
        return record

    def find_by_id(self, document_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED'",
                (str(object_id(document_id, "document_id")),),
            ).fetchone()
            return self._load(conn, row)

    def list(self, page: int, page_size: int, status: str | None,
             search: str | None, *, subject_id: ObjectId | None = None,
             uploaded_by_user_id: ObjectId | None = None,
             visible_to_user_id: ObjectId | None = None,
             shared_subject_ids=None) -> tuple[list[dict], int]:
        filters = [sql.SQL("d.status <> 'ARCHIVED'")] if status != "ARCHIVED" else []
        params = []
        if status:
            filters.append(sql.SQL("d.status=%s"))
            params.append(status)
        if subject_id:
            filters.append(sql.SQL("EXISTS (SELECT 1 FROM document_subjects s "
                                   "WHERE s.document_id=d.id AND s.subject_id=%s)"))
            params.append(str(subject_id))
        if uploaded_by_user_id:
            filters.append(sql.SQL("d.uploaded_by_user_id=%s"))
            params.append(str(uploaded_by_user_id))
        if visible_to_user_id:
            filters.append(sql.SQL("(d.uploaded_by_user_id=%s OR "
                                   "d.payload->'shared_with_user_ids' ? %s OR "
                                   "(d.payload->>'shared_scope'='SUBJECT' AND EXISTS ("
                                   "SELECT 1 FROM document_subjects ds "
                                   "WHERE ds.document_id=d.id AND ds.subject_id=ANY(%s))))"))
            params.extend((str(visible_to_user_id), str(visible_to_user_id),
                           [str(item) for item in shared_subject_ids or ()]))
        if search:
            filters.append(sql.SQL("(d.title ILIKE %s OR d.original_filename ILIKE %s)"))
            pattern = f"%{search}%"
            params.extend((pattern, pattern))
        where = sql.SQL(" AND ").join(filters)
        with postgres_connection() as conn:
            total = conn.execute(
                sql.SQL("SELECT count(*) AS n FROM documents d WHERE {}").format(where),
                params,
            ).fetchone()["n"]
            rows = conn.execute(
                sql.SQL("SELECT d.* FROM documents d WHERE {} "
                        "ORDER BY d.created_at DESC, d.id DESC LIMIT %s OFFSET %s").format(where),
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [self._load(conn, row) for row in rows], total

    def count_owned(self, user_id: str | ObjectId) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                """SELECT count(*) AS n FROM documents
                   WHERE uploaded_by_user_id=%s AND status<>'ARCHIVED'""",
                (str(object_id(user_id, "user_id")),),
            ).fetchone()["n"]

    def count_using_subject(self, subject_id, chapter_id=None) -> int:
        """Tài liệu chưa lưu trữ đang gắn môn (hoặc chương của môn chính)."""
        subject_key = str(object_id(subject_id, "subject_id"))
        with postgres_connection() as conn:
            if chapter_id is None:
                return conn.execute(
                    """SELECT count(*) AS n FROM documents d
                       WHERE d.status<>'ARCHIVED' AND (d.subject_id=%s OR EXISTS (
                         SELECT 1 FROM document_subjects ds
                         WHERE ds.document_id=d.id AND ds.subject_id=%s))""",
                    (subject_key, subject_key),
                ).fetchone()["n"]
            return conn.execute(
                """SELECT count(*) AS n FROM documents
                   WHERE status<>'ARCHIVED' AND subject_id=%s
                     AND payload->>'chapter_id'=%s""",
                (subject_key, str(object_id(chapter_id, "chapter_id"))),
            ).fetchone()["n"]

    def count_by_status(self, statuses: list[str] | None = None) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM documents WHERE status<>'ARCHIVED'"
                + (" AND status=ANY(%s)" if statuses is not None else ""),
                (statuses,) if statuses is not None else (),
            ).fetchone()["n"]

    def list_owned(self, user_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM documents WHERE uploaded_by_user_id=%s
                   AND status<>'ARCHIVED' ORDER BY created_at DESC, id DESC""",
                (str(object_id(user_id, "user_id")),),
            ).fetchall()
            return [self._load(conn, row) for row in rows]

    @staticmethod
    def _admin_job_filter(statuses: list[str] | None, user_id: ObjectId | None,
                          date_from: datetime | None, date_to: datetime | None):
        clauses = ["true"]
        params = []
        if statuses is not None:
            clauses.append("j.status=ANY(%s)")
            params.append(statuses)
        if user_id is not None:
            clauses.append("d.uploaded_by_user_id=%s")
            params.append(str(user_id))
        if date_from is not None:
            clauses.append("j.created_at >= %s")
            params.append(date_from)
        if date_to is not None:
            clauses.append("j.created_at <= %s")
            params.append(date_to)
        return " AND ".join(clauses), params

    def count_admin_jobs(self, statuses: list[str] | None = None,
                         user_id: ObjectId | None = None,
                         date_from: datetime | None = None,
                         date_to: datetime | None = None) -> int:
        where, params = self._admin_job_filter(statuses, user_id, date_from, date_to)
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM document_jobs j "
                "JOIN documents d ON d.id=j.document_id WHERE " + where,
                params,
            ).fetchone()["n"]

    def admin_jobs(self, statuses: list[str] | None = None,
                   user_id: ObjectId | None = None,
                   date_from: datetime | None = None,
                   date_to: datetime | None = None, limit: int = 500) -> list[tuple[dict, dict]]:
        where, params = self._admin_job_filter(statuses, user_id, date_from, date_to)
        with postgres_connection() as conn:
            rows = conn.execute(
                "SELECT j.*, d.title AS document_title, "
                "d.original_filename AS document_filename, "
                "d.uploaded_by_user_id AS document_owner FROM document_jobs j "
                "JOIN documents d ON d.id=j.document_id WHERE " + where
                + " ORDER BY j.created_at DESC, j.id DESC LIMIT %s",
                [*params, limit],
            ).fetchall()
        return [(self._job(row), {
            "_id": ObjectId(row["document_id"]), "title": row["document_title"],
            "original_filename": row["document_filename"],
            "uploaded_by_user_id": (ObjectId(row["document_owner"])
                                    if row["document_owner"] else None),
        }) for row in rows]

    def update(self, document_id: str | ObjectId, fields: dict, *, audit: dict | None = None) -> dict | None:
        key = str(object_id(document_id, "document_id"))
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                return None
            current = self._load(conn, row)
            normalized = dict(fields)
            if "subject_ids" in normalized:
                normalized["subject_ids"] = list(dict.fromkeys(
                    object_id(value, "subject_id") for value in normalized.get("subject_ids") or []
                ))
                self.validator.validate_subject_ids(normalized["subject_ids"])
                normalized["subject_id"] = (normalized["subject_ids"][0]
                                            if normalized["subject_ids"] else None)
                if current.get("chapter_id") and normalized["subject_id"] != current.get("subject_id"):
                    normalized["chapter_id"] = None
            if "subject_id" in normalized:
                normalized["subject_id"] = (object_id(normalized["subject_id"], "subject_id")
                                            if normalized["subject_id"] else None)
                if "subject_ids" not in normalized:
                    normalized["subject_ids"] = ([normalized["subject_id"]]
                                                 if normalized["subject_id"] else [])
            if "chapter_id" in normalized and normalized["chapter_id"]:
                normalized["chapter_id"] = object_id(normalized["chapter_id"], "chapter_id")
            if normalized.get("uploaded_by_user_id"):
                normalized["uploaded_by_user_id"] = object_id(
                    normalized["uploaded_by_user_id"], "owner_user_id",
                )
            if "shared_with_user_ids" in normalized:
                normalized["shared_with_user_ids"] = [
                    object_id(value, "shared_with_user_id")
                    for value in normalized["shared_with_user_ids"] or []
                ]
            self.validator.validate_subject_chapter(
                normalized.get("subject_id", current.get("subject_id")),
                normalized.get("chapter_id", current.get("chapter_id")),
            )
            current.update(normalized)
            current["updated_at"] = utc_now()
            self._save(conn, current)
            if audit:
                write_postgres_audit_event(conn, **audit)
            return current

    def archive(self, document_id: str | ObjectId, *, audit: dict | None = None) -> bool:
        key = str(object_id(document_id, "document_id"))
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                return False
            busy = conn.execute(
                "SELECT 1 FROM document_jobs WHERE document_id=%s AND status=ANY(%s) LIMIT 1",
                (key, sorted(ACTIVE_DOCUMENT_JOB_STATUSES)),
            ).fetchone()
            if busy:
                raise DocumentBusyError(DOCUMENT_BUSY_MESSAGE)
            record = self._load(conn, row)
            record.update(status="ARCHIVED", archived_at=utc_now(), updated_at=utc_now(), pending_processing={})
            self._save(conn, record)
            if audit:
                write_postgres_audit_event(conn, **audit)
            return True

    def find_archived(self, document_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status='ARCHIVED'",
                (str(object_id(document_id, "document_id")),),
            ).fetchone()
            return self._load(conn, row)

    def restore(self, document_id: str | ObjectId, *, status: str, audit: dict | None = None) -> dict | None:
        key = str(object_id(document_id, "document_id"))
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status='ARCHIVED' FOR UPDATE", (key,),
            ).fetchone()
            if not row:
                return None
            record = self._load(conn, row)
            record.update(status=status, archived_at=None, updated_at=utc_now())
            self._save(conn, record)
            if audit:
                write_postgres_audit_event(conn, **audit)
            return record

    @staticmethod
    def _job(row: dict | None) -> dict | None:
        if row is None:
            return None
        record = _restore(row["payload"] or {})
        record.update({
            "_id": ObjectId(row["id"]),
            "document_id": ObjectId(row["document_id"]),
            "job_type": row["job_type"], "status": row["status"],
            "attempt_no": row["attempt_no"],
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
        })
        return record

    @staticmethod
    def _page(row: dict | None) -> dict | None:
        if row is None:
            return None
        record = _restore(row["payload"] or {})
        record.update({
            "_id": ObjectId(row["id"]),
            "document_id": ObjectId(row["document_id"]),
            "ocr_job_id": ObjectId(row["ocr_job_id"]) if row["ocr_job_id"] else None,
            "document_version": row["version"],
            "unit_number": row["unit_number"], "page_number": row["page_number"],
            "source_location": row["source_location"],
            "raw_text": row["raw_text"], "cleaned_text": row["clean_text"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
        return record

    def list_jobs(self, document_id: str | ObjectId, *, limit: int = 20) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM document_jobs WHERE document_id=%s "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (str(object_id(document_id, "document_id")), limit),
            ).fetchall()
        return [self._job(row) for row in rows]

    def find_job(self, job_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute("SELECT * FROM document_jobs WHERE id=%s",
                               (str(object_id(job_id, "job_id")),)).fetchone()
            return self._job(row)

    def list_pages(self, document_id: str | ObjectId, *, document_version: int | None = None,
                   limit: int = 100) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM document_pages WHERE document_id=%s "
                + ("AND version=%s " if document_version is not None else "")
                + "ORDER BY unit_number NULLS LAST, page_number NULLS LAST, id LIMIT %s",
                ((str(object_id(document_id, "document_id")), document_version, limit)
                 if document_version is not None else
                 (str(object_id(document_id, "document_id")), limit)),
            ).fetchall()
        return [self._page(row) for row in rows]

    def list_pages_for_job(self, document_id: str | ObjectId,
                           ocr_job_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM document_pages WHERE document_id=%s AND ocr_job_id=%s
                   ORDER BY unit_number NULLS LAST, page_number NULLS LAST, id""",
                (str(object_id(document_id, "document_id")),
                 str(object_id(ocr_job_id, "job_id"))),
            ).fetchall()
        return [self._page(row) for row in rows]

    def list_pages_for_document(self, document_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM document_pages WHERE document_id=%s
                   ORDER BY unit_number NULLS LAST, page_number NULLS LAST, id""",
                (str(object_id(document_id, "document_id")),),
            ).fetchall()
        return [self._page(row) for row in rows]

    def update_page(self, document_id: str | ObjectId, page_id: str | ObjectId, *,
                    document_version: int, cleaned_text: str) -> dict | None:
        document_key = str(object_id(document_id, "document_id"))
        with postgres_connection() as conn:
            document_row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (document_key,),
            ).fetchone()
            if not document_row:
                return None
            row = conn.execute(
                """SELECT * FROM document_pages
                   WHERE id=%s AND document_id=%s AND version=%s FOR UPDATE""",
                (str(object_id(page_id, "page_id")), document_key, document_version),
            ).fetchone()
            if not row:
                return None
            page = self._page(row)
            page["cleaned_text"] = cleaned_text
            page["updated_at"] = utc_now()
            for table, projection in projected_rows("document_pages", page):
                upsert(conn, table, projection)
            document = self._load(conn, document_row)
            document["updated_at"] = page["updated_at"]
            self._save(conn, document)
            return page

    def find_duplicate_original(self, uploaded_by_user_id, sha256: str) -> dict | None:
        if uploaded_by_user_id is None:
            return None
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT d.id, d.title FROM documents d "
                "JOIN document_artifacts a ON a.document_id=d.id "
                "WHERE d.uploaded_by_user_id=%s AND NOT (d.status = ANY(%s)) "
                "AND a.checksum_sha256=%s AND a.artifact_type LIKE 'ORIGINAL\\_%%' "
                "ORDER BY d.created_at LIMIT 1",
                (str(uploaded_by_user_id), list(DUPLICATE_IGNORED_STATUSES), sha256),
            ).fetchone()
        return {"_id": ObjectId(row["id"]), "title": row["title"]} if row else None

    def attach_original_artifact(self, document_id: str | ObjectId, *, uri: str,
                                 size_bytes: int, sha256: str,
                                 artifact_type: str = "ORIGINAL_PDF",
                                 mime_type: str = "application/pdf",
                                 provider: str = "LOCAL") -> None:
        self._attach_artifact(
            document_id, uri=uri, size_bytes=size_bytes, sha256=sha256,
            artifact_type=artifact_type, mime_type=mime_type,
            document_version=1, job_id=None, provider=provider,
        )

    def attach_processing_artifact(self, document_id: str | ObjectId, *,
                                   job_id: str | ObjectId, uri: str, size_bytes: int,
                                   sha256: str, artifact_type: str, mime_type: str,
                                   provider: str = "LOCAL") -> None:
        job = self.find_job(job_id)
        if not job or job["document_id"] != object_id(document_id, "document_id"):
            raise ValueError("Processing artifact không thuộc tài liệu/job")
        self._attach_artifact(
            document_id, uri=uri, size_bytes=size_bytes, sha256=sha256,
            artifact_type=artifact_type, mime_type=mime_type,
            document_version=job["document_version"], job_id=job["_id"],
            provider=provider,
        )

    def _attach_artifact(self, document_id: str | ObjectId, *, uri: str,
                         size_bytes: int, sha256: str, artifact_type: str,
                         mime_type: str, document_version: int,
                         job_id: ObjectId | None, provider: str = "LOCAL") -> None:
        key = str(object_id(document_id, "document_id"))
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                raise ValueError("Không tìm thấy tài liệu")
            record = self._load(conn, row)
            now = utc_now()
            artifact = {
                "_id": ObjectId(), "type": artifact_type,
                "document_version": document_version,
                "storage": {"provider": provider, "uri": uri, "gridfs_file_id": None},
                "mime_type": mime_type, "size_bytes": size_bytes, "sha256": sha256,
                "is_current": True, "created_at": now,
            }
            if job_id:
                artifact["job_id"] = job_id
            record.setdefault("artifacts", []).append(artifact)
            record["updated_at"] = now
            self._save(conn, record)

    def create_job(self, document_id: str | ObjectId, job_type: str,
                   config: dict | None = None) -> dict:
        key = str(object_id(document_id, "document_id"))
        normalized_type = job_type.upper()
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                raise ValueError("Không tìm thấy tài liệu")
            document = self._load(conn, row)
            latest = conn.execute(
                """SELECT attempt_no FROM document_jobs
                   WHERE document_id=%s AND job_type=%s
                     AND (payload->>'document_version')::integer=%s
                   ORDER BY attempt_no DESC LIMIT 1""",
                (key, normalized_type, document["current_version"]),
            ).fetchone()
            now = utc_now()
            current = document.get("current_processing") or {}
            active_pointer = (current.get("ocr_job_id") if normalized_type == "OCR"
                              else current.get("chunk_set_id") if normalized_type in {"CHUNK", "INDEX"}
                              else None)
            record = {
                "_id": ObjectId(), "schema_version": document["schema_version"],
                "document_id": document["_id"],
                "document_version": document["current_version"],
                "job_type": normalized_type,
                "attempt_no": latest["attempt_no"] + 1 if latest else 1,
                "status": "QUEUED", "config": config or {}, "progress": 0,
                "stats": None, "error": None, "queued_at": now,
                "started_at": None, "finished_at": None,
                "preserves_active_pipeline": bool(active_pointer),
                "previous_document_state": {
                    "status": document.get("status"),
                    "pipeline_status": (document.get("pipeline_summary") or {}).get(
                        normalized_type.lower() + "_status"
                    ),
                },
            }
            for table, projection in projected_rows("document_jobs", record):
                upsert(conn, table, projection)
            attempts = document.setdefault("pipeline_attempts", {})
            attempts[normalized_type.lower()] = {"status": "QUEUED", "job_id": record["_id"]}
            document["latest_error"] = None
            document["updated_at"] = now
            if not active_pointer:
                document["status"] = "PROCESSING"
                document.setdefault("pipeline_summary", {})[normalized_type.lower() + "_status"] = "QUEUED"
            self._save(conn, document)
            return record

    def update_job(self, job_id: str | ObjectId, status: str, *,
                   progress: int | None = None, stats: dict | None = None,
                   error_message: str | None = None,
                   expected_statuses: set[str] | None = None,
                   stale_before: datetime | None = None) -> dict | None:
        job_key = str(object_id(job_id, "job_id"))
        with postgres_connection() as conn:
            identity = conn.execute(
                "SELECT document_id FROM document_jobs WHERE id=%s", (job_key,),
            ).fetchone()
            if not identity:
                return None
            document_row = conn.execute(
                "SELECT * FROM documents WHERE id=%s FOR UPDATE",
                (identity["document_id"],),
            ).fetchone()
            job_row = conn.execute(
                "SELECT * FROM document_jobs WHERE id=%s FOR UPDATE", (job_key,),
            ).fetchone()
            if not job_row:
                return None
            if expected_statuses is not None and job_row["status"] not in expected_statuses:
                return None
            if stale_before is not None and job_row["updated_at"] >= stale_before:
                return None
            job = self._job(job_row)
            normalized = status.upper()
            if job["status"] == "CANCELLED" and normalized != "CANCELLED":
                return job
            now = utc_now()
            job["status"] = normalized
            job["updated_at"] = now
            if progress is not None:
                job["progress"] = max(0, min(100, progress))
            if stats is not None:
                job["stats"] = stats
            if error_message is not None:
                job["error"] = {"message": error_message, "at": now}
            if normalized == "PROCESSING" and not job.get("started_at"):
                job["started_at"] = now
            if normalized in {"COMPLETED", "FAILED", "CANCELLED"}:
                job["finished_at"] = now
                if normalized == "COMPLETED":
                    job["progress"] = 100
            for table, projection in projected_rows("document_jobs", job):
                upsert(conn, table, projection)
            if document_row and document_row["status"] != "ARCHIVED":
                document = self._load(conn, document_row)
                kind = job["job_type"].lower()
                attempt = document.setdefault("pipeline_attempts", {}).setdefault(kind, {})
                attempt.update(status=normalized, job_id=job["_id"])
                summary = document.setdefault("pipeline_summary", {})
                preserves_active = bool(job.get("preserves_active_pipeline"))
                if not preserves_active:
                    summary[kind + "_status"] = normalized
                if normalized in {"FAILED", "CANCELLED"}:
                    message = error_message or ("Job đã bị hủy" if normalized == "CANCELLED" else None)
                    if preserves_active:
                        previous = job.get("previous_document_state") or {}
                        document["status"] = previous.get("status") or "READY"
                        if previous.get("pipeline_status"):
                            summary[kind + "_status"] = previous["pipeline_status"]
                    else:
                        document["status"] = "FAILED"
                    document["latest_error"] = {
                        "job_id": job["_id"], "job_type": job["job_type"],
                        "message": message, "at": now,
                    }
                if normalized == "COMPLETED" and job["job_type"] == "OCR":
                    pointer = ("pending_processing" if document.get("current_processing", {}).get("chunk_set_id")
                               else "current_processing")
                    document.setdefault(pointer, {})["ocr_job_id"] = job["_id"]
                document["updated_at"] = now
                self._save(conn, document)
            if normalized == "CANCELLED" and job["job_type"] == "CHUNK":
                conn.execute(
                    """INSERT INTO outbox_events
                       (id, event_key, event_type, aggregate_type, aggregate_id, payload)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (event_key) DO NOTHING""",
                    (str(ObjectId()), f"document.chunk_cancel:{job_key}",
                     "document.chunk_cancel", "document", identity["document_id"],
                     Jsonb({"job_id": job_key, "document_id": identity["document_id"],
                            "error_message": error_message})),
                )
            return job

    def recover_stale_jobs(self, cutoff: datetime, message: str) -> int:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT id FROM document_jobs
                   WHERE status IN ('QUEUED','PROCESSING') AND updated_at < %s
                   ORDER BY updated_at, id""",
                (cutoff,),
            ).fetchall()
        recovered = 0
        for row in rows:
            updated = self.update_job(
                row["id"], "FAILED", error_message=message,
                expected_statuses={"QUEUED", "PROCESSING"},
                stale_before=cutoff,
            )
            if updated:
                recovered += 1
        return recovered

    def finish_chunk_job(self, document_id: str | ObjectId, job_id: str | ObjectId,
                         chunk_set_id: str | ObjectId, source_ocr_job_id: str | ObjectId | None,
                         vector_collection_id: str | ObjectId | None, *,
                         total_chunks: int, stats: dict, dry_run: bool) -> bool:
        """Promote the PostgreSQL pointer only after vector writes completed."""
        document_key = str(object_id(document_id, "document_id"))
        job_key = str(object_id(job_id, "job_id"))
        with postgres_connection() as conn:
            document_row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (document_key,),
            ).fetchone()
            if not document_row:
                raise RuntimeError("DOCUMENT_ARCHIVED")
            job_row = conn.execute(
                "SELECT * FROM document_jobs WHERE id=%s FOR UPDATE", (job_key,),
            ).fetchone()
            if (not job_row or job_row["document_id"] != document_key
                    or job_row["job_type"] != "CHUNK"):
                raise ValueError("CHUNK job không thuộc tài liệu")
            if job_row["status"] == "CANCELLED":
                return False
            document = self._load(conn, document_row)
            target_set = object_id(chunk_set_id, "chunk_set_id")
            if (job_row["status"] == "COMPLETED" and
                    target_set in {
                        (document.get("current_processing") or {}).get("chunk_set_id"),
                        (document.get("pending_processing") or {}).get("chunk_set_id"),
                    }):
                return True
            job = self._job(job_row)
            now = utc_now()
            job.update(status="COMPLETED", progress=100, stats=stats,
                       finished_at=now, updated_at=now)
            for table, projection in projected_rows("document_jobs", job):
                upsert(conn, table, projection)
            document["updated_at"] = now
            if not dry_run:
                source_id = (object_id(source_ocr_job_id, "ocr_job_id")
                             if source_ocr_job_id else None)
                set_id = target_set
                vector_id = (object_id(vector_collection_id, "vector_collection_id")
                             if vector_collection_id else None)
                if (document.get("current_processing") or {}).get("chunk_set_id"):
                    document["pending_processing"] = {
                        "ocr_job_id": source_id, "chunk_set_id": set_id,
                        "vector_collection_id": vector_id,
                        "validation_status": "AWAITING_VALIDATION",
                        "completed_at": now,
                    }
                    document.setdefault("pipeline_attempts", {})["chunk"] = {
                        "status": "COMPLETED", "job_id": object_id(job_id, "job_id"),
                    }
                    document["status"] = "READY"
                else:
                    document["current_processing"] = {
                        "ocr_job_id": source_id, "chunk_set_id": set_id,
                        "vector_collection_id": vector_id,
                    }
                    summary = document.setdefault("pipeline_summary", {})
                    summary.update(chunk_status="COMPLETED", total_chunks=total_chunks,
                                   index_status="COMPLETED")
                    document["pending_processing"] = {}
                    document["status"] = "READY"
            self._save(conn, document)
            return True

    def promote_lineage(self, document_id: str | ObjectId, target: dict, *,
                        operation_id: str, validation: dict,
                        expected_current: dict, expected_version: int,
                        actor: str, reason: str) -> dict:
        key = str(object_id(document_id, "document_id"))
        after = _lineage_snapshot(target)
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (key,),
            ).fetchone()
            if not row:
                raise LookupError("document missing or archived")
            document = self._load(conn, row)
            before = _lineage_snapshot(document.get("current_processing") or {})
            if (before != _lineage_snapshot(expected_current)
                    or document["current_version"] != expected_version):
                raise RuntimeError("active lineage changed concurrently")
            if _lineage_snapshot(document.get("pending_processing") or {}) != after:
                raise RuntimeError("active lineage changed concurrently")
            document["current_processing"] = dict(after)
            document["pending_processing"] = {}
            document["status"] = "READY"
            document.setdefault("pipeline_summary", {}).update(
                ocr_status="COMPLETED", chunk_status="COMPLETED", index_status="COMPLETED",
            )
            document["updated_at"] = utc_now()
            self._save(conn, document)
            event_payload = normalized({
                "operation_id": operation_id, "event_type": "PROMOTE",
                "document_id": document["_id"], "actor": actor, "reason": reason,
                "from_snapshot": before, "to_snapshot": after,
                "validation": validation, "rollback_available": True,
                "created_at": document["updated_at"],
            })
            conn.execute(
                """INSERT INTO document_lineage_events
                   (operation_id, document_id, event_type, actor, reason,
                    from_snapshot, to_snapshot, validation, rollback_available,
                    payload, created_at)
                   VALUES (%s,%s,'PROMOTE',%s,%s,%s,%s,%s,true,%s,%s)""",
                (operation_id, key, actor, reason,
                 Jsonb(normalized(before)), Jsonb(normalized(after)),
                 Jsonb(normalized(validation)), Jsonb(event_payload),
                 document["updated_at"]),
            )
            write_postgres_audit_event(
                conn, action="document.lineage_promote", entity_type="document",
                entity_id=key, before=normalized(before), after=normalized(after),
                metadata={"operation_id": operation_id, "actor": actor, "reason": reason},
            )
        return {"operation_id": operation_id, "from_snapshot": before,
                "to_snapshot": after}

    def promotion_event(self, operation_id: str) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT * FROM document_lineage_events
                   WHERE operation_id=%s AND event_type='PROMOTE' AND rollback_available""",
                (operation_id,),
            ).fetchone()
        if not row:
            return None
        return {**row, "from_snapshot": _restore(row["from_snapshot"]),
                "to_snapshot": _restore(row["to_snapshot"])}

    def rollback_lineage(self, operation_id: str, *, rollback_id: str,
                         actor: str, reason: str) -> dict:
        with postgres_connection() as conn:
            event = conn.execute(
                """SELECT * FROM document_lineage_events
                   WHERE operation_id=%s AND event_type='PROMOTE'
                     AND rollback_available FOR UPDATE""",
                (operation_id,),
            ).fetchone()
            if not event:
                raise LookupError("promotion is missing or no longer rollbackable")
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (event["document_id"],),
            ).fetchone()
            if not row:
                raise LookupError("document missing or archived")
            document = self._load(conn, row)
            before = _restore(event["from_snapshot"])
            after = _restore(event["to_snapshot"])
            if _lineage_snapshot(document.get("current_processing") or {}) != _lineage_snapshot(after):
                raise RuntimeError("rollback target is not the active lineage")
            document["current_processing"] = dict(before)
            document["updated_at"] = utc_now()
            self._save(conn, document)
            conn.execute(
                "UPDATE document_lineage_events SET rollback_available=false WHERE operation_id=%s",
                (operation_id,),
            )
            event_payload = normalized({
                "operation_id": rollback_id, "event_type": "ROLLBACK",
                "document_id": document["_id"], "actor": actor, "reason": reason,
                "from_snapshot": after, "to_snapshot": before,
                "promotion_operation_id": operation_id,
                "created_at": document["updated_at"],
            })
            conn.execute(
                """INSERT INTO document_lineage_events
                   (operation_id, document_id, event_type, actor, reason,
                    from_snapshot, to_snapshot, promotion_operation_id,
                    payload, created_at)
                   VALUES (%s,%s,'ROLLBACK',%s,%s,%s,%s,%s,%s,%s)""",
                (rollback_id, event["document_id"], actor, reason,
                 Jsonb(normalized(after)), Jsonb(normalized(before)), operation_id,
                 Jsonb(event_payload), document["updated_at"]),
            )
            write_postgres_audit_event(
                conn, action="document.lineage_rollback", entity_type="document",
                entity_id=event["document_id"], before=normalized(after),
                after=normalized(before),
                metadata={"operation_id": rollback_id,
                          "promotion_operation_id": operation_id,
                          "actor": actor, "reason": reason},
            )
        return {"operation_id": rollback_id, "restored_snapshot": before}

    def queue_archive_lineage(self, document_id: str | ObjectId, snapshot: dict, *,
                              operation_id: str, actor: str, reason: str) -> dict:
        key = str(object_id(document_id, "document_id"))
        set_id = str(object_id(snapshot["chunk_set_id"], "chunk_set_id"))
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id=%s FOR UPDATE", (key,),
            ).fetchone()
            if not row:
                raise LookupError("document missing")
            document = self._load(conn, row)
            referenced = {
                (document.get("current_processing") or {}).get("chunk_set_id"),
                (document.get("pending_processing") or {}).get("chunk_set_id"),
            }
            if object_id(set_id, "chunk_set_id") in referenced:
                raise ValueError("active or pending lineage cannot be archived")
            rollback_ref = conn.execute(
                """SELECT 1 FROM document_lineage_events
                   WHERE document_id=%s AND rollback_available
                     AND (from_snapshot->>'chunk_set_id'=%s
                          OR to_snapshot->>'chunk_set_id'=%s)
                   LIMIT 1""",
                (key, set_id, set_id),
            ).fetchone()
            if rollback_ref:
                raise ValueError("rollback lineage cannot be archived")
            created = utc_now()
            event_payload = normalized({
                "operation_id": operation_id, "event_type": "ARCHIVE",
                "document_id": key, "actor": actor, "reason": reason,
                "from_snapshot": snapshot, "to_snapshot": {"archive_requested_at": created},
                "status": "PENDING", "created_at": created,
            })
            conn.execute(
                """INSERT INTO document_lineage_events
                   (operation_id, document_id, event_type, actor, reason,
                    from_snapshot, to_snapshot, status, payload, created_at)
                   VALUES (%s,%s,'ARCHIVE',%s,%s,%s,%s,'PENDING',%s,%s)""",
                (operation_id, key, actor, reason, Jsonb(normalized(snapshot)),
                 Jsonb({"archive_requested_at": created.isoformat()}),
                 Jsonb(event_payload), created),
            )
            conn.execute(
                """INSERT INTO outbox_events
                   (id, event_key, event_type, aggregate_type, aggregate_id, payload)
                   VALUES (%s,%s,'document.chunk_archive','document',%s,%s)""",
                (str(ObjectId()), f"document.chunk_archive:{operation_id}", key,
                 Jsonb({"operation_id": operation_id, "document_id": key,
                        "chunk_set_id": set_id, "actor": actor, "reason": reason})),
            )
            write_postgres_audit_event(
                conn, action="document.lineage_archive_requested",
                entity_type="document", entity_id=key,
                metadata={"operation_id": operation_id, "actor": actor,
                          "reason": reason, "chunk_set_id": set_id},
            )
        return {"operation_id": operation_id, "status": "PENDING",
                "archived_at": None}

    def request_permanent_delete(self, document_id: str | ObjectId, snapshot: dict, *,
                                 operation_id: str, actor: str, reason: str) -> dict:
        key = str(object_id(document_id, "document_id"))
        set_id = str(object_id(snapshot["chunk_set_id"], "chunk_set_id"))
        with postgres_connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id=%s FOR UPDATE",
                               (key,)).fetchone()
            if not row:
                raise LookupError("document missing")
            document = self._load(conn, row)
            referenced = {
                (document.get("current_processing") or {}).get("chunk_set_id"),
                (document.get("pending_processing") or {}).get("chunk_set_id"),
            }
            if object_id(set_id, "chunk_set_id") in referenced:
                raise ValueError("active or pending lineage cannot be deleted")
            rollback_ref = conn.execute(
                """SELECT 1 FROM document_lineage_events
                   WHERE document_id=%s AND rollback_available
                     AND (from_snapshot->>'chunk_set_id'=%s
                          OR to_snapshot->>'chunk_set_id'=%s)
                   LIMIT 1""",
                (key, set_id, set_id),
            ).fetchone()
            if rollback_ref:
                raise ValueError("rollback lineage cannot be deleted")
            now = utc_now()
            status = "AWAITING_OFFLINE_BACKUP_AND_EXECUTION"
            event_payload = normalized({
                "operation_id": operation_id,
                "event_type": "PERMANENT_DELETE_REQUESTED",
                "document_id": key, "actor": actor, "reason": reason,
                "to_snapshot": snapshot, "status": status, "created_at": now,
            })
            conn.execute(
                """INSERT INTO document_lineage_events
                   (operation_id, document_id, event_type, actor, reason,
                    to_snapshot, status, payload, created_at)
                   VALUES (%s,%s,'PERMANENT_DELETE_REQUESTED',%s,%s,%s,%s,%s,%s)""",
                (operation_id, key, actor, reason, Jsonb(normalized(snapshot)),
                 status, Jsonb(event_payload), now),
            )
            write_postgres_audit_event(
                conn, action="document.lineage_delete_requested",
                entity_type="document", entity_id=key,
                metadata={"operation_id": operation_id, "actor": actor,
                          "reason": reason, "chunk_set_id": set_id},
            )
        return {"operation_id": operation_id, "status": status, "deleted": False}

    def queue_permanent_delete(self, request_operation_id: str) -> dict:
        with postgres_connection() as conn:
            request = conn.execute(
                """SELECT * FROM document_lineage_events
                   WHERE operation_id=%s AND event_type='PERMANENT_DELETE_REQUESTED'
                     AND status='AWAITING_OFFLINE_BACKUP_AND_EXECUTION' FOR UPDATE""",
                (request_operation_id,),
            ).fetchone()
            if not request:
                raise LookupError("delete request missing or already executed")
            document_row = conn.execute(
                "SELECT * FROM documents WHERE id=%s FOR UPDATE",
                (request["document_id"],),
            ).fetchone()
            if not document_row:
                raise LookupError("document missing")
            document = self._load(conn, document_row)
            target = request["to_snapshot"]
            set_id = target["chunk_set_id"]
            ocr_id = target["ocr_job_id"]
            referenced = {
                *((document.get("current_processing") or {}).values()),
                *((document.get("pending_processing") or {}).values()),
            }
            if ObjectId(set_id) in referenced or ObjectId(ocr_id) in referenced:
                raise ValueError("active or pending lineage cannot be deleted")
            rollback_ref = conn.execute(
                """SELECT 1 FROM document_lineage_events
                   WHERE document_id=%s AND rollback_available
                     AND (from_snapshot->>'chunk_set_id'=%s
                          OR to_snapshot->>'chunk_set_id'=%s) LIMIT 1""",
                (request["document_id"], set_id, set_id),
            ).fetchone()
            if rollback_ref:
                raise ValueError("rollback lineage cannot be deleted")
            conn.execute(
                """UPDATE document_lineage_events
                   SET status='DELETE_QUEUED',
                       payload=jsonb_set(payload, '{status}', '"DELETE_QUEUED"'::jsonb)
                   WHERE operation_id=%s""",
                (request_operation_id,),
            )
            conn.execute(
                """INSERT INTO outbox_events
                   (id, event_key, event_type, aggregate_type, aggregate_id, payload)
                   VALUES (%s,%s,'document.chunk_delete','document',%s,%s)""",
                (str(ObjectId()), f"document.chunk_delete:{request_operation_id}",
                 request["document_id"],
                 Jsonb({"operation_id": request_operation_id,
                        "document_id": request["document_id"],
                        "ocr_job_id": ocr_id, "chunk_set_id": set_id,
                        "vector_collection_id": target["vector_collection_id"]})),
            )
            write_postgres_audit_event(
                conn, action="document.lineage_delete_queued",
                entity_type="document", entity_id=request["document_id"],
                metadata={"operation_id": request_operation_id,
                          "chunk_set_id": set_id},
            )
        return {"operation_id": request_operation_id,
                "status": "DELETE_QUEUED", "deleted": False}

    def save_pages(self, document_id: str, ocr_job_id: str, pages: list[dict]) -> int:
        document_key = str(object_id(document_id, "document_id"))
        job_key = str(object_id(ocr_job_id, "job_id"))
        with postgres_connection() as conn:
            document_row = conn.execute(
                "SELECT * FROM documents WHERE id=%s AND status<>'ARCHIVED' FOR UPDATE",
                (document_key,),
            ).fetchone()
            if not document_row:
                raise ValueError("Không tìm thấy tài liệu")
            job_row = conn.execute("SELECT * FROM document_jobs WHERE id=%s FOR UPDATE",
                                   (job_key,)).fetchone()
            if not job_row or job_row["document_id"] != document_key or job_row["job_type"] != "OCR":
                raise ValueError("OCR job không thuộc tài liệu")
            job = self._job(job_row)
            now = utc_now()
            conn.execute("DELETE FROM document_pages WHERE document_id=%s AND ocr_job_id=%s",
                         (document_key, job_key))
            for index, page in enumerate(pages, start=1):
                record = {
                    "_id": ObjectId(), "document_id": ObjectId(document_key),
                    "document_version": job["document_version"],
                    "ocr_job_id": ObjectId(job_key),
                    "unit_number": int(page.get("unit_number") or index),
                    "page_number": (int(page["page_number"])
                                    if page.get("page_number") is not None else None),
                    "source_location": json_safe(page.get("source_location") or {}),
                    "raw_text": page.get("original_text") or page.get("text", ""),
                    "cleaned_text": page.get("text", ""),
                    "content_blocks": json_safe(page.get("content_blocks") or []),
                    "assets": json_safe(page.get("assets") or []),
                    "raw_extraction": compact_raw_extraction(page.get("raw_extraction")),
                    "quality": json_safe(page.get("quality") or {}),
                    "formula_blocks": json_safe(page.get("formula_blocks", [])),
                    "created_at": now,
                }
                for table, projection in projected_rows("document_pages", record):
                    upsert(conn, table, projection)
            document = self._load(conn, document_row)
            document["page_count"] = len(pages)
            document["updated_at"] = now
            self._save(conn, document)
            return len(pages)
