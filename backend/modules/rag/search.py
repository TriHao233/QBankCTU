import json
import logging
import re
import unicodedata

from bson import ObjectId

from core.config import settings
from core.database import get_rag_db
from core.gpu_coordination import current_gpu_operation_label
from modules.documents.store import get_document_repository
from modules.rag.chromadb_engine import embedding_config_matches, embedding_model_names_match, get_collection, model_scoped_collection_name

logger = logging.getLogger(__name__)

MIN_EVIDENCE_CHARS = 24
MIN_EVIDENCE_WORDS = 4


def _stored_id(value):
    text = str(value)
    return ObjectId(text) if ObjectId.is_valid(text) else value


def _active_vector_snapshot(
    document_id: str, collection_name: str, pinned: dict | None = None,
) -> tuple[str, str, str]:
    """Resolve the chunk set / vector collection to search.

    ``pinned`` (from a generation job) keeps retrieval on the snapshot that was
    active when the job was created, even if the document is re-indexed meanwhile.
    """
    try:
        document_oid = ObjectId(document_id)
    except Exception as exc:
        raise ValueError("document_id không hợp lệ") from exc

    db = get_rag_db()
    document = get_document_repository(db).find_by_id(document_oid)
    if not document:
        raise ValueError("Không tìm thấy tài liệu")

    if pinned:
        chunk_set_id = pinned.get("chunk_set_id")
        vector_collection_id = _stored_id(pinned.get("vector_collection_id"))
        vector_query = {"_id": vector_collection_id}
    else:
        current = document.get("current_processing") or {}
        chunk_set_id = current.get("chunk_set_id")
        vector_collection_id = current.get("vector_collection_id")
        vector_query = {"_id": vector_collection_id, "is_active": True}
    if not chunk_set_id or not vector_collection_id:
        raise ValueError("Tài liệu chưa có chunk set đã được index")

    vector = db.vector_collections.find_one(vector_query)
    if not vector:
        raise ValueError("Không tìm thấy cấu hình vector hiện hành")
    active_collection_name = vector.get("collection_name")
    if (
        active_collection_name != collection_name
        and active_collection_name != model_scoped_collection_name(collection_name)
        and collection_name != settings.chromadb_collection_name
    ):
        raise ValueError(
            f"Tài liệu đang được index trong collection '{active_collection_name}'"
        )
    indexed_model = vector.get("embedding_model") or {}
    if not embedding_model_names_match(indexed_model.get("model_name"), settings.embedding_model_name):
        raise ValueError(
            "Embedding model hiện tại không khớp snapshot đã index: "
            f"'{settings.embedding_model_name}' != '{indexed_model.get('model_name')}'"
        )
    if not embedding_config_matches(indexed_model, vector.get("embedding_config_hash")):
        raise ValueError("Cấu hình embedding hiện tại không khớp snapshot đã index")
    return str(chunk_set_id), str(vector_collection_id), str(active_collection_name)

def _strip_accents(text: str) -> str:
    text = (text or "").replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_heading_text(text: str) -> str:
    stripped = re.sub(r"^#+\s*", "", (text or "").strip())
    folded = _strip_accents(stripped).lower()
    return re.sub(r"\s+", " ", folded).strip()

def _coerce_heading_path(meta: dict) -> list[str]:
    heading_path = meta.get("heading_path")
    if isinstance(heading_path, list):
        return [str(x) for x in heading_path if x]
    if isinstance(heading_path, str) and heading_path:
        try:
            parsed = json.loads(heading_path)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except Exception:
            return [heading_path]
    return []


def _coerce_metadata_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return [item for item in value.split(",") if item]
    return []


def _build_heading_label(meta: dict) -> str:
    heading_path_text = meta.get("heading_path_text")
    if isinstance(heading_path_text, str) and heading_path_text.strip():
        return heading_path_text.strip()
    path = _coerce_heading_path(meta)
    if path:
        return " > ".join(path)
    heading = meta.get("heading")
    if isinstance(heading, str) and heading.strip():
        return heading.strip()
    return ""


