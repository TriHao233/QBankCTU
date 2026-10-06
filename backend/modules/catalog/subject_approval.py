"""Approval rules for teacher-created subjects.

A subject created by a teacher waits for an admin before it joins the shared
catalog. Until then only its owner can see it or use it, and it cannot enter
review, sharing or exams. Records without ``approval_status`` predate this
workflow and count as approved.
"""

from typing import Any

__all__ = [
    "SUBJECT_APPROVED",
    "SUBJECT_PENDING_APPROVAL",
    "SUBJECT_REJECTED",
    "ensure_subject_approved",
    "ensure_subject_usable_by",
    "subject_approval_status",
    "subject_is_approved",
    "subject_usable_by",
]

SUBJECT_APPROVED = "APPROVED"
SUBJECT_PENDING_APPROVAL = "PENDING_APPROVAL"
SUBJECT_REJECTED = "REJECTED"


def subject_approval_status(record: dict | None) -> str:
    return str((record or {}).get("approval_status") or SUBJECT_APPROVED).upper()


def subject_is_approved(record: dict | None) -> bool:
    return subject_approval_status(record) == SUBJECT_APPROVED


def _is_owner(record: dict, user_id: Any) -> bool:
    owner_id = record.get("owner_id")
    return owner_id is not None and user_id is not None and str(owner_id) == str(user_id)


def subject_usable_by(record: dict | None, user_id: Any, *, is_admin: bool = False) -> bool:
    """Approved subjects are shared; pending/rejected ones belong to their owner (and admins)."""
    if not record:
        return False
    return subject_is_approved(record) or is_admin or _is_owner(record, user_id)


def ensure_subject_usable_by(record: dict | None, user_id: Any, *, is_admin: bool = False) -> None:
    if not subject_usable_by(record, user_id, is_admin=is_admin):
        raise LookupError("Không tìm thấy học phần")


def ensure_subject_approved(record: dict | None, action: str) -> None:
    if record and not subject_is_approved(record):
        raise ValueError(f"Học phần đang chờ quản trị viên duyệt, chưa thể {action}")
