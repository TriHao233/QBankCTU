"""Read reviewer and admin identities from the selected user store."""

from bson import ObjectId

from core.config import settings
from core.postgres import postgres_connection
from modules.users.postgres_repository import PostgresUserRepository, _user


REVIEWER_CANDIDATE_FILTER = [
    # Admin không còn là ứng viên duyệt; chỉ Teacher/Reviewer (cũ) có reviews.manage.
    {"role": "Reviewer"},
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
        where = """(is_active AND (role = 'Reviewer'
                   OR permission_grants ? 'reviews.manage'
                   OR permissions ? 'reviews.manage'
                   OR (role = 'Teacher' AND NOT permission_revokes ? 'reviews.manage')))"""
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


def active_admin_ids(database) -> list[ObjectId]:
    if settings.user_store == "postgres":
        with postgres_connection() as conn:
            rows = conn.execute("SELECT id FROM users WHERE role='Admin' AND is_active").fetchall()
        return [ObjectId(row["id"]) for row in rows]
    return [item["_id"] for item in database.users.find(
        {"role": "Admin", "is_active": True}, {"_id": 1},
    )]
