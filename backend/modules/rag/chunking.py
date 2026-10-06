import hashlib
import logging
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from time import perf_counter
from typing import Iterable

from bson import ObjectId
from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from fastapi.concurrency import run_in_threadpool
from pymongo import ReturnDocument

from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import get_database
from core.dependencies import CurrentUser, require_document_manager
from modules.documents.ingest.quality import validate_chunks
from modules.documents.repository import object_id
from modules.documents.store import get_document_repository
from modules.documents.service import DocumentService, get_document_service
from modules.dictionary.dictionary import run_dictionary_auto_learning
from modules.dictionary.service import dictionary_key_for_document, get_active_keywords
from modules.rag.chunking_export import export_chunks_to_file
from modules.rag.chromadb_engine import (
    chroma_persist_uri,
    embedding_config_hash,
    embedding_config_matches,
    embedding_config_snapshot,
    embedding_token_lengths,
    embedding_token_offsets,
    model_scoped_collection_name,
    store_chunks,
)
from modules.rag.mongodb import (
    complete_chunk_set,
    fail_chunk_set,
    get_document_record,
    iter_document_pages,
    is_document_job_cancelled,
    persist_chunks,
    start_chunk_set,
    update_chunking_status,
)
from modules.rag.lineage import auto_promote_pending_lineage
from modules.rag.schemas import ChunkingStats, DocumentChunkRequest, DocumentChunkResponse
from modules.rag.retrieval_policy import filter_retrieval_pages

router = APIRouter(prefix=f"{settings.api_prefix}/chunk", tags=["chunking"])
logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

PAGE_MARKER_PATTERN = re.compile(r"<!--\s*PAGE:(\d+)\s*-->")
CODE_BLOCK_PATTERN = re.compile(r"```[^\n]*\n[\s\S]*?```")
FORMULA_BLOCK_PATTERN = re.compile(r"<FORMULA_BLOCK[\s\S]*?</FORMULA_BLOCK>")
LATEX_BLOCK_PATTERN = re.compile(r"\$\$[\s\S]*?\$\$")
TABLE_BLOCK_PATTERN = re.compile(r"(\|[^\n]+\|\n\|[-:\s|]+\|\n(?:\|[^\n]+\|\n?)+)")
HEADING_PATTERN = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

TOP_LEVEL_PATTERNS = [
	re.compile(r"^#\s+.+$"),
	re.compile(r"^(CHUONG|CHAPTER|PHAN|BAI)\s+([IVX0-9]+)\b"),
]

SUB_LEVEL_2_PATTERN = re.compile(r"^(I|II|III|IV|V|VI|VII|VIII|IX|X)[\.\s]+", re.IGNORECASE)
SUB_LEVEL_3_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?[\.\s]+")

BIG_O_PATTERN = re.compile(r"\bO\([^)]*\)")
EXAMPLE_PATTERN = re.compile(r"\b(thí dụ|ví dụ|vd)\b", re.IGNORECASE)
EXERCISE_PATTERN = re.compile(r"\b(bài tập|câu hỏi|thực hành|luyện tập|ôn tập)\b", re.IGNORECASE)
DEFINITION_PATTERN = re.compile(r"\b(định nghĩa|khái niệm|là gì)\b", re.IGNORECASE)


