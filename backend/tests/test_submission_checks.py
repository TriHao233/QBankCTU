import pytest
from bson import ObjectId

from modules.questions.submission_checks import (
    GroundingError,
    answer_quality_issues,
    check_grounding,
    duplicate_flag,
    evidence_texts,
    submission_schema_errors,
)

QUOTE = "Ngăn xếp hoạt động theo nguyên tắc vào sau ra trước"


def _version(**overrides):
    version = {
        "content": "Ngăn xếp hoạt động theo nguyên tắc nào?",
        "classification": {"assessment_type": "TRAC_NGHIEM"},
        "question_data": {
            "options": {"A": "LIFO", "B": "FIFO", "C": "Ngẫu nhiên", "D": "Ưu tiên"},
            "correct_answer": "A",
            "model_source_context": QUOTE,
        },
        "sources": [{"source_type": "CHUNK", "chunk_id": ObjectId()}],
    }
    version.update(overrides)
    return version


def test_schema_errors_cover_answer_and_options():
    assert submission_schema_errors(_version()) == []
    broken = _version(question_data={"options": {"A": "LIFO", "B": ""}, "correct_answer": "E"})
    errors = submission_schema_errors(broken)
    assert "Các lựa chọn không được để trống" in errors
    assert "Đáp án đúng phải là một trong các lựa chọn" in errors
    assert submission_schema_errors(_version(content=" ", question_data={"options": None}))[:2] == [
        "Nội dung câu hỏi không được để trống", "Đáp án đúng không được để trống",
    ]


MATCHING_OPTIONS = {"1": "Ngăn xếp", "2": "Hàng đợi", "3": "Cây", "a": "FIFO", "b": "LIFO", "c": "Phân cấp", "d": "Băm"}


def _structured(question_type, options, correct_answer):
    return _version(
        classification={"assessment_type": question_type},
        question_data={"options": options, "correct_answer": correct_answer},
    )


def test_matching_question_pairs_must_be_complete_unique_and_existing():
    assert submission_schema_errors(_structured("GHEP_COT", MATCHING_OPTIONS, "1-b, 2-a, 3-c")) == []
    errors = submission_schema_errors(_structured("GHEP_COT", MATCHING_OPTIONS, "1-b, 1-a, 4-e"))
    assert "ghep_cot: mục số '1' được ghép nhiều lần" in errors
    assert "ghep_cot: đáp án tham chiếu mục số '4' không tồn tại" in errors
    assert "ghep_cot: đáp án tham chiếu mục chữ 'e' không tồn tại" in errors
    assert "ghep_cot: thiếu cặp ghép cho mục số 2, 3" in errors
    reused = submission_schema_errors(_structured("GHEP_COT", MATCHING_OPTIONS, "1-b, 2-b, 3-c"))
    assert "ghep_cot: mục chữ 'b' được ghép cho nhiều mục số" in reused
    duplicated = submission_schema_errors(
        _structured("GHEP_COT", {**MATCHING_OPTIONS, "d": "fifo"}, "1-b, 2-a, 3-c")
    )
    assert "ghep_cot: có các mục trùng nội dung trong cùng một cột" in duplicated


def test_ordering_question_steps_must_be_complete_and_not_repeated():
    steps = {"A": "Khởi tạo", "B": "So sánh", "C": "Hoán đổi", "D": "Kết thúc"}
    assert submission_schema_errors(_structured("SAP_XEP", steps, "A, B, C, D")) == []
    errors = submission_schema_errors(_structured("SAP_XEP", steps, "A, B, B, E"))
    assert "sap_xep: đáp án tham chiếu bước không tồn tại: E" in errors
    assert "sap_xep: bước bị lặp trong đáp án: B" in errors
    assert "sap_xep: đáp án thiếu bước C, D" in errors
    duplicate_steps = submission_schema_errors(_structured("SAP_XEP", {**steps, "D": "khởi tạo"}, "A, B, C, D"))
    assert "sap_xep: có các bước trùng nội dung" in duplicate_steps


