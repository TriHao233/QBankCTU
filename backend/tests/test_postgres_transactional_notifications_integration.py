"""Run with RUN_POSTGRES_INTEGRATION=1 and POSTGRES_DSN on a test database."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bson import ObjectId

from core.config import settings
from core.dependencies import CurrentUser
from core.postgres import postgres_connection
from modules.notifications.service import NotificationService
from modules.questions.postgres_repository import PostgresQuestionRepository
from modules.questions.workflow_schemas import (
    QuestionCommentCreateRequest, ReviewAssignmentRequest, ReviewCreateRequest,
)
from modules.questions.workflow_service import QuestionWorkflowService, notification_outbox
from modules.users.postgres_repository import PostgresUserRepository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1",
    reason="Requires an explicitly configured PostgreSQL test database",
)


def _actor(user: dict, role: str) -> CurrentUser:
    return CurrentUser(id=user["_id"], firebase_uid="", email=user["email"], role=role,
                       is_active=True, permissions=("reviews.manage",))


def _kinds(user_id) -> list[str]:
    with postgres_connection() as conn:
        rows = conn.execute("SELECT kind FROM notifications WHERE recipient_user_id=%s "
                            "ORDER BY created_at, id", (str(user_id),)).fetchall()
    return [row["kind"] for row in rows]


def test_notifications_commit_and_roll_back_with_the_question_change(monkeypatch):
    for key in ("user_store", "catalog_store", "question_store", "notification_store",
                "review_policy_store", "audit_store"):
        monkeypatch.setattr(settings, key, "postgres")
    suffix = uuid4().hex[:12]
    users = PostgresUserRepository()
    teacher, reviewer, admin = (
        users.create({"firebase_uid": f"{role}-{suffix}", "email": f"{role}-{suffix}@example.test",
                      "display_name": role, "role": role})
        for role in ("Teacher", "Reviewer", "Admin")
    )
    admin_actor, reviewer_actor = _actor(admin, "Admin"), _actor(reviewer, "Reviewer")
    now = datetime.now(timezone.utc)
    question_id, version_id = ObjectId(), ObjectId()
    repository = PostgresQuestionRepository()
    try:
        repository.create({
            "_id": question_id, "schema_version": 2, "question_code": f"TN-{suffix}",
            "created_by_user_id": teacher["_id"], "current_version": 1,
            "current_version_id": version_id, "approved_version_id": None,
            "lifecycle_status": "ACTIVE", "review_status": "PENDING",
            "evaluation_status": "NOT_STARTED", "publication_status": "NOT_PUBLISHED",
            "review_assignment": {"status": "UNASSIGNED"}, "quality_summary": {},
            "review_submission": {"submitted_at": now - timedelta(hours=1)},
            "created_at": now, "updated_at": now,
        }, {
            "_id": version_id, "question_id": question_id, "version": 1, "origin": "MANUAL",
            "content": "Câu hỏi thông báo", "question_data": {}, "classification": {},
            "clos": [], "sources": [], "content_hash": f"h-{suffix}",
            "created_by_user_id": teacher["_id"], "created_at": now,
        })
        workflow = QuestionWorkflowService(SimpleNamespace())
        assert notification_outbox() == []

        # A notification that cannot be stored rolls the assignment back with it.
        good: list[dict] = []
        NotificationService(None, sink=good).notify_review_assigned(
            question={"_id": question_id, "question_code": "x"},
            version={"_id": version_id}, reviewer_user_id=reviewer["_id"],
            actor_user_id=admin["_id"])
        broken = [*good, {**good[0], "_id": ObjectId(), "type": None}]
        assignment = {"status": "ASSIGNED", "reviewer_user_id": reviewer["_id"],
                      "lock_expires_at": now + timedelta(hours=1)}
        with pytest.raises(Exception):
            repository.assign_review(question_id, expected_version_id=version_id,
                                     assignment=assignment, actor_user_id=admin["_id"],
                                     actor_role="Admin", action="QUESTION_REVIEW_ASSIGNED",
                                     now=now, notifications=broken)
        assert repository.find_pair(question_id)[0]["review_assignment"]["status"] == "UNASSIGNED"
        assert _kinds(reviewer["_id"]) == []

        # Recipients that no longer exist are skipped instead of failing the change.
        ghost = {**good[0], "_id": ObjectId(), "recipient_user_id": ObjectId()}
        repository.assign_review(question_id, expected_version_id=version_id,
                                 assignment=assignment, actor_user_id=admin["_id"],
                                 actor_role="Admin", action="QUESTION_REVIEW_ASSIGNED",
                                 now=now, notifications=[ghost])
        assert repository.find_pair(question_id)[0]["review_assignment"]["status"] == "ASSIGNED"

        # Workflow paths write their notifications inside the same transaction.
        workflow.assign_review(str(question_id),
                               ReviewAssignmentRequest(reviewer_user_id=str(reviewer["_id"])),
                               admin_actor)
        assert _kinds(reviewer["_id"]) == ["QUESTION_REVIEW_ASSIGNED"]
        workflow.add_comment(str(question_id), QuestionCommentCreateRequest(
            body="Xem giúp", mention_user_ids=[str(teacher["_id"])]), reviewer_actor)
        assert _kinds(teacher["_id"]) == ["QUESTION_MENTION"]
        workflow.claim_review(str(question_id), reviewer_actor)
        # Keep recipient assertions independent of other active test/demo users.
        monkeypatch.setattr("modules.questions.workflow_service.review_candidate_users",
                            lambda *_args, **_kwargs: [teacher, reviewer, admin])
        workflow.review(str(question_id),
                        ReviewCreateRequest(expected_version=1, decision="APPROVED", secondary_required=True),
                        reviewer_actor)
        assert _kinds(teacher["_id"]) == ["QUESTION_MENTION", "QUESTION_SECONDARY_REVIEW_PENDING"]
        assert "QUESTION_SECONDARY_REVIEW_AVAILABLE" in _kinds(admin["_id"])
        assert "QUESTION_SECONDARY_REVIEW_AVAILABLE" not in _kinds(reviewer["_id"])
        workflow.claim_review(str(question_id), admin_actor)
        workflow.review(str(question_id), ReviewCreateRequest(expected_version=1, decision="APPROVED"), admin_actor)
        assert _kinds(teacher["_id"]) == ["QUESTION_MENTION", "QUESTION_SECONDARY_REVIEW_PENDING", "QUESTION_APPROVED"]
        # A conflicting second decision fails before anything is written.
        with pytest.raises(Exception):
            workflow.review(str(question_id),
                            ReviewCreateRequest(expected_version=1, decision="REJECTED"),
                            reviewer_actor)
        assert _kinds(teacher["_id"]) == ["QUESTION_MENTION", "QUESTION_SECONDARY_REVIEW_PENDING", "QUESTION_APPROVED"]
    finally:
        user_ids = [str(teacher["_id"]), str(reviewer["_id"]), str(admin["_id"])]
        with postgres_connection() as conn:
            conn.execute("DELETE FROM notifications WHERE recipient_user_id=ANY(%s)", (user_ids,))
            conn.execute("DELETE FROM audit_logs WHERE entity_id=%s OR actor_user_id=ANY(%s)",
                         (str(question_id), user_ids))
            for table in ("question_comments", "question_reviews", "question_review_drafts"):
                conn.execute(f"DELETE FROM {table} WHERE question_id=%s", (str(question_id),))
            conn.execute("UPDATE questions SET current_version_id=NULL WHERE id=%s",
                         (str(question_id),))
            conn.execute("DELETE FROM question_versions WHERE question_id=%s", (str(question_id),))
            conn.execute("DELETE FROM questions WHERE id=%s", (str(question_id),))
            conn.execute("DELETE FROM users WHERE id=ANY(%s)", (user_ids,))


def test_resubmission_notifies_previous_reviewer_with_the_status_change(monkeypatch):
    from db.copy_business_data import projected_rows, upsert
    from modules.questions.repository import MongoQuestionReferenceRepository
    from modules.questions.router import submit_question_for_review
    from modules.questions.schemas import QuestionSubmitRequest
    from modules.questions.service import QuestionService

    for key in ("user_store", "catalog_store", "question_store", "notification_store",
                "audit_store"):
        monkeypatch.setattr(settings, key, "postgres")
    suffix = uuid4().hex[:12]
    users = PostgresUserRepository()
    teacher, reviewer = (
        users.create({"firebase_uid": f"r{role}-{suffix}", "email": f"r{role}-{suffix}@example.test",
                      "display_name": role, "role": role})
        for role in ("Teacher", "Reviewer")
    )
    now = datetime.now(timezone.utc)
    question_id, version_id, review_id = ObjectId(), ObjectId(), ObjectId()
    repository = PostgresQuestionRepository()
    try:
        repository.create({
            "_id": question_id, "schema_version": 2, "question_code": f"RS-{suffix}",
            "created_by_user_id": teacher["_id"], "current_version": 1,
            "current_version_id": version_id, "approved_version_id": None,
            "lifecycle_status": "ACTIVE", "review_status": "NEEDS_REVISION",
            "evaluation_status": "NOT_STARTED", "publication_status": "NOT_PUBLISHED",
            "review_assignment": {"status": "UNASSIGNED"}, "quality_summary": {},
            "latest_review_id": review_id, "created_at": now, "updated_at": now,
        }, {
            "_id": version_id, "question_id": question_id, "version": 1, "origin": "MANUAL",
            "content": "Câu hỏi gửi lại", "question_data": {}, "classification": {},
            "clos": [], "sources": [], "content_hash": f"h-{suffix}",
            "created_by_user_id": teacher["_id"], "created_at": now,
        })
        with postgres_connection() as conn:
            for table, row in projected_rows("question_reviews", {
                "_id": review_id, "question_id": question_id, "question_version_id": version_id,
                "reviewer_user_id": reviewer["_id"], "decision": "NEEDS_REVISION",
                "reviewed_at": now,
            }):
                upsert(conn, table, row)
        service = QuestionService(repository, MongoQuestionReferenceRepository(SimpleNamespace()))
        workflow = QuestionWorkflowService(SimpleNamespace())
        monkeypatch.setattr(workflow, "enqueue_auto_evaluation", lambda *_a, **_k: {})
        author = _actor(teacher, "Teacher")

        submitted = submit_question_for_review(
            str(question_id), author, service, workflow,
            QuestionSubmitRequest(response_note="Đã giữ nguyên, đáp án đúng theo tài liệu"),
        )
        assert submitted["review_status"] == "PENDING"
        assert _kinds(reviewer["_id"]) == ["QUESTION_RESUBMITTED"]
        # Submitting an already pending question changes nothing and sends nothing.
        submit_question_for_review(str(question_id), author, service, workflow)
        assert _kinds(reviewer["_id"]) == ["QUESTION_RESUBMITTED"]
    finally:
        user_ids = [str(teacher["_id"]), str(reviewer["_id"])]
        with postgres_connection() as conn:
            conn.execute("DELETE FROM notifications WHERE recipient_user_id=ANY(%s)", (user_ids,))
            conn.execute("DELETE FROM audit_logs WHERE entity_id=%s OR actor_user_id=ANY(%s)",
                         (str(question_id), user_ids))
            conn.execute("DELETE FROM question_reviews WHERE question_id=%s", (str(question_id),))
            conn.execute("UPDATE questions SET current_version_id=NULL WHERE id=%s",
                         (str(question_id),))
            conn.execute("DELETE FROM question_versions WHERE question_id=%s", (str(question_id),))
            conn.execute("DELETE FROM questions WHERE id=%s", (str(question_id),))
            conn.execute("DELETE FROM users WHERE id=ANY(%s)", (user_ids,))
