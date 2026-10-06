from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Protocol

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.database import Database

from core.audit import write_audit_event
from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import mongo_transaction
from modules.catalog.postgres_subject_repository import subject_record
from modules.documents.store import get_document_repository
from modules.users.store import find_user_record


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def object_id(value: str | ObjectId, field_name: str = "id") -> ObjectId:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(value)
    except Exception as exc:
        raise ValueError(f"{field_name} không hợp lệ") from exc


SHARING_ID_LIST_FIELDS = ("shared_with_user_ids", "shared_edit_user_ids")


def normalize_sharing_fields(fields: dict) -> dict:
    normalized = dict(fields)
    for key in SHARING_ID_LIST_FIELDS:
        if key in normalized:
            normalized[key] = [object_id(item, key[:-1]) for item in normalized.get(key) or []]
    if normalized.get("created_by_user_id") is not None:
        normalized["created_by_user_id"] = object_id(normalized["created_by_user_id"], "created_by_user_id")
    return normalized


REVIEW_IN_PROGRESS = "REVIEW_IN_PROGRESS"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _empty_assignment() -> dict:
    return {
        "status": "UNASSIGNED", "reviewer_user_id": None, "assigned_by_user_id": None,
        "assigned_at": None, "claimed_at": None, "lock_expires_at": None,
        "last_released_at": None, "release_reason": None,
    }


def review_claim_active(question: dict, now: datetime) -> bool:
    assignment = question.get("review_assignment") or {}
    if question.get("review_status") != "PENDING" or assignment.get("status") != "IN_REVIEW":
        return False
    expires_at = _aware(assignment.get("lock_expires_at"))
    return expires_at is None or expires_at > _aware(now)


def review_fields_after_edit(question: dict, now: datetime, review_submission: dict | None) -> dict:
    """Review state of a question after its author saves a new version.

    Every edit returns the question to DRAFT, so a PENDING question leaves the queue
    and must be resubmitted through the submission checks; a reviewer's active claim
    blocks the edit. A standing two-reviewer requirement survives the edit and
    restarts from its first review.
    """
    if review_claim_active(question, now):
        raise RuntimeError(REVIEW_IN_PROGRESS)
    secondary = question.get("secondary_review") or {}
    sticky_secondary = {}
    if secondary.get("required") and secondary.get("status") in {"REQUESTED", "AWAITING_SECONDARY"}:
        sticky_secondary = {
            **secondary, "status": "REQUESTED", "primary_review_id": None,
            "primary_reviewer_user_id": None, "secondary_review_id": None, "completed_at": None,
        }
    return {
        "review_status": "DRAFT", "review_assignment": _empty_assignment(),
        "review_submission": {}, "secondary_review": sticky_secondary,
    }


def _subject_oids(values) -> list:
    return [ObjectId(str(item)) for item in values or () if ObjectId.is_valid(str(item))]


