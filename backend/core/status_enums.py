"""Canonical status helpers shared across document / generation / evaluation jobs.

Document and evaluation jobs persist UPPERCASE statuses.
Generation jobs persist lowercase statuses.
API responses and admin filters may need either form — normalize before compare.
"""

from __future__ import annotations

DOCUMENT_JOB_STATUSES = frozenset(
    {"QUEUED", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED", "ERROR", "STALE", "BLOCKED"}
)
GENERATION_JOB_STATUSES = frozenset({"queued", "processing", "completed", "failed", "cancelled"})
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_JOB_STATUSES = frozenset({"queued", "processing"})
RETRYABLE_JOB_STATUSES = frozenset({"failed", "error", "stale", "blocked"})


def normalize_job_status(status: str | None) -> str:
    return str(status or "").strip().lower()


def canonicalize_document_job_status(status: str | None) -> str:
    return normalize_job_status(status).upper()


def is_terminal_job_status(status: str | None) -> bool:
    return normalize_job_status(status) in TERMINAL_JOB_STATUSES


def is_active_job_status(status: str | None) -> bool:
    return normalize_job_status(status) in ACTIVE_JOB_STATUSES


def is_retryable_job_status(status: str | None) -> bool:
    return normalize_job_status(status) in RETRYABLE_JOB_STATUSES
