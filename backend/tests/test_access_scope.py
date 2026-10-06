from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import ObjectId

from modules.documents.service import DocumentService
from modules.questions.workflow_service import QuestionWorkflowService
from modules.users.store import SubjectMembershipError, ensure_teaches_subjects


class FakeQuestions:
    def __init__(self, versions):
        self.versions = versions

    def list_versions(self, _question_id):
        return self.versions


def _service(versions=()):
    service = object.__new__(QuestionWorkflowService)
    service.db = None
    service.questions = FakeQuestions(list(versions))
    return service


def _user(role="Reviewer"):
    return SimpleNamespace(id=ObjectId(), role=role)


def test_authors_of_every_version_and_submitter_cannot_review():
    creator, old_editor, submitter, current_editor = ObjectId(), ObjectId(), ObjectId(), ObjectId()
    question = {"_id": ObjectId(), "created_by_user_id": creator,
                "review_submission": {"submitted_by_user_id": submitter}}
    version = {"created_by_user_id": current_editor}
    service = _service([{"created_by_user_id": old_editor}, version])

    assert service._review_author_ids(question, version) == {
        str(creator), str(old_editor), str(submitter), str(current_editor),
    }
    for author in (creator, old_editor, submitter, current_editor):
        with pytest.raises(PermissionError):
            service._ensure_not_author(question, version, SimpleNamespace(id=author, role="Reviewer"))
    service._ensure_not_author(question, version, _user())


def test_reviewer_scope_limits_subjects_and_empty_scope_covers_none():
    subject, other = ObjectId(), ObjectId()
    question = {"_id": ObjectId(), "subject_id": subject}
    service = _service()

    assert service._reviewer_covers(question, {}, {"_id": ObjectId(), "review_subject_ids": [subject]})
    assert not service._reviewer_covers(question, {}, {"_id": ObjectId(), "review_subject_ids": []})
    assert service._reviewer_covers(question, {}, {"_id": ObjectId(), "role": "Admin", "review_subject_ids": []})
    assert not service._reviewer_covers(question, {}, {"_id": ObjectId(), "review_subject_ids": [other]})
    assert service._reviewer_covers(question, {}, {"_id": ObjectId(), "role": "Admin", "review_subject_ids": [other]})

    reviewer = _user()
    with patch("modules.questions.workflow_service.find_user_record",
               return_value={"_id": reviewer.id, "review_subject_ids": [other]}):
        with pytest.raises(PermissionError):
            service._ensure_reviewer_covers(question, {}, reviewer)
    with patch("modules.questions.workflow_service.find_user_record",
               return_value={"_id": reviewer.id, "review_subject_ids": [subject]}):
        service._ensure_reviewer_covers(question, {}, reviewer)


def test_document_hidden_is_missing_but_shared_non_owner_is_forbidden():
    owner, shared_user, stranger = (
        SimpleNamespace(id=ObjectId(), role="Teacher", permissions=["questions.use_shared_bank"]) for _ in range(3)
    )
    record = {"_id": ObjectId(), "uploaded_by_user_id": owner.id, "shared_with_user_ids": [shared_user.id]}

    DocumentService._ensure_manage_access(record, owner)
    DocumentService._ensure_access(record, shared_user)
    with pytest.raises(PermissionError):
        DocumentService._ensure_manage_access(record, shared_user)
    with pytest.raises(LookupError):
        DocumentService._ensure_access(record, stranger)
    with pytest.raises(LookupError):
        DocumentService._ensure_manage_access(record, stranger)


def test_teachers_work_only_in_taught_subjects():
    subject = ObjectId()
    teacher = SimpleNamespace(id=ObjectId(), role="Teacher")
    with patch("modules.users.store.teaching_subject_ids", return_value=set()):
        with pytest.raises(SubjectMembershipError):
            ensure_teaches_subjects(None, teacher, [subject], "tạo đề thi")
        ensure_teaches_subjects(None, SimpleNamespace(id=ObjectId(), role="Admin"), [subject], "tạo đề thi")
        ensure_teaches_subjects(None, teacher, [None], "tạo đề thi")
    with patch("modules.users.store.teaching_subject_ids", return_value={str(subject)}):
        ensure_teaches_subjects(None, teacher, [subject], "tạo đề thi")
