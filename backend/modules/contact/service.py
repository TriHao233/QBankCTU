"""Business logic for internal contact / support tickets."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId

from core.config import settings
from core.dependencies import CurrentUser
from core.email_service import build_email_html, get_email_service
from core.postgres import postgres_connection
from modules.contact.postgres_repository import PostgresContactRepository
from modules.contact.schemas import (
    ContactMessageCreate,
    ContactRequestCreate,
    ContactRequestUpdate,
    ContactResponseSubmit,
    ContactStatus,
)
from modules.notifications.service import NotificationService, get_notification_service

logger = logging.getLogger(__name__)

CATEGORY_LABELS = {
    "REVIEW_REQUEST": "Yêu cầu duyệt câu hỏi",
    "BUG": "Báo lỗi hệ thống",
    "SUPPORT": "Yêu cầu hỗ trợ",
    "FEEDBACK": "Góp ý / đề xuất",
    "CONTENT_ISSUE": "Báo sai sót nội dung câu hỏi",
}

STATUS_LABELS = {
    "NEW": "Mới",
    "IN_PROGRESS": "Đang xử lý",
    "RESOLVED": "Đã giải quyết",
    "CLOSED": "Đã đóng",
    "WITHDRAWN": "Đã thu hồi",
}


class ContactConflictError(Exception):
    """The ticket changed or its lifecycle no longer permits this action."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContactService:
    def __init__(self) -> None:
        self.repo = PostgresContactRepository()

    # ---- helpers -------------------------------------------------------

    def _authorize(self, request: dict, user: CurrentUser) -> None:
        if user.role == "Admin":
            return
        if str(request["user_id"]) != str(user.id):
            raise PermissionError("Bạn không có quyền truy cập yêu cầu này")

    def _make_ticket_code(self) -> str:
        now = utc_now()
        seq = self.repo.next_ticket_sequence(now.year)
        return f"CT-{now.year}-{seq:04d}"

    def _notifier(self) -> NotificationService | None:
        try:
            return get_notification_service()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Không khởi tạo được NotificationService: %s", exc)
            return None

    # ---- create / list -------------------------------------------------

    def create_request(self, payload: ContactRequestCreate, user: CurrentUser) -> dict:
        if user.role not in ("Teacher", "Reviewer", "Admin"):
            raise PermissionError("Chỉ người dùng nội bộ mới có thể gửi yêu cầu")
        now = utc_now()
        record = {
            "id": str(ObjectId()),
            "ticket_code": self._make_ticket_code(),
            "user_id": str(user.id),
            "category": payload.category.value,
            "title": payload.title.strip(),
            "content": payload.content.strip(),
            "status": ContactStatus.NEW.value,
            "created_at": now,
            "updated_at": now,
            "resolved_at": None,
        }
        created = self.repo.create_request(record)
        # Notify admin via email (best-effort).
        self._email_admin_new_ticket(created, requester=user)
        return created

    def list_for_user(
        self,
        user: CurrentUser,
        *,
        page: int,
        page_size: int,
        status: str | None,
        category: str | None,
        search: str | None,
        all_tickets: bool,
        deleted: bool = False,
    ) -> dict:
        # Teacher can only see their own; Admin sees everything when all_tickets=True.
        user_filter: str | None
        if user.role == "Admin" and all_tickets:
            user_filter = None
        else:
            user_filter = str(user.id)
        items, total = self.repo.list_requests(
            page=page,
            page_size=page_size,
            user_id=user_filter,
            status=status,
            category=category,
            search=search,
            deleted=deleted,
        )
        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ---- detail / messages --------------------------------------------

    def get_detail(self, request_id: str, user: CurrentUser) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        self._authorize(request, user)
        messages = self.repo.list_messages(request_id)
        events = self.repo.list_events(request_id)
        return {**request, "messages": messages, "events": events}

    def update_request(self, request_id: str, payload: ContactRequestUpdate,
                       user: CurrentUser) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        self._authorize(request, user)
        if request["deleted_at"]:
            raise ContactConflictError("Yêu cầu đã xóa, hãy khôi phục trước khi sửa")
        changes = payload.model_dump(exclude_unset=True, exclude_none=True)
        if not changes:
            raise ValueError("Chưa có nội dung cần cập nhật")
        for field in ("title", "content"):
            if field in changes:
                changes[field] = changes[field].strip()
                if not changes[field]:
                    raise ValueError(f"{field} không được để trống")
        if "category" in changes:
            changes["category"] = changes["category"].value
        is_owner = str(request["user_id"]) == str(user.id)
        admin_reclassify = user.role == "Admin" and (
            not is_owner or request["status"] != ContactStatus.NEW.value
        )
        if admin_reclassify:
            if set(changes) != {"category"}:
                raise PermissionError("Quản trị viên chỉ được phân loại lại yêu cầu của người khác")
        elif not is_owner or request["status"] != ContactStatus.NEW.value or request["message_count"]:
            raise ContactConflictError("Chỉ có thể sửa yêu cầu mới khi chưa có phản hồi")
        updated = self.repo.edit_request(request, changes, str(user.id), utc_now(),
                                         admin_reclassify=admin_reclassify)
        if not updated:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        return updated

    def withdraw_request(self, request_id: str, user: CurrentUser) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        if str(request["user_id"]) != str(user.id):
            raise PermissionError("Chỉ người gửi được thu hồi yêu cầu")
        if request["deleted_at"] or request["status"] not in (
            ContactStatus.NEW.value, ContactStatus.IN_PROGRESS.value
        ):
            raise ContactConflictError("Chỉ có thể thu hồi yêu cầu đang chờ hoặc đang xử lý")
        updated = self.repo.withdraw_request(request, str(user.id), utc_now())
        if not updated:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        return updated

    def delete_request(self, request_id: str, user: CurrentUser) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        self._authorize(request, user)
        if request["deleted_at"]:
            raise ContactConflictError("Yêu cầu đã được xóa")
        is_owner = str(request["user_id"]) == str(user.id)
        allow_new = is_owner and request["status"] == ContactStatus.NEW.value and not request["message_count"]
        terminal = request["status"] in (
            ContactStatus.WITHDRAWN.value, ContactStatus.RESOLVED.value,
            ContactStatus.CLOSED.value,
        )
        if not (allow_new or terminal):
            raise ContactConflictError("Hãy thu hồi hoặc hoàn tất yêu cầu trước khi xóa")
        deleted = self.repo.delete_request(request, str(user.id), utc_now(),
                                           allow_new=allow_new)
        if not deleted:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        return deleted

    def restore_request(self, request_id: str, user: CurrentUser) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        self._authorize(request, user)
        if not request["deleted_at"]:
            raise ContactConflictError("Yêu cầu chưa bị xóa")
        restored = self.repo.restore_request(request, str(user.id), utc_now())
        if not restored:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        return restored

    def add_message(
        self, request_id: str, payload: ContactMessageCreate, user: CurrentUser,
    ) -> dict:
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        self._authorize(request, user)
        if request["deleted_at"] or request["status"] in (
            ContactStatus.CLOSED.value, ContactStatus.WITHDRAWN.value
        ):
            raise ContactConflictError("Yêu cầu đã đóng, thu hồi hoặc xóa, không thể phản hồi thêm")
        now = utc_now()
        record = {
            "id": str(ObjectId()),
            "request_id": request_id,
            "sender_id": str(user.id),
            "message": payload.message.strip(),
            "created_at": now,
        }
        message = self.repo.add_message(record)
        # If admin replies to a NEW ticket, move to IN_PROGRESS automatically.
        new_status = None
        if user.role == "Admin" and request["status"] == ContactStatus.NEW.value:
            self.repo.update_status(request_id, ContactStatus.IN_PROGRESS.value, now)
            new_status = ContactStatus.IN_PROGRESS.value
        else:
            self.repo.touch(request_id, now)

        # Notify the requester when admin replies (skip self-notify).
        if user.role == "Admin" and str(request["user_id"]) != str(user.id):
            self._notify_reply(request, user.id, new_status)
            self._email_user_reply(request, payload.message.strip(), new_status)
        return message

    def update_status(
        self, request_id: str, status: ContactStatus, user: CurrentUser,
    ) -> dict:
        if user.role != "Admin":
            raise PermissionError("Chỉ quản trị viên có thể cập nhật trạng thái")
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        if status == ContactStatus.WITHDRAWN:
            raise PermissionError("Chỉ người gửi được thu hồi yêu cầu")
        if request["deleted_at"] or request["status"] == ContactStatus.WITHDRAWN.value:
            raise ContactConflictError("Không thể đổi trạng thái yêu cầu đã thu hồi hoặc xóa")
        now = utc_now()
        updated = self.repo.update_status(request_id, status.value, now)
        if not updated:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        # Notify the requester of status change (skip self-notify).
        if str(request["user_id"]) != str(user.id):
            self._notify_status(updated, user.id)
        return updated

    def respond_to_request(
        self, request_id: str, payload: ContactResponseSubmit, user: CurrentUser,
    ) -> dict:
        if user.role != "Admin":
            raise PermissionError("Chỉ quản trị viên có thể gửi phản hồi kèm trạng thái")
        request = self.repo.find_request(request_id)
        if not request:
            raise LookupError("Không tìm thấy yêu cầu liên hệ")
        if request["deleted_at"] or request["status"] in (
            ContactStatus.WITHDRAWN.value, ContactStatus.CLOSED.value,
        ):
            raise ContactConflictError("Yêu cầu đã đóng, thu hồi hoặc xóa, không thể phản hồi thêm")
        if payload.status == ContactStatus.WITHDRAWN:
            raise PermissionError("Chỉ người gửi được thu hồi yêu cầu")

        message = (payload.message or "").strip()
        selected_status = payload.status.value if payload.status else request["status"]
        if message and payload.status is None and selected_status == ContactStatus.NEW.value:
            selected_status = ContactStatus.IN_PROGRESS.value
        status_changed = selected_status != request["status"]
        if not message and not status_changed:
            raise ValueError("Nhập nội dung phản hồi hoặc chọn trạng thái mới")

        updated = self.repo.submit_response(
            request, str(user.id), selected_status, message or None, utc_now(),
        )
        if not updated:
            raise ContactConflictError("Yêu cầu vừa thay đổi, vui lòng tải lại")
        if str(request["user_id"]) != str(user.id):
            if message:
                self._notify_reply(request, user.id, selected_status if status_changed else None)
                self._email_user_reply(request, message, selected_status)
            else:
                self._notify_status(updated, user.id)
                self._email_user_status(updated)
        return updated

    # ---- notifications -------------------------------------------------

    def _notify_reply(self, request: dict, actor_id, new_status: str | None) -> None:
        service = self._notifier()
        if service is None:
            return
        title = f"[{request['ticket_code']}] Quản trị viên đã phản hồi"
        body = "Có phản hồi mới cho yêu cầu liên hệ của bạn."
        if new_status:
            body += f" Trạng thái: {STATUS_LABELS.get(new_status, new_status)}."
        try:
            service.create(
                recipient_user_id=ObjectId(request["user_id"]),
                actor_user_id=actor_id,
                type="CONTACT_REPLY",
                title=title,
                body=body,
                link=f"/lien-he?ticket={request['id']}",
                entity={"type": "CONTACT_REQUEST", "id": request["id"],
                        "ticket_code": request["ticket_code"]},
            )
        except Exception as exc:
            logger.warning("Failed to send contact reply notification: %s", exc)

    def _notify_status(self, request: dict, actor_id) -> None:
        service = self._notifier()
        if service is None:
            return
        status_label = STATUS_LABELS.get(request["status"], request["status"])
        try:
            service.create(
                recipient_user_id=ObjectId(request["user_id"]),
                actor_user_id=actor_id,
                type="CONTACT_STATUS",
                title=f"[{request['ticket_code']}] Trạng thái: {status_label}",
                body=f"Yêu cầu liên hệ của bạn đã được cập nhật trạng thái: {status_label}.",
                link=f"/lien-he?ticket={request['id']}",
                entity={"type": "CONTACT_REQUEST", "id": request["id"],
                        "ticket_code": request["ticket_code"]},
            )
        except Exception as exc:
            logger.warning("Failed to send contact status notification: %s", exc)


    # ---- email helpers -------------------------------------------------

    def _user_email(self, user_id: str) -> tuple[str | None, str, bool]:
        """Return (email, display_name, email_enabled) for a stored user."""
        try:
            with postgres_connection() as conn:
                row = conn.execute(
                    "SELECT email, display_name, profile FROM users WHERE id = %s",
                    (str(user_id),),
                ).fetchone()
        except Exception as exc:  # pragma: no cover
            logger.warning("Không đọc được email người dùng: %s", exc)
            return None, "", True
        if not row:
            return None, "", True
        profile = row.get("profile") or {}
        enabled = profile.get("email_notifications_enabled", True)
        override = (profile.get("notification_email") or "").strip()
        return (override or row.get("email") or None, row.get("display_name") or "", bool(enabled))

    def _email_admin_new_ticket(self, request: dict, *, requester: CurrentUser) -> None:
        admin_email = settings.contact_admin_email
        if not admin_email:
            logger.info("Email admin skipped: CONTACT_ADMIN_EMAIL chưa cấu hình")
            return
        subject = f"[{request['ticket_code']}] Yêu cầu {CATEGORY_LABELS.get(request['category'], request['category'])} mới"
        meta = {
            "Mã ticket": request["ticket_code"],
            "Loại": CATEGORY_LABELS.get(request["category"], request["category"]),
            "Người gửi": requester.display_name or requester.email,
            "Email liên hệ": requester.email,
        }
        html_body = build_email_html(
            title=f"Yêu cầu liên hệ mới: {request['title']}",
            paragraphs=[
                "Một giảng viên vừa gửi yêu cầu liên hệ. Thông tin chi tiết được ghi bên dưới.",
                "Đăng nhập vào QBankCTU, mở mục Xem liên hệ để xem và phản hồi yêu cầu này.",
            ],
            meta=meta,
            highlight=request["content"],
            highlight_label="NỘI DUNG YÊU CẦU",
        )
        get_email_service().send(
            to_email=admin_email, subject=subject, html_body=html_body,
        )

    def _email_user_reply(self, request: dict, admin_message: str, new_status: str | None) -> None:
        email, name, enabled = self._user_email(request["user_id"])
        if not email or not enabled:
            return
        status_line = STATUS_LABELS.get(new_status or request["status"], new_status or request["status"])
        subject = f"[{request['ticket_code']}] Quản trị viên đã phản hồi"
        html_body = build_email_html(
            title="Yêu cầu liên hệ của bạn đã có phản hồi",
            paragraphs=[
                "Quản trị viên đã phản hồi yêu cầu liên hệ của bạn.",
                "Đăng nhập vào QBankCTU để xem toàn bộ trao đổi và trả lời tiếp nếu cần.",
            ],
            meta={
                "Mã ticket": request["ticket_code"],
                "Tiêu đề": request["title"],
                "Trạng thái": status_line,
            },
            highlight=admin_message,
            highlight_label="PHẢN HỒI CỦA QUẢN TRỊ VIÊN",
        )
        get_email_service().send(
            to_email=email, to_name=name, subject=subject, html_body=html_body,
        )

    def _email_user_status(self, request: dict) -> None:
        email, name, enabled = self._user_email(request["user_id"])
        if not email or not enabled:
            return
        status_label = STATUS_LABELS.get(request["status"], request["status"])
        subject = f"[{request['ticket_code']}] Trạng thái: {status_label}"
        html_body = build_email_html(
            title=f"Yêu cầu liên hệ đã được cập nhật: {status_label}",
            paragraphs=[
                f"Quản trị viên đã cập nhật trạng thái yêu cầu liên hệ của bạn sang \"{status_label}\".",
                "Đăng nhập vào QBankCTU để xem chi tiết trao đổi.",
            ],
            meta={
                "Mã ticket": request["ticket_code"],
                "Tiêu đề": request["title"],
                "Trạng thái": status_label,
            },
        )
        get_email_service().send(
            to_email=email, to_name=name, subject=subject, html_body=html_body,
        )


def get_contact_service() -> ContactService:
    return ContactService()