def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def serialize_question(question: dict, version: dict) -> dict:
    classification = version.get("classification") or {}
    subject = classification.get("subject") or {}
    subject_id = question.get("subject_id")
    if subject_id is None and isinstance(subject, dict):
        subject_id = subject.get("id")
    elif subject_id is None and subject:
        subject_id = subject
    subject_snapshot = {
        "id": subject_id,
        "code": (
            subject.get("code") or subject.get("subject_code") or ""
            if isinstance(subject, dict)
            else ""
        ),
        "name": (
            subject.get("name") or subject.get("subject_name") or ""
            if isinstance(subject, dict)
            else ""
        ),
    }
    review_submission = deepcopy(question.get("review_submission") or {})
    # Read compatibility for the first additive version of these fields.
    if not review_submission and (
        question.get("submitted_by_user_id") or question.get("submitted_at")
    ):
        review_submission = {
            "submitted_by_user_id": question.get("submitted_by_user_id"),
            "submitted_by": {"id": question.get("submitted_by_user_id")},
            "submitted_at": question.get("submitted_at"),
            "subject_id": subject_id,
            "subject": subject_snapshot,
        }
    submitted_by_user_id = review_submission.get("submitted_by_user_id")
    if not submitted_by_user_id and question.get("review_status") != "DRAFT":
        # Câu cũ không lưu người gửi duyệt: người tạo câu là người gửi.
        submitted_by_user_id = question.get("created_by_user_id") or version.get("created_by_user_id")
    submitted_at = review_submission.get("submitted_at")
    return json_safe(
        {
            "id": question["_id"],
            "question_code": question["question_code"],
            "current_version": question["current_version"],
            "current_version_id": question["current_version_id"],
            "approved_version_id": question.get("approved_version_id"),
            "document_id": version.get("document_id"),
            "subject_id": subject_id,
            "subject": subject_snapshot,
            "review_submission": review_submission,
            "submitted_by_user_id": submitted_by_user_id,
            "submitted_at": submitted_at,
            # Người tạo câu và người tạo phiên bản hiện tại: không được tự duyệt (trừ Admin có lý do).
            "author_user_ids": list(
                dict.fromkeys(
                    str(item)
                    for item in (question.get("created_by_user_id"), version.get("created_by_user_id"))
                    if item
                )
            ),
            "lifecycle_status": question["lifecycle_status"],
            "evaluation_status": question["evaluation_status"],
            "review_status": question["review_status"],
            "publication_status": question["publication_status"],
            "content": version["content"],
            "question_data": version["question_data"],
            "classification": version["classification"],
            "clos": version.get("clos") or [],
            "sources": version.get("sources") or [],
            "content_hash": version["content_hash"],
            "quality_summary": question.get("quality_summary") or {},
            "review_assignment": question.get("review_assignment") or {"status": "UNASSIGNED"},
            "shared_with_user_ids": question.get("shared_with_user_ids") or [],
            "shared_edit_user_ids": question.get("shared_edit_user_ids") or [],
            "shared_scope": question.get("shared_scope") or "PRIVATE",
            "secondary_review": question.get("secondary_review") or {},
            "latest_review_id": question.get("latest_review_id"),
            "created_at": question["created_at"],
            "updated_at": question["updated_at"],
        }
    )


class QuestionRepository(Protocol):
    def find_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None: ...

    def find_approved_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None: ...

    def reviewer_has_decided(self, question_id: str | ObjectId, reviewer_user_id: ObjectId) -> bool: ...

    def list(
        self,
        page: int,
        page_size: int,
        review_status: str | None,
        search: str | None,
        *,
        question_type: str | None = None,
        bloom_level: int | None = None,
        document_id: str | None = None,
        subject_id: str | None = None,
        chapter_id: str | None = None,
        clo_id: str | None = None,
        difficulty: str | None = None,
        quality_color: str | None = None,
        min_score: float | None = None,
        publication_status: str | None = None,
        evaluation_status: str | None = None,
        assignment_status: str | None = None,
        assigned_reviewer_user_id: ObjectId | None = None,
        creator_user_id: ObjectId | None = None,
        owner_user_id: ObjectId | None = None,
        visible_to_user_id: ObjectId | None = None,
        shared_subject_ids=None,
        include_shared: bool = True,
        approved_current_only: bool = False,
        approved_version_join: bool = False,
        reviewer_scope=None,
        waiting_since: datetime | None = None,
        overdue_at: datetime | None = None,
        override_only: bool = False,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        include_status_counts: bool = False,
        sort_by: str = "priority",
        source_presence: str | None = None,
        secondary_status: str | None = None,
    ) -> tuple[list[tuple[dict, dict]], int] | tuple[list[tuple[dict, dict]], int, dict[str, int]]: ...

    def create(self, aggregate: dict, version: dict) -> tuple[dict, dict]: ...

    def create_version(
        self,
        question_id: str | ObjectId,
        expected_version: int,
        version: dict,
        *,
        review_submission: dict | None = None,
    ) -> tuple[dict, dict] | None: ...

    def update_review_status(
        self,
        question_id: str | ObjectId,
        allowed_statuses: set[str],
        review_status: str,
        *,
        review_submission: dict | None = None,
    ) -> tuple[dict, dict] | None: ...

    def archive(self, question_id: str | ObjectId, *, audit: dict | None = None,
                expected_review_status: str | None = None, extra_fields: dict | None = None) -> bool: ...

    def list_versions(self, question_id: str | ObjectId) -> list[dict]: ...

    def update_sharing(self, question_id: str | ObjectId, fields: dict, *,
                       audit: dict | None = None) -> tuple[dict, dict] | None: ...


