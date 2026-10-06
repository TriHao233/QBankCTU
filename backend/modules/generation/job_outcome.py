"""Normalized terminal outcome of a generation job."""

from __future__ import annotations

CANCEL_REASON_USER = "USER_REQUEST"
CANCEL_REASON_USER_DEACTIVATED = "USER_DEACTIVATED"


def generation_job_outcome(job: dict) -> str | None:
    """SUCCEEDED, PARTIAL, CANCELLED, FAILED or DEAD_LETTER; None while the job is queued or running.

    A cancelled job keeps status ``failed`` (terminal for SSE/polling clients) and is
    recognised by its ``cancelled_at`` marker.
    """
    status = job.get("status")
    if job.get("cancelled_at") and status in {"failed", "cancelled"}:
        return "CANCELLED"
    if status == "failed":
        return "DEAD_LETTER" if job.get("dead_lettered_at") else "FAILED"
    if status != "completed":
        return None
    result = job.get("result") or {}
    if result.get("status") == "partial":
        return "PARTIAL"
    summary = result.get("summary") or []
    requested = sum(int(item.get("requested_count") or 0) for item in summary)
    saved = sum(int(item.get("saved_count") or 0) for item in summary)
    return "PARTIAL" if requested and saved < requested else "SUCCEEDED"


__all__ = ["CANCEL_REASON_USER", "CANCEL_REASON_USER_DEACTIVATED", "generation_job_outcome"]
