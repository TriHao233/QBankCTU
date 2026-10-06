from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from core.config import settings
from core.database import get_database

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stale_time_filter(cutoff: datetime) -> dict:
    return {
        "$or": [
            {"updated_at": {"$lt": cutoff}},
            {"updated_at": {"$exists": False}, "started_at": {"$lt": cutoff}},
            {
                "updated_at": {"$exists": False},
                "started_at": None,
                "queued_at": {"$lt": cutoff},
            },
        ]
    }


def recover_stale_jobs(timeout_minutes: int | None = None) -> dict:
    db = get_database()
    now = utc_now()
    timeout = timeout_minutes or settings.job_recovery_timeout_minutes
    cutoff = now - timedelta(minutes=max(timeout, 1))
    message = (
        "Job was active before backend startup and exceeded "
        f"{timeout} minute recovery timeout"
    )
    results = {
        "generation_requeued": _recover_generation_jobs(db, cutoff, now, message),
        "evaluation_stale": (
            _recover_postgres_evaluation_jobs(cutoff, message)
            if settings.question_store == "postgres"
            else _recover_evaluation_jobs(db, cutoff, now, message)
        ),
        "document_failed": (
            _recover_postgres_document_jobs(cutoff, message)
            if settings.document_store == "postgres"
            else _recover_document_jobs(db, cutoff, now, message)
        ),
    }
    total = sum(results.values())
    if total:
        logger.warning("Recovered stale background jobs on startup: %s", results)
    return results


def _recover_postgres_document_jobs(cutoff: datetime, message: str) -> int:
    from modules.documents.postgres_repository import PostgresDocumentRepository
    return PostgresDocumentRepository().recover_stale_jobs(cutoff, message)


def _recover_postgres_evaluation_jobs(cutoff: datetime, message: str) -> int:
    from modules.questions.postgres_evaluation_jobs import PostgresEvaluationJobs
    return PostgresEvaluationJobs().recover_stale(cutoff, message)


def _recover_generation_jobs(db, cutoff: datetime, now: datetime, message: str) -> int:
    """Queued jobs are left for the worker; only processing jobs whose lease expired are requeued."""
    if settings.generation_store == "postgres":
        from modules.generation.postgres_store import PostgresGenerationStore
        result = PostgresGenerationStore().requeue_orphaned(WORKER_RESTART_MESSAGE)
    else:
        result = _requeue_orphaned_mongo_generation_jobs(db, now)
    return result["requeued"] + result["dead_lettered"]


WORKER_RESTART_MESSAGE = "Worker đã khởi động lại; job được xếp hàng chạy lại"


def requeue_orphaned_generation_jobs_on_worker_start() -> dict:
    """Return generation jobs abandoned by a stopped worker to the queue.

    Queued jobs and jobs still holding a live lease (another worker) are left alone.
    Abandoned jobs that already used every attempt are dead-lettered.
    """
    if settings.generation_store == "postgres":
        from modules.generation.postgres_store import PostgresGenerationStore
        result = PostgresGenerationStore().requeue_orphaned(WORKER_RESTART_MESSAGE)
    else:
        result = _requeue_orphaned_mongo_generation_jobs(get_database(), utc_now())
    if result["requeued"] or result["dead_lettered"]:
        logger.warning("Recovered generation jobs from a previous worker: %s", result)
    return result


def _requeue_orphaned_mongo_generation_jobs(db, now: datetime) -> dict:
    orphaned = {
        "status": "processing",
        "$or": [{"lease_expires_at": {"$lte": now}}, {"lease_expires_at": None}],
    }
    result = {"requeued": 0, "dead_lettered": 0}
    for job in list(db.generation_jobs.find(orphaned, {"attempt_count": 1, "max_attempts": 1})):
        attempts = int(job.get("attempt_count") or 0)
        max_attempts = int(job.get("max_attempts") or settings.job_max_attempts)
        if attempts >= max_attempts:
            fields = {
                "status": "failed",
                "error_message": WORKER_RESTART_MESSAGE,
                "dead_lettered_at": now,
                "expires_at": now + timedelta(days=settings.job_retention_days),
                "updated_at": now,
                "progress": {"stage": "failed", "completed": 0, "total": 1},
            }
            outcome = "dead_lettered"
        else:
            fields = {
                "status": "queued",
                "error_message": WORKER_RESTART_MESSAGE,
                "last_failed_at": now,
                "next_attempt_at": now,
                "updated_at": now,
                "progress": {"stage": "retry_wait", "completed": 0, "total": 1},
            }
            outcome = "requeued"
        updated = db.generation_jobs.update_one(
            {"_id": job["_id"], **orphaned},
            {"$set": fields, "$unset": {"locked_by": "", "lease_expires_at": "", "heartbeat_at": ""}},
        )
        result[outcome] += updated.modified_count
    return result


