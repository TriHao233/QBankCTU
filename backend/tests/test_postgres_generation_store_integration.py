"""Run with RUN_POSTGRES_INTEGRATION=1 and POSTGRES_DSN on a test database."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks

from core import job_recovery
from core.config import settings
from core.dependencies import CurrentUser
from core.postgres import postgres_connection
from db.copy_business_data import projected_rows, upsert
from modules.admin.job_metrics import collect_job_metrics
from modules.admin.jobs_service import AdminJobService
from modules.admin.overview_service import AdminOverviewService
from modules.generation import mongodb as generation
from modules.generation.postgres_store import PostgresGenerationStore
from modules.questions.postgres_repository import PostgresQuestionRepository
from modules.users.postgres_repository import PostgresUserRepository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Requires an explicitly configured PostgreSQL test database",
)

GEMINI = {"model_code": "gemini-test", "model_name": "Gemini test", "runtime": "GEMINI"}


def _expire_backoff(job_id: str) -> None:
    with postgres_connection() as conn:
        conn.execute("UPDATE generation_jobs SET next_attempt_at=now()-interval '1 second' "
                     "WHERE id=%s", (job_id,))


def test_generation_queue_runs_and_admin_views_use_postgres(monkeypatch):
    for key in ("user_store", "catalog_store", "document_store", "question_store",
                "generation_store", "llm_slot_store"):
        monkeypatch.setattr(settings, key, "postgres")
    monkeypatch.setattr(settings, "job_max_attempts", 2)
    suffix = uuid4().hex[:12]
    users = PostgresUserRepository()
    teacher = users.create({"firebase_uid": f"g-{suffix}", "email": f"g-{suffix}@example.test",
                            "display_name": "Generator", "role": "Teacher"})
    admin = users.create({"firebase_uid": f"ga-{suffix}", "email": f"ga-{suffix}@example.test",
                          "display_name": "Admin", "role": "Admin"})
    admin_actor = CurrentUser(id=admin["_id"], firebase_uid="", email=admin["email"],
                              role="Admin", is_active=True, permissions=())
    uid = teacher["_id"]
    now = datetime.now(timezone.utc)
    document_id, question_id, version_id = ObjectId(), ObjectId(), ObjectId()
    request = {"document_id": str(document_id), "model_provider": "gemini-test",
               "client_telemetry": {"elapsed_before_generate_ms": 10}}
    store = PostgresGenerationStore()
    try:
        with postgres_connection() as conn:
            for table, row in projected_rows("documents", {
                "_id": document_id, "title": "Generation doc", "original_filename": "g.pdf",
                "uploaded_by_user_id": uid, "status": "READY", "current_version": 1,
                "created_at": now, "updated_at": now,
            }):
                upsert(conn, table, row)

        # Idempotent enqueue, per-lane selection, claim and progress.
        job_id = generation.create_generation_job(request, uid, f"idem-{suffix}",
                                                  model_snapshot=GEMINI,
                                                  code_model_snapshot=GEMINI)
        assert generation.create_generation_job(request, uid, f"idem-{suffix}",
                                                model_snapshot=GEMINI) == job_id
        assert generation.get_generation_job_by_idempotency(uid, f"idem-{suffix}")["job_id"] == job_id
        assert generation.count_active_generation_jobs(uid) == 1
        job = generation.get_generation_job(job_id, requested_by_user_id=uid)
        assert job["request"]["document_id"] == str(document_id)
        assert job["code_model_snapshot"]["runtime"] == "GEMINI"
        assert generation.get_generation_job(job_id, requested_by_user_id=admin["_id"]) is None
        assert generation.get_next_queued_generation_job_id("ollama") is None
        assert generation.get_next_queued_generation_job_id("gemini") == job_id
        claimed = generation.claim_generation_job(job_id, "worker-a")
        assert claimed["status"] == "processing" and claimed["attempt_count"] == 1
        assert generation.claim_generation_job(job_id, "worker-b") is None
        assert generation.heartbeat_generation_job(job_id, "worker-b") is False
        assert generation.heartbeat_generation_job(job_id, "worker-a") is True
        assert generation.update_generation_progress(job_id, "worker-a",
                                                     {"stage": "retrieval", "completed": 1,
                                                      "total": 3}) is True

        # Retry with backoff, then dead-letter on the last attempt.
        assert generation.retry_or_dead_letter_generation_job(
            claimed, "worker-a", error_message="timeout") == "queued"
        assert generation.get_generation_job(job_id)["progress"]["stage"] == "retry_wait"
        assert generation.claim_generation_job(job_id, "worker-a") is None
        _expire_backoff(job_id)
        claimed = generation.claim_generation_job(job_id, "worker-a")
        assert claimed["attempt_count"] == 2
        assert generation.retry_or_dead_letter_generation_job(
            claimed, "worker-a", error_message="still down") == "failed"
        dead = generation.get_generation_job(job_id)
        assert dead["dead_lettered_at"] is not None and dead["locked_by"] is None

        # Completion keeps the result JSON exactly as the API reads it.
        done_id = generation.create_generation_job(request, uid, model_snapshot=GEMINI)
        generation.claim_generation_job(done_id, "worker-a")
        assert generation.update_generation_job(done_id, "completed", worker_id="worker-b") is False
        assert generation.update_generation_job(
            done_id, "completed", result={"status": "ok", "data": [], "summary": []},
            metrics={"server": {"total_ms": 5}}, worker_id="worker-a") is True
        done = generation.get_generation_job(done_id)
        assert done["status"] == "completed" and done["result"]["status"] == "ok"
        assert done["expires_at"] is not None and done["progress"]["stage"] == "completed"

        cancelled_id = generation.create_generation_job(request, uid, model_snapshot=GEMINI)
        assert generation.cancel_generation_job(cancelled_id, requested_by_user_id=uid) is True
        assert generation.cancel_generation_job(cancelled_id, requested_by_user_id=uid) is False
        assert generation.get_generation_job(cancelled_id)["progress"]["stage"] == "cancelled"

        # Admin views, cancel and retry.
        service = AdminJobService(SimpleNamespace())
        listed = service.list_jobs(page=1, page_size=20, job_kind="generation", user_id=str(uid))
        assert listed["total"] == 3
        assert service.list_jobs(page=1, page_size=20, job_kind="generation",
                                 status="retryable", user_id=str(uid))["total"] == 2
        admin_cancel_id = generation.create_generation_job(request, uid, model_snapshot=GEMINI)
        assert service.cancel_job("generation", admin_cancel_id, admin_actor)["job"]["status"] == "failed"
        retried = service.retry_job("generation", job_id, BackgroundTasks(), admin_actor)
        assert retried["job"]["model_snapshot"]["runtime"] == "GEMINI"
        assert retried["job"]["status"] == "queued"
        metrics = collect_job_metrics(MagicMock())["queues"]["generation"]
        assert metrics["queued"] >= 1 and metrics["dead_lettered"] >= 1
        with postgres_connection() as conn:
            actions = {row["action"] for row in conn.execute(
                "SELECT action FROM audit_logs WHERE entity_type='generation' AND entity_id=ANY(%s)",
                ([job_id, admin_cancel_id],),
            ).fetchall()}
        assert actions == {"admin.job_retry", "admin.job_cancel"}

        # Runs: create, finish and report model performance.
        run_id = store.create_run({
            "_id": ObjectId(), "requested_by_user_id": uid, "document_id": document_id,
            "request": request, "model": GEMINI, "prompts": [],
            "retrieval": {"results": [{"chunk_id": "c1"}]},
            "execution": {"attempt_no": 1, "latency_ms": None}, "status": "GENERATING",
            "created_at": now, "started_at": now, "finished_at": None,
        })
        generation.finish_generation_run(run_id, status="completed", generated_count=2,
                                         latency_ms=1500, model_execution={"used": "gemini"})
        groups: dict = {}
        AdminOverviewService(SimpleNamespace())._collect_generation_model_performance(
            groups, now - timedelta(days=1))
        group = groups["generation:gemini-test"]
        assert group["completed"] == 1 and group["_latencies"] == [1500]

        # Dedup source texts come from PostgreSQL questions of this document.
        PostgresQuestionRepository().create({
            "_id": question_id, "schema_version": 2, "question_code": f"G-{suffix}",
            "created_by_user_id": uid, "current_version": 1, "current_version_id": version_id,
            "approved_version_id": None, "lifecycle_status": "ACTIVE",
            "review_status": "DRAFT", "evaluation_status": "NOT_STARTED",
            "publication_status": "NOT_PUBLISHED", "review_assignment": {},
            "created_at": now, "updated_at": now,
        }, {
            "_id": version_id, "question_id": question_id, "version": 1, "origin": "AI",
            "content": "Câu đã có", "document_id": document_id,
            "question_data": {}, "classification": {}, "clos": [], "sources": [],
            "content_hash": f"h-{suffix}", "created_by_user_id": uid, "created_at": now,
        })
        assert generation.get_existing_question_texts(str(document_id), viewer_user_id=uid) == ["Câu đã có"]
        other_user = ObjectId()
        assert generation.get_existing_question_texts(str(document_id), viewer_user_id=other_user) == []
        assert generation.get_existing_question_texts(
            str(document_id), viewer_user_id=other_user, include_shared=True,
        ) == []
        assert generation.get_existing_question_texts(str(document_id), viewer_user_id=None) == []

        # Startup recovery and worker restart only touch unfinished jobs.
        stale_id = retried["job"]["job_id"]
        with postgres_connection() as conn:
            conn.execute("UPDATE generation_jobs SET updated_at=now()-interval '3 hours' "
                         "WHERE id=%s", (stale_id,))
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        # A queued job is never failed by recovery, however old; the worker will pick it up.
        assert job_recovery._recover_generation_jobs(None, cutoff, now, "timeout") == 0
        assert generation.get_generation_job(stale_id)["status"] == "queued"
        restart_id = generation.create_generation_job(request, uid, model_snapshot=GEMINI)
        job_recovery.requeue_orphaned_generation_jobs_on_worker_start()
        assert generation.get_generation_job(restart_id)["status"] == "queued"
        orphan_id = generation.create_generation_job(request, uid, model_snapshot=GEMINI)
        assert generation.claim_generation_job(orphan_id, "dead-worker")
        with postgres_connection() as conn:
            conn.execute("UPDATE generation_jobs SET lease_expires_at=now()-interval '1 minute' "
                         "WHERE id=%s", (orphan_id,))
        assert job_recovery.requeue_orphaned_generation_jobs_on_worker_start()["requeued"] >= 1
        assert generation.get_generation_job(orphan_id)["status"] == "queued"
        assert generation.get_generation_job(done_id)["status"] == "completed"
    finally:
        user_ids = [str(uid), str(admin["_id"])]
        with postgres_connection() as conn:
            conn.execute("DELETE FROM audit_logs WHERE entity_type='generation' "
                         "OR actor_user_id=ANY(%s)", (user_ids,))
            conn.execute("UPDATE questions SET current_version_id=NULL WHERE id=%s",
                         (str(question_id),))
            conn.execute("DELETE FROM question_versions WHERE question_id=%s", (str(question_id),))
            conn.execute("DELETE FROM questions WHERE id=%s", (str(question_id),))
            conn.execute("DELETE FROM generation_runs WHERE requested_by_user_id=ANY(%s)",
                         (user_ids,))
            conn.execute("DELETE FROM generation_jobs WHERE requested_by_user_id=ANY(%s)",
                         (user_ids,))
            conn.execute("DELETE FROM document_subjects WHERE document_id=%s", (str(document_id),))
            conn.execute("DELETE FROM documents WHERE id=%s", (str(document_id),))
            conn.execute("DELETE FROM users WHERE id=ANY(%s)", (user_ids,))
