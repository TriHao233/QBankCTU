from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.database import Database

from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import get_database
from core.dependencies import effective_permissions
from modules.users.store import review_candidate_users
from modules.notifications.postgres_repository import PostgresNotificationRepository
from modules.questions.repository import json_safe, object_id

logger = logging.getLogger(__name__)
# Decisions after which a resubmission should reach the reviewer who made them.
RESUBMITTABLE_DECISIONS = {"NEEDS_REVISION", "REJECTED"}


def submission_review_recipients(users: list[dict], *, question: dict, version: dict, actor_user_id) -> list:
    """Only notify active subject specialists about first-time submissions."""
    subject = (version.get("classification") or {}).get("subject") or {}
    subject_id = str(question.get("subject_id") or (subject.get("id") if isinstance(subject, dict) else subject) or "")
    if not subject_id:
        return []
    excluded = {str(actor_user_id), str(question.get("created_by_user_id")), str(version.get("created_by_user_id"))}
    excluded.update(str(item) for item in question.get("author_user_ids") or [])
    return list(dict.fromkeys(user["_id"] for user in users
        if user.get("is_active", True) and user.get("role") != "Admin"
        and str(user["_id"]) not in excluded and "reviews.manage" in effective_permissions(user)
        and subject_id in {str(item) for item in user.get("review_subject_ids") or []}))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(record: dict) -> dict:
    return json_safe(
        {
            "id": record["_id"],
            "type": record["type"],
            "title": record["title"],
            "body": record.get("body", ""),
            "link": record.get("link", ""),
            "entity": record.get("entity") or {},
            "actor_user_id": record.get("actor_user_id"),
            "is_read": record.get("is_read", False),
            "read_at": record.get("read_at"),
            "created_at": record["created_at"],
        }
    )


