"""PostgreSQL source of truth for subjects, chapters and learning outcomes."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from bson import ObjectId
from psycopg import errors
from psycopg.types.json import Jsonb

from core.config import settings
from core.postgres import postgres_connection
from core.postgres_audit import write_postgres_audit_event


class SubjectCodeConflict(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _mongo_id(value: str) -> ObjectId | str:
    return ObjectId(value) if ObjectId.is_valid(value) else value


def _subject(row: dict) -> dict:
    # Typed columns and child tables are authoritative. Historical payload only
    # supplies fields that have not been promoted into dedicated columns.
    record = dict(row["payload"] or {})
    record.update({
        "_id": _mongo_id(row["id"]),
        "subject_code": row["subject_code"],
        "subject_name": row["subject_name"],
        "owner_id": _mongo_id(row["owner_id"]) if row["owner_id"] else None,
        "is_active": row["is_active"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "chapters": [],
        "learning_outcomes": [],
    })
    return record


def _child(row: dict, *, chapter: bool) -> dict:
    record = dict(row["payload"] or {})
    record["_id"] = _mongo_id(row["id"])
    record["is_active"] = row["is_active"]
    if chapter:
        record.update(chapter_code=row["chapter_code"], chapter_name=row["chapter_name"],
                      sequence_no=row["sequence_no"])
    else:
        record.update(clo_code=row["clo_code"], description=row["description"],
                      target_weight=float(row["target_weight"]))
    return record


def _audit(conn, action: str, entity_type: str, entity_id: str, actor: Any,
           before: dict, after: dict, *, label: str, subject_id: str | None = None) -> None:
    if before == after:
        return
    metadata = {"label": label, "actor_role": getattr(actor, "role", None)}
    if subject_id:
        metadata["subject_id"] = subject_id
    write_postgres_audit_event(
        conn, action=action, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=getattr(actor, "id", None), actor_role=getattr(actor, "role", None),
        before=before, after=after, metadata=metadata,
    )


def _snapshot(record: dict, fields: tuple[str, ...]) -> dict:
    return {field: _json(record.get(field)) for field in fields}


SUBJECT_FIELDS = ("subject_code", "subject_name", "description", "is_active", "approval_status",
                  "approval_reason")
CHAPTER_FIELDS = ("chapter_code", "chapter_name", "sequence_no", "is_active")
CLO_FIELDS = ("clo_code", "description", "target_weight", "is_active")


class PostgresSubjectRepository:
    def __init__(self) -> None:
        if settings.user_store != "postgres":
            raise RuntimeError("CATALOG_STORE=postgres requires USER_STORE=postgres")

    @staticmethod
    def _load(conn, *, ids: list[str] | None = None, active_only: bool = False) -> list[dict]:
        where = []
        params: list[Any] = []
        if ids is not None:
            if not ids:
                return []
            where.append("id = ANY(%s)")
            params.append(ids)
        if active_only:
            where.append("is_active")
        query = "SELECT * FROM subjects" + (" WHERE " + " AND ".join(where) if where else "")
        query += " ORDER BY subject_code, id"
        rows = conn.execute(query, params).fetchall()
        if not rows:
            return []
        records = {_row["id"]: _subject(_row) for _row in rows}
        subject_ids = list(records)
        for row in conn.execute("""
            SELECT * FROM subject_chapters WHERE subject_id = ANY(%s)
            ORDER BY sequence_no, chapter_code, id
        """, (subject_ids,)):
            records[row["subject_id"]]["chapters"].append(_child(row, chapter=True))
        for row in conn.execute("""
            SELECT * FROM learning_outcomes WHERE subject_id = ANY(%s)
            ORDER BY clo_code, id
        """, (subject_ids,)):
            records[row["subject_id"]]["learning_outcomes"].append(_child(row, chapter=False))
        return list(records.values())

    def list(self, *, ids: list[str] | None = None, active_only: bool = False) -> list[dict]:
        with postgres_connection() as conn:
            return self._load(conn, ids=ids, active_only=active_only)

    def find_by_id(self, subject_id: str | ObjectId, *, active_only: bool = False) -> dict | None:
        records = self.list(ids=[str(subject_id)], active_only=active_only)
        return records[0] if records else None

    def find_by_code(self, subject_code: str) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute("""
                SELECT id FROM subjects WHERE lower(btrim(subject_code))=lower(btrim(%s))
            """, (subject_code,)).fetchone()
            return self._load(conn, ids=[row["id"]])[0] if row else None

    def create(self, record: dict, actor: Any = None) -> dict:
        row_id = str(record["_id"])
        try:
            with postgres_connection() as conn:
                with conn.transaction():
                    conn.execute("""
                        INSERT INTO subjects
                        (id, subject_code, subject_name, owner_id, is_active,
                         payload, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (row_id, record["subject_code"], record["subject_name"],
                          str(record["owner_id"]) if record.get("owner_id") else None,
                          record["is_active"], Jsonb(_json(record)),
                          record["created_at"], record["updated_at"]))
                    _audit(conn, "catalog.subject_create", "subject", row_id, actor,
                           {}, _snapshot(record, SUBJECT_FIELDS), label=record["subject_code"])
                    return self._load(conn, ids=[row_id])[0]
        except errors.UniqueViolation as exc:
            raise SubjectCodeConflict("Mã môn học đã tồn tại") from exc

    def update(self, subject_id: str | ObjectId, fields: dict, actor: Any = None,
               *, deactivate: bool = False, action: str | None = None,
               expected_approval_status: str | None = None) -> dict:
        row_id = str(subject_id)
        try:
            with postgres_connection() as conn:
                with conn.transaction():
                    row = conn.execute("SELECT * FROM subjects WHERE id=%s FOR UPDATE", (row_id,)).fetchone()
                    if not row:
                        raise LookupError("Không tìm thấy học phần")
                    before = _subject(row)
                    current_status = str(before.get("approval_status") or "APPROVED")
                    if expected_approval_status and current_status != expected_approval_status:
                        raise ValueError("Trạng thái duyệt của học phần vừa thay đổi, hãy tải lại")
                    after = {**before, **fields, "updated_at": _now()}
                    conn.execute("""
                        UPDATE subjects SET subject_code=%s, subject_name=%s, is_active=%s,
                            payload=%s, updated_at=%s WHERE id=%s
                    """, (after["subject_code"], after["subject_name"], after["is_active"],
                          Jsonb(_json(after)), after["updated_at"], row_id))
                    _audit(conn, action or ("catalog.subject_deactivate" if deactivate else "catalog.subject_update"),
                           "subject", row_id, actor, _snapshot(before, SUBJECT_FIELDS),
                           _snapshot(after, SUBJECT_FIELDS), label=after["subject_code"])
                    return self._load(conn, ids=[row_id])[0]
        except errors.UniqueViolation as exc:
            raise SubjectCodeConflict("Mã môn học đã tồn tại") from exc

    def save_child(self, subject_id: str | ObjectId, kind: str, child: dict,
                   actor: Any = None, *, create: bool) -> dict:
        if kind not in {"chapter", "clo"}:
            raise ValueError("Loại mục học phần không hợp lệ")
        row_id = str(subject_id)
        child_id = str(child["_id"])
        table = "subject_chapters" if kind == "chapter" else "learning_outcomes"
        fields = CHAPTER_FIELDS if kind == "chapter" else CLO_FIELDS
        code_key = "chapter_code" if kind == "chapter" else "clo_code"
        try:
            with postgres_connection() as conn:
                with conn.transaction():
                    if not conn.execute("SELECT id FROM subjects WHERE id=%s FOR UPDATE", (row_id,)).fetchone():
                        raise LookupError("Không tìm thấy học phần")
                    previous = conn.execute(
                        f"SELECT * FROM {table} WHERE id=%s AND subject_id=%s FOR UPDATE",
                        (child_id, row_id),
                    ).fetchone()
                    if create and previous:
                        raise SubjectCodeConflict("Mã đã tồn tại trong học phần này")
                    if not create and not previous:
                        raise LookupError("Không tìm thấy chương" if kind == "chapter" else "Không tìm thấy CLO")
                    before = _child(previous, chapter=kind == "chapter") if previous else {}
                    child = {**before, **child} if previous else child
                    if kind == "chapter":
                        values = (child["chapter_code"], child["chapter_name"],
                                  child["sequence_no"], child["is_active"], Jsonb(_json(child)))
                        if create:
                            conn.execute("""
                                INSERT INTO subject_chapters
                                (id, subject_id, chapter_code, chapter_name, sequence_no, is_active, payload)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)
                            """, (child_id, row_id, *values))
                        else:
                            conn.execute("""
                                UPDATE subject_chapters SET chapter_code=%s, chapter_name=%s,
                                    sequence_no=%s, is_active=%s, payload=%s WHERE id=%s
                            """, (*values, child_id))
                    else:
                        values = (child["clo_code"], child["description"],
                                  child["target_weight"], child["is_active"], Jsonb(_json(child)))
                        if create:
                            conn.execute("""
                                INSERT INTO learning_outcomes
                                (id, subject_id, clo_code, description, target_weight, is_active, payload)
                                VALUES (%s,%s,%s,%s,%s,%s,%s)
                            """, (child_id, row_id, *values))
                        else:
                            conn.execute("""
                                UPDATE learning_outcomes SET clo_code=%s, description=%s,
                                    target_weight=%s, is_active=%s, payload=%s WHERE id=%s
                            """, (*values, child_id))
                    conn.execute("UPDATE subjects SET updated_at=%s WHERE id=%s", (_now(), row_id))
                    _audit(conn, f"catalog.{kind}_{'create' if create else 'update'}", kind,
                           child_id, actor, _snapshot(before, fields), _snapshot(child, fields),
                           label=child[code_key], subject_id=row_id)
                    return self._load(conn, ids=[row_id])[0]
        except errors.UniqueViolation as exc:
            raise SubjectCodeConflict("Mã đã tồn tại trong học phần này") from exc


def subject_records(database, *, ids: list[ObjectId | str] | None = None,
                    active_only: bool = False) -> list[dict]:
    """Read subjects from the selected store for business modules outside catalog."""
    if settings.catalog_store == "postgres":
        return PostgresSubjectRepository().list(
            ids=[str(value) for value in ids] if ids is not None else None,
            active_only=active_only,
        )
    query: dict = {}
    if ids is not None:
        if not ids:
            return []
        query["_id"] = {"$in": ids}
    if active_only:
        query["is_active"] = True
    return list(database.subjects.find(query))


def subject_record(database, subject_id: ObjectId | str,
                   *, active_only: bool = False) -> dict | None:
    if settings.catalog_store == "postgres":
        return PostgresSubjectRepository().find_by_id(subject_id, active_only=active_only)
    query = {"_id": subject_id}
    if active_only:
        query["is_active"] = True
    return database.subjects.find_one(query)
