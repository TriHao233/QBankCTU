from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Callable, Protocol

from bson import ObjectId
from firebase_admin import auth

from core.audit import record_audit_event
from core.config import settings
from core.database import get_rag_db
from core.dependencies import CurrentUser, effective_permissions, permission_overrides
from modules.auth.session_repository import (
    FirebaseSessionRepository,
    get_firebase_session_repository,
)
from modules.users import calendar_service
from modules.users.repository import MongoUserRepository, UserRepository, serialize_user
from modules.users.schemas import (
    PublicRegisterRequest,
    GenerationPresetPayload,
    TaskCalendarPayload,
    TaskCalendarUpdateRequest,
    UserAdminUpdateRequest,
    UserCreateRequest,
    UserImportRequest,
    UserInviteRequest,
    UserSelfUpdateRequest,
)

logger = logging.getLogger(__name__)

MAX_GENERATION_PRESETS = 12
MAX_TASK_CALENDAR_ITEMS = 500


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class IdentityGateway(Protocol):
    def create_user(self, *, email: str, password: str, display_name: str): ...

    def update_user(self, firebase_uid: str, **fields): ...

    def generate_password_reset_link(self, email: str) -> str: ...

    def set_user_disabled(self, firebase_uid: str, disabled: bool): ...

    def delete_user(self, firebase_uid: str): ...


class FirebaseIdentityGateway:
    def create_user(self, *, email: str, password: str, display_name: str):
        return auth.create_user(email=email, password=password, display_name=display_name)

    def update_user(self, firebase_uid: str, **fields):
        return auth.update_user(firebase_uid, **fields)

    def generate_password_reset_link(self, email: str) -> str:
        return auth.generate_password_reset_link(email)

    def set_user_disabled(self, firebase_uid: str, disabled: bool):
        return auth.update_user(firebase_uid, disabled=disabled)

    def delete_user(self, firebase_uid: str):
        return auth.delete_user(firebase_uid)


