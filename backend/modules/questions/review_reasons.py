"""Standard reasons a reviewer cites when rejecting a question or asking for revision."""

from __future__ import annotations

REVIEW_REASON_CATALOG: tuple[dict, ...] = (
    {"code": "FACTUAL_ERROR", "label": "Sai kiến thức hoặc sai sự thật"},
    {"code": "WRONG_ANSWER", "label": "Đáp án sai hoặc không duy nhất"},
    {"code": "AMBIGUOUS_STEM", "label": "Đề bài mơ hồ, khó hiểu"},
    {"code": "WEAK_DISTRACTORS", "label": "Phương án nhiễu yếu hoặc lộ đáp án"},
    {"code": "NOT_GROUNDED", "label": "Không bám tài liệu nguồn"},
    {"code": "OUT_OF_SCOPE", "label": "Ngoài phạm vi học phần/chương"},
    {"code": "BLOOM_MISMATCH", "label": "Sai mức Bloom hoặc độ khó"},
    {"code": "CLO_MISMATCH", "label": "Không khớp chuẩn đầu ra (CLO)"},
    {"code": "DUPLICATE", "label": "Trùng với câu hỏi đã có"},
    {"code": "FORMAT_ERROR", "label": "Lỗi cấu trúc hoặc trình bày"},
    {"code": "LANGUAGE_ERROR", "label": "Lỗi chính tả, ngữ pháp"},
    {"code": "OTHER", "label": "Lý do khác (ghi rõ trong nhận xét)"},
)

REVIEW_REASON_CODES = frozenset(item["code"] for item in REVIEW_REASON_CATALOG)
DECISIONS_REQUIRING_REASON = frozenset({"REJECTED", "NEEDS_REVISION"})


def review_reason_errors(decision: str, reason_codes: list[str], *, has_note: bool) -> list[str]:
    """Validation messages for the reason codes of a review decision; empty when valid."""
    unknown = sorted({code for code in reason_codes if code not in REVIEW_REASON_CODES})
    if unknown:
        return ["Mã lý do không hợp lệ: " + ", ".join(unknown)]
    if decision not in DECISIONS_REQUIRING_REASON:
        return []
    if not reason_codes:
        return ["Cần chọn ít nhất một lý do theo danh mục chuẩn"]
    if "OTHER" in reason_codes and not has_note:
        return ["Lý do khác cần ghi rõ trong nhận xét"]
    return []


__all__ = [
    "DECISIONS_REQUIRING_REASON",
    "REVIEW_REASON_CATALOG",
    "REVIEW_REASON_CODES",
    "review_reason_errors",
]
