import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core.bootstrap import SCHEMA_VERSION
from core.audit import write_audit_event
from core.config import settings
from core.database import get_database
from modules.catalog.schemas import (
    AiModelActivationPayload,
    AiModelVersionActivationPayload,
    AiModelHealthCheckPayload,
    AiModelPayload,
    ChapterPayload,
    ChapterUpdatePayload,
    EvaluationPolicyActivationPayload,
    EvaluationPolicyPayload,
    LearningOutcomePayload,
    LearningOutcomeUpdatePayload,
    PromptTemplateActivationPayload,
    PromptTemplatePayload,
    PromptTemplateTestPayload,
    SubjectPayload,
    SubjectUpdatePayload,
)
from modules.generation.llm.factory import get_llm_service
from modules.generation.llm.model_registry import (
    EVALUATION_CAPABILITY,
    GENERATION_CAPABILITY,
    available_model_options,
    resolve_model_snapshot,
)
from modules.generation.prompt_builder import PromptBuilder
from modules.questions.repository import json_safe, object_id
from modules.catalog.postgres_ai_repository import PostgresAiRepository
from modules.catalog.postgres_subject_repository import PostgresSubjectRepository, SubjectCodeConflict
from modules.catalog.subject_approval import (
    SUBJECT_APPROVED,
    SUBJECT_PENDING_APPROVAL,
    SUBJECT_REJECTED,
    subject_approval_status,
    subject_usable_by,
)

logger = logging.getLogger(__name__)

SUBJECT_AUDIT_FIELDS = ("subject_code", "subject_name", "description", "is_active", "approval_status",
                        "approval_reason")
CHAPTER_AUDIT_FIELDS = ("chapter_code", "chapter_name", "sequence_no", "is_active")
CLO_AUDIT_FIELDS = ("clo_code", "description", "target_weight", "is_active")
MODEL_AUDIT_FIELDS = (
    "model_code", "model_name", "display_name", "description", "runtime", "kind",
    "revision", "capabilities", "priority", "is_local", "is_active",
)
PROMPT_AUDIT_FIELDS = ("template_key", "version", "kind", "name", "content_hash", "is_active")
POLICY_AUDIT_FIELDS = ("policy_name", "version", "weights", "thresholds", "is_active")


def _audit_snapshot(record: dict | None, fields: tuple[str, ...]) -> dict:
    if not record:
        return {}
    snapshot = {key: record.get(key) for key in fields}
    if fields == MODEL_AUDIT_FIELDS:
        # Model config can contain credentials. Record a fingerprint, never its values.
        if "config_hash" in record:
            snapshot["config_hash"] = record["config_hash"]
        else:
            config = json.dumps(record.get("config") or {}, sort_keys=True, default=str)
            snapshot["config_hash"] = hashlib.sha256(config.encode("utf-8")).hexdigest()
    return json_safe(snapshot)


def _child(record: dict, key: str, identifier: ObjectId) -> dict | None:
    return next((item for item in record.get(key) or [] if _subdoc_id(item) == identifier), None)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


FALLBACK_EVALUATION_POLICY = {
    "policy_name": "Default fallback",
    "version": 1,
    "weights": {
        "faithfulness": 0.35,
        "contextual_relevancy": 0.20,
        "answer_relevancy": 0.15,
        "bloom_alignment": 0.15,
        "clo_alignment": 0.15,
    },
    "thresholds": {"yellow_min": 0.50, "green_min": 0.75, "pass_min": 0.65},
    "is_active": True,
}


def _subdoc_id(item: dict) -> ObjectId | str | None:
    return item.get("_id") or item.get("id")


def _with_child_usage(item: dict, usage_counts: dict[str, dict]) -> dict:
    item_id = _subdoc_id(item)
    return {
        **item,
        "id": item_id,
        "usage_counts": usage_counts.get(str(item_id), {}),
    }


ADMIN_CATALOG_PERMISSION = "admin.catalog"
SUBJECT_OWNER_PERMISSION = "catalog.subjects.manage_own"


CatalogConflictError = SubjectCodeConflict


def _is_catalog_admin(viewer: Any) -> bool:
    # viewer=None means an internal/system caller, which bypasses ownership rules.
    if viewer is None:
        return True
    return ADMIN_CATALOG_PERMISSION in set(getattr(viewer, "permissions", ()) or ())


def _can_manage_subject(record: dict, viewer: Any) -> bool:
    if _is_catalog_admin(viewer):
        return True
    owner_id = record.get("owner_id")
    return owner_id is not None and owner_id == getattr(viewer, "id", None)


def _subject_response(
    record: dict,
    usage_counts: dict[str, Any] | None = None,
    viewer: Any = None,
) -> dict:
    usage_counts = usage_counts or {}
    chapters = sorted(
        record.get("chapters") or [],
        key=lambda item: (item.get("sequence_no") or 0, item.get("chapter_code") or ""),
    )
    learning_outcomes = record.get("learning_outcomes") or []
    return json_safe(
        {
            "id": record["_id"],
            "subject_code": record["subject_code"],
            "subject_name": record["subject_name"],
            "description": record.get("description", ""),
            "chapters": [
                _with_child_usage(chapter, usage_counts.get("chapters", {}))
                for chapter in chapters
            ],
            "learning_outcomes": [
                _with_child_usage(outcome, usage_counts.get("learning_outcomes", {}))
                for outcome in learning_outcomes
            ],
            "is_active": record.get("is_active", True),
            "usage_counts": usage_counts.get("subject", {}),
            "owner_id": record.get("owner_id"),
            "owner_email": record.get("owner_email", ""),
            "can_manage": _can_manage_subject(record, viewer),
            "approval_status": subject_approval_status(record),
            "approval_reason": record.get("approval_reason") or "",
            "approval_reviewed_at": record.get("approval_reviewed_at"),
            "approval_reviewed_by_user_id": record.get("approval_reviewed_by_user_id"),
        }
    )


def _subject_visible_to(record: dict, viewer: Any) -> bool:
    return subject_usable_by(record, getattr(viewer, "id", None), is_admin=_is_catalog_admin(viewer))