def _is_substantive_evidence(doc: str, meta: dict | None = None) -> bool:
    """Return whether a chunk can support a question, rather than just label it."""
    text = str(doc or "").strip()
    if not text:
        return False

    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) == 1 and nonempty_lines[0].startswith("#"):
        return False

    metadata = meta or {}
    heading_values = [
        _build_heading_label(metadata),
        str(metadata.get("heading") or ""),
        *(_coerce_heading_path(metadata)[-1:] or []),
    ]
    normalized_headings = {
        _normalize_heading_text(value)
        for value in heading_values
        if _normalize_heading_text(value)
    }
    without_markdown_heading = re.sub(r"^#+\s*", "", text).strip()
    normalized = _normalize_heading_text(without_markdown_heading)
    if normalized in normalized_headings:
        return False

    words = re.findall(r"\w+", without_markdown_heading, flags=re.UNICODE)
    return len(without_markdown_heading) >= MIN_EVIDENCE_CHARS and len(words) >= MIN_EVIDENCE_WORDS


def _evidence_text(doc: str) -> str:
    """Keep a section label out of the text that the model may quote as evidence."""
    lines = str(doc or "").splitlines()
    if lines and lines[0].lstrip().startswith("#") and any(line.strip() for line in lines[1:]):
        return "\n".join(lines[1:]).strip()
    return str(doc or "").strip()


