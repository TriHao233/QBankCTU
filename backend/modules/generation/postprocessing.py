from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

from modules.generation.schemas import GeneratedQuestion, GenerationRejection


POSTPROCESSOR_VERSION = "question-post-v3"
MAX_QUESTION_CODE_LINES = 12
MAX_QUESTION_CODE_CHARS = 800
MAX_TRUE_FALSE_LENGTH = 320
MAX_SOURCE_KEYWORDS = 6
MIN_SOURCE_CONTEXT_CHARS = 24
MIN_SOURCE_CONTEXT_WORDS = 4


@dataclass(frozen=True)
class DuplicateStats:
    exact: int = 0
    near: int = 0

    @property
    def total(self) -> int:
        return self.exact + self.near


def normalize_exact_text(value: str) -> str:
    """Normalize only representation details; accents and punctuation stay intact."""
    normalized = unicodedata.normalize("NFC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def contains_exact_text(container: str, expected: str) -> bool:
    normalized_expected = normalize_exact_text(expected)
    if not normalized_expected:
        return False
    normalized_container = normalize_exact_text(container)
    pattern = rf"(?<!\w){re.escape(normalized_expected)}(?!\w)"
    if re.search(pattern, normalized_container, flags=re.UNICODE) is not None:
        return True

    # OCR/PDF extraction can split a Vietnamese word at different positions
    # (for example ``n ày`` versus ``nà y``). Keep accents and character order
    # strict while ignoring only separators so grounded quotes still match.
    compact_expected = re.sub(r"[\W_]+", "", normalized_expected, flags=re.UNICODE)
    compact_container = re.sub(r"[\W_]+", "", normalized_container, flags=re.UNICODE)
    return len(compact_expected) >= 12 and compact_expected in compact_container


def _context_evidence_sections(context_text: str) -> list[str]:
    """Extract only the RAG payload after each `Nội dung:` label."""
    return [
        match.strip()
        for match in re.findall(
            # A Markdown rule inside a chunk is content; only a rule followed by the next header ends a section.
            r"(?:^|\n)Nội dung:\s*(.*?)(?=\n\s*---\s*\n\s*Mục lục:|\Z)",
            str(context_text or ""),
            flags=re.DOTALL,
        )
        if match.strip()
    ]


def _is_substantive_source_context(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    without_heading_marker = re.sub(r"^#+\s*", "", text).strip()
    if "\n" not in text and text.lstrip().startswith("#"):
        return False
    words = re.findall(r"\w+", without_heading_marker, flags=re.UNICODE)
    return len(without_heading_marker) >= MIN_SOURCE_CONTEXT_CHARS and len(words) >= MIN_SOURCE_CONTEXT_WORDS


def question_fingerprint(question: str) -> str:
    normalized = normalize_exact_text(question)
    normalized = re.sub(r"[_\W]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def _near_duplicate(first: str, second: str) -> bool:
    if not first or not second:
        return False
    if first == second:
        return True

    first_tokens = set(first.split())
    second_tokens = set(second.split())
    union = first_tokens | second_tokens
    jaccard = len(first_tokens & second_tokens) / len(union) if union else 0.0
    containment = (
        len(first_tokens & second_tokens) / min(len(first_tokens), len(second_tokens))
        if first_tokens and second_tokens
        else 0.0
    )
    sequence_ratio = SequenceMatcher(None, first, second).ratio()

    # Short statements share many generic words, so use the stricter sequence gate.
    if min(len(first_tokens), len(second_tokens)) < 5:
        return sequence_ratio >= 0.96
    return (
        jaccard >= 0.88
        or sequence_ratio >= 0.94
        or (
            containment >= 0.92
            and abs(len(first_tokens) - len(second_tokens)) <= 4
        )
    )


def _plausible_near_duplicate(first: str, second: str) -> bool:
    """Cheap prefilter before the more expensive SequenceMatcher comparison."""
    first_tokens = set(first.split())
    second_tokens = set(second.split())
    if not first_tokens or not second_tokens:
        return False
    length_ratio = min(len(first), len(second)) / max(len(first), len(second))
    if length_ratio < 0.55:
        return False
    overlap = len(first_tokens & second_tokens) / min(len(first_tokens), len(second_tokens))
    return overlap >= 0.5


def duplicate_level(question: str, existing_texts: Iterable[str]) -> str | None:
    """"exact" or "near" when ``question`` repeats one of ``existing_texts``, else None."""
    fingerprint = question_fingerprint(question)
    if not fingerprint:
        return None
    level = None
    for text in existing_texts:
        other = question_fingerprint(text)
        if other == fingerprint:
            return "exact"
        if level is None and _plausible_near_duplicate(fingerprint, other) and _near_duplicate(fingerprint, other):
            level = "near"
    return level


def filter_duplicate_questions(
    questions: list[GeneratedQuestion],
    seen_question_fingerprints: set[str],
    *,
    limit: int,
    keep_duplicates: bool = False,
) -> tuple[list[GeneratedQuestion], DuplicateStats]:
    kept: list[GeneratedQuestion] = []
    exact_count = 0
    near_count = 0

    for question in questions:
        fingerprint = question_fingerprint(question.question)
        if not fingerprint or fingerprint in seen_question_fingerprints:
            exact_count += 1
            if keep_duplicates:
                question.validation_warnings.append(
                    "Câu này có nội dung trùng với câu đã có hoặc câu vừa sinh."
                )
                kept.append(question)
                if len(kept) >= limit:
                    break
            continue
        if any(
            _near_duplicate(fingerprint, seen)
            for seen in seen_question_fingerprints
            if _plausible_near_duplicate(fingerprint, seen)
        ):
            near_count += 1
            if keep_duplicates:
                question.validation_warnings.append(
                    "Câu này gần giống một câu đã có hoặc câu vừa sinh."
                )
                kept.append(question)
                if len(kept) >= limit:
                    break
            continue
        seen_question_fingerprints.add(fingerprint)
        kept.append(question)
        if len(kept) >= limit:
            break

    return kept, DuplicateStats(exact=exact_count, near=near_count)


def _rejection(
    code: str,
    message: str,
    *,
    item: dict,
    candidate_index: int,
    repairable: bool,
) -> GenerationRejection:
    return GenerationRejection(
        code=code,
        message=message,
        candidate_index=candidate_index,
        question_excerpt=str(item.get("question") or "")[:180],
        repairable=repairable,
    )


def validate_source_grounding(
    item: dict,
    *,
    context_text: str,
    question_type: str,
    candidate_index: int,
) -> list[GenerationRejection]:
    errors: list[GenerationRejection] = []
    source_context = str(item.get("source_context") or "").strip()
    # Small Ollama models sometimes copy the RAG wrapper together with the
    # evidence. Remove only that wrapper; the quote must still match content.
    source_context = re.sub(
        r"^Nội dung:\s*(?:\.{3}|…)?\s*",
        "",
        source_context,
        count=1,
        flags=re.IGNORECASE,
    )
    item["source_context"] = source_context
    statement = str(item.get("question") or "").strip()

    if not source_context:
        errors.append(
            _rejection(
                "SOURCE_CONTEXT_MISSING",
                "Thiếu trích dẫn source_context để đối chiếu với ngữ cảnh RAG.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
        return errors
    evidence_sections = _context_evidence_sections(context_text)
    if not any(contains_exact_text(section, source_context) for section in evidence_sections):
        errors.append(
            _rejection(
                "SOURCE_CONTEXT_NOT_IN_CONTENT",
                "source_context phải là trích dẫn nguyên văn từ phần Nội dung của RAG, không phải mục lục.",
                item=item,
                candidate_index=candidate_index,
                repairable=False,
            )
        )
    if not _is_substantive_source_context(source_context):
        errors.append(
            _rejection(
                "SOURCE_CONTEXT_TOO_SHORT",
                "source_context phải là một đoạn nội dung cụ thể, không được chỉ là tiêu đề chương/mục.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )

    raw_keywords = item.get("source_keywords")
    keywords = raw_keywords if isinstance(raw_keywords, list) else []
    invalid_keyword_types = [keyword for keyword in keywords if not isinstance(keyword, str)]
    keywords = [keyword.strip() for keyword in keywords if isinstance(keyword, str) and keyword.strip()]
    if question_type != "dung_sai":
        # Keywords are optional highlighting metadata for non true/false
        # questions. Keep only grounded values instead of rejecting an
        # otherwise traceable question because the model returned noisy tags.
        keywords = [
            keyword
            for keyword in keywords
            if contains_exact_text(source_context, keyword)
        ][:MAX_SOURCE_KEYWORDS]
        item["source_keywords"] = keywords
        invalid_keyword_types = []
    if invalid_keyword_types:
        errors.append(
            _rejection(
                "SOURCE_KEYWORDS_INVALID",
                "source_keywords chỉ được chứa chuỗi không rỗng.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if question_type == "dung_sai" and not keywords:
        errors.append(
            _rejection(
                "SOURCE_KEYWORDS_MISSING",
                "Câu Đúng/Sai phải khai báo ít nhất một keyword neo từ nguồn.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if question_type == "dung_sai" and len(keywords) > MAX_SOURCE_KEYWORDS:
        errors.append(
            _rejection(
                "TOO_MANY_SOURCE_KEYWORDS",
                f"Chỉ cho phép tối đa {MAX_SOURCE_KEYWORDS} keyword nguồn.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )

    for keyword in keywords[:MAX_SOURCE_KEYWORDS]:
        if not contains_exact_text(source_context, keyword):
            errors.append(
                _rejection(
                    "KEYWORD_NOT_IN_EVIDENCE",
                    f"Keyword '{keyword}' không xuất hiện chính xác trong source_context.",
                    item=item,
                    candidate_index=candidate_index,
                    repairable=False,
                )
            )
        elif question_type == "dung_sai" and not contains_exact_text(statement, keyword):
            errors.append(
                _rejection(
                    "KEYWORD_NOT_IN_STATEMENT",
                    f"Keyword '{keyword}' chưa được dùng nguyên dạng trong mệnh đề Đúng/Sai.",
                    item=item,
                    candidate_index=candidate_index,
                    repairable=True,
                )
            )

    if question_type == "dung_sai":
        answer = str(item.get("correct_answer") or "").strip()
        mutation = item.get("false_mutation")
        if answer == "B":
            if not isinstance(mutation, dict):
                errors.append(
                    _rejection(
                        "FALSE_MUTATION_MISSING",
                        "Câu Sai phải mô tả một phép biến đổi có kiểm soát từ evidence.",
                        item=item,
                        candidate_index=candidate_index,
                        repairable=True,
                    )
                )
            else:
                original = str(mutation.get("original") or "").strip()
                replacement = str(mutation.get("replacement") or "").strip()
                field = str(mutation.get("field") or "").strip()
                if not field or not original or not replacement or normalize_exact_text(original) == normalize_exact_text(replacement):
                    errors.append(
                        _rejection(
                            "FALSE_MUTATION_INVALID",
                            "false_mutation phải có field, original và replacement khác nhau.",
                            item=item,
                            candidate_index=candidate_index,
                            repairable=True,
                        )
                    )
                else:
                    if not contains_exact_text(source_context, original):
                        errors.append(
                            _rejection(
                                "MUTATION_ORIGINAL_NOT_IN_EVIDENCE",
                                "Giá trị original của câu Sai không có trong source_context.",
                                item=item,
                                candidate_index=candidate_index,
                                repairable=False,
                            )
                        )
                    if not contains_exact_text(statement, replacement):
                        errors.append(
                            _rejection(
                                "MUTATION_REPLACEMENT_NOT_IN_STATEMENT",
                                "Giá trị replacement của câu Sai không có trong mệnh đề.",
                                item=item,
                                candidate_index=candidate_index,
                                repairable=True,
                            )
                        )
        elif mutation not in (None, {}):
            errors.append(
                _rejection(
                    "TRUE_STATEMENT_HAS_MUTATION",
                    "Câu có đáp án Đúng không được khai báo false_mutation.",
                    item=item,
                    candidate_index=candidate_index,
                    repairable=True,
                )
            )

    return errors


def validate_question_code(item: dict, *, candidate_index: int) -> list[GenerationRejection]:
    question = str(item.get("question") or "")
    blocks = re.findall(r"```[^\n`]*\n([\s\S]*?)```", question)
    prose = re.sub(r"```[\s\S]*?```", "", question)
    depends_on_code = re.search(
        r"(?:đoạn\s+(?:mã|code)|mã nguồn|chương trình)\s+(?:sau|dưới đây|này|trên)"
        r"|(?:cho|xét)\s+(?:đoạn\s+)?(?:mã|code|chương trình)"
        r"|(?:kết quả|đầu ra|tìm lỗi|thực thi|chạy)\b[^.?!\n]{0,80}\bđoạn\s+(?:mã|code)",
        prose,
        flags=re.IGNORECASE,
    )
    if depends_on_code and (not blocks or not any(block.strip() for block in blocks)):
        return [_rejection(
            "QUESTION_CODE_MISSING",
            "Câu hỏi tham chiếu mã nhưng thiếu khối mã trong question. Kèm mã nguồn đủ dữ kiện "
            "trong khối ```, tối đa 12 dòng không trống và 800 ký tự; không thay bằng source_context.",
            item=item, candidate_index=candidate_index, repairable=True,
        )]
    code = "\n".join(block.strip() for block in blocks)
    if len(code) > MAX_QUESTION_CODE_CHARS or sum(bool(line.strip()) for line in code.splitlines()) > MAX_QUESTION_CODE_LINES:
        return [_rejection(
            "QUESTION_CODE_TOO_LONG",
            "Rút đoạn mã về tối đa 12 dòng không trống và 800 ký tự, vẫn giữ đủ dữ kiện để trả lời.",
            item=item, candidate_index=candidate_index, repairable=True,
        )]
    return []


def validate_true_false_clarity(
    item: dict,
    *,
    candidate_index: int,
) -> list[GenerationRejection]:
    prose = re.sub(r"```[\s\S]*?```", "", str(item.get("question") or ""))
    statement = re.sub(r"\s+", " ", prose).strip()
    normalized = normalize_exact_text(statement)
    errors: list[GenerationRejection] = []

    if len(statement) > MAX_TRUE_FALSE_LENGTH:
        errors.append(
            _rejection(
                "STATEMENT_TOO_LONG",
                f"Mệnh đề Đúng/Sai vượt quá {MAX_TRUE_FALSE_LENGTH} ký tự.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if statement.endswith("?"):
        errors.append(
            _rejection(
                "STATEMENT_IS_QUESTION",
                "Dạng Đúng/Sai phải là mệnh đề, không phải câu nghi vấn.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if ";" in statement or re.search(r"\b(đồng thời|trong khi|tuy nhiên|nhưng)\b", normalized):
        errors.append(
            _rejection(
                "MULTIPLE_PROPOSITIONS",
                "Mệnh đề có dấu hiệu gộp nhiều nhận định độc lập.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if len(re.findall(r"\b(không|chưa|chẳng)\b", normalized)) >= 2:
        errors.append(
            _rejection(
                "DOUBLE_NEGATION",
                "Mệnh đề chứa nhiều lớp phủ định và có thể gây mơ hồ.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    if re.search(
        r"\b(theo tài liệu|theo giáo trình|nội dung trên|đoạn trên|điều này|như đã nêu|ở trên)\b",
        normalized,
    ):
        errors.append(
            _rejection(
                "CONTEXT_DEPENDENT_STATEMENT",
                "Mệnh đề phụ thuộc vào cách gọi nguồn hoặc ngữ cảnh bên ngoài.",
                item=item,
                candidate_index=candidate_index,
                repairable=True,
            )
        )
    return errors


def rejection_counts(rejections: Iterable[GenerationRejection]) -> dict[str, int]:
    counts = {"format": 0, "grounding": 0, "clarity": 0}
    for rejection in rejections:
        if rejection.code.startswith(("SOURCE_", "KEYWORD_", "FALSE_", "MUTATION_", "TRUE_STATEMENT_")):
            counts["grounding"] += 1
        elif rejection.code in {
            "STATEMENT_TOO_LONG",
            "STATEMENT_IS_QUESTION",
            "MULTIPLE_PROPOSITIONS",
            "DOUBLE_NEGATION",
            "CONTEXT_DEPENDENT_STATEMENT",
            "QUESTION_CODE_MISSING",
            "QUESTION_CODE_TOO_LONG",
        }:
            counts["clarity"] += 1
        else:
            counts["format"] += 1
    return counts
