from bson import ObjectId
from modules.questions.workflow_service import secondary_review_recipients
from modules.notifications.service import NotificationService


def user(role="Reviewer", **fields):
    return {"_id": ObjectId(), "role": role, "is_active": True, **fields}


def test_specialists_exclude_authors_primary_inactive_and_revoked():
    subject = ObjectId()
    author, primary, specialist, general = user(), user(), user(review_subject_ids=[subject]), user()
    inactive = user(is_active=False, review_subject_ids=[subject])
    revoked = user(permission_revokes=["reviews.manage"], review_subject_ids=[subject])
    question = {"subject_id": subject, "created_by_user_id": author["_id"]}
    recipients = secondary_review_recipients([author, primary, specialist, general, inactive, revoked],
        question=question, version={}, primary_reviewer_user_id=primary["_id"])
    assert recipients == [specialist["_id"]]


def test_fallback_to_admins_and_limit():
    subject = ObjectId()
    author, primary, admin = user(), user(), user("Admin")
    general = user(review_subject_ids=[subject])
    unassigned = user()
    kwargs = {"question": {"subject_id": subject, "created_by_user_id": author["_id"]}, "version": {},
              "primary_reviewer_user_id": primary["_id"]}
    assert secondary_review_recipients([author, primary, general, admin], **kwargs) == [general["_id"]]
    assert secondary_review_recipients([author, primary, unassigned, admin], **kwargs) == [admin["_id"]]
    assert secondary_review_recipients([author, primary], **kwargs) == []
    assert len(secondary_review_recipients([user(review_subject_ids=[subject]) for _ in range(30)], **kwargs)) == 20
    editor = user(review_subject_ids=[subject])
    assert secondary_review_recipients([editor], **{**kwargs, "version": {"created_by_user_id": editor["_id"]}}) == []
    granted = user("Teacher", permission_grants=["reviews.manage"], review_subject_ids=[subject])
    assert secondary_review_recipients([granted], **kwargs) == [granted["_id"]]


def test_notification_sink_collects_outbox_without_writes_and_deduplicates():
    author, primary, recipient = ObjectId(), ObjectId(), ObjectId()
    question, version = {"_id": ObjectId(), "created_by_user_id": author, "question_code": "Q-2"}, {"_id": ObjectId()}
    outbox = []
    NotificationService(None, sink=outbox).notify_secondary_review_available(question=question, version=version,
        primary_reviewer_user_id=primary, actor_user_id=primary, recipients=[author, primary, recipient, recipient])
    assert len(outbox) == 1 and outbox[0]["recipient_user_id"] == recipient
    assert outbox[0]["type"] == "QUESTION_SECONDARY_REVIEW_AVAILABLE"
    assert outbox[0]["link"] == f"/kiem-duyet?questionId={question['_id']}"