class QuestionReferenceRepository(Protocol):
    def find_chunk(self, chunk_id: ObjectId) -> dict | None: ...

    def find_document(self, document_id: ObjectId) -> dict | None: ...

    def document_contains_text(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId | None,
        text: str,
    ) -> bool: ...

    def find_pages(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId | None,
        page_numbers: list[int],
    ) -> list[dict]: ...

    def find_excerpt_pages(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId,
        text: str,
    ) -> list[int]: ...

    def find_subject(self, subject_id: ObjectId) -> dict | None: ...

    def reviewer_subject_ids(self, user_id: ObjectId) -> list: ...


class MongoQuestionReferenceRepository:
    def __init__(self, database: Database):
        self.db = database

    def reviewer_subject_ids(self, user_id: ObjectId) -> list:
        record = find_user_record(self.db, user_id) or {}
        return list(record.get("review_subject_ids") or [])

    def find_chunk(self, chunk_id: ObjectId) -> dict | None:
        return self.db.document_chunks.find_one({"_id": chunk_id})

    def find_document(self, document_id: ObjectId) -> dict | None:
        return get_document_repository(self.db).find_by_id(document_id)

    def _document_pages(self, document_id: ObjectId,
                        ocr_job_id: ObjectId | None) -> list[dict]:
        if settings.document_store == "mongo":
            query = {"document_id": document_id}
            if ocr_job_id is not None:
                query["ocr_job_id"] = ocr_job_id
            return list(self.db.document_pages.find(query).sort("page_number", 1))
        repository = get_document_repository(self.db)
        return (repository.list_pages_for_job(document_id, ocr_job_id)
                if ocr_job_id is not None else repository.list_pages_for_document(document_id))

    def document_contains_text(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId | None,
        text: str,
    ) -> bool:
        normalized_text = " ".join(str(text or "").split()).casefold()
        if not normalized_text:
            return False
        pages = self._document_pages(document_id, ocr_job_id)
        document_text = " ".join(
            " ".join(str(page.get("cleaned_text") or page.get("raw_text") or "").split())
            for page in pages
        ).casefold()
        return normalized_text in document_text

    def find_pages(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId | None,
        page_numbers: list[int],
    ) -> list[dict]:
        if not page_numbers:
            return []
        selected = set(page_numbers)
        return [page for page in self._document_pages(document_id, ocr_job_id)
                if page.get("page_number") in selected]

    def find_excerpt_pages(
        self,
        document_id: ObjectId,
        ocr_job_id: ObjectId,
        text: str,
    ) -> list[int]:
        excerpt = " ".join(str(text or "").split()).casefold()
        if not excerpt:
            return []
        pages = self._document_pages(document_id, ocr_job_id)
        parts: list[str] = []
        spans: list[tuple[int, int, int]] = []
        offset = 0
        for page in pages:
            content = " ".join(str(page.get("cleaned_text") or page.get("raw_text") or "").split()).casefold()
            if not content:
                continue
            if parts:
                offset += 1
            start = offset
            offset += len(content)
            parts.append(content)
            spans.append((start, offset, int(page.get("page_number") or page.get("unit_number") or 0)))
        match_start = " ".join(parts).find(excerpt)
        if match_start < 0:
            return []
        match_end = match_start + len(excerpt)
        return [number for start, end, number in spans if start < match_end and end > match_start]

    def find_subject(self, subject_id: ObjectId) -> dict | None:
        return subject_record(self.db, subject_id, active_only=True)