class NotificationService:
    def __init__(self, database: Database, *, sink: list[dict] | None = None):
        self.db = database
        self.repo = PostgresNotificationRepository() if settings.notification_store == "postgres" else None
        # With a sink, notifications are only built; the caller writes them in
        # the same PostgreSQL transaction as the change they report.
        self.sink = sink

    def create(
        self,
        *,
        recipient_user_id: str | ObjectId | None,
        type: str,
        title: str,
        body: str = "",
        link: str = "",
        entity: dict[str, Any] | None = None,
        actor_user_id: str | ObjectId | None = None,
    ) -> dict | None:
        if recipient_user_id is None:
            return None
        now = utc_now()
        record = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "recipient_user_id": object_id(recipient_user_id, "recipient_user_id"),
            "actor_user_id": object_id(actor_user_id, "actor_user_id")
            if actor_user_id is not None
            else None,
            "type": type,
            "title": title,
            "body": body,
            "link": link,
            "entity": entity or {},
            "is_read": False,
            "read_at": None,
            "created_at": now,
        }
        if self.sink is not None:
            self.sink.append(record)
        elif self.repo:
            record = self.repo.create(record)
        else:
            self.db.notifications.insert_one(record)
        return _serialize(record)

    def create_many(self, notifications: list[dict[str, Any]]) -> list[dict]:
        created = []
        seen: set[str] = set()
        for payload in notifications:
            recipient = payload.get("recipient_user_id")
            key = str(recipient)
            if not recipient or key in seen:
                continue
            seen.add(key)
            item = self.create(**payload)
            if item:
                created.append(item)
        return created

    def list(self, current_user, page: int, page_size: int, unread_only: bool = False) -> dict:
        if self.repo:
            items, total = self.repo.list(current_user.id, page, page_size, unread_only=unread_only)
            return {
                "items": [_serialize(item) for item in items],
                "total": total,
                "page": page,
                "page_size": page_size,
            }
        query: dict = {"recipient_user_id": current_user.id}
        if unread_only:
            query["is_read"] = False
        total = self.db.notifications.count_documents(query)
        items = list(
            self.db.notifications.find(query)
            .sort("created_at", -1)
            .skip((page - 1) * page_size)
            .limit(page_size)
        )
        return {
            "items": [_serialize(item) for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def unread_count(self, current_user) -> int:
        if self.repo:
            return self.repo.unread_count(current_user.id)
        return self.db.notifications.count_documents(
            {"recipient_user_id": current_user.id, "is_read": False}
        )

    def mark_read(self, notification_id: str, current_user) -> dict | None:
        now = utc_now()
        if self.repo:
            record = self.repo.mark_read(object_id(notification_id, "notification_id"),
                                         current_user.id, now)
            return _serialize(record) if record else None
        record = self.db.notifications.find_one_and_update(
            {
                "_id": object_id(notification_id, "notification_id"),
                "recipient_user_id": current_user.id,
            },
            {"$set": {"is_read": True, "read_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        return _serialize(record) if record else None

    def mark_all_read(self, current_user) -> int:
        now = utc_now()
        if self.repo:
            return self.repo.mark_all_read(current_user.id, now)
        result = self.db.notifications.update_many(
            {"recipient_user_id": current_user.id, "is_read": False},
            {"$set": {"is_read": True, "read_at": now}},
        )
        return result.modified_count

    @staticmethod
    def _question_owner(question: dict, version: dict) -> ObjectId | None:
        return question.get("created_by_user_id") or version.get("created_by_user_id")

    @staticmethod
    def _question_entity(question: dict, version: dict) -> dict:
        return json_safe(
            {
                "type": "QUESTION",
                "id": question["_id"],
                "version_id": version["_id"],
                "question_code": question.get("question_code"),
            }
        )

    def notify_review_decision(
        self,
        *,
        question: dict,
        version: dict,
        review: dict,
        actor_user_id: ObjectId,
    ) -> dict | None:
        """Notify the owner and, when someone else submitted it (a shared editor), the submitter too."""
        owner = self._question_owner(question, version)
        submitter = (question.get("review_submission") or {}).get("submitted_by_user_id")
        by_key = {}
        for item in (owner, submitter):
            if item is not None and str(item) != str(actor_user_id):
                by_key.setdefault(str(item), item)
        recipients = list(by_key.values())
        created = [self._review_decision_notice(recipient, question, version, review, actor_user_id)
                   for recipient in recipients]
        return next((item for item in created if item), None)

    def _review_decision_notice(self, recipient, question: dict, version: dict, review: dict,
                                actor_user_id: ObjectId) -> dict | None:
        decision = review.get("decision") or "PENDING"
        label = {
            "APPROVED": "đã được duyệt",
            "NEEDS_REVISION": "cần chỉnh sửa",
            "REJECTED": "bị từ chối",
        }.get(decision, decision)
        question_code = question.get("question_code", "Câu hỏi")
        return self.create(
            recipient_user_id=recipient,
            actor_user_id=actor_user_id,
            type=f"QUESTION_{decision}",
            title=f"{question_code} {label}",
            body=review.get("note") or "Reviewer đã cập nhật trạng thái kiểm duyệt.",
            link=f"/quan-ly?questionId={question['_id']}",
            entity=self._question_entity(question, version),
        )

    def notify_secondary_review_pending(
        self,
        *,
        question: dict,
        version: dict,
        reason: str,
        actor_user_id: ObjectId,
    ) -> dict | None:
        recipient = self._question_owner(question, version)
        if recipient is None or recipient == actor_user_id:
            return None
        question_code = question.get("question_code", "Câu hỏi")
        return self.create(
            recipient_user_id=recipient,
            actor_user_id=actor_user_id,
            type="QUESTION_SECONDARY_REVIEW_PENDING",
            title=f"{question_code} đạt lần duyệt đầu, đang chờ duyệt lần 2",
            body=reason or "Câu hỏi cần thêm một người duyệt xác nhận trước khi vào ngân hàng.",
            link=f"/quan-ly?questionId={question['_id']}",
            entity=self._question_entity(question, version),
        )

    def notify_secondary_review_available(self, *, question, version, primary_reviewer_user_id, actor_user_id, recipients) -> list[dict]:
        code = question.get("question_code") or "Câu hỏi"
        excluded = {str(primary_reviewer_user_id), str(question.get("created_by_user_id")), str(version.get("created_by_user_id"))}
        return self.create_many([{
            "recipient_user_id": recipient, "actor_user_id": actor_user_id,
            "type": "QUESTION_SECONDARY_REVIEW_AVAILABLE", "title": f"{code} cần duyệt lần 2",
            "body": "Câu hỏi cần một người duyệt khác xác nhận trước khi vào ngân hàng.",
            "link": f"/kiem-duyet?questionId={question['_id']}", "entity": self._question_entity(question, version),
        } for recipient in dict.fromkeys(recipients) if str(recipient) not in excluded])

    def notify_exam_owners_question_reopened(
        self,
        *,
        question_id: str | ObjectId,
        question_code: str,
        actor_user_id: ObjectId,
    ) -> list[dict]:
        """An approved question was edited and left review: warn owners of open exams using it."""
        question_oid = object_id(question_id, "question_id")
        if settings.exam_store == "postgres":
            from modules.exams.postgres_repository import PostgresExamRepository
            exams = PostgresExamRepository.open_exams_using_question(question_oid)
        else:
            exams = list(
                self.db.exams.find(
                    {
                        "questions.question_id": question_oid,
                        "status": {"$nin": ["FINALIZED", "PUBLISHED", "ARCHIVED", "finalized", "published", "archived"]},
                        "deleted_at": None,
                    },
                    {"name": 1, "created_by_user_id": 1},
                )
            )
        created = []
        for exam in exams:
            owner = exam.get("created_by_user_id")
            if not owner or owner == actor_user_id:
                continue
            item = self.create(
                recipient_user_id=owner,
                actor_user_id=actor_user_id,
                type="EXAM_QUESTION_NEEDS_REVIEW",
                title=f"{question_code} trong đề \"{exam.get('name', '')}\" đã bị sửa",
                body=("Câu hỏi đã duyệt vừa được chỉnh sửa. Đề vẫn dùng phiên bản đã duyệt; "
                      "bản sửa chỉ dùng được sau khi duyệt lại và chọn lại vào đề."),
                link=f"/lam-de-thi/{exam['_id']}",
                entity=json_safe({"type": "EXAM", "id": exam["_id"], "question_id": question_oid}),
            )
            if item:
                created.append(item)
        return created

    def notify_review_assigned(
        self,
        *,
        question: dict,
        version: dict,
        reviewer_user_id: str | ObjectId | None,
        actor_user_id: ObjectId,
    ) -> dict | None:
        if reviewer_user_id is None:
            return None
        if reviewer_user_id == actor_user_id:
            return None
        question_code = question.get("question_code", "Câu hỏi")
        return self.create(
            recipient_user_id=reviewer_user_id,
            actor_user_id=actor_user_id,
            type="QUESTION_REVIEW_ASSIGNED",
            title=f"{question_code} được phân công kiểm duyệt",
            body="Admin đã phân công câu hỏi này cho bạn.",
            link=f"/kiem-duyet?questionId={question['_id']}",
            entity=self._question_entity(question, version),
        )

    def notify_review_taken_over(
        self,
        *,
        question: dict,
        version: dict,
        reviewer_user_id: str | ObjectId | None,
        actor_user_id: ObjectId,
        reason: str,
        archived: bool = False,
    ) -> dict | None:
        """An admin took (or archived) a question the reviewer was holding."""
        if reviewer_user_id is None or reviewer_user_id == actor_user_id:
            return None
        question_code = question.get("question_code", "Câu hỏi")
        action = "lưu trữ" if archived else "tiếp nhận"
        return self.create(
            recipient_user_id=reviewer_user_id,
            actor_user_id=actor_user_id,
            type="QUESTION_REVIEW_TAKEN_OVER",
            title=f"{question_code} đã được Admin {action}",
            body=f"Câu hỏi bạn đang giữ để duyệt đã được Admin {action}. Lý do: {reason}",
            link=f"/kiem-duyet?questionId={question['_id']}",
            entity=self._question_entity(question, version),
        )

    def notify_pending_question_edited(
        self,
        *,
        question: dict,
        version: dict,
        actor_user_id: ObjectId,
    ) -> dict | None:
        """The author changed a question still waiting in the review queue."""
        reviewer_user_id = (question.get("review_assignment") or {}).get("reviewer_user_id")
        if reviewer_user_id is None or reviewer_user_id == actor_user_id:
            return None
        question_code = question.get("question_code", "Câu hỏi")
        return self.create(
            recipient_user_id=reviewer_user_id,
            actor_user_id=actor_user_id,
            type="QUESTION_EDITED_DURING_REVIEW",
            title=f"{question_code} vừa được tác giả cập nhật",
            body=f"Câu hỏi vẫn chờ bạn duyệt, nay ở phiên bản {version.get('version')}. Hãy đọc lại bản mới.",
            link=f"/kiem-duyet?questionId={question['_id']}",
            entity=self._question_entity(question, version),
        )

    def _question_with_latest_review(self, question_id) -> tuple[dict, dict, dict | None] | None:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            repository = PostgresQuestionRepository()
            pair = repository.find_pair(question_id)
            if not pair:
                return None
            return (*pair, repository.latest_review(pair[0]))
        question = self.db.questions.find_one(
            {
                "_id": object_id(question_id, "question_id"),
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
            }
        )
        if not question:
            return None
        version = self.db.question_versions.find_one({"_id": question["current_version_id"]})
        if not version:
            return None
        review = None
        if question.get("latest_review_id"):
            review = self.db.question_reviews.find_one({"_id": question["latest_review_id"]})
        if not review:
            review = self.db.question_reviews.find_one(
                {"question_id": question["_id"]},
                sort=[("reviewed_at", -1)],
            )
        return question, version, review

    def notify_question_submitted(self, *, question_id, previous_review_status, actor_user_id) -> list[dict]:
        if previous_review_status == "PENDING":
            return []
        found = self._question_with_latest_review(question_id)
        if not found:
            return []
        question, version, review = found
        if review:
            return self.notify_question_resubmitted(question_id=question_id,
                previous_review_status=previous_review_status, actor_user_id=actor_user_id)
        recipients = submission_review_recipients(review_candidate_users(self.db),
            question=question, version=version, actor_user_id=actor_user_id)
        code = question.get("question_code") or "Câu hỏi"
        return self.create_many([{
            "recipient_user_id": recipient, "actor_user_id": actor_user_id,
            "type": "QUESTION_SUBMITTED_FOR_REVIEW", "title": f"{code} mới được gửi duyệt",
            "body": "Có câu hỏi mới thuộc học phần bạn phụ trách cần kiểm duyệt.",
            "link": f"/kiem-duyet?questionId={question['_id']}",
            "entity": self._question_entity(question, version),
        } for recipient in recipients])

    def notify_question_resubmitted(
        self,
        *,
        question_id: str | ObjectId,
        previous_review_status: str | None,
        actor_user_id: ObjectId,
    ) -> list[dict]:
        # Editing a question moves it back to DRAFT before it is resubmitted, so
        # the status right before submission says nothing about earlier feedback.
        # Decide from the latest review decision instead.
        if previous_review_status == "PENDING":
            return []
        found = self._question_with_latest_review(question_id)
        if not found:
            return []
        question, version, review = found
        decision = review.get("decision") if review else None
        if decision not in RESUBMITTABLE_DECISIONS:
            return []
        reviewer_id = review.get("reviewer_user_id")
        if not reviewer_id or reviewer_id == actor_user_id:
            return []
        question_code = question.get("question_code", "Câu hỏi")
        after_rejection = decision == "REJECTED"
        return self.create_many(
            [
                {
                    "recipient_user_id": reviewer_id,
                    "actor_user_id": actor_user_id,
                    "type": "QUESTION_RESUBMITTED",
                    "title": (
                        f"{question_code} được sửa và gửi lại sau khi bị từ chối"
                        if after_rejection
                        else f"{question_code} đã được gửi lại"
                    ),
                    "body": (
                        "Giảng viên đã chỉnh sửa câu hỏi bạn từ chối trước đó và gửi duyệt lại."
                        if after_rejection
                        else "Giảng viên đã chỉnh sửa theo yêu cầu và gửi lại câu hỏi cần duyệt."
                    ),
                    "link": f"/kiem-duyet?questionId={question['_id']}",
                    "entity": self._question_entity(question, version),
                }
            ]
        )


def get_notification_service() -> NotificationService:
    return NotificationService(get_database())


def safe_notify_review_decision(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_review_decision(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify review decision: %s", exc)


def safe_notify_exam_owners_question_reopened(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_exam_owners_question_reopened(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify exam owners about an edited question: %s", exc)


def safe_notify_secondary_review_pending(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_secondary_review_pending(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify pending secondary review: %s", exc)


def safe_notify_review_assigned(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_review_assigned(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify review assignment: %s", exc)


def safe_notify_review_taken_over(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_review_taken_over(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify review takeover: %s", exc)


def safe_notify_question_submitted(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_question_submitted(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify question submission: %s", exc)


def safe_notify_question_resubmitted(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_question_resubmitted(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify question resubmission: %s", exc)


def safe_notify_secondary_review_available(**kwargs) -> None:
    try:
        NotificationService(kwargs.pop("database")).notify_secondary_review_available(**kwargs)
    except Exception as exc:
        logger.warning("Failed to notify available secondary review: %s", exc)
