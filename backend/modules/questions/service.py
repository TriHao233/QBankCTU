from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bson import ObjectId

from core.bootstrap import SCHEMA_VERSION
from core.audit import record_audit_event
from core.config import resolve_path, settings
from core.database import get_database
from core.dependencies import CurrentUser, has_permission
from modules.catalog.subject_approval import ensure_subject_approved, subject_usable_by
from modules.questions.review_scope import ReviewerScope, reviewer_can_read
from modules.users.store import teaching_subject_ids, users_by_ids
from modules.questions.repository import (
    MongoQuestionRepository,
    MongoQuestionReferenceRepository,
    QuestionRepository,
    QuestionReferenceRepository,
    json_safe,
    object_id,
    serialize_question,
    utc_now,
)
from modules.questions.schemas import (
    QuestionCreateRequest,
    QuestionOwnerTransferRequest,
    QuestionSharingRequest,
    QuestionUpdateRequest,
)
from modules.generation.postprocessing import contains_exact_text
from modules.questions.review_limits import MIN_TAKEOVER_REASON_LENGTH, active_holder_id, released_assignment
from modules.questions.submission_checks import (
    SourceNotLocatedError,
    answer_quality_issues,
    check_grounding,
    duplicate_flag,
    evidence_texts,
    submission_schema_errors,
)

logger = logging.getLogger(__name__)

BLOOM_LEVELS = {
    1: ("REMEMBER", "Nhớ"),
    2: ("UNDERSTAND", "Hiểu"),
    3: ("APPLY", "Vận dụng"),
    4: ("ANALYZE", "Phân tích"),
    5: ("EVALUATE", "Đánh giá"),
    6: ("CREATE", "Sáng tạo"),
}

INITIAL_REVIEW_STATUSES = {"DRAFT", "PENDING"}
SUBMITTABLE_REVIEW_STATUSES = {"DRAFT", "NEEDS_REVISION"}
MIN_RESUBMIT_NOTE_LENGTH = 10
SOURCE_PAGE_TEXT_LIMIT = 6000


def stable_hash(value: dict) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_original_pdf_artifact(document: dict | None) -> dict | None:
    if not document:
        return None
    artifacts = document.get("artifacts") or []
    current = [
        artifact
        for artifact in artifacts
        if artifact.get("type") == "ORIGINAL_PDF" and artifact.get("is_current", True)
    ]
    candidates = current or [
        artifact for artifact in artifacts if artifact.get("type") == "ORIGINAL_PDF"
    ]
    for artifact in candidates:
        storage = artifact.get("storage") or {}
        if storage.get("provider") in {"LOCAL", "S3"} and storage.get("uri"):
            return artifact
    return None


def _source_page_numbers(source: dict, chunk: dict | None) -> list[int]:
    page_range = (chunk or {}).get("page_range") or source.get("page_range") or {}
    explicit_pages = page_range.get("pages") or []
    pages: list[int] = []
    for page in explicit_pages:
        try:
            pages.append(int(page))
        except (TypeError, ValueError):
            continue
    start = page_range.get("start")
    end = page_range.get("end")
    if start is not None and end is not None:
        try:
            start_int = int(start)
            end_int = int(end)
            pages.extend(range(start_int, end_int + 1))
        except (TypeError, ValueError):
            pass
    elif start is not None:
        try:
            pages.append(int(start))
        except (TypeError, ValueError):
            pass
    return sorted({page for page in pages if page > 0})


def _document_source_payload(
    document: dict | None,
    question_id: str,
) -> dict | None:
    if not document:
        return None
    current_processing = document.get("current_processing") or {}
    current_ocr_job_id = current_processing.get("ocr_job_id")
    current_chunk_set_id = current_processing.get("chunk_set_id")
    return json_safe(
        {
            "id": document["_id"],
            "title": document["title"],
            "original_filename": document["original_filename"],
            "page_count": document.get("page_count"),
            "current_ocr_job_id": current_ocr_job_id,
            "current_chunk_set_id": current_chunk_set_id,
            "pdf_available": _current_original_pdf_artifact(document) is not None,
            "pdf_url": f"/questions/{question_id}/source-pdf",
        }
    )