def _recover_evaluation_jobs(db, cutoff: datetime, now: datetime, message: str) -> int:
    active_jobs = list(
        db.evaluation_jobs.find(
            {
                "status": {"$in": ["QUEUED", "PROCESSING"]},
                **_stale_time_filter(cutoff),
            }
        )
    )
    error = {"message": message, "at": now}
    for job in active_jobs:
        db.evaluation_jobs.update_one(
            {"_id": job["_id"], "status": {"$in": ["QUEUED", "PROCESSING"]}},
            {
                "$set": {
                    "status": "STALE",
                    "error": error,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
        )
        db.questions.update_one(
            {
                "_id": job.get("question_id"),
                "evaluation_status": {"$in": ["QUEUED", "PROCESSING"]},
                "quality_summary.latest_evaluation_job_id": job["_id"],
            },
            {
                "$set": {
                    "evaluation_status": "STALE",
                    "quality_summary.error": error,
                    "updated_at": now,
                }
            },
        )
    return len(active_jobs)


def _recover_document_jobs(db, cutoff: datetime, now: datetime, message: str) -> int:
    active_jobs = list(
        db.document_jobs.find(
            {
                "status": {"$in": ["QUEUED", "PROCESSING"]},
                **_stale_time_filter(cutoff),
            }
        )
    )
    error = {"message": message, "at": now}
    for job in active_jobs:
        db.document_jobs.update_one(
            {"_id": job["_id"], "status": {"$in": ["QUEUED", "PROCESSING"]}},
            {
                "$set": {
                    "status": "FAILED",
                    "error": error,
                    "finished_at": now,
                    "updated_at": now,
                }
            },
        )
        job_type = str(job.get("job_type") or "UNKNOWN").lower()
        document_fields = {
            "status": "FAILED",
            f"pipeline_summary.{job_type}_status": "FAILED",
            f"pipeline_attempts.{job_type}.status": "FAILED",
            "latest_error": {
                "job_id": job["_id"],
                "job_type": job.get("job_type"),
                "message": message,
                "at": now,
            },
            "updated_at": now,
        }
        if job.get("preserves_active_pipeline"):
            # A failed re-index must not take down the index that is still serving generation.
            previous = job.get("previous_document_state") or {}
            document_fields["status"] = previous.get("status") or "READY"
            if previous.get("pipeline_status"):
                document_fields[f"pipeline_summary.{job_type}_status"] = previous["pipeline_status"]
            else:
                document_fields.pop(f"pipeline_summary.{job_type}_status")
        db.documents.update_one({"_id": job.get("document_id"), "archived_at": None}, {"$set": document_fields})
        if job.get("job_type") == "CHUNK":
            chunk_sets = list(
                db.chunk_sets.find(
                    {"chunk_job_id": job["_id"], "status": "PROCESSING"},
                    {"_id": 1},
                )
            )
            chunk_set_ids = [chunk_set["_id"] for chunk_set in chunk_sets]
            db.chunk_sets.update_many(
                {"chunk_job_id": job["_id"], "status": "PROCESSING"},
                {
                    "$set": {
                        "status": "FAILED",
                        "error": error,
                        "completed_at": now,
                    }
                },
            )
            if chunk_set_ids:
                db.chunk_embeddings.update_many(
                    {"status": "PENDING", "chunk_set_id": {"$in": chunk_set_ids}},
                    {"$set": {"status": "FAILED", "error": error, "updated_at": now}},
                )
    return len(active_jobs)
