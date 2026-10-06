"""Checks re-run on the working version when it is submitted for review.

Schema problems and missing grounding for AI-generated questions block the
submission; invalid sources, duplicates and answer/distractor heuristics only
produce warnings/flags for the reviewer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from bson import ObjectId

from modules.generation.postprocessing import (
    contains_exact_text,
    duplicate_level,
    normalize_exact_text,
    question_fingerprint,
)
from modules.generation.structured_answers import structured_answer_errors

__all__ = [
    "GroundingError",
    "SourceNotLocatedError",
    "answer_quality_issues",
    "check_grounding",
    "duplicate_flag",
    "evidence_texts",
    "submission_schema_errors",
]

CHOICE_TYPES = {"trac_nghiem", "tinh_huong", "dung_sai", "nhieu_lua_chon"}
MULTI_ANSWER_TYPES = {"nhieu_lua_chon"}


class GroundingError(ValueError):
    pass


class SourceNotLocatedError(GroundingError):
    """An AI candidate whose evidence quote is not found in any retrieved chunk."""

    code = "SOURCE_NOT_LOCATED"

    def __init__(self) -> None:
        super().__init__("Không định vị được chunk chứa đoạn minh chứng của câu hỏi (SOURCE_NOT_LOCATED)")


def _answer_keys(correct_answer: str) -> list[str]:
    return [item.strip() for item in str(correct_answer or "").split(",") if item.strip()]


def submission_schema_errors(version: dict) -> list[str]:
    """Content, answer and option rules that must hold before a reviewer sees the question."""
    errors: list[str] = []
    question_data = version.get("question_data") or {}
    question_type = str((version.get("classification") or {}).get("assessment_type") or "").lower()
    if not str(version.get("content") or "").strip():
        errors.append("Nội dung câu hỏi không được để trống")
    correct_answer = str(question_data.get("correct_answer") or "").strip()
    if not correct_answer:
        errors.append("Đáp án đúng không được để trống")
    options = question_data.get("options")
    if isinstance(options, dict):
        if any(not str(value or "").strip() for value in options.values()):
            errors.append("Các lựa chọn không được để trống")
        if question_type in CHOICE_TYPES and correct_answer:
            keys = _answer_keys(correct_answer)
            if any(key not in options for key in keys):
                errors.append("Đáp án đúng phải là một trong các lựa chọn")
            if question_type in MULTI_ANSWER_TYPES and len(keys) < 2:
                errors.append("Câu nhiều lựa chọn cần ít nhất 2 đáp án đúng")
    elif question_type in CHOICE_TYPES:
        errors.append("Câu trắc nghiệm phải có danh sách lựa chọn")
    if correct_answer:
        errors.extend(structured_answer_errors(question_type, options, correct_answer))
    return errors


def check_grounding(
    version: dict,
    *,
    grounding_required: bool,
    find_chunk: Callable[[ObjectId], dict | None],
) -> dict:
    """Recheck cited sources; raise GroundingError when a required grounding is missing."""
    sources = version.get("sources") or []
    quote = str((version.get("question_data") or {}).get("model_source_context") or "").strip()
    valid, invalid = 0, 0
    quote_found = False
    for source in sources:
        if source.get("source_type") == "MANUAL_EXCERPT":
            valid += 1
            quote_found = quote_found or (bool(quote) and contains_exact_text(source.get("context_excerpt") or "", quote))
            continue
        chunk_id = source.get("chunk_id")
        chunk = find_chunk(chunk_id) if chunk_id is not None else None
        if not chunk or chunk.get("content_removed") or chunk.get("archived_at"):
            invalid += 1
            continue
        valid += 1
        if quote and contains_exact_text(chunk.get("content") or "", quote):
            quote_found = True
    warnings = []
    if invalid:
        warnings.append(f"{invalid} nguồn trích dẫn không còn hợp lệ và không được tính")
    if grounding_required:
        if not valid:
            raise GroundingError("Câu hỏi do AI sinh phải còn ít nhất một nguồn hợp lệ (GROUNDING_REQUIRED)")
        if not quote_found:
            raise GroundingError(
                "Đoạn minh chứng của câu hỏi không còn xuất hiện trong nguồn đã trích dẫn (GROUNDING_FAILED)"
            )
    status = "verified" if valid and (quote_found or not quote) else ("none" if not sources else "weak")
    if status == "weak":
        warnings.append("Đoạn minh chứng không khớp nguồn trích dẫn")
    return {"status": status, "valid_sources": valid, "invalid_sources": invalid, "warnings": warnings}


def duplicate_flag(content: str, existing_texts: Iterable[str]) -> str | None:
    level = duplicate_level(content, existing_texts)
    return {"exact": "duplicate_suspect", "near": "near_duplicate"}.get(level or "")


ANSWER_SUPPORT_MIN_RATIO = 0.6
OPTION_ANSWER_TYPES = {"trac_nghiem", "tinh_huong", "nhieu_lua_chon"}
TEXT_ANSWER_TYPES = {"dien_khuyet"}


def evidence_texts(version: dict, find_chunk: Callable[[ObjectId], dict | None]) -> list[str]:
    """The evidence quote plus the text of every still-valid cited source."""
    texts = [str((version.get("question_data") or {}).get("model_source_context") or "")]
    for source in version.get("sources") or []:
        if source.get("source_type") == "MANUAL_EXCERPT":
            texts.append(str(source.get("context_excerpt") or ""))
            continue
        chunk_id = source.get("chunk_id")
        chunk = find_chunk(chunk_id) if chunk_id is not None else None
        if chunk and not chunk.get("content_removed") and not chunk.get("archived_at"):
            texts.append(str(chunk.get("content") or ""))
    return [text for text in texts if text.strip()]


def _tokens(value: str) -> list[str]:
    return [token for token in re.findall(r"\w+", normalize_exact_text(value), flags=re.UNICODE) if token]


def _support_ratio(answer: str, evidence_tokens: set[str]) -> float:
    tokens = _tokens(answer)
    if not tokens:
        return 1.0
    return sum(token in evidence_tokens for token in tokens) / len(tokens)


def answer_quality_issues(version: dict, evidence: Iterable[str]) -> list[tuple[str, str]]:
    """Deterministic answer/distractor heuristics (spec 8.4 steps 3-4); warnings only, never blocking."""
    question_data = version.get("question_data") or {}
    question_type = str((version.get("classification") or {}).get("assessment_type") or "").lower()
    evidence = [text for text in evidence if str(text or "").strip()]
    evidence_tokens = {token for text in evidence for token in _tokens(text)}
    quote = str(question_data.get("model_source_context") or "")
    issues: list[tuple[str, str]] = []

    if question_type in TEXT_ANSWER_TYPES:
        answer = str(question_data.get("correct_answer") or "")
        if evidence_tokens and _support_ratio(answer, evidence_tokens) < ANSWER_SUPPORT_MIN_RATIO:
            issues.append(("answer_not_in_source", "Đáp án đúng không tìm thấy căn cứ trong nguồn trích dẫn"))
        return issues
    options = question_data.get("options")
    if question_type not in OPTION_ANSWER_TYPES or not isinstance(options, dict):
        return issues

    keys = set(_answer_keys(question_data.get("correct_answer") or ""))
    texts = {key: str(value) for key, value in options.items() if isinstance(value, str) and value.strip()}
    correct = {key: text for key, text in texts.items() if key in keys}
    distractors = {key: text for key, text in texts.items() if key not in keys}

    correct_prints = {question_fingerprint(text) for text in correct.values()}
    if any(question_fingerprint(text) in correct_prints for text in distractors.values()):
        issues.append(("distractor_equals_answer", "Có phương án nhiễu trùng nội dung với đáp án đúng"))
    distractor_prints = [question_fingerprint(text) for text in distractors.values()]
    if len(distractor_prints) != len(set(distractor_prints)) or len(correct_prints) != len(correct):
        issues.append(("duplicate_options", "Có các lựa chọn trùng nội dung nhau"))

    if evidence_tokens and any(
        _support_ratio(text, evidence_tokens) < ANSWER_SUPPORT_MIN_RATIO for text in correct.values()
    ):
        issues.append(("answer_not_in_source", "Đáp án đúng không tìm thấy căn cứ trong nguồn trích dẫn"))

    if quote.strip() and len(keys) == 1 and not any(contains_exact_text(quote, text) for text in correct.values()):
        if any(len(_tokens(text)) >= 2 and contains_exact_text(quote, text) for text in distractors.values()):
            issues.append((
                "distractor_in_evidence",
                "Đoạn minh chứng chứa nguyên văn một phương án nhiễu nhưng không chứa đáp án đúng",
            ))
    return issues
