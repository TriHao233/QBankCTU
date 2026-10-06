import asyncio
import logging
import os
import socket
from datetime import datetime, timezone

from core.config import settings
from core.database import get_database

logger = logging.getLogger(__name__)
SLA_FIRST_CHECK_DELAY_SECONDS = 60.0


def get_worker_id() -> str:
    return os.getenv("WORKER_ID", f"{socket.gethostname()}-{os.getpid()}")


async def maintain_lease(heartbeat, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.job_heartbeat_seconds)
        except asyncio.TimeoutError:
            if not await asyncio.to_thread(heartbeat):
                logger.warning("Worker lost its job lease")
                return


def get_next_queued_evaluation_job_id() -> str | None:
    if settings.question_store == "postgres":
        from modules.questions.postgres_evaluation_jobs import PostgresEvaluationJobs
        return PostgresEvaluationJobs().next_queued_id()
    now = datetime.now(timezone.utc)
    doc = get_database().evaluation_jobs.find_one(
        {
            "$or": [
                {
                    "status": "QUEUED",
                    "$or": [
                        {"next_attempt_at": {"$exists": False}},
                        {"next_attempt_at": {"$lte": now}},
                    ],
                },
                {"status": "PROCESSING", "lease_expires_at": {"$lte": now}},
            ]
        },
        sort=[("queued_at", 1)],
        projection={"_id": 1},
    )
    return str(doc["_id"]) if doc else None


async def process_available_jobs_once(worker_id: str | None = None) -> bool:
    """Process at most one job per queue and report whether work was found."""
    from modules.generation.generate import process_generate_background
    from modules.generation.mongodb import get_next_queued_generation_job_id
    from modules.questions.workflow_service import process_evaluation_job_background

    worker_id = worker_id or get_worker_id()
    found_work = False
    if settings.user_store == "postgres":
        from core.outbox import process_available_outbox_once
        found_work = await asyncio.to_thread(process_available_outbox_once, worker_id)
    generation_job_id = await asyncio.to_thread(get_next_queued_generation_job_id)
    evaluation_job_id = await asyncio.to_thread(get_next_queued_evaluation_job_id)
    tasks = []
    if generation_job_id:
        tasks.append(process_generate_background(generation_job_id, worker_id))
    if evaluation_job_id:
        tasks.append(process_evaluation_job_background(evaluation_job_id, worker_id))
    if tasks:
        found_work = True
        await asyncio.gather(*tasks)
    return found_work


async def _requeue_interrupted_generation_jobs(job_ids: list[str], worker_id: str) -> None:
    from modules.generation.mongodb import requeue_generation_job_for_shutdown
    for job_id in job_ids:
        try:
            await asyncio.to_thread(requeue_generation_job_for_shutdown, job_id, worker_id)
        except Exception:
            logger.exception("Cannot requeue generation job [%s] on shutdown", job_id)


async def run_job_worker(stop_event: asyncio.Event) -> None:
    """Run independent generation lanes for Ollama and Gemini.

    Each lane processes one job at a time, but neither lane waits for the other.
    This keeps provider-specific limits intact while removing cross-provider
    head-of-line blocking.
    """
    from modules.generation.generate import process_generate_background
    from modules.generation.mongodb import get_next_queued_generation_job_id
    from modules.questions.workflow_service import (
        get_workflow_service,
        process_evaluation_job_background,
    )

    def send_review_sla_reminders() -> int:
        return get_workflow_service().send_review_sla_reminders()

    def escalate_overdue_reviews() -> dict:
        return get_workflow_service().escalate_overdue_reviews()

    worker_id = get_worker_id()
    logger.info("Mongo job worker started: %s", worker_id)
    generation_tasks: dict[str, tuple[str, asyncio.Task]] = {}
    evaluation_task: asyncio.Task | None = None
    # First SLA pass runs shortly after start-up, not before the first job poll.
    next_sla_check = asyncio.get_running_loop().time() + SLA_FIRST_CHECK_DELAY_SECONDS

    async def finish_task(task: asyncio.Task, label: str) -> None:
        try:
            await task
        except asyncio.CancelledError:
            logger.info("Background %s task cancelled", label)
        except Exception:
            logger.exception("Background %s task failed", label)

    try:
        while not stop_event.is_set():
            if settings.user_store == "postgres":
                from core.outbox import process_available_outbox_once
                try:
                    await asyncio.to_thread(process_available_outbox_once, worker_id)
                except Exception:
                    logger.exception("PostgreSQL outbox worker iteration failed")
            for provider_group, (job_id, task) in list(generation_tasks.items()):
                if not task.done():
                    from modules.generation.mongodb import get_generation_job
                    try:
                        job = await asyncio.to_thread(get_generation_job, job_id)
                        if job and job.get("progress", {}).get("stage") == "cancelled":
                            task.cancel()
                    except Exception:
                        logger.exception("Cannot check cancellation for generation job [%s]", job_id)
                if task.done():
                    await finish_task(task, f"generation/{provider_group}")
                    generation_tasks.pop(provider_group, None)

            if evaluation_task is not None and evaluation_task.done():
                await finish_task(evaluation_task, "evaluation")
                evaluation_task = None

            try:
                for provider_group in ("ollama", "gemini"):
                    if provider_group in generation_tasks:
                        continue
                    job_id = await asyncio.to_thread(
                        get_next_queued_generation_job_id,
                        provider_group,
                    )
                    if job_id:
                        logger.info(
                            "Dispatching generation job [%s] on independent %s lane",
                            job_id,
                            provider_group,
                        )
                        generation_tasks[provider_group] = (
                            job_id,
                            asyncio.create_task(process_generate_background(job_id, worker_id)),
                        )

                if evaluation_task is None:
                    evaluation_job_id = await asyncio.to_thread(get_next_queued_evaluation_job_id)
                    if evaluation_job_id:
                        evaluation_task = asyncio.create_task(
                            process_evaluation_job_background(evaluation_job_id, worker_id)
                        )
            except Exception:
                logger.exception("Mongo job worker iteration failed")

            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_sla_check:
                next_sla_check = loop_time + max(60.0, settings.review_sla_reminder_interval_seconds)
                try:
                    reminded = await asyncio.to_thread(send_review_sla_reminders)
                    if reminded:
                        logger.info("Sent review SLA reminders for %s question(s)", reminded)
                except Exception:
                    logger.exception("Review SLA reminder pass failed")
                try:
                    escalated = await asyncio.to_thread(escalate_overdue_reviews)
                    if escalated.get("notified") or escalated.get("requeued"):
                        logger.info("Review escalation: %s", escalated)
                except Exception:
                    logger.exception("Review escalation pass failed")

            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=max(0.05, settings.job_worker_poll_seconds),
                )
            except asyncio.TimeoutError:
                pass

        interrupted = [job_id for job_id, task in generation_tasks.values() if not task.done()]
        for _, task in generation_tasks.values():
            if not task.done():
                task.cancel()
        pending = [task for _, task in generation_tasks.values()]
        if evaluation_task is not None:
            pending.append(evaluation_task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _requeue_interrupted_generation_jobs(interrupted, worker_id)
    except asyncio.CancelledError:
        interrupted = [job_id for job_id, task in generation_tasks.values() if not task.done()]
        pending = [task for _, task in generation_tasks.values()]
        if evaluation_task is not None:
            pending.append(evaluation_task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _requeue_interrupted_generation_jobs(interrupted, worker_id)
        raise
    finally:
        logger.info("Mongo job worker stopped")
