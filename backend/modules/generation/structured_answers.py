"""Integrity rules for matching (ghep_cot) and ordering (sap_xep) answers."""

from __future__ import annotations

import re
import unicodedata

__all__ = ["structured_answer_errors"]

PAIR_PATTERN = re.compile(r"^\s*(\d+)\s*-\s*([A-Za-z]+)\s*$")


def _content_key(value) -> str:
    return " ".join(unicodedata.normalize("NFC", str(value or "")).lower().split())


def _duplicate_contents(options: dict, keys: list[str]) -> bool:
    contents = [_content_key(options[key]) for key in keys if _content_key(options[key])]
    return len(contents) != len(set(contents))


def _matching_errors(options: dict, correct_answer: str) -> list[str]:
    left = [str(key) for key in options if str(key).isdigit()]
    right = {str(key).lower(): str(key) for key in options if str(key).isalpha()}
    errors: list[str] = []
    parts = [part for part in str(correct_answer or "").split(",") if part.strip()]
    pairs = [PAIR_PATTERN.match(part) for part in parts]
    if not parts or not all(pairs):
        return ["ghep_cot: correct_answer phải theo định dạng '1-b, 2-a, ...'"]
    used_left: list[str] = []
    used_right: list[str] = []
    for match in pairs:
        left_key, right_key = match.group(1), match.group(2).lower()
        if left_key not in left:
            errors.append(f"ghep_cot: đáp án tham chiếu mục số '{left_key}' không tồn tại")
        if right_key not in right:
            errors.append(f"ghep_cot: đáp án tham chiếu mục chữ '{right_key}' không tồn tại")
        if left_key in used_left:
            errors.append(f"ghep_cot: mục số '{left_key}' được ghép nhiều lần")
        if right_key in used_right:
            errors.append(f"ghep_cot: mục chữ '{right_key}' được ghép cho nhiều mục số")
        used_left.append(left_key)
        used_right.append(right_key)
    missing = [key for key in left if key not in used_left]
    if missing:
        errors.append(f"ghep_cot: thiếu cặp ghép cho mục số {', '.join(missing)}")
    if _duplicate_contents(options, left) or _duplicate_contents(options, list(right.values())):
        errors.append("ghep_cot: có các mục trùng nội dung trong cùng một cột")
    return errors


def _ordering_errors(options: dict, correct_answer: str) -> list[str]:
    keys = [str(key) for key in options]
    order = [item.strip() for item in str(correct_answer or "").split(",") if item.strip()]
    errors: list[str] = []
    unknown = [key for key in order if key not in keys]
    if unknown:
        errors.append(f"sap_xep: đáp án tham chiếu bước không tồn tại: {', '.join(unknown)}")
    repeated = sorted({key for key in order if order.count(key) > 1})
    if repeated:
        errors.append(f"sap_xep: bước bị lặp trong đáp án: {', '.join(repeated)}")
    missing = [key for key in keys if key not in order]
    if missing:
        errors.append(f"sap_xep: đáp án thiếu bước {', '.join(missing)}")
    if _duplicate_contents(options, keys):
        errors.append("sap_xep: có các bước trùng nội dung")
    return errors


def structured_answer_errors(question_type: str, options, correct_answer: str) -> list[str]:
    question_type = str(question_type or "").lower()
    if question_type not in {"ghep_cot", "sap_xep"}:
        return []
    if not isinstance(options, dict) or not options:
        return [f"{question_type} phải có options dạng object"]
    if question_type == "ghep_cot":
        return _matching_errors(options, correct_answer)
    return _ordering_errors(options, correct_answer)
