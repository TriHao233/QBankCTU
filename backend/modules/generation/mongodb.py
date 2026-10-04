import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone

from bson.errors import InvalidId
from bson.objectid import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import get_database
from modules.catalog.postgres_subject_repository import subject_record
from modules.documents.repository import object_id
from modules.documents.store import get_document_repository
from modules.questions.schemas import QuestionDifficulty

def utc_now():
    return datetime.now(timezone.utc)


def _postgres_store():
    """PostgreSQL generation store when GENERATION_STORE=postgres, else None."""
    if settings.generation_store != "postgres":
        return None
    from modules.generation.postgres_store import PostgresGenerationStore
    return PostgresGenerationStore()


def get_document_learning_outcomes(document_id: str) -> list[dict]:
    db = get_database()
    try:
        document = get_document_repository(db).find_by_id(object_id(document_id, "document_id"))
    except ValueError:
        return []
    if not document or not document.get("subject_id"):
        return []
    subject = subject_record(db, document["subject_id"], active_only=True)
    if not subject:
        return []
    return [
        {
            "id": str(item["_id"]),
            "clo_code": str(item.get("clo_code") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "outcome_type": str(item.get("outcome_type") or "kien_thuc").strip() or "kien_thuc",
        }
        for item in (subject.get("learning_outcomes") or [])
        if item.get("_id") and item.get("is_active", True)
    ]


def _fold_tokens(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").lower())
    ascii_text = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return {token for token in re.findall(r"[a-z0-9_]{3,}", ascii_text)}


def _resolve_clo_ids(
    document_id: str,
    question: dict,
    outcomes: list[dict] | None = None,
) -> list[str]:
    outcomes = outcomes if outcomes is not None else get_document_learning_outcomes(document_id)
    if not outcomes:
        return []
    by_code = {item["clo_code"].upper(): item for item in outcomes if item["clo_code"]}
    selected = []
    for code in question.get("clo_codes") or []:
        item = by_code.get(str(code).strip().upper())
        if item and item["id"] not in selected:
            selected.append(item["id"])
    if selected:
        return selected[:2]

    question_text = " ".join(
        str(question.get(field) or "")
        for field in ("question", "explanation", "source_context")
    )
    question_tokens = _fold_tokens(question_text)
    ranked = []
    for item in outcomes:
        outcome_tokens = _fold_tokens(f"{item['clo_code']} {item['description']}")
        overlap = len(question_tokens & outcome_tokens)
        if overlap:
            ranked.append((overlap / max(1, len(outcome_tokens)), overlap, item["id"]))
    ranked.sort(reverse=True)
    return [item_id for _ratio, _overlap, item_id in ranked[:2]]


def _model_snapshot(database, provider: str, fallback: dict | None = None) -> dict:
    if settings.ai_config_store == "postgres":
        from modules.catalog.postgres_ai_repository import PostgresAiRepository
        model = PostgresAiRepository().model(provider)
        if model and not model["is_active"]:
            raise ValueError("Mô hình AI này đang tạm dừng")
    else:
        model = database.ai_models.find_one({"model_code": provider, "is_active": True})
    if not model:
        return fallback or {"provider": provider}
    return {
        "id": model["_id"],
        "model_code": model["model_code"],
        "model_name": model.get("model_name"),
        "runtime": model.get("runtime"),
        "revision": model.get("revision"),
        "capabilities": model.get("capabilities") or [],
        "is_local": model.get("is_local", True),
        "config": model.get("config") or {},
    }


def create_generation_run(
    *,
    document_id: str,
    requested_by_user_id,
    request_snapshot: dict,
    model_snapshot: dict,
    rendered_prompt: str,
    context_text: str,
    retrieval_results: list[dict],
    chunk_set_id: str,
    vector_collection_id: str,
) -> str:
    document_oid = object_id(document_id, "document_id")
    db = get_database()
    provider = model_snapshot.get("provider") or model_snapshot.get("model_code") or ""
    if provider and not model_snapshot.get("source"):
        model_snapshot = _model_snapshot(db, provider, model_snapshot)
    document = get_document_repository(db).find_by_id(document_oid)
    if not document:
        raise ValueError("Không tìm thấy tài liệu")
    chunk_set_oid = object_id(chunk_set_id, "chunk_set_id")
    vector_collection_oid = object_id(
        vector_collection_id,
        "vector_collection_id",
    )
    if not db.chunk_sets.find_one(
        {
            "_id": chunk_set_oid,
            "document_id": document_oid,
            "status": "COMPLETED",
        },
        {"_id": 1},
    ):
        raise ValueError("Chunk set truy xuất không hợp lệ")
    if not db.vector_collections.find_one(
        {"_id": vector_collection_oid, "is_active": True},
        {"_id": 1},
    ):
        raise ValueError("Vector collection truy xuất không hợp lệ")
    if any(
        result.get("chunk_set_id") != chunk_set_id
        for result in retrieval_results
    ):
        raise ValueError("Kết quả retrieval không cùng chunk set")
    now = utc_now()
    record = {
        "_id": ObjectId(),
        "schema_version": SCHEMA_VERSION,
        "requested_by_user_id": requested_by_user_id,
        "document_id": document_oid,
        "document_version": document.get("current_version", 1),
        "chunk_set_id": chunk_set_oid,
        "vector_collection_id": vector_collection_oid,
        "subject": {"id": document.get("subject_id")},
        "chapter": {"id": document.get("chapter_id")},
        "request": request_snapshot,
        "model": model_snapshot,
        "prompts": [],
        "rendered_prompt": rendered_prompt,
        "rendered_prompt_hash": hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest(),
        "retrieval": {
            "context_hash": hashlib.sha256(context_text.encode("utf-8")).hexdigest(),
            "context_excerpt": context_text[:4000],
            "results": retrieval_results,
        },
        "execution": {
            "attempt_no": 1,
            "latency_ms": None,
            "parser_version": "question-json-v2",
        },
        "raw_model_response": None,
        "status": "GENERATING",
        "generated_count": 0,
        "validation_errors": [],
        "error": None,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
    }
    store = _postgres_store()
    if store is not None:
        return store.create_run(record)
    db.generation_runs.insert_one(record)
    return str(record["_id"])


def finish_generation_run(
    generation_run_id: str,
    *,
    status: str,
    raw_model_response: str | None = None,
    generated_count: int = 0,
    latency_ms: int | None = None,
    error_message: str | None = None,
    validation_errors: list[dict] | None = None,
    post_processing: dict | None = None,
    model_execution: dict | None = None,
) -> None:
    now = utc_now()
    fields = {
        "status": status.upper(),
        "raw_model_response": raw_model_response,
        "generated_count": generated_count,
        "execution.latency_ms": latency_ms,
        "error": {"message": error_message, "at": now} if error_message else None,
        "finished_at": now,
    }
    if validation_errors is not None:
        fields["validation_errors"] = validation_errors
    if post_processing is not None:
        fields["post_processing"] = post_processing
    if model_execution is not None:
        fields["execution.model"] = model_execution
    store = _postgres_store()
    if store is not None:
        execution = {"latency_ms": fields.pop("execution.latency_ms")}
        if "execution.model" in fields:
            execution["model"] = fields.pop("execution.model")
        store.finish_run(generation_run_id, fields, execution)
        return
    get_database().generation_runs.update_one(
        {"_id": object_id(generation_run_id, "generation_run_id")},
        {"$set": fields},
    )


def get_existing_question_texts(document_id: str, *, limit: int = 2000) -> list[str]:
    """Load current active question text for post-generation deduplication only."""
    document_oid = object_id(document_id, "document_id")
    if settings.question_store == "postgres":
        from core.postgres import postgres_connection
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT v.content FROM questions q
                   JOIN question_versions v ON v.id=q.current_version_id
                   WHERE q.lifecycle_status='ACTIVE' AND v.payload->>'document_id'=%s
                   LIMIT %s""",
                (str(document_oid), max(1, limit)),
            ).fetchall()
        return [str(row["content"] or "").strip() for row in rows
                if str(row["content"] or "").strip()]
    pipeline = [
        {
            "$match": {
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
                "current_version_id": {"$exists": True},
            }
        },
        {
            "$lookup": {
                "from": "question_versions",
                "localField": "current_version_id",
                "foreignField": "_id",
                "as": "current_version",
            }
        },
        {"$unwind": "$current_version"},
        {"$match": {"current_version.document_id": document_oid}},
        {"$project": {"_id": 0, "content": "$current_version.content"}},
        {"$limit": max(1, limit)},
    ]
    return [
        str(item.get("content") or "").strip()
        for item in get_database().questions.aggregate(pipeline)
        if str(item.get("content") or "").strip()
    ]


def save_generated_questions(
    document_id: str,
    questions: list,
    *,
    generation_run_id: str,
    requested_by_user_id,
    source_chunk_ids: list[str],
    learning_outcomes: list[dict] | None = None,
) -> list[dict]:
    # Generated candidates stay attached to the generation job until the
    # teacher explicitly submits them. This prevents unreviewed AI output
    # from filling the question bank with draft records.
    prepared_questions = []
    allowed_difficulties = {item.value for item in QuestionDifficulty}
    for question in questions:
        difficulty = question.get("difficulty")
        if difficulty not in allowed_difficulties:
            difficulty = None
        prepared_questions.append(
            {
                **question,
                "difficulty": difficulty,
                "document_id": document_id,
                "generation_run_id": generation_run_id,
                "source_chunk_ids": source_chunk_ids,
                "clo_ids": _resolve_clo_ids(document_id, question, learning_outcomes),
                "validation_warnings": question.get("validation_warnings") or [],
            }
        )
    return prepared_questions


def create_generation_job(
    request: dict,
    requested_by_user_id=None,
    idempotency_key: str | None = None,
    *,
    model_snapshot: dict | None = None,
    code_model_snapshot: dict | None = None,
    fallback_model_snapshot: dict | None = None,
) -> str:
    """Tạo job sinh câu hỏi với trạng thái queued (dùng cho polling ở FE)."""
    store = _postgres_store()
    if store is not None:
        return store.create_job(
            request, requested_by_user_id, idempotency_key,
            model_snapshot=model_snapshot, code_model_snapshot=code_model_snapshot,
            fallback_model_snapshot=fallback_model_snapshot,
        )
    db = get_database()
    now = utc_now()
    doc = {
        "request": request,
        "model_snapshot": model_snapshot,
        "code_model_snapshot": code_model_snapshot,
        "fallback_model_snapshot": fallback_model_snapshot,
        "requested_by_user_id": requested_by_user_id,
        "idempotency_key": idempotency_key,
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": settings.job_max_attempts,
        "result": None,
        "metrics": None,
        "progress": {"stage": "queued", "completed": 0, "total": 0},
        "error_message": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = db["generation_jobs"].insert_one(doc)
    except DuplicateKeyError:
        existing = db.generation_jobs.find_one(
            {
                "requested_by_user_id": requested_by_user_id,
                "idempotency_key": idempotency_key,
            },
            {"_id": 1},
        )
        if not existing:
            raise
        return str(existing["_id"])
    return str(result.inserted_id)


def get_generation_job_by_idempotency(requested_by_user_id, idempotency_key: str) -> dict | None:
    store = _postgres_store()
    if store is not None:
        return store.get_by_idempotency(requested_by_user_id, idempotency_key)
    doc = get_database().generation_jobs.find_one(
        {"requested_by_user_id": requested_by_user_id, "idempotency_key": idempotency_key}
    )
    return _serialize_generation_job(doc)


def count_active_generation_jobs(requested_by_user_id) -> int:
    store = _postgres_store()
    if store is not None:
        return store.count_active(requested_by_user_id)
    return get_database().generation_jobs.count_documents(
        {"requested_by_user_id": requested_by_user_id, "status": {"$in": ["queued", "processing"]}}
    )


def update_generation_job(
    job_id: str,
    status: str,
    result: dict | None = None,
    metrics: dict | None = None,
    error_message: str | None = None,
    worker_id: str | None = None,
):
    """Cập nhật trạng thái job sinh câu hỏi."""
    store = _postgres_store()
    if store is not None:
        return store.update(job_id, status, result, metrics, error_message, worker_id)
    db = get_database()
    update_data: dict = {
        "status": status,
        "updated_at": utc_now(),
    }
    if status == "completed":
        update_data["progress"] = {"stage": "completed", "completed": 1, "total": 1}
    elif status == "failed":
        update_data["progress"] = {"stage": "failed", "completed": 0, "total": 1}
    if result is not None:
        update_data["result"] = result
    if metrics is not None:
        update_data["metrics"] = metrics
    if error_message is not None:
        update_data["error_message"] = error_message

    query = {"_id": ObjectId(job_id)}
    if worker_id:
        query["locked_by"] = worker_id
    update: dict = {"$set": update_data}
    if status in {"completed", "failed", "cancelled"}:
        update_data["expires_at"] = utc_now() + timedelta(days=settings.job_retention_days)
        update["$unset"] = {
            "locked_by": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "next_attempt_at": "",
        }
    return db["generation_jobs"].update_one(query, update).modified_count == 1


def update_generation_progress(job_id: str, worker_id: str, progress: dict) -> bool:
    store = _postgres_store()
    if store is not None:
        return store.update_progress(job_id, worker_id, progress)
    result = get_database().generation_jobs.update_one(
        {"_id": ObjectId(job_id), "status": "processing", "locked_by": worker_id},
        {"$set": {"progress": progress, "updated_at": utc_now()}},
    )
    return result.matched_count == 1


def _serialize_generation_job(doc: dict | None) -> dict | None:
    if not doc:
        return None
    result = dict(doc)
    result["job_id"] = str(result.pop("_id"))
    return result


def get_generation_job(job_id: str, *, requested_by_user_id=None) -> dict | None:
    """Lấy thông tin job sinh câu hỏi theo job_id."""
    store = _postgres_store()
    if store is not None:
        return store.get(job_id, requested_by_user_id=requested_by_user_id)
    db = get_database()
    try:
        query = {"_id": ObjectId(job_id)}
        if requested_by_user_id is not None:
            query["requested_by_user_id"] = requested_by_user_id
        doc = db["generation_jobs"].find_one(query)
    except (InvalidId, TypeError):
        return None
    return _serialize_generation_job(doc)


def cancel_generation_job(job_id: str, *, requested_by_user_id=None) -> bool:
    store = _postgres_store()
    if store is not None:
        return store.cancel(job_id, requested_by_user_id=requested_by_user_id) is not None
    try:
        query = {"_id": ObjectId(job_id), "status": {"$in": ["queued", "processing"]}}
    except (InvalidId, TypeError):
        return False
    if requested_by_user_id is not None:
        query["requested_by_user_id"] = requested_by_user_id
    now = utc_now()
    result = get_database().generation_jobs.update_one(
        query,
        {
            "$set": {
                "status": "failed",
                "error_message": "Đã dừng theo yêu cầu của người dùng",
                "updated_at": now,
                "expires_at": now + timedelta(days=settings.job_retention_days),
                "progress": {"stage": "cancelled", "completed": 0, "total": 1},
            },
            "$unset": {"locked_by": "", "lease_expires_at": "", "heartbeat_at": "", "next_attempt_at": ""},
        },
    )
    return result.modified_count == 1


def claim_generation_job(job_id: str, worker_id: str) -> dict | None:
    """Atomically claim one queued job so multiple workers cannot run it twice."""
    store = _postgres_store()
    if store is not None:
        return store.claim(job_id, worker_id)
    try:
        object_id = ObjectId(job_id)
    except (InvalidId, TypeError):
        return None
    now = utc_now()
    doc = get_database()["generation_jobs"].find_one_and_update(
        {
            "_id": object_id,
            "$or": [
                {
                    "status": "queued",
                    "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": now}},
                    ],
                },
                {"status": "processing", "lease_expires_at": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "status": "processing",
                "locked_by": worker_id,
                "started_at": now,
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=settings.job_lease_seconds),
                "updated_at": now,
                "progress": {"stage": "starting", "completed": 0, "total": 0},
            },
            "$inc": {"attempt_count": 1},
            "$unset": {"next_attempt_at": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    return _serialize_generation_job(doc)


def get_next_queued_generation_job_id(provider_group: str | None = None) -> str | None:
    """Return the oldest runnable generation job, optionally for one provider lane.

    Ollama-backed models and Gemini are deliberately selected independently so a
    slow local generation cannot head-of-line block a remote Gemini request (and
    vice versa).
    """
    store = _postgres_store()
    if store is not None:
        return store.next_queued_id(provider_group)
    now = utc_now()
    runnable_filter = {
        "$or": [
            {
                "status": "queued",
                "$or": [
                    {"next_attempt_at": {"$exists": False}},
                    {"next_attempt_at": {"$lte": now}},
                ],
            },
            {"status": "processing", "lease_expires_at": {"$lte": now}},
        ]
    }
    normalized_group = (provider_group or "").strip().lower()
    if normalized_group == "gemini":
        provider_filter = {
            "$or": [
                {"model_snapshot.runtime": "GEMINI"},
                {
                    "model_snapshot.runtime": {"$exists": False},
                    "request.model_provider": "gemini",
                },
            ]
        }
    elif normalized_group == "ollama":
        provider_filter = {
            "$or": [
                {"model_snapshot.runtime": "OLLAMA"},
                {
                    "model_snapshot.runtime": {"$exists": False},
                    "request.model_provider": {"$ne": "gemini"},
                },
            ]
        }
    else:
        provider_filter = None

    query = (
        {"$and": [runnable_filter, provider_filter]}
        if provider_filter is not None
        else runnable_filter
    )
    doc = get_database()["generation_jobs"].find_one(
        query,
        sort=[("created_at", 1)],
        projection={"_id": 1},
    )
    return str(doc["_id"]) if doc else None


def heartbeat_generation_job(job_id: str, worker_id: str) -> bool:
    store = _postgres_store()
    if store is not None:
        return store.heartbeat(job_id, worker_id)
    now = utc_now()
    result = get_database().generation_jobs.update_one(
        {"_id": ObjectId(job_id), "status": "processing", "locked_by": worker_id},
        {
            "$set": {
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=settings.job_lease_seconds),
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


def retry_or_dead_letter_generation_job(
    job: dict,
    worker_id: str,
    *,
    error_message: str,
    metrics: dict | None = None,
) -> str:
    store = _postgres_store()
    if store is not None:
        return store.retry_or_dead_letter(job, worker_id, error_message=error_message,
                                          metrics=metrics)
    now = utc_now()
    attempts = int(job.get("attempt_count") or 1)
    max_attempts = int(job.get("max_attempts") or settings.job_max_attempts)
    if attempts < max_attempts:
        delay = min(
            settings.job_retry_base_seconds * (2 ** max(0, attempts - 1)),
            settings.job_retry_max_seconds,
        )
        status = "queued"
        fields = {
            "status": status,
            "error_message": error_message,
            "last_failed_at": now,
            "next_attempt_at": now + timedelta(seconds=delay),
            "updated_at": now,
            "progress": {"stage": "retry_wait", "completed": 0, "total": 1},
        }
    else:
        status = "failed"
        fields = {
            "status": status,
            "error_message": error_message,
            "dead_lettered_at": now,
            "expires_at": now + timedelta(days=settings.job_retention_days),
            "updated_at": now,
            "progress": {"stage": "failed", "completed": 0, "total": 1},
        }
    if metrics is not None:
        fields["metrics"] = metrics
    get_database().generation_jobs.update_one(
        {"_id": ObjectId(job["job_id"]), "status": "processing", "locked_by": worker_id},
        {
            "$set": fields,
            "$unset": {"locked_by": "", "lease_expires_at": "", "heartbeat_at": ""},
        },
    )
    return status
