import pytest
from bson import ObjectId

from modules.questions.service import QuestionService
from modules.questions.submission_checks import SourceNotLocatedError

QUOTE = "Hàng đợi hoạt động theo nguyên tắc vào trước ra trước"


class Repository:
    def __init__(self):
        self.created = None

    def create(self, question, version):
        self.created = (question, version)
        return question, version


class References:
    def __init__(self, document, chunks):
        self.document = document
        self.chunks = chunks

    def find_document(self, document_id):
        return self.document if document_id == self.document["_id"] else None

    def find_chunk(self, chunk_id):
        return self.chunks.get(chunk_id)

    def find_subject(self, _subject_id):
        return None


def _setup():
    document_id, chunk_set_id = ObjectId(), ObjectId()
    cited, other = ObjectId(), ObjectId()
    document = {"_id": document_id, "current_processing": {"chunk_set_id": chunk_set_id}}
    chunks = {
        cited: {"_id": cited, "document_id": document_id, "chunk_set_id": chunk_set_id,
                "content": f"Giới thiệu. {QUOTE}. Ví dụ.", "content_hash": "a"},
        other: {"_id": other, "document_id": document_id, "chunk_set_id": chunk_set_id,
                "content": "Ngăn xếp là cấu trúc LIFO.", "content_hash": "b"},
    }
    repository = Repository()
    service = QuestionService(repository, References(document, chunks))
    item = {
        "question": "Hàng đợi hoạt động theo nguyên tắc nào?",
        "question_type": "trac_nghiem",
        "bloom_level": "2_hieu",
        "difficulty": "de",
        "options": {"A": "FIFO", "B": "LIFO", "C": "Ngẫu nhiên", "D": "Ưu tiên"},
        "correct_answer": "A",
        "explanation": "Theo giáo trình.",
        "source_context": QUOTE,
        "document_id": str(document_id),
    }
    return service, repository, item, cited, other


def test_generated_draft_is_stored_as_ai_draft_citing_only_matching_chunks():
    service, repository, item, cited, other = _setup()
    owner = ObjectId()

    stored = service.create_generated_draft(
        item, created_by_user_id=owner, candidate_chunk_ids=[str(other), str(cited)],
    )

    question, version = repository.created
    assert stored["review_status"] == "DRAFT"
    assert question["grounding_required"] is True
    assert question["created_by_user_id"] == owner
    assert version["origin"] == "AI"
    assert version["classification"]["bloom"]["level"] == 2
    assert [source["chunk_id"] for source in version["sources"]] == [cited]
    assert version["question_data"]["model_source_context"] == QUOTE


def test_generated_draft_without_matching_chunk_is_rejected_and_not_stored():
    service, repository, item, cited, other = _setup()
    item["source_context"] = "Đoạn không có trong tài liệu nào cả"

    with pytest.raises(SourceNotLocatedError) as error:
        service.create_generated_draft(
            item, created_by_user_id=ObjectId(), candidate_chunk_ids=[str(cited), str(other)],
        )

    assert error.value.code == "SOURCE_NOT_LOCATED"
    assert repository.created is None


def test_unlocated_candidate_is_reported_as_rejection_by_the_generation_store(monkeypatch):
    from modules.generation import mongodb as generation_store
    from modules.questions import service as question_service_module

    service, repository, item, cited, _other = _setup()
    item["source_context"] = "Đoạn không có trong tài liệu nào cả"
    monkeypatch.setattr(question_service_module, "get_question_service", lambda: service)
    rejections = []

    saved = generation_store.save_generated_questions(
        item["document_id"], [item], generation_run_id=None, requested_by_user_id=ObjectId(),
        source_chunk_ids=[str(cited)], learning_outcomes=[], rejections=rejections,
    )

    assert saved == []
    assert [entry["code"] for entry in rejections] == ["SOURCE_NOT_LOCATED"]


def test_generated_draft_records_answer_quality_warnings():
    service, repository, item, cited, _other = _setup()
    item["options"] = {"A": "Vào trước ra trước", "B": "vào trước, ra trước", "C": "Ưu tiên", "D": "Ngẫu nhiên"}

    service.create_generated_draft(item, created_by_user_id=ObjectId(), candidate_chunk_ids=[str(cited)])

    _question, version = repository.created
    warnings = version["question_data"]["validation_warnings"]
    assert any("phương án nhiễu trùng nội dung với đáp án đúng" in warning for warning in warnings)
    assert not any("không tìm thấy căn cứ" in warning for warning in warnings)