class QuestionService:
    def __init__(
        self,
        repository: QuestionRepository,
        references: QuestionReferenceRepository,
    ):
        self.repository = repository
        self.references = references

    @staticmethod
    def _owns_question(question: dict, version: dict, user_id: ObjectId) -> bool:
        """Only the question owner; version authors and document uploaders gain nothing."""
        owner = question.get("created_by_user_id")
        if owner is not None:
            return owner == user_id
        return version.get("created_by_user_id") == user_id

    @staticmethod
    def _can_manage_all(current_user: CurrentUser) -> bool:
        return current_user.role == "Admin" or has_permission(current_user, "questions.manage_all")

    @staticmethod
    def _can_review_all(current_user: CurrentUser) -> bool:
        return has_permission(current_user, "reviews.manage")

    @staticmethod
    def _teaching_subject_ids(current_user: CurrentUser) -> set[str]:
        return teaching_subject_ids(get_database(), current_user.id)

    def _is_shared_question(self, question: dict, current_user: CurrentUser, version: dict | None = None) -> bool:
        if not has_permission(current_user, "questions.use_shared_bank"):
            return False
        shared_with = set(question.get("shared_with_user_ids") or [])
        if current_user.id in shared_with:
            return True
        if question.get("shared_scope") != "SUBJECT" or not question.get("approved_version_id"):
            return False
        subject_id = question.get("subject_id") or (
            ((version or {}).get("classification") or {}).get("subject") or {}
        ).get("id")
        return bool(subject_id) and str(subject_id) in self._teaching_subject_ids(current_user)

    @staticmethod
    def _can_use_document(document: dict | None, current_user: CurrentUser) -> bool:
        if not document:
            return True
        if current_user.role == "Admin" or has_permission(current_user, "documents.manage_all"):
            return True
        if document.get("uploaded_by_user_id") == current_user.id:
            return True
        if not has_permission(current_user, "questions.use_shared_bank"):
            return False
        shared_with = set(document.get("shared_with_user_ids") or [])
        if current_user.id in shared_with:
            return True
        if document.get("shared_scope") != "SUBJECT":
            return False
        document_subjects = {str(item) for item in [document.get("subject_id"), *(document.get("subject_ids") or [])]
                             if item}
        return bool(document_subjects & QuestionService._teaching_subject_ids(current_user))

    def _reviewer_scope(self, current_user: CurrentUser | None) -> ReviewerScope | None:
        """Read scope of a reviewer who cannot manage every question; None when unrestricted."""
        if not current_user or self._can_manage_all(current_user) or not self._can_review_all(current_user):
            return None
        loader = getattr(self.references, "reviewer_subject_ids", None)
        return ReviewerScope(current_user.id, loader(current_user.id) if loader else [])

    def _ensure_read_access(
        self,
        pair: tuple[dict, dict] | None,
        current_user: CurrentUser | None,
    ) -> tuple[dict, dict] | None:
        """Return the pair when readable, None when it must look missing (out of reviewer scope)."""
        if not pair or not current_user or self._can_manage_all(current_user):
            return pair
        question, version = pair
        if self._owns_question(question, version, current_user.id) or self._is_shared_question(
            question, current_user, version,
        ):
            return pair
        scope = self._reviewer_scope(current_user)
        if scope is not None:
            has_decided = getattr(self.repository, "reviewer_has_decided", None)
            visible = reviewer_can_read(
                question, version, scope,
                lambda: bool(has_decided and has_decided(question["_id"], current_user.id)),
            )
            return pair if visible else None
        # Questions the user cannot see look missing rather than forbidden.
        return None

    def _is_primary_owner(self, question: dict, version: dict, user_id: ObjectId) -> bool:
        """The question owner; a shared editor who authored a version is not an owner."""
        return self._owns_question(question, version, user_id)

    @staticmethod
    def _is_shared_editor(question: dict, current_user: CurrentUser) -> bool:
        if not has_permission(current_user, "questions.use_shared_bank"):
            return False
        return current_user.id in set(question.get("shared_edit_user_ids") or [])

    def _ensure_write_access(
        self,
        pair: tuple[dict, dict] | None,
        current_user: CurrentUser | None,
        *,
        allow_shared_editor: bool = True,
    ) -> None:
        """Owners write; shared editors may edit and submit but not archive or reshare."""
        if not pair or not current_user or self._can_manage_all(current_user):
            return
        question, version = pair
        if allow_shared_editor:
            if self._owns_question(question, version, current_user.id) or self._is_shared_editor(
                question, current_user,
            ):
                return
        elif self._is_primary_owner(question, version, current_user.id):
            return
        if self._ensure_read_access(pair, current_user) is None:
            raise LookupError("Không tìm thấy câu hỏi")
        if not allow_shared_editor:
            raise PermissionError("Chỉ chủ sở hữu câu hỏi được thực hiện thao tác này")
        raise PermissionError("Bạn không có quyền chỉnh sửa câu hỏi này")

    def _sources(
        self,
        chunk_ids: list[str],
        expected_document_id: ObjectId | None = None,
    ) -> tuple[list[dict], ObjectId | None]:
        unique_chunk_ids = list(dict.fromkeys(chunk_ids))
        if not unique_chunk_ids:
            return [], expected_document_id
        sources = []
        resolved_document_id = expected_document_id
        for citation_order, chunk_id in enumerate(unique_chunk_ids, start=1):
            chunk_oid = object_id(chunk_id, "chunk_id")
            chunk = self.references.find_chunk(chunk_oid)
            if not chunk:
                raise ValueError(f"Chunk không tồn tại: {chunk_id}")
            chunk_document_id = chunk.get("document_id")
            if resolved_document_id is None:
                resolved_document_id = chunk_document_id
            elif chunk_document_id != resolved_document_id:
                raise ValueError("Các chunk nguồn phải thuộc cùng tài liệu")
            sources.append({
                "source_type": "CHUNK",
                "chunk_id": chunk_oid,
                "chunk_set_id": chunk["chunk_set_id"],
                "chunk_content_hash": chunk.get("content_hash"),
                "citation_order": citation_order,
                "is_primary": citation_order == 1,
                "scores": {},
                "context_excerpt": chunk.get("content", "")[:2000],
            })
        return sources, resolved_document_id

    def _document(self, document_id: str | ObjectId | None) -> dict | None:
        if document_id is None:
            return None
        document_oid = object_id(document_id, "document_id")
        document = self.references.find_document(document_oid)
        if not document:
            raise ValueError("Tài liệu nguồn không tồn tại hoặc đã lưu trữ")
        return document

    def _validate_subject(
        self,
        subject_id: str | ObjectId | None,
        actor: CurrentUser | None = None,
    ) -> dict | None:
        if subject_id is None:
            return None
        subject_oid = object_id(subject_id, "subject_id")
        subject = self.references.find_subject(subject_oid)
        if not subject or (actor is not None and not subject_usable_by(
            subject, actor.id, is_admin=self._can_manage_all(actor),
        )):
            raise ValueError("Học phần không tồn tại hoặc đã ngừng hoạt động")
        return subject

    def _question_subject(self, version: dict) -> dict | None:
        subject_id = ((version.get("classification") or {}).get("subject") or {}).get("id")
        if not subject_id:
            return None
        return self.references.find_subject(object_id(subject_id, "subject_id"))

    def _ensure_question_subject_approved(self, version: dict, action: str) -> None:
        subject = self._question_subject(version)
        if subject is not None:
            ensure_subject_approved(subject, action)

    def _validate_classification_refs(
        self,
        subject_id: str | ObjectId | None,
        chapter_id: str | ObjectId | None,
        actor: CurrentUser | None = None,
    ) -> dict | None:
        subject = self._validate_subject(subject_id, actor)
        if chapter_id is None:
            return subject
        if subject is None:
            raise ValueError("Chương phải thuộc một học phần")
        chapter_oid = object_id(chapter_id, "chapter_id")
        chapter_ids = {
            object_id(chapter.get("_id") or chapter.get("id"), "chapter_id")
            for chapter in subject.get("chapters", [])
            if chapter.get("_id") or chapter.get("id")
        }
        if chapter_oid not in chapter_ids:
            raise ValueError("Chương không thuộc học phần đã chọn")
        return subject

    def _clo_snapshots(
        self,
        subject_id: str | ObjectId | None,
        clo_ids: list[str] | None,
        *,
        subject: dict | None = None,
    ) -> list[dict]:
        if not clo_ids:
            return []
        if subject is None:
            subject = self._validate_subject(subject_id)
        if subject is None:
            raise ValueError("CLO phải thuộc một học phần")
        requested = {object_id(clo_id, "clo_id") for clo_id in clo_ids}
        snapshots = []
        for outcome in subject.get("learning_outcomes", []):
            outcome_id = outcome.get("_id") or outcome.get("id")
            if not outcome_id:
                continue
            outcome_oid = object_id(outcome_id, "clo_id")
            if outcome_oid not in requested:
                continue
            snapshots.append(
                {
                    "id": outcome_oid,
                    "code": outcome.get("clo_code") or outcome.get("code"),
                    "description": outcome.get("description", ""),
                    "target_weight": outcome.get("target_weight", 1.0),
                }
            )
        if len(snapshots) != len(requested):
            raise ValueError("Một hoặc nhiều CLO không thuộc học phần đã chọn")
        return snapshots

    @staticmethod
    def _validate_active_sources(sources: list[dict], document: dict | None) -> None:
        if not sources or not document:
            return
        chunk_sources = [source for source in sources if source.get("source_type") == "CHUNK"]
        if not chunk_sources:
            return
        active_chunk_set_id = (document.get("current_processing") or {}).get(
            "chunk_set_id"
        )
        if not active_chunk_set_id:
            raise ValueError("Tài liệu chưa có chunk set hiện hành")
        if any(
            source.get("chunk_set_id") != active_chunk_set_id
            for source in chunk_sources
        ):
            raise ValueError("Chunk nguồn không thuộc phiên xử lý hiện hành của tài liệu")

    def _classification(
        self,
        *,
        question_type: str,
        bloom_level: int | None,
        subject_id: str | ObjectId | None,
        chapter_id: str | ObjectId | None,
        difficulty: str | None = None,
        subject: dict | None = None,
    ) -> dict:
        bloom_code, bloom_name = BLOOM_LEVELS.get(bloom_level, ("", ""))
        if subject is None and subject_id is not None:
            subject = self._validate_subject(subject_id)
        return {
            "subject": {
                "id": subject.get("_id") if subject else None,
                "code": subject.get("subject_code", "") if subject else "",
                "name": subject.get("subject_name", "") if subject else "",
            },
            "chapter": {"id": object_id(chapter_id, "chapter_id") if chapter_id else None},
            "assessment_type": question_type.upper(),
            "bloom": {
                "level": bloom_level,
                "code": bloom_code,
                "name": bloom_name,
            },
            "difficulty": difficulty,
        }

    @staticmethod
    def _review_submission(
        current_user: CurrentUser | None,
        version: dict,
        submitted_at,
        fallback_user_id: ObjectId | None = None,
    ) -> dict:
        user_id = current_user.id if current_user else fallback_user_id
        subject = ((version.get("classification") or {}).get("subject") or {})
        subject_id = subject.get("id") if isinstance(subject, dict) else subject
        subject_code = (
            subject.get("code") or subject.get("subject_code") or ""
            if isinstance(subject, dict)
            else ""
        )
        subject_name = (
            subject.get("name") or subject.get("subject_name") or ""
            if isinstance(subject, dict)
            else ""
        )
        submitter_email = current_user.email if current_user else ""
        submitter_name = (
            current_user.display_name or submitter_email if current_user else ""
        )
        subject_snapshot = {
            "id": subject_id,
            "code": subject_code,
            "name": subject_name,
        }
        return {
            "submitted_by_user_id": user_id,
            "submitted_by": {
                "id": user_id,
                "email": submitter_email,
                "display_name": submitter_name,
            },
            "submitted_at": submitted_at,
            "subject_id": subject_id,
            "subject": subject_snapshot,
        }

    def create(
        self,
        payload: QuestionCreateRequest,
        created_by_user_id,
        *,
        actor_role: str | None = None,
        current_user: CurrentUser | None = None,
        origin: str = "MANUAL",
        generation_run_id: ObjectId | None = None,
        initial_review_status: str = "DRAFT",
    ) -> dict:
        initial_review_status = initial_review_status.upper()
        if initial_review_status not in INITIAL_REVIEW_STATUSES:
            raise ValueError("Trạng thái kiểm duyệt khởi tạo không hợp lệ")
        now = utc_now()
        question_id = ObjectId()
        version_id = ObjectId()
        document = self._document(payload.document_id)
        actor = current_user
        if actor is None and actor_role:
            actor = CurrentUser(
                id=created_by_user_id,
                firebase_uid="",
                email="",
                role=actor_role,
                is_active=True,
            )
        if (
            document
            and actor
            and not self._can_use_document(document, actor)
        ):
            raise PermissionError("Bạn không có quyền dùng tài liệu này để tạo câu hỏi")
        source_chunk_ids = payload.source_chunk_ids or (
            [payload.chunk_id] if payload.chunk_id else []
        )
        expected_document_id = document["_id"] if document else None
        sources, resolved_document_id = self._sources(
            source_chunk_ids,
            expected_document_id,
        )
        if resolved_document_id and document is None:
            document = self._document(resolved_document_id)
        source_context = str(payload.source_context or "").strip()
        if source_context and not document:
            raise ValueError("Đoạn minh chứng phải gắn với một tài liệu nguồn")
        if document and not sources and origin in {"MANUAL", "IMPORT"}:
            if not source_context:
                raise ValueError(
                    "Câu hỏi gắn tài liệu phải có đoạn minh chứng hoặc chunk nguồn"
                )
            if not self.references.document_contains_text(
                document["_id"],
                document.get("current_ocr_job_id"),
                source_context,
            ):
                raise ValueError("Đoạn minh chứng không xuất hiện trong tài liệu nguồn")
            excerpt_hash = hashlib.sha256(source_context.encode("utf-8")).hexdigest()
            sources = [{
                "source_type": "MANUAL_EXCERPT",
                "chunk_id": None,
                "chunk_set_id": document.get("current_chunk_set_id"),
                "chunk_content_hash": excerpt_hash,
                "citation_order": 1,
                "is_primary": True,
                "scores": {},
                "context_excerpt": source_context,
            }]
        self._validate_active_sources(sources, document)
        document_subject_id = document.get("subject_id") if document else None
        document_chapter_id = document.get("chapter_id") if document else None
        if (
            payload.subject_id
            and document_subject_id
            and object_id(payload.subject_id, "subject_id") != document_subject_id
        ):
            raise ValueError("Học phần câu hỏi không khớp với tài liệu nguồn")
        if (
            payload.chapter_id
            and document_chapter_id
            and object_id(payload.chapter_id, "chapter_id") != document_chapter_id
        ):
            raise ValueError("Chương câu hỏi không khớp với tài liệu nguồn")
        subject_id = document_subject_id or payload.subject_id
        chapter_id = document_chapter_id or payload.chapter_id
        validated_subject = self._validate_classification_refs(subject_id, chapter_id, actor)
        if initial_review_status == "PENDING" and validated_subject is not None:
            ensure_subject_approved(validated_subject, "gửi duyệt câu hỏi")
        classification = self._classification(
            question_type=payload.question_type,
            bloom_level=payload.bloom_level,
            subject_id=subject_id,
            chapter_id=chapter_id,
            difficulty=payload.difficulty.value if payload.difficulty else None,
            subject=validated_subject,
        )
        clos = self._clo_snapshots(
            subject_id,
            payload.clo_ids,
            subject=validated_subject,
        )
        question_data = deepcopy(payload.question_data)
        if source_context:
            question_data["model_source_context"] = source_context
        content_hash = stable_hash(
            {
                "content": payload.content,
                "question_data": question_data,
                "classification": classification,
                "clos": clos,
                "sources": sources,
            }
        )
        review_submission = (
            self._review_submission(
                current_user,
                {"classification": classification},
                now,
                fallback_user_id=created_by_user_id,
            )
            if initial_review_status == "PENDING"
            else {}
        )
        aggregate = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": f"Q-{str(question_id).upper()}",
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "NOT_STARTED",
            "review_status": initial_review_status,
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {},
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
            "shared_with_user_ids": [],
            "shared_scope": "PRIVATE",
            "secondary_review": {},
            "latest_review_id": None,
            "created_by_user_id": created_by_user_id,
            "subject_id": classification["subject"].get("id"),
            "review_submission": review_submission,
            "grounding_required": origin == "AI",
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": origin,
            "generation_run_id": generation_run_id,
            "document_id": resolved_document_id,
            "created_by_user_id": created_by_user_id,
            "generated_by_model_id": None,
            "classification": classification,
            "clos": clos,
            "content": payload.content,
            "question_data": question_data,
            "sources": sources,
            "keywords": [],
            "content_hash": content_hash,
            "change_note": "Initial version",
            "created_at": now,
        }
        question, current = self.repository.create(aggregate, version)
        return serialize_question(question, current)

    def create_generated_draft(
        self,
        item: dict,
        *,
        created_by_user_id,
        generation_run_id: ObjectId | None = None,
        candidate_chunk_ids: list[str] | tuple = (),
    ) -> dict:
        """Persist one accepted AI candidate as a DRAFT question.

        Sources are chosen here, never by the model: only retrieved chunks that
        contain the candidate's quote are cited. A candidate whose quote is in no
        retrieved chunk is not stored (SourceNotLocatedError) so the generator
        can replace it.
        """
        quote = str(item.get("source_context") or "").strip()[:4000]
        candidates = list(dict.fromkeys(str(chunk_id) for chunk_id in candidate_chunk_ids if chunk_id))
        cited = []
        evidence = [quote]
        for chunk_id in candidates:
            chunk = self.references.find_chunk(object_id(chunk_id, "chunk_id"))
            if chunk and quote and contains_exact_text(chunk.get("content") or "", quote):
                cited.append(chunk_id)
                evidence.append(chunk.get("content") or "")
        warnings = list(item.get("validation_warnings") or [])
        if not cited:
            raise SourceNotLocatedError()
        draft_version = {
            "classification": {"assessment_type": str(getattr(item.get("question_type"), "value", None)
                                                      or item.get("question_type") or "")},
            "question_data": {
                "options": item.get("options"),
                "correct_answer": item.get("correct_answer"),
                "model_source_context": quote,
            },
        }
        warnings.extend(message for _flag, message in answer_quality_issues(draft_version, evidence))
        bloom_digits = "".join(ch for ch in str(item.get("bloom_level") or "")[:1] if ch.isdigit())
        payload = QuestionCreateRequest(
            content=item["question"],
            question_type=item["question_type"],
            bloom_level=int(bloom_digits) if bloom_digits else None,
            difficulty=item.get("difficulty") or None,
            question_data={
                "options": item.get("options"),
                "correct_answer": item.get("correct_answer"),
                "explanation": item.get("explanation"),
                "source_keywords": item.get("source_keywords") or [],
                "false_mutation": item.get("false_mutation"),
                "validation_warnings": warnings,
            },
            document_id=item.get("document_id"),
            source_context=quote or None,
            source_chunk_ids=cited,
            clo_ids=item.get("clo_ids") or [],
        )
        return self.create(
            payload,
            created_by_user_id,
            origin="AI",
            generation_run_id=generation_run_id,
        )

    def get(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
    ) -> dict | None:
        pair = self._ensure_read_access(self.repository.find_pair(question_id), current_user)
        return serialize_question(*pair) if pair else None

    def duplicate(self, question_id: str, current_user: CurrentUser) -> dict | None:
        pair = self._ensure_read_access(self.repository.find_pair(question_id), current_user)
        if not pair:
            return None

        source_question, source_version = pair
        is_owner_level = self._can_manage_all(current_user) or self._owns_question(
            source_question, source_version, current_user.id,
        )
        if not is_owner_level:
            if not self._is_shared_question(source_question, current_user, source_version):
                # Reviewer read scope lets a reviewer inspect a question, not copy it into their bank.
                raise PermissionError("Chỉ được nhân bản câu hỏi bạn sở hữu hoặc được chia sẻ")
            approved_id = source_question.get("approved_version_id")
            approved = next(
                (item for item in self.repository.list_versions(source_question["_id"])
                 if approved_id is not None and str(item.get("_id")) == str(approved_id)),
                None,
            ) if approved_id is not None else None
            if approved is None:
                raise ValueError("Chỉ nhân bản được câu hỏi đã có phiên bản được duyệt")
            source_version = approved
        now = utc_now()
        new_question_id = ObjectId()
        new_version_id = ObjectId()
        classification = deepcopy(source_version.get("classification") or {})
        clos = deepcopy(source_version.get("clos") or [])
        question_data = deepcopy(source_version.get("question_data") or {})
        sources = deepcopy(source_version.get("sources") or [])
        content = source_version.get("content") or ""
        content_hash = stable_hash(
            {
                "content": content,
                "question_data": question_data,
                "classification": classification,
                "clos": clos,
                "sources": sources,
            }
        )

        aggregate = {
            "_id": new_question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": f"Q-{str(new_question_id).upper()}",
            "current_version": 1,
            "current_version_id": new_version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "NOT_STARTED",
            "review_status": "DRAFT",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {},
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
            "shared_with_user_ids": [],
            "shared_scope": "PRIVATE",
            "secondary_review": {},
            "latest_review_id": None,
            "created_by_user_id": current_user.id,
            "subject_id": (classification.get("subject") or {}).get("id"),
            "review_submission": {},
            "grounding_required": bool(source_question.get("grounding_required")),
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        version = {
            "_id": new_version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": new_question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": source_version.get("document_id"),
            "created_by_user_id": current_user.id,
            "generated_by_model_id": None,
            "classification": classification,
            "clos": clos,
            "content": content,
            "question_data": question_data,
            "sources": sources,
            "keywords": deepcopy(source_version.get("keywords") or []),
            "content_hash": content_hash,
            "change_note": f"Duplicated from {source_question.get('question_code') or question_id}",
            "created_at": now,
        }
        question, current = self.repository.create(aggregate, version)
        return serialize_question(question, current)

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
        assigned_to: str | None = None,
        creator_user_id: str | None = None,
        waiting_hours_min: float | None = None,
        overdue_only: bool = False,
        sla_breached_only: bool = False,
        override_only: bool = False,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        submitted_from: datetime | None = None,
        submitted_to: datetime | None = None,
        include_status_counts: bool = False,
        sort_by: str = "priority",
        source_presence: str | None = None,
        secondary_status: str | None = None,
        current_user: CurrentUser | None = None,
    ) -> dict:
        if created_from and created_from.tzinfo is None:
            created_from = created_from.replace(tzinfo=timezone.utc)
        if created_to and created_to.tzinfo is None:
            created_to = created_to.replace(tzinfo=timezone.utc)
        if created_from and created_to and created_from > created_to:
            raise ValueError("created_from phải trước hoặc bằng created_to")
        if submitted_from and submitted_from.tzinfo is None:
            submitted_from = submitted_from.replace(tzinfo=timezone.utc)
        if submitted_to and submitted_to.tzinfo is None:
            submitted_to = submitted_to.replace(tzinfo=timezone.utc)
        if submitted_from and submitted_to and submitted_from > submitted_to:
            raise ValueError("submitted_from phải trước hoặc bằng submitted_to")
        if sort_by not in {"priority", "oldest", "newest", "ai_lowest", "updated"}:
            raise ValueError("Kiểu sắp xếp hàng kiểm duyệt không hợp lệ")
        if source_presence not in {None, "WITH_SOURCE", "MISSING_SOURCE"}:
            raise ValueError("Bộ lọc nguồn không hợp lệ")
        if secondary_status not in {None, "AWAITING_SECONDARY", "COMPLETED"}:
            raise ValueError("Bộ lọc duyệt lần hai không hợp lệ")
        owner_user_id = (
            current_user.id
            if current_user
            and not self._can_manage_all(current_user)
            and not self._can_review_all(current_user)
            else None
        )
        assigned_reviewer_user_id = None
        if assigned_to == "me" and current_user:
            assigned_reviewer_user_id = current_user.id
        elif assigned_to:
            assigned_reviewer_user_id = object_id(assigned_to, "assigned_to")
        creator_oid = (
            object_id(creator_user_id, "creator_user_id")
            if creator_user_id
            else None
        )
        waiting_since = None
        if waiting_hours_min is not None:
            if waiting_hours_min < 0:
                raise ValueError("waiting_hours_min không hợp lệ")
            waiting_since = utc_now() - timedelta(hours=waiting_hours_min)
        overdue_at = utc_now() if overdue_only else None
        if sla_breached_only:
            sla_cutoff = utc_now() - timedelta(hours=max(1, settings.review_sla_hours))
            submitted_to = min(submitted_to, sla_cutoff) if submitted_to else sla_cutoff
        scope_options: dict = {}
        reviewer_scope = self._reviewer_scope(current_user)
        uses_shared = bool(current_user) and has_permission(current_user, "questions.use_shared_bank")
        if reviewer_scope is not None:
            scope_options = {
                "reviewer_scope": reviewer_scope,
                "shared_subject_ids": sorted(self._teaching_subject_ids(current_user)) if uses_shared else [],
                "include_shared": uses_shared,
            }
        elif owner_user_id is not None:
            scope_options = {
                "shared_subject_ids": sorted(self._teaching_subject_ids(current_user)) if uses_shared else [],
                "include_shared": uses_shared,
            }

        list_result = self.repository.list(
            page,
            page_size,
            review_status,
            search,
            question_type=question_type,
            bloom_level=bloom_level,
            document_id=document_id,
            subject_id=subject_id,
            chapter_id=chapter_id,
            clo_id=clo_id,
            difficulty=difficulty,
            quality_color=quality_color,
            min_score=min_score,
            publication_status=publication_status,
            evaluation_status=evaluation_status,
            assignment_status=assignment_status,
            assigned_reviewer_user_id=assigned_reviewer_user_id,
            creator_user_id=creator_oid,
            visible_to_user_id=owner_user_id,
            **scope_options,
            waiting_since=waiting_since,
            overdue_at=overdue_at,
            override_only=override_only,
            created_from=created_from,
            created_to=created_to,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            include_status_counts=include_status_counts,
            sort_by=sort_by,
            source_presence=source_presence,
            secondary_status=secondary_status,
        )
        if len(list_result) == 3:
            pairs, total, status_counts = list_result
        else:
            pairs, total = list_result
            status_counts = {}
        return {
            "items": [serialize_question(question, version) for question, version in pairs],
            "total": total,
            "page": page,
            "page_size": page_size,
            "status_counts": status_counts,
        }

    def versions(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
    ) -> list[dict] | None:
        pair = self._ensure_read_access(self.repository.find_pair(question_id), current_user)
        if not pair:
            return None
        versions = self.repository.list_versions(question_id)
        if not versions:
            return None
        return [
            json_safe(
                {
                    "id": version["_id"],
                    "version": version["version"],
                    "origin": version["origin"],
                    "generation_run_id": version.get("generation_run_id"),
                    "document_id": version.get("document_id"),
                    "created_by_user_id": version.get("created_by_user_id"),
                    "generated_by_model_id": version.get("generated_by_model_id"),
                    "classification": version["classification"],
                    "clos": version.get("clos") or [],
                    "content": version["content"],
                    "question_data": version["question_data"],
                    "sources": version.get("sources") or [],
                    "keywords": version.get("keywords") or [],
                    "content_hash": version["content_hash"],
                    "change_note": version.get("change_note", ""),
                    "created_at": version["created_at"],
                }
            )
            for version in versions
        ]

    def source_viewer(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
    ) -> dict | None:
        pair = self._ensure_read_access(self.repository.find_pair(question_id), current_user)
        if not pair:
            return None
        question, version = pair
        sources = version.get("sources") or []
        document = None
        if version.get("document_id"):
            document = self.references.find_document(version["document_id"])
        if document is None:
            for source in sources:
                chunk_id = source.get("chunk_id")
                if not chunk_id:
                    continue
                chunk = self.references.find_chunk(object_id(chunk_id, "chunk_id"))
                if chunk and chunk.get("document_id"):
                    document = self.references.find_document(chunk["document_id"])
                    break
        current_processing = (document or {}).get("current_processing") or {}
        current_chunk_set_id = current_processing.get("chunk_set_id")
        current_ocr_job_id = current_processing.get("ocr_job_id")
        warnings = []
        if sources and not document:
            warnings.append("Tài liệu nguồn không còn khả dụng")

        items = []
        for order, source in enumerate(sources, start=1):
            chunk = None
            chunk_id = source.get("chunk_id")
            source_warnings = []
            if chunk_id:
                chunk = self.references.find_chunk(object_id(chunk_id, "chunk_id"))
            if chunk_id and not chunk:
                source_warnings.append("Chunk nguồn không còn tồn tại")

            source_chunk_set_id = source.get("chunk_set_id") or (chunk or {}).get("chunk_set_id")
            is_current_chunk_set = None
            if current_chunk_set_id is not None and source_chunk_set_id is not None:
                is_current_chunk_set = source_chunk_set_id == current_chunk_set_id
                if not is_current_chunk_set:
                    source_warnings.append("Nguồn không còn thuộc chunk set hiện hành")

            source_hash = source.get("chunk_content_hash")
            current_hash = (chunk or {}).get("content_hash")
            content_hash_matches = None
            if source_hash and current_hash:
                content_hash_matches = source_hash == current_hash
                if not content_hash_matches:
                    source_warnings.append("Nội dung chunk đã thay đổi so với snapshot câu hỏi")

            page_numbers = _source_page_numbers(source, chunk)
            if (
                not page_numbers
                and source.get("source_type") == "MANUAL_EXCERPT"
                and document
                and current_ocr_job_id
            ):
                page_numbers = self.references.find_excerpt_pages(
                    document["_id"],
                    current_ocr_job_id,
                    source.get("context_excerpt") or "",
                )
            page_records = []
            if document and current_ocr_job_id:
                page_records = self.references.find_pages(
                    document["_id"],
                    current_ocr_job_id,
                    page_numbers,
                )
            page_map = {int(page.get("page_number", 0)): page for page in page_records}
            pages = [
                {
                    "page_number": page_number,
                    "text": (
                        page_map.get(page_number, {}).get("cleaned_text")
                        or page_map.get(page_number, {}).get("raw_text")
                        or ""
                    )[:SOURCE_PAGE_TEXT_LIMIT],
                    "formula_blocks": page_map.get(page_number, {}).get("formula_blocks") or [],
                }
                for page_number in page_numbers
            ]

            items.append(
                {
                    "citation_order": int(source.get("citation_order") or order),
                    "source_type": source.get("source_type") or "CHUNK",
                    "is_primary": bool(source.get("is_primary")),
                    "chunk_id": chunk_id,
                    "chunk_no": (chunk or {}).get("chunk_no"),
                    "chunk_set_id": source_chunk_set_id,
                    "current_chunk_set_id": current_chunk_set_id,
                    "is_current_chunk_set": is_current_chunk_set,
                    "chunk_content_hash": source_hash,
                    "current_content_hash": current_hash,
                    "content_hash_matches": content_hash_matches,
                    "page_range": (chunk or {}).get("page_range") or source.get("page_range") or (
                        {"pages": page_numbers} if page_numbers else {}
                    ),
                    "heading": (chunk or {}).get("heading") or {},
                    "content_type": (chunk or {}).get("content_type"),
                    "semantic_type": (chunk or {}).get("semantic_type"),
                    "information_density": (chunk or {}).get("information_density"),
                    "context_excerpt": source.get("context_excerpt") or (chunk or {}).get("content", "")[:2000],
                    "chunk_text": ((chunk or {}).get("content") or source.get("context_excerpt") or "")[:4000],
                    "pages": pages,
                    "warnings": source_warnings,
                }
            )

        return json_safe(
            {
                "question_id": question["_id"],
                "question_code": question["question_code"],
                "version_id": version["_id"],
                "version": version["version"],
                "document": _document_source_payload(document, question_id),
                "items": items,
                "warnings": warnings,
            }
        )

    def source_pdf_artifact(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
    ) -> dict | None:
        pair = self._ensure_read_access(self.repository.find_pair(question_id), current_user)
        if not pair:
            return None
        _, version = pair
        document = None
        if version.get("document_id"):
            document = self.references.find_document(version["document_id"])
        if document is None:
            for source in version.get("sources") or []:
                chunk_id = source.get("chunk_id")
                if not chunk_id:
                    continue
                chunk = self.references.find_chunk(object_id(chunk_id, "chunk_id"))
                if chunk and chunk.get("document_id"):
                    document = self.references.find_document(chunk["document_id"])
                    break
        if document is not None and current_user is not None and not self._can_use_document(document, current_user):
            question, _ = pair
            via_review = (
                not self._owns_question(question, version, current_user.id)
                and not self._is_shared_question(question, current_user, version)
                and self._reviewer_scope(current_user) is not None
            )
            if not via_review:
                raise PermissionError("Bạn không có quyền tải tài liệu nguồn của câu hỏi này")
        artifact = _current_original_pdf_artifact(document)
        if not artifact:
            return None
        storage = artifact.get("storage") or {}
        uri = storage.get("uri")
        if not uri:
            return None
        provider = storage.get("provider") or "LOCAL"
        if provider == "LOCAL" and not Path(uri).is_absolute():
            uri = str(resolve_path(uri))
        return {
            "provider": provider,
            "uri": uri,
            "filename": (document or {}).get("original_filename") or "source.pdf",
            "mime_type": artifact.get("mime_type") or "application/pdf",
        }

    def update(
        self,
        question_id: str,
        payload: QuestionUpdateRequest,
        created_by_user_id,
        actor_role: str | None = None,
        current_user: CurrentUser | None = None,
        notifications: list[dict] | None = None,
    ) -> dict | None:
        pair = self.repository.find_pair(question_id)
        if not pair:
            return None
        if current_user:
            self._ensure_write_access(pair, current_user)
        elif actor_role and actor_role != "Admin" and not self._owns_question(
            pair[0],
            pair[1],
            created_by_user_id,
        ):
            raise PermissionError("Bạn không có quyền chỉnh sửa câu hỏi này")
        actor = current_user
        if actor is None and actor_role:
            actor = CurrentUser(
                id=created_by_user_id,
                firebase_uid="",
                email="",
                role=actor_role,
                is_active=True,
            )
        _, current = pair
        classification = current["classification"]
        current_subject_id = (classification.get("subject") or {}).get("id")
        subject_changed = (
            payload.subject_id is not None
            and str(object_id(payload.subject_id, "subject_id")) != str(current_subject_id or "")
        )
        if subject_changed and current_subject_id:
            raise ValueError("Không thể đổi học phần của câu hỏi; hãy tạo câu hỏi mới trong học phần đích")
        validated_subject = None
        if any(
            value is not None
            for value in (
                payload.question_type,
                payload.bloom_level,
                payload.subject_id,
                payload.chapter_id,
                payload.difficulty,
            )
        ):
            next_subject_id = (
                payload.subject_id
                if payload.subject_id is not None
                else classification["subject"].get("id")
            )
            next_chapter_id = (
                payload.chapter_id
                if payload.chapter_id is not None
                else classification["chapter"].get("id")
            )
            current_document = self._document(current.get("document_id"))
            if current_document:
                document_subject_id = current_document.get("subject_id")
                document_chapter_id = current_document.get("chapter_id")
                if (
                    document_subject_id
                    and object_id(next_subject_id, "subject_id")
                    != document_subject_id
                ):
                    raise ValueError(
                        "Học phần câu hỏi không khớp với tài liệu nguồn"
                    )
                if (
                    document_chapter_id
                    and object_id(next_chapter_id, "chapter_id")
                    != document_chapter_id
                ):
                    raise ValueError(
                        "Chương câu hỏi không khớp với tài liệu nguồn"
                    )
            validated_subject = self._validate_classification_refs(
                next_subject_id,
                next_chapter_id,
                actor,
            )
            classification = self._classification(
                question_type=payload.question_type or classification["assessment_type"],
                bloom_level=(
                    payload.bloom_level
                    if payload.bloom_level is not None
                    else classification["bloom"]["level"]
                ),
                subject_id=(
                    payload.subject_id
                    if payload.subject_id is not None
                    else classification["subject"].get("id")
                ),
                chapter_id=(
                    payload.chapter_id
                    if payload.chapter_id is not None
                    else classification["chapter"].get("id")
                ),
                difficulty=(
                    payload.difficulty.value
                    if payload.difficulty is not None
                    else classification.get("difficulty")
                ),
                subject=validated_subject,
            )
        content = payload.content or current["content"]
        question_data = deepcopy(current["question_data"])
        if payload.question_data is not None:
            question_data.update(payload.question_data)
        if payload.clo_ids is not None:
            clos = self._clo_snapshots(
                classification["subject"].get("id"),
                payload.clo_ids,
                subject=validated_subject,
            )
        elif subject_changed:
            clos = []
        else:
            clos = current.get("clos", [])
        current_document_id = current.get("document_id")
        resolved_document_id = current_document_id
        if payload.source_chunk_ids is not None:
            sources, resolved_document_id = self._sources(
                payload.source_chunk_ids,
                current_document_id,
            )
        elif payload.chunk_id is not None:
            sources, resolved_document_id = self._sources(
                [payload.chunk_id],
                current_document_id,
            )
        else:
            sources = current["sources"]
        if current_document_id is None and resolved_document_id is not None:
            source_document = self._document(resolved_document_id)
            source_subject_id = source_document.get("subject_id")
            source_chapter_id = source_document.get("chapter_id")
            current_subject_id = classification["subject"].get("id")
            current_chapter_id = classification["chapter"].get("id")
            if (
                current_subject_id
                and source_subject_id
                and current_subject_id != source_subject_id
            ):
                raise ValueError("Học phần câu hỏi không khớp với tài liệu nguồn")
            if (
                current_chapter_id
                and source_chapter_id
                and current_chapter_id != source_chapter_id
            ):
                raise ValueError("Chương câu hỏi không khớp với tài liệu nguồn")
            next_subject_id = source_subject_id or current_subject_id
            next_chapter_id = source_chapter_id or current_chapter_id
            validated_subject = self._validate_classification_refs(
                next_subject_id,
                next_chapter_id,
                actor,
            )
            classification = self._classification(
                question_type=classification["assessment_type"],
                bloom_level=classification["bloom"]["level"],
                subject_id=next_subject_id,
                chapter_id=next_chapter_id,
                difficulty=classification.get("difficulty"),
                subject=validated_subject,
            )
        if payload.source_chunk_ids is not None or payload.chunk_id is not None:
            document = self._document(resolved_document_id)
            if (
                document
                and actor
                and not self._can_use_document(document, actor)
            ):
                raise PermissionError("Bạn không có quyền dùng tài liệu này để cập nhật câu hỏi")
            self._validate_active_sources(sources, document)
        content_hash = stable_hash(
            {
                "content": content,
                "question_data": question_data,
                "classification": classification,
                "clos": clos,
                "sources": sources,
            }
        )
        version_created_at = utc_now()
        next_version = {
            "origin": "MANUAL",
            "created_by_user_id": created_by_user_id,
            "document_id": resolved_document_id,
            "classification": classification,
            "clos": clos,
            "content": content,
            "question_data": question_data,
            "sources": sources,
            "content_hash": content_hash,
            "change_note": payload.change_note,
            "created_at": version_created_at,
        }
        updated = self.repository.create_version(
            question_id,
            payload.expected_version,
            next_version,
            **_transactional(notifications),
        )
        return serialize_question(*updated) if updated else None

    def update_sharing(
        self,
        question_id: str,
        payload: QuestionSharingRequest,
        current_user: CurrentUser,
    ) -> dict | None:
        pair = self.repository.find_pair(question_id)
        if not pair:
            return None
        question, version = pair
        self._ensure_write_access(pair, current_user, allow_shared_editor=False)
        owner_id = str(question.get("created_by_user_id") or "")
        editors = [item for item in dict.fromkeys(payload.shared_edit_user_ids) if item != owner_id]
        # Editors can always read, so they are part of the view list too.
        viewers = [item for item in dict.fromkeys([*payload.shared_with_user_ids, *editors]) if item != owner_id]
        if payload.shared_scope != "PRIVATE" or viewers:
            self._ensure_question_subject_approved(version, "chia sẻ câu hỏi")
        if payload.shared_scope == "SUBJECT" and not question.get("approved_version_id"):
            raise ValueError("Chỉ chia sẻ toàn học phần cho câu hỏi đã được duyệt")
        before = {
            "shared_with_user_ids": [str(item) for item in question.get("shared_with_user_ids") or []],
            "shared_edit_user_ids": [str(item) for item in question.get("shared_edit_user_ids") or []],
            "shared_scope": question.get("shared_scope") or "PRIVATE",
        }
        after = {"shared_with_user_ids": viewers, "shared_edit_user_ids": editors,
                 "shared_scope": payload.shared_scope}
        updated = self.repository.update_sharing(
            question_id,
            after,
            audit={
                "action": "question.sharing_update",
                "entity_type": "question",
                "entity_id": question["_id"],
                "actor_user_id": current_user.id,
                "actor_role": current_user.role,
                "before": before,
                "after": after,
                "metadata": {"version_id": str(version["_id"])},
            },
        )
        return serialize_question(*updated) if updated else None

    def transfer_owner(self, question_id: str, payload: QuestionOwnerTransferRequest,
                       current_user: CurrentUser) -> dict | None:
        if current_user.role != "Admin":
            raise PermissionError("Chỉ Admin được chuyển chủ sở hữu câu hỏi")
        pair = self.repository.find_pair(question_id)
        if not pair:
            return None
        question, version = pair
        new_owner = object_id(payload.new_owner_user_id, "new_owner_user_id")
        users = users_by_ids(get_database(), [new_owner], active_only=True)
        if not users:
            raise ValueError("Người nhận không tồn tại hoặc đã bị vô hiệu hóa")
        if users[0].get("role") not in {"Teacher", "Admin"}:
            raise ValueError("Chỉ chuyển câu hỏi cho giảng viên hoặc Admin")
        previous = question.get("created_by_user_id")
        if previous == new_owner:
            return serialize_question(question, version)
        fields = {
            "created_by_user_id": new_owner,
            "shared_with_user_ids": [item for item in question.get("shared_with_user_ids") or [] if item != new_owner],
            "shared_edit_user_ids": [item for item in question.get("shared_edit_user_ids") or [] if item != new_owner],
        }
        updated = self.repository.update_sharing(question_id, fields, audit={
            "action": "question.owner_transferred",
            "entity_type": "question",
            "entity_id": question["_id"],
            "actor_user_id": current_user.id,
            "actor_role": current_user.role,
            "before": {"created_by_user_id": previous},
            "after": {"created_by_user_id": new_owner},
            "metadata": {"version_id": str(version["_id"]), "reason": payload.reason.strip()},
        })
        return serialize_question(*updated) if updated else None

    def _submission_checks(self, question: dict, version: dict, current_user: CurrentUser | None) -> dict:
        errors = submission_schema_errors(version)
        if errors:
            raise ValueError("Câu hỏi chưa hợp lệ để gửi duyệt: " + "; ".join(errors))
        grounding = check_grounding(
            version,
            grounding_required=bool(question.get("grounding_required")),
            find_chunk=lambda chunk_id: self.references.find_chunk(chunk_id),
        )
        flags = []
        duplicate_of = None
        document_id = version.get("document_id")
        subject_id = question.get("subject_id") or ((version.get("classification") or {}).get("subject") or {}).get("id")
        if document_id or subject_id:
            try:
                from modules.generation.mongodb import get_existing_question_texts
                viewer_id = current_user.id if current_user else question.get("created_by_user_id")
                texts = get_existing_question_texts(
                    document_id,
                    viewer_user_id=viewer_id,
                    include_shared=True,
                    shared_subject_ids=self._teaching_subject_ids(current_user) if current_user else (),
                    exclude_question_id=question["_id"],
                    subject_id=str(subject_id) if subject_id else None,
                )
                duplicate_of = duplicate_flag(version.get("content") or "", texts)
            except Exception as exc:
                # Duplicate detection only flags; it must not block a valid submission.
                logger.warning("Duplicate check failed for question %s: %s", question.get("_id"), exc)
        if duplicate_of:
            flags.append(duplicate_of)
        if grounding["status"] == "weak":
            flags.append("weak_grounding")
        if grounding["status"] == "none":
            flags.append("ungrounded")
        warnings = list(grounding["warnings"])
        evidence = evidence_texts(version, lambda chunk_id: self.references.find_chunk(chunk_id))
        for flag, message in answer_quality_issues(version, evidence):
            flags.append(flag)
            warnings.append(message)
        return {"grounding": grounding, "flags": flags, "warnings": warnings}

    def submit_for_review(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
        notifications: list[dict] | None = None,
        response_note: str = "",
    ) -> dict | None:
        pair = self.repository.find_pair(question_id)
        if not pair:
            return None
        self._ensure_write_access(pair, current_user)
        question, version = pair
        review_status = question.get("review_status")
        if review_status == "PENDING":
            return serialize_question(question, version)
        if review_status not in SUBMITTABLE_REVIEW_STATUSES:
            raise ValueError("Chỉ câu hỏi nháp hoặc cần sửa mới được gửi duyệt")
        response_note = (response_note or "").strip()
        # Any edit returns the question to DRAFT, so NEEDS_REVISION here means unchanged.
        if review_status == "NEEDS_REVISION" and len(response_note) < MIN_RESUBMIT_NOTE_LENGTH:
            raise ValueError(
                "Câu hỏi chưa được sửa; cần ghi chú phản hồi Reviewer "
                f"(ít nhất {MIN_RESUBMIT_NOTE_LENGTH} ký tự) khi gửi lại"
            )
        self._ensure_question_subject_approved(version, "gửi duyệt câu hỏi")
        checks = self._submission_checks(question, version, current_user)

        submission = self._review_submission(
            current_user,
            version,
            utc_now(),
            fallback_user_id=question.get("created_by_user_id"),
        )
        submission["checks"] = checks
        if response_note:
            submission["response_note"] = response_note
            submission["resubmitted_unchanged"] = review_status == "NEEDS_REVISION"
        updated = self.repository.update_review_status(
            question_id,
            SUBMITTABLE_REVIEW_STATUSES,
            "PENDING",
            review_submission=submission,
            **_transactional(notifications),
        )
        if not updated:
            raise RuntimeError("VERSION_CONFLICT")
        record_audit_event(
            action="question.submit_review",
            entity_type="question",
            entity_id=question["_id"],
            actor_user_id=current_user.id if current_user else None,
            actor_role=current_user.role if current_user else None,
            before={"review_status": review_status},
            after={"review_status": "PENDING"},
            metadata={
                "version_id": str(version["_id"]),
                "review_submission": json_safe(submission),
            },
        )
        return serialize_question(*updated)

    def archive(
        self,
        question_id: str,
        current_user: CurrentUser | None = None,
        reason: str | None = None,
        on_reviewer_displaced=None,
    ) -> bool:
        """Archive a question; a PENDING one must be withdrawn first unless an Admin forces it.

        ``on_reviewer_displaced(question, version, reviewer_user_id, reason)`` is called
        after a forced archive took the question from a reviewer holding it.
        """
        pair = self.repository.find_pair(question_id)
        if not pair:
            return False
        self._ensure_write_access(pair, current_user, allow_shared_editor=False)
        question, version = pair
        review_status = question.get("review_status")
        reason = (reason or "").strip()
        now = utc_now()
        extra_fields = None
        holder = None
        if review_status == "PENDING":
            if current_user is not None and current_user.role != "Admin":
                raise ValueError("Câu hỏi đang chờ duyệt; hãy rút lại khỏi hàng đợi trước khi lưu trữ")
            if len(reason) < MIN_TAKEOVER_REASON_LENGTH:
                raise ValueError(
                    f"Cần nêu lý do (ít nhất {MIN_TAKEOVER_REASON_LENGTH} ký tự) để lưu trữ câu hỏi đang chờ duyệt"
                )
            holder = active_holder_id(question.get("review_assignment"), now)
            extra_fields = {"review_assignment": released_assignment({
                "status": "UNASSIGNED", "reviewer_user_id": None, "assigned_by_user_id": None,
                "assigned_at": None, "claimed_at": None, "lock_expires_at": None,
                "last_released_at": now, "release_reason": "archived",
            }, question.get("review_assignment"))}
        metadata = {"version_id": str(version["_id"]), "review_status": review_status}
        if review_status == "PENDING":
            metadata.update(forced=True, reason=reason,
                            previous_reviewer_user_id=str(holder) if holder else None)
        archived = self.repository.archive(
            question_id,
            audit={
                "action": "question.archived",
                "entity_type": "question",
                "entity_id": question["_id"],
                "actor_user_id": current_user.id if current_user else None,
                "actor_role": current_user.role if current_user else None,
                "before": {"lifecycle_status": question.get("lifecycle_status") or "ACTIVE"},
                "after": {"lifecycle_status": "ARCHIVED"},
                "metadata": metadata,
            },
            expected_review_status=review_status,
            extra_fields=extra_fields,
        )
        if not archived:
            if self.repository.find_pair(question_id):
                raise RuntimeError("VERSION_CONFLICT")
            return False
        if holder is not None and on_reviewer_displaced is not None:
            on_reviewer_displaced(question, version, holder, reason)
        return True

    def _ensure_owner_or_manager(self, question: dict, current_user: CurrentUser | None) -> None:
        if current_user is None or self._can_manage_all(current_user):
            return
        if question.get("created_by_user_id") != current_user.id:
            raise LookupError("Không tìm thấy câu hỏi")

    def list_archived(self, page: int, page_size: int, current_user: CurrentUser) -> dict:
        owner = None if self._can_manage_all(current_user) else current_user.id
        pairs, total = self.repository.list_archived(page, page_size, owner)
        return {
            "items": [serialize_question(question, version) for question, version in pairs],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def restore(self, question_id: str, current_user: CurrentUser) -> dict | None:
        """Undo an archive; the question returns with its review status and approved version intact."""
        pair = self.repository.find_archived_pair(question_id)
        if not pair:
            return None
        question, version = pair
        self._ensure_owner_or_manager(question, current_user)
        restored = self.repository.restore(question_id, audit={
            "action": "question.restored",
            "entity_type": "question",
            "entity_id": question["_id"],
            "actor_user_id": current_user.id,
            "actor_role": current_user.role,
            "before": {"lifecycle_status": "ARCHIVED"},
            "after": {"lifecycle_status": "ACTIVE"},
            "metadata": {"version_id": str(version["_id"]), "review_status": question.get("review_status")},
        })
        return serialize_question(*restored) if restored else None

    def withdraw_submission(self, question_id: str, expected_version: int | None,
                            current_user: CurrentUser) -> dict | None:
        """Pull a PENDING question back to DRAFT while no reviewer has claimed it."""
        pair = self.repository.find_pair(question_id)
        if not pair:
            return None
        question, version = pair
        self._ensure_write_access(pair, current_user)
        if question.get("review_status") != "PENDING":
            raise ValueError("Chỉ câu hỏi đang chờ duyệt mới có thể rút lại")
        if expected_version is not None and int(expected_version) != int(question.get("current_version") or 0):
            raise RuntimeError("VERSION_CONFLICT")
        now = utc_now()
        fields = {
            "review_status": "DRAFT",
            "review_assignment": {
                "status": "UNASSIGNED", "reviewer_user_id": None, "assigned_by_user_id": None,
                "assigned_at": None, "claimed_at": None, "lock_expires_at": None,
                "last_released_at": now, "release_reason": "withdrawn",
            },
            "review_submission": {},
            "updated_at": now,
        }
        updated = self.repository.withdraw_submission(
            question_id, expected_version_id=version["_id"], fields=fields,
            audit={
                "action": "question.submission_withdrawn",
                "entity_type": "question",
                "entity_id": question["_id"],
                "actor_user_id": current_user.id,
                "actor_role": current_user.role,
                "before": {"review_status": "PENDING"},
                "after": {"review_status": "DRAFT"},
                "metadata": {"version_id": str(version["_id"])},
            },
        )
        return serialize_question(*updated) if updated else None


def _transactional(notifications: list[dict] | None) -> dict:
    # Only the PostgreSQL repository writes notifications with the change.
    return {"notifications": notifications} if notifications is not None else {}


def get_question_service() -> QuestionService:
    database = get_database()
    if settings.question_store == "postgres":
        from modules.questions.postgres_repository import PostgresQuestionRepository
        return QuestionService(
            PostgresQuestionRepository(), MongoQuestionReferenceRepository(database),
        )
    return QuestionService(
        MongoQuestionRepository(database),
        MongoQuestionReferenceRepository(database),
    )
