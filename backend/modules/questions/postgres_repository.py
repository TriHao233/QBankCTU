"""Transactional PostgreSQL question/version aggregate (runtime routing follows)."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from bson import ObjectId
from psycopg.types.json import Jsonb

from core.config import settings
from core.postgres import postgres_connection
from core.postgres_audit import write_postgres_audit_event
from db.bson_json import restore
from db.copy_business_data import projected_rows, upsert
from modules.notifications.postgres_repository import insert_notifications
from modules.questions.repository import object_id, utc_now


def _question(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = restore(row["payload"] or {})
    record.update({
        "_id": ObjectId(row["id"]), "question_code": row["question_code"],
        "subject_id": ObjectId(row["subject_id"]) if row["subject_id"] else None,
        "created_by_user_id": (ObjectId(row["created_by_user_id"])
                               if row["created_by_user_id"] else None),
        "current_version": row["current_version"],
        "current_version_id": (ObjectId(row["current_version_id"])
                               if row["current_version_id"] else None),
        "approved_version_id": (ObjectId(row["approved_version_id"])
                                if row["approved_version_id"] else None),
        "lifecycle_status": row["lifecycle_status"],
        "review_status": row["review_status"],
        "evaluation_status": row["evaluation_status"],
        "publication_status": row["publication_status"],
        "review_assignment": restore(row["assignment"] or {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    })
    return record


def _version(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = restore(row["payload"] or {})
    record.update({
        "_id": ObjectId(row["id"]), "question_id": ObjectId(row["question_id"]),
        "version": row["version"], "origin": row["origin"],
        "content": row["content"], "question_data": restore(row["question_data"]),
        "classification": restore(row["classification"]),
        "clos": restore(row["clos"]), "sources": restore(row["sources"]),
        "content_hash": row["content_hash"],
        "created_by_user_id": (ObjectId(row["created_by_user_id"])
                               if row["created_by_user_id"] else None),
        "generation_run_id": (ObjectId(row["generation_run_id"])
                              if row["generation_run_id"] else None),
        "created_at": row["created_at"],
    })
    return record


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a search term is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _review(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = restore(row["payload"] or {})
    record.update({
        "_id": ObjectId(row["id"]),
        "question_id": ObjectId(row["question_id"]),
        "question_version_id": ObjectId(row["question_version_id"]),
        "reviewer_user_id": (ObjectId(row["reviewer_user_id"])
                             if row["reviewer_user_id"] else None),
        "decision": row["decision"], "reviewed_at": row["reviewed_at"],
    })
    return record


def _evaluation(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = restore(row["result"] or {})
    record.update({
        "_id": ObjectId(row["id"]),
        "question_id": ObjectId(row["question_id"]),
        "question_version_id": ObjectId(row["question_version_id"]),
        "evaluation_job_id": (ObjectId(row["evaluation_job_id"])
                              if row["evaluation_job_id"] else None),
        "created_at": row["created_at"],
    })
    return record


def _publication(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = restore(row["payload"] or {})
    record.update({
        "_id": ObjectId(row["id"]),
        "question_id": ObjectId(row["question_id"]),
        "question_version_id": ObjectId(row["question_version_id"]),
        "publisher_user_id": (ObjectId(row["publisher_user_id"])
                              if row["publisher_user_id"] else None),
        "idempotency_key": row["idempotency_key"], "status": row["status"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    })
    return record


class PostgresQuestionRepository:
    def record_evaluation(self, evaluation: dict, *,
                          expected_version_id: ObjectId,
                          evaluation_status: str,
                          quality_summary: dict,
                          require_active_job: bool = False) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, evaluation["question_id"], lock=True)
            if not pair or pair[1]["_id"] != expected_version_id:
                raise RuntimeError("VERSION_CONFLICT")
            question, version = pair
            job_id = evaluation.get("evaluation_job_id")
            if require_active_job:
                if not job_id:
                    raise ValueError("Evaluation job bắt buộc")
                job = conn.execute(
                    """SELECT id FROM evaluation_jobs WHERE id=%s
                       AND question_id=%s AND question_version_id=%s
                       AND status='PROCESSING' FOR UPDATE""",
                    (str(job_id), str(question["_id"]), str(version["_id"])),
                ).fetchone()
                if not job:
                    from modules.questions.workflow_service import EvaluationInterruptedError
                    raise EvaluationInterruptedError("Tác vụ AI đánh giá đã bị dừng")
            for table, row in projected_rows("question_evaluations", evaluation):
                upsert(conn, table, row)
            before = question.get("quality_summary") or {}
            question.update(evaluation_status=evaluation_status,
                            quality_summary=quality_summary,
                            updated_at=evaluation["created_at"])
            self._save_question(conn, question)
            write_postgres_audit_event(
                conn, action="QUESTION_EVALUATED", entity_type="question",
                entity_id=question["_id"],
                actor_user_id=evaluation.get("requested_by_user_id"),
                before={"quality_summary": before},
                after={"quality_summary": {
                    "latest_evaluation_id": evaluation["_id"],
                    "overall_score": evaluation["scores"]["overall"],
                    "color": evaluation["color"],
                }},
                metadata={"evaluation_id": str(evaluation["_id"]),
                          "question_version_id": str(version["_id"])},
            )
            return question, version

    def __init__(self):
        if settings.user_store != "postgres" or settings.catalog_store != "postgres":
            raise RuntimeError("QUESTION_STORE=postgres requires USER_STORE and CATALOG_STORE=postgres")

    @staticmethod
    def _draft(row: dict | None) -> dict | None:
        if row is None:
            return None
        record = restore(row["payload"] or {})
        record.update({
            "_id": ObjectId(row["id"]),
            "question_id": ObjectId(row["question_id"]),
            "question_version_id": ObjectId(row["question_version_id"]),
            "reviewer_user_id": ObjectId(row["reviewer_user_id"]),
            "draft": restore(row["draft"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        })
        return record

    def get_review_draft(self, question_id: str | ObjectId,
                         reviewer_user_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT * FROM question_review_drafts
                   WHERE question_id=%s AND reviewer_user_id=%s""",
                (str(object_id(question_id, "question_id")),
                 str(object_id(reviewer_user_id, "reviewer_user_id"))),
            ).fetchone()
        return self._draft(row)

    def save_review_draft(self, question_id: str | ObjectId,
                          reviewer_user_id: ObjectId, *, expected_version: int,
                          decision: str | None, draft: dict) -> dict:
        key = str(object_id(question_id, "question_id"))
        with postgres_connection() as conn:
            pair = self._pair(conn, key, lock=True)
            if not pair:
                raise LookupError("Không tìm thấy câu hỏi")
            question, version = pair
            if question["current_version"] != expected_version:
                raise RuntimeError("VERSION_CONFLICT")
            existing = conn.execute(
                """SELECT * FROM question_review_drafts
                   WHERE question_id=%s AND reviewer_user_id=%s FOR UPDATE""",
                (key, str(reviewer_user_id)),
            ).fetchone()
            now = utc_now()
            record = self._draft(existing) if existing else {
                "_id": ObjectId(), "question_id": question["_id"],
                "reviewer_user_id": reviewer_user_id, "created_at": now,
            }
            record.update({
                "schema_version": question.get("schema_version", 2),
                "question_version_id": version["_id"],
                "question_version": version["version"],
                "decision": decision, "draft": draft, "updated_at": now,
            })
            for table, row in projected_rows("question_review_drafts", record):
                upsert(conn, table, row)
            return record

    def delete_review_draft(self, question_id: str | ObjectId,
                            reviewer_user_id: str | ObjectId) -> bool:
        with postgres_connection() as conn:
            result = conn.execute(
                """DELETE FROM question_review_drafts
                   WHERE question_id=%s AND reviewer_user_id=%s RETURNING id""",
                (str(object_id(question_id, "question_id")),
                 str(object_id(reviewer_user_id, "reviewer_user_id"))),
            ).fetchone()
        return result is not None

    @staticmethod
    def _comment(row: dict) -> dict:
        record = restore(row["payload"] or {})
        record.update({
            "_id": ObjectId(row["id"]),
            "question_id": ObjectId(row["question_id"]),
            "question_version_id": ObjectId(row["question_version_id"]),
            "author_user_id": ObjectId(row["author_user_id"]),
            "body": row["body"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "deleted_at": row["deleted_at"],
        })
        return record

    def list_comments(self, question_id: str | ObjectId) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM question_comments WHERE question_id=%s
                   AND deleted_at IS NULL ORDER BY created_at, id""",
                (str(object_id(question_id, "question_id")),),
            ).fetchall()
        return [self._comment(row) for row in rows]

    def add_comment(self, comment: dict, *, actor_role: str,
                    notifications: list[dict] | None = None) -> dict:
        with postgres_connection() as conn:
            pair = self._pair(conn, comment["question_id"], lock=True)
            if not pair or pair[1]["_id"] != comment["question_version_id"]:
                raise RuntimeError("VERSION_CONFLICT")
            for table, row in projected_rows("question_comments", comment):
                upsert(conn, table, row)
            write_postgres_audit_event(
                conn, action="QUESTION_COMMENT_ADDED", entity_type="question",
                entity_id=comment["question_id"],
                actor_user_id=comment["author_user_id"], actor_role=actor_role,
                metadata={"comment_id": str(comment["_id"]),
                          "question_version_id": str(comment["question_version_id"]),
                          "mentions": [str(item) for item in comment.get("mention_user_ids") or []]},
            )
            insert_notifications(conn, notifications)
        return comment

    def change_comment(self, question_id: str | ObjectId, comment_id: str | ObjectId,
                       *, actor_user_id: ObjectId, actor_role: str,
                       body: str | None = None, delete: bool = False) -> dict:
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT * FROM question_comments WHERE id=%s AND question_id=%s
                   AND deleted_at IS NULL FOR UPDATE""",
                (str(object_id(comment_id, "comment_id")),
                 str(object_id(question_id, "question_id"))),
            ).fetchone()
            if not row or (actor_role != "Admin" and row["author_user_id"] != str(actor_user_id)):
                raise PermissionError("Bạn chỉ có thể sửa bình luận của mình")
            comment = self._comment(row)
            now = utc_now()
            if delete:
                comment.update(body="", deleted_at=now,
                               deleted_by_user_id=actor_user_id)
                action = "QUESTION_COMMENT_DELETED"
            else:
                comment.update(body=body or "", edited_at=now)
                action = "QUESTION_COMMENT_UPDATED"
            comment["updated_at"] = now
            for table, projection in projected_rows("question_comments", comment):
                upsert(conn, table, projection)
            write_postgres_audit_event(
                conn, action=action, entity_type="question", entity_id=question_id,
                actor_user_id=actor_user_id, actor_role=actor_role,
                metadata={"comment_id": str(comment["_id"]),
                          "question_version_id": str(comment["question_version_id"])},
            )
            return comment

    def active_evaluation_job_ids(self, question_id: str | ObjectId) -> list[ObjectId]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT id FROM evaluation_jobs WHERE question_id=%s
                   AND status IN ('QUEUED','PROCESSING') ORDER BY created_at, id""",
                (str(object_id(question_id, "question_id")),),
            ).fetchall()
        return [ObjectId(row["id"]) for row in rows]

    def record_review(self, review: dict, question_fields: dict, *,
                      expected_version_id: ObjectId,
                      expected_latest_review_id: ObjectId | None,
                      actor_role: str, audit_action: str,
                      notifications: list[dict] | None = None) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, review["question_id"], lock=True)
            if not pair:
                raise RuntimeError("VERSION_CONFLICT")
            question, version = pair
            if (question["review_status"] != "PENDING"
                    or version["_id"] != expected_version_id
                    or question.get("latest_review_id") != expected_latest_review_id):
                raise RuntimeError("VERSION_CONFLICT")
            actor_id = review["reviewer_user_id"]
            if actor_role != "Admin":
                assignment = question.get("review_assignment") or {}
                expires = assignment.get("lock_expires_at")
                if (assignment.get("status") != "IN_REVIEW"
                        or assignment.get("reviewer_user_id") != actor_id
                        or (expires is not None and expires <= utc_now())):
                    raise PermissionError("Bạn cần claim câu hỏi trước khi kiểm duyệt")
            interrupted = review.get("interrupted_evaluation_job_ids") or []
            if interrupted:
                now = review["reviewed_at"]
                error = {"message": "Người duyệt đã chốt kết quả nên dừng AI đánh giá",
                         "stage": "REVIEWER_DECIDED", "at": now.isoformat()}
                expires_at = now + timedelta(days=settings.job_retention_days)
                conn.execute(
                    """UPDATE evaluation_jobs SET status='CANCELLED',
                       error=%s, lease_owner=NULL, lease_expires_at=NULL,
                       next_attempt_at=NULL, updated_at=%s,
                       payload=(payload - 'locked_by' - 'worker_id'
                                - 'lease_expires_at' - 'heartbeat_at' - 'next_attempt_at')
                               || %s
                       WHERE question_id=%s AND id=ANY(%s)
                         AND status IN ('QUEUED','PROCESSING')""",
                    (Jsonb(error), now,
                     Jsonb({"status": "CANCELLED", "error": error,
                            "finished_at": now.isoformat(),
                            "expires_at": expires_at.isoformat(),
                            "updated_at": now.isoformat()}),
                     str(question["_id"]), [str(item) for item in interrupted]),
                )
            for table, row in projected_rows("question_reviews", review):
                upsert(conn, table, row)
            previous_status = question["review_status"]
            previous_assignment = deepcopy(question.get("review_assignment") or {})
            question.update(question_fields)
            self._save_question(conn, question)
            conn.execute(
                "DELETE FROM question_review_drafts WHERE question_id=%s AND reviewer_user_id=%s",
                (str(question["_id"]), str(actor_id)),
            )
            write_postgres_audit_event(
                conn, action=audit_action, entity_type="question",
                entity_id=question["_id"], actor_user_id=actor_id,
                actor_role=actor_role,
                before={"review_status": previous_status},
                after={"review_status": question["review_status"]},
                metadata={
                    "review_id": str(review["_id"]),
                    "correlation_id": str(review["_id"]),
                    "question_version_id": str(version["_id"]),
                    # Dashboard tính thời gian duyệt từ claimed_at/assigned_at.
                    "review_assignment": previous_assignment,
                    "review_form": review.get("review_form") or {},
                    "secondary_review": question.get("secondary_review") or {},
                    "interrupted_evaluation_job_ids": [str(item) for item in interrupted],
                    "self_review_reason": review.get("self_review_reason") or "",
                },
            )
            insert_notifications(conn, notifications)
            return question, version

    def find_review(self, review_id: str | ObjectId) -> dict | None:
        with postgres_connection() as conn:
            row = conn.execute(
                "SELECT * FROM question_reviews WHERE id=%s",
                (str(object_id(review_id, "review_id")),),
            ).fetchone()
        return _review(row)

    def latest_review(self, question: dict) -> dict | None:
        with postgres_connection() as conn:
            row = None
            if question.get("latest_review_id"):
                row = conn.execute("SELECT * FROM question_reviews WHERE id=%s",
                                   (str(question["latest_review_id"]),)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM question_reviews WHERE question_id=%s "
                    "ORDER BY reviewed_at DESC, id DESC LIMIT 1", (str(question["_id"]),),
                ).fetchone()
        return _review(row)

    # ---- Reviewer queue, dashboard and history reads -------------------

    def release_reviewer_assignments(self, reviewer_user_id: ObjectId, *, reason: str,
                                     actor_user_id: ObjectId | None,
                                     actor_role: str | None, now) -> int:
        """Trả mọi câu hỏi Reviewer đang giữ về hàng đợi chung trong một transaction."""
        empty = {
            "status": "UNASSIGNED", "reviewer_user_id": None,
            "assigned_by_user_id": None, "assigned_at": None, "claimed_at": None,
            "lock_expires_at": None, "last_released_at": now, "release_reason": reason,
        }
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM questions
                   WHERE lifecycle_status='ACTIVE' AND review_status='PENDING'
                     AND assignment->>'status' IN ('ASSIGNED','IN_REVIEW')
                     AND assignment->>'reviewer_user_id'=%s
                   ORDER BY id FOR UPDATE""",
                (str(reviewer_user_id),),
            ).fetchall()
            for row in rows:
                question = _question(row)
                previous = question.get("review_assignment") or {}
                question.update(review_assignment=dict(empty), updated_at=now)
                self._save_question(conn, question)
                write_postgres_audit_event(
                    conn, action="QUESTION_REVIEW_RELEASED", entity_type="question",
                    entity_id=question["_id"], actor_user_id=actor_user_id,
                    actor_role=actor_role,
                    service_name=None if actor_user_id else "user_management",
                    before={"review_assignment": previous},
                    after={"review_assignment": empty},
                    metadata={"reason": reason, "reviewer_user_id": str(reviewer_user_id),
                              "question_version_id": str(question["current_version_id"])},
                )
        return len(rows)

    def open_review_pairs(self, now, *, question_ids: list[ObjectId] | None = None,
                          limit: int = 50) -> list[tuple[dict, dict]]:
        """Câu hỏi PENDING chưa ai giữ hoặc đã quá hạn khóa, cũ nhất trước."""
        clauses = ["""q.lifecycle_status='ACTIVE' AND q.review_status='PENDING'
            AND (q.assignment->>'status' IS NULL OR q.assignment->>'status'='UNASSIGNED'
                 OR (q.assignment->>'lock_expires_at')::timestamptz <= %s)"""]
        params: list = [now]
        if question_ids is not None:
            clauses.append("q.id=ANY(%s)")
            params.append([str(item) for item in question_ids])
        return self._pairs(
            "WHERE " + " AND ".join(clauses)
            + " ORDER BY (q.payload->'review_submission'->>'submitted_at')::timestamptz"
              " NULLS FIRST, q.id LIMIT %s",
            [*params, limit],
        )

    def count_unassigned_pending(self) -> int:
        with postgres_connection() as conn:
            row = conn.execute("""SELECT count(*) AS n FROM questions
                WHERE lifecycle_status='ACTIVE' AND review_status='PENDING'
                AND (assignment->>'status' IS NULL OR assignment->>'status'='UNASSIGNED')""").fetchone()
        return row["n"]

    def held_reviews(self, *, active_at=None) -> list[dict]:
        """Assignment đang giữ (ASSIGNED/IN_REVIEW); `active_at` lọc khóa còn hạn."""
        query = """SELECT * FROM questions
                   WHERE lifecycle_status='ACTIVE' AND review_status='PENDING'
                     AND assignment->>'status' IN ('ASSIGNED','IN_REVIEW')"""
        params: list = []
        if active_at is not None:
            query += " AND (assignment->>'lock_expires_at')::timestamptz > %s"
            params.append(active_at)
        with postgres_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_question(row) for row in rows]

    def review_workload(self, now, *, reviewer_user_id: ObjectId, sla_cutoff) -> dict:
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT count(*) AS pending,
                     count(*) FILTER (WHERE assignment->>'status' IS NULL
                                       OR assignment->>'status'='UNASSIGNED') AS unassigned,
                     count(*) FILTER (WHERE assignment->>'status'='ASSIGNED') AS assigned,
                     count(*) FILTER (WHERE assignment->>'status'='IN_REVIEW') AS in_review,
                     count(*) FILTER (WHERE assignment->>'status'='IN_REVIEW'
                       AND (assignment->>'lock_expires_at')::timestamptz <= %s) AS lock_expired,
                     count(*) FILTER (WHERE assignment->>'status' IN ('ASSIGNED','IN_REVIEW')
                       AND assignment->>'reviewer_user_id'=%s) AS mine,
                     count(*) FILTER (WHERE
                       (payload->'review_submission'->>'submitted_at')::timestamptz <= %s
                     ) AS sla_breached
                   FROM questions
                   WHERE lifecycle_status='ACTIVE' AND review_status='PENDING'""",
                (now, str(reviewer_user_id), sla_cutoff),
            ).fetchone()
        return dict(row)

    def reviews_since(self, since, *, reviewer_user_id: ObjectId | None = None) -> list[dict]:
        query = "SELECT * FROM question_reviews WHERE reviewed_at >= %s"
        params: list = [since]
        if reviewer_user_id is not None:
            query += " AND reviewer_user_id=%s"
            params.append(str(reviewer_user_id))
        with postgres_connection() as conn:
            rows = conn.execute(query + " ORDER BY reviewed_at DESC, id DESC",
                                params).fetchall()
        return [_review(row) for row in rows]

    def version_subjects(self, version_ids: list[ObjectId]) -> dict[ObjectId, dict]:
        if not version_ids:
            return {}
        with postgres_connection() as conn:
            rows = conn.execute(
                "SELECT id, classification->'subject' AS subject FROM question_versions "
                "WHERE id=ANY(%s)", ([str(item) for item in version_ids],),
            ).fetchall()
        return {ObjectId(row["id"]): restore(row["subject"] or {}, "subject")
                for row in rows}

    def latest_evaluations(self, version_ids: list[ObjectId]) -> dict[ObjectId, dict]:
        if not version_ids:
            return {}
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT ON (question_version_id) *
                   FROM question_evaluations WHERE question_version_id=ANY(%s)
                   ORDER BY question_version_id, created_at DESC, id DESC""",
                ([str(item) for item in version_ids],),
            ).fetchall()
        return {ObjectId(row["question_version_id"]): _evaluation(row) for row in rows}

    def review_decision_audits(self, since, *, actor_user_id: ObjectId | None = None
                               ) -> list[dict]:
        query = """SELECT payload, created_at FROM audit_logs
                   WHERE entity_type='question' AND created_at >= %s
                     AND action IN ('QUESTION_APPROVED','QUESTION_REJECTED',
                                    'QUESTION_NEEDS_REVISION')"""
        params: list = [since]
        if actor_user_id is not None:
            query += " AND actor_user_id=%s"
            params.append(str(actor_user_id))
        with postgres_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [{**restore(row["payload"] or {}), "created_at": row["created_at"]}
                for row in rows]

    def history(self, question_id: ObjectId, kind: str) -> list[dict]:
        key = str(object_id(question_id, "question_id"))
        with postgres_connection() as conn:
            if kind == "evaluations":
                rows = conn.execute(
                    "SELECT * FROM question_evaluations WHERE question_id=%s "
                    "ORDER BY created_at DESC, id DESC", (key,),
                ).fetchall()
                return [_evaluation(row) for row in rows]
            if kind == "publications":
                rows = conn.execute(
                    "SELECT * FROM moodle_publications WHERE question_id=%s "
                    "ORDER BY created_at DESC, id DESC", (key,),
                ).fetchall()
                return [_publication(row) for row in rows]
            rows = conn.execute(
                "SELECT * FROM question_reviews WHERE question_id=%s "
                "ORDER BY reviewed_at DESC, id DESC", (key,),
            ).fetchall()
        return [_review(row) for row in rows]

    def sla_breach_candidates(self, cutoff, limit: int) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM questions
                   WHERE lifecycle_status='ACTIVE' AND review_status='PENDING'
                     AND (payload->'review_submission'->>'submitted_at')::timestamptz <= %s
                     AND (payload->'review_sla'->>'reminded_submission_at') IS DISTINCT FROM
                         payload->'review_submission'->>'submitted_at'
                   ORDER BY (payload->'review_submission'->>'submitted_at')::timestamptz, id
                   LIMIT %s""",
                (cutoff, limit),
            ).fetchall()
        return [_question(row) for row in rows]

    def mark_sla_reminded(self, question_id: ObjectId, submitted_at, now,
                          notifications: list[dict] | None = None) -> bool:
        """Chỉ nhắc một lần cho mỗi lượt gửi duyệt, kể cả khi nhiều worker cùng chạy."""
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair or pair[0]["review_status"] != "PENDING":
                return False
            question = pair[0]
            if (question.get("review_sla") or {}).get("reminded_submission_at") == submitted_at:
                return False
            question["review_sla"] = {"reminded_submission_at": submitted_at, "reminded_at": now}
            self._save_question(conn, question)
            insert_notifications(conn, notifications)
            return True

    def record_moodle_publication(self, publication: dict, *,
                                  expected_version_id: ObjectId) -> dict:
        """Ghi publication theo idempotency key và đánh dấu PUBLISHED cùng transaction."""
        with postgres_connection() as conn:
            pair = self._pair(conn, publication["question_id"], lock=True)
            if (not pair or pair[1]["_id"] != expected_version_id
                    or pair[0]["review_status"] != "APPROVED"):
                raise RuntimeError("VERSION_CONFLICT")
            question, _version = pair
            row = conn.execute(
                "SELECT * FROM moodle_publications WHERE idempotency_key=%s FOR UPDATE",
                (publication["idempotency_key"],),
            ).fetchone()
            existing = _publication(row)
            if existing and existing.get("status") != "FAILED":
                saved = existing
            else:
                saved = dict(publication)
                if existing:
                    saved.update(_id=existing["_id"],
                                 created_at=existing.get("created_at") or publication["created_at"],
                                 attempt_no=int(existing.get("attempt_no") or 1) + 1)
                for table, projected in projected_rows("moodle_publications", saved):
                    upsert(conn, table, projected)
            if saved.get("status") == "PUBLISHED" and question["publication_status"] != "PUBLISHED":
                question.update(publication_status="PUBLISHED",
                                updated_at=publication["updated_at"])
                self._save_question(conn, question)
            return saved

    @staticmethod
    def find_publication(publication_id) -> dict | None:
        with postgres_connection() as conn:
            return _publication(conn.execute(
                "SELECT * FROM moodle_publications WHERE id=%s",
                (str(object_id(publication_id, "publication_id")),),
            ).fetchone())

    @staticmethod
    def list_publications(*, page: int, page_size: int, status: str | None,
                          site_key: str | None, search: str | None
                          ) -> tuple[list[dict], int]:
        clauses, params = [], []
        if status:
            clauses.append("status=%s")
            params.append(status)
        if site_key:
            clauses.append("payload->'target'->>'moodle_site_id'=%s")
            params.append(site_key)
        if search:
            pattern = "%" + _escape_like(search) + "%"
            clauses.append("""(external_ref_id ILIKE %s
                OR request_payload->>'question_code' ILIKE %s
                OR payload->'error'->>'message' ILIKE %s)""")
            params.extend([pattern, pattern, pattern])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with postgres_connection() as conn:
            total = conn.execute("SELECT count(*) AS n FROM moodle_publications" + where,
                                 params).fetchone()["n"]
            rows = conn.execute(
                "SELECT * FROM moodle_publications" + where
                + " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [_publication(row) for row in rows], total

    @staticmethod
    def publication_summary(site_key: str | None) -> dict:
        where, params = "", []
        if site_key:
            where, params = " WHERE payload->'target'->>'moodle_site_id'=%s", [site_key]
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT count(*) AS total,
                     count(*) FILTER (WHERE status='PUBLISHED') AS published,
                     count(*) FILTER (WHERE payload->>'publication_mode'='MOCK'
                       OR response_payload->>'publication_mode'='MOCK'
                       OR request_payload->>'mock'='true') AS simulated,
                     count(*) FILTER (WHERE status='FAILED') AS failed,
                     count(*) FILTER (WHERE status IN ('QUEUED','PROCESSING')) AS pending
                   FROM moodle_publications""" + where, params,
            ).fetchone()
        return dict(row)

    @staticmethod
    def status_summary() -> dict:
        """Số câu hỏi active theo trạng thái duyệt, xuất bản và màu chất lượng."""
        with postgres_connection() as conn:
            return dict(conn.execute(
                """SELECT count(*) AS total,
                     count(*) FILTER (WHERE review_status='DRAFT') AS draft,
                     count(*) FILTER (WHERE review_status='PENDING') AS pending,
                     count(*) FILTER (WHERE review_status='APPROVED') AS approved,
                     count(*) FILTER (WHERE review_status='NEEDS_REVISION') AS needs_revision,
                     count(*) FILTER (WHERE review_status='REJECTED') AS rejected,
                     count(*) FILTER (WHERE publication_status='PUBLISHED') AS published,
                     count(*) FILTER (WHERE payload->'quality_summary'->>'color'='GREEN') AS green,
                     count(*) FILTER (WHERE payload->'quality_summary'->>'color'='YELLOW') AS yellow,
                     count(*) FILTER (WHERE payload->'quality_summary'->>'color'='RED') AS red,
                     count(*) FILTER (WHERE payload->'quality_summary'->>'color' IS NULL)
                       AS not_evaluated
                   FROM questions WHERE lifecycle_status='ACTIVE'"""
            ).fetchone())

    @staticmethod
    def owner_counts(user_id) -> dict:
        with postgres_connection() as conn:
            return dict(conn.execute(
                """SELECT count(*) FILTER (WHERE lifecycle_status<>'ARCHIVED') AS questions_count,
                     count(*) FILTER (WHERE review_status='PENDING') AS pending_questions_count
                   FROM questions WHERE created_by_user_id=%s""",
                (str(object_id(user_id, "user_id")),),
            ).fetchone())

    @staticmethod
    def ownership_overview(limit: int = 50) -> list[dict]:
        """Tổng quan số câu hỏi theo chủ sở hữu (giảng viên)."""
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT q.created_by_user_id AS user_id,
                          coalesce(u.display_name, '') AS display_name,
                          coalesce(u.email, '') AS email,
                          coalesce(u.role, '') AS role,
                          count(*) FILTER (WHERE q.lifecycle_status='ACTIVE') AS total,
                          count(*) FILTER (WHERE q.review_status='DRAFT') AS draft,
                          count(*) FILTER (WHERE q.review_status='PENDING') AS pending,
                          count(*) FILTER (WHERE q.review_status='APPROVED') AS approved,
                          count(*) FILTER (WHERE q.review_status='NEEDS_REVISION') AS needs_revision,
                          count(*) FILTER (WHERE q.review_status='REJECTED') AS rejected
                   FROM questions q
                   LEFT JOIN users u ON u.id = q.created_by_user_id
                   WHERE q.lifecycle_status='ACTIVE'
                   GROUP BY q.created_by_user_id, u.display_name, u.email, u.role
                   ORDER BY total DESC, email ASC
                   LIMIT %s""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def owned_questions(user_id) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT * FROM questions WHERE created_by_user_id=%s
                   AND lifecycle_status<>'ARCHIVED' ORDER BY created_at, id""",
                (str(object_id(user_id, "user_id")),),
            ).fetchall()
        return [_question(row) for row in rows]

    @staticmethod
    def document_ids_with_questions(document_ids: list) -> set[str]:
        if not document_ids:
            return set()
        with postgres_connection() as conn:
            rows = conn.execute(
                """SELECT DISTINCT COALESCE(q.payload->>'document_id', v.payload->>'document_id')
                          AS document_id
                   FROM questions q
                   LEFT JOIN question_versions v ON v.id=q.current_version_id
                   WHERE q.lifecycle_status<>'ARCHIVED'
                     AND COALESCE(q.payload->>'document_id', v.payload->>'document_id')=ANY(%s)""",
                ([str(item) for item in document_ids],),
            ).fetchall()
        return {row["document_id"] for row in rows}

    @staticmethod
    def count_current_using(*, subject_id=None, chapter_id=None, clo_id=None) -> int:
        """Câu hỏi active có version hiện hành dùng môn/chương/CLO này."""
        if subject_id is not None:
            clause, value = "v.classification->'subject'->>'id'=%s", str(subject_id)
        elif chapter_id is not None:
            clause, value = "v.classification->'chapter'->>'id'=%s", str(chapter_id)
        else:
            clause = "v.clos @> %s::jsonb"
            value = '[{"id":"' + str(object_id(clo_id, "clo_id")) + '"}]'
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM questions q "
                "JOIN question_versions v ON v.id=q.current_version_id "
                "WHERE q.lifecycle_status='ACTIVE' AND " + clause, (value,),
            ).fetchone()["n"]

    def _pairs(self, suffix: str, params: list) -> list[tuple[dict, dict]]:
        with postgres_connection() as conn:
            rows = conn.execute("SELECT q.* FROM questions q " + suffix, params).fetchall()
            version_ids = [row["current_version_id"] for row in rows]
            version_rows = conn.execute(
                "SELECT * FROM question_versions WHERE id=ANY(%s)", (version_ids,),
            ).fetchall() if version_ids else []
        versions = {row["id"]: _version(row) for row in version_rows}
        return [(_question(row), versions[row["current_version_id"]]) for row in rows]


    def set_secondary_review(self, question_id: str | ObjectId, *,
                             expected_version_id: ObjectId,
                             expected_review_status: str,
                             fields: dict,
                             actor_user_id: ObjectId,
                             actor_role: str,
                             reason: str,
                             notifications: list[dict] | None = None) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                raise RuntimeError("VERSION_CONFLICT")
            question, version = pair
            if (version["_id"] != expected_version_id
                    or question["review_status"] != expected_review_status):
                raise RuntimeError("VERSION_CONFLICT")
            before = question.get("secondary_review") or {}
            question.update(fields)
            self._save_question(conn, question)
            write_postgres_audit_event(
                conn, action="QUESTION_SECONDARY_REVIEW_SET",
                entity_type="question", entity_id=question["_id"],
                actor_user_id=actor_user_id, actor_role=actor_role,
                before={"secondary_review": before},
                after={"secondary_review": question.get("secondary_review") or {}},
                metadata={"reason": reason,
                          "question_version_id": str(version["_id"])},
            )
            insert_notifications(conn, notifications)
            return question, version

    def _assignment_audit(self, conn, action: str, question: dict, version: dict,
                          actor_user_id: ObjectId, actor_role: str,
                          before: dict, after: dict, metadata: dict | None = None) -> None:
        write_postgres_audit_event(
            conn, action=action, entity_type="question", entity_id=question["_id"],
            actor_user_id=actor_user_id, actor_role=actor_role,
            before={"review_assignment": before},
            after={"review_assignment": after},
            metadata={"question_version_id": str(version["_id"]), **(metadata or {})},
        )

    def claim_review(self, question_id: str | ObjectId, *, actor_user_id: ObjectId,
                     actor_role: str, lock_expires_at, now) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                raise LookupError("Không tìm thấy câu hỏi")
            question, version = pair
            if question["review_status"] != "PENDING":
                raise ValueError("Chỉ câu hỏi đang chờ duyệt mới có thể claim")
            secondary = question.get("secondary_review") or {}
            if (secondary.get("status") == "AWAITING_SECONDARY"
                    and secondary.get("primary_reviewer_user_id") == actor_user_id):
                raise PermissionError("Người duyệt lần đầu không được nhận lượt duyệt lần hai")
            if actor_role != "Admin" and actor_user_id in {
                question.get("created_by_user_id"), version.get("created_by_user_id"),
            }:
                raise PermissionError("Bạn không thể kiểm duyệt câu hỏi do chính mình tạo hoặc chỉnh sửa")
            previous = question.get("review_assignment") or {}
            expires = previous.get("lock_expires_at")
            if (actor_role != "Admin" and previous.get("status") != "UNASSIGNED"
                    and previous.get("reviewer_user_id") != actor_user_id
                    and expires is not None and expires > now):
                raise PermissionError("Câu hỏi đang được Reviewer khác xử lý")
            assignment = {
                "status": "IN_REVIEW", "reviewer_user_id": actor_user_id,
                "assigned_by_user_id": previous.get("assigned_by_user_id") or actor_user_id,
                "assigned_at": previous.get("assigned_at") or now,
                "claimed_at": now, "lock_expires_at": lock_expires_at,
                "last_released_at": None, "release_reason": None,
            }
            question["review_assignment"] = assignment
            question["updated_at"] = now
            self._save_question(conn, question)
            self._assignment_audit(conn, "QUESTION_REVIEW_CLAIMED", question, version,
                                   actor_user_id, actor_role, previous, assignment)
            return question, version

    def release_review(self, question_id: str | ObjectId, *, actor_user_id: ObjectId,
                       actor_role: str, assignment: dict, now) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                raise LookupError("Không tìm thấy câu hỏi")
            question, version = pair
            previous = question.get("review_assignment") or {}
            if (question["review_status"] != "PENDING" or
                    (actor_role != "Admin" and previous.get("reviewer_user_id") != actor_user_id)):
                raise PermissionError("Bạn không thể release assignment này")
            question["review_assignment"] = assignment
            question["updated_at"] = now
            self._save_question(conn, question)
            self._assignment_audit(conn, "QUESTION_REVIEW_RELEASED", question, version,
                                   actor_user_id, actor_role, previous, assignment)
            return question, version

    def renew_review(self, question_id: str | ObjectId, *, actor_user_id: ObjectId,
                     lock_expires_at, now) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                raise LookupError("Không tìm thấy câu hỏi")
            question, version = pair
            assignment = question.get("review_assignment") or {}
            if (question["review_status"] != "PENDING"
                    or assignment.get("status") != "IN_REVIEW"
                    or assignment.get("reviewer_user_id") != actor_user_id):
                raise PermissionError("Bạn không còn giữ khóa kiểm duyệt câu hỏi này")
            assignment["lock_expires_at"] = lock_expires_at
            question["review_assignment"] = assignment
            question["updated_at"] = now
            self._save_question(conn, question)
            return question, version

    def assign_review(self, question_id: str | ObjectId, *,
                      expected_version_id: ObjectId, assignment: dict,
                      actor_user_id: ObjectId, actor_role: str,
                      action: str, now, note: str | None = None,
                      notifications: list[dict] | None = None) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                raise LookupError("Không tìm thấy câu hỏi")
            question, version = pair
            if (question["review_status"] != "PENDING"
                    or version["_id"] != expected_version_id):
                raise RuntimeError("VERSION_CONFLICT")
            previous = question.get("review_assignment") or {}
            question["review_assignment"] = assignment
            question["updated_at"] = now
            self._save_question(conn, question)
            self._assignment_audit(conn, action, question, version,
                                   actor_user_id, actor_role, previous, assignment,
                                   {"note": note})
            insert_notifications(conn, notifications)
            return question, version

    def list(
        self, page: int, page_size: int, review_status: str | None,
        search: str | None, *, question_type: str | None = None,
        bloom_level: int | None = None, document_id: str | None = None,
        subject_id: str | None = None, chapter_id: str | None = None,
        clo_id: str | None = None, difficulty: str | None = None,
        quality_color: str | None = None, min_score: float | None = None,
        publication_status: str | None = None,
        evaluation_status: str | None = None,
        assignment_status: str | None = None,
        assigned_reviewer_user_id: ObjectId | None = None,
        creator_user_id: ObjectId | None = None,
        owner_user_id: ObjectId | None = None,
        visible_to_user_id: ObjectId | None = None,
        approved_current_only: bool = False,
        waiting_since=None, overdue_at=None,
        override_only: bool = False,
        created_from=None, created_to=None,
        submitted_from=None, submitted_to=None,
        include_status_counts: bool = False,
        sort_by: str = "priority",
        source_presence: str | None = None,
        secondary_status: str | None = None,
    ):
        clauses = ["q.lifecycle_status='ACTIVE'"]
        params = []

        def add(expression, *values):
            clauses.append(expression)
            params.extend(values)

        if publication_status:
            add("q.publication_status=%s", publication_status)
        if evaluation_status:
            statuses = [item.strip().upper() for item in evaluation_status.split(",")
                        if item.strip()]
            if statuses:
                add("q.evaluation_status=ANY(%s)", statuses)
        if assignment_status == "UNASSIGNED":
            add("(q.assignment->>'status' IS NULL OR q.assignment->>'status'='UNASSIGNED')")
        elif assignment_status:
            add("q.assignment->>'status'=%s", assignment_status)
        if assigned_reviewer_user_id is not None:
            add("q.assignment->>'reviewer_user_id'=%s", str(assigned_reviewer_user_id))
        if quality_color:
            add("q.payload->'quality_summary'->>'color'=%s", quality_color.upper())
        if min_score is not None:
            add("(q.payload->'quality_summary'->>'overall_score')::numeric >= %s", float(min_score))
        if waiting_since is not None:
            add("q.updated_at <= %s", waiting_since)
        if overdue_at is not None:
            add("(q.assignment->>'lock_expires_at')::timestamptz <= %s", overdue_at)
        if secondary_status:
            add("q.payload->'secondary_review'->>'status'=%s", secondary_status)
        if override_only:
            add("""EXISTS (SELECT 1 FROM question_reviews review
                 WHERE review.id=q.payload->>'latest_review_id'
                   AND review.payload->'override'->>'applied'='true')""")
        if created_from is not None:
            add("q.created_at >= %s", created_from)
        if created_to is not None:
            add("q.created_at <= %s", created_to)
        if submitted_from is not None:
            add("(q.payload->'review_submission'->>'submitted_at')::timestamptz >= %s",
                submitted_from)
        if submitted_to is not None:
            add("(q.payload->'review_submission'->>'submitted_at')::timestamptz <= %s",
                submitted_to)
        if subject_id:
            add("q.subject_id=%s", str(object_id(subject_id, "subject_id")))
        if approved_current_only:
            add("q.approved_version_id=q.current_version_id")
        if creator_user_id is not None:
            add("(q.created_by_user_id=%s OR v.created_by_user_id=%s)",
                str(creator_user_id), str(creator_user_id))
        if owner_user_id is not None:
            add("(q.created_by_user_id=%s OR v.created_by_user_id=%s)",
                str(owner_user_id), str(owner_user_id))
        if visible_to_user_id is not None:
            uid = str(visible_to_user_id)
            add("""(q.created_by_user_id=%s OR v.created_by_user_id=%s
                 OR q.payload->'shared_with_user_ids' ? %s
                 OR q.payload->>'shared_scope'='SUBJECT')""", uid, uid, uid)
        if question_type:
            add("v.classification->>'assessment_type'=%s", question_type.upper())
        if bloom_level is not None:
            add("(v.classification->'bloom'->>'level')::integer=%s", int(bloom_level))
        if document_id:
            add("v.payload->>'document_id'=%s", str(object_id(document_id, "document_id")))
        if chapter_id:
            add("v.classification->'chapter'->>'id'=%s",
                str(object_id(chapter_id, "chapter_id")))
        if clo_id:
            add("v.clos @> %s::jsonb", '[{"id":"' + str(object_id(clo_id, "clo_id")) + '"}]')
        if difficulty:
            add("v.classification->>'difficulty'=%s", difficulty)
        if source_presence == "WITH_SOURCE":
            add("jsonb_array_length(v.sources)>0")
        elif source_presence == "MISSING_SOURCE":
            add("jsonb_array_length(v.sources)=0")
        if search:
            pattern = "%" + _escape_like(search) + "%"
            add("(q.question_code ILIKE %s OR v.content ILIKE %s)", pattern, pattern)

        base_where = " AND ".join(clauses)
        status_clause = ""
        status_params = []
        if review_status == "PROCESSED":
            status_clause = " AND q.review_status=ANY(%s)"
            status_params = [["APPROVED", "NEEDS_REVISION", "REJECTED"]]
        elif review_status:
            status_clause = " AND q.review_status=%s"
            status_params = [review_status]
        source = " FROM questions q JOIN question_versions v ON v.id=q.current_version_id WHERE "
        submitted = "(q.payload->'review_submission'->>'submitted_at')::timestamptz"
        sla_hours = int(max(1, settings.review_sla_hours))
        order = {
            # Cùng thứ tự với bản Mongo: theo thời điểm gửi duyệt, giá trị thiếu
            # đứng đầu khi tăng dần và cuối khi giảm dần.
            "oldest": f"{submitted} NULLS FIRST, q.id",
            "newest": f"{submitted} DESC NULLS LAST, q.id DESC",
            "updated": "q.updated_at DESC, q.id DESC",
            "ai_lowest": ("(q.payload->'quality_summary'->>'overall_score')::numeric NULLS FIRST, "
                          f"{submitted} NULLS FIRST, q.id"),
            "priority": f"""CASE
                WHEN q.assignment->>'status'='IN_REVIEW'
                     AND (q.assignment->>'lock_expires_at')::timestamptz <= now() THEN 0
                WHEN q.review_status='PENDING' AND
                     {submitted} <= now() - interval '{sla_hours} hours' THEN 1
                WHEN q.payload->'secondary_review'->>'status'='AWAITING_SECONDARY' THEN 2
                WHEN q.payload->'quality_summary'->>'color'='RED'
                     AND q.evaluation_status IN ('PASSED','FAILED') THEN 3
                WHEN q.evaluation_status IN ('NOT_STARTED','ERROR','STALE') THEN 4
                ELSE 5 END,
                {submitted} NULLS FIRST,
                q.id""",
        }.get(sort_by)
        if order is None:
            raise ValueError("Kiểu sắp xếp hàng kiểm duyệt không hợp lệ")
        with postgres_connection() as conn:
            total = conn.execute(
                "SELECT count(*) AS n" + source + base_where + status_clause,
                [*params, *status_params],
            ).fetchone()["n"]
            rows = conn.execute(
                "SELECT q.*" + source + base_where + status_clause
                + " ORDER BY " + order + " LIMIT %s OFFSET %s",
                [*params, *status_params, page_size, (page - 1) * page_size],
            ).fetchall()
            version_ids = [row["current_version_id"] for row in rows]
            version_rows = conn.execute(
                "SELECT * FROM question_versions WHERE id=ANY(%s)", (version_ids,),
            ).fetchall() if version_ids else []
            versions = {row["id"]: _version(row) for row in version_rows}
            pairs = [(_question(row), versions[row["current_version_id"]]) for row in rows]
            if include_status_counts:
                counts = conn.execute(
                    "SELECT q.review_status, count(*) AS n" + source + base_where
                    + " GROUP BY q.review_status", params,
                ).fetchall()
                return pairs, total, {row["review_status"]: row["n"] for row in counts}
        return pairs, total

    @staticmethod
    def _save_question(conn, question: dict) -> None:
        for table, row in projected_rows("questions", question):
            upsert(conn, table, row)

    @staticmethod
    def _save_version(conn, version: dict) -> None:
        for table, row in projected_rows("question_versions", version):
            upsert(conn, table, row)

    @staticmethod
    def _pair(conn, question_id: str | ObjectId, *, active_only: bool = True,
              lock: bool = False) -> tuple[dict, dict] | None:
        query = "SELECT * FROM questions WHERE id=%s"
        if active_only:
            query += " AND lifecycle_status='ACTIVE'"
        if lock:
            query += " FOR UPDATE"
        row = conn.execute(query, (str(object_id(question_id, "question_id")),)).fetchone()
        if not row:
            return None
        question = _question(row)
        version_row = conn.execute(
            "SELECT * FROM question_versions WHERE id=%s AND question_id=%s",
            (str(question["current_version_id"]), str(question["_id"])),
        ).fetchone()
        if not version_row:
            raise RuntimeError("Question aggregate bị thiếu current version")
        return question, _version(version_row)

    def find_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None:
        with postgres_connection() as conn:
            return self._pair(conn, question_id)

    def create(self, aggregate: dict, version: dict) -> tuple[dict, dict]:
        with postgres_connection() as conn:
            self._save_question(conn, aggregate)
            self._save_version(conn, version)
        return aggregate, version

    def create_version(self, question_id: str | ObjectId, expected_version: int,
                       version: dict, *, review_submission: dict | None = None,
                       notifications: list[dict] | None = None
                       ) -> tuple[dict, dict] | None:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                return None
            question, current = pair
            if question["current_version"] != expected_version:
                raise RuntimeError("VERSION_CONFLICT")
            new_version = deepcopy(current)
            new_version.update(version)
            new_version.update(_id=ObjectId(), question_id=question["_id"],
                               version=expected_version + 1)
            now = utc_now()
            question.update({
                "current_version": expected_version + 1,
                "current_version_id": new_version["_id"],
                "evaluation_status": "NOT_STARTED",
                "review_status": "DRAFT",
                "publication_status": ("STALE" if question["publication_status"] == "PUBLISHED"
                                       else question["publication_status"]),
                "review_assignment": {
                    "status": "UNASSIGNED", "reviewer_user_id": None,
                    "assigned_by_user_id": None, "assigned_at": None,
                    "claimed_at": None, "lock_expires_at": None,
                    "last_released_at": None, "release_reason": None,
                },
                "review_submission": {}, "secondary_review": {},
                "quality_summary": {},
                "subject_id": ((new_version.get("classification") or {}).get("subject") or {}).get("id"),
                "updated_at": now,
            })
            self._save_version(conn, new_version)
            self._save_question(conn, question)
            insert_notifications(conn, notifications)
            return question, new_version

    def update_review_status(self, question_id: str | ObjectId,
                             allowed_statuses: set[str], review_status: str, *,
                             review_submission: dict | None = None,
                             notifications: list[dict] | None = None
                             ) -> tuple[dict, dict] | None:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair or pair[0]["review_status"] not in allowed_statuses:
                return None
            question, version = pair
            now = utc_now()
            question["review_status"] = review_status
            question["review_assignment"] = {
                "status": "UNASSIGNED", "reviewer_user_id": None,
                "assigned_by_user_id": None, "assigned_at": None,
                "claimed_at": None, "lock_expires_at": None,
                "last_released_at": None, "release_reason": None,
            }
            question["updated_at"] = now
            if review_status == "PENDING" and review_submission:
                submitted = deepcopy(review_submission)
                submitted["submitted_at"] = now
                question["review_submission"] = submitted
            self._save_question(conn, question)
            insert_notifications(conn, notifications)
            return question, version

    def archive(self, question_id: str | ObjectId) -> bool:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                return False
            question, _version_record = pair
            question.update(lifecycle_status="ARCHIVED", archived_at=utc_now(),
                            updated_at=utc_now())
            self._save_question(conn, question)
            return True

    def list_versions(self, question_id: str | ObjectId) -> list[dict]:
        key = str(object_id(question_id, "question_id"))
        with postgres_connection() as conn:
            if not conn.execute("SELECT 1 FROM questions WHERE id=%s", (key,)).fetchone():
                return []
            rows = conn.execute(
                "SELECT * FROM question_versions WHERE question_id=%s ORDER BY version DESC",
                (key,),
            ).fetchall()
        return [_version(row) for row in rows]

    def update_sharing(self, question_id: str | ObjectId,
                       fields: dict) -> tuple[dict, dict] | None:
        with postgres_connection() as conn:
            pair = self._pair(conn, question_id, lock=True)
            if not pair:
                return None
            question, version = pair
            normalized = dict(fields)
            if "shared_with_user_ids" in normalized:
                normalized["shared_with_user_ids"] = [
                    object_id(item, "shared_with_user_id")
                    for item in normalized.get("shared_with_user_ids") or []
                ]
            question.update(normalized)
            question["updated_at"] = utc_now()
            self._save_question(conn, question)
            return question, version
