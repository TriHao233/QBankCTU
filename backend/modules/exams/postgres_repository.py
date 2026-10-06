"""PostgreSQL exams and exam variants (EXAM_STORE=postgres)."""

from __future__ import annotations

from bson import ObjectId
from psycopg import errors

from core.config import settings
from core.postgres import postgres_connection
from core.postgres_audit import write_postgres_audit_event
from db.bson_json import restore
from db.copy_business_data import projected_rows, upsert
from modules.exams.repository import object_id, utc_now
from modules.exams.schemas import MAX_VARIANTS_PER_EXAM

# Frozen question snapshots and print headers stay exactly as they were saved.
FROZEN_KEYS = {"content_snapshot", "header", "scoring_config"}
NOT_DELETED = "payload->>'deleted_at' IS NULL"


def _restore(value, key: str = ""):
    if key in FROZEN_KEYS:
        return value
    if isinstance(value, dict):
        return {name: _restore(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_restore(item, key[:-1] if key.endswith("s") else key) for item in value]
    return restore(value, key)


def _exam(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = _restore(row["payload"] or {})
    record.update(
        _id=ObjectId(row["id"]), status=row["status"],
        subject_id=ObjectId(row["subject_id"]) if row["subject_id"] else None,
        created_by_user_id=(ObjectId(row["created_by_user_id"])
                            if row["created_by_user_id"] else None),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )
    return record


def _variant(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = _restore(row["payload"] or {})
    record.update(_id=ObjectId(row["id"]), exam_id=ObjectId(row["exam_id"]),
                  created_at=row["created_at"])
    return record


def _save_exam(conn, exam: dict) -> None:
    # exam_questions mirrors the current question list; rebuild it on every save.
    conn.execute("DELETE FROM exam_questions WHERE exam_id=%s", (str(exam["_id"]),))
    for table, row in projected_rows("exams", exam):
        upsert(conn, table, row)


class PostgresExamRepository:
    def __init__(self):
        if settings.question_store != "postgres" or settings.catalog_store != "postgres":
            raise RuntimeError("EXAM_STORE=postgres requires QUESTION_STORE and CATALOG_STORE=postgres")

    def create(self, exam: dict) -> dict:
        with postgres_connection() as conn:
            _save_exam(conn, exam)
        return exam

    def find(self, exam_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            return _exam(conn.execute("SELECT * FROM exams WHERE id=%s AND " + NOT_DELETED,
                                      (str(object_id(exam_id, "exam_id")),)).fetchone())

    def list(self, page: int, page_size: int, created_by_user_id: ObjectId | None,
             *, visible_to_user_id: ObjectId | None = None) -> tuple[list[dict], int]:
        where, params = " WHERE " + NOT_DELETED, []
        if created_by_user_id is not None:
            where, params = where + " AND created_by_user_id=%s", [str(created_by_user_id)]
        if visible_to_user_id is not None:
            where += " AND (created_by_user_id=%s OR payload->'shared_with_user_ids' ? %s)"
            params += [str(visible_to_user_id), str(visible_to_user_id)]
        with postgres_connection() as conn:
            total = conn.execute("SELECT count(*) AS n FROM exams" + where, params).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM exams" + where
                + " ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [_exam(row) for row in rows], total

    def update(self, exam_id: str | ObjectId, updates: dict, *, audit: dict | None = None,
               expected_status: str | None = None, require_no_variants: bool = False) -> dict | None:
        key = str(object_id(exam_id, "exam_id"))
        with postgres_connection() as conn:
            exam = _exam(conn.execute("SELECT * FROM exams WHERE id=%s AND " + NOT_DELETED + " FOR UPDATE",
                                      (key,)).fetchone())
            if not exam:
                return None
            if expected_status and str(exam.get("status") or "").upper() != expected_status:
                return None
            if require_no_variants and conn.execute(
                "SELECT 1 FROM exam_variants WHERE exam_id=%s LIMIT 1", (key,),
            ).fetchone():
                raise ValueError("Đề thi đã có mã đề")
            exam.update(updates)
            exam["updated_at"] = utc_now()
            _save_exam(conn, exam)
            if audit:
                write_postgres_audit_event(conn, **audit)
            return exam

    def delete(self, exam_id: str | ObjectId, deleted_by_user_id: ObjectId | None = None,
               *, audit: dict | None = None) -> bool:
        key = str(object_id(exam_id, "exam_id"))
        with postgres_connection() as conn:
            exam = _exam(conn.execute("SELECT * FROM exams WHERE id=%s AND " + NOT_DELETED + " FOR UPDATE",
                                      (key,)).fetchone())
            if not exam:
                return False
            now = utc_now()
            exam.update(deleted_at=now, deleted_by_user_id=deleted_by_user_id, updated_at=now)
            _save_exam(conn, exam)
            if audit:
                write_postgres_audit_event(conn, **audit)
            return True

    def count_variants(self, exam_id: str | ObjectId) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM exam_variants WHERE exam_id=%s",
                (str(object_id(exam_id, "exam_id")),),
            ).fetchone()["n"]

    @staticmethod
    def open_exams_using_question(question_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT e.* FROM exams e
                   JOIN exam_questions eq ON eq.exam_id=e.id
                   WHERE eq.question_id=%s
                     AND upper(e.status) NOT IN ('FINALIZED','PUBLISHED','ARCHIVED')
                     AND e.payload->>'deleted_at' IS NULL""",
                (str(object_id(question_id, "question_id")),),
            ).fetchall()
        return [_exam(row) for row in rows]

    @staticmethod
    def count(*, subject_id=None, chapter_id=None) -> int:
        clauses, params = [NOT_DELETED], []
        if subject_id is not None:
            clauses.append("subject_id=%s")
            params.append(str(subject_id))
        if chapter_id is not None:
            clauses.append("payload->'matrix' @> %s::jsonb")
            params.append('[{"chapter_id":"' + str(chapter_id) + '"}]')
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with postgres_connection() as conn:
            return conn.execute("SELECT count(*) AS n FROM exams" + where,
                                params).fetchone()["n"]


class PostgresExamVariantRepository:
    def create(self, variant: dict) -> dict:
        exam_id = str(variant["exam_id"])
        try:
            with postgres_connection() as conn:
                # Khóa đề thi để giới hạn số mã đề không bị vượt khi tạo song song.
                exam_row = conn.execute("SELECT status FROM exams WHERE id=%s AND " + NOT_DELETED + " FOR UPDATE",
                                        (exam_id,)).fetchone()
                if not exam_row:
                    raise LookupError("Không tìm thấy đề thi")
                if str(exam_row["status"] or "").upper() != "FINALIZED":
                    raise ValueError("Chỉ có thể tạo mã đề khi đề thi đang ở trạng thái đã chốt")
                existing = conn.execute(
                    "SELECT count(*) AS n FROM exam_variants WHERE exam_id=%s", (exam_id,),
                ).fetchone()["n"]
                if existing >= MAX_VARIANTS_PER_EXAM:
                    raise ValueError(f"Đã đạt tối đa {MAX_VARIANTS_PER_EXAM} mã đề cho kỳ thi này")
                for table, row in projected_rows("exam_variants", variant):
                    upsert(conn, table, row)
        except errors.UniqueViolation as exc:
            raise ValueError("Mã đề này đã tồn tại trong kỳ thi") from exc
        return variant

    def find(self, variant_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            return _variant(conn.execute(
                "SELECT * FROM exam_variants WHERE id=%s",
                (str(object_id(variant_id, "variant_id")),),
            ).fetchone())

    def list_by_exam(self, exam_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM exam_variants WHERE exam_id=%s ORDER BY created_at, id",
                (str(object_id(exam_id, "exam_id")),),
            ).fetchall()
        return [_variant(row) for row in rows]

    def delete(self, variant_id: str | ObjectId, *, audit: dict | None = None) -> bool:
        """Refuses (returns False) once the exam has an official export."""
        key = str(object_id(variant_id, "variant_id"))
        with postgres_connection() as conn:
            row = conn.execute("SELECT exam_id FROM exam_variants WHERE id=%s", (key,)).fetchone()
            if not row:
                return False
            exam = conn.execute(
                "SELECT payload->>'official_exported_at' AS exported FROM exams WHERE id=%s AND "
                + NOT_DELETED + " FOR UPDATE",
                (row["exam_id"],),
            ).fetchone()
            if not exam or exam["exported"]:
                return False
            deleted = conn.execute("DELETE FROM exam_variants WHERE id=%s", (key,)).rowcount == 1
            if deleted and audit:
                write_postgres_audit_event(conn, **audit)
            return deleted