class MongoQuestionRepository:
    def __init__(self, database: Database):
        self.db = database

    def find_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None:
        question = self.db.questions.find_one(
            {
                "_id": object_id(question_id, "question_id"),
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
            }
        )
        if not question:
            return None
        version = self.db.question_versions.find_one({"_id": question["current_version_id"]})
        if not version:
            raise RuntimeError("Question aggregate bị thiếu current version")
        return question, version

    def reviewer_has_decided(self, question_id: str | ObjectId, reviewer_user_id: ObjectId) -> bool:
        return self.db.question_reviews.find_one(
            {"question_id": object_id(question_id, "question_id"), "reviewer_user_id": reviewer_user_id},
            {"_id": 1},
        ) is not None

    def find_approved_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None:
        """The question with its last approved version, independent of newer drafts."""
        question = self.db.questions.find_one(
            {
                "_id": object_id(question_id, "question_id"),
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
            }
        )
        if not question or not question.get("approved_version_id"):
            return None
        version = self.db.question_versions.find_one(
            {"_id": question["approved_version_id"], "question_id": question["_id"]}
        )
        return (question, version) if version else None

    def list(
        self,
        page: int,
        page_size: int,
        review_status: str | None,
        search: str | None,
        *,
        question_type: str | None = None,
        bloom_level: int | None = None,
        document_id: str | None = None,
        subject_id: str | None = None,
        chapter_id: str | None = None,
        clo_id: str | None = None,
        difficulty: str | None = None,
        quality_color: str | None = None,
        min_score: float | None = None,
        publication_status: str | None = None,
        evaluation_status: str | None = None,
        assignment_status: str | None = None,
        assigned_reviewer_user_id: ObjectId | None = None,
        creator_user_id: ObjectId | None = None,
        owner_user_id: ObjectId | None = None,
        visible_to_user_id: ObjectId | None = None,
        shared_subject_ids=None,
        include_shared: bool = True,
        approved_current_only: bool = False,
        approved_version_join: bool = False,
        reviewer_scope=None,
        waiting_since: datetime | None = None,
        overdue_at: datetime | None = None,
        override_only: bool = False,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        include_status_counts: bool = False,
        sort_by: str = "priority",
        source_presence: str | None = None,
        secondary_status: str | None = None,
    ) -> tuple[list[tuple[dict, dict]], int] | tuple[list[tuple[dict, dict]], int, dict[str, int]]:
        match: dict = {"schema_version": SCHEMA_VERSION, "lifecycle_status": "ACTIVE"}
        review_status_condition = (
            {"$in": ["APPROVED", "NEEDS_REVISION", "REJECTED"]}
            if review_status == "PROCESSED"
            else review_status
        )

        if review_status_condition and not include_status_counts:
            match["review_status"] = review_status_condition
        if publication_status:
            match["publication_status"] = publication_status
        if evaluation_status:
            evaluation_statuses = [
                item.strip().upper()
                for item in str(evaluation_status).split(",")
                if item.strip()
            ]
            match["evaluation_status"] = (
                {"$in": evaluation_statuses}
                if len(evaluation_statuses) > 1
                else evaluation_statuses[0]
            )
        if assignment_status == "UNASSIGNED":
            # Questions never assigned have no review_assignment.status at all;
            # {$in: [None, ...]} matches the missing field as well.
            match["review_assignment.status"] = {"$in": ["UNASSIGNED", None]}
        elif assignment_status:
            match["review_assignment.status"] = assignment_status
        if assigned_reviewer_user_id is not None:
            match["review_assignment.reviewer_user_id"] = assigned_reviewer_user_id
        if quality_color:
            match["quality_summary.color"] = quality_color.upper()
        if min_score is not None:
            match["quality_summary.overall_score"] = {"$gte": float(min_score)}
        if waiting_since is not None:
            match["updated_at"] = {"$lte": waiting_since}
        if overdue_at is not None:
            match["review_assignment.lock_expires_at"] = {"$lte": overdue_at}
        if secondary_status:
            match["secondary_review.status"] = secondary_status
        if override_only:
            # Questions whose latest decision was taken against the AI suggestion.
            override_review_ids = [
                review["_id"]
                for review in self.db.question_reviews.find({"override.applied": True}, {"_id": 1})
            ]
            match["latest_review_id"] = {"$in": override_review_ids}
        if created_from is not None or created_to is not None:
            created_at_match: dict = {}
            if created_from is not None:
                created_at_match["$gte"] = created_from
            if created_to is not None:
                created_at_match["$lte"] = created_to
            match["created_at"] = created_at_match
        if submitted_from is not None or submitted_to is not None:
            submitted_at_match: dict = {}
            if submitted_from is not None:
                submitted_at_match["$gte"] = submitted_from
            if submitted_to is not None:
                submitted_at_match["$lte"] = submitted_to
            match["review_submission.submitted_at"] = submitted_at_match
        if subject_id:
            match["subject_id"] = object_id(subject_id, "subject_id")
        pipeline: list[dict] = [
            {"$match": match},
            {
                "$lookup": {
                    "from": "question_versions",
                    "localField": "approved_version_id" if approved_version_join else "current_version_id",
                    "foreignField": "_id",
                    "as": "version",
                }
            },
            {"$unwind": "$version"},
        ]
        if approved_current_only:
            pipeline.append(
                {
                    "$match": {
                        "$expr": {"$eq": ["$approved_version_id", "$current_version_id"]}
                    }
                }
            )
        if owner_user_id is not None:
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"created_by_user_id": owner_user_id},
                            {"created_by_user_id": None, "version.created_by_user_id": owner_user_id},
                        ]
                    }
                }
            )
        def shared_clauses(uid) -> list[dict]:
            if not include_shared:
                return []
            return [
                {"shared_with_user_ids": uid},
                {"shared_scope": "SUBJECT", "approved_version_id": {"$ne": None},
                 "subject_id": {"$in": _subject_oids(shared_subject_ids)}},
            ]

        if visible_to_user_id is not None:
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"created_by_user_id": visible_to_user_id},
                            {"created_by_user_id": None, "version.created_by_user_id": visible_to_user_id},
                            *shared_clauses(visible_to_user_id),
                        ]
                    }
                }
            )
        if reviewer_scope is not None:
            uid = reviewer_scope.user_id
            reviewed_ids = self.db.question_reviews.distinct("question_id", {"reviewer_user_id": uid})
            pending_scope: dict = {
                "review_status": "PENDING",
                "subject_id": {"$in": _subject_oids(reviewer_scope.subject_ids)},
            }
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"created_by_user_id": uid},
                            {"created_by_user_id": None, "version.created_by_user_id": uid},
                            *shared_clauses(uid),
                            {"review_assignment.reviewer_user_id": uid},
                            {"secondary_review.primary_reviewer_user_id": uid},
                            {"secondary_review.secondary_reviewer_user_id": uid},
                            pending_scope,
                            {"_id": {"$in": reviewed_ids}},
                        ]
                    }
                }
            )
        if creator_user_id is not None:
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"created_by_user_id": creator_user_id},
                            {"version.created_by_user_id": creator_user_id},
                        ]
                    }
                }
            )
        version_match: dict = {}
        if question_type:
            version_match["version.classification.assessment_type"] = question_type.upper()
        if bloom_level is not None:
            version_match["version.classification.bloom.level"] = int(bloom_level)
        if document_id:
            version_match["version.document_id"] = object_id(document_id, "document_id")
        if chapter_id:
            version_match["version.classification.chapter.id"] = object_id(chapter_id, "chapter_id")
        if clo_id:
            version_match["version.clos.id"] = object_id(clo_id, "clo_id")
        if difficulty:
            version_match["version.classification.difficulty"] = difficulty
        if version_match:
            pipeline.append({"$match": version_match})
        if source_presence == "WITH_SOURCE":
            pipeline.append({"$match": {"version.sources.0": {"$exists": True}}})
        elif source_presence == "MISSING_SOURCE":
            pipeline.append({"$match": {"version.sources.0": {"$exists": False}}})
        if search:
            pipeline.append(
                {
                    "$match": {
                        "$or": [
                            {"question_code": {"$regex": search, "$options": "i"}},
                            {"version.content": {"$regex": search, "$options": "i"}},
                        ]
                    }
                }
            )
        status_counts_pipeline = deepcopy(pipeline) if include_status_counts else None
        if review_status_condition and include_status_counts:
            pipeline[0]["$match"]["review_status"] = review_status_condition
        sort_spec = {
            "oldest": {"review_submission.submitted_at": 1, "_id": 1},
            "newest": {"review_submission.submitted_at": -1, "_id": -1},
            "ai_lowest": {"quality_summary.overall_score": 1, "review_submission.submitted_at": 1},
            "updated": {"updated_at": -1, "_id": -1},
        }.get(sort_by)
        if sort_spec is None:
            now = utc_now()
            sla_cutoff = now - timedelta(hours=max(1, settings.review_sla_hours))
            pipeline.append(
                {
                    "$addFields": {
                        "review_priority": {
                            "$switch": {
                                "branches": [
                                    {
                                        "case": {
                                            "$and": [
                                                {"$eq": ["$review_assignment.status", "IN_REVIEW"]},
                                                {"$lte": ["$review_assignment.lock_expires_at", now]},
                                            ]
                                        },
                                        "then": 0,
                                    },
                                    # Câu đã chờ quá hạn duyệt (SLA) được đưa lên ngay sau câu mất khoá.
                                    {
                                        "case": {
                                            "$and": [
                                                {"$eq": ["$review_status", "PENDING"]},
                                                {"$ne": [{"$ifNull": ["$review_submission.submitted_at", None]}, None]},
                                                {"$lte": ["$review_submission.submitted_at", sla_cutoff]},
                                            ]
                                        },
                                        "then": 1,
                                    },
                                    {"case": {"$eq": ["$secondary_review.status", "AWAITING_SECONDARY"]}, "then": 2},
                                    {
                                        "case": {
                                            "$and": [
                                                {"$eq": ["$quality_summary.color", "RED"]},
                                                {"$in": ["$evaluation_status", ["PASSED", "FAILED"]]},
                                            ]
                                        },
                                        "then": 3,
                                    },
                                    {"case": {"$in": ["$evaluation_status", ["NOT_STARTED", "ERROR", "STALE"]]}, "then": 4},
                                ],
                                "default": 5,
                            }
                        }
                    }
                }
            )
            sort_spec = {"review_priority": 1, "review_submission.submitted_at": 1, "_id": 1}
        pipeline.append(
            {
                "$facet": {
                    "items": [
                        {"$sort": sort_spec},
                        {"$skip": (page - 1) * page_size},
                        {"$limit": page_size},
                    ],
                    "count": [{"$count": "total"}],
                }
            }
        )
        result = list(self.db.questions.aggregate(pipeline))[0]
        total = result["count"][0]["total"] if result["count"] else 0
        pairs = [(item, item["version"]) for item in result["items"]]
        if not include_status_counts:
            return pairs, total
        status_counts_pipeline.append(
            {"$group": {"_id": "$review_status", "count": {"$sum": 1}}}
        )
        status_count_rows = list(self.db.questions.aggregate(status_counts_pipeline))
        status_counts = {
            str(item["_id"]): int(item["count"])
            for item in status_count_rows
            if item.get("_id")
        }
        return pairs, total, status_counts

    def create(self, aggregate: dict, version: dict) -> tuple[dict, dict]:
        with mongo_transaction() as session:
            self.db.questions.insert_one(aggregate, session=session)
            self.db.question_versions.insert_one(version, session=session)
        return aggregate, version

    def create_version(
        self,
        question_id: str | ObjectId,
        expected_version: int,
        version: dict,
        *,
        review_submission: dict | None = None,
    ) -> tuple[dict, dict] | None:
        pair = self.find_pair(question_id)
        if not pair:
            return None
        question, current = pair
        if question["current_version"] != expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        new_version = deepcopy(current)
        new_version.update(version)
        new_version["_id"] = ObjectId()
        new_version["question_id"] = question["_id"]
        new_version["version"] = expected_version + 1
        now = utc_now()
        aggregate_fields = {
            "current_version": expected_version + 1,
            "current_version_id": new_version["_id"],
            "evaluation_status": "NOT_STARTED",
            "publication_status": (
                "STALE"
                if question["publication_status"] == "PUBLISHED"
                else question["publication_status"]
            ),
            **review_fields_after_edit(question, now, review_submission),
            "quality_summary": {},
            "subject_id": (
                ((new_version.get("classification") or {}).get("subject") or {}).get("id")
            ),
            "updated_at": now,
        }
        with mongo_transaction() as session:
            self.db.question_versions.insert_one(new_version, session=session)
            result = self.db.questions.update_one(
                {
                    "_id": question["_id"],
                    "current_version": expected_version,
                    "lifecycle_status": "ACTIVE",
                    # A reviewer claiming between our read and this write wins.
                    "$nor": [{
                        "review_status": "PENDING",
                        "review_assignment.status": "IN_REVIEW",
                        "review_assignment.lock_expires_at": {"$gt": now},
                    }],
                },
                {"$set": aggregate_fields},
                session=session,
            )
            if not result.modified_count:
                latest = self.db.questions.find_one({"_id": question["_id"]}) or {}
                if review_claim_active(latest, now):
                    raise RuntimeError(REVIEW_IN_PROGRESS)
                raise RuntimeError("VERSION_CONFLICT")
        updated = self.db.questions.find_one({"_id": question["_id"]})
        return updated, new_version

    def update_review_status(
        self,
        question_id: str | ObjectId,
        allowed_statuses: set[str],
        review_status: str,
        *,
        review_submission: dict | None = None,
    ) -> tuple[dict, dict] | None:
        question_oid = object_id(question_id, "question_id")
        now = utc_now()
        fields = {
            "review_status": review_status,
            "review_assignment": {
                "status": "UNASSIGNED",
                "reviewer_user_id": None,
                "assigned_by_user_id": None,
                "assigned_at": None,
                "claimed_at": None,
                "lock_expires_at": None,
                "last_released_at": None,
                "release_reason": None,
            },
            "updated_at": now,
        }
        if review_status == "PENDING" and review_submission:
            normalized_submission = deepcopy(review_submission)
            normalized_submission["submitted_at"] = now
            fields["review_submission"] = normalized_submission
        result = self.db.questions.update_one(
            {
                "_id": question_oid,
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
                "review_status": {"$in": list(allowed_statuses)},
            },
            {"$set": fields},
        )
        if not result.matched_count:
            return None
        return self.find_pair(question_oid)

    def archive(self, question_id: str | ObjectId, *, audit: dict | None = None,
                expected_review_status: str | None = None, extra_fields: dict | None = None) -> bool:
        now = utc_now()
        query = {
            "_id": object_id(question_id, "question_id"),
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ACTIVE",
        }
        if expected_review_status is not None:
            query["review_status"] = expected_review_status
        with mongo_transaction() as session:
            result = self.db.questions.update_one(
                query,
                {
                    "$set": {
                        **(extra_fields or {}),
                        "lifecycle_status": "ARCHIVED",
                        "archived_at": now,
                        "updated_at": now,
                    }
                },
                session=session,
            )
            if result.matched_count == 1 and audit:
                write_audit_event(self.db, session=session, **audit)
        return result.matched_count == 1

    def _with_current_version(self, question: dict | None) -> tuple[dict, dict] | None:
        if not question:
            return None
        version = self.db.question_versions.find_one({"_id": question["current_version_id"]})
        return (question, version) if version else None

    def find_archived_pair(self, question_id: str | ObjectId) -> tuple[dict, dict] | None:
        return self._with_current_version(self.db.questions.find_one({
            "_id": object_id(question_id, "question_id"),
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ARCHIVED",
        }))

    def list_archived(self, page: int, page_size: int,
                      owner_user_id: ObjectId | None) -> tuple[list[tuple[dict, dict]], int]:
        match: dict = {"schema_version": SCHEMA_VERSION, "lifecycle_status": "ARCHIVED"}
        if owner_user_id is not None:
            match["created_by_user_id"] = owner_user_id
        total = self.db.questions.count_documents(match)
        questions = self.db.questions.find(match).sort("updated_at", -1).skip((page - 1) * page_size).limit(page_size)
        pairs = [pair for pair in (self._with_current_version(item) for item in questions) if pair]
        return pairs, total

    def restore(self, question_id: str | ObjectId, *, audit: dict | None = None) -> tuple[dict, dict] | None:
        now = utc_now()
        question_oid = object_id(question_id, "question_id")
        with mongo_transaction() as session:
            question = self.db.questions.find_one(
                {"_id": question_oid, "schema_version": SCHEMA_VERSION, "lifecycle_status": "ARCHIVED"},
                session=session,
            )
            if not question:
                return None
            updates: dict = {"lifecycle_status": "ACTIVE", "archived_at": None, "updated_at": now}
            if question.get("review_status") == "PENDING":
                updates["review_assignment"] = {
                    "status": "UNASSIGNED", "reviewer_user_id": None, "assigned_by_user_id": None,
                    "assigned_at": None, "claimed_at": None, "lock_expires_at": None,
                    "last_released_at": now, "release_reason": "restored",
                }
            restored = self.db.questions.find_one_and_update(
                {"_id": question_oid, "lifecycle_status": "ARCHIVED"},
                {"$set": updates},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if restored and audit:
                write_audit_event(self.db, session=session, **audit)
        return self._with_current_version(restored)

    def withdraw_submission(self, question_id: str | ObjectId, *, expected_version_id: ObjectId,
                            fields: dict, audit: dict | None = None) -> tuple[dict, dict] | None:
        question_oid = object_id(question_id, "question_id")
        now = utc_now()
        with mongo_transaction() as session:
            updated = self.db.questions.find_one_and_update(
                {
                    "_id": question_oid,
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                    "current_version_id": expected_version_id,
                    "secondary_review.status": {"$ne": "AWAITING_SECONDARY"},
                    "$nor": [{
                        "review_assignment.status": "IN_REVIEW",
                        "review_assignment.lock_expires_at": {"$gt": now},
                    }],
                },
                {"$set": fields},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if updated and audit:
                write_audit_event(self.db, session=session, **audit)
        if updated:
            return self._with_current_version(updated)
        current = self.find_pair(question_oid)
        if not current:
            return None
        question, version = current
        if version["_id"] != expected_version_id or question.get("review_status") != "PENDING":
            raise RuntimeError("VERSION_CONFLICT")
        raise PermissionError("Reviewer đã nhận câu hỏi này hoặc đã có kết quả duyệt lần đầu nên không thể rút lại")

    def list_versions(self, question_id: str | ObjectId) -> list[dict]:
        question_oid = object_id(question_id, "question_id")
        if not self.db.questions.find_one(
            {"_id": question_oid, "schema_version": SCHEMA_VERSION},
            {"_id": 1},
        ):
            return []
        return list(
            self.db.question_versions.find({"question_id": question_oid}).sort(
                "version",
                -1,
            )
        )

    def update_sharing(self, question_id: str | ObjectId, fields: dict, *,
                       audit: dict | None = None) -> tuple[dict, dict] | None:
        """Sharing/ownership fields; ``audit`` is written in the same transaction."""
        normalized = normalize_sharing_fields(fields)
        normalized["updated_at"] = utc_now()
        with mongo_transaction() as session:
            question = self.db.questions.find_one_and_update(
                {
                    "_id": object_id(question_id, "question_id"),
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                },
                {"$set": normalized},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not question:
                return None
            version = self.db.question_versions.find_one({"_id": question["current_version_id"]}, session=session)
            if not version:
                raise RuntimeError("Question aggregate bị thiếu current version")
            if audit:
                write_audit_event(self.db, session=session, **audit)
        return question, version
