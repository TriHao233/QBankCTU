import asyncio
import logging
import signal

from core.bootstrap import bootstrap_database
from core.database import close_database, ping_database
from core.postgres import close_postgres, ping_postgres
from core.job_recovery import (
    recover_stale_jobs,
    requeue_orphaned_generation_jobs_on_worker_start,
)
from core.job_worker import run_job_worker
from core.logging import setup_logging
from core.config import settings
from modules.generation.llm.ollama import close_ollama_client

logger = logging.getLogger(__name__)


async def run() -> None:
    setup_logging()
    await asyncio.to_thread(ping_database)
    await asyncio.to_thread(bootstrap_database)
    if settings.user_store == "postgres" or settings.ai_config_store == "postgres":
        await asyncio.to_thread(ping_postgres)
        from db.migrate import apply_migrations
        from core.postgres import postgres_connection
        with postgres_connection() as connection:
            apply_migrations(connection, check=True)
    await asyncio.to_thread(recover_stale_jobs)
    await asyncio.to_thread(requeue_orphaned_generation_jobs_on_worker_start)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            signal.signal(signum, lambda *_args: loop.call_soon_threadsafe(stop_event.set))

    worker_task = asyncio.create_task(run_job_worker(stop_event))
    stop_task = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {worker_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if worker_task in done:
            await worker_task
            return
        try:
            await asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_grace_seconds)
        except asyncio.TimeoutError:
            logger.warning("Worker exceeded shutdown grace period; cancelling active work")
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)
    finally:
        stop_task.cancel()
        await close_ollama_client()
        close_database()
        close_postgres()
        logger.info("Worker database connection closed")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
