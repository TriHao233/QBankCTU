"""Limits on how many questions a reviewer may hold and how long."""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "MIN_TAKEOVER_REASON_LENGTH",
    "active_holder_id",
    "carried_renewal_count",
    "released_assignment",
    "review_hold_limit_message",
    "review_renewal_limit_message",
]

MIN_TAKEOVER_REASON_LENGTH = 5


def review_hold_limit_message(max_held: int) -> str:
    return (
        f"Bạn đang giữ tối đa {max_held} câu hỏi. "
        "Hãy hoàn tất hoặc trả lại bớt câu trước khi nhận thêm."
    )


def review_renewal_limit_message(max_renewals: int) -> str:
    return (
        f"Đã gia hạn khóa kiểm duyệt tối đa {max_renewals} lần. "
        "Hãy hoàn tất kiểm duyệt hoặc trả câu hỏi về hàng đợi."
    )


def carried_renewal_count(previous: dict, actor_user_id) -> int:
    """Renewals already used by this reviewer on the current submission.

    Claiming again (after expiry or after releasing) must not reset the renewal limit.
    """
    previous = previous or {}
    if previous.get("reviewer_user_id") is not None and str(previous["reviewer_user_id"]) == str(actor_user_id):
        return int(previous.get("renewal_count") or 0)
    if previous.get("last_reviewer_user_id") is not None and str(previous["last_reviewer_user_id"]) == str(actor_user_id):
        return int(previous.get("last_renewal_count") or 0)
    return 0


def released_assignment(empty: dict, previous: dict) -> dict:
    """An empty assignment that remembers who held it and how many renewals they used."""
    previous = previous or {}
    holder = previous.get("reviewer_user_id") or previous.get("last_reviewer_user_id")
    if holder is None:
        return dict(empty)
    count = (
        previous.get("renewal_count") if previous.get("reviewer_user_id") is not None
        else previous.get("last_renewal_count")
    )
    return {**empty, "last_reviewer_user_id": holder, "last_renewal_count": int(count or 0)}


def _aware(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def active_holder_id(assignment: dict, now):
    """Reviewer currently holding the question (claimed or assigned, lock not expired), else None."""
    assignment = assignment or {}
    if assignment.get("status") not in {"IN_REVIEW", "ASSIGNED"} or assignment.get("reviewer_user_id") is None:
        return None
    expires = _aware(assignment.get("lock_expires_at"))
    if expires is not None and expires <= _aware(now):
        return None
    return assignment["reviewer_user_id"]