def test_ai_question_needs_a_valid_source_containing_the_quote():
    chunk = {"content": f"Mở đầu. {QUOTE}. Kết thúc."}
    result = check_grounding(_version(), grounding_required=True, find_chunk=lambda _id: chunk)
    assert result["status"] == "verified"

    with pytest.raises(GroundingError, match="GROUNDING_REQUIRED"):
        check_grounding(_version(), grounding_required=True, find_chunk=lambda _id: None)
    with pytest.raises(GroundingError, match="GROUNDING_FAILED"):
        check_grounding(_version(), grounding_required=True, find_chunk=lambda _id: {"content": "khác hẳn"})


def test_manual_question_only_gets_flags():
    weak = check_grounding(_version(), grounding_required=False, find_chunk=lambda _id: {"content": "khác hẳn"})
    assert weak["status"] == "weak" and weak["warnings"]
    none = check_grounding(_version(sources=[]), grounding_required=False, find_chunk=lambda _id: None)
    assert none["status"] == "none"


def test_duplicate_flag_levels():
    content = "Ngăn xếp hoạt động theo nguyên tắc nào?"
    assert duplicate_flag(content, ["ngăn xếp hoạt động theo nguyên tắc nào"]) == "duplicate_suspect"
    assert duplicate_flag(content, ["Hàng đợi dùng cấu trúc gì?"]) is None


def _choice(options, answer="A", quote=QUOTE, question_type="TRAC_NGHIEM"):
    return {
        "classification": {"assessment_type": question_type},
        "question_data": {"options": options, "correct_answer": answer, "model_source_context": quote},
    }


def _flags(version, evidence=(QUOTE,)):
    return [flag for flag, _message in answer_quality_issues(version, evidence)]


def test_answer_supported_by_source_has_no_issue():
    version = _choice({"A": "Vào sau ra trước", "B": "Vào trước ra trước", "C": "Ngẫu nhiên", "D": "Ưu tiên"})
    assert _flags(version) == []


def test_answer_without_source_support_is_flagged():
    version = _choice({"A": "Truy cập ngẫu nhiên theo chỉ số", "B": "Vào sau ra trước"})
    assert "answer_not_in_source" in _flags(version)
    assert _flags(version, evidence=()) == ["distractor_in_evidence"]


def test_distractor_equal_to_answer_and_duplicate_options_are_flagged():
    same_as_answer = _choice({"A": "Vào sau ra trước", "B": "vào sau, ra trước", "C": "Ưu tiên"})
    assert "distractor_equals_answer" in _flags(same_as_answer)
    duplicate = _choice({"A": "Vào sau ra trước", "B": "Ưu tiên", "C": "ưu tiên"})
    assert _flags(duplicate) == ["duplicate_options"]


def test_quote_proving_a_distractor_instead_of_the_answer_is_flagged():
    version = _choice({"A": "Nguyên tắc ưu tiên", "B": "vào sau ra trước"}, quote=QUOTE)
    assert "distractor_in_evidence" in _flags(version, evidence=(QUOTE, "Nguyên tắc ưu tiên được dùng ở heap"))


def test_fill_in_answer_must_appear_in_source_and_other_types_are_skipped():
    fill = {"classification": {"assessment_type": "DIEN_KHUYET"}, "question_data": {"correct_answer": "vào sau"}}
    assert _flags(fill) == []
    fill["question_data"]["correct_answer"] = "hàng đợi vòng"
    assert _flags(fill) == ["answer_not_in_source"]
    match = {"classification": {"assessment_type": "GHEP_COT"}, "question_data": {"options": {"A": "x"}}}
    assert _flags(match) == []


def test_evidence_texts_skip_removed_chunks():
    removed, kept = ObjectId(), ObjectId()
    chunks = {removed: {"content": "đã gỡ", "content_removed": True}, kept: {"content": "còn hiệu lực"}}
    version = {
        "question_data": {"model_source_context": QUOTE},
        "sources": [
            {"source_type": "CHUNK", "chunk_id": removed},
            {"source_type": "CHUNK", "chunk_id": kept},
            {"source_type": "MANUAL_EXCERPT", "context_excerpt": "trích tay"},
        ],
    }
    assert evidence_texts(version, chunks.get) == [QUOTE, "còn hiệu lực", "trích tay"]
