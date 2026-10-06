from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.database import Database

from core.bootstrap import SCHEMA_VERSION
from core.dependencies import DEFAULT_ROLE_PERMISSIONS, effective_permissions


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def object_id(value: str | ObjectId) -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception as exc:
        raise ValueError("ID người dùng không hợp lệ") from exc


def serialize_user(user: dict) -> dict:
    # Some documents predate the nested `profile` schema and still carry
    # school/address/avatar as top-level legacy fields (see
    # scripts/database/migrate_v2.py). Fall back to those so already-stored
    # data isn't silently dropped for users that haven't been migrated.
    profile = user.get("profile") or {}
    profile = {
        "school": profile.get("school") or user.get("School", ""),
        "address": profile.get("address") or user.get("Địa Chỉ", ""),
        "avatar": profile.get("avatar") or user.get("avatar", ""),
    }
    return {
        "id": str(user["_id"]),
        "firebase_uid": user["firebase_uid"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "permissions": list(effective_permissions(user)),
        "permission_grants": sorted(
            set(effective_permissions(user)) - DEFAULT_ROLE_PERMISSIONS.get(user["role"], set())
        ),
        "permission_revokes": sorted(
            DEFAULT_ROLE_PERMISSIONS.get(user["role"], set()) - set(effective_permissions(user))
        ),
        "review_subject_ids": [str(item) for item in user.get("review_subject_ids") or []],
        "profile": profile,
        "is_active": user.get("is_active", True),
        "approval_status": user.get("approval_status") or "APPROVED",
        "created_at": user["created_at"],
        "updated_at": user["updated_at"],
    }


class UserRepository(Protocol):
    def get_review_assignment_questions(self, user_id: str | ObjectId) -> list[dict]: ...

    def count_unassigned_pending(self) -> int: ...

    def find_by_id(self, user_id: str | ObjectId) -> dict | None: ...

    def find_by_firebase_uid(self, firebase_uid: str) -> dict | None: ...

    def find_by_email(self, email: str) -> dict | None: ...

    def create(self, data: dict) -> dict: ...

    def sync_identity(self, claims: dict, *, self_signup: bool = False) -> dict: ...

    def list(
        self,
        page: int,
        page_size: int,
        role: str | None,
        search: str | None,
        approval_status: str | None = None,
    ) -> tuple[list[dict], int]: ...

    def update(self, user_id: str | ObjectId, fields: dict) -> dict | None: ...

    def count_active_admins(self) -> int: ...

    def delete_by_id(self, user_id: str | ObjectId) -> None: ...


class MongoUserRepository:
    def __init__(self, database: Database):
        self.db = database
        self.collection = database.users

    def find_by_id(self, user_id: str | ObjectId) -> dict | None:
        return self.collection.find_one({"_id": object_id(user_id)})

    def find_by_firebase_uid(self, firebase_uid: str) -> dict | None:
        return self.collection.find_one({"firebase_uid": firebase_uid})

    def find_by_email(self, email: str) -> dict | None:
        return self.collection.find_one({"email": email.lower()})

    def create(self, data: dict) -> dict:
        now = utc_now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "firebase_uid": data["firebase_uid"],
            "email": data["email"].lower(),
            "display_name": data["display_name"],
            "role": data.get("role", "Teacher"),
            "permissions": [],
            "permission_grants": data.get("permission_grants") or [],
            "permission_revokes": data.get("permission_revokes") or [],
            "profile": data.get("profile") or {"school": "", "address": "", "avatar": ""},
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        result = self.collection.insert_one(record)
        return self.collection.find_one({"_id": result.inserted_id})

    def sync_identity(self, claims: dict, *, self_signup: bool = False) -> dict:
        now = utc_now()
        firebase_uid = claims["uid"]
        email = (claims.get("email") or f"{firebase_uid}@firebase.local").lower()
        display_name = claims.get("name") or email.split("@", 1)[0]
        avatar = claims.get("picture") or ""
        profile_defaults = {"school": "", "address": "", "avatar": ""}
        profile_overlay = {"avatar": avatar} if avatar else {}
        identity_filter = (
            {"$or": [{"firebase_uid": firebase_uid}, {"email": email}]}
            if claims.get("email")
            else {"firebase_uid": firebase_uid}
        )
        return self.collection.find_one_and_update(
            identity_filter,
            [
                {
                    "$set": {
                        "schema_version": {"$ifNull": ["$schema_version", SCHEMA_VERSION]},
                        "firebase_uid": firebase_uid,
                        "email": email,
                        "display_name": {"$ifNull": ["$display_name", display_name]},
                        "role": {"$ifNull": ["$role", "Teacher"]},
                        "permissions": {"$ifNull": ["$permissions", []]},
                        "profile": {
                            "$mergeObjects": [
                                profile_defaults,
                                {"$ifNull": ["$profile", {}]},
                                profile_overlay,
                            ]
                        },
                        "is_active": {
                            "$ifNull": [
                                "$is_active",
                                {"$cond": [{"$ifNull": ["$created_at", False]}, True, not self_signup]},
                            ]
                        },
                        "approval_status": {
                            "$ifNull": [
                                "$approval_status",
                                {"$cond": [
                                    {"$ifNull": ["$created_at", False]},
                                    "APPROVED",
                                    "PENDING" if self_signup else "APPROVED",
                                ]},
                            ]
                        },
                        "created_at": {"$ifNull": ["$created_at", now]},
                        "updated_at": now,
                    }
                }
            ],
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    def list(
        self,
        page: int,
        page_size: int,
        role: str | None,
        search: str | None,
        approval_status: str | None = None,
    ) -> tuple[list[dict], int]:
        query: dict = {}
        if role:
            query["role"] = role
        if approval_status == "PENDING":
            query["approval_status"] = "PENDING"
        elif approval_status == "APPROVED":
            query["approval_status"] = {"$ne": "PENDING"}
        if search:
            query["$or"] = [
                {"display_name": {"$regex": search, "$options": "i"}},
                {"email": {"$regex": search, "$options": "i"}},
            ]
        total = self.collection.count_documents(query)
        records = list(
            self.collection.find(query)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return records, total

    def update(self, user_id: str | ObjectId, fields: dict) -> dict | None:
        fields = {**fields, "updated_at": utc_now()}
        return self.collection.find_one_and_update(
            {"_id": object_id(user_id)},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )

    def count_active_admins(self) -> int:
        return self.collection.count_documents({"role": "Admin", "is_active": True})

    def delete_by_id(self, user_id: str | ObjectId) -> None:
        self.collection.delete_one({"_id": object_id(user_id)})

    def get_stats(self, user_id: str | ObjectId) -> dict:
        oid = object_id(user_id)
        return {
            "documents_count": self.db.documents.count_documents(
                {"uploaded_by_user_id": oid, "status": {"$ne": "ARCHIVED"}}
            ),
            "questions_count": self.db.questions.count_documents(
                {"created_by_user_id": oid, "lifecycle_status": {"$ne": "ARCHIVED"}}
            ),
            "pending_questions_count": self.db.questions.count_documents(
                {"created_by_user_id": oid, "review_status": "PENDING"}
            ),
        }

    def get_calendar_documents(self, user_id: str | ObjectId) -> list[dict]:
        oid = object_id(user_id)
        return list(
            self.db.documents.find(
                {"uploaded_by_user_id": oid, "status": {"$ne": "ARCHIVED"}}
            )
        )

    def get_calendar_questions(self, user_id: str | ObjectId) -> list[dict]:
        oid = object_id(user_id)
        return list(
            self.db.questions.find(
                {"created_by_user_id": oid, "lifecycle_status": {"$ne": "ARCHIVED"}}
            )
        )

    def get_review_assignment_questions(self, user_id: str | ObjectId) -> list[dict]:
        return list(self.db.questions.find({
            "lifecycle_status": "ACTIVE", "review_status": "PENDING",
            "review_assignment.reviewer_user_id": object_id(user_id),
            "review_assignment.status": {"$in": ["ASSIGNED", "IN_REVIEW"]},
        }))

    def count_unassigned_pending(self) -> int:
        return self.db.questions.count_documents({
            "lifecycle_status": "ACTIVE", "review_status": "PENDING",
            "review_assignment.status": {"$in": [None, "UNASSIGNED"]},
        })

    def get_document_ids_with_questions(self, document_ids: list[ObjectId]) -> set[str]:
        if not document_ids:
            return set()
        cursor = self.db.questions.find(
            {"document_id": {"$in": document_ids}, "lifecycle_status": {"$ne": "ARCHIVED"}},
            {"document_id": 1},
        )
        return {str(doc["document_id"]) for doc in cursor if doc.get("document_id")}
