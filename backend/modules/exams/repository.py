from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from bson import ObjectId
from pymongo.database import Database

from core.audit import write_audit_event
from core.bootstrap import SCHEMA_VERSION
from core.database import mongo_transaction
from modules.exams.schemas import MAX_VARIANTS_PER_EXAM


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def object_id(value: str | ObjectId, field_name: str = "id") -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception as exc:
        raise ValueError(f"{field_name} không hợp lệ") from exc


def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


class ExamRepository(Protocol):
    def create(self, exam: dict) -> dict: ...

    def find(self, exam_id: str | ObjectId) -> dict | None: ...

    def list(self, page: int, page_size: int, created_by_user_id: ObjectId | None,
             *, visible_to_user_id: ObjectId | None = None) -> tuple[list[dict], int]: ...

    def update(self, exam_id: str | ObjectId, updates: dict, *, audit: dict | None = None,
               expected_status: str | None = None, require_no_variants: bool = False) -> dict | None: ...

    def delete(self, exam_id: str | ObjectId, deleted_by_user_id: ObjectId | None = None,
               *, audit: dict | None = None) -> bool: ...

    def count_variants(self, exam_id: str | ObjectId) -> int: ...


class ExamVariantRepository(Protocol):
    def create(self, variant: dict) -> dict: ...

    def find(self, variant_id: str | ObjectId) -> dict | None: ...

    def list_by_exam(self, exam_id: str | ObjectId) -> list[dict]: ...

    def delete(self, variant_id: str | ObjectId, *, audit: dict | None = None) -> bool: ...


class MongoExamRepository:
    def __init__(self, database: Database):
        self.db = database

    def create(self, exam: dict) -> dict:
        self.db.exams.insert_one(exam)
        return exam

    def find(self, exam_id: str | ObjectId) -> dict | None:
        return self.db.exams.find_one(
            {"_id": object_id(exam_id, "exam_id"), "schema_version": SCHEMA_VERSION, "deleted_at": None}
        )

    def list(
        self,
        page: int,
        page_size: int,
        created_by_user_id: ObjectId | None,
        *,
        visible_to_user_id: ObjectId | None = None,
    ) -> tuple[list[dict], int]:
        match: dict = {"schema_version": SCHEMA_VERSION, "deleted_at": None}
        if created_by_user_id is not None:
            match["created_by_user_id"] = created_by_user_id
        if visible_to_user_id is not None:
            match["$or"] = [
                {"created_by_user_id": visible_to_user_id},
                {"shared_with_user_ids": visible_to_user_id},
            ]
        total = self.db.exams.count_documents(match)
        items = list(
            self.db.exams.find(match)
            .sort("updated_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return items, total

    def update(self, exam_id: str | ObjectId, updates: dict, *, audit: dict | None = None,
               expected_status: str | None = None, require_no_variants: bool = False) -> dict | None:
        """``audit`` is written in the same transaction; an audit failure rolls the update back.

        Returns None when the exam is missing or no longer in ``expected_status``.
        """
        updates = dict(updates)
        updates["updated_at"] = utc_now()
        exam_oid = object_id(exam_id, "exam_id")
        query: dict = {"_id": exam_oid, "schema_version": SCHEMA_VERSION, "deleted_at": None}
        if expected_status:
            query["status"] = {"$in": [expected_status, expected_status.lower()]}
        with mongo_transaction() as session:
            if require_no_variants and self.db.exam_variants.count_documents(
                {"exam_id": exam_oid}, session=session,
            ):
                raise ValueError("Đề thi đã có mã đề")
            result = self.db.exams.find_one_and_update(
                query,
                {"$set": updates},
                return_document=True,
                session=session,
            )
            if result and audit:
                write_audit_event(self.db, session=session, **audit)
        return result

    def delete(self, exam_id: str | ObjectId, deleted_by_user_id: ObjectId | None = None,
               *, audit: dict | None = None) -> bool:
        now = utc_now()
        with mongo_transaction() as session:
            result = self.db.exams.update_one(
                {"_id": object_id(exam_id, "exam_id"), "schema_version": SCHEMA_VERSION, "deleted_at": None},
                {"$set": {"deleted_at": now, "deleted_by_user_id": deleted_by_user_id, "updated_at": now}},
                session=session,
            )
            if result.modified_count == 1 and audit:
                write_audit_event(self.db, session=session, **audit)
        return result.modified_count == 1

    def count_variants(self, exam_id: str | ObjectId) -> int:
        return self.db.exam_variants.count_documents(
            {"exam_id": object_id(exam_id, "exam_id")}
        )


class MongoExamVariantRepository:
    def __init__(self, database: Database):
        self.db = database

    def create(self, variant: dict) -> dict:
        exam_id = variant["exam_id"]
        with mongo_transaction() as session:
            exam = self.db.exams.find_one({"_id": exam_id, "deleted_at": None}, {"status": 1}, session=session)
            if not exam:
                raise LookupError("Không tìm thấy đề thi")
            if str(exam.get("status") or "").upper() != "FINALIZED":
                raise ValueError("Chỉ có thể tạo mã đề khi đề thi đang ở trạng thái đã chốt")
            existing = self.db.exam_variants.count_documents(
                {"exam_id": exam_id}, session=session
            )
            if existing >= MAX_VARIANTS_PER_EXAM:
                raise ValueError(
                    f"Đã đạt tối đa {MAX_VARIANTS_PER_EXAM} mã đề cho kỳ thi này"
                )
            duplicate = self.db.exam_variants.find_one(
                {"exam_id": exam_id, "exam_code": variant["exam_code"]},
                session=session,
            )
            if duplicate:
                raise ValueError("Mã đề này đã tồn tại trong kỳ thi")
            self.db.exam_variants.insert_one(variant, session=session)
        return variant

    def find(self, variant_id: str | ObjectId) -> dict | None:
        return self.db.exam_variants.find_one(
            {"_id": object_id(variant_id, "variant_id")}
        )

    def list_by_exam(self, exam_id: str | ObjectId) -> list[dict]:
        return list(
            self.db.exam_variants.find(
                {"exam_id": object_id(exam_id, "exam_id")}
            ).sort("created_at", 1)
        )

    def delete(self, variant_id: str | ObjectId, *, audit: dict | None = None) -> bool:
        """Refuses (returns False) once the exam has an official export."""
        variant_oid = object_id(variant_id, "variant_id")
        with mongo_transaction() as session:
            variant = self.db.exam_variants.find_one({"_id": variant_oid}, {"exam_id": 1}, session=session)
            if not variant:
                return False
            exam = self.db.exams.find_one(
                {"_id": variant["exam_id"], "deleted_at": None, "official_exported_at": None},
                {"_id": 1}, session=session,
            )
            if not exam:
                return False
            result = self.db.exam_variants.delete_one({"_id": variant_oid}, session=session)
            if result.deleted_count == 1 and audit:
                write_audit_event(self.db, session=session, **audit)
        return result.deleted_count == 1
