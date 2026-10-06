"""Which questions a reviewer (without all-question management) may read.

A reviewer reads a question when it waits in the review queue for one of the
subjects they review (no subjects assigned means none), when it is
assigned to them, or when they already decided on it. Anything else is hidden
as if it did not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["ReviewerScope", "reviewer_can_read"]


class ReviewerScope:
    def __init__(self, user_id: Any, subject_ids: list | tuple | set | None):
        self.user_id = user_id
        self.subject_ids = {str(item) for item in subject_ids or ()}

    def covers_subject(self, subject_id: Any) -> bool:
        return subject_id is not None and str(subject_id) in self.subject_ids

    def covers_question(self, question: dict, version: dict | None) -> bool:
        return self.covers_subject(_question_subject_id(question, version))


def _question_subject_id(question: dict, version: dict | None) -> Any:
    return question.get("subject_id") or (
        ((version or {}).get("classification") or {}).get("subject") or {}
    ).get("id")


def reviewer_can_read(
    question: dict,
    version: dict | None,
    scope: ReviewerScope,
    has_decided: Callable[[], bool],
) -> bool:
    uid = str(scope.user_id)
    assignment = question.get("review_assignment") or {}
    if str(assignment.get("reviewer_user_id")) == uid:
        return True
    secondary = question.get("secondary_review") or {}
    if uid in {str(secondary.get("primary_reviewer_user_id")), str(secondary.get("secondary_reviewer_user_id"))}:
        return True
    if question.get("review_status") == "PENDING" and scope.covers_subject(_question_subject_id(question, version)):
        return True
    return has_decided()
