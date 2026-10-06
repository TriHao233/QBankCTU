"""PostgreSQL generation queue and run history (GENERATION_STORE=postgres).

Mirrors the Mongo helpers in ``modules.generation.mongodb`` so the API, worker
and Admin views keep the same job shape (``job_id``, lowercase status, ...).
"""

from __future__ import annotations

from datetime import timedelta

from bson import ObjectId
from bson.errors import InvalidId
from psycopg import errors

from core.config import settings
from core.postgres import postgres_connection
from db.bson_json import restore
from db.copy_business_data import projected_rows, upsert
from modules.documents.repository import object_id
from modules.generation.job_outcome import CANCEL_REASON_USER

ACTIVE = ("queued", "processing")
TERMINAL = {"completed", "failed", "cancelled"}
LEASE_FIELDS = ("locked_by", "lease_expires_at", "heartbeat_at", "next_attempt_at")


def utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _shallow(payload: dict | None) -> dict:
    # Nested request/result/metrics stay plain JSON exactly as the API wrote them.
    return {key: value if isinstance(value, (dict, list)) else restore(value, key)
            for key, value in (payload or {}).items()}


def _record(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = _shallow(row["payload"])
    record.update(
        _id=ObjectId(row["id"]),
        requested_by_user_id=(ObjectId(row["requested_by_user_id"])
                              if row["requested_by_user_id"] else None),
        idempotency_key=row["idempotency_key"], status=row["status"],
        request=row["request"], result=row["result"], metrics=row["metrics"],
        attempt_count=row["attempt_no"], locked_by=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"], next_attempt_at=row["next_attempt_at"],
        error_message=row["error_message"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
    record["model_snapshot"] = record.get("model_snapshot") or None
    return record


def _public(record: dict | None) -> dict | None:
    if record is None:
        return None
    result = dict(record)
    result["job_id"] = str(result.pop("_id"))
    return result


def _key(job_id) -> str | None:
    try:
        return str(ObjectId(job_id))
    except (InvalidId, TypeError):
        return None


def _save(conn, record: dict) -> None:
    for table, row in projected_rows("generation_jobs", record):
        upsert(conn, table, row)


def _release(record: dict) -> None:
    for field in LEASE_FIELDS:
        record.pop(field, None)


class PostgresGenerationStore:
    def __init__(self):
        if settings.user_store != "postgres" or settings.document_store != "postgres":
            raise RuntimeError("GENERATION_STORE=postgres requires USER_STORE and DOCUMENT_STORE=postgres")

    # ---- Jobs ------------------------------------------------------------

    def create_job(self, request: dict, requested_by_user_id=None,
                   idempotency_key: str | None = None, *, model_snapshot=None,
                   code_model_snapshot=None, fallback_model_snapshot=None,
                   index_snapshot=None, request_hash=None, max_active=None) -> str:
        from modules.generation.mongodb import ActiveJobLimitError

        now = utc_now()
        record = {
            "_id": ObjectId(), "request": request, "model_snapshot": model_snapshot,
            "code_model_snapshot": code_model_snapshot,
            "fallback_model_snapshot": fallback_model_snapshot,
            "index_snapshot": index_snapshot, "request_hash": request_hash,
            "requested_by_user_id": requested_by_user_id,
            "idempotency_key": idempotency_key, "status": "queued",
            "attempt_count": 0, "max_attempts": settings.job_max_attempts,
            "result": None, "metrics": None,
            "progress": {"stage": "queued", "completed": 0, "total": 0},
            "error_message": None, "created_at": now, "updated_at": now,
        }
        try:
            with postgres_connection() as conn:
                if max_active is not None and requested_by_user_id is not None:
                    owner = str(requested_by_user_id)
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"generation:{owner}",))
                    active = conn.execute(
                        "SELECT count(*) AS n FROM generation_jobs WHERE requested_by_user_id=%s "
                        "AND status=ANY(%s)", (owner, list(ACTIVE)),
                    ).fetchone()["n"]
                    if active >= max_active:
                        raise ActiveJobLimitError()
                _save(conn, record)
        except errors.UniqueViolation:
            existing = self.get_by_idempotency(requested_by_user_id, idempotency_key)
            if not existing:
                raise
            return existing["job_id"]
        return str(record["_id"])

    def get(self, job_id, *, requested_by_user_id=None) -> dict | None:
        key = _key(job_id)
        if key is None:
            return None
        query, params = "SELECT * FROM generation_jobs WHERE id=%s", [key]
        if requested_by_user_id is not None:
            query += " AND requested_by_user_id=%s"
            params.append(str(requested_by_user_id))
        with postgres_connection() as conn:
            return _public(_record(conn.execute(query, params).fetchone()))

    def get_by_idempotency(self, requested_by_user_id, idempotency_key: str | None) -> dict | None:
        if not idempotency_key:
            return None
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM generation_jobs WHERE requested_by_user_id=%s "
                "AND idempotency_key=%s",
                (str(requested_by_user_id) if requested_by_user_id else None, idempotency_key),
            ).fetchone()
        return _public(_record(row))

    def count_active(self, requested_by_user_id) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM generation_jobs WHERE requested_by_user_id=%s "
                "AND status=ANY(%s)", (str(requested_by_user_id), list(ACTIVE)),
            ).fetchone()["n"]

    def active_job_ids(self, requested_by_user_id) -> list[str]:
        with postgres_connection() as conn:
            return [row["id"] for row in conn.execute(
                "SELECT id FROM generation_jobs WHERE requested_by_user_id=%s AND status=ANY(%s) ORDER BY id",
                (str(requested_by_user_id), list(ACTIVE)),
            ).fetchall()]

    def _locked(self, conn, key: str) -> dict | None:
        return _record(conn.execute(
            "SELECT * FROM generation_jobs WHERE id=%s FOR UPDATE", (key,),
        ).fetchone())

    def update(self, job_id, status: str, result=None, metrics=None,
               error_message: str | None = None, worker_id: str | None = None) -> bool:
        key = _key(job_id)
        if key is None:
            return False
        with postgres_connection() as conn:
            record = self._locked(conn, key)
            if not record or (worker_id and record.get("locked_by") != worker_id):
                return False
            now = utc_now()
            record.update(status=status, updated_at=now)
            if status == "completed":
                record["progress"] = {"stage": "completed", "completed": 1, "total": 1}
            elif status == "failed":
                record["progress"] = {"stage": "failed", "completed": 0, "total": 1}
            if result is not None:
                record["result"] = result
            if metrics is not None:
                record["metrics"] = metrics
            if error_message is not None:
                record["error_message"] = error_message
            if status in TERMINAL:
                record["expires_at"] = now + timedelta(days=settings.job_retention_days)
                _release(record)
            _save(conn, record)
            return True

    def update_progress(self, job_id, worker_id: str, progress: dict) -> bool:
        key = _key(job_id)
        with postgres_connection() as conn:
            record = self._locked(conn, key) if key else None
            if (not record or record["status"] != "processing"
                    or record.get("locked_by") != worker_id):
                return False
            record.update(progress=progress, updated_at=utc_now())
            _save(conn, record)
            return True

    def cancel(self, job_id, *, requested_by_user_id=None,
               message: str = "Đã dừng theo yêu cầu của người dùng",
               stage: str = "cancelled", reason: str | None = CANCEL_REASON_USER) -> dict | None:
        """Stop an active job; ``reason=None`` records a plain failure instead of CANCELLED."""
        key = _key(job_id)
        if key is None:
            return None
        with postgres_connection() as conn:
            record = self._locked(conn, key)
            if (not record or record["status"] not in ACTIVE
                    or (requested_by_user_id is not None
                        and record.get("requested_by_user_id") != requested_by_user_id)):
                return None
            now = utc_now()
            record.update(status="failed", error_message=message, updated_at=now,
                          expires_at=now + timedelta(days=settings.job_retention_days),
                          progress={"stage": stage, "completed": 0, "total": 1})
            if reason:
                record.update(cancelled_at=now, cancel_reason=reason)
            _release(record)
            _save(conn, record)
            return _public(record)

    def claim(self, job_id, worker_id: str) -> dict | None:
        key = _key(job_id)
        if key is None:
            return None
        with postgres_connection() as conn:
            record = self._locked(conn, key)
            if not record:
                return None
            now = utc_now()
            runnable = (
                (record["status"] == "queued"
                 and (record.get("next_attempt_at") is None or record["next_attempt_at"] <= now))
                or (record["status"] == "processing"
                    and record.get("lease_expires_at") is not None
                    and record["lease_expires_at"] <= now)
            )
            if not runnable:
                return None
            record.pop("next_attempt_at", None)
            record.update(status="processing", locked_by=worker_id, started_at=now,
                          heartbeat_at=now, updated_at=now,
                          lease_expires_at=now + timedelta(seconds=settings.job_lease_seconds),
                          progress={"stage": "starting", "completed": 0, "total": 0},
                          attempt_count=int(record.get("attempt_count") or 0) + 1)
            _save(conn, record)
            return _public(record)

    def next_queued_id(self, provider_group: str | None = None) -> str | None:
        clauses = ["""((status='queued' AND (next_attempt_at IS NULL OR next_attempt_at <= now()))
                       OR (status='processing' AND lease_expires_at <= now()))"""]
        group = (provider_group or "").strip().lower()
        runtime = "model_snapshot->>'runtime'"
        if group == "gemini":
            clauses.append(f"({runtime}='GEMINI' OR ({runtime} IS NULL "
                           "AND request->>'model_provider'='gemini'))")
        elif group == "ollama":
            clauses.append(f"({runtime}='OLLAMA' OR ({runtime} IS NULL "
                           "AND request->>'model_provider' IS DISTINCT FROM 'gemini'))")
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT id FROM generation_jobs WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def heartbeat(self, job_id, worker_id: str) -> bool:
        key = _key(job_id)
        with postgres_connection() as conn:
            record = self._locked(conn, key) if key else None
            if (not record or record["status"] != "processing"
                    or record.get("locked_by") != worker_id):
                return False
            now = utc_now()
            record.update(heartbeat_at=now, updated_at=now,
                          lease_expires_at=now + timedelta(seconds=settings.job_lease_seconds))
            _save(conn, record)
            return True

    def retry_or_dead_letter(self, job: dict, worker_id: str, *, error_message: str,
                             metrics: dict | None = None) -> str:
        attempts = int(job.get("attempt_count") or 1)
        max_attempts = int(job.get("max_attempts") or settings.job_max_attempts)
        status = "queued" if attempts < max_attempts else "failed"
        with postgres_connection() as conn:
            record = self._locked(conn, str(job["job_id"]))
            if (not record or record["status"] != "processing"
                    or record.get("locked_by") != worker_id):
                return status
            now = utc_now()
            for field in ("locked_by", "lease_expires_at", "heartbeat_at"):
                record.pop(field, None)
            record.update(status=status, error_message=error_message, updated_at=now)
            if status == "queued":
                delay = min(settings.job_retry_base_seconds * (2 ** max(0, attempts - 1)),
                            settings.job_retry_max_seconds)
                record.update(last_failed_at=now,
                              next_attempt_at=now + timedelta(seconds=delay),
                              progress={"stage": "retry_wait", "completed": 0, "total": 1})
            else:
                record.update(dead_lettered_at=now,
                              expires_at=now + timedelta(days=settings.job_retention_days),
                              progress={"stage": "failed", "completed": 0, "total": 1})
            if metrics is not None:
                record["metrics"] = metrics
            _save(conn, record)
        return status

    def requeue_orphaned(self, message: str) -> dict:
        """Requeue processing jobs whose lease expired; dead-letter those out of attempts."""
        with postgres_connection() as conn:
            ids = [row["id"] for row in conn.execute(
                """SELECT id FROM generation_jobs WHERE status='processing'
                   AND (lease_expires_at IS NULL OR lease_expires_at <= now()) ORDER BY id"""
            ).fetchall()]
        result = {"requeued": 0, "dead_lettered": 0}
        for key in ids:
            with postgres_connection() as conn:
                record = self._locked(conn, key)
                now = utc_now()
                if (not record or record["status"] != "processing"
                        or (record.get("lease_expires_at") is not None and record["lease_expires_at"] > now)):
                    continue
                _release(record)
                attempts = int(record.get("attempt_count") or 0)
                max_attempts = int(record.get("max_attempts") or settings.job_max_attempts)
                if attempts >= max_attempts:
                    record.update(status="failed", error_message=message, updated_at=now,
                                  dead_lettered_at=now,
                                  expires_at=now + timedelta(days=settings.job_retention_days),
                                  progress={"stage": "failed", "completed": 0, "total": 1})
                    result["dead_lettered"] += 1
                else:
                    record.update(status="queued", error_message=message, updated_at=now,
                                  last_failed_at=now, next_attempt_at=now,
                                  progress={"stage": "retry_wait", "completed": 0, "total": 1})
                    result["requeued"] += 1
                _save(conn, record)
        return result

    def requeue_for_shutdown(self, job_id, worker_id: str, message: str) -> bool:
        """Return a job interrupted by worker shutdown to the queue without spending an attempt."""
        key = _key(job_id)
        if key is None:
            return False
        with postgres_connection() as conn:
            record = self._locked(conn, key)
            if not record or record["status"] != "processing" or record.get("locked_by") != worker_id:
                return False
            now = utc_now()
            _release(record)
            record.update(status="queued", error_message=message, updated_at=now, next_attempt_at=now,
                          attempt_count=max(0, int(record.get("attempt_count") or 0) - 1),
                          progress={"stage": "retry_wait", "completed": 0, "total": 1})
            _save(conn, record)
            return True

    def fail_active(self, message: str, *, updated_before=None) -> int:
        """Đánh failed các job còn queued/processing (khởi động worker hoặc recovery)."""
        query = "SELECT id FROM generation_jobs WHERE status=ANY(%s)"
        params: list = [list(ACTIVE)]
        if updated_before is not None:
            query += " AND updated_at < %s"
            params.append(updated_before)
        with postgres_connection() as conn:
            ids = [row["id"] for row in conn.execute(query + " ORDER BY id", params).fetchall()]
        changed = 0
        for key in ids:
            with postgres_connection() as conn:
                record = self._locked(conn, key)
                if (not record or record["status"] not in ACTIVE
                        or (updated_before is not None and record["updated_at"] >= updated_before)):
                    continue
                now = utc_now()
                record.update(status="failed", error_message=message, updated_at=now)
                if updated_before is None:
                    record.update(
                        expires_at=now + timedelta(days=settings.job_retention_days),
                        progress={"stage": "cancelled", "completed": 0, "total": 1},
                    )
                    _release(record)
                _save(conn, record)
                changed += 1
        return changed

    # ---- Admin ------------------------------------------------------------

    @staticmethod
    def _admin_where(statuses, requested_by_user_id, date_from, date_to) -> tuple[str, list]:
        clauses, params = [], []
        if statuses:
            clauses.append("status=ANY(%s)")
            params.append(list(statuses))
        if requested_by_user_id is not None:
            clauses.append("requested_by_user_id=%s")
            params.append(str(requested_by_user_id))
        if date_from is not None:
            clauses.append("updated_at >= %s")
            params.append(date_from)
        if date_to is not None:
            clauses.append("updated_at <= %s")
            params.append(date_to)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def admin_jobs(self, statuses, requested_by_user_id, date_from, date_to,
                   limit: int) -> list[dict]:
        where, params = self._admin_where(statuses, requested_by_user_id, date_from, date_to)
        with postgres_connection() as conn:
            rows = conn.execute("SELECT * FROM generation_jobs" + where
                                + " ORDER BY updated_at DESC, id DESC LIMIT %s",
                                [*params, limit]).fetchall()
        return [_record(row) for row in rows]

    def count_admin_jobs(self, statuses, requested_by_user_id, date_from, date_to) -> int:
        where, params = self._admin_where(statuses, requested_by_user_id, date_from, date_to)
        with postgres_connection() as conn:
            return conn.execute("SELECT count(*) AS n FROM generation_jobs" + where,
                                params).fetchone()["n"]

    def queue_metrics(self) -> dict:
        with postgres_connection() as conn:
            return dict(conn.execute(
                """SELECT
                     count(*) FILTER (WHERE status='queued') AS queued,
                     count(*) FILTER (WHERE status='processing') AS processing,
                     count(*) FILTER (WHERE status='queued' AND next_attempt_at > now())
                       AS retry_wait,
                     count(*) FILTER (WHERE payload->>'dead_lettered_at' IS NOT NULL)
                       AS dead_lettered,
                     count(*) FILTER (WHERE status='processing' AND lease_expires_at <= now())
                       AS expired_leases,
                     min(created_at) FILTER (WHERE status='queued') AS oldest_queued
                   FROM generation_jobs"""
            ).fetchone())

    # ---- Runs --------------------------------------------------------------

    def create_run(self, record: dict) -> str:
        with postgres_connection() as conn:
            for table, row in projected_rows("generation_runs", record):
                upsert(conn, table, row)
        return str(record["_id"])

    def finish_run(self, run_id, fields: dict, execution: dict) -> None:
        key = str(object_id(run_id, "generation_run_id"))
        with postgres_connection() as conn:
            row = conn.execute("SELECT * FROM generation_runs WHERE id=%s FOR UPDATE",
                               (key,)).fetchone()
            if not row:
                return
            record = _shallow(row["result"])
            record.update(fields)
            record["execution"] = {**(record.get("execution") or {}), **execution}
            record["_id"] = ObjectId(row["id"])
            record["updated_at"] = fields.get("finished_at") or utc_now()
            for table, projected in projected_rows("generation_runs", record):
                upsert(conn, table, projected)

    def completed_runs(self, generation_job_id) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT result FROM generation_runs
                   WHERE generation_job_id=%s AND status='COMPLETED'
                   ORDER BY created_at, id""",
                (str(object_id(generation_job_id, "generation_job_id")),),
            ).fetchall()
        return [_shallow(row["result"]) for row in rows]

    def recent_runs(self, since, limit: int = 1000) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT result FROM generation_runs
                   WHERE created_at >= %s OR updated_at >= %s
                   ORDER BY updated_at DESC, id DESC LIMIT %s""",
                (since, since, limit),
            ).fetchall()
        return [_shallow(row["result"]) for row in rows]