def _heading_words(text: str) -> str:
    """Normalized heading reduced to words, so "2.1. Ngăn xếp" and "2.1 - ngan xep" compare equal."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", _normalize_heading_text(text))).strip(" .")


def _section_number(text: str) -> str | None:
    match = re.match(r"^(?:chuong|phan|muc|bai)?\s*(\d+(?:\.\d+)*)\b", text)
    return match.group(1) if match else None


def _heading_matches_target(meta: dict, normalized_target: str) -> bool:
    """A chunk belongs to the selected section when that section is its heading or any ancestor."""
    if not normalized_target:
        return True
    heading_path = _coerce_heading_path(meta)
    heading_candidates = [
        _normalize_heading_text(value)
        for value in (
            meta.get("heading", "") or "",
            meta.get("heading_path_text", "") or "",
            " > ".join(heading_path),
        )
        if value
    ]
    target_chapter = re.search(r"\bchuong\s+(\d+)\b", normalized_target)
    for candidate in heading_candidates:
        if normalized_target in candidate or candidate in normalized_target:
            return True
        candidate_chapter = re.search(r"\bchuong\s+(\d+)\b", candidate)
        if target_chapter and candidate_chapter and target_chapter.group(1) == candidate_chapter.group(1):
            return True
    # Compare each level of the path (sub-headings included) ignoring punctuation
    # and numbering style; "2.1" also covers its sub-sections "2.1.3".
    target_leaf = _heading_words(normalized_target.split(">")[-1])
    target_number = _section_number(target_leaf)
    if not target_leaf:
        return False
    for level in [*heading_path, meta.get("heading", "") or ""]:
        words = _heading_words(level)
        if not words:
            continue
        if target_leaf == words or target_leaf in words:
            return True
        level_number = _section_number(words)
        if target_number and level_number and (
            level_number == target_number or level_number.startswith(f"{target_number}.")
        ):
            return True
    return False


_KEYWORD_STOP_WORDS = {
    "cau", "hoi", "tao", "sinh", "theo", "trong", "ngoai", "cho", "voi",
    "mot", "nhung", "cac", "noi", "dung", "kien", "thuc", "phan", "trinh",
}


def _keyword_tokens(text: str) -> set[str]:
    normalized = _strip_accents(text).lower()
    return {
        token
        for token in re.findall(r"[a-z0-9_]{3,}", normalized)
        if token not in _KEYWORD_STOP_WORDS
    }


def _looks_like_identifier_query(text: str) -> bool:
    """Return true only for identifiers that vector search commonly underweights."""
    return bool(
        re.search(r"\b[A-ZĐ]{2,}(?:[0-9_\-][A-ZĐ0-9_\-]*)?\b", text or "")
        or re.search(r"\b[A-Za-z]+_[A-Za-z0-9_]+\b|\b[A-Za-z]+\d+[A-Za-z0-9]*\b", text or "")
        or re.search(r"(?:->|::|==|!=|<=|>=)", text or "")
    )


def _requires_lexical_fallback(text: str, distances: list[float] | None) -> bool:
    if _looks_like_identifier_query(text):
        return True
    usable = [float(value) for value in distances or [] if value is not None]
    return bool(usable and min(usable) >= settings.lexical_fallback_distance_threshold)


def _hybrid_score(doc: str, meta: dict, query_tokens: set[str], vector_rank: int) -> float:
    doc_tokens = _keyword_tokens(f"{_build_heading_label(meta)} {doc}")
    lexical = len(query_tokens & doc_tokens) / max(1, len(query_tokens)) if query_tokens else 0.0
    density = min(1.0, max(0.0, float(meta.get("information_density", 0) or 0)))
    reciprocal_rank = 1.0 / (vector_rank + 1)
    return (0.35 * reciprocal_rank) + (0.60 * lexical) + (0.05 * density)


def get_context_snapshot(
    document_id: str,
    collection_name: str,
    target_heading: str = None,
    query_text: str = None,
    min_density: float = 0.0,
    limit: int = 5,
    avoid_ollama_gpu_wait: bool = False,
    pinned_snapshot: dict | None = None,
) -> dict:
    """Truy xuất các chunk từ ChromaDB làm Context cho LLM"""
    active_snapshot = _active_vector_snapshot(
        document_id,
        collection_name,
        pinned_snapshot,
    )
    if len(active_snapshot) == 2:
        chunk_set_id, vector_collection_id = active_snapshot
        active_collection_name = collection_name
    else:
        chunk_set_id, vector_collection_id, active_collection_name = active_snapshot
    collection = get_collection(active_collection_name)

    where_filter = {
        "$and": [
            {"document_id": document_id},
            {"chunk_set_id": chunk_set_id},
        ]
    }
    normalized_target = _normalize_heading_text(target_heading) if target_heading else ""
    semantic_query = " ".join(
        part.strip() for part in (target_heading, query_text) if part and part.strip()
    )
    query_tokens = _keyword_tokens(semantic_query)

    try:
        # Fetch beyond the final result limit because title-only chunks often rank highly
        # for a heading query but must never become evidence for a generated question.
        candidate_limit = max(1, min(max(limit * 10, limit, 1), collection.count()))
        ollama_is_using_gpu = (
            avoid_ollama_gpu_wait
            and current_gpu_operation_label() == "ollama"
        )
        if ollama_is_using_gpu:
            # Gemini is remote and must not sit behind a long local Ollama call
            # merely to embed one retrieval query. Use the bounded lexical pool
            # already stored in ChromaDB until the shared GPU becomes available.
            lexical_limit = min(
                max(limit * 60, 200),
                settings.lexical_fallback_max_chunks,
                collection.count(),
            )
            results = collection.get(
                where=where_filter,
                limit=max(1, lexical_limit),
                include=["documents", "metadatas"],
            )
            raw_docs = results.get("documents") or []
            raw_metas = results.get("metadatas") or []
            logger.info(
                "Ollama đang giữ GPU; dùng lexical retrieval cho Gemini để tránh chờ chéo provider"
            )
        elif semantic_query:
            results = collection.query(
                query_texts=[semantic_query],
                where=where_filter,
                n_results=candidate_limit,
                include=["documents", "metadatas", "distances"],
            )
            raw_docs = (results.get("documents") or [[]])[0] or []
            raw_metas = (results.get("metadatas") or [[]])[0] or []
            vector_distances = (results.get("distances") or [[]])[0] or []
            if query_tokens and _requires_lexical_fallback(semantic_query, vector_distances) and hasattr(collection, "get"):
                # Vector top-k alone can miss exact technical identifiers such
                # as FIFO/LIFO. Fetch a bounded lexical pool from the active
                # document and let the normalized hybrid score rerank the union.
                lexical_limit = min(
                    max(limit * 60, 200),
                    settings.lexical_fallback_max_chunks,
                    collection.count(),
                )
                lexical_results = collection.get(
                    where=where_filter,
                    limit=max(1, lexical_limit),
                    include=["documents", "metadatas"],
                )
                seen = {
                    (meta.get("chunk_id") or "", doc)
                    for doc, meta in zip(raw_docs, raw_metas)
                }
                for lexical_doc, lexical_meta in zip(
                    lexical_results.get("documents") or [],
                    lexical_results.get("metadatas") or [],
                ):
                    marker = (lexical_meta.get("chunk_id") or "", lexical_doc)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    raw_docs.append(lexical_doc)
                    raw_metas.append(lexical_meta)
        else:
            initial_limit = candidate_limit
            results = collection.get(
                where=where_filter,
                limit=initial_limit,
                include=["documents", "metadatas"],
            )
            raw_docs = results.get("documents") or []
            raw_metas = results.get("metadatas") or []
            seed_text = ""
            if raw_docs and raw_metas:
                seed_pairs = list(zip(raw_docs, raw_metas))
                seed_pairs.sort(
                    key=lambda x: (-(x[1].get("information_density") or 0.0), x[1].get("page_start") or 0)
                )
                seed_doc, seed_meta = seed_pairs[0]
                seed_text = (seed_doc[:500] if seed_doc else "") or _build_heading_label(seed_meta)

            if seed_text:
                query_results = collection.query(
                    query_texts=[seed_text],
                    where=where_filter,
                    n_results=initial_limit,
                    include=["documents", "metadatas"],
                )
                query_docs = (query_results.get("documents") or [[]])[0] or []
                query_metas = (query_results.get("metadatas") or [[]])[0] or []
                if query_docs:
                    raw_docs = query_docs
                    raw_metas = query_metas

        if not raw_docs:
            raise ValueError("Không tìm thấy đoạn văn nào hợp lệ để sinh câu hỏi.")

        chunks_with_meta = list(zip(raw_docs, raw_metas))
        candidate_chunks: list[tuple[str, dict]] = []
        heading_matched_chunks: list[tuple[str, dict]] = []
        for vector_rank, (doc, meta) in enumerate(chunks_with_meta):
            if not doc:
                continue
            if meta.get("requires_review"):
                continue
            if not _is_substantive_evidence(doc, meta):
                continue
            evidence_text = _evidence_text(doc)
            density = float(meta.get("information_density", 0) or 0)
            if density < min_density:
                continue

            ranked_meta = {
                **meta,
                "_hybrid_score": _hybrid_score(evidence_text, meta, query_tokens, vector_rank),
            }
            candidate_chunks.append((evidence_text, ranked_meta))
            if _heading_matches_target(ranked_meta, normalized_target):
                heading_matched_chunks.append((evidence_text, ranked_meta))

        filtered_chunks = heading_matched_chunks or candidate_chunks
        if not filtered_chunks:
            raise ValueError("Không tìm thấy đoạn văn nào hợp lệ theo tiêu chí đã chọn.")
        if not normalized_target:
            heading_scope = {"target": None, "status": "NOT_REQUESTED", "matched_chunks": 0}
        elif heading_matched_chunks:
            heading_scope = {
                "target": target_heading,
                "status": "MATCHED" if len(heading_matched_chunks) >= limit else "NARROW",
                "matched_chunks": len(heading_matched_chunks),
            }
        else:
            logger.info(
                "Không có heading khớp target_heading; dùng kết quả vector query làm fallback."
            )
            heading_scope = {"target": target_heading, "status": "NO_MATCH", "matched_chunks": 0}

        filtered_chunks.sort(
            key=lambda x: (-(x[1].get("_hybrid_score") or 0.0), x[1].get("page_start") or 0)
        )
        selected_chunks = filtered_chunks[:limit]

        assembled_context = []
        retrieval_results = []
        for doc, meta in selected_chunks:
            heading_label = _build_heading_label(meta)
            heading = f"[{heading_label}]" if heading_label else ""
            assembled_context.append(f"Mục lục: {heading}\nNội dung: {doc}")
            retrieval_results.append(
                {
                    "chunk_id": meta.get("chunk_id"),
                    "chunk_set_id": meta.get("chunk_set_id"),
                    "chunk_content_hash": meta.get("content_hash"),
                    "page_start": meta.get("page_start"),
                    "page_end": meta.get("page_end"),
                    "information_density": meta.get("information_density", 0),
                    "hybrid_score": round(float(meta.get("_hybrid_score", 0) or 0), 4),
                    "heading": heading_label,
                    "source_uri": meta.get("source_uri"),
                    "page_marks": _coerce_metadata_list(meta.get("page_marks")),
                    "content_type": meta.get("content_type"),
                    "source_block_types": meta.get("source_block_types_text"),
                    "asset_ids": meta.get("asset_ids_text"),
                    "source_asset_types": meta.get("source_asset_types_text"),
                }
            )

        return {
            "context_text": "\n\n---\n\n".join(assembled_context),
            # Chunks may contain Markdown rules (`---`), so consumers use this list instead of re-splitting.
            "context_sections": assembled_context,
            "results": retrieval_results,
            "chunk_set_id": chunk_set_id,
            "vector_collection_id": vector_collection_id,
            "heading_scope": heading_scope,
        }

    except Exception as e:
        logger.error(f"Lỗi khi truy xuất ChromaDB: {e}")
        raise ValueError(f"Lỗi truy xuất hệ thống Vector: {str(e)}")


def get_context_for_generation(
    document_id: str,
    collection_name: str,
    target_heading: str = None,
    query_text: str = None,
    min_density: float = 0.0,
    limit: int = 5,
) -> str:
    """Compatibility wrapper for callers that only need the assembled text."""
    return get_context_snapshot(
        document_id=document_id,
        collection_name=collection_name,
        target_heading=target_heading,
        query_text=query_text,
        min_density=min_density,
        limit=limit,
    )["context_text"]


def get_evaluation_evidence(
    document_id: str,
    query_text: str,
    collection_name: str | None = None,
    *,
    limit: int = 5,
    preferred_chunk_ids: list[str] | None = None,
) -> dict:
    """Retrieve and verify the current source chunks used by an AI evaluation.

    Unlike generation retrieval, this function always performs a semantic query
    and verifies every result against the active MongoDB chunk snapshot.  The
    returned text is therefore suitable for citation validation after the model
    responds.
    """
    query_text = re.sub(r"\s+", " ", str(query_text or "")).strip()
    if not query_text:
        raise ValueError("Không thể truy xuất nguồn vì nội dung đánh giá rỗng")

    resolved_collection = collection_name or settings.chromadb_collection_name
    chunk_set_id, vector_collection_id, active_collection_name = _active_vector_snapshot(
        document_id,
        resolved_collection,
    )
    collection = get_collection(active_collection_name)
    where_filter = {
        "$and": [
            {"document_id": document_id},
            {"chunk_set_id": chunk_set_id},
        ]
    }

    try:
        results = collection.query(
            query_texts=[query_text],
            where=where_filter,
            n_results=max(1, limit),
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        logger.error("Lỗi truy xuất bằng chứng đánh giá: %s", exc)
        raise ValueError(f"Lỗi truy xuất bằng chứng đánh giá: {exc}") from exc

    raw_docs = (results.get("documents") or [[]])[0] or []
    raw_metas = (results.get("metadatas") or [[]])[0] or []
    raw_distances = (results.get("distances") or [[]])[0] or []
    db = get_rag_db()
    verified_results = []

    for index, (content, metadata) in enumerate(zip(raw_docs, raw_metas)):
        chunk_id = metadata.get("chunk_id")
        if not content or not chunk_id:
            continue
        try:
            chunk_oid = ObjectId(str(chunk_id))
        except Exception:
            logger.warning("Bỏ qua chunk Chroma có id không hợp lệ: %s", chunk_id)
            continue

        chunk = db.document_chunks.find_one(
            {
                "_id": chunk_oid,
                "document_id": ObjectId(document_id),
                "chunk_set_id": ObjectId(chunk_set_id),
            }
        )
        if not chunk:
            continue
        content_hash = metadata.get("content_hash")
        if not content_hash or content_hash != chunk.get("content_hash"):
            logger.warning("Bỏ qua chunk %s vì content hash không khớp", chunk_id)
            continue
        if re.sub(r"\s+", " ", str(content)).strip() != re.sub(
            r"\s+", " ", str(chunk.get("content") or "")
        ).strip():
            logger.warning("Bỏ qua chunk %s vì nội dung Chroma không khớp MongoDB", chunk_id)
            continue

        distance = raw_distances[index] if index < len(raw_distances) else None
        similarity = None
        if isinstance(distance, (int, float)):
            similarity = round(max(0.0, min(1.0, 1.0 - float(distance))), 4)
        page_range = chunk.get("page_range") or {}
        verified_results.append(
            {
                "label": f"S{len(verified_results) + 1}",
                "chunk_id": str(chunk_oid),
                "chunk_set_id": chunk_set_id,
                "content_hash": content_hash,
                "page_start": metadata.get("page_start") or page_range.get("start"),
                "page_end": metadata.get("page_end") or page_range.get("end"),
                "heading": _build_heading_label(metadata),
                "similarity": similarity,
                "retrieval_mode": "SEMANTIC",
                "excerpt": re.sub(r"\s+", " ", str(content)).strip()[:4000],
            }
        )

    preferred_results = []
    for chunk_id in dict.fromkeys(preferred_chunk_ids or []):
        try:
            chunk_oid = ObjectId(str(chunk_id))
        except Exception:
            continue
        chunk = db.document_chunks.find_one(
            {
                "_id": chunk_oid,
                "document_id": ObjectId(document_id),
                "chunk_set_id": ObjectId(chunk_set_id),
            }
        )
        content = re.sub(r"\s+", " ", str((chunk or {}).get("content") or "")).strip()
        if not chunk or len(content) < 80:
            continue
        heading = chunk.get("heading") or {}
        heading_path = heading.get("path") or []
        heading_label = " > ".join(str(item) for item in heading_path if item)
        heading_label = heading_label or heading.get("title") or ""
        page_range = chunk.get("page_range") or {}
        semantic_match = next(
            (item for item in verified_results if item.get("chunk_id") == str(chunk_oid)),
            None,
        )
        preferred_results.append(
            {
                "label": "",
                "chunk_id": str(chunk_oid),
                "chunk_set_id": chunk_set_id,
                "content_hash": chunk.get("content_hash"),
                "page_start": page_range.get("start"),
                "page_end": page_range.get("end"),
                "heading": heading_label,
                "similarity": (semantic_match or {}).get("similarity"),
                "information_density": float(chunk.get("information_density") or 0),
                "retrieval_mode": "QUESTION_SOURCE",
                "excerpt": content[:4000],
            }
        )

    preferred_results.sort(
        key=lambda item: (
            -(item.get("information_density") or 0),
            -len(item.get("excerpt") or ""),
        )
    )
    preferred_ids = {item["chunk_id"] for item in preferred_results}
    merged_results = preferred_results + [
        item for item in verified_results if item.get("chunk_id") not in preferred_ids
    ]
    verified_results = merged_results[: max(1, limit)]
    for index, item in enumerate(verified_results, start=1):
        item["label"] = f"S{index}"

    return {
        "query": query_text,
        "document_id": document_id,
        "chunk_set_id": chunk_set_id,
        "vector_collection_id": vector_collection_id,
        "collection_name": active_collection_name,
        "results": verified_results,
    }