class UserService:
    def __init__(
        self,
        repository: UserRepository,
        identity: IdentityGateway,
        sessions: FirebaseSessionRepository,
        release_review_assignments: Callable[[ObjectId, CurrentUser | None, str], int] | None = None,
    ):
        self.repository = repository
        self.identity = identity
        self.sessions = sessions
        self.release_review_assignments = release_review_assignments

    def _release_reviewer_work(
        self,
        user_id: ObjectId,
        could_review: bool,
        actor: CurrentUser | None,
        reason: str,
    ) -> None:
        if self.release_review_assignments and could_review:
            self.release_review_assignments(user_id, actor, reason)

    def sync_from_claims(self, claims: dict) -> dict:
        known = self.repository.find_by_firebase_uid(claims["uid"]) is not None
        user = self.repository.sync_identity(claims, self_signup=True)
        if not known:
            self._notify_admins_pending(user)
        return serialize_user(user)

    def register_teacher(self, payload: PublicRegisterRequest) -> dict:
        firebase_user = self.identity.create_user(
            email=str(payload.email),
            password=payload.password,
            display_name=payload.full_name,
        )
        user = None
        try:
            user = self.repository.sync_identity(
                {
                    "uid": firebase_user.uid,
                    "email": str(payload.email),
                    "name": payload.full_name,
                },
                self_signup=True,
            )
            self.sessions.upsert(firebase_user.uid, None)
        except Exception:
            if user:
                self.repository.delete_by_id(user["_id"])
            self.identity.delete_user(firebase_user.uid)
            raise
        self._notify_admins_pending(user)
        return serialize_user(user)

    def _notify_admins_pending(self, user: dict) -> None:
        if (user.get("approval_status") or "APPROVED") != "PENDING":
            return
        try:
            from modules.notifications.service import get_notification_service

            admins, _total = self.repository.list(1, 100, "Admin", None)
            get_notification_service().create_many([
                {
                    "recipient_user_id": admin["_id"],
                    "type": "user.pending_approval",
                    "title": "Tài khoản mới chờ kích hoạt",
                    "body": f"{user.get('display_name') or user.get('email')} ({user.get('email')}) vừa đăng ký.",
                    "link": "/quan-ly-nguoi-dung?approval=PENDING",
                    "entity": {"user_id": str(user["_id"])},
                }
                for admin in admins
                if admin.get("is_active", True)
            ])
        except Exception as exc:
            logger.warning("Failed to notify admins about pending user %s: %s", user.get("email"), exc)

    def create_user(self, payload: UserCreateRequest) -> dict:
        overrides = permission_overrides(
            payload.role.value, self._normalize_permissions(payload.permissions) or None
        )
        self._ensure_email_available(str(payload.email))
        firebase_user = self.identity.create_user(
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
        )
        user = None
        try:
            user = self.repository.create(
                {
                    "firebase_uid": firebase_user.uid,
                    "email": str(payload.email),
                    "display_name": payload.display_name,
                    "role": payload.role.value,
                    "profile": payload.profile.model_dump(),
                    **overrides,
                }
            )
            self.sessions.upsert(firebase_user.uid, None)
        except Exception:
            if user:
                self.repository.delete_by_id(user["_id"])
            self.identity.delete_user(firebase_user.uid)
            raise
        return serialize_user(user)

    def _ensure_email_available(self, email: str) -> None:
        # Kiểm tra trước khi tạo tài khoản Firebase để không tạo rồi phải xoá lại.
        if self.repository.find_by_email(email):
            raise ValueError(f"Email {email} đã có tài khoản trong hệ thống")

    @staticmethod
    def _normalize_object_ids(values: list[str] | None) -> list[ObjectId]:
        normalized: list[ObjectId] = []
        for value in values or []:
            text = str(value or "").strip()
            if not ObjectId.is_valid(text):
                raise ValueError("Mã học phần phụ trách không hợp lệ")
            oid = ObjectId(text)
            if oid not in normalized:
                normalized.append(oid)
        return normalized

    @staticmethod
    def _normalize_permissions(permissions: list[str] | None) -> list[str]:
        normalized = []
        seen = set()
        for permission in permissions or []:
            value = str(permission or "").strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    def invite_user(
        self,
        payload: UserInviteRequest,
        actor: CurrentUser | None = None,
    ) -> dict:
        overrides = permission_overrides(
            payload.role.value, self._normalize_permissions(payload.permissions) or None
        )
        self._ensure_email_available(str(payload.email))
        temporary_password = secrets.token_urlsafe(18)
        firebase_user = self.identity.create_user(
            email=str(payload.email),
            password=temporary_password,
            display_name=payload.display_name,
        )
        user = None
        try:
            user = self.repository.create(
                {
                    "firebase_uid": firebase_user.uid,
                    "email": str(payload.email),
                    "display_name": payload.display_name,
                    "role": payload.role.value,
                    "profile": payload.profile.model_dump(),
                    **overrides,
                }
            )
            self.sessions.upsert(firebase_user.uid, None)
            reset_link = self.identity.generate_password_reset_link(str(payload.email))
        except Exception:
            if user:
                self.repository.delete_by_id(user["_id"])
            self.identity.delete_user(firebase_user.uid)
            raise
        record_audit_event(
            action="user.invite",
            entity_type="user",
            entity_id=user["_id"],
            actor_user_id=actor.id if actor else None,
            actor_role=actor.role if actor else None,
            after=self._user_audit_snapshot(user),
            metadata={"email": str(payload.email), "role": payload.role.value, "label": str(payload.email)},
        )
        return {"user": serialize_user(user), "reset_link": reset_link}

    def reset_password(
        self,
        user_id: str,
        actor: CurrentUser | None = None,
    ) -> dict | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        reset_link = self.identity.generate_password_reset_link(user["email"])
        record_audit_event(
            action="user.password_reset",
            entity_type="user",
            entity_id=user["_id"],
            actor_user_id=actor.id if actor else None,
            actor_role=actor.role if actor else None,
            metadata={"email": user.get("email"), "label": user.get("email")},
        )
        return {
            "user_id": str(user["_id"]),
            "email": user["email"],
            "reset_link": reset_link,
        }

    def import_users(
        self,
        payload: UserImportRequest,
        actor: CurrentUser | None = None,
    ) -> dict:
        items = []
        for item in payload.users:
            try:
                invited = self.invite_user(item, actor)
                items.append(
                    {
                        "email": str(item.email),
                        "ok": True,
                        "user": invited["user"],
                        "reset_link": invited["reset_link"],
                    }
                )
            except Exception as exc:
                items.append({"email": str(item.email), "ok": False, "error": str(exc)})
        created = len([item for item in items if item["ok"]])
        return {"items": items, "created": created, "failed": len(items) - created}

    def get(self, user_id: str) -> dict | None:
        user = self.repository.find_by_id(user_id)
        return serialize_user(user) if user else None

    def get_by_firebase_uid(self, firebase_uid: str) -> dict | None:
        user = self.repository.find_by_firebase_uid(firebase_uid)
        return serialize_user(user) if user else None

    def list(
        self,
        page: int,
        page_size: int,
        role: str | None,
        search: str | None,
        approval_status: str | None = None,
    ) -> dict:
        filters = {"approval_status": approval_status} if approval_status else {}
        records, total = self.repository.list(page, page_size, role, search, **filters)
        return {
            "items": [serialize_user(item) for item in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_teacher_options(self, search: str | None = None) -> dict:
        records, total = self.repository.list(1, 100, "Teacher", search)
        return {
            "items": [
                {
                    "id": str(user["_id"]),
                    "email": user.get("email", ""),
                    "display_name": user.get("display_name", ""),
                    "is_active": user.get("is_active", True),
                }
                for user in records
            ],
            "total": total,
        }

    def list_reviewer_options(self, search: str | None = None) -> dict:
        # Người được giao duyệt = người thật sự có quyền kiểm duyệt (kể cả giảng viên được cấp thêm).
        all_records, _total = self.repository.list(1, 500, None, search)
        records = [
            user
            for user in all_records
            if user.get("is_active", True) and "reviews.manage" in effective_permissions(user)
        ]
        return {
            "items": [
                {
                    "id": str(user["_id"]),
                    "email": user.get("email", ""),
                    "display_name": user.get("display_name", ""),
                    "is_active": user.get("is_active", True),
                    "review_subject_ids": [str(item) for item in user.get("review_subject_ids") or []],
                }
                for user in records
            ],
            "total": len(records),
        }

    def update_self(self, user_id: str, payload: UserSelfUpdateRequest) -> dict | None:
        fields: dict = {}
        if payload.display_name is not None:
            fields["display_name"] = payload.display_name
        if payload.profile is not None:
            fields["profile"] = payload.profile.model_dump()
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        if payload.display_name is not None:
            self.identity.update_user(user["firebase_uid"], display_name=payload.display_name)
        updated = self.repository.update(user_id, fields)
        return serialize_user(updated) if updated else None

    @staticmethod
    def _generation_presets(user: dict) -> list[dict]:
        presets = user.get("generation_presets") or []
        return presets if isinstance(presets, list) else []

    def list_generation_presets(self, user_id: str) -> dict | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        return {"items": self._generation_presets(user)}

    def save_generation_preset(
        self,
        user_id: str,
        payload: GenerationPresetPayload,
    ) -> dict | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        now = utc_now()
        preset = {
            "id": str(ObjectId()),
            **payload.model_dump(),
            "createdAt": now,
            "updatedAt": now,
        }
        existing = self._generation_presets(user)
        next_presets = [preset, *existing][:MAX_GENERATION_PRESETS]
        updated = self.repository.update(
            user_id,
            {"generation_presets": next_presets},
        )
        if not updated:
            return None
        return preset

    def delete_generation_preset(self, user_id: str, preset_id: str) -> bool | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        existing = self._generation_presets(user)
        next_presets = [preset for preset in existing if preset.get("id") != preset_id]
        if len(next_presets) == len(existing):
            return False
        updated = self.repository.update(
            user_id,
            {"generation_presets": next_presets},
        )
        return updated is not None

    @staticmethod
    def _task_calendar(user: dict) -> list[dict]:
        tasks = user.get("task_calendar") or []
        return tasks if isinstance(tasks, list) else []

    def get_calendar(
        self,
        user_id: str,
        *,
        date_from=None,
        date_to=None,
        status: str | None = None,
        priority: str | None = None,
    ) -> dict | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        manual_tasks = self._task_calendar(user)
        documents = self.repository.get_calendar_documents(user_id)
        questions = self.repository.get_calendar_questions(user_id)
        document_ids = [document["_id"] for document in documents]
        document_ids_with_questions = self.repository.get_document_ids_with_questions(document_ids)
        return calendar_service.build_calendar(
            review_questions=self.repository.get_review_assignment_questions(user_id)
            if "reviews.manage" in effective_permissions(user) else (),
            review_backlog=self.repository.count_unassigned_pending() if user.get("role") == "Admin" else None,
            lock_timeout_minutes=settings.review_lock_timeout_minutes,
            assignment_timeout_hours=settings.review_assignment_timeout_hours,
            sla_hours=settings.review_sla_hours,
            manual_tasks=manual_tasks,
            documents=documents,
            questions=questions,
            document_ids_with_questions=document_ids_with_questions,
            date_from=date_from,
            date_to=date_to,
            status=status,
            priority=priority,
        )

    def create_task(self, user_id: str, payload: TaskCalendarPayload) -> dict | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        now = utc_now()
        task = {
            "id": str(ObjectId()),
            **payload.model_dump(),
            "status": "todo",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        existing = self._task_calendar(user)
        next_tasks = [task, *existing][:MAX_TASK_CALENDAR_ITEMS]
        updated = self.repository.update(user_id, {"task_calendar": next_tasks})
        if not updated:
            return None
        return task

    def update_task(
        self,
        user_id: str,
        task_id: str,
        payload: TaskCalendarUpdateRequest,
    ) -> dict | None | bool:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        existing = self._task_calendar(user)
        target = next((task for task in existing if task.get("id") == task_id), None)
        if target is None:
            return False
        fields = payload.model_dump(exclude_unset=True)
        now = utc_now()
        updated_task = {**target, **fields, "updated_at": now}
        if fields.get("status") == "done" and target.get("status") != "done":
            updated_task["completed_at"] = now
        elif fields.get("status") == "todo":
            updated_task["completed_at"] = None
        next_tasks = [updated_task if task.get("id") == task_id else task for task in existing]
        updated = self.repository.update(user_id, {"task_calendar": next_tasks})
        if not updated:
            return None
        return updated_task

    def delete_task(self, user_id: str, task_id: str) -> bool | None:
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        existing = self._task_calendar(user)
        next_tasks = [task for task in existing if task.get("id") != task_id]
        if len(next_tasks) == len(existing):
            return False
        updated = self.repository.update(user_id, {"task_calendar": next_tasks})
        return updated is not None

    def _ensure_admin_floor(self, user: dict, fields: dict) -> None:
        if user.get("role") != "Admin" or not user.get("is_active", True):
            return
        next_role = fields.get("role", user.get("role"))
        next_active = fields.get("is_active", user.get("is_active", True))
        if next_role == "Admin" and next_active is True:
            return
        if self.repository.count_active_admins() <= 1:
            raise ValueError("Hệ thống phải còn ít nhất một Admin active")

    @staticmethod
    def _user_audit_snapshot(user: dict | None) -> dict:
        if not user:
            return {}
        return {
            "role": user.get("role"),
            "is_active": user.get("is_active", True),
            "approval_status": user.get("approval_status") or "APPROVED",
            "permissions": list(effective_permissions(user)),
            "review_subject_ids": [str(item) for item in user.get("review_subject_ids") or []],
        }

    def update_admin(
        self,
        user_id: str,
        payload: UserAdminUpdateRequest,
        actor: CurrentUser | None = None,
    ) -> dict | None:
        fields = payload.model_dump(exclude_none=True)
        profile = fields.get("profile")
        if profile is not None and hasattr(profile, "model_dump"):
            fields["profile"] = profile.model_dump()
        role = fields.get("role")
        if role is not None:
            fields["role"] = role.value if hasattr(role, "value") else role
        if "review_subject_ids" in fields:
            fields["review_subject_ids"] = self._normalize_object_ids(fields["review_subject_ids"])
        user = self.repository.find_by_id(user_id)
        if not user:
            return None
        next_role = fields.get("role", user.get("role"))
        desired_permissions = fields.pop("permissions", None)
        role_changed = next_role != user.get("role")
        if desired_permissions is not None or role_changed:
            # Đổi vai trò mà không gửi quyền thì về đúng mặc định của vai trò mới.
            fields.update(permission_overrides(next_role, desired_permissions))
            fields["permissions"] = []  # bỏ dữ liệu quyền kiểu cũ (chỉ cộng thêm)
        if fields.get("is_active") is True and user.get("approval_status") == "PENDING":
            fields["approval_status"] = "APPROVED"
        self._ensure_admin_floor(user, fields)
        before = self._user_audit_snapshot(user)
        # Chỉ gọi Firebase khi tên hoặc trạng thái thật sự thay đổi.
        try:
            if payload.display_name is not None and payload.display_name != user.get("display_name"):
                self.identity.update_user(user["firebase_uid"], display_name=payload.display_name)
            if payload.is_active is not None and payload.is_active != user.get("is_active", True):
                self.identity.set_user_disabled(user["firebase_uid"], not payload.is_active)
        except Exception as exc:
            raise ValueError(f"Không cập nhật được tài khoản đăng nhập Firebase: {exc}") from exc
        updated = self.repository.update(user_id, fields)
        if not updated:
            return None
        after = self._user_audit_snapshot(updated)
        could_review = "reviews.manage" in before["permissions"]
        can_review = "reviews.manage" in after["permissions"]
        if payload.is_active is False and before["is_active"]:
            self.sessions.upsert(user["firebase_uid"], None)
            self._release_reviewer_work(user["_id"], could_review, actor, "reviewer_deactivated")
        elif could_review and not can_review:
            self._release_reviewer_work(user["_id"], could_review, actor, "reviewer_role_removed")
        changed_fields = sorted(key for key in before if before[key] != after[key])
        if changed_fields:
            record_audit_event(
                action="user.admin_update",
                entity_type="user",
                entity_id=user["_id"],
                actor_user_id=actor.id if actor else None,
                actor_role=actor.role if actor else None,
                before=before,
                after=after,
                metadata={"changed_fields": changed_fields, "label": user.get("email")},
            )
        return serialize_user(updated)

    def deactivate(self, user_id: str, actor: CurrentUser | None = None) -> bool:
        user = self.repository.find_by_id(user_id)
        if not user:
            return False
        self._ensure_admin_floor(user, {"is_active": False})
        before = self._user_audit_snapshot(user)
        self.identity.set_user_disabled(user["firebase_uid"], True)
        updated = self.repository.update(user_id, {"is_active": False})
        if updated:
            self.sessions.upsert(user["firebase_uid"], None)
            self._release_reviewer_work(
                user["_id"],
                "reviews.manage" in effective_permissions(user),
                actor,
                "reviewer_deactivated",
            )
            record_audit_event(
                action="user.deactivate",
                entity_type="user",
                entity_id=user["_id"],
                actor_user_id=actor.id if actor else None,
                actor_role=actor.role if actor else None,
                before=before,
                after=self._user_audit_snapshot(updated),
                metadata={"label": user.get("email")},
            )
        return updated is not None


def get_user_service() -> UserService:
    database = get_rag_db()

    def release_review_assignments(reviewer_user_id, actor, reason) -> int:
        # Imported lazily: the question workflow module is heavy and imports
        # user-facing helpers of its own.
        from modules.questions.workflow_service import QuestionWorkflowService

        return QuestionWorkflowService(database).release_assignments_for_reviewer(
            reviewer_user_id,
            actor,
            reason,
        )

    if settings.user_store == "postgres":
        from modules.users.postgres_repository import PostgresUserRepository

        repository = PostgresUserRepository()
    else:
        repository = MongoUserRepository(database)
    return UserService(
        repository,
        FirebaseIdentityGateway(),
        get_firebase_session_repository(),
        release_review_assignments=release_review_assignments,
    )