# -------------------------------------------------------------
# API: Chunk tài liệu OCR và lưu vào ChromaDB
# -------------------------------------------------------------
@router.post("/document", response_model=DocumentChunkResponse, summary="Chunk OCR document and store to ChromaDB")
async def chunk_document(
    req: DocumentChunkRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_document_manager),
    document_service: DocumentService = Depends(get_document_service),
):
    doc = get_document_record(req.document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    try:
        if req.dry_run:
            document_service.can_use(req.document_id, current_user)
        else:
            document_service.can_manage(req.document_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        result = await run_in_threadpool(
            chunk_document_and_store,
            document_id=req.document_id,
            chunk_size=req.chunk_size,
            chunk_overlap=req.chunk_overlap,
            collection_name=req.collection_name,
            buffer_max_pages=req.buffer_max_pages,
            buffer_max_chars=req.buffer_max_chars,
            max_code_block_lines=req.max_code_block_lines,
            dry_run=req.dry_run,
        )

        dictionary_key = dictionary_key_for_document(doc)
        if not req.dry_run and result.total_chunks > 0 and dictionary_key:
            logger.info("Đang kích hoạt tiến trình AI học từ khóa ngầm...")
            background_tasks.add_task(
                run_dictionary_auto_learning,
                document_id=req.document_id,
                course_id=dictionary_key,
            )

        return result
    except ValueError as ex:
        logger.warning("Chunking rejected: %s", ex)
        update_chunking_status(req.document_id, status="failed", error_message=str(ex))
        raise HTTPException(status_code=400, detail=str(ex)) from ex
    except Exception as ex:
        logger.exception("Chunking failed: %s", ex)
        update_chunking_status(req.document_id, status="failed", error_message=str(ex))
        raise HTTPException(status_code=500, detail="Không thể chia chunk tài liệu") from ex


def _chunk_run_config(config: dict) -> dict:
    return {
        "strategy": config.get("strategy") or "recursive",
        "chunk_size": int(config.get("chunk_size") or settings.chunk_size_default),
        "chunk_overlap": int(config.get("chunk_overlap") or settings.chunk_overlap_default),
        "buffer_max_pages": int(config.get("buffer_max_pages") or settings.chunk_buffer_max_pages),
        "buffer_max_chars": int(config.get("buffer_max_chars") or settings.chunk_buffer_max_chars),
        "max_code_block_lines": int(config.get("max_code_block_lines") or settings.max_code_block_lines),
        "embedding_max_tokens": int(config.get("embedding_max_tokens") or settings.embedding_max_tokens),
        "embedding_token_overlap": int(
            config.get("embedding_token_overlap") or settings.embedding_token_overlap
        ),
        "dry_run": bool(config.get("dry_run", False)),
        "collection_name": config.get("collection_name") or settings.chromadb_collection_name,
    }


def _empty_chunk_stats() -> ChunkingStats:
    return ChunkingStats(
        original_length=0,
        total_chunks=0,
        junk_removed=0,
        avg_chunk_size=0,
        min_chunk_size=0,
        max_chunk_size=0,
        content_type_distribution={},
    )


def _cancelled_chunk_response(
    document_id: str,
    chunk_job_id: str,
    chunk_set_id: str,
    collection_name: str,
) -> DocumentChunkResponse:
    return DocumentChunkResponse(
        document_id=document_id,
        chunk_job_id=chunk_job_id,
        chunk_set_id=chunk_set_id,
        vector_collection_id=None,
        collection_name=collection_name,
        total_chunks=0,
        stored_chunks=0,
        stats=_empty_chunk_stats(),
    )


def _promote_reprocessed_lineage(document_id: str, promote_actor: str | None) -> None:
    if not promote_actor or not settings.auto_promote_reprocessed_lineage:
        return
    outcome = auto_promote_pending_lineage(
        document_id,
        actor=promote_actor,
        reason="Tự động promote sau khi xử lý lại tài liệu từ giao diện",
    )
    if outcome.get("status") == "FAILED":
        logger.warning("Auto-promotion of document %s kept pending: %s", document_id, outcome.get("reason"))


def process_chunk_retry_background(
    document_id: str,
    chunk_job_id: str,
    chunk_set_id: str,
    run_config: dict,
    promote_actor: str | None = None,
) -> None:
    try:
        result = _process_existing_chunk_set(document_id, chunk_job_id, chunk_set_id, run_config)
    except Exception as exc:
        logger.exception("Chunk retry job %s failed", chunk_job_id)
        update_chunking_status(document_id, status="failed", error_message=str(exc))
        return
    if result.total_chunks and not run_config.get("dry_run"):
        _promote_reprocessed_lineage(document_id, promote_actor)


def queue_chunk_retry(
    background_tasks: BackgroundTasks,
    document_id: str,
    config: dict,
    *,
    promote_actor: str | None = None,
) -> dict:
    run_config = _chunk_run_config(config)
    chunk_job_id, chunk_set_id = start_chunk_set(document_id, run_config)
    background_tasks.add_task(
        process_chunk_retry_background,
        document_id=document_id,
        chunk_job_id=chunk_job_id,
        chunk_set_id=chunk_set_id,
        run_config=run_config,
        promote_actor=promote_actor,
    )
    return {"chunk_job_id": chunk_job_id, "chunk_set_id": chunk_set_id}


def auto_chunk_extracted_document(document_id: str, *, promote_actor: str | None = None) -> DocumentChunkResponse | None:
    """Chunk and index right after a successful extraction; failures are recorded on the chunk job."""
    try:
        result = chunk_document_and_store(
            document_id=document_id,
            chunk_size=settings.chunk_size_default,
            chunk_overlap=settings.chunk_overlap_default,
        )
    except Exception as exc:
        logger.exception("Automatic chunking of document %s failed", document_id)
        update_chunking_status(document_id, status="failed", error_message=str(exc))
        return None
    if result.total_chunks:
        _promote_reprocessed_lineage(document_id, promote_actor)
    return result


def _vector_collection_for_current_model(collection_name: str) -> tuple[dict, str]:
    db = get_database()
    now = utc_now()
    current_config_hash = embedding_config_hash()
    resolved_collection = model_scoped_collection_name(collection_name)
    record = db.vector_collections.find_one(
        {"provider": "CHROMA", "collection_name": resolved_collection, "is_active": True},
        sort=[("created_at", -1)],
    )
    if record and not embedding_config_matches(record.get("embedding_model") or {}, record.get("embedding_config_hash")):
        raise ValueError("Vector collection name collision for a different embedding configuration")
    if record:
        return record, resolved_collection
    record = db.vector_collections.find_one_and_update(
        {"provider": "CHROMA", "collection_name": resolved_collection},
        {
            "$setOnInsert": {
                "_id": ObjectId(),
                "schema_version": SCHEMA_VERSION,
                "persist_uri": chroma_persist_uri(),
                "embedding_model": {"provider": "SENTENCE_TRANSFORMERS", **embedding_config_snapshot()},
                "embedding_config_hash": current_config_hash,
                "distance_metric": "COSINE",
                "is_active": True,
                "created_at": now,
                "retired_at": None,
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return record, resolved_collection


def _heading_path_text(path: list | None) -> str:
    return " > ".join(str(item) for item in (path or []) if item)


def _metadata_for_existing_chunk(chunk: dict, vector_id: ObjectId) -> dict:
    heading = chunk.get("heading") or {}
    heading_path = heading.get("path") or []
    page_range = chunk.get("page_range") or {}
    source = chunk.get("source") or {}
    continuation = chunk.get("continuation") or {}
    return {
        "document_id": str(chunk["document_id"]),
        "chunk_id": str(chunk["_id"]),
        "chunk_set_id": str(chunk["chunk_set_id"]),
        "vector_collection_id": str(vector_id),
        "content_hash": chunk.get("content_hash"),
        "heading": heading.get("title") or "",
        "heading_path": heading_path,
        "heading_path_text": _heading_path_text(heading_path),
        "heading_norm": heading.get("normalized") or "",
        "page_start": page_range.get("start"),
        "page_end": page_range.get("end"),
        "page_marks": page_range.get("pages") or [],
        "content_type": chunk.get("content_type", "text"),
        "semantic_type": chunk.get("semantic_type", "theory"),
        "information_density": chunk.get("information_density", 0),
        "token_count": chunk.get("token_count", 0),
        **source,
        **continuation,
    }


def _reindex_payload(chunks: list[dict], vector: dict) -> tuple[list[str], list[str], list[dict], list[ObjectId]]:
    db = get_database()
    now = utc_now()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    embedding_ids: list[ObjectId] = []
    for chunk in chunks:
        external_id = f"{chunk['_id']}:{vector['_id']}"
        embedding = db.chunk_embeddings.find_one_and_update(
            {
                "chunk_id": chunk["_id"],
                "chunk_set_id": chunk["chunk_set_id"],
                "vector_collection_id": vector["_id"],
            },
            {
                "$setOnInsert": {
                    "_id": ObjectId(),
                    "schema_version": SCHEMA_VERSION,
                    "chunk_id": chunk["_id"],
                    "chunk_set_id": chunk["chunk_set_id"],
                    "vector_collection_id": vector["_id"],
                    "external_vector_id": external_id,
                    "chunk_content_hash": chunk.get("content_hash"),
                    "created_at": now,
                },
                "$set": {
                    "status": "PENDING",
                    "embedding_content_hash": None,
                    "error": None,
                    "updated_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        ids.append(embedding.get("external_vector_id") or external_id)
        documents.append(chunk.get("content", ""))
        metadatas.append(_metadata_for_existing_chunk(chunk, vector["_id"]))
        embedding_ids.append(embedding["_id"])
    return ids, documents, metadatas, embedding_ids


def _mark_embeddings_cancelled(embedding_ids: list[ObjectId], message: str = "Index job cancelled") -> None:
    if not embedding_ids:
        return
    get_database().chunk_embeddings.update_many(
        {"_id": {"$in": embedding_ids}, "status": "PENDING"},
        {
            "$set": {
                "status": "CANCELLED",
                "error": {"message": message, "at": utc_now()},
                "updated_at": utc_now(),
            }
        },
    )


def process_document_reindex_background(
    document_id: str,
    index_job_id: str,
    collection_name: str,
) -> None:
    repository = get_document_repository()
    try:
        document = repository.find_by_id(document_id)
        if not document:
            raise ValueError("Không tìm thấy tài liệu")
        chunk_set_id = (document.get("current_processing") or {}).get("chunk_set_id")
        if not chunk_set_id:
            raise ValueError("Tài liệu chưa có chunk set để re-index")
        if is_document_job_cancelled(index_job_id):
            return
        repository.update_job(index_job_id, "PROCESSING", progress=5)
        chunks = list(
            get_database().document_chunks.find(
                {
                    "document_id": document["_id"],
                    "chunk_set_id": object_id(chunk_set_id, "chunk_set_id"),
                }
            ).sort("chunk_no", 1)
        )
        if not chunks:
            raise ValueError("Chunk set hiện hành chưa có chunks")
        vector, resolved_collection = _vector_collection_for_current_model(collection_name)
        ids, documents, metadatas, embedding_ids = _reindex_payload(chunks, vector)
        if is_document_job_cancelled(index_job_id):
            _mark_embeddings_cancelled(embedding_ids)
            return
        embedding_metrics: dict = {}
        stored_chunks = store_chunks(
            ids,
            documents,
            metadatas,
            resolved_collection,
            metrics=embedding_metrics,
        )
        now = utc_now()
        if is_document_job_cancelled(index_job_id):
            _mark_embeddings_cancelled(embedding_ids)
            return
        get_database().chunk_embeddings.update_many(
            {"_id": {"$in": embedding_ids}},
            [
                {
                    "$set": {
                        "status": "INDEXED",
                        "embedding_content_hash": "$chunk_content_hash",
                        "indexed_at": now,
                        "updated_at": now,
                        "error": None,
                    }
                }
            ],
        )
        repository.update_job(
            index_job_id,
            "COMPLETED",
            progress=100,
            stats={
                "total_chunks": len(chunks),
                "stored_chunks": stored_chunks,
                "collection_name": resolved_collection,
                "vector_collection_id": str(vector["_id"]),
                "embedding_model_name": settings.embedding_model_name,
                "embedding_config_hash": embedding_config_hash(),
                "embedding_metrics": embedding_metrics,
            },
        )
        latest = repository.find_by_id(document_id)
        if not latest:
            raise RuntimeError("DOCUMENT_ARCHIVED")
        processing = dict(latest.get("current_processing") or {})
        processing["vector_collection_id"] = vector["_id"]
        summary = dict(latest.get("pipeline_summary") or {})
        summary["index_status"] = "COMPLETED"
        repository.update(document_id, {
            "current_processing": processing,
            "pipeline_summary": summary,
            "status": "READY",
        })
    except Exception as exc:
        logger.exception("Re-index job %s failed", index_job_id)
        repository.update_job(index_job_id, "FAILED", error_message=str(exc))


def queue_document_reindex(
    background_tasks: BackgroundTasks,
    document_id: str,
    collection_name: str | None = None,
) -> dict:
    resolved_collection = collection_name or settings.chromadb_collection_name
    repository = get_document_repository()
    job = repository.create_job(
        document_id,
        "INDEX",
        config={
            "collection_name": resolved_collection,
            "embedding_model_name": settings.embedding_model_name,
            "embedding_config_hash": embedding_config_hash(),
        },
    )
    background_tasks.add_task(
        process_document_reindex_background,
        document_id=document_id,
        index_job_id=str(job["_id"]),
        collection_name=resolved_collection,
    )
    return {"index_job_id": str(job["_id"])}


def chunk_document_and_store(
    document_id: str,
    chunk_size: int,
    chunk_overlap: int,
    collection_name: str | None = None,
    buffer_max_pages: int | None = None,
    buffer_max_chars: int | None = None,
    max_code_block_lines: int | None = None,
    dry_run: bool = False,
) -> DocumentChunkResponse:
    doc = get_document_record(document_id)
    if not doc:
        raise ValueError("Không tìm thấy tài liệu")

    run_config = _chunk_run_config(
        {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "collection_name": collection_name,
            "buffer_max_pages": buffer_max_pages,
            "buffer_max_chars": buffer_max_chars,
            "max_code_block_lines": max_code_block_lines,
            "dry_run": dry_run,
        }
    )
    chunk_job_id, chunk_set_id = start_chunk_set(document_id, run_config)
    return _process_existing_chunk_set(document_id, chunk_job_id, chunk_set_id, run_config)


def _source_snapshot(document: dict) -> dict:
    original_types = {
        "ORIGINAL_PDF",
        "ORIGINAL_DOCX",
        "ORIGINAL_DOC",
        "ORIGINAL_MARKDOWN",
        "ORIGINAL_TEXT",
    }
    artifact = next(
        (
            item
            for item in document.get("artifacts") or []
            if item.get("type") in original_types and item.get("is_current", True)
        ),
        {},
    )
    storage = artifact.get("storage") or {}
    filename = document.get("original_filename") or document.get("title") or str(document["_id"])
    return {
        "source_file_name": filename,
        "source_uri": storage.get("uri") or f"document:{document['_id']}",
        "source_artifact_id": str(artifact.get("_id")) if artifact.get("_id") else None,
        "document_type": filename.rsplit(".", 1)[-1].lower() if "." in filename else "unknown",
        "document_title": document.get("title") or filename,
        "document_version": int(document.get("current_version") or 1),
    }


def _split_protected_block(block: dict, chunk_size: int, max_code_lines: int) -> list[dict]:
    content = block.get("content") or ""
    if block.get("block_type") == "formula" or len(content) <= chunk_size:
        return [{"content": content, "continuation_of": None, "part_index": None, "part_count": None}]
    lines = content.splitlines(keepends=True)
    limit = max_code_lines if block.get("block_type") == "code" else max(len(lines), 1)
    groups: list[str] = []
    current: list[str] = []
    current_chars = 0
    for line in lines:
        if current and (len(current) >= limit or current_chars + len(line) > chunk_size):
            groups.append("".join(current))
            current = []
            current_chars = 0
        current.append(line)
        current_chars += len(line)
    if current:
        groups.append("".join(current))
    if len(groups) <= 1:
        return [{"content": content, "continuation_of": None, "part_index": None, "part_count": None}]
    parent = block.get("block_id")
    return [
        {
            "content": group,
            "continuation_of": parent,
            "part_index": index,
            "part_count": len(groups),
        }
        for index, group in enumerate(groups, start=1)
    ]


def _merge_small_structured_chunks(
    chunks: list[dict],
    document_id: str,
    chunk_size: int,
    dynamic_keywords: list[str],
    min_chars: int = 120,
    embedding_max_tokens: int | None = None,
) -> list[dict]:
    """Attach tiny same-page fragments to context without changing their text."""
    merged: list[dict] = []
    for chunk in chunks:
        current = {"chunk_id": chunk["chunk_id"], "content": chunk["content"], "metadata": dict(chunk["metadata"])}
        if merged:
            previous = merged[-1]
            previous_metadata = previous["metadata"]
            current_metadata = current["metadata"]
            same_page = previous_metadata.get("page_marks") == current_metadata.get("page_marks")
            same_context = all(
                previous_metadata.get(key) == current_metadata.get(key)
                for key in ("document_id", "source_uri", "source_file_name", "heading")
            ) and bool(previous_metadata.get("page_marks") or (
                previous_metadata.get("source_locations")
                and previous_metadata.get("source_locations") == current_metadata.get("source_locations")
            ))
            has_continuation = previous_metadata.get("continuation_of") or current_metadata.get("continuation_of")
            combined_length = len(previous["content"]) + len(current["content"]) + 2
            should_merge = len(previous["content"]) < min_chars or len(current["content"]) < min_chars
            merge_allowed = same_page and same_context and not has_continuation and should_merge
            if merge_allowed and combined_length <= chunk_size and (
                embedding_max_tokens is None
                or embedding_token_lengths([f"{previous['content']}\n\n{current['content']}"])[0] <= embedding_max_tokens
            ):
                previous["content"] = f"{previous['content']}\n\n{current['content']}"
                previous_metadata["block_ids"] = list(
                    dict.fromkeys((previous_metadata.get("block_ids") or []) + (current_metadata.get("block_ids") or []))
                )
                previous_metadata["source_locations"] = (
                    (previous_metadata.get("source_locations") or []) + (current_metadata.get("source_locations") or [])
                )
                previous_metadata["validation_statuses"] = sorted(
                    set(previous_metadata.get("validation_statuses") or ["passed"])
                    | set(current_metadata.get("validation_statuses") or ["passed"])
                )
                previous_metadata["requires_review"] = bool(
                    previous_metadata.get("requires_review") or current_metadata.get("requires_review")
                )
                for key in ("source_block_types_text", "asset_ids_text", "source_asset_types_text"):
                    previous_values = set(filter(None, str(previous_metadata.get(key) or "").split(",")))
                    current_values = set(filter(None, str(current_metadata.get(key) or "").split(",")))
                    previous_metadata[key] = ",".join(sorted(previous_values | current_values))
                if previous_metadata.get("content_type") != current_metadata.get("content_type"):
                    previous_metadata["content_type"] = "mixed"
                if not previous_metadata.get("heading") and current_metadata.get("heading"):
                    for key in ("heading", "heading_path", "heading_path_text", "heading_norm"):
                        previous_metadata[key] = current_metadata.get(key)
                continue
        merged.append(current)

    token_lengths = embedding_token_lengths([chunk["content"] for chunk in merged]) if merged else []
    for index, (chunk, token_length) in enumerate(zip(merged, token_lengths), start=1):
        content = chunk["content"]
        metadata = chunk["metadata"]
        word_count = _count_tokens(content)
        keyword_hits, density = _information_density(content, word_count, dynamic_keywords)
        metadata.update(
            {
                "word_count": word_count,
                "token_count": token_length,
                "keyword_hits": keyword_hits,
                "information_density": density,
            }
        )
        chunk["chunk_id"] = _generate_chunk_id(document_id, index, content)
    return merged


def _structured_chunks(
    document: dict,
    pages: list[dict],
    document_id: str,
    chunk_size: int,
    chunk_overlap: int,
    max_code_lines: int,
    dynamic_keywords: list[str],
    embedding_max_tokens: int,
    *,
    retrieval_metrics: dict | None = None,
) -> list[dict]:
    source = _source_snapshot(document)
    pages = filter_retrieval_pages(pages, retrieval_metrics)
    asset_type_by_id = {
        asset.get("asset_id"): asset.get("asset_type")
        for page in pages
        for asset in page.get("assets") or []
        if asset.get("asset_id") and asset.get("asset_type")
    }
    chunks: list[dict] = []
    pending: list[dict] = []
    pending_chars = 0
    active_heading = ""

    def block_pages(entries: list[dict]) -> list[int]:
        return sorted(
            {
                int(entry["provenance"]["page_number"])
                for entry in entries
                if (entry.get("provenance") or {}).get("page_number") is not None
            }
        )

    def source_locations(entries: list[dict]) -> list[dict]:
        locations: list[dict] = []
        seen: set[str] = set()
        for entry in entries:
            provenance = entry.get("provenance") or {}
            location = provenance.get("source_location") or {}
            marker = repr(sorted(location.items()))
            if location and marker not in seen:
                locations.append(location)
                seen.add(marker)
        return locations

    def emit(
        entries: list[dict],
        content: str,
        *,
        content_type: str,
        continuation_of: str | None = None,
        part_index: int | None = None,
        part_count: int | None = None,
    ) -> None:
        nonlocal chunks
        if not content.strip():
            return
        token_length = embedding_token_lengths([content])[0]
        windows = [(content, content, token_length)]
        if token_length > embedding_max_tokens and content_type not in {"table", "code", "formula"}:
            windows = _embedding_windows(content, "", embedding_max_tokens, 0)
        pages_for_chunk = block_pages(entries)
        locations = source_locations(entries)
        validation_statuses = sorted(
            {entry.get("validation_status") or "passed" for entry in entries}
        )
        requires_review = "needs_review" in validation_statuses
        # Document validation reports missing layout coordinates; keep that warning
        # visible downstream instead of declaring such chunks fully verified.
        if any(
            entry.get("block_type") in {"table", "formula"}
            and (entry.get("provenance") or {}).get("page_number") is not None
            and (entry.get("provenance") or {}).get("bbox") is None
            for entry in entries
        ):
            requires_review = True
            validation_statuses = sorted(set(validation_statuses) | {"needs_review"})
        source_block_types = sorted({entry.get("block_type") or "prose" for entry in entries})
        asset_ids = sorted({asset_id for entry in entries for asset_id in entry.get("asset_ids") or []})
        source_asset_types = sorted(
            {
                *(
                    entry.get("block_type")
                    for entry in entries
                    if entry.get("block_type") in {"image", "diagram"}
                ),
                *(asset_type_by_id[asset_id] for asset_id in asset_ids if asset_id in asset_type_by_id),
            }
        )
        for segment, _embedding_content, embedding_tokens in windows:
            chunk_index = len(chunks) + 1
            word_count = _count_tokens(segment)
            keyword_hits, density = _information_density(segment, word_count, dynamic_keywords)
            metadata = {
                "document_id": document_id,
                **source,
                "heading": active_heading,
                "heading_path": [active_heading] if active_heading else [],
                "heading_path_text": active_heading,
                "heading_norm": _normalize_heading_meta(active_heading),
                "page_start": min(pages_for_chunk) if pages_for_chunk else None,
                "page_end": max(pages_for_chunk) if pages_for_chunk else None,
                "page_marks": pages_for_chunk,
                "source_locations": locations,
                "block_ids": [entry.get("block_id") for entry in entries if entry.get("block_id")],
                "content_type": content_type,
                "semantic_type": _detect_semantic_type(segment, active_heading),
                "keyword_hits": keyword_hits,
                "information_density": density,
                "token_count": embedding_tokens,
                "word_count": word_count,
                "continuation_of": continuation_of,
                "part_index": part_index,
                "part_count": part_count,
                "provenance_schema": "content-block-v1",
                "validation_statuses": validation_statuses,
                "requires_review": requires_review,
                "source_block_types_text": ",".join(source_block_types),
                "asset_ids_text": ",".join(asset_ids),
                "source_asset_types_text": ",".join(source_asset_types),
            }
            chunks.append(
                {
                    "chunk_id": _generate_chunk_id(document_id, chunk_index, segment),
                    "content": segment,
                    "metadata": metadata,
                }
            )

    def flush_pending() -> None:
        nonlocal pending, pending_chars, active_heading
        if not pending:
            return
        heading_introduction = any(entry.get("block_type") == "heading" for entry in pending) and all(
            entry.get("block_type") == "heading" or (
                entry.get("block_type") in {"prose", "list"}
                and (entry.get("content") or "").strip().endswith(":")
                and "\n" not in (entry.get("content") or "").strip()
                and len((entry.get("content") or "").split()) <= 12
            ) for entry in pending
        )
        if heading_introduction:
            # The title stays in active_heading for following content, including
            # a body beginning on the next page; it is not a knowledge chunk.
            labels = [entry["content"].strip() for entry in pending if entry.get("block_type") != "heading"]
            if labels:
                active_heading = " / ".join([active_heading, *labels])
            if retrieval_metrics is not None:
                retrieval_metrics["heading_only_buffers_suppressed"] = (
                    retrieval_metrics.get("heading_only_buffers_suppressed", 0) + 1
                )
            pending = []
            pending_chars = 0
            return
        content = "\n\n".join(entry.get("content") or "" for entry in pending).strip()
        if len(content) <= 2 and all(entry.get("block_type") in {"prose", "caption"} for entry in pending):
            if retrieval_metrics is not None:
                retrieval_metrics["isolated_fragments_suppressed"] = retrieval_metrics.get("isolated_fragments_suppressed", 0) + 1
            pending = []
            pending_chars = 0
            return
        if len(content) <= chunk_size:
            emit(pending, content, content_type="text")
        else:
            for part in _split_recursive(content, chunk_size, chunk_overlap):
                emit(pending, part, content_type="text")
        pending = []
        pending_chars = 0

    protected = {"table", "code", "formula"}
    for page in pages:
        # Do not absorb a tiny page into the next page to satisfy a size target.
        flush_pending()
        for block in page.get("content_blocks") or []:
            if block.get("validation_status") == "failed":
                continue
            block_type = block.get("block_type") or "prose"
            if block_type in {"image", "diagram", "page_break"} and not (block.get("content") or "").strip():
                continue
            if block_type == "heading":
                heading = (block.get("content") or "").strip()
                if pending and all(entry.get("block_type") == "heading" for entry in pending):
                    # Consecutive title lines belong to one context, not separate chunks.
                    active_heading = f"{active_heading} / {heading}"
                    pending.append(block)
                    pending_chars += len(heading) + 2
                else:
                    flush_pending()
                    active_heading = heading
                    pending = [block]
                    pending_chars = len(heading)
                continue
            if block_type in protected:
                flush_pending()
                for part in _split_protected_block(block, chunk_size, max_code_lines):
                    emit(
                        [block],
                        part["content"],
                        content_type=block_type,
                        continuation_of=part["continuation_of"],
                        part_index=part["part_index"],
                        part_count=part["part_count"],
                    )
                continue
            content = block.get("content") or ""
            projected = pending_chars + len(content) + (2 if pending else 0)
            if pending and projected > chunk_size:
                flush_pending()
            pending.append(block)
            pending_chars += len(content) + (2 if pending_chars else 0)
    flush_pending()
    return _merge_small_structured_chunks(
        chunks, document_id, chunk_size, dynamic_keywords, embedding_max_tokens=embedding_max_tokens,
    )


def _chunk_policy_metrics(chunks: list[dict]) -> dict:
    protected_types = {"table", "code", "formula"}
    under_80 = sum(len(chunk.get("content") or "") < 80 for chunk in chunks)
    under_120 = sum(len(chunk.get("content") or "") < 120 for chunk in chunks)
    protected = sum((chunk.get("metadata") or {}).get("content_type") in protected_types for chunk in chunks)
    continuations = sum(bool((chunk.get("metadata") or {}).get("continuation_of")) for chunk in chunks)
    needs_review = sum(bool((chunk.get("metadata") or {}).get("requires_review")) for chunk in chunks)
    repeated_boundary_chars = 0
    for previous, current in zip(chunks, chunks[1:]):
        previous_content = previous.get("content") or ""
        current_content = current.get("content") or ""
        max_overlap = min(len(previous_content), len(current_content), 800)
        repeated_boundary_chars += next(
            (
                size
                for size in range(max_overlap, 0, -1)
                if previous_content[-size:] == current_content[:size]
            ),
            0,
        )
    return {
        "under_80_chars": under_80,
        "under_120_chars": under_120,
        "protected_structure_chunks": protected,
        "protected_continuations": continuations,
        "needs_review_chunks": needs_review,
        "repeated_boundary_characters": repeated_boundary_chars,
        "interpretation": (
            "preservation_driven" if protected >= max(1, len(chunks) // 3) else "review_small_fragment_policy"
        ),
    }


def _process_existing_chunk_set(
    document_id: str,
    chunk_job_id: str,
    chunk_set_id: str,
    run_config: dict,
) -> DocumentChunkResponse:
    overall_started_at = perf_counter()
    chunk_size = int(run_config["chunk_size"])
    chunk_overlap = int(run_config["chunk_overlap"])
    resolved_buffer_pages = int(run_config["buffer_max_pages"])
    resolved_buffer_chars = int(run_config["buffer_max_chars"])
    resolved_code_lines = int(run_config["max_code_block_lines"])
    embedding_max_tokens = max(int(run_config.get("embedding_max_tokens") or settings.embedding_max_tokens), 32)
    embedding_token_overlap = min(
        max(int(run_config.get("embedding_token_overlap") or settings.embedding_token_overlap), 0),
        embedding_max_tokens - 1,
    )
    resolved_collection = run_config["collection_name"]
    dry_run = bool(run_config.get("dry_run", False))

    if not dry_run:
        _, resolved_collection = _vector_collection_for_current_model(resolved_collection)

    if is_document_job_cancelled(chunk_job_id):
        return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)

    total_chunks = 0
    stored_chunks = 0
    original_length = 0
    total_chars = 0
    min_chunk = None
    max_chunk = 0
    total_embedding_tokens = 0
    max_embedding_tokens = 0
    content_type_distribution: Counter[str] = Counter()
    timings_ms: dict[str, float] = {}
    embedding_metrics: dict = {}

    all_chunks_for_export: list[dict] = []
    vector_collection_id: str | None = None

    document = get_document_record(document_id)
    keyword_started_at = perf_counter()
    dynamic_keywords = get_active_keywords(dictionary_key_for_document(document))
    timings_ms["keyword_load"] = round((perf_counter() - keyword_started_at) * 1000, 2)

    split_started_at = perf_counter()
    chunk_set = get_database().chunk_sets.find_one(
        {"_id": object_id(chunk_set_id, "chunk_set_id")},
        {"source_ocr_job_id": 1},
    )
    source_ocr_job_id = (chunk_set or {}).get("source_ocr_job_id")
    pages = list(iter_document_pages(document_id, source_ocr_job_id))
    structured_mode = any(page.get("content_blocks") for page in pages)
    retrieval_metrics: dict = {}
    if structured_mode:
        generated_chunks = _structured_chunks(
            document,
            pages,
            document_id,
            chunk_size,
            chunk_overlap,
            resolved_code_lines,
            dynamic_keywords,
            embedding_max_tokens,
            retrieval_metrics=retrieval_metrics,
        )
        original_length = sum(len(page.get("raw_text") or page.get("text") or "") for page in pages)
        chunk_iterable = generated_chunks
    else:
        legacy_chunks: list[dict] = []
        for buffer_text, source_len in _iter_chapter_buffers(
            document_id,
            resolved_buffer_pages,
            resolved_buffer_chars,
            source_ocr_job_id,
        ):
            if is_document_job_cancelled(chunk_job_id):
                return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)
            original_length += source_len
            legacy_chunks.extend(
                _chunk_buffer(
                    buffer_text,
                    document_id,
                    chunk_size,
                    chunk_overlap,
                    resolved_code_lines,
                    dynamic_keywords,
                    embedding_max_tokens,
                    embedding_token_overlap,
                )
            )
        chunk_iterable = legacy_chunks

    for chunk in chunk_iterable:
        content = chunk["content"]
        metadata = chunk["metadata"]
        all_chunks_for_export.append(chunk)
        total_chunks += 1
        char_count = len(content)
        total_chars += char_count
        min_chunk = char_count if min_chunk is None else min(min_chunk, char_count)
        max_chunk = max(max_chunk, char_count)
        embedding_tokens = int(metadata.get("token_count", 0))
        total_embedding_tokens += embedding_tokens
        max_embedding_tokens = max(max_embedding_tokens, embedding_tokens)
        content_type_distribution[metadata.get("content_type", "text")] += 1
        if total_chunks % 32 == 0 and is_document_job_cancelled(chunk_job_id):
            return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)

    timings_ms["buffer_and_chunk"] = round((perf_counter() - split_started_at) * 1000, 2)
    quality_report = validate_chunks(all_chunks_for_export) if structured_mode else None
    if quality_report:
        quality_report.metrics["chunk_policy"] = _chunk_policy_metrics(all_chunks_for_export)
        quality_report.metrics["retrieval_filter"] = retrieval_metrics
    if quality_report and not quality_report.passed:
        message = "; ".join(quality_report.errors[:20])
        fail_chunk_set(document_id, f"quality_failed: {message}")
        raise ValueError(f"quality_failed: {message}")

    avg_chunk = round(total_chars / max(total_chunks, 1), 2)
    stats = ChunkingStats(
        original_length=original_length,
        total_chunks=total_chunks,
        junk_removed=int(retrieval_metrics.get("filtered_characters", 0)),
        avg_chunk_size=avg_chunk,
        min_chunk_size=min_chunk or 0,
        max_chunk_size=max_chunk,
        content_type_distribution=dict(content_type_distribution),
        avg_embedding_tokens=round(total_embedding_tokens / max(total_chunks, 1), 2),
        max_embedding_tokens=max_embedding_tokens,
        timings_ms=timings_ms,
        embedding_metrics=embedding_metrics,
        quality=quality_report.to_dict() if quality_report else {"status": "legacy_not_evaluated"},
    )

    if is_document_job_cancelled(chunk_job_id):
        return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)

    if not dry_run and all_chunks_for_export:
        persist_started_at = perf_counter()
        (
            vector_collection_id,
            vector_ids,
            vector_documents,
            vector_metadatas,
        ) = persist_chunks(
            document_id,
            chunk_set_id,
            resolved_collection,
            all_chunks_for_export,
        )
        timings_ms["mongo_chunk_persist"] = round((perf_counter() - persist_started_at) * 1000, 2)
        if is_document_job_cancelled(chunk_job_id):
            return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)
        embedding_started_at = perf_counter()
        stored_chunks = store_chunks(
            vector_ids,
            vector_documents,
            vector_metadatas,
            resolved_collection,
            metrics=embedding_metrics,
        )
        timings_ms["embedding_and_chroma"] = round((perf_counter() - embedding_started_at) * 1000, 2)

    if is_document_job_cancelled(chunk_job_id):
        return _cancelled_chunk_response(document_id, chunk_job_id, chunk_set_id, resolved_collection)

    # THỰC HIỆN XUẤT FILE SAU KHI ĐÃ CẮT XONG (Bao gồm cả dry_run và real run)
    if all_chunks_for_export:
        export_started_at = perf_counter()
        try:
            export_chunks_to_file(document_id, all_chunks_for_export)
        except Exception as e:
            logger.error(f"Lỗi khi xuất file metadata: {e}")
        timings_ms["artifact_export"] = round((perf_counter() - export_started_at) * 1000, 2)

    timings_ms["pre_completion_total"] = round((perf_counter() - overall_started_at) * 1000, 2)
    stats.timings_ms = timings_ms
    stats.embedding_metrics = embedding_metrics
    completion_started_at = perf_counter()
    complete_chunk_set(
        document_id,
        chunk_job_id,
        chunk_set_id,
        vector_collection_id,
        total_chunks=total_chunks,
        total_characters=total_chars,
        stats=stats.model_dump(),
        dry_run=dry_run,
    )
    timings_ms["mongo_completion"] = round((perf_counter() - completion_started_at) * 1000, 2)
    timings_ms["total"] = round((perf_counter() - overall_started_at) * 1000, 2)
    stats.timings_ms = timings_ms

    return DocumentChunkResponse(
        document_id=document_id,
        chunk_job_id=chunk_job_id,
        chunk_set_id=chunk_set_id,
        vector_collection_id=vector_collection_id,
        collection_name=resolved_collection,
        total_chunks=total_chunks,
        stored_chunks=stored_chunks,
        stats=stats,
    )


def _iter_chapter_buffers(
	document_id: str,
	buffer_max_pages: int,
	buffer_max_chars: int,
	ocr_job_id: str | ObjectId | None = None,
) -> Iterable[tuple[str, int]]:
	buffer_lines: list[str] = []
	buffer_pages = 0
	buffer_chars = 0
	buffer_source_len = 0

	for page in iter_document_pages(document_id, ocr_job_id):
		page_text = page.get("text", "") or ""
		page_number = page.get("page_number", 0)
		marker = f"<!-- PAGE:{page_number} -->"

		if not buffer_lines:
			buffer_lines.append(marker)
		else:
			buffer_lines.append(marker)

		lines = page_text.splitlines() if page_text else []
		for line in lines:
			normalized_line = _smart_normalize_heading(line)
			if normalized_line.startswith("# "):
				if _buffer_has_content(buffer_lines):
					yield "\n".join(buffer_lines).strip(), buffer_source_len
					buffer_lines = [marker]
					buffer_pages = 0
					buffer_chars = 0
					buffer_source_len = 0

			buffer_lines.append(normalized_line)
			buffer_chars += len(normalized_line) + 1

		buffer_pages += 1
		buffer_source_len += len(page_text)

		if buffer_pages >= buffer_max_pages or buffer_chars >= buffer_max_chars:
			yield "\n".join(buffer_lines).strip(), buffer_source_len
			buffer_lines = []
			buffer_pages = 0
			buffer_chars = 0
			buffer_source_len = 0

	if buffer_lines:
		yield "\n".join(buffer_lines).strip(), buffer_source_len


def _chunk_buffer(
	text: str,
	document_id: str,
	chunk_size: int,
	chunk_overlap: int,
	max_code_block_lines: int,
	dynamic_keywords: list[str],
	embedding_max_tokens: int,
	embedding_token_overlap: int,
) -> Iterable[dict]:
	sections = _split_by_markdown_headers(text)
	chunk_index = 0

	for section in sections:
		masked_text, protected = _mask_blocks(section["content"], max_code_block_lines)
		parts = _split_recursive(masked_text, chunk_size, chunk_overlap)
		heading_path_text = " > ".join(section["heading_path"]) if section["heading_path"] else ""
		heading_norm = _normalize_heading_meta(section["heading"]) if section["heading"] else ""
		heading_path_norm = _normalize_heading_meta(heading_path_text) if heading_path_text else ""
		parent_heading = section["heading_path"][-2] if len(section["heading_path"]) > 1 else section["heading"]

		for part in parts:
			restored = _restore_blocks(part, protected)
			page_marks = _extract_page_marks(restored)
			clean_text = _strip_page_markers(restored)
			if not clean_text.strip():
				continue

			for segment_text, enriched_content, embedding_tokens in _embedding_windows(
				clean_text,
				heading_path_text,
				embedding_max_tokens,
				embedding_token_overlap,
			):
				chunk_index += 1
				word_count = _count_tokens(segment_text)
				keyword_hits, density = _information_density(segment_text, word_count, dynamic_keywords)
				semantic = _detect_semantic_type(segment_text, section["heading"])
				metadata = {
					"document_id": document_id,
					"heading_path": section["heading_path"],
					"heading": section["heading"],
					"parent_heading": parent_heading,
					"heading_path_text": heading_path_text,
					"heading_norm": heading_norm,
					"heading_path_norm": heading_path_norm,
					"page_start": page_marks["page_start"],
					"page_end": page_marks["page_end"],
					"page_marks": page_marks["pages"],
					"content_type": _detect_content_type(segment_text),
					"semantic_type": semantic,
					"keyword_hits": keyword_hits,
					"information_density": density,
					"token_count": embedding_tokens,
					"word_count": word_count,
				}

				chunk_id = _generate_chunk_id(document_id, chunk_index, enriched_content)
				yield {
					"chunk_id": chunk_id,
					"content": enriched_content,
					"metadata": metadata,
				}


def _balance_markdown_blocks(text: str, start: int, end: int) -> str:
	segment = text[start:end].strip()
	before = text[:start]
	through_end = text[:end]

	if before.count("```") % 2:
		last_fence = before.rfind("```")
		language = before[last_fence + 3 :].splitlines()[0].strip() if last_fence >= 0 else ""
		segment = f"```{language}\n{segment}"
	if through_end.count("```") % 2:
		segment = f"{segment}\n```"
	if before.count("$$") % 2:
		segment = f"$$\n{segment}"
	if through_end.count("$$") % 2:
		segment = f"{segment}\n$$"
	return segment


def _embedding_windows(
    text: str,
    heading_path_text: str,
    max_tokens: int,
    overlap_tokens: int,
) -> list[tuple[str, str, int]]:
    """Use original Unicode word spans, never individual tokenizer subwords."""
    def enrich(value: str) -> str:
        return f"[{heading_path_text}]\n\n{value}" if heading_path_text else value

    full_length = embedding_token_lengths([enrich(text)])[0]
    if full_length <= max_tokens:
        return [(text, enrich(text), full_length)]
    words = list(re.finditer(r"\S+", text))
    windows = []
    start = 0
    while start < len(words):
        low, high = start + 1, len(words)
        best = None
        while low <= high:
            end = (low + high) // 2
            segment = _balance_markdown_blocks(text, words[start].start(), words[end - 1].end())
            enriched = enrich(segment)
            tokens = embedding_token_lengths([enriched])[0]
            if tokens <= max_tokens:
                best = (end, segment, enriched, tokens)
                low = end + 1
            else:
                high = end - 1
        if best is None:
            raise ValueError("A source word or heading exceeds the embedding token budget; review required")
        end, segment, enriched, tokens = best
        if end < len(words):
            # Prefer a paragraph/sentence boundary in the latter half of the window.
            candidates = list(re.finditer(r"\n\s*\n|[.!?](?=\s)", segment))
            boundaries = [m.end() for m in candidates if m.end() >= len(segment) * 0.5]
            if boundaries:
                target = words[start].start() + boundaries[-1]
                end = next((i for i in range(start + 1, end) if words[i].start() >= target), end)
                segment = _balance_markdown_blocks(text, words[start].start(), words[end - 1].end())
                enriched = enrich(segment)
                tokens = embedding_token_lengths([enriched])[0]
        windows.append((segment, enriched, tokens))
        if end == len(words):
            break
        next_start = end
        if overlap_tokens > 0:
            for candidate in range(end - 1, start, -1):
                overlap_text = text[words[candidate].start():words[end - 1].end()]
                if embedding_token_lengths([overlap_text])[0] > overlap_tokens:
                    break
                next_start = candidate
        start = next_start
    return windows


def _split_by_markdown_headers(text: str) -> list[dict]:
	matches = list(HEADING_PATTERN.finditer(text))
	if not matches:
		return [{"heading": "", "heading_path": [], "content": text}]

	sections: list[dict] = []
	if matches[0].start() > 0:
		pre = text[: matches[0].start()].strip()
		if pre:
			sections.append({"heading": "", "heading_path": [], "content": pre})

	heading_stack: list[str] = []
	for i, match in enumerate(matches):
		level = len(match.group(1))
		heading = match.group(2).strip()

		if level == 1:
			heading_stack = [heading]
		elif level == 2:
			heading_stack = heading_stack[:1] + [heading] if heading_stack else [heading]
		else:
			heading_stack = heading_stack[:2] + [heading] if heading_stack else [heading]

		body_start = match.end()
		body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
		body = text[body_start:body_end].strip()
		content = f"{match.group(0).strip()}\n\n{body}" if body else match.group(0).strip()

		sections.append(
			{
				"heading": heading,
				"heading_path": list(heading_stack),
				"content": content,
			}
		)

	return sections


def _strip_accents(text: str) -> str:
	normalized = unicodedata.normalize("NFKD", text)
	return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_heading_meta(value: str) -> str:
	if not value:
		return ""
	folded = _strip_accents(value)
	return re.sub(r"\s+", " ", folded).strip().lower()


def _is_heading_like_remainder(remainder: str) -> bool:
	"""Real Roman-numeral headings in this corpus are followed by ALL-CAPS
	Vietnamese text (e.g. "I TỪ BÀI TOÁN ĐẾN CHƯƠNG TRÌNH"). Single-letter
	numerals (I/V/X) also collide with common variable names in code/math
	sentences (e.g. "x giống như x = X + 1"), which are lowercase/mixed-case.
	Require the remainder to be substantially uppercase to reject those."""
	letters = [ch for ch in _strip_accents(remainder) if ch.isalpha()]
	if len(letters) < 3:
		return False
	return all(ch.isupper() for ch in letters)


def _smart_normalize_heading(line: str) -> str:
	stripped = line.strip()
	if not stripped:
		return stripped
	if stripped.startswith("#"):
		return stripped
	if len(stripped) > 100:
		return stripped

	folded = _strip_accents(stripped).upper()
	if any(pattern.match(folded) for pattern in TOP_LEVEL_PATTERNS):
		return f"# {stripped}"
	sub2_match = SUB_LEVEL_2_PATTERN.match(stripped)
	if sub2_match and _is_heading_like_remainder(stripped[sub2_match.end():]):
		return f"## {stripped}"
	if SUB_LEVEL_3_PATTERN.match(stripped):
		return f"### {stripped}"

	return stripped


def _buffer_has_content(lines: list[str]) -> bool:
	"""A buffer only counts as real content once it has body text, not just
	page markers or heading lines. Without this, a run of consecutive
	headings (e.g. a table-of-contents page listing every chapter title)
	each triggers a yield, producing near-empty chunks."""
	for line in lines:
		s = line.strip()
		if not s or PAGE_MARKER_PATTERN.match(s):
			continue
		if s.startswith("#"):
			continue
		return True
	return False


def _mask_blocks(text: str, max_code_block_lines: int) -> tuple[str, dict]:
	protected: dict[str, str] = {}
	counter = 0

	def _store(prefix: str, value: str) -> str:
		nonlocal counter
		key = f"__{prefix}_{counter}__"
		protected[key] = value
		counter += 1
		return key

	def _split_code_block(block: str) -> str:
		lines = block.splitlines()
		if len(lines) < 2:
			return _store("CODEBLOCK", block)

		fence = lines[0]
		lang = fence.replace("```", "").strip() or "cpp"
		code_lines = lines[1:-1]
		if len(code_lines) <= max_code_block_lines:
			return _store("CODEBLOCK", block)

		segments = []
		total = (len(code_lines) + max_code_block_lines - 1) // max_code_block_lines
		for idx in range(total):
			start = idx * max_code_block_lines
			end = min(start + max_code_block_lines, len(code_lines))
			seg_lines = code_lines[start:end]
			if idx > 0:
				seg_lines = ["// (Continuation of previous code block)"] + seg_lines
			if idx < total - 1:
				seg_lines = seg_lines + ["// (Continues in next code block)"]
			seg_block = f"```{lang}\n" + "\n".join(seg_lines) + "\n```"
			segments.append(_store("CODEBLOCK", seg_block))

		return "\n\n".join(segments)

	def _split_table_block(block: str) -> str:
		lines = block.strip().splitlines()
		# Bảng Markdown cần ít nhất 3 dòng: header, separator, 1 row data
		if len(lines) < 3:
			return _store("TABLE", block)

		# Chia bảng theo nhóm dòng nếu quá dài (ví dụ: tối đa 15 dòng data)
		header = lines[0]
		separator = lines[1]
		rows = lines[2:]

		max_rows = 15
		if len(rows) <= max_rows:
			return _store("TABLE", block)

		segments = []
		total = (len(rows) + max_rows - 1) // max_rows
		for idx in range(total):
			start = idx * max_rows
			end = min(start + max_rows, len(rows))
			seg_rows = rows[start:end]
			seg_block = "\n".join([header, separator] + seg_rows)
			segments.append(_store("TABLE", seg_block))

		return "\n\n".join(segments)

	def _replace_code(match: re.Match) -> str:
		return _split_code_block(match.group())

	def _replace_table(match: re.Match) -> str:
		return _split_table_block(match.group())

	text = CODE_BLOCK_PATTERN.sub(_replace_code, text)
	text = FORMULA_BLOCK_PATTERN.sub(lambda m: _store("FORMULA", m.group()), text)
	text = LATEX_BLOCK_PATTERN.sub(lambda m: _store("LATEX", m.group()), text)
	text = TABLE_BLOCK_PATTERN.sub(_replace_table, text)

	return text, protected


def _restore_blocks(text: str, protected: dict) -> str:
	for key, value in protected.items():
		text = text.replace(key, value)
	return text


def _split_recursive(
	text: str,
	chunk_size: int,
	chunk_overlap: int,
	separators: list[str] | None = None,
) -> list[str]:
	if separators is None:
		separators = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "]

	sep = separators[-1]
	rest = []
	for i, s in enumerate(separators):
		if s == "":
			sep = s
			break
		if s in text:
			sep = s
			rest = separators[i + 1 :]
			break

	# Retain punctuation with the preceding fragment. The final fallback is
	# a whole source word, even when it exceeds the preferred character size.
	splits = re.split(r"(?<=" + re.escape(sep) + r")", text) if sep else [text]
	final: list[str] = []
	good: list[str] = []
	for part in splits:
		if len(part) < chunk_size:
			good.append(part)
		else:
			if good:
				final.extend(_merge(good, "", chunk_size, chunk_overlap))
				good = []
			if not rest:
				final.append(part)
			else:
				final.extend(_split_recursive(part, chunk_size, chunk_overlap, rest))

	if good:
		final.extend(_merge(good, "", chunk_size, chunk_overlap))

	return final


def _merge(splits: list[str], sep: str, chunk_size: int, chunk_overlap: int) -> list[str]:
	chunks: list[str] = []
	parts: list[str] = []
	cur_len = 0

	for part in splits:
		test = cur_len + len(part) + (len(sep) if parts else 0)
		if test > chunk_size and parts:
			t = sep.join(parts).strip()
			if t:
				chunks.append(t)
			while cur_len > chunk_overlap and parts:
				cur_len -= len(parts.pop(0)) + len(sep)

		parts.append(part)
		cur_len = sum(len(p) for p in parts) + len(sep) * (len(parts) - 1)

	if parts:
		t = sep.join(parts).strip()
		if t:
			chunks.append(t)

	return chunks


def _extract_page_marks(text: str) -> dict:
	pages = [int(m.group(1)) for m in PAGE_MARKER_PATTERN.finditer(text)]
	if pages:
		return {"pages": sorted(set(pages)), "page_start": min(pages), "page_end": max(pages)}
	return {"pages": [], "page_start": None, "page_end": None}


def _strip_page_markers(text: str) -> str:
	cleaned = PAGE_MARKER_PATTERN.sub("", text)
	cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
	return cleaned.strip()


def _generate_chunk_id(doc_id: str, index: int, content: str) -> str:
	raw = f"{doc_id}::{index}::{content}"
	return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _detect_content_type(text: str) -> str:
	has_code = "```" in text
	has_formula = "<FORMULA_BLOCK" in text or "$$" in text
	if has_code and has_formula:
		return "mixed"
	if has_code:
		return "code"
	if has_formula:
		return "formula"
	return "text"


def _detect_semantic_type(text: str, heading: str) -> str:
	context_to_check = f"{heading} {text[:200]}".strip().lower()
	if EXERCISE_PATTERN.search(context_to_check):
		return "exercise"
	if EXAMPLE_PATTERN.search(context_to_check):
		return "example"
	if DEFINITION_PATTERN.search(context_to_check):
		return "definition"
	return "theory"


def _count_tokens(text: str) -> int:
	return len(re.findall(r"[^\W_]+", text, flags=re.UNICODE))


def _information_density(text: str, token_count: int, keywords: list[str]) -> tuple[int, float]:
	lowered = text.lower()
	hits = 0
	for pattern in _compiled_keyword_patterns(tuple(keywords)):
		hits += len(pattern.findall(lowered))
	hits += len(BIG_O_PATTERN.findall(text))
	density = round(hits / max(token_count, 1), 4)
	return hits, density


@lru_cache(maxsize=32)
def _compiled_keyword_patterns(keywords: tuple[str, ...]) -> tuple[re.Pattern, ...]:
	return tuple(re.compile(rf"\b{re.escape(keyword.lower())}\b") for keyword in keywords if keyword)
