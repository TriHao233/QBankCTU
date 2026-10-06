"""Permission and lifecycle rules for Contact requests."""

from unittest.mock import Mock

import pytest
from bson import ObjectId

from core.dependencies import CurrentUser
from modules.contact.schemas import ContactRequestUpdate, ContactResponseSubmit, ContactStatus
from modules.contact.service import ContactConflictError, ContactService


TEACHER_ID = ObjectId()
ADMIN_ID = ObjectId()


def user(user_id, role):
    return CurrentUser(id=user_id, firebase_uid="test", email="test@example.com",
                       role=role, is_active=True)


def ticket(**changes):
    data = {"id": str(ObjectId()), "user_id": str(TEACHER_ID),
            "category": "BUG", "title": "Lỗi", "content": "Mô tả lỗi",
            "status": "NEW", "message_count": 0, "deleted_at": None,
            "updated_at": object()}
    return {**data, **changes}


@pytest.fixture
def service():
    instance = ContactService()
    instance.repo = Mock()
    return instance


def test_teacher_can_edit_only_own_unanswered_new_ticket(service):
    record = ticket()
    service.repo.find_request.return_value = record
    service.repo.edit_request.return_value = {**record, "title": "Đã sửa"}
    result = service.update_request(record["id"], ContactRequestUpdate(title=" Đã sửa "),
                                    user(TEACHER_ID, "Teacher"))
    assert result["title"] == "Đã sửa"
    assert service.repo.edit_request.call_args.args[1] == {"title": "Đã sửa"}


@pytest.mark.parametrize("changes", [{"status": "IN_PROGRESS"}, {"message_count": 1},
                                      {"deleted_at": object()}])
def test_teacher_cannot_edit_after_processing(service, changes):
    record = ticket(**changes)
    service.repo.find_request.return_value = record
    with pytest.raises(ContactConflictError):
        service.update_request(record["id"], ContactRequestUpdate(title="Sửa"),
                               user(TEACHER_ID, "Teacher"))
    service.repo.edit_request.assert_not_called()


def test_admin_can_reclassify_but_cannot_rewrite_teacher_content(service):
    record = ticket(status="IN_PROGRESS")
    service.repo.find_request.return_value = record
    service.repo.edit_request.return_value = {**record, "category": "SUPPORT"}
    service.update_request(record["id"], ContactRequestUpdate(category="SUPPORT"),
                           user(ADMIN_ID, "Admin"))
    assert service.repo.edit_request.call_args.kwargs["admin_reclassify"] is True
    with pytest.raises(PermissionError):
        service.update_request(record["id"], ContactRequestUpdate(title="Đổi lời người gửi"),
                               user(ADMIN_ID, "Admin"))


def test_only_owner_can_withdraw_active_ticket(service):
    record = ticket()
    service.repo.find_request.return_value = record
    service.repo.withdraw_request.return_value = {**record, "status": "WITHDRAWN"}
    assert service.withdraw_request(record["id"], user(TEACHER_ID, "Teacher"))["status"] == "WITHDRAWN"
    with pytest.raises(PermissionError):
        service.withdraw_request(record["id"], user(ADMIN_ID, "Admin"))


def test_delete_is_reversible_and_active_answered_ticket_is_protected(service):
    record = ticket(status="IN_PROGRESS", message_count=1)
    service.repo.find_request.return_value = record
    with pytest.raises(ContactConflictError):
        service.delete_request(record["id"], user(TEACHER_ID, "Teacher"))
    record = ticket(status="WITHDRAWN", message_count=1)
    service.repo.find_request.return_value = record
    service.repo.delete_request.return_value = {**record, "deleted_at": object()}
    assert service.delete_request(record["id"], user(TEACHER_ID, "Teacher"))["deleted_at"]
    service.repo.find_request.return_value = {**record, "deleted_at": object()}
    service.repo.restore_request.return_value = record
    assert service.restore_request(record["id"], user(TEACHER_ID, "Teacher"))["deleted_at"] is None


def test_admin_cannot_change_withdrawn_status(service):
    record = ticket(status="WITHDRAWN")
    service.repo.find_request.return_value = record
    with pytest.raises(ContactConflictError):
        service.update_status(record["id"], ContactStatus.IN_PROGRESS, user(ADMIN_ID, "Admin"))


def test_status_change_alone_does_not_send_email(service):
    record = ticket()
    service.repo.find_request.return_value = record
    service.repo.update_status.return_value = {**record, "status": "IN_PROGRESS"}
    service._notify_status = Mock()
    service._email_user_status = Mock()

    service.update_status(record["id"], ContactStatus.IN_PROGRESS, user(ADMIN_ID, "Admin"))

    service._email_user_status.assert_not_called()


@pytest.mark.parametrize("message", [None, "  Đã kiểm tra yêu cầu  "])
def test_admin_sends_one_email_when_submitting_status_with_optional_message(service, message):
    record = ticket()
    service.repo.find_request.return_value = record
    service.repo.submit_response.return_value = {**record, "status": "RESOLVED"}
    service._notify_status = Mock()
    service._notify_reply = Mock()
    service._email_user_status = Mock()
    service._email_user_reply = Mock()

    result = service.respond_to_request(
        record["id"],
        ContactResponseSubmit(status=ContactStatus.RESOLVED, message=message),
        user(ADMIN_ID, "Admin"),
    )

    assert result["status"] == "RESOLVED"
    assert service.repo.submit_response.call_args.args[2:4] == (
        "RESOLVED", message.strip() if message else None,
    )
    if message:
        service._email_user_reply.assert_called_once()
        service._email_user_status.assert_not_called()
    else:
        service._email_user_status.assert_called_once()
        service._email_user_reply.assert_not_called()


def test_reviewer_can_open_support_ticket(service, monkeypatch):
    from modules.contact.schemas import ContactRequestCreate

    monkeypatch.setattr(service, "_email_admin_new_ticket", lambda *args, **kwargs: None)
    service.repo.create_request.side_effect = lambda record: record
    service.repo.next_ticket_sequence.return_value = 7
    reviewer_id = ObjectId()
    created = service.create_request(
        ContactRequestCreate(category="SUPPORT", title=" Cần hỗ trợ ", content="Không mở được hàng duyệt"),
        user(reviewer_id, "Reviewer"),
    )
    assert created["user_id"] == str(reviewer_id)
    assert created["title"] == "Cần hỗ trợ"
