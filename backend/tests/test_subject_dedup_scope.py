from __future__ import annotations

from types import SimpleNamespace

from bson import ObjectId

from core.config import settings
from modules.generation import mongodb as generation_store


def _capture_pipeline(monkeypatch):
    captured = {}

    class Questions:
        def aggregate(self, pipeline):
            captured["pipeline"] = pipeline
            return [{"content": "Câu ở tài liệu khác"}]

    monkeypatch.setattr(settings, "question_store", "mongo")
    monkeypatch.setattr(generation_store, "get_database", lambda: SimpleNamespace(questions=Questions()))
    return captured


def test_subject_scope_matches_questions_across_documents(monkeypatch):
    captured = _capture_pipeline(monkeypatch)
    subject_id = str(ObjectId())

    texts = generation_store.get_existing_question_texts(
        str(ObjectId()), viewer_user_id=ObjectId(), include_shared=True, subject_id=subject_id,
    )

    assert texts == ["Câu ở tài liệu khác"]
    scope_stage = captured["pipeline"][3]["$match"]
    assert "current_version.document_id" not in scope_stage
    subject_filters = scope_stage["$and"][0]["$or"]
    assert {"subject_id": {"$in": [subject_id, ObjectId(subject_id)]}} in subject_filters


def test_document_scope_is_kept_without_subject(monkeypatch):
    captured = _capture_pipeline(monkeypatch)
    document_id = ObjectId()

    generation_store.get_existing_question_texts(str(document_id), viewer_user_id=ObjectId())

    assert captured["pipeline"][3]["$match"]["current_version.document_id"] == document_id


def test_missing_scope_returns_nothing(monkeypatch):
    _capture_pipeline(monkeypatch)
    assert generation_store.get_existing_question_texts(None, viewer_user_id=ObjectId()) == []