class CatalogService:
    def __init__(self, database):
        self.db = database
        self.ai_repo = PostgresAiRepository() if settings.ai_config_store == "postgres" else None
        self.subject_repo = PostgresSubjectRepository() if settings.catalog_store == "postgres" else None

    def _audit(
        self, action: str, entity_type: str, record: dict, viewer: Any,
        fields: tuple[str, ...], *, before: dict | None = None,
        label: str = "", metadata: dict | None = None,
    ) -> None:
        previous = _audit_snapshot(before, fields)
        current = _audit_snapshot(record, fields)
        if previous == current:
            return
        try:
            write_audit_event(
                self.db,
                action=action,
                entity_type=entity_type,
                entity_id=record["_id"],
                actor_user_id=getattr(viewer, "id", None),
                actor_role=getattr(viewer, "role", None),
                before=previous,
                after=current,
                metadata={"label": label, **(metadata or {})},
            )
        except Exception as exc:
            logger.warning("Failed to write catalog audit event %s: %s", action, exc)

    def _audit_deactivated_versions(
        self, active: list[dict], selected_id: ObjectId, viewer: Any,
        *, entity_type: str, fields: tuple[str, ...], action: str, name_key: str,
    ) -> None:
        for old in active:
            if old["_id"] == selected_id:
                continue
            current = {**old, "is_active": False}
            self._audit(action, entity_type, current, viewer, fields, before=old,
                        label=f'{old[name_key]} v{old["version"]}')

    def _count(self, collection_name: str, query: dict) -> int:
        collection = getattr(self.db, collection_name, None)
        if collection is None:
            return 0
        return collection.count_documents(query)

    def _count_documents(self, subject_id, chapter_id=None) -> int:
        if settings.document_store == "postgres":
            from modules.documents.postgres_repository import PostgresDocumentRepository
            return PostgresDocumentRepository().count_using_subject(subject_id, chapter_id)
        if chapter_id is None:
            return self._count(
                "documents",
                {
                    "$or": [{"subject_ids": subject_id}, {"subject_id": subject_id}],
                    "archived_at": None,
                },
            )
        return self._count(
            "documents",
            {"subject_id": subject_id, "chapter_id": chapter_id, "archived_at": None},
        )

    def _count_questions_using(self, *, subject_id=None, chapter_id=None, clo_id=None) -> int:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            return PostgresQuestionRepository.count_current_using(
                subject_id=subject_id, chapter_id=chapter_id, clo_id=clo_id,
            )
        if subject_id is not None:
            return self._count_current_questions({"classification.subject.id": subject_id})
        if chapter_id is not None:
            return self._count_current_questions({"classification.chapter.id": chapter_id})
        return self._count_current_questions({"clos.id": clo_id})

    def _count_exams(self, *, subject_id=None, chapter_id=None) -> int:
        if settings.exam_store == "postgres":
            from modules.exams.postgres_repository import PostgresExamRepository
            return PostgresExamRepository.count(subject_id=subject_id, chapter_id=chapter_id)
        if subject_id is not None:
            return self._count("exams", {"subject_id": subject_id, "deleted_at": None})
        return self._count("exams", {"matrix.chapter_id": chapter_id, "deleted_at": None})

    def _count_current_questions(self, version_query: dict) -> int:
        versions = getattr(self.db, "question_versions", None)
        questions = getattr(self.db, "questions", None)
        if versions is None or questions is None:
            return 0
        version_ids = [
            item["_id"]
            for item in versions.find(version_query, {"_id": 1})
        ]
        if not version_ids:
            return 0
        return questions.count_documents(
            {
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
                "current_version_id": {"$in": version_ids},
            }
        )

    def _usage_counts(self, subject: dict) -> dict[str, Any]:
        subject_id = subject["_id"]
        subject_counts = {
            "documents": self._count_documents(subject_id),
            "questions": self._count_questions_using(subject_id=subject_id),
            "exams": self._count_exams(subject_id=subject_id),
        }
        chapter_counts = {}
        for chapter in subject.get("chapters") or []:
            chapter_id = _subdoc_id(chapter)
            if not chapter_id:
                continue
            chapter_counts[str(chapter_id)] = {
                "documents": self._count_documents(subject_id, chapter_id),
                "questions": self._count_questions_using(chapter_id=chapter_id),
                "exams": self._count_exams(chapter_id=chapter_id),
            }
        outcome_counts = {}
        for outcome in subject.get("learning_outcomes") or []:
            outcome_id = _subdoc_id(outcome)
            if not outcome_id:
                continue
            outcome_counts[str(outcome_id)] = {
                "questions": self._count_questions_using(clo_id=outcome_id),
            }
        return {
            "subject": subject_counts,
            "chapters": chapter_counts,
            "learning_outcomes": outcome_counts,
        }

    def _subject_or_404(self, subject_id: str | ObjectId) -> dict:
        identifier = object_id(subject_id, "subject_id")
        subject = (self.subject_repo.find_by_id(identifier) if self.subject_repo else
                   self.db.subjects.find_one({"_id": identifier}))
        if not subject:
            raise LookupError("Không tìm thấy học phần")
        return subject

    @staticmethod
    def _ensure_can_manage(subject: dict, viewer: Any) -> None:
        if not _subject_visible_to(subject, viewer):
            raise LookupError("Không tìm thấy học phần")
        if not _can_manage_subject(subject, viewer):
            raise PermissionError("Bạn chỉ có thể chỉnh sửa học phần do mình tạo")

    def _find_subject_by_code(self, subject_code: str) -> dict | None:
        if self.subject_repo:
            return self.subject_repo.find_by_code(subject_code)
        normalized = subject_code.strip().lower()
        for subject in self.db.subjects.find():
            if str(subject.get("subject_code", "")).strip().lower() == normalized:
                return subject
        return None

    def _ensure_subject_code_available(
        self,
        subject_code: str,
        *,
        current_subject_id: ObjectId | None = None,
    ) -> str:
        normalized = subject_code.strip()
        duplicate = self._find_subject_by_code(normalized)
        if duplicate and duplicate.get("_id") != current_subject_id:
            raise CatalogConflictError("Mã môn học đã tồn tại")
        return normalized

    @staticmethod
    def _ensure_child_code_available(
        subject: dict,
        collection_key: str,
        code_key: str,
        code: str,
        *,
        current_child_id: ObjectId | None = None,
    ) -> str:
        normalized = code.strip()
        for item in subject.get(collection_key) or []:
            item_id = _subdoc_id(item)
            if current_child_id is not None and item_id == current_child_id:
                continue
            if str(item.get(code_key, "")).strip().lower() == normalized.lower():
                raise ValueError("Mã đã tồn tại trong học phần này")
        return normalized

    def list_subjects(self, viewer: Any = None) -> list[dict]:
        records = (self.subject_repo.list() if self.subject_repo else
                   self.db.subjects.find().sort("subject_code", 1))
        return [
            _subject_response(record, self._usage_counts(record), viewer)
            for record in records
            if _subject_visible_to(record, viewer)
        ]

    def create_subject(self, payload: SubjectPayload, viewer: Any = None) -> dict:
        now = utc_now()
        subject_code = self._ensure_subject_code_available(payload.subject_code)
        approved = _is_catalog_admin(viewer)
        record = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "subject_code": subject_code,
            "subject_name": payload.subject_name,
            "description": payload.description,
            "chapters": [],
            "learning_outcomes": [],
            "is_active": payload.is_active,
            "created_at": now,
            "updated_at": now,
            "owner_id": getattr(viewer, "id", None),
            "owner_email": getattr(viewer, "email", "") or "",
            # Teacher-created subjects stay private to their owner until an admin approves them.
            "approval_status": SUBJECT_APPROVED if approved else SUBJECT_PENDING_APPROVAL,
            "approval_reason": "",
            "approval_reviewed_at": now if approved else None,
            "approval_reviewed_by_user_id": getattr(viewer, "id", None) if approved else None,
        }
        if self.subject_repo:
            record = self.subject_repo.create(record, viewer)
        else:
            try:
                self.db.subjects.insert_one(record)
            except DuplicateKeyError as exc:
                raise CatalogConflictError("Mã môn học đã tồn tại") from exc
            self._audit("catalog.subject_create", "subject", record, viewer, SUBJECT_AUDIT_FIELDS,
                        label=record["subject_code"])
        if not approved:
            self._notify_admins_subject_pending(record, viewer)
        return _subject_response(record, self._usage_counts(record), viewer)

    def _set_subject_approval(self, subject: dict, fields: dict, viewer: Any, *, action: str,
                              expected_status: str) -> dict:
        if self.subject_repo:
            return self.subject_repo.update(subject["_id"], fields, viewer, action=action,
                                            expected_approval_status=expected_status)
        previous = _audit_snapshot(subject, SUBJECT_AUDIT_FIELDS)
        status_filter = (
            {"$in": [None, SUBJECT_APPROVED]} if expected_status == SUBJECT_APPROVED else expected_status
        )
        record = self.db.subjects.find_one_and_update(
            {"_id": subject["_id"], "approval_status": status_filter},
            {"$set": {**fields, "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )
        if record is None:
            raise ValueError("Trạng thái duyệt của học phần vừa thay đổi, hãy tải lại")
        self._audit(action, "subject", record, viewer, SUBJECT_AUDIT_FIELDS, before=previous,
                    label=record["subject_code"])
        return record

    def approve_subject(self, subject_id: str, viewer: Any) -> dict:
        if not _is_catalog_admin(viewer):
            raise PermissionError("Chỉ quản trị viên được duyệt học phần")
        subject = self._subject_or_404(subject_id)
        if subject_approval_status(subject) != SUBJECT_PENDING_APPROVAL:
            raise ValueError("Học phần không ở trạng thái chờ duyệt")
        record = self._set_subject_approval(subject, {
            "approval_status": SUBJECT_APPROVED, "approval_reason": "",
            "approval_reviewed_at": utc_now(), "approval_reviewed_by_user_id": getattr(viewer, "id", None),
        }, viewer, action="catalog.subject_approve", expected_status=SUBJECT_PENDING_APPROVAL)
        self._notify_subject_owner(record, viewer, approved=True)
        return _subject_response(record, self._usage_counts(record), viewer)

    def reject_subject(self, subject_id: str, reason: str, viewer: Any) -> dict:
        if not _is_catalog_admin(viewer):
            raise PermissionError("Chỉ quản trị viên được từ chối học phần")
        subject = self._subject_or_404(subject_id)
        if subject_approval_status(subject) != SUBJECT_PENDING_APPROVAL:
            raise ValueError("Học phần không ở trạng thái chờ duyệt")
        record = self._set_subject_approval(subject, {
            "approval_status": SUBJECT_REJECTED, "approval_reason": reason,
            "approval_reviewed_at": utc_now(), "approval_reviewed_by_user_id": getattr(viewer, "id", None),
        }, viewer, action="catalog.subject_reject", expected_status=SUBJECT_PENDING_APPROVAL)
        self._notify_subject_owner(record, viewer, approved=False)
        return _subject_response(record, self._usage_counts(record), viewer)

    def resubmit_subject(self, subject_id: str, viewer: Any) -> dict:
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        if subject_approval_status(subject) != SUBJECT_REJECTED:
            raise ValueError("Chỉ gửi duyệt lại được học phần đã bị từ chối")
        record = self._set_subject_approval(subject, {
            "approval_status": SUBJECT_PENDING_APPROVAL,
            "approval_reviewed_at": None, "approval_reviewed_by_user_id": None,
        }, viewer, action="catalog.subject_resubmit", expected_status=SUBJECT_REJECTED)
        self._notify_admins_subject_pending(record, viewer)
        return _subject_response(record, self._usage_counts(record), viewer)

    def _notify_admins_subject_pending(self, record: dict, viewer: Any) -> None:
        try:
            from modules.notifications.service import NotificationService
            from modules.users.store import active_admin_ids
            NotificationService(self.db).create_many([{
                "recipient_user_id": admin_id, "actor_user_id": getattr(viewer, "id", None),
                "type": "SUBJECT_APPROVAL_REQUESTED",
                "title": f"Học phần {record['subject_code']} chờ duyệt",
                "body": f"{getattr(viewer, 'email', '') or 'Giảng viên'} đề nghị thêm học phần "
                        f"{record['subject_name']} vào danh mục chung.",
                "link": "/danh-muc",
                "entity": {"type": "subject", "id": str(record["_id"])},
            } for admin_id in active_admin_ids(self.db)])
        except Exception as exc:
            logger.warning("Failed to notify admins about pending subject: %s", exc)

    def _notify_subject_owner(self, record: dict, viewer: Any, *, approved: bool) -> None:
        owner_id = record.get("owner_id")
        if owner_id is None or str(owner_id) == str(getattr(viewer, "id", None)):
            return
        try:
            from modules.notifications.service import NotificationService
            reason = record.get("approval_reason") or ""
            NotificationService(self.db).create(
                recipient_user_id=owner_id, actor_user_id=getattr(viewer, "id", None),
                type="SUBJECT_APPROVED" if approved else "SUBJECT_REJECTED",
                title=(f"Học phần {record['subject_code']} đã được duyệt" if approved
                       else f"Học phần {record['subject_code']} bị từ chối"),
                body=("Học phần đã vào danh mục chung." if approved
                      else f"Lý do: {reason}. Bạn có thể sửa rồi gửi duyệt lại."),
                link="/quan-ly-hoc-phan",
                entity={"type": "subject", "id": str(record["_id"])},
            )
        except Exception as exc:
            logger.warning("Failed to notify subject owner: %s", exc)

    def deactivate_subject(self, subject_id: str, viewer: Any = None) -> dict:
        """Xoá mềm học phần: giữ nguyên dữ liệu lịch sử, chỉ ẩn khỏi danh sách chọn."""
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        previous = _audit_snapshot(subject, SUBJECT_AUDIT_FIELDS)
        if self.subject_repo:
            record = self.subject_repo.update(subject["_id"], {"is_active": False}, viewer,
                                              deactivate=True)
        else:
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"]},
                {"$set": {"is_active": False, "updated_at": utc_now()}},
                return_document=ReturnDocument.AFTER,
            )
            self._audit("catalog.subject_deactivate", "subject", record, viewer, SUBJECT_AUDIT_FIELDS,
                        before=previous, label=record["subject_code"])
        return _subject_response(record, self._usage_counts(record), viewer)

    def update_subject(
        self,
        subject_id: str,
        payload: SubjectUpdatePayload,
        viewer: Any = None,
    ) -> dict:
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        previous = _audit_snapshot(subject, SUBJECT_AUDIT_FIELDS)
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        confirm_code_change = fields.pop("confirm_code_change", False)
        if "subject_code" in fields:
            fields["subject_code"] = self._ensure_subject_code_available(
                fields["subject_code"],
                current_subject_id=subject["_id"],
            )
            if fields["subject_code"] == subject.get("subject_code"):
                fields.pop("subject_code")
            elif not confirm_code_change:
                usage = self._usage_counts(subject)["subject"]
                used = {key: value for key, value in usage.items() if value}
                if used:
                    labels = {"questions": "câu hỏi", "documents": "tài liệu", "exams": "đề thi"}
                    detail = ", ".join(f"{count} {labels.get(key, key)}" for key, count in used.items())
                    raise CatalogConflictError(
                        f"Học phần đang được dùng ({detail}). Các phiên bản câu hỏi đã lưu vẫn ghi mã cũ; "
                        "xác nhận nếu vẫn muốn đổi mã."
                    )
        if not fields:
            return _subject_response(subject, self._usage_counts(subject), viewer)
        if self.subject_repo:
            record = self.subject_repo.update(subject["_id"], fields, viewer)
        else:
            fields["updated_at"] = utc_now()
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"]},
                {"$set": fields},
                return_document=ReturnDocument.AFTER,
            )
            self._audit("catalog.subject_update", "subject", record, viewer, SUBJECT_AUDIT_FIELDS,
                        before=previous, label=record["subject_code"])
        return _subject_response(record, self._usage_counts(record), viewer)

    def add_chapter(
        self,
        subject_id: str,
        payload: ChapterPayload,
        viewer: Any = None,
    ) -> dict:
        now = utc_now()
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        chapter_code = self._ensure_child_code_available(
            subject,
            "chapters",
            "chapter_code",
            payload.chapter_code,
        )
        chapter = {
            "_id": ObjectId(),
            "chapter_code": chapter_code,
            "chapter_name": payload.chapter_name,
            "sequence_no": payload.sequence_no,
            "is_active": payload.is_active,
            "created_at": now,
            "updated_at": now,
        }
        if self.subject_repo:
            record = self.subject_repo.save_child(subject["_id"], "chapter", chapter, viewer,
                                                  create=True)
        else:
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"]},
                {"$push": {"chapters": chapter}, "$set": {"updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            self._audit("catalog.chapter_create", "chapter", chapter, viewer, CHAPTER_AUDIT_FIELDS,
                        label=chapter["chapter_code"], metadata={"subject_id": str(subject["_id"])})
        return _subject_response(record, self._usage_counts(record), viewer)

    def update_chapter(
        self,
        subject_id: str,
        chapter_id: str,
        payload: ChapterUpdatePayload,
        viewer: Any = None,
    ) -> dict:
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        chapter_oid = object_id(chapter_id, "chapter_id")
        if not any(_subdoc_id(item) == chapter_oid for item in subject.get("chapters") or []):
            raise LookupError("Không tìm thấy chương")
        previous = _audit_snapshot(_child(subject, "chapters", chapter_oid), CHAPTER_AUDIT_FIELDS)
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "chapter_code" in fields:
            fields["chapter_code"] = self._ensure_child_code_available(
                subject,
                "chapters",
                "chapter_code",
                fields["chapter_code"],
                current_child_id=chapter_oid,
            )
        if not fields:
            return _subject_response(subject, self._usage_counts(subject), viewer)
        now = utc_now()
        if self.subject_repo:
            chapter = {"_id": chapter_oid, **fields, "updated_at": now}
            record = self.subject_repo.save_child(subject["_id"], "chapter", chapter, viewer,
                                                  create=False)
        else:
            update_fields = {
                f"chapters.$.{key}": value
                for key, value in fields.items()
            }
            update_fields["chapters.$.updated_at"] = now
            update_fields["updated_at"] = now
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"], "chapters._id": chapter_oid},
                {"$set": update_fields},
                return_document=ReturnDocument.AFTER,
            )
        if not record:
            raise LookupError("Không tìm thấy chương")
        updated_chapter = _child(record, "chapters", chapter_oid)
        if not self.subject_repo:
            self._audit("catalog.chapter_update", "chapter", updated_chapter, viewer, CHAPTER_AUDIT_FIELDS,
                        before=previous,
                        label=updated_chapter["chapter_code"], metadata={"subject_id": str(subject["_id"])})
        return _subject_response(record, self._usage_counts(record), viewer)

    def add_learning_outcome(
        self,
        subject_id: str,
        payload: LearningOutcomePayload,
        viewer: Any = None,
    ) -> dict:
        now = utc_now()
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        clo_code = self._ensure_child_code_available(
            subject,
            "learning_outcomes",
            "clo_code",
            payload.clo_code,
        )
        outcome = {
            "_id": ObjectId(),
            "clo_code": clo_code,
            "description": payload.description,
            "target_weight": payload.target_weight,
            "is_active": payload.is_active,
            "created_at": now,
            "updated_at": now,
        }
        if self.subject_repo:
            record = self.subject_repo.save_child(subject["_id"], "clo", outcome, viewer,
                                                  create=True)
        else:
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"]},
                {"$push": {"learning_outcomes": outcome}, "$set": {"updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            self._audit("catalog.clo_create", "clo", outcome, viewer, CLO_AUDIT_FIELDS,
                        label=outcome["clo_code"], metadata={"subject_id": str(subject["_id"])})
        return _subject_response(record, self._usage_counts(record), viewer)

    def update_learning_outcome(
        self,
        subject_id: str,
        clo_id: str,
        payload: LearningOutcomeUpdatePayload,
        viewer: Any = None,
    ) -> dict:
        subject = self._subject_or_404(subject_id)
        self._ensure_can_manage(subject, viewer)
        clo_oid = object_id(clo_id, "clo_id")
        if not any(
            _subdoc_id(item) == clo_oid
            for item in subject.get("learning_outcomes") or []
        ):
            raise LookupError("Không tìm thấy CLO")
        previous = _audit_snapshot(_child(subject, "learning_outcomes", clo_oid), CLO_AUDIT_FIELDS)
        fields = payload.model_dump(exclude_unset=True, exclude_none=True)
        if "clo_code" in fields:
            fields["clo_code"] = self._ensure_child_code_available(
                subject,
                "learning_outcomes",
                "clo_code",
                fields["clo_code"],
                current_child_id=clo_oid,
            )
        if not fields:
            return _subject_response(subject, self._usage_counts(subject), viewer)
        now = utc_now()
        if self.subject_repo:
            outcome = {"_id": clo_oid, **fields, "updated_at": now}
            record = self.subject_repo.save_child(subject["_id"], "clo", outcome, viewer,
                                                  create=False)
        else:
            update_fields = {
                f"learning_outcomes.$.{key}": value
                for key, value in fields.items()
            }
            update_fields["learning_outcomes.$.updated_at"] = now
            update_fields["updated_at"] = now
            record = self.db.subjects.find_one_and_update(
                {"_id": subject["_id"], "learning_outcomes._id": clo_oid},
                {"$set": update_fields},
                return_document=ReturnDocument.AFTER,
            )
        if not record:
            raise LookupError("Không tìm thấy CLO")
        updated_clo = _child(record, "learning_outcomes", clo_oid)
        if not self.subject_repo:
            self._audit("catalog.clo_update", "clo", updated_clo, viewer, CLO_AUDIT_FIELDS,
                        before=previous,
                        label=updated_clo["clo_code"], metadata={"subject_id": str(subject["_id"])})
        return _subject_response(record, self._usage_counts(record), viewer)

    def list_ai_models(self) -> list[dict]:
        if self.ai_repo:
            return [json_safe({**item, "factory_status": self._model_factory_status(item["model_code"])})
                    for item in self.ai_repo.models()]
        return [
            json_safe({**item, "factory_status": self._model_factory_status(item["model_code"])})
            for item in self.db.ai_models.find().sort("priority", 1)
        ]

    def _model_factory_status(self, model_code: str) -> dict:
        try:
            snapshot = resolve_model_snapshot(
                model_code, database=None if self.ai_repo else self.db
            )
            provider = get_llm_service(model_code, model_snapshot=snapshot)
            runtime = {
                "provider_class": provider.__class__.__name__,
                "model_name": getattr(provider, "model_name", None),
                "url": getattr(provider, "url", None),
                "resolved": snapshot,
            }
            return {"supported": True, "runtime": runtime}
        except Exception as exc:
            return {"supported": False, "error": str(exc)}

    def _catalog_model(self, model_code: str) -> dict | None:
        if self.ai_repo:
            return self.ai_repo.model(model_code)
        return self.db.ai_models.find_one({"model_code": model_code})

    def available_ai_models(self, capability: str) -> dict:
        normalized = capability.strip().upper()
        if normalized not in {GENERATION_CAPABILITY, EVALUATION_CAPABILITY}:
            raise ValueError("Tác vụ model không hợp lệ")
        default_code = (
            settings.model_provider
            if normalized == GENERATION_CAPABILITY
            else settings.evaluation_model_provider
        )
        if self.ai_repo:
            return available_model_options(
                None, capability=normalized, default_code=default_code
            )
        return available_model_options(
            self.db,
            capability=normalized,
            default_code=default_code,
        )

    def runtime_config(self) -> dict:
        generation_code = settings.model_provider
        evaluation_code = settings.evaluation_model_provider
        active_prompt_count = (
            self.ai_repo.active_prompt_count() if self.ai_repo else
            self.db.prompt_templates.count_documents({"is_active": True})
        )
        active_policy = (
            self.ai_repo.policy(active_only=True) if self.ai_repo else
            self.db.evaluation_policies.find_one(
                {"is_active": True}, sort=[("version", -1)]
            )
        )
        warnings: list[str] = []
        if settings.prompt_source != "db" and active_prompt_count:
            warnings.append(
                "PROMPT_SOURCE đang là file nên các prompt active trong DB chưa tác động runtime."
            )
        if settings.prompt_source == "db" and not active_prompt_count:
            warnings.append("PROMPT_SOURCE=db nhưng chưa có prompt active trong DB; runtime sẽ fallback file.")
        generation_catalog_model = self._catalog_model(generation_code)
        evaluation_catalog_model = self._catalog_model(evaluation_code)
        if not generation_catalog_model:
            warnings.append(f"Model sinh câu hỏi '{generation_code}' chưa có trong catalog DB.")
        elif generation_catalog_model.get("is_active") is False:
            warnings.append(f"Model sinh câu hỏi '{generation_code}' đang inactive trong catalog DB.")
        if not evaluation_catalog_model:
            warnings.append(f"Model đánh giá '{evaluation_code}' chưa có trong catalog DB.")
        elif evaluation_catalog_model.get("is_active") is False:
            warnings.append(f"Model đánh giá '{evaluation_code}' đang inactive trong catalog DB.")

        return json_safe(
            {
                "app_env": settings.app_env,
                "prompt_source": settings.prompt_source,
                "prompts_dir": settings.prompts_dir,
                "generation_model_provider": generation_code,
                "evaluation_model_provider": evaluation_code,
                "ollama_generate_url": settings.ollama_generate_url,
                "ollama_timeout_seconds": settings.ollama_timeout_seconds,
                "ollama_num_predict": settings.ollama_num_predict,
                "ollama_temperature": settings.ollama_temperature,
                "qwen3_model_name": "qwen3:8b",
                "deepseek_model_name": settings.deepseek_model_name,
                "deepseek_timeout_seconds": settings.deepseek_timeout_seconds,
                "gemini_default_model": settings.gemini_model_name,
                "gemini_api_key_configured": bool(settings.gemini_api_key),
                "generation_factory": self._model_factory_status(generation_code),
                "evaluation_factory": self._model_factory_status(evaluation_code),
                "generation_catalog_model": generation_catalog_model,
                "evaluation_catalog_model": evaluation_catalog_model,
                "active_prompt_count": active_prompt_count,
                "active_evaluation_policy": active_policy or FALLBACK_EVALUATION_POLICY,
                "warnings": warnings,
                "supported_provider_patterns": [
                    "qwen3-8b",
                    "gemini",
                    "deepseek",
                    "deepseek-r1",
                    "ollama:<model-name>",
                    "gemini:<model-name>",
                ],
            }
        )

    def save_ai_model(self, payload: AiModelPayload, *, create: bool, viewer: Any = None) -> dict:
        if self.ai_repo:
            try:
                record = self.ai_repo.save_model(payload, create=create, actor_id=getattr(viewer, "id", None))
            except FileExistsError as exc:
                raise CatalogConflictError("Mã mô hình đã tồn tại") from exc
            return json_safe({**record, "factory_status": self._model_factory_status(record["model_code"])})
        now = utc_now()
        values = {"schema_version": SCHEMA_VERSION, **payload.model_dump(), "updated_at": now}
        query = {"model_code": payload.model_code}
        previous = _audit_snapshot(self.db.ai_models.find_one(query), MODEL_AUDIT_FIELDS)
        if create:
            try:
                result = self.db.ai_models.update_one(
                    query,
                    {"$setOnInsert": {"_id": ObjectId(), **values, "created_at": now}},
                    upsert=True,
                )
            except DuplicateKeyError as exc:
                raise CatalogConflictError("Mã mô hình đã tồn tại") from exc
            if result.upserted_id is None:
                raise CatalogConflictError("Mã mô hình đã tồn tại")
            record = self.db.ai_models.find_one({"_id": result.upserted_id})
        else:
            record = self.db.ai_models.find_one_and_update(
                query,
                {"$set": values},
                return_document=ReturnDocument.AFTER,
            )
            if not record:
                raise LookupError("Không tìm thấy mô hình")
        self._audit("catalog.ai_model_create" if create else "catalog.ai_model_update",
                    "ai_model", record, viewer, MODEL_AUDIT_FIELDS,
                    before=previous, label=record["model_code"])
        return json_safe({**record, "factory_status": self._model_factory_status(record["model_code"])})

    def set_ai_model_active(self, payload: AiModelActivationPayload, viewer: Any = None) -> dict:
        if self.ai_repo:
            record = self.ai_repo.activate_model(payload.model_code, payload.is_active,
                                                 actor_id=getattr(viewer, "id", None))
            return json_safe({**record, "factory_status": self._model_factory_status(record["model_code"])})
        now = utc_now()
        previous = _audit_snapshot(self.db.ai_models.find_one({"model_code": payload.model_code}), MODEL_AUDIT_FIELDS)
        record = self.db.ai_models.find_one_and_update(
            {"model_code": payload.model_code},
            {"$set": {"is_active": payload.is_active, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not record:
            raise LookupError("Không tìm thấy model")
        self._audit("catalog.ai_model_activate" if payload.is_active else "catalog.ai_model_deactivate",
                    "ai_model", record, viewer, MODEL_AUDIT_FIELDS,
                    before=previous, label=record["model_code"])
        return json_safe({**record, "factory_status": self._model_factory_status(record["model_code"])})

    def list_ai_model_versions(self, model_code: str) -> list[dict]:
        if not self.ai_repo:
            raise ValueError("Lịch sử phiên bản model chỉ có khi AI_CONFIG_STORE=postgres")
        return [json_safe(row) for row in self.ai_repo.model_versions(model_code)]

    def activate_ai_model_version(self, payload: AiModelVersionActivationPayload, *, actor_id=None) -> dict:
        if not self.ai_repo:
            raise ValueError("Rollback phiên bản model chỉ có khi AI_CONFIG_STORE=postgres")
        record = self.ai_repo.activate_model_version(
            payload.model_code, payload.version, actor_id=actor_id
        )
        return json_safe({**record, "factory_status": self._model_factory_status(record["model_code"])})

    async def check_ai_model_health(self, payload: AiModelHealthCheckPayload) -> dict:
        started = time.perf_counter()
        factory_status = self._model_factory_status(payload.model_code)
        if not factory_status["supported"]:
            snapshot = {
                "status": "UNSUPPORTED",
                "latency_ms": 0,
                "response_preview": "",
                "error": factory_status.get("error"),
                "checked_at": utc_now(),
            }
            if self.ai_repo and self._catalog_model(payload.model_code):
                self.ai_repo.update_health(payload.model_code, json_safe(snapshot))
            elif self._catalog_model(payload.model_code):
                self.db.ai_models.find_one_and_update(
                    {"model_code": payload.model_code},
                    {"$set": {"last_health_check": snapshot, "updated_at": snapshot["checked_at"]}},
                    return_document=ReturnDocument.AFTER,
                )
            return json_safe({
                "model_code": payload.model_code,
                "factory_status": factory_status,
                **snapshot,
            })
        try:
            snapshot = resolve_model_snapshot(
                payload.model_code, database=None if self.ai_repo else self.db
            )
            provider = get_llm_service(payload.model_code, model_snapshot=snapshot)
            response_text = await asyncio.wait_for(
                provider.generate_text(payload.prompt),
                timeout=payload.timeout_seconds,
            )
            status = "OK"
            error = None
        except asyncio.TimeoutError:
            response_text = ""
            status = "FAILED"
            error = f"Quá thời gian chờ {payload.timeout_seconds:g} giây"
        except Exception as exc:
            response_text = ""
            status = "FAILED"
            error = str(exc)
        latency_ms = round((time.perf_counter() - started) * 1000)
        snapshot = {
            "status": status,
            "latency_ms": latency_ms,
            "response_preview": response_text[:500],
            "error": error,
            "checked_at": utc_now(),
        }
        if self.ai_repo and self._catalog_model(payload.model_code):
            self.ai_repo.update_health(payload.model_code, json_safe(snapshot))
        elif self._catalog_model(payload.model_code):
            self.db.ai_models.find_one_and_update(
                {"model_code": payload.model_code},
                {"$set": {"last_health_check": snapshot, "updated_at": snapshot["checked_at"]}},
                return_document=ReturnDocument.AFTER,
            )
        return json_safe(
            {
                "model_code": payload.model_code,
                "factory_status": factory_status,
                **snapshot,
            }
        )

    def list_prompt_templates(self) -> list[dict]:
        if self.ai_repo:
            return [json_safe(item) for item in self.ai_repo.prompts()]
        cursor = self.db.prompt_templates.find().sort([("template_key", 1), ("version", -1)])
        return [json_safe(item) for item in cursor]

    def save_prompt_template(self, payload: PromptTemplatePayload, viewer: Any = None) -> dict:
        if self.ai_repo:
            return json_safe(self.ai_repo.save_prompt(payload, actor_id=getattr(viewer, "id", None)))
        now = utc_now()
        latest = self.db.prompt_templates.find_one(
            {"template_key": payload.template_key},
            sort=[("version", -1)],
        )
        version = int(latest.get("version", 0)) + 1 if latest and payload.create_new_version else int((latest or {}).get("version", 1))
        query = {"template_key": payload.template_key, "version": version}
        previous = _audit_snapshot(self.db.prompt_templates.find_one(query), PROMPT_AUDIT_FIELDS)
        old_active = [
            {"_id": item["_id"], **_audit_snapshot(item, PROMPT_AUDIT_FIELDS)}
            for item in self.db.prompt_templates.find({"template_key": payload.template_key, "is_active": True})
        ] if payload.is_active else []
        if payload.is_active:
            self.db.prompt_templates.update_many(
                {"template_key": payload.template_key},
                {"$set": {"is_active": False, "updated_at": now}},
            )
        body_hash = hashlib.sha256(payload.prompt_body.encode("utf-8")).hexdigest()
        record = self.db.prompt_templates.find_one_and_update(
            query,
            {
                "$set": {
                    "schema_version": SCHEMA_VERSION,
                    "template_key": payload.template_key,
                    "version": version,
                    "kind": payload.kind,
                    "name": payload.name,
                    "prompt_body": payload.prompt_body,
                    "content_hash": body_hash,
                    "is_active": payload.is_active,
                    "updated_at": now,
                },
                "$setOnInsert": {"_id": ObjectId(), "created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        self._audit_deactivated_versions(old_active, record["_id"], viewer,
                                         entity_type="prompt_template", fields=PROMPT_AUDIT_FIELDS,
                                         action="catalog.prompt_deactivate", name_key="template_key")
        self._audit("catalog.prompt_create" if not previous else "catalog.prompt_update",
                    "prompt_template", record, viewer, PROMPT_AUDIT_FIELDS,
                    before=previous, label=f'{record["template_key"]} v{record["version"]}')
        return json_safe(record)

    def activate_prompt_template(self, payload: PromptTemplateActivationPayload, viewer: Any = None) -> dict:
        if self.ai_repo:
            return json_safe(self.ai_repo.activate_prompt(
                payload.template_key, payload.version, payload.is_active,
                actor_id=getattr(viewer, "id", None),
            ))
        now = utc_now()
        query = {"template_key": payload.template_key, "version": payload.version}
        target = self.db.prompt_templates.find_one(query)
        if not target:
            raise LookupError("Không tìm thấy prompt version")
        previous = _audit_snapshot(target, PROMPT_AUDIT_FIELDS)
        old_active = [
            {"_id": item["_id"], **_audit_snapshot(item, PROMPT_AUDIT_FIELDS)}
            for item in self.db.prompt_templates.find({"template_key": payload.template_key, "is_active": True})
        ] if payload.is_active else []
        if payload.is_active:
            self.db.prompt_templates.update_many(
                {"template_key": payload.template_key},
                {"$set": {"is_active": False, "updated_at": now}},
            )
        record = self.db.prompt_templates.find_one_and_update(
            query,
            {"$set": {"is_active": payload.is_active, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        self._audit_deactivated_versions(old_active, record["_id"], viewer,
                                         entity_type="prompt_template", fields=PROMPT_AUDIT_FIELDS,
                                         action="catalog.prompt_deactivate", name_key="template_key")
        self._audit("catalog.prompt_activate" if payload.is_active else "catalog.prompt_deactivate",
                    "prompt_template", record, viewer, PROMPT_AUDIT_FIELDS,
                    before=previous, label=f'{record["template_key"]} v{record["version"]}')
        return json_safe(record)

    def test_prompt_template(self, payload: PromptTemplateTestPayload) -> dict:
        if bool(payload.template_key) != bool(payload.version):
            raise ValueError("Chọn cả mã prompt và phiên bản để chạy thử")
        selected = None
        if payload.template_key:
            selected = self.db.prompt_templates.find_one(
                {"template_key": payload.template_key, "version": payload.version}
            )
            if not selected:
                raise LookupError("Không tìm thấy phiên bản prompt")
        selected_key = selected["template_key"] if selected else None
        bloom_level = payload.bloom_level
        question_type = payload.question_type
        if selected_key and selected_key.startswith("bloom:"):
            bloom_level = selected_key.split(":", 1)[1]
        if selected_key and selected_key.startswith(("question_type:", "question_structure:")):
            question_type = selected_key.split(":", 1)[1]
        template_keys = [
            "system",
            "question_rule",
            f"bloom:{bloom_level}",
            "quy_dinh_do_kho",
            f"question_type:{question_type}",
            f"question_structure:{question_type}",
            "output_format",
        ]
        effective_sources = {}
        for key in template_keys:
            active_db = (
                self.ai_repo.prompt(key, active_only=True) if self.ai_repo else
                self.db.prompt_templates.find_one(
                    {"template_key": key, "is_active": True}, sort=[("version", -1)]
                )
            )
            effective_sources[key] = (
                "db"
                if settings.prompt_source == "db" and active_db
                else "file"
            )
        preview_mode = "assembled" if not selected_key or selected_key in template_keys else "template"
        try:
            rendered = (
                PromptBuilder().build(
                    context=payload.context,
                    bloom_level=bloom_level,
                    question_type=question_type,
                    num_questions=payload.num_questions,
                    instruction=payload.instruction,
                    template_overrides={selected_key: selected["prompt_body"]} if selected else None,
                )
                if preview_mode == "assembled" else selected["prompt_body"]
            )
        except Exception as exc:
            raise ValueError(f"Không build được prompt mẫu: {exc}") from exc
        warnings = []
        if settings.prompt_source != "db":
            warnings.append("Prompt DB chưa có hiệu lực vì PROMPT_SOURCE không phải db.")
        if selected:
            effective_sources[selected_key] = "selected"
        if preview_mode == "template":
            warnings.append("Mẫu này không nằm trong prompt sinh câu hỏi; đang xem nội dung phiên bản đã chọn.")
        return {
            "prompt_source": settings.prompt_source,
            "effective_sources": effective_sources,
            "rendered_prompt": rendered,
            "length": len(rendered),
            "warnings": warnings,
            "preview_mode": preview_mode,
            "template_key": selected_key,
            "version": selected.get("version") if selected else None,
        }

    def list_evaluation_policies(self) -> list[dict]:
        if self.ai_repo:
            return [json_safe(item) for item in self.ai_repo.policies()]
        cursor = self.db.evaluation_policies.find().sort([("policy_name", 1), ("version", -1)])
        return [json_safe(item) for item in cursor]

    def save_evaluation_policy(self, payload: EvaluationPolicyPayload, viewer: Any = None) -> dict:
        if self.ai_repo:
            return json_safe(self.ai_repo.save_policy(payload, actor_id=getattr(viewer, "id", None)))
        now = utc_now()
        latest = self.db.evaluation_policies.find_one(
            {"policy_name": payload.policy_name},
            sort=[("version", -1)],
        )
        version = int(latest.get("version", 0)) + 1 if latest and payload.create_new_version else int((latest or {}).get("version", 1))
        query = {"policy_name": payload.policy_name, "version": version}
        previous = _audit_snapshot(self.db.evaluation_policies.find_one(query), POLICY_AUDIT_FIELDS)
        old_active = [
            {"_id": item["_id"], **_audit_snapshot(item, POLICY_AUDIT_FIELDS)}
            for item in self.db.evaluation_policies.find({"policy_name": payload.policy_name, "is_active": True})
        ] if payload.is_active else []
        if payload.is_active:
            self.db.evaluation_policies.update_many(
                {"policy_name": payload.policy_name},
                {"$set": {"is_active": False}},
            )
        weights_hash = hashlib.sha256(str(sorted(payload.weights.items())).encode()).hexdigest()
        record = self.db.evaluation_policies.find_one_and_update(
            query,
            {
                "$set": {
                    "schema_version": SCHEMA_VERSION,
                    "policy_name": payload.policy_name,
                    "version": version,
                    "weights": payload.weights,
                    "weights_hash": weights_hash,
                    "thresholds": payload.thresholds,
                    "is_active": payload.is_active,
                    "updated_at": now,
                },
                "$setOnInsert": {"_id": ObjectId(), "created_at": now},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        self._audit_deactivated_versions(old_active, record["_id"], viewer,
                                         entity_type="evaluation_policy", fields=POLICY_AUDIT_FIELDS,
                                         action="catalog.evaluation_policy_deactivate", name_key="policy_name")
        self._audit("catalog.evaluation_policy_create" if not previous else "catalog.evaluation_policy_update",
                    "evaluation_policy", record, viewer, POLICY_AUDIT_FIELDS,
                    before=previous, label=f'{record["policy_name"]} v{record["version"]}')
        return json_safe(record)

    def activate_evaluation_policy(self, payload: EvaluationPolicyActivationPayload, viewer: Any = None) -> dict:
        if self.ai_repo:
            return json_safe(self.ai_repo.activate_policy(
                payload.policy_name, payload.version, payload.is_active,
                actor_id=getattr(viewer, "id", None),
            ))
        now = utc_now()
        query = {"policy_name": payload.policy_name, "version": payload.version}
        target = self.db.evaluation_policies.find_one(query)
        if not target:
            raise LookupError("Không tìm thấy policy version")
        previous = _audit_snapshot(target, POLICY_AUDIT_FIELDS)
        old_active = [
            {"_id": item["_id"], **_audit_snapshot(item, POLICY_AUDIT_FIELDS)}
            for item in self.db.evaluation_policies.find({"policy_name": payload.policy_name, "is_active": True})
        ] if payload.is_active else []
        if payload.is_active:
            self.db.evaluation_policies.update_many(
                {"policy_name": payload.policy_name},
                {"$set": {"is_active": False, "updated_at": now}},
            )
        record = self.db.evaluation_policies.find_one_and_update(
            query,
            {"$set": {"is_active": payload.is_active, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        self._audit_deactivated_versions(old_active, record["_id"], viewer,
                                         entity_type="evaluation_policy", fields=POLICY_AUDIT_FIELDS,
                                         action="catalog.evaluation_policy_deactivate", name_key="policy_name")
        self._audit("catalog.evaluation_policy_activate" if payload.is_active else "catalog.evaluation_policy_deactivate",
                    "evaluation_policy", record, viewer, POLICY_AUDIT_FIELDS,
                    before=previous, label=f'{record["policy_name"]} v{record["version"]}')
        return json_safe(record)

    def overview(self, viewer: Any = None) -> dict[str, Any]:
        return {
            "subjects": self.list_subjects(viewer),
            "ai_models": self.list_ai_models(),
            "prompt_templates": self.list_prompt_templates(),
            "evaluation_policies": self.list_evaluation_policies(),
            "runtime_config": self.runtime_config(),
        }


def get_catalog_service() -> CatalogService:
    return CatalogService(get_database())
