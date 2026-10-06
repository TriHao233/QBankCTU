"""PostgreSQL implementation of the existing user repository contract."""

from __future__ import annotations

from datetime import datetime, timezone

from bson import ObjectId
from psycopg import sql
from psycopg.types.json import Jsonb

from core.database import get_database
from core.config import settings
from core.postgres import postgres_connection
from core.postgres_audit import write_postgres_audit_event
from modules.users.repository import (
    AuditBuilder,
    MongoUserRepository,
    ProfileNotProvisionedError,
    UnverifiedEmailLinkError,
    has_verified_email,
)

JSON_FIELDS = {"permissions", "permission_grants", "permission_revokes",
               "review_subject_ids", "teaching_subject_ids", "profile", "generation_presets",
               "task_calendar"}
UPDATE_FIELDS = JSON_FIELDS | {"email", "display_name", "role", "is_active"}
TASK_DATES = {"created_at", "updated_at", "completed_at", "due_date", "createdAt", "updatedAt"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _id(value: str | ObjectId) -> str:
    try:
        return str(ObjectId(value))
    except Exception as exc:
        raise ValueError("ID người dùng không hợp lệ") from exc


def _json_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _task_dates(value):
    if isinstance(value, list):
        return [_task_dates(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in TASK_DATES and isinstance(item, str):
                try:
                    result[key] = datetime.fromisoformat(item.replace("Z", "+00:00"))
                    continue
                except ValueError:
                    pass
            result[key] = _task_dates(item)
        return result
    return value


def _user(row: dict | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["_id"] = ObjectId(result.pop("id"))
    result["task_calendar"] = _task_dates(result.get("task_calendar") or [])
    result["generation_presets"] = _task_dates(result.get("generation_presets") or [])
    result["review_subject_ids"] = [ObjectId(item) for item in result.get("review_subject_ids") or []]
    result["teaching_subject_ids"] = [ObjectId(item) for item in result.get("teaching_subject_ids") or []]
    return result


def _active_business_repository() -> MongoUserRepository:
    # Documents and questions still write to MongoDB during the staged cutover.
    return MongoUserRepository(get_database())


class PostgresUserRepository:
    def find_by_id(self, user_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            return _user(conn.execute("SELECT * FROM users WHERE id = %s", (_id(user_id),)).fetchone())

    def find_by_firebase_uid(self, firebase_uid: str) -> dict | None:
        with postgres_connection() as conn:
            return _user(conn.execute(
                "SELECT * FROM users WHERE firebase_uid = %s", (firebase_uid,)
            ).fetchone())

    def find_by_email(self, email: str) -> dict | None:
        with postgres_connection() as conn:
            return _user(conn.execute(
                "SELECT * FROM users WHERE lower(email) = lower(%s)", (email,)
            ).fetchone())

    def create(self, data: dict) -> dict:
        now = _utc_now()
        user_id = str(ObjectId())
        with postgres_connection() as conn:
            row = conn.execute(
                """INSERT INTO users (
                    id, firebase_uid, email, display_name, role, permissions,
                    permission_grants, permission_revokes, review_subject_ids, profile,
                    generation_presets, task_calendar, is_active, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                          '[]'::jsonb, '[]'::jsonb, true, %s, %s)
                RETURNING *""",
                (
                    user_id, data["firebase_uid"], data["email"].lower(),
                    data["display_name"], data.get("role", "Teacher"),
                    Jsonb(_json_value(data.get("permissions") or [])),
                    Jsonb(_json_value(data.get("permission_grants") or [])),
                    Jsonb(_json_value(data.get("permission_revokes") or [])),
                    Jsonb(_json_value(data.get("review_subject_ids") or [])),
                    Jsonb(_json_value(data.get("profile") or {})), now, now,
                ),
            ).fetchone()
        return _user(row)

    def sync_identity(self, claims: dict, *, allow_create: bool = True) -> dict:
        firebase_uid = claims["uid"]
        email = (claims.get("email") or f"{firebase_uid}@firebase.local").lower()
        display_name = claims.get("name") or email.split("@", 1)[0]
        avatar = claims.get("picture") or ""
        now = _utc_now()
        with postgres_connection() as conn:
            if has_verified_email(claims):
                # Preserve the old profile/role when Firebase refreshes its claims.
                existing = conn.execute(
                    """SELECT * FROM users
                       WHERE firebase_uid = %s OR lower(email) = lower(%s)
                       ORDER BY (firebase_uid = %s) DESC LIMIT 1 FOR UPDATE""",
                    (firebase_uid, email, firebase_uid),
                ).fetchone()
            else:
                if claims.get("email") and conn.execute(
                    """SELECT 1 FROM users
                       WHERE lower(email) = lower(%s) AND firebase_uid IS DISTINCT FROM %s
                       LIMIT 1""",
                    (email, firebase_uid),
                ).fetchone():
                    raise UnverifiedEmailLinkError()
                existing = conn.execute(
                    "SELECT * FROM users WHERE firebase_uid = %s LIMIT 1 FOR UPDATE",
                    (firebase_uid,),
                ).fetchone()
            if existing:
                profile = {"school": "", "address": "", "avatar": "", **(existing["profile"] or {})}
                if avatar:
                    profile["avatar"] = avatar
                row = conn.execute(
                    """UPDATE users SET firebase_uid = %s, email = %s, profile = %s,
                       updated_at = %s WHERE id = %s RETURNING *""",
                    (firebase_uid, email, Jsonb(profile), now, existing["id"]),
                ).fetchone()
            elif not allow_create:
                raise ProfileNotProvisionedError()
            else:
                row = conn.execute(
                    """INSERT INTO users (
                        id, firebase_uid, email, display_name, role, permissions, profile,
                        generation_presets, task_calendar, is_active, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, 'Teacher', '[]'::jsonb, %s,
                              '[]'::jsonb, '[]'::jsonb, true, %s, %s)
                    RETURNING *""",
                    (
                        str(ObjectId()), firebase_uid, email, display_name,
                        Jsonb({"school": "", "address": "", "avatar": avatar}), now, now,
                    ),
                ).fetchone()
        return _user(row)

    def list(self, page: int, page_size: int, role: str | None, search: str | None):
        conditions = []
        params = []
        if role:
            conditions.append("role = %s")
            params.append(role)
        if search:
            conditions.append("(display_name ILIKE %s OR email ILIKE %s)")
            params.extend([f"%{search}%", f"%{search}%"])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with postgres_connection() as conn:
            total = conn.execute("SELECT count(*) AS n FROM users" + where, params).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM users" + where + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return [_user(row) for row in rows], total

    def update(self, user_id: str | ObjectId, fields: dict, *, audit: AuditBuilder | None = None) -> dict | None:
        unknown = set(fields) - UPDATE_FIELDS
        if unknown:
            raise ValueError(f"Unsupported user update fields: {', '.join(sorted(unknown))}")
        changes = {**fields, "updated_at": _utc_now()}
        assignments = sql.SQL(", ").join(
            sql.SQL("{} = %s").format(sql.Identifier(key)) for key in changes
        )
        values = [Jsonb(_json_value(value)) if key in JSON_FIELDS else value
                  for key, value in changes.items()]
        with postgres_connection() as conn:
            row = conn.execute(
                sql.SQL("UPDATE users SET {} WHERE id = %s RETURNING *").format(assignments),
                (*values, _id(user_id)),
            ).fetchone()
            updated = _user(row)
            event = audit(updated) if updated is not None and audit else None
            if event:
                write_postgres_audit_event(conn, **event)
        return updated

    def role_summary(self) -> dict:
        with postgres_connection() as conn:
            return dict(conn.execute(
                """SELECT count(*) AS total,
                     count(*) FILTER (WHERE is_active) AS active,
                     count(*) FILTER (WHERE is_active AND role='Admin') AS admins,
                     count(*) FILTER (WHERE is_active AND role='Teacher') AS teachers,
                     count(*) FILTER (WHERE is_active AND role='Reviewer') AS reviewers
                   FROM users"""
            ).fetchone())

    def count_active_admins(self) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM users WHERE role = 'Admin' AND is_active"
            ).fetchone()["n"]

    def delete_by_id(self, user_id: str | ObjectId) -> None:
        with postgres_connection() as conn:
            conn.execute("DELETE FROM users WHERE id = %s", (_id(user_id),))

    def get_stats(self, user_id: str | ObjectId) -> dict:
        if settings.document_store == "postgres":
            from modules.documents.postgres_repository import PostgresDocumentRepository
            document_count = PostgresDocumentRepository().count_owned(user_id)
            if settings.question_store == "postgres":
                from modules.questions.postgres_repository import PostgresQuestionRepository
                return {"documents_count": document_count,
                        **PostgresQuestionRepository.owner_counts(user_id)}
            db = get_database()
            oid = ObjectId(user_id)
            return {
                "documents_count": document_count,
                "questions_count": db.questions.count_documents({
                    "created_by_user_id": oid,
                    "lifecycle_status": {"$ne": "ARCHIVED"},
                }),
                "pending_questions_count": db.questions.count_documents({
                    "created_by_user_id": oid, "review_status": "PENDING",
                }),
            }
        return _active_business_repository().get_stats(user_id)

    def get_calendar_documents(self, user_id: str | ObjectId) -> list[dict]:
        if settings.document_store == "postgres":
            from modules.documents.postgres_repository import PostgresDocumentRepository
            return PostgresDocumentRepository().list_owned(user_id)
        return _active_business_repository().get_calendar_documents(user_id)

    def get_calendar_questions(self, user_id: str | ObjectId) -> list[dict]:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            return PostgresQuestionRepository.owned_questions(user_id)
        return _active_business_repository().get_calendar_questions(user_id)

    def get_review_assignment_questions(self, user_id: str | ObjectId) -> list[dict]:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            return [question for question in PostgresQuestionRepository().held_reviews()
                    if str((question.get("review_assignment") or {}).get("reviewer_user_id")) == str(user_id)]
        return _active_business_repository().get_review_assignment_questions(user_id)

    def count_unassigned_pending(self) -> int:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            return PostgresQuestionRepository().count_unassigned_pending()
        return _active_business_repository().count_unassigned_pending()

    def get_document_ids_with_questions(self, document_ids: list[ObjectId]) -> set[str]:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            return PostgresQuestionRepository.document_ids_with_questions(document_ids)
        return _active_business_repository().get_document_ids_with_questions(document_ids)
