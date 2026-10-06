"""Read reviewer and admin identities from the selected user store."""

from bson import ObjectId

from core.config import settings
from core.postgres import postgres_connection
from modules.users.postgres_repository import PostgresUserRepository, _user


REVIEWER_CANDIDATE_FILTER = [
    {"role": {"$in": ["Reviewer", "Admin"]}},
    {"permission_grants": "reviews.manage"},
    {"permissions": "reviews.manage"},
]


def find_user_record(database, user_id: ObjectId, *, active_only: bool = False):
    if settings.user_store == "postgres":
        record = PostgresUserRepository().find_by_id(user_id)
        return record if record and (not active_only or record["is_active"]) else None
    query = {"_id": user_id}
    if active_only:
        query["is_active"] = True
    return database.users.find_one(query)


def users_by_ids(database, user_ids: list[ObjectId], *, active_only: bool = False) -> list[dict]:
    if not user_ids:
        return []
    if settings.user_store == "postgres":
        query = "SELECT * FROM users WHERE id=ANY(%s)"
        if active_only:
            query += " AND is_active"
        with postgres_connection() as conn:
            rows = conn.execute(query, ([str(item) for item in user_ids],)).fetchall()
        return [_user(row) for row in rows]
    query = {"_id": {"$in": user_ids}}
    if active_only:
        query["is_active"] = True
    return list(database.users.find(query))


def review_candidate_users(database, *, include_ids: list[ObjectId] | None = None) -> list[dict]:
    include_ids = include_ids or []
    if settings.user_store == "postgres":
        where = """(is_active AND (role IN ('Reviewer','Admin')
                   OR permission_grants ? 'reviews.manage'
                   OR permissions ? 'reviews.manage'))"""
        params = []
        if include_ids:
            where = f"({where} OR id=ANY(%s))"
            params.append([str(item) for item in include_ids])
        with postgres_connection() as conn:
            rows = conn.execute("SELECT * FROM users WHERE " + where, params).fetchall()
        return [_user(row) for row in rows]
    candidate = {"is_active": True, "$or": REVIEWER_CANDIDATE_FILTER}
    query = {"$or": [candidate, {"_id": {"$in": include_ids}}]} if include_ids else candidate
    return list(database.users.find(query))


def teaching_subject_ids(database, user_id) -> set[str]:
    """Subjects a user teaches: assigned teaching subjects plus subjects the user owns."""
    if user_id is None:
        return set()
    uid = str(user_id)
    if settings.user_store == "postgres":
        with postgres_connection() as conn:
            row = conn.execute("SELECT teaching_subject_ids FROM users WHERE id=%s", (uid,)).fetchone()
            owned = conn.execute("SELECT id FROM subjects WHERE owner_id=%s", (uid,)).fetchall()
        assigned = (row or {}).get("teaching_subject_ids") or []
        return {str(item) for item in assigned} | {str(item["id"]) for item in owned}
    oid = ObjectId(uid) if ObjectId.is_valid(uid) else uid
    user = database.users.find_one({"_id": oid}, {"teaching_subject_ids": 1}) or {}
    owned = database.subjects.find({"owner_id": {"$in": [oid, uid]}}, {"_id": 1})
    return {str(item) for item in user.get("teaching_subject_ids") or []} | {str(item["_id"]) for item in owned}


class SubjectMembershipError(PermissionError):
    pass


def ensure_teaches_subjects(database, current_user, subject_ids, action: str) -> None:
    """Non-admin users act only inside subjects they teach (assigned or owned)."""
    wanted = {str(item) for item in subject_ids or [] if item}
    if not wanted or current_user is None or getattr(current_user, "role", None) == "Admin":
        return
    if not wanted <= teaching_subject_ids(database, current_user.id):
        raise SubjectMembershipError(f"Bạn không được phân công học phần này nên không thể {action}")


def active_admin_ids(database) -> list[ObjectId]:
    if settings.user_store == "postgres":
        with postgres_connection() as conn:
            rows = conn.execute("SELECT id FROM users WHERE role='Admin' AND is_active").fetchall()
        return [ObjectId(row["id"]) for row in rows]
    return [item["_id"] for item in database.users.find(
        {"role": "Admin", "is_active": True}, {"_id": 1},
    )]
