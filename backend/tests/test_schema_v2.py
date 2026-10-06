import hashlib
import inspect
import json
import re
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from bson import ObjectId
from docx import Document
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from main import app
from core.bootstrap import SCHEMA_VERSION, VALIDATORS
from core.config import settings
from core.dependencies import (
    ALL_PERMISSIONS,
    CurrentUser,
    effective_permissions,
    permission_overrides,
    require_admin,
    require_exam_manager,
    require_permissions,
    require_reviewer_or_admin,
)
from core import job_recovery
from modules.admin import jobs_service as admin_jobs_module
from modules.admin.jobs_service import (
    AdminJobService,
    _generation_status_filter,
    _parse_object_id,
    _uppercase_status_filter,
)
from modules.admin.audit_service import AdminAuditService
from modules.admin.moodle_service import MoodleTargetService, _safe_publication_item
from modules.admin.moodle_schemas import MoodleTargetPayload
from modules.admin.overview_service import AdminOverviewService
from modules.catalog.schemas import (
    AiModelActivationPayload,
    AiModelPayload,
    ChapterPayload,
    ChapterUpdatePayload,
    EvaluationPolicyActivationPayload,
    EvaluationPolicyPayload,
    LearningOutcomePayload,
    LearningOutcomeUpdatePayload,
    PromptTemplateActivationPayload,
    PromptTemplatePayload,
    PromptTemplateTestPayload,
    SubjectPayload,
    SubjectUpdatePayload,
)
from modules.catalog.service import CatalogService
from modules.notifications.service import NotificationService
from modules.auth import login as auth_login
from modules.documents.repository import MongoDocumentRepository
from modules.documents.schemas import DocumentPageUpdateRequest, DocumentSharingRequest, DocumentStatus
from modules.documents.service import DocumentService
from modules.exams.service import ExamService, ExamVariantService
from modules.exams.pdf_service import render_exam_docx
from modules.ocr.ocr import extract_docx_pages
from modules.exams.schemas import (
    AddQuestionsManualRequest,
    ExamMatrixRequest,
    ExamOwnerTransferRequest,
    ExamSharingRequest,
    ExamStatusUpdateRequest,
    ExamUpdateRequest,
    ExamVariantCreateRequest,
)
from modules.generation.schemas import (
    BloomLevel,
    GeneratedQuestion,
    GenerationPlanSummary,
    QuestionGenerateRequest,
    QuestionType,
)
from modules.generation.llm.ollama import OllamaProvider
from modules.generation.llm.concurrency import ConcurrencyLimitedProvider
from modules.generation.llm.factory import get_llm_service
from modules.generation.prompt_builder import PromptBuilder
from modules.generation.question import _build_retry_prompt, _check_type_format, _normalize_difficulty
from modules.questions.schemas import (
    QuestionCreateRequest,
    QuestionResponse,
    QuestionOwnerTransferRequest,
    QuestionSharingRequest,
    QuestionSourceViewerResponse,
    QuestionUpdateRequest,
)
from modules.questions import repository as question_repository_module
from modules.questions import router as question_router_module
from modules.questions.repository import MongoQuestionRepository, normalize_sharing_fields, serialize_question
from modules.questions.service import QuestionService, stable_hash
from modules.questions.workflow_schemas import (
    AutoAssignRequest,
    AutoEvaluationRequest,
    EvaluationCreateRequest,
    EvaluationScores,
    MoodlePublicationRequest,
    QuestionCommentCreateRequest,
    ReviewAssignmentRequest,
    ReviewCreateRequest,
    ReviewDraftUpsertRequest,
    ReviewOverride,
    ReviewPolicyPayload,
    SecondaryReviewRequest,
)
from modules.questions import workflow_service as question_workflow_module
from modules.questions.workflow_service import QuestionWorkflowService, _prepare_evaluation_attempt
from modules.rag.search import _heading_matches_target, _normalize_heading_text
from modules.users.schemas import (
    GenerationPresetPayload,
    PublicRegisterRequest,
    RoleEnum,
    UserAdminUpdateRequest,
    UserImportRequest,
    UserInviteRequest,
)
from modules.users.service import UserService


def _current_user(role="Teacher", user_id=None, permissions=None):
    oid = user_id or ObjectId()
    return CurrentUser(
        id=oid,
        firebase_uid=f"firebase-{oid}",
        email=f"{role.lower()}@example.com",
        role=role,
        is_active=True,
        permissions=tuple(effective_permissions({"role": role}) if permissions is None else permissions),
        display_name=f"{role} User",
    )


def _exam_doc(owner_id):
    now = datetime.now(timezone.utc)
    return {
        "_id": ObjectId(),
        "name": "Midterm",
        "exam_title": "Data Structures Midterm",
        "subject_id": ObjectId(),
        "question_count": 1,
        "header": {
            "school_name": "CTU",
            "exam_name": "Midterm",
            "subject_name": "Data Structures",
        },
        "matrix": [],
        "questions": [],
        "status": "draft",
        "created_by_user_id": owner_id,
        "created_at": now,
        "updated_at": now,
    }


class FakeExamRepository:
    def __init__(self, exams=None, variants=None):
        self.exams = {str(exam["_id"]): exam for exam in (exams or [])}
        self.variants = variants or []
        self.audit_events = []

    def create(self, exam):
        self.exams[str(exam["_id"])] = exam
        return exam

    def find(self, exam_id):
        exam = self.exams.get(str(exam_id))
        return exam if exam and not exam.get("deleted_at") else None

    def list(self, page, page_size, created_by_user_id, *, visible_to_user_id=None):
        items = [exam for exam in self.exams.values() if not exam.get("deleted_at")]
        if created_by_user_id is not None:
            items = [
                exam for exam in items
                if str(exam.get("created_by_user_id")) == str(created_by_user_id)
            ]
        if visible_to_user_id is not None:
            items = [
                exam for exam in items
                if str(exam.get("created_by_user_id")) == str(visible_to_user_id)
                or str(visible_to_user_id) in {str(item) for item in exam.get("shared_with_user_ids") or []}
            ]
        return items, len(items)

    def update(self, exam_id, updates, *, audit=None, expected_status=None, require_no_variants=False):
        exam = self.find(exam_id)
        if not exam:
            return None
        if expected_status and str(exam.get("status")).upper() != expected_status:
            return None
        if require_no_variants and self.count_variants(exam_id):
            raise ValueError("Đề thi đã có mã đề")
        exam.update(updates)
        exam["updated_at"] = datetime.now(timezone.utc)
        if audit:
            self.audit_events.append(audit)
        return exam

    def delete(self, exam_id, deleted_by_user_id=None, *, audit=None):
        exam = self.find(exam_id)
        if not exam:
            return False
        exam.update(deleted_at=datetime.now(timezone.utc), deleted_by_user_id=deleted_by_user_id)
        if audit:
            self.audit_events.append(audit)
        return True

    def count_variants(self, exam_id):
        return len([v for v in self.variants if str(v["exam_id"]) == str(exam_id)])


class FakeExamVariantRepository:
    def __init__(self, variants=None):
        self.variants = {str(variant["_id"]): variant for variant in (variants or [])}

    def create(self, variant):
        self.variants[str(variant["_id"])] = variant
        return variant

    def find(self, variant_id):
        return self.variants.get(str(variant_id))

    def list_by_exam(self, exam_id):
        return [
            variant for variant in self.variants.values()
            if str(variant["exam_id"]) == str(exam_id)
        ]

    def delete(self, variant_id, *, audit=None):
        self.deleted_audit = audit
        return self.variants.pop(str(variant_id), None) is not None


class FakeExamQuestionRepository:
    def __init__(self, pairs=None):
        self.pairs = {str(question["_id"]): (question, version) for question, version in (pairs or [])}

    def find_pair(self, question_id):
        return self.pairs.get(str(question_id))

    def find_approved_pair(self, question_id):
        pair = self.pairs.get(str(question_id))
        if not pair or str(pair[0].get("approved_version_id")) != str(pair[1]["_id"]):
            return None
        if pair[0].get("lifecycle_status") == "ARCHIVED":
            return None
        return pair

    def find_archived_pair(self, question_id):
        pair = self.pairs.get(str(question_id))
        return pair if pair and pair[0].get("lifecycle_status") == "ARCHIVED" else None

    def list(self, page, page_size, review_status, search, **filters):
        pairs = list(self.pairs.values())
        if filters.get("approved_version_join"):
            pairs = [pair for pair in pairs if str(pair[0].get("approved_version_id")) == str(pair[1]["_id"])]
        if review_status:
            pairs = [
                pair for pair in pairs
                if pair[0].get("review_status") == review_status
            ]
        subject_id = filters.get("subject_id")
        if subject_id:
            pairs = [
                pair for pair in pairs
                if str((pair[1].get("classification") or {}).get("subject", {}).get("id")) == str(subject_id)
            ]
        if filters.get("approved_current_only"):
            pairs = [
                pair for pair in pairs
                if str(pair[0].get("approved_version_id")) == str(pair[0].get("current_version_id"))
            ]
        total = len(pairs)
        start = (page - 1) * page_size
        return pairs[start:start + page_size], total


def _pending_question_pair(code, subject_id, submitted_at, author, now, **extra):
    """A pending question aggregate and its version, ready for review workflow tests."""
    question_id, version_id = ObjectId(), ObjectId()
    return (
        {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": code,
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "PENDING",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {},
            "latest_review_id": None,
            "subject_id": subject_id,
            "created_by_user_id": author,
            "review_assignment": {"status": "UNASSIGNED", "lock_expires_at": None},
            "review_submission": {"submitted_at": submitted_at},
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
            **extra,
        },
        {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": None,
            "created_by_user_id": author,
            "generated_by_model_id": None,
            "classification": {
                "subject": {"id": subject_id},
                "chapter": {"id": None},
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 1},
                "difficulty": None,
            },
            "clos": [],
            "content": code,
            "question_data": {"options": {"A": "x"}, "correct_answer": "A"},
            "sources": [],
            "keywords": [],
            "content_hash": f"hash-{code}",
            "change_note": "",
            "created_at": now,
        },
    )


def _user_doc(role="Admin", is_active=True):
    now = datetime.now(timezone.utc)
    oid = ObjectId()
    return {
        "_id": oid,
        "firebase_uid": f"firebase-{oid}",
        "email": f"{role.lower()}-{oid}@example.com",
        "display_name": f"{role} User",
        "role": role,
        "profile": {"school": "", "address": "", "avatar": ""},
        "is_active": is_active,
        "created_at": now,
        "updated_at": now,
    }


class FakeUserRepository:
    def __init__(self, users):
        self.users = {str(user["_id"]): dict(user) for user in users}
        self.audit_events = []

    def find_by_id(self, user_id):
        return self.users.get(str(user_id))

    def find_by_firebase_uid(self, firebase_uid):
        return next(
            (user for user in self.users.values() if user.get("firebase_uid") == firebase_uid),
            None,
        )

    def find_by_email(self, email):
        return next(
            (user for user in self.users.values() if str(user.get("email", "")).lower() == email.lower()),
            None,
        )

    def create(self, data):
        now = datetime.now(timezone.utc)
        user = {
            "_id": ObjectId(),
            "firebase_uid": data["firebase_uid"],
            "email": data["email"].lower(),
            "display_name": data["display_name"],
            "role": data.get("role", "Teacher"),
            "permissions": data.get("permissions") or [],
            "profile": data.get("profile") or {"school": "", "address": "", "avatar": ""},
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        self.users[str(user["_id"])] = user
        return user

    def list(self, page, page_size, role, search):
        records = list(self.users.values())
        if role:
            records = [user for user in records if user.get("role") == role]
        if search:
            search_text = search.lower()
            records = [
                user for user in records
                if search_text in user.get("display_name", "").lower()
                or search_text in user.get("email", "").lower()
            ]
        total = len(records)
        start = (page - 1) * page_size
        return records[start:start + page_size], total

    def update(self, user_id, fields, *, audit=None):
        user = self.find_by_id(user_id)
        if not user:
            return None
        if audit and getattr(self, "fail_audit", False):
            raise RuntimeError("audit store unavailable")
        user.update(fields)
        user["updated_at"] = datetime.now(timezone.utc)
        event = audit(user) if audit else None
        if event:
            self.audit_events.append(event)
        return user

    def count_active_admins(self):
        return len([
            user for user in self.users.values()
            if user.get("role") == "Admin" and user.get("is_active", True)
        ])

    def delete_by_id(self, user_id):
        self.users.pop(str(user_id), None)


class FakeIdentityGateway:
    def __init__(self):
        self.disabled_calls = []
        self.update_calls = []
        self.created = []
        self.reset_links = []

    def create_user(self, *, email, password, display_name):
        uid = f"firebase-{len(self.created) + 1}"
        self.created.append({"uid": uid, "email": email, "password": password, "display_name": display_name})
        return type("FirebaseUser", (), {"uid": uid})()

    def update_user(self, firebase_uid, **fields):
        self.update_calls.append((firebase_uid, fields))

    def generate_password_reset_link(self, email):
        self.reset_links.append(email)
        return f"https://reset.example/{email}"

    def set_user_disabled(self, firebase_uid, disabled):
        self.disabled_calls.append((firebase_uid, disabled))

    def delete_user(self, firebase_uid):
        raise NotImplementedError


class FakeSessions:
    def __init__(self):
        self.upserts = []

    def upsert(self, firebase_uid, id_token):
        self.upserts.append((firebase_uid, id_token))


def _path_values(record, path):
    parts = path.split(".")

    def walk(value, remaining):
        if not remaining:
            return [value]
        if isinstance(value, list):
            results = []
            for item in value:
                results.extend(walk(item, remaining))
            return results
        if not isinstance(value, dict):
            return []
        key = remaining[0]
        if key not in value:
            return []
        return walk(value[key], remaining[1:])

    return walk(record, parts)


def _matches_query(record, query):
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches_query(record, item) for item in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches_query(record, item) for item in expected):
                return False
            continue
        if key == "$nor":
            if any(_matches_query(record, item) for item in expected):
                return False
            continue
        if key == "$expr":
            (operator, (left, right)), = expected.items()

            def operand(value):
                if isinstance(value, str) and value.startswith("$"):
                    return (_path_values(record, value[1:]) or [None])[0]
                return value

            equal = operand(left) == operand(right)
            if (operator == "$eq" and not equal) or (operator == "$ne" and equal):
                return False
            continue
        values = _path_values(record, key)
        if isinstance(expected, dict):
            for operator, operand in expected.items():
                if operator == "$in":
                    if not any(value in operand for value in values):
                        return False
                elif operator == "$nin":
                    if any(value in operand for value in values):
                        return False
                elif operator == "$ne":
                    if any(value == operand for value in values):
                        return False
                elif operator == "$lt":
                    if not any(value is not None and value < operand for value in values):
                        return False
                elif operator == "$lte":
                    if not any(value is not None and value <= operand for value in values):
                        return False
                elif operator == "$gt":
                    if not any(value is not None and value > operand for value in values):
                        return False
                elif operator == "$gte":
                    if not any(value is not None and value >= operand for value in values):
                        return False
                elif operator == "$exists":
                    if bool(values) is not bool(operand):
                        return False
                elif operator == "$regex":
                    flags = re.IGNORECASE if expected.get("$options") == "i" else 0
                    if not any(re.search(operand, str(value), flags) for value in values if value is not None):
                        return False
                elif operator == "$options":
                    continue
                else:
                    return False
        elif expected is None:
            if values and not any(value is None for value in values):
                return False
        elif not any(value == expected for value in values):
            return False
    return True


class InMemoryCursor(list):
    def sort(self, key_or_list, direction=1):
        if isinstance(key_or_list, list):
            sort_keys = key_or_list
        else:
            sort_keys = [(key_or_list, direction)]
        for key, sort_direction in reversed(sort_keys):
            self[:] = sorted(
                self,
                key=lambda item: (_path_values(item, key) or [None])[0],
                reverse=sort_direction == -1,
            )
        return self

    def skip(self, count):
        return InMemoryCursor(self[count:])

    def limit(self, count):
        return InMemoryCursor(self[:count])


class InMemoryCollection:
    def __init__(self, records=None):
        self.records = [dict(record) for record in (records or [])]

    def count_documents(self, query):
        return len([record for record in self.records if _matches_query(record, query)])

    def find(self, query=None, *_args, **_kwargs):
        return InMemoryCursor([record for record in self.records if _matches_query(record, query)])

    def find_one(self, query=None, *_args, **_kwargs):
        return next((record for record in self.records if _matches_query(record, query)), None)

    @staticmethod
    def _set_path(record, path, value, filter_query):
        if ".$." not in path:
            current = record
            parts = path.split(".")
            for part in parts[:-1]:
                current = current.setdefault(part, {})
            current[parts[-1]] = value
            return
        collection_key, field_key = path.split(".$.", 1)
        target_id = filter_query.get(f"{collection_key}._id")
        for item in record.get(collection_key, []):
            if item.get("_id") == target_id:
                item[field_key] = value
                return

    @staticmethod
    def _unset_path(record, path):
        current = record
        parts = path.split(".")
        for part in parts[:-1]:
            current = current.get(part, {})
        current.pop(parts[-1], None)

    def insert_one(self, record, *_args, **_kwargs):
        item = dict(record)
        self.records.append(item)
        return type("Result", (), {"inserted_id": item.get("_id")})()

    def find_one_and_update(
        self,
        filter_query,
        update,
        *,
        upsert=False,
        return_document=None,
        **_kwargs,
    ):
        record = self.find_one(filter_query)
        if record is None and upsert:
            record = dict((update.get("$setOnInsert") or {}))
            record.update(update.get("$set") or {})
            self.records.append(record)
        if record is None:
            return None
        for path, value in (update.get("$set") or {}).items():
            self._set_path(record, path, value, filter_query)
        for path, value in (update.get("$inc") or {}).items():
            current = _path_values(record, path)
            self._set_path(record, path, (current[0] if current else 0) + value, filter_query)
        for path, value in (update.get("$push") or {}).items():
            record.setdefault(path, []).append(value)
        return record

    def update_one(self, filter_query, update, *, upsert=False, **_kwargs):
        matched = 0
        modified = 0
        upserted_id = None
        record = self.find_one(filter_query)
        if record is None and upsert:
            record = dict(update.get("$setOnInsert") or {})
            self.records.append(record)
            upserted_id = record.get("_id")
            matched = 1
            modified = 1
        elif record is not None:
            matched = 1

        if record is not None:
            changed = False
            for path, value in (update.get("$set") or {}).items():
                self._set_path(record, path, value, filter_query)
                changed = True
            for path in (update.get("$unset") or {}):
                self._unset_path(record, path)
                changed = True
            for path, value in (update.get("$push") or {}).items():
                record.setdefault(path, []).append(value)
                changed = True
            if changed:
                modified = 1

        return type(
            "Result",
            (),
            {"matched_count": matched, "modified_count": modified, "upserted_id": upserted_id},
        )()

    def update_many(self, filter_query, update, **_kwargs):
        matched = 0
        modified = 0
        for record in self.records:
            if not _matches_query(record, filter_query):
                continue
            matched += 1
            changed = False
            for path, value in (update.get("$set") or {}).items():
                self._set_path(record, path, value, filter_query)
                changed = True
            for path in (update.get("$unset") or {}):
                self._unset_path(record, path)
                changed = True
            for path, value in (update.get("$push") or {}).items():
                record.setdefault(path, []).append(value)
                changed = True
            if changed:
                modified += 1

        return type(
            "Result",
            (),
            {"matched_count": matched, "modified_count": modified},
        )()


class FakeCatalogDatabase:
    def __init__(
        self,
        *,
        subjects=None,
        documents=None,
        question_versions=None,
        questions=None,
        exams=None,
        ai_models=None,
        prompt_templates=None,
        evaluation_policies=None,
    ):
        self.subjects = InMemoryCollection(subjects)
        self.documents = InMemoryCollection(documents)
        self.question_versions = InMemoryCollection(question_versions)
        self.questions = InMemoryCollection(questions)
        self.exams = InMemoryCollection(exams)
        self.ai_models = InMemoryCollection(ai_models)
        self.prompt_templates = InMemoryCollection(prompt_templates)
        self.evaluation_policies = InMemoryCollection(evaluation_policies)
        self.audit_logs = InMemoryCollection()


class SchemaV2Tests(unittest.TestCase):
    def test_only_admin_teacher_and_reviewer_roles_exist(self):
        self.assertEqual({role.value for role in RoleEnum}, {"Admin", "Teacher", "Reviewer"})

    def test_admin_permissions_belong_only_to_admin_role(self):
        # Dữ liệu cũ từng cấp quyền quản trị cho giảng viên: không còn hiệu lực.
        legacy_teacher = effective_permissions({"role": "Teacher", "permissions": ["admin.users"]})
        self.assertNotIn("admin.users", legacy_teacher)
        teacher = _current_user("Teacher", permissions=legacy_teacher)
        with self.assertRaises(HTTPException) as ctx:
            require_permissions("admin.users")(teacher)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("Quản lý người dùng", ctx.exception.detail)

        with self.assertRaisesRegex(ValueError, "Quản trị viên"):
            permission_overrides("Reviewer", ["reviews.manage", "admin.overview"])

        self.assertEqual(set(effective_permissions({"role": "Admin"})), set(ALL_PERMISSIONS))
        self.assertEqual(
            set(effective_permissions({"role": "Admin", "permission_revokes": ["admin.users"]})),
            set(ALL_PERMISSIONS),
        )

    def test_permission_overrides_grant_and_revoke_relative_to_role(self):
        overrides = permission_overrides("Reviewer", ["questions.export_moodle", "exams.manage_own"])
        self.assertEqual(overrides, {"permission_grants": ["exams.manage_own"], "permission_revokes": ["reviews.manage"]})
        permissions = set(effective_permissions({"role": "Reviewer", **overrides}))
        self.assertEqual(permissions, {"questions.export_moodle", "exams.manage_own"})

        reviewer = _current_user("Reviewer", permissions=tuple(permissions))
        with self.assertRaises(HTTPException):
            require_reviewer_or_admin(reviewer)
        self.assertIs(require_exam_manager(reviewer), reviewer)

    def test_role_guards_do_not_infer_roles_from_permissions(self):
        teacher_with_review = _current_user("Teacher", permissions=("reviews.manage",))
        self.assertIs(require_reviewer_or_admin(teacher_with_review), teacher_with_review)
        with self.assertRaises(HTTPException):
            require_admin(teacher_with_review)

    def test_public_register_cannot_choose_role(self):
        with self.assertRaises(ValidationError):
            PublicRegisterRequest(
                email="teacher@example.com",
                password="secret1",
                full_name="Teacher",
                role="Admin",
            )

    def test_question_update_requires_optimistic_version(self):
        with self.assertRaises(ValidationError):
            QuestionUpdateRequest(content="Updated")

    def test_question_can_reference_multiple_chunks(self):
        payload = QuestionCreateRequest(
            content="Question",
            source_chunk_ids=["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"],
        )
        self.assertEqual(len(payload.source_chunk_ids), 2)

    def test_question_restore_creates_new_version_and_resets_workflow(self):
        owner = _current_user("Teacher")
        question_id = ObjectId()
        version1_id = ObjectId()
        version2_id = ObjectId()
        review_id = ObjectId()
        reviewer_id = ObjectId()
        now = datetime.now(timezone.utc)
        classification = {
            "subject": {"id": None},
            "chapter": {"id": None},
            "assessment_type": "TRAC_NGHIEM",
            "bloom": {"level": 2, "code": "UNDERSTAND", "name": "Understand"},
            "difficulty": None,
        }
        version1_data = {
            "content": "Original stack question",
            "question_data": {
                "options": {"A": "LIFO", "B": "FIFO"},
                "correct_answer": "A",
                "explanation": "Stack pops the newest item first.",
            },
        }
        version2_data = {
            "content": "Edited queue question",
            "question_data": {
                "options": {"A": "LIFO", "B": "FIFO"},
                "correct_answer": "B",
                "explanation": "Queue removes the oldest item first.",
            },
        }
        versions = [
            {
                "_id": version1_id,
                "schema_version": SCHEMA_VERSION,
                "question_id": question_id,
                "version": 1,
                "origin": "MANUAL",
                "generation_run_id": None,
                "document_id": None,
                "created_by_user_id": owner.id,
                "generated_by_model_id": None,
                "classification": classification,
                "clos": [],
                "sources": [],
                "keywords": [],
                "content_hash": stable_hash(
                    {**version1_data, "classification": classification, "clos": [], "sources": []}
                ),
                "change_note": "Initial version",
                "created_at": now - timedelta(minutes=2),
                **version1_data,
            },
            {
                "_id": version2_id,
                "schema_version": SCHEMA_VERSION,
                "question_id": question_id,
                "version": 2,
                "origin": "MANUAL",
                "generation_run_id": None,
                "document_id": None,
                "created_by_user_id": owner.id,
                "generated_by_model_id": None,
                "classification": classification,
                "clos": [],
                "sources": [],
                "keywords": [],
                "content_hash": stable_hash(
                    {**version2_data, "classification": classification, "clos": [], "sources": []}
                ),
                "change_note": "Teacher edit",
                "created_at": now - timedelta(minutes=1),
                **version2_data,
            },
        ]
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-RESTORE",
            "current_version": 2,
            "current_version_id": version2_id,
            "approved_version_id": version2_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "APPROVED",
            "publication_status": "PUBLISHED",
            "quality_summary": {"overall_score": 0.92, "color": "GREEN"},
            "review_assignment": {
                "status": "CLAIMED",
                "reviewer_user_id": reviewer_id,
                "assigned_by_user_id": ObjectId(),
                "assigned_at": now - timedelta(minutes=5),
                "claimed_at": now - timedelta(minutes=4),
                "lock_expires_at": now + timedelta(minutes=30),
                "last_released_at": None,
                "release_reason": None,
            },
            "latest_review_id": review_id,
            "created_by_user_id": owner.id,
            "created_at": now - timedelta(minutes=3),
            "updated_at": now - timedelta(minutes=1),
            "archived_at": None,
        }
        db = FakeCatalogDatabase(questions=[question], question_versions=versions)
        original_transaction = question_repository_module.mongo_transaction
        try:
            question_repository_module.mongo_transaction = lambda: nullcontext(None)
            service = QuestionService(MongoQuestionRepository(db), references=None)

            history = service.versions(str(question_id), owner)
            restored = service.update(
                str(question_id),
                QuestionUpdateRequest(
                    expected_version=2,
                    content=version1_data["content"],
                    question_type=classification["assessment_type"],
                    bloom_level=classification["bloom"]["level"],
                    question_data=version1_data["question_data"],
                    source_chunk_ids=[],
                    clo_ids=[],
                    change_note="Restore from version 1",
                ),
                owner.id,
                actor_role=owner.role,
            )
            restored_history = service.versions(str(question_id), owner)
        finally:
            question_repository_module.mongo_transaction = original_transaction

        self.assertEqual([item["version"] for item in history], [2, 1])
        self.assertEqual(restored["current_version"], 3)
        self.assertEqual(restored["content"], version1_data["content"])
        self.assertEqual(restored["question_data"], version1_data["question_data"])
        self.assertEqual(restored["evaluation_status"], "NOT_STARTED")
        self.assertEqual(restored["review_status"], "DRAFT")
        self.assertEqual(restored["publication_status"], "STALE")
        self.assertEqual(restored["quality_summary"], {})
        self.assertEqual(restored["review_assignment"]["status"], "UNASSIGNED")
        self.assertEqual([item["version"] for item in restored_history], [3, 2, 1])
        self.assertEqual(restored_history[0]["change_note"], "Restore from version 1")
        self.assertEqual(restored_history[0]["content"], version1_data["content"])

    def test_review_override_requires_reason(self):
        with self.assertRaises(ValidationError):
            ReviewOverride(applied=True, score=0.9)

    def test_structured_review_requires_reject_reason_and_revision_issue(self):
        with self.assertRaises(ValidationError):
            ReviewCreateRequest(expected_version=1, decision="REJECTED")

        with self.assertRaises(ValidationError):
            ReviewCreateRequest(
                expected_version=1,
                decision="NEEDS_REVISION",
                review_form={"overall_note": "Cần sửa đáp án."},
            )

        form = {
            "overall_note": "Cần sửa trước khi duyệt.",
            "revision_issues": [
                {
                    "title": "Đáp án chưa khớp nguồn",
                    "severity": "HIGH",
                    "detail": "Nguồn nói FIFO nhưng đáp án chọn Stack.",
                    "page_number": 2,
                }
            ],
        }
        with self.assertRaisesRegex(ValidationError, "danh mục chuẩn"):
            ReviewCreateRequest(expected_version=1, decision="NEEDS_REVISION", review_form=form)
        with self.assertRaisesRegex(ValidationError, "không hợp lệ"):
            ReviewCreateRequest(expected_version=1, decision="NEEDS_REVISION", review_form=form,
                                reason_codes=["MADE_UP"])
        with self.assertRaisesRegex(ValidationError, "Lý do khác"):
            ReviewCreateRequest(expected_version=1, decision="NEEDS_REVISION", reason_codes=["OTHER"],
                                review_form={**form, "overall_note": ""})
        payload = ReviewCreateRequest(
            expected_version=1,
            decision="NEEDS_REVISION",
            review_form=form,
            reason_codes=["wrong_answer", "NOT_GROUNDED", "WRONG_ANSWER"],
        )
        self.assertEqual(payload.review_form.revision_issues[0].severity, "HIGH")
        self.assertEqual(payload.reason_codes, ["WRONG_ANSWER", "NOT_GROUNDED"])
        self.assertEqual(ReviewCreateRequest(expected_version=1, decision="APPROVED").reason_codes, [])

    def test_review_assignment_request_allows_admin_unassign(self):
        payload = ReviewAssignmentRequest(reviewer_user_id=None, note="unassign")
        self.assertIsNone(payload.reviewer_user_id)
        self.assertEqual(payload.note, "unassign")

    def test_question_response_exposes_review_assignment(self):
        now = datetime.now(timezone.utc)
        response = QuestionResponse(
            id=str(ObjectId()),
            question_code="Q-1",
            current_version=1,
            current_version_id=str(ObjectId()),
            approved_version_id=None,
            document_id=None,
            lifecycle_status="ACTIVE",
            evaluation_status="PASSED",
            review_status="PENDING",
            publication_status="NOT_PUBLISHED",
            content="Question",
            question_data={},
            classification={},
            clos=[],
            sources=[],
            content_hash="hash",
            quality_summary={},
            review_assignment={
                "status": "IN_REVIEW",
                "reviewer_user_id": str(ObjectId()),
            },
            latest_review_id=None,
            created_at=now,
            updated_at=now,
        )
        self.assertEqual(response.review_assignment["status"], "IN_REVIEW")

    def test_question_source_viewer_schema_accepts_chunk_pages(self):
        response = QuestionSourceViewerResponse(
            question_id=str(ObjectId()),
            question_code="Q-1",
            version_id=str(ObjectId()),
            version=1,
            document={
                "id": str(ObjectId()),
                "title": "Document",
                "original_filename": "source.pdf",
                "page_count": 4,
                "current_ocr_job_id": str(ObjectId()),
                "current_chunk_set_id": str(ObjectId()),
                "pdf_available": True,
                "pdf_url": "/questions/q/source-pdf",
            },
            items=[
                {
                    "citation_order": 1,
                    "source_type": "CHUNK",
                    "is_primary": True,
                    "chunk_id": str(ObjectId()),
                    "page_range": {"start": 2, "end": 3, "pages": [2, 3]},
                    "context_excerpt": "Queue follows FIFO.",
                    "pages": [{"page_number": 2, "text": "Queue follows FIFO."}],
                }
            ],
        )
        self.assertEqual(response.items[0].pages[0].page_number, 2)

    def test_question_source_viewer_reports_stale_chunk_set_and_pages(self):
        question_id = ObjectId()
        version_id = ObjectId()
        document_id = ObjectId()
        old_chunk_set_id = ObjectId()
        current_chunk_set_id = ObjectId()
        ocr_job_id = ObjectId()
        chunk_id = ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id,
            "question_code": "Q-1",
            "current_version_id": version_id,
            "current_version": 1,
            "created_by_user_id": ObjectId(),
            "review_status": "PENDING",
            "subject_id": ObjectId(),
        }
        version = {
            "_id": version_id,
            "version": 1,
            "document_id": document_id,
            "created_by_user_id": question["created_by_user_id"],
            "sources": [
                {
                    "source_type": "CHUNK",
                    "chunk_id": chunk_id,
                    "chunk_set_id": old_chunk_set_id,
                    "chunk_content_hash": "hash-a",
                    "citation_order": 1,
                    "is_primary": True,
                    "context_excerpt": "Queue follows FIFO.",
                }
            ],
        }
        document = {
            "_id": document_id,
            "title": "Data Structures",
            "original_filename": "ctdl.pdf",
            "page_count": 5,
            "current_processing": {
                "ocr_job_id": ocr_job_id,
                "chunk_set_id": current_chunk_set_id,
            },
            "artifacts": [
                {
                    "type": "ORIGINAL_PDF",
                    "is_current": True,
                    "storage": {"provider": "LOCAL", "uri": "data/uploads/source.pdf"},
                    "mime_type": "application/pdf",
                }
            ],
        }
        chunk = {
            "_id": chunk_id,
            "document_id": document_id,
            "chunk_set_id": old_chunk_set_id,
            "chunk_no": 7,
            "content": "Queue follows FIFO.",
            "content_hash": "hash-a",
            "page_range": {"start": 2, "end": 3, "pages": [2, 3]},
            "heading": {"title": "Queue"},
        }

        class PairRepository:
            def find_pair(self, _question_id):
                return question, version

        class References:
            def find_chunk(self, requested_chunk_id):
                return chunk if requested_chunk_id == chunk_id else None

            def find_document(self, requested_document_id):
                return document if requested_document_id == document_id else None

            def find_pages(self, requested_document_id, requested_ocr_job_id, page_numbers):
                self.page_query = (requested_document_id, requested_ocr_job_id, page_numbers)
                return [
                    {
                        "page_number": 2,
                        "cleaned_text": "Page 2 Queue follows FIFO.",
                        "formula_blocks": [],
                    }
                ]

            def find_subject(self, _subject_id):
                return None

            def reviewer_subject_ids(self, _user_id):
                return [question["subject_id"]]

        references = References()
        service = QuestionService(repository=PairRepository(), references=references)
        result = service.source_viewer(str(question_id), _current_user("Reviewer"))

        self.assertEqual(result["document"]["title"], "Data Structures")
        self.assertTrue(result["document"]["pdf_available"])
        self.assertEqual(result["items"][0]["chunk_no"], 7)
        self.assertFalse(result["items"][0]["is_current_chunk_set"])
        self.assertIn("Nguồn không còn thuộc chunk set hiện hành", result["items"][0]["warnings"])
        self.assertEqual(result["items"][0]["pages"][0]["page_number"], 2)
        self.assertEqual(references.page_query, (document_id, ocr_job_id, [2, 3]))

    def test_manual_excerpt_source_viewer_resolves_ocr_page(self):
        question_id = ObjectId()
        document_id = ObjectId()
        ocr_job_id = ObjectId()
        question = {"_id": question_id, "question_code": "Q-EXCERPT"}
        version = {
            "_id": ObjectId(),
            "version": 1,
            "document_id": document_id,
            "sources": [{"source_type": "MANUAL_EXCERPT", "context_excerpt": "Bình B3 là trung gian."}],
        }
        document = {
            "_id": document_id,
            "title": "Giáo trình",
            "original_filename": "giaotrinh.pdf",
            "current_processing": {"ocr_job_id": ocr_job_id},
            "artifacts": [],
        }

        class PairRepository:
            def find_pair(self, _question_id):
                return question, version

        class References:
            def find_document(self, _document_id):
                return document

            def find_excerpt_pages(self, _document_id, _ocr_job_id, _text):
                return [5]

            def find_pages(self, _document_id, _ocr_job_id, page_numbers):
                assert page_numbers == [5]
                return [{"page_number": 5, "cleaned_text": "Bình B3 là trung gian."}]

        result = QuestionService(PairRepository(), References()).source_viewer(str(question_id))
        self.assertEqual(result["items"][0]["page_range"]["pages"], [5])
        self.assertEqual(result["items"][0]["pages"][0]["page_number"], 5)

    def test_submit_for_review_moves_draft_or_revision_to_pending(self):
        teacher = _current_user("Teacher")

        class SubmitRepository:
            def __init__(self, review_status):
                question_id = ObjectId()
                version_id = ObjectId()
                now = datetime.now(timezone.utc)
                self.update_calls = 0
                self.question = {
                    "_id": question_id,
                    "schema_version": SCHEMA_VERSION,
                    "question_code": "Q-SUBMIT",
                    "current_version": 1,
                    "current_version_id": version_id,
                    "approved_version_id": None,
                    "lifecycle_status": "ACTIVE",
                    "evaluation_status": "NOT_STARTED",
                    "review_status": review_status,
                    "publication_status": "NOT_PUBLISHED",
                    "quality_summary": {},
                    "review_assignment": {"status": "UNASSIGNED"},
                    "latest_review_id": None,
                    "created_by_user_id": teacher.id,
                    "created_at": now,
                    "updated_at": now,
                    "archived_at": None,
                }
                self.version = {
                    "_id": version_id,
                    "schema_version": SCHEMA_VERSION,
                    "question_id": question_id,
                    "version": 1,
                    "origin": "MANUAL",
                    "generation_run_id": None,
                    "document_id": None,
                    "created_by_user_id": teacher.id,
                    "generated_by_model_id": None,
                    "classification": {
                        "subject": {"id": None},
                        "chapter": {"id": None},
                        "assessment_type": "TRAC_NGHIEM",
                        "bloom": {"level": 2},
                        "difficulty": None,
                    },
                    "clos": [],
                    "content": "Queue follows FIFO.",
                    "question_data": {"options": {"A": "FIFO", "B": "LIFO"}, "correct_answer": "A"},
                    "sources": [],
                    "keywords": [],
                    "content_hash": "hash-submit",
                    "change_note": "Initial version",
                    "created_at": now,
                }

            def find_pair(self, _question_id):
                return self.question, self.version

            def update_review_status(
                self,
                _question_id,
                allowed_statuses,
                review_status,
                *,
                review_submission=None,
            ):
                self.update_calls += 1
                if self.question["review_status"] not in allowed_statuses:
                    return None
                self.question["review_status"] = review_status
                self.question["updated_at"] = datetime.now(timezone.utc)
                if review_submission:
                    self.question["review_submission"] = {
                        **review_submission,
                        "submitted_at": self.question["updated_at"],
                    }
                return self.question, self.version

        for initial_status in ("DRAFT", "NEEDS_REVISION"):
            repository = SubmitRepository(initial_status)
            service = QuestionService(repository=repository, references=object())
            note = ""
            if initial_status == "NEEDS_REVISION":
                # Unchanged resubmission needs a reply to the reviewer.
                for short in ("", "ok"):
                    with self.assertRaises(ValueError):
                        service.submit_for_review(str(repository.question["_id"]), teacher, response_note=short)
                note = "Đáp án A đúng theo giáo trình trang 12"

            submitted = service.submit_for_review(str(repository.question["_id"]), teacher, response_note=note)
            submitted_again = service.submit_for_review(str(repository.question["_id"]), teacher)
            if note:
                self.assertEqual(submitted["review_submission"]["response_note"], note)
                self.assertTrue(submitted["review_submission"]["resubmitted_unchanged"])

            self.assertEqual(submitted["review_status"], "PENDING")
            self.assertEqual(submitted["submitted_by_user_id"], str(teacher.id))
            self.assertIsNotNone(submitted["submitted_at"])
            self.assertEqual(
                submitted["review_submission"]["submitted_by"]["display_name"],
                "Teacher User",
            )
            self.assertEqual(submitted_again["review_status"], "PENDING")
            self.assertEqual(repository.update_calls, 1)

        repository = SubmitRepository("APPROVED")
        service = QuestionService(repository=repository, references=object())
        with self.assertRaises(ValueError):
            service.submit_for_review(str(repository.question["_id"]), teacher)
        self.assertEqual(repository.update_calls, 0)

    def test_submit_route_automatically_queues_ai_evaluation(self):
        teacher = _current_user("Teacher")
        question_id = str(ObjectId())

        class Service:
            def __init__(self):
                self.get_calls = 0

            def get(self, _question_id, _current_user):
                self.get_calls += 1
                return {
                    "id": question_id,
                    "review_status": "DRAFT" if self.get_calls == 1 else "PENDING",
                    "evaluation_status": "QUEUED" if self.get_calls > 1 else "NOT_STARTED",
                    "current_version": 2,
                }

            def submit_for_review(self, _question_id, _current_user, notifications=None, response_note=""):
                return {
                    "id": question_id,
                    "review_status": "PENDING",
                    "evaluation_status": "NOT_STARTED",
                    "current_version": 2,
                }

        class Workflow:
            def __init__(self):
                self.db = object()
                self.calls = []

            def enqueue_auto_evaluation(self, question_id_arg, **kwargs):
                self.calls.append((question_id_arg, kwargs))
                return {"status": "QUEUED"}

        service = Service()
        workflow = Workflow()
        original_notify = question_router_module.safe_notify_question_resubmitted
        try:
            question_router_module.safe_notify_question_resubmitted = lambda **_kwargs: None
            result = question_router_module.submit_question_for_review(
                question_id,
                teacher,
                service,
                workflow,
            )
        finally:
            question_router_module.safe_notify_question_resubmitted = original_notify

        self.assertEqual(result["evaluation_status"], "QUEUED")
        self.assertEqual(workflow.calls[0][0], question_id)
        self.assertEqual(workflow.calls[0][1]["trigger"], "REVIEW_SUBMISSION")
        self.assertEqual(workflow.calls[0][1]["expected_version"], 2)

    def test_new_evaluation_job_supersedes_active_jobs_for_older_versions(self):
        question_id = ObjectId()
        old_version_id = ObjectId()
        current_version_id = ObjectId()
        old_queued_id = ObjectId()
        old_processing_id = ObjectId()
        current_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class Database:
            def __init__(self):
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": old_queued_id,
                            "question_id": question_id,
                            "question_version_id": old_version_id,
                            "status": "QUEUED",
                        },
                        {
                            "_id": old_processing_id,
                            "question_id": question_id,
                            "question_version_id": old_version_id,
                            "status": "PROCESSING",
                            "locked_by": "worker-old",
                        },
                        {
                            "_id": current_job_id,
                            "question_id": question_id,
                            "question_version_id": current_version_id,
                            "status": "QUEUED",
                        },
                    ]
                )

        db = Database()
        modified = QuestionWorkflowService(db)._supersede_previous_evaluation_jobs(
            question_id,
            current_version_id,
            current_job_id,
            now,
        )

        self.assertEqual(modified, 2)
        for job_id in (old_queued_id, old_processing_id):
            job = db.evaluation_jobs.find_one({"_id": job_id})
            self.assertEqual(job["status"], "STALE")
            self.assertEqual(job["superseded_by_job_id"], current_job_id)
            self.assertEqual(job["error"]["stage"], "SUPERSEDED")
        self.assertEqual(
            db.evaluation_jobs.find_one({"_id": current_job_id})["status"],
            "QUEUED",
        )

    def test_qwen_evaluation_snapshot_limits_output_without_changing_deepseek(self):
        qwen = question_workflow_module._limit_evaluation_output(
            {
                "model_code": "qwen3-8b",
                "runtime": "OLLAMA",
                "parameters": {"num_predict": 32768, "num_ctx": 8192, "think": False},
            }
        )
        deepseek = question_workflow_module._limit_evaluation_output(
            {
                "model_code": "deepseek",
                "runtime": "OLLAMA",
                "parameters": {"num_predict": 900},
            }
        )

        self.assertEqual(qwen["parameters"]["num_predict"], settings.evaluation_num_predict)
        self.assertEqual(qwen["parameters"]["num_ctx"], 8192)
        self.assertEqual(deepseek["parameters"]["num_predict"], 900)

    def test_evaluation_retry_uses_compact_prompt_and_restores_output_budget(self):
        prompt = "Evaluate this question"
        prompt_snapshot = {
            "rendered_prompt_hash": "initial",
            "rendered_prompt_chars": len(prompt),
        }
        model_snapshot = {
            "model_code": "qwen",
            "runtime": "OLLAMA",
            "parameters": {"num_predict": 720, "num_ctx": 8192},
        }

        retry_prompt, retry_snapshot, retry_model = _prepare_evaluation_attempt(
            prompt,
            prompt_snapshot,
            model_snapshot,
            2,
        )

        self.assertIn("LẦN THỬ LẠI", retry_prompt)
        self.assertTrue(retry_snapshot["retry_instruction_applied"])
        self.assertEqual(retry_snapshot["retry_attempt"], 2)
        self.assertNotEqual(retry_snapshot["rendered_prompt_hash"], "initial")
        self.assertEqual(
            retry_model["parameters"]["num_predict"],
            settings.evaluation_retry_num_predict,
        )
        self.assertGreater(
            settings.evaluation_retry_num_predict,
            settings.evaluation_num_predict,
        )
        self.assertEqual(retry_model["parameters"]["num_ctx"], 8192)
        self.assertEqual(model_snapshot["parameters"]["num_predict"], 720)

    def test_duplicate_question_creates_draft_copy_without_review_state(self):
        teacher = _current_user("Teacher")
        question_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)
        source_question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-SOURCE",
            "current_version": 3,
            "current_version_id": version_id,
            "approved_version_id": version_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "APPROVED",
            "publication_status": "PUBLISHED",
            "quality_summary": {"color": "GREEN"},
            "review_assignment": {"status": "ASSIGNED"},
            "latest_review_id": ObjectId(),
            "created_by_user_id": teacher.id,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        source_version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 3,
            "origin": "AI",
            "generation_run_id": ObjectId(),
            "document_id": None,
            "created_by_user_id": teacher.id,
            "generated_by_model_id": ObjectId(),
            "classification": {
                "subject": {"id": None},
                "chapter": {"id": None},
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 2},
                "difficulty": "de",
            },
            "clos": [{"code": "CLO1"}],
            "content": "Queue follows FIFO.",
            "question_data": {"options": {"A": "FIFO", "B": "LIFO"}, "correct_answer": "A"},
            "sources": [],
            "keywords": ["queue"],
            "content_hash": "source-hash",
            "change_note": "Approved version",
            "created_at": now,
        }

        class DuplicateRepository:
            def __init__(self):
                self.created_question = None
                self.created_version = None

            def find_pair(self, _question_id):
                return source_question, source_version

            def create(self, aggregate, version):
                self.created_question = aggregate
                self.created_version = version
                return aggregate, version

        repository = DuplicateRepository()
        service = QuestionService(repository=repository, references=object())

        duplicated = service.duplicate(str(question_id), teacher)

        self.assertNotEqual(duplicated["id"], str(question_id))
        self.assertEqual(duplicated["review_status"], "DRAFT")
        self.assertEqual(duplicated["evaluation_status"], "NOT_STARTED")
        self.assertEqual(duplicated["publication_status"], "NOT_PUBLISHED")
        self.assertIsNone(duplicated["approved_version_id"])
        self.assertEqual(duplicated["content"], source_version["content"])
        self.assertEqual(repository.created_question["created_by_user_id"], teacher.id)
        self.assertEqual(repository.created_version["origin"], "MANUAL")
        self.assertEqual(source_question["review_status"], "APPROVED")

    def test_question_sharing_allows_read_without_write(self):
        owner = _current_user("Teacher")
        shared_teacher = _current_user("Teacher")
        question_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-SHARE",
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "NOT_STARTED",
            "review_status": "DRAFT",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {},
            "review_assignment": {"status": "UNASSIGNED"},
            "shared_with_user_ids": [shared_teacher.id],
            "shared_scope": "PRIVATE",
            "latest_review_id": None,
            "created_by_user_id": owner.id,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }
        version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": None,
            "created_by_user_id": owner.id,
            "generated_by_model_id": None,
            "classification": {"subject": {"id": None}, "chapter": {"id": None}, "assessment_type": "TRAC_NGHIEM", "bloom": {"level": 2}, "difficulty": None},
            "clos": [],
            "content": "Shared question",
            "question_data": {},
            "sources": [],
            "keywords": [],
            "content_hash": "share-hash",
            "change_note": "Initial version",
            "created_at": now,
        }

        class SharingRepository:
            def __init__(self):
                self.fields = None

            def find_pair(self, _question_id):
                return question, version

            def update_sharing(self, _question_id, fields, *, audit=None):
                self.fields = fields
                self.audit = audit
                question.update(normalize_sharing_fields(fields))
                return question, version

            def archive(self, _question_id, *, audit=None, **_kwargs):
                question["lifecycle_status"] = "ARCHIVED"
                return True

        repository = SharingRepository()
        service = QuestionService(repository=repository, references=object())

        self.assertEqual(service.get(str(question_id), shared_teacher)["id"], str(question_id))
        with self.assertRaises(PermissionError):
            service.update_sharing(
                str(question_id), QuestionSharingRequest(shared_with_user_ids=[]), shared_teacher,
            )
        with self.assertRaises(PermissionError):
            service.update(
                str(question_id),
                QuestionUpdateRequest(expected_version=1, content="Edited"),
                shared_teacher.id,
                actor_role="Teacher",
            )
        subject_wide = QuestionSharingRequest(shared_with_user_ids=[str(shared_teacher.id)], shared_scope="SUBJECT")
        with self.assertRaisesRegex(ValueError, "đã được duyệt"):
            service.update_sharing(str(question_id), subject_wide, owner)
        question["approved_version_id"] = version_id
        updated = service.update_sharing(str(question_id), subject_wide, owner)
        self.assertEqual(updated["shared_scope"], "SUBJECT")
        self.assertEqual(repository.fields["shared_scope"], "SUBJECT")
        self.assertEqual(repository.audit["action"], "question.sharing_update")

        editor = _current_user("Teacher")
        updated = service.update_sharing(
            str(question_id),
            QuestionSharingRequest(shared_with_user_ids=[str(shared_teacher.id)],
                                   shared_edit_user_ids=[str(editor.id)]),
            owner,
        )
        self.assertEqual(updated["shared_edit_user_ids"], [str(editor.id)])
        # Editors are added to the view list so reads and listings include them.
        self.assertIn(str(editor.id), updated["shared_with_user_ids"])
        service._ensure_write_access((question, version), editor)
        with self.assertRaises(PermissionError):
            service._ensure_write_access((question, version), shared_teacher)
        # An editor who authored the current version still cannot archive or reshare.
        version["created_by_user_id"] = editor.id
        with self.assertRaises(PermissionError):
            service.archive(str(question_id), editor)
        with self.assertRaises(PermissionError):
            service.update_sharing(str(question_id), QuestionSharingRequest(), editor)

        admin = _current_user("Admin")
        transfer = QuestionOwnerTransferRequest(new_owner_user_id=str(editor.id), reason="Bàn giao học phần")
        with patch("modules.questions.service.get_database", return_value=None), \
                patch("modules.questions.service.users_by_ids",
                      return_value=[{"_id": editor.id, "role": "Teacher"}]):
            with self.assertRaises(PermissionError):
                service.transfer_owner(str(question_id), transfer, owner)
            moved = service.transfer_owner(str(question_id), transfer, admin)
        self.assertEqual(question["created_by_user_id"], editor.id)
        self.assertEqual(moved["shared_edit_user_ids"], [])
        self.assertEqual(repository.audit["action"], "question.owner_transferred")

    def test_question_create_with_explicit_permission_still_checks_document_access(self):
        owner = _current_user("Teacher")
        actor = _current_user("Reviewer", permissions=["questions.generate"])
        document_id = ObjectId()

        class References:
            def find_document(self, _document_id):
                return {
                    "_id": document_id,
                    "title": "Private source",
                    "original_filename": "private.pdf",
                    "uploaded_by_user_id": owner.id,
                    "shared_with_user_ids": [],
                    "shared_scope": "PRIVATE",
                }

            def find_chunk(self, _chunk_id):
                return None

        service = QuestionService(repository=object(), references=References())

        with self.assertRaises(PermissionError):
            service.create(
                QuestionCreateRequest(
                    content="Question from a private document",
                    document_id=str(document_id),
                ),
                actor.id,
                actor_role=actor.role,
                current_user=actor,
            )

    def test_reviewer_must_hold_active_review_lock(self):
        service = QuestionWorkflowService(database=None)
        reviewer_id = ObjectId()
        reviewer = _current_user("Reviewer", reviewer_id)
        now = datetime.now(timezone.utc)

        with self.assertRaises(PermissionError):
            service._ensure_review_lock(
                {"review_assignment": {"status": "ASSIGNED", "reviewer_user_id": reviewer_id}},
                reviewer,
                now,
            )

        service._ensure_review_lock(
            {
                "review_assignment": {
                    "status": "IN_REVIEW",
                    "reviewer_user_id": reviewer_id,
                    "lock_expires_at": (now + timedelta(minutes=1)).replace(tzinfo=None),
                }
            },
            reviewer,
            now,
        )

        with self.assertRaises(ValueError):
            service._ensure_review_lock(
                {
                    "review_assignment": {
                        "status": "IN_REVIEW",
                        "reviewer_user_id": reviewer_id,
                        "lock_expires_at": now - timedelta(minutes=1),
                    }
                },
                reviewer,
                now,
            )

    def test_review_assignment_claim_and_review_enforce_lock_owner(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        other_reviewer = _current_user("Reviewer")
        question_id = ObjectId()
        version_id = ObjectId()
        subject_id = ObjectId()
        now = datetime.now(timezone.utc)
        version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": None,
            "created_by_user_id": teacher.id,
            "generated_by_model_id": None,
            "classification": {
                "subject": {"id": None},
                "chapter": {"id": None},
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 2},
                "difficulty": None,
            },
            "clos": [],
            "content": "Stack follows which rule?",
            "question_data": {
                "options": {"A": "LIFO", "B": "FIFO"},
                "correct_answer": "A",
                "explanation": "Stack is last-in-first-out.",
            },
            "sources": [],
            "keywords": [],
            "content_hash": "hash-review-lock",
            "change_note": "Initial version",
            "created_at": now,
        }
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-LOCK",
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "PENDING",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {"overall_score": 0.91, "color": "GREEN"},
            "review_assignment": {"status": "UNASSIGNED"},
            "latest_review_id": None,
            "subject_id": subject_id,
            "created_by_user_id": teacher.id,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }

        class FakeWorkflowDatabase:
            def __init__(self):
                self.questions = InMemoryCollection([question])
                self.question_versions = InMemoryCollection([version])
                self.users = InMemoryCollection(
                    [
                        {"_id": reviewer.id, "role": "Reviewer", "is_active": True, "review_subject_ids": [subject_id]},
                        {"_id": other_reviewer.id, "role": "Reviewer", "is_active": True,
                         "review_subject_ids": [subject_id]},
                    ]
                )
                self.question_reviews = InMemoryCollection()
                self.evaluation_jobs = InMemoryCollection()
                self.audit_logs = InMemoryCollection()
                self.notifications = InMemoryCollection()

        db = FakeWorkflowDatabase()
        original_transaction = question_workflow_module.mongo_transaction
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            service = QuestionWorkflowService(db)

            assigned = service.assign_review(
                str(question_id),
                ReviewAssignmentRequest(
                    reviewer_user_id=str(reviewer.id),
                    note="Assign to reviewer",
                ),
                admin,
            )
            with self.assertRaises(PermissionError):
                service.claim_review(str(question_id), other_reviewer)

            claimed = service.claim_review(str(question_id), reviewer)
            with self.assertRaises(PermissionError):
                service.release_review(str(question_id), other_reviewer)
            with self.assertRaises(PermissionError):
                service.review(
                    str(question_id),
                    ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                    other_reviewer,
                )

            review = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )
        finally:
            question_workflow_module.mongo_transaction = original_transaction

        self.assertEqual(assigned["review_assignment"]["status"], "ASSIGNED")
        self.assertEqual(assigned["review_assignment"]["reviewer_user_id"], str(reviewer.id))
        self.assertEqual(claimed["review_assignment"]["status"], "IN_REVIEW")
        self.assertEqual(claimed["review_assignment"]["reviewer_user_id"], str(reviewer.id))
        self.assertEqual(review["decision"], "APPROVED")
        updated_question = db.questions.find_one({"_id": question_id})
        self.assertEqual(updated_question["review_status"], "APPROVED")
        self.assertEqual(updated_question["approved_version_id"], version_id)
        self.assertEqual(updated_question["review_assignment"]["status"], "UNASSIGNED")
        self.assertEqual(len(db.question_reviews.records), 1)
        self.assertEqual(len(db.notifications.records), 2)
        self.assertEqual(db.notifications.records[0]["recipient_user_id"], reviewer.id)
        self.assertEqual(db.notifications.records[0]["type"], "QUESTION_REVIEW_ASSIGNED")
        self.assertEqual(db.notifications.records[0]["link"], f"/kiem-duyet?questionId={question_id}")
        self.assertEqual(db.notifications.records[1]["recipient_user_id"], teacher.id)
        self.assertEqual(db.notifications.records[1]["type"], "QUESTION_APPROVED")
        self.assertEqual(
            [event["action"] for event in db.audit_logs.records],
            ["QUESTION_REVIEW_ASSIGNED", "QUESTION_REVIEW_CLAIMED", "QUESTION_APPROVED"],
        )

    def test_reviewer_hold_cap_and_renewal_cap(self):
        reviewer = _current_user("Reviewer")
        author = ObjectId()
        subject_id = ObjectId()
        now = datetime.now(timezone.utc)
        held_question, held_version = _pending_question_pair(
            "Q-HELD", subject_id, now, author, now,
            review_assignment={"status": "IN_REVIEW", "reviewer_user_id": reviewer.id,
                               "lock_expires_at": now + timedelta(hours=1)},
        )
        target, target_version = _pending_question_pair("Q-NEW", subject_id, now, author, now)

        class Db:
            questions = InMemoryCollection([held_question, target])
            question_versions = InMemoryCollection([held_version, target_version])
            users = InMemoryCollection([{"_id": reviewer.id, "role": "Reviewer", "is_active": True,
                                         "review_subject_ids": [subject_id]}])
            audit_logs = InMemoryCollection()

        service = QuestionWorkflowService(Db())
        with (
            patch.object(settings, "review_max_held_questions", 1),
            patch.object(settings, "review_max_lock_renewals", 2),
        ):
            with self.assertRaisesRegex(ValueError, "tối đa 1 câu"):
                service.claim_review(str(target["_id"]), reviewer)

            renewed = [service.renew_review(str(held_question["_id"]), reviewer) for _ in range(2)]
            with self.assertRaisesRegex(ValueError, "tối đa 2 lần"):
                service.renew_review(str(held_question["_id"]), reviewer)

        self.assertEqual(renewed[-1]["review_assignment"]["renewal_count"], 2)

    def _question_service_db(self, *pairs):
        class Db:
            questions = InMemoryCollection([question for question, _version in pairs])
            question_versions = InMemoryCollection([version for _question, version in pairs])
            audit_logs = InMemoryCollection()
        return Db()

    def test_withdraw_pending_question_only_while_unclaimed(self):
        teacher = _current_user("Teacher")
        now = datetime.now(timezone.utc)
        free, free_version = _pending_question_pair("Q-FREE", ObjectId(), now, teacher.id, now)
        claimed, claimed_version = _pending_question_pair(
            "Q-CLAIMED", ObjectId(), now, teacher.id, now,
            review_assignment={"status": "IN_REVIEW", "reviewer_user_id": ObjectId(),
                               "lock_expires_at": now + timedelta(hours=1)},
        )
        db = self._question_service_db((free, free_version), (claimed, claimed_version))
        service = QuestionService(MongoQuestionRepository(db), references=None)
        with patch.object(question_repository_module, "mongo_transaction", lambda: nullcontext(None)):
            withdrawn = service.withdraw_submission(str(free["_id"]), 1, teacher)
            with self.assertRaises(PermissionError):
                service.withdraw_submission(str(claimed["_id"]), 1, teacher)

        self.assertEqual(withdrawn["review_status"], "DRAFT")
        self.assertEqual(db.questions.find_one({"_id": claimed["_id"]})["review_status"], "PENDING")
        self.assertEqual([event["action"] for event in db.audit_logs.records], ["question.submission_withdrawn"])

    def test_restore_archived_question_keeps_approved_version_and_requires_owner(self):
        teacher = _current_user("Teacher")
        now = datetime.now(timezone.utc)
        question, version = _pending_question_pair("Q-ARCH", ObjectId(), now, teacher.id, now)
        question.update(lifecycle_status="ARCHIVED", archived_at=now, review_status="APPROVED",
                        approved_version_id=version["_id"])
        db = self._question_service_db((question, version))
        service = QuestionService(MongoQuestionRepository(db), references=None)
        with patch.object(question_repository_module, "mongo_transaction", lambda: nullcontext(None)):
            with self.assertRaises(LookupError):
                service.restore(str(question["_id"]), _current_user("Teacher"))
            listed = service.list_archived(1, 20, teacher)
            restored = service.restore(str(question["_id"]), teacher)

        self.assertEqual(listed["total"], 1)
        self.assertEqual(restored["lifecycle_status"], "ACTIVE")
        self.assertEqual(restored["review_status"], "APPROVED")
        self.assertEqual(str(restored["approved_version_id"]), str(version["_id"]))
        self.assertEqual(db.audit_logs.records[-1]["action"], "question.restored")

    def test_unclaimed_pending_questions_escalate_to_admins_then_requeue(self):
        now = datetime.now(timezone.utc)
        admin_id, reviewer_id = ObjectId(), ObjectId()
        late, late_version = _pending_question_pair(
            "Q-LATE", ObjectId(), now - timedelta(hours=80), ObjectId(), now,
        )
        stale, stale_version = _pending_question_pair(
            "Q-STALE", ObjectId(), now - timedelta(days=8), ObjectId(), now,
            review_assignment={"status": "ASSIGNED", "reviewer_user_id": reviewer_id, "lock_expires_at": None},
        )

        class Db:
            questions = InMemoryCollection([late, stale])
            question_versions = InMemoryCollection([late_version, stale_version])
            users = InMemoryCollection([{"_id": admin_id, "role": "Admin", "is_active": True}])
            notifications = InMemoryCollection()
            audit_logs = InMemoryCollection()

        db = Db()
        service = QuestionWorkflowService(db)
        with patch.object(question_workflow_module, "mongo_transaction", lambda: nullcontext(None)):
            first = service.escalate_overdue_reviews(now)
            second = service.escalate_overdue_reviews(now)

        self.assertEqual(first, {"notified": 1, "requeued": 1})
        self.assertEqual(second, {"notified": 1, "requeued": 0})
        requeued = db.questions.find_one({"_id": stale["_id"]})
        self.assertEqual(requeued["review_assignment"]["status"], "UNASSIGNED")
        self.assertEqual(requeued["review_assignment"]["release_reason"], "escalation_requeued")
        escalated = [item for item in db.notifications.records if item["type"] == "QUESTION_REVIEW_ESCALATED"]
        self.assertEqual({str(item["entity"]["id"]) for item in escalated}, {str(late["_id"]), str(stale["_id"])})

    def test_escalation_batch_skips_questions_already_handled(self):
        now = datetime.now(timezone.utc)
        admin_id = ObjectId()
        handled_at = now - timedelta(hours=90)
        handled, handled_version = _pending_question_pair(
            "Q-HANDLED", ObjectId(), handled_at, ObjectId(), now,
        )
        handled["review_escalation"] = {"submitted_at": handled_at, "notified_at": now - timedelta(hours=10)}
        newer, newer_version = _pending_question_pair(
            "Q-NEWER", ObjectId(), now - timedelta(hours=80), ObjectId(), now,
        )

        class Db:
            questions = InMemoryCollection([handled, newer])
            question_versions = InMemoryCollection([handled_version, newer_version])
            users = InMemoryCollection([{"_id": admin_id, "role": "Admin", "is_active": True}])
            notifications = InMemoryCollection()
            audit_logs = InMemoryCollection()

        db = Db()
        service = QuestionWorkflowService(db)
        with (
            patch.object(question_workflow_module, "mongo_transaction", lambda: nullcontext(None)),
            patch.object(question_workflow_module, "SLA_REMINDER_BATCH_SIZE", 1),
        ):
            result = service.escalate_overdue_reviews(now)

        self.assertEqual(result, {"notified": 1, "requeued": 0})
        escalated = [item for item in db.notifications.records if item["type"] == "QUESTION_REVIEW_ESCALATED"]
        self.assertEqual({str(item["entity"]["id"]) for item in escalated}, {str(newer["_id"])})

    def test_secondary_review_requires_a_different_reviewer_and_comments_notify_mentions(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        second_reviewer = _current_user("Reviewer")
        question_id = ObjectId()
        version_id = ObjectId()
        subject_id = ObjectId()
        now = datetime.now(timezone.utc)
        version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": None,
            "created_by_user_id": teacher.id,
            "generated_by_model_id": None,
            "classification": {
                "subject": {"id": None},
                "chapter": {"id": None},
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 2},
                "difficulty": None,
            },
            "clos": [],
            "content": "Queue follows which rule?",
            "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
            "sources": [],
            "keywords": [],
            "content_hash": "hash-secondary",
            "change_note": "Initial version",
            "created_at": now,
        }
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-SECONDARY",
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "PENDING",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {"overall_score": 0.9, "color": "GREEN"},
            "review_assignment": {"status": "UNASSIGNED"},
            "latest_review_id": None,
            "subject_id": subject_id,
            "created_by_user_id": teacher.id,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }

        class FakeWorkflowDatabase:
            def __init__(self):
                self.questions = InMemoryCollection([question])
                self.question_versions = InMemoryCollection([version])
                self.users = InMemoryCollection(
                    [
                        {"_id": teacher.id, "role": "Teacher", "is_active": True},
                        {"_id": reviewer.id, "role": "Reviewer", "is_active": True, "review_subject_ids": [subject_id]},
                        {"_id": second_reviewer.id, "role": "Reviewer", "is_active": True,
                         "review_subject_ids": [subject_id]},
                    ]
                )
                self.question_reviews = InMemoryCollection()
                self.evaluation_jobs = InMemoryCollection()
                self.question_comments = InMemoryCollection()
                self.audit_logs = InMemoryCollection()
                self.notifications = InMemoryCollection()

        db = FakeWorkflowDatabase()
        original_transaction = question_workflow_module.mongo_transaction
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            service = QuestionWorkflowService(db)

            service.claim_review(str(question_id), reviewer)
            primary = service.review(
                str(question_id),
                ReviewCreateRequest(
                    expected_version=1,
                    decision="APPROVED",
                    secondary_required=True,
                    secondary_reason="Câu quan trọng",
                ),
                reviewer,
            )
            after_primary = dict(db.questions.find_one({"_id": question_id}))
            available = [item for item in db.notifications.records if item["type"] == "QUESTION_SECONDARY_REVIEW_AVAILABLE"]
            self.assertEqual([item["recipient_user_id"] for item in available], [second_reviewer.id])
            self.assertEqual(available[0]["link"], f"/kiem-duyet?questionId={question_id}")
            with self.assertRaises(PermissionError):
                service.claim_review(str(question_id), reviewer)
            service.claim_review(str(question_id), second_reviewer)
            secondary = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                second_reviewer,
            )
            comment = service.add_comment(
                str(question_id),
                QuestionCommentCreateRequest(
                    body="Nhờ giảng viên xem thêm ghi chú này",
                    mention_user_ids=[str(teacher.id)],
                ),
                second_reviewer,
            )
        finally:
            question_workflow_module.mongo_transaction = original_transaction

        self.assertEqual(primary["resulting_status"], "PENDING")
        self.assertEqual(after_primary["review_status"], "PENDING")
        self.assertEqual(after_primary["secondary_review"]["status"], "AWAITING_SECONDARY")
        self.assertEqual(secondary["review_stage"], "SECONDARY")
        final_question = db.questions.find_one({"_id": question_id})
        self.assertEqual(final_question["review_status"], "APPROVED")
        self.assertEqual(final_question["approved_version_id"], version_id)
        self.assertEqual(final_question["secondary_review"]["status"], "COMPLETED")
        self.assertEqual(comment["body"], "Nhờ giảng viên xem thêm ghi chú này")
        self.assertEqual(db.notifications.records[-1]["type"], "QUESTION_MENTION")

    def _review_guard_fixture(self, *, author, users, **question_fields):
        question_id = ObjectId()
        version_id = ObjectId()
        subject_id = question_fields.pop("subject_id", ObjectId())
        now = datetime.now(timezone.utc)
        version = {
            "_id": version_id,
            "schema_version": SCHEMA_VERSION,
            "question_id": question_id,
            "version": 1,
            "origin": "MANUAL",
            "generation_run_id": None,
            "document_id": None,
            "created_by_user_id": author.id,
            "generated_by_model_id": None,
            "classification": {
                "subject": {"id": subject_id},
                "chapter": {"id": None},
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 2},
                "difficulty": None,
            },
            "clos": [],
            "content": "Which structure is LIFO?",
            "question_data": {"options": {"A": "Stack", "B": "Queue"}, "correct_answer": "A"},
            "sources": [],
            "keywords": [],
            "content_hash": "hash-review-guard",
            "change_note": "Initial version",
            "created_at": now,
        }
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-GUARD",
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": None,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "PENDING",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {"overall_score": 0.9, "color": "GREEN"},
            "review_assignment": {"status": "UNASSIGNED"},
            "latest_review_id": None,
            "subject_id": subject_id,
            "created_by_user_id": author.id,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
            **question_fields,
        }

        class FakeWorkflowDatabase:
            def __init__(self):
                self.questions = InMemoryCollection([question])
                self.question_versions = InMemoryCollection([version])
                self.users = InMemoryCollection(
                    [
                        {"_id": user.id, "role": user.role, "is_active": True, "review_subject_ids": [subject_id]}
                        for user in users
                    ]
                )
                self.question_reviews = InMemoryCollection()
                self.evaluation_jobs = InMemoryCollection()
                self.question_comments = InMemoryCollection()
                self.audit_logs = InMemoryCollection()
                self.notifications = InMemoryCollection()

        return FakeWorkflowDatabase(), question_id, version_id

    def _with_workflow_service(self, db, callback):
        original_transaction = question_workflow_module.mongo_transaction
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            return callback(QuestionWorkflowService(db))
        finally:
            question_workflow_module.mongo_transaction = original_transaction

    def test_admin_assignment_reserves_question_beyond_interactive_lock(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        other_reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer, other_reviewer],
        )

        def scenario(service):
            before = datetime.now(timezone.utc)
            assigned = service.assign_review(
                str(question_id),
                ReviewAssignmentRequest(reviewer_user_id=str(reviewer.id)),
                admin,
            )
            stored = db.questions.find_one({"_id": question_id})["review_assignment"]
            expires_at = stored["lock_expires_at"]
            self.assertGreaterEqual(
                expires_at,
                before + timedelta(hours=settings.review_assignment_timeout_hours) - timedelta(seconds=5),
            )
            # Well past the 30-minute interactive lock, the assignee still owns it.
            with self.assertRaises(PermissionError):
                service.claim_review(str(question_id), other_reviewer)
            # Once the assignment window lapses, anyone may pick it up.
            stored["lock_expires_at"] = before - timedelta(minutes=1)
            claimed = service.claim_review(str(question_id), other_reviewer)
            return assigned, claimed

        assigned, claimed = self._with_workflow_service(db, scenario)
        self.assertEqual(assigned["review_assignment"]["status"], "ASSIGNED")
        self.assertEqual(claimed["review_assignment"]["reviewer_user_id"], str(other_reviewer.id))

    def test_review_lock_renewal_is_limited_to_current_holder(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        other_reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[teacher, reviewer, other_reviewer],
        )

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            stored = db.questions.find_one({"_id": question_id})["review_assignment"]
            stored["lock_expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
            with self.assertRaises(PermissionError):
                service.renew_review(str(question_id), other_reviewer)
            renewed = service.renew_review(str(question_id), reviewer)
            review = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )
            return renewed, review

        renewed, review = self._with_workflow_service(db, scenario)
        self.assertEqual(renewed["review_assignment"]["status"], "IN_REVIEW")
        self.assertEqual(review["decision"], "APPROVED")

    def test_admin_must_claim_and_takeover_needs_reason(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(author=teacher, users=[admin, teacher, reviewer])

        def scenario(service):
            with self.assertRaises(PermissionError):
                service.review(str(question_id), ReviewCreateRequest(expected_version=1, decision="APPROVED"), admin)
            service.claim_review(str(question_id), reviewer)
            with self.assertRaises(PermissionError):
                service.review(str(question_id), ReviewCreateRequest(expected_version=1, decision="APPROVED"), admin)
            with self.assertRaises(ValueError):
                service.claim_review(str(question_id), admin)
            with self.assertRaises(ValueError):
                service.claim_review(str(question_id), admin, reason="gấp")
            claimed = service.claim_review(str(question_id), admin, reason="Reviewer nghỉ phép")
            review = service.review(
                str(question_id), ReviewCreateRequest(expected_version=1, decision="APPROVED"), admin,
            )
            return claimed, review

        claimed, review = self._with_workflow_service(db, scenario)
        self.assertEqual(claimed["review_assignment"]["reviewer_user_id"], str(admin.id))
        self.assertEqual(review["decision"], "APPROVED")
        taken = [item for item in db.audit_logs.records if item["action"] == "QUESTION_REVIEW_TAKEN_OVER"]
        self.assertEqual(len(taken), 1)
        self.assertIn(
            reviewer.id,
            [item["recipient_user_id"] for item in db.notifications.records
             if item["type"] == "QUESTION_REVIEW_TAKEN_OVER"],
        )

    def test_reclaim_does_not_reset_renewal_counter(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(author=teacher, users=[teacher, reviewer])

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            service.renew_review(str(question_id), reviewer)
            service.release_review(str(question_id), reviewer)
            return service.claim_review(str(question_id), reviewer)

        reclaimed = self._with_workflow_service(db, scenario)
        stored = db.questions.find_one({"_id": question_id})["review_assignment"]
        self.assertEqual(stored["renewal_count"], 1)
        self.assertEqual(reclaimed["review_assignment"]["status"], "IN_REVIEW")

    def test_approving_flagged_question_requires_acknowledging_every_flag(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(author=teacher, users=[teacher, reviewer])
        db.questions.find_one({"_id": question_id})["review_submission"] = {
            "submitted_at": datetime.now(timezone.utc),
            "checks": {"flags": ["weak_grounding", "near_duplicate"], "warnings": []},
        }

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            for acknowledged in ([], ["weak_grounding"]):
                with self.assertRaisesRegex(ValueError, "near_duplicate"):
                    service.review(
                        str(question_id),
                        ReviewCreateRequest(expected_version=1, decision="APPROVED", acknowledged_flags=acknowledged),
                        reviewer,
                    )
            return service.review(
                str(question_id),
                ReviewCreateRequest(
                    expected_version=1, decision="APPROVED",
                    acknowledged_flags=["near_duplicate", " weak_grounding "],
                ),
                reviewer,
            )

        self._with_workflow_service(db, scenario)
        self.assertEqual(db.questions.find_one({"_id": question_id})["review_status"], "APPROVED")
        stored_review = db.question_reviews.find_one({"question_id": question_id})
        self.assertEqual(stored_review["acknowledged_flags"], ["weak_grounding", "near_duplicate"])

    def test_reopening_approved_question_keeps_primary_reviewer_out_of_secondary(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        second_reviewer = _current_user("Reviewer")
        db, question_id, version_id = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer, second_reviewer],
        )

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )
            # A reviewer cannot silently un-approve a question.
            with self.assertRaises(PermissionError):
                service.set_secondary_review(
                    str(question_id),
                    SecondaryReviewRequest(required=True, reason="Xem lại"),
                    reviewer,
                )
            reopened = service.set_secondary_review(
                str(question_id),
                SecondaryReviewRequest(required=True, reason="Câu thi cuối kỳ"),
                admin,
            )
            with self.assertRaises(PermissionError):
                service.claim_review(str(question_id), reviewer)
            service.claim_review(str(question_id), second_reviewer)
            secondary = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                second_reviewer,
            )
            return reopened, secondary

        reopened, secondary = self._with_workflow_service(db, scenario)
        self.assertEqual(reopened["review_status"], "PENDING")
        self.assertEqual(reopened["secondary_review"]["status"], "AWAITING_SECONDARY")
        self.assertEqual(reopened["secondary_review"]["primary_reviewer_user_id"], str(reviewer.id))
        self.assertEqual(secondary["review_stage"], "SECONDARY")
        final_question = db.questions.find_one({"_id": question_id})
        self.assertEqual(final_question["review_status"], "APPROVED")
        self.assertEqual(final_question["approved_version_id"], version_id)
        self.assertEqual(final_question["secondary_review"]["status"], "COMPLETED")

    def test_secondary_review_requested_before_primary_approval(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        second_reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer, second_reviewer],
        )

        def scenario(service):
            requested = service.set_secondary_review(
                str(question_id),
                SecondaryReviewRequest(
                    required=True,
                    reason="Câu khó",
                    reviewer_user_id=str(second_reviewer.id),
                ),
                admin,
            )
            service.claim_review(str(question_id), reviewer)
            primary = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )
            after_primary = dict(db.questions.find_one({"_id": question_id}))
            service.claim_review(str(question_id), second_reviewer)
            secondary = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                second_reviewer,
            )
            return requested, primary, after_primary, secondary

        requested, primary, after_primary, secondary = self._with_workflow_service(db, scenario)
        self.assertEqual(requested["secondary_review"]["status"], "REQUESTED")
        self.assertEqual(requested["review_assignment"]["status"], "UNASSIGNED")
        self.assertEqual(primary["resulting_status"], "PENDING")
        self.assertEqual(after_primary["secondary_review"]["status"], "AWAITING_SECONDARY")
        self.assertEqual(after_primary["secondary_review"]["primary_reviewer_user_id"], reviewer.id)
        self.assertEqual(after_primary["review_assignment"]["status"], "ASSIGNED")
        self.assertEqual(after_primary["review_assignment"]["reviewer_user_id"], second_reviewer.id)
        self.assertIn(
            second_reviewer.id,
            [
                item["recipient_user_id"]
                for item in db.notifications.records
                if item["type"] == "QUESTION_REVIEW_ASSIGNED"
            ],
        )
        self.assertEqual(secondary["review_stage"], "SECONDARY")
        self.assertEqual(db.questions.find_one({"_id": question_id})["review_status"], "APPROVED")

    def test_authors_cannot_review_their_own_questions(self):
        admin = _current_user("Admin")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(author=admin, users=[admin, reviewer])

        reason = "Không còn người duyệt khác phụ trách học phần"

        def self_review(service, **fields):
            return service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED", **fields),
                admin,
            )

        def admin_scenario(service):
            # Break-glass disabled: neither a reason nor an override lets the author decide.
            for fields in ({}, {"self_review_reason": reason},
                           {"override": {"applied": True, "reason": "Không còn người duyệt khác"}}):
                with self.assertRaises(PermissionError):
                    self_review(service, **fields)

        self._with_workflow_service(db, admin_scenario)

        original_break_glass = settings.admin_self_review_break_glass
        settings.admin_self_review_break_glass = True
        try:
            # Another eligible reviewer exists, so break-glass is refused.
            with self.assertRaises(PermissionError):
                self._with_workflow_service(db, lambda service: self_review(service, self_review_reason=reason))

            db, question_id, _ = self._review_guard_fixture(author=admin, users=[admin])
            with self.assertRaises(ValueError):
                self._with_workflow_service(db, lambda service: self_review(service, self_review_reason="gấp"))
            with self.assertRaises(PermissionError):
                # Break-glass still requires holding the claim.
                self._with_workflow_service(db, lambda service: self_review(service, self_review_reason=reason))
            self._with_workflow_service(db, lambda service: service.claim_review(str(question_id), admin))
            review = self._with_workflow_service(db, lambda service: self_review(service, self_review_reason=reason))
        finally:
            settings.admin_self_review_break_glass = original_break_glass
        self.assertTrue(review["break_glass"])
        self.assertEqual(review["self_review_reason"], reason)
        self.assertEqual(review["resulting_status"], "PENDING")
        stored = db.questions.find_one({"_id": question_id})
        self.assertEqual(stored["secondary_review"]["status"], "AWAITING_SECONDARY")
        self.assertIsNone(stored["approved_version_id"])
        self.assertEqual(stored["break_glass_review"]["reason"], reason)
        self.assertIn("QUESTION_BREAK_GLASS_REVIEW", [item["action"] for item in db.audit_logs.records])

        teacher = _current_user("Teacher")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer],
        )
        db.question_versions.records[0]["created_by_user_id"] = reviewer.id

        def reviewer_scenario(service):
            with self.assertRaises(PermissionError):
                service.claim_review(str(question_id), reviewer)
            with self.assertRaises(ValueError):
                service.assign_review(
                    str(question_id),
                    ReviewAssignmentRequest(reviewer_user_id=str(reviewer.id)),
                    admin,
                )

        self._with_workflow_service(db, reviewer_scenario)

    def test_reviewer_decides_without_override_when_ai_has_no_verdict(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")

        def approve(service, question_id, **fields):
            service.claim_review(str(question_id), reviewer)
            return service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED", **fields),
                reviewer,
            )

        for status in ("NOT_STARTED", "ERROR", "STALE", "INSUFFICIENT_EVIDENCE"):
            db, question_id, _ = self._review_guard_fixture(
                author=teacher,
                users=[teacher, reviewer],
                evaluation_status=status,
            )
            review = self._with_workflow_service(db, lambda service: approve(service, question_id))
            self.assertFalse(review["override"]["applied"], status)
            self.assertEqual(review["resulting_status"], "APPROVED", status)

        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[teacher, reviewer],
            evaluation_status="FAILED",
        )

        def failed_scenario(service):
            with self.assertRaisesRegex(ValueError, "override"):
                approve(service, question_id)
            return service.review(
                str(question_id),
                ReviewCreateRequest(
                    expected_version=1,
                    decision="APPROVED",
                    override={"applied": True, "reason": "Đã đối chiếu nguồn"},
                ),
                reviewer,
            )

        review = self._with_workflow_service(db, failed_scenario)
        self.assertTrue(review["override"]["applied"])

    def test_reviewer_decision_interrupts_running_ai_evaluation(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, version_id = self._review_guard_fixture(
            author=teacher,
            users=[teacher, reviewer],
            evaluation_status="PROCESSING",
        )
        job_id = ObjectId()
        db.evaluation_jobs = InMemoryCollection(
            [
                {
                    "_id": job_id,
                    "question_id": question_id,
                    "question_version_id": version_id,
                    "status": "PROCESSING",
                    "locked_by": "worker-1",
                }
            ]
        )
        db.question_evaluations = InMemoryCollection()

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            review = service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )
            # Worker trả kết quả sau khi người duyệt đã chốt: kết quả bị bỏ.
            with self.assertRaises(question_workflow_module.EvaluationInterruptedError):
                service.evaluate(
                    str(question_id),
                    EvaluationCreateRequest(
                        expected_version=1,
                        scores=EvaluationScores(
                            faithfulness=0.9,
                            contextual_relevancy=0.9,
                            answer_relevancy=0.9,
                            bloom_alignment=0.9,
                            clo_alignment=0.9,
                        ),
                        model_snapshot={"model_code": "test", "model_name": "Test"},
                        policy_snapshot={
                            "version": 1,
                            "weights": {
                                "faithfulness": 0.35,
                                "contextual_relevancy": 0.20,
                                "answer_relevancy": 0.15,
                                "bloom_alignment": 0.15,
                                "clo_alignment": 0.15,
                            },
                            "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
                        },
                        evaluation_job_id=str(job_id),
                    ),
                    None,
                    require_active_job=True,
                )
            return review

        review = self._with_workflow_service(db, scenario)
        self.assertFalse(review["override"]["applied"])
        self.assertEqual(review["resulting_status"], "APPROVED")
        job = db.evaluation_jobs.find_one({"_id": job_id})
        self.assertEqual(job["status"], "CANCELLED")
        self.assertEqual(job["error"]["stage"], "REVIEWER_DECIDED")
        stored = db.questions.find_one({"_id": question_id})
        self.assertEqual(stored["review_status"], "APPROVED")
        self.assertEqual(stored["evaluation_status"], "NOT_STARTED")
        self.assertEqual(db.question_evaluations.records, [])

    def test_releasing_reviewer_work_returns_held_questions_to_queue(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer],
        )

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            return service.release_assignments_for_reviewer(reviewer.id, admin, "reviewer_deactivated")

        released = self._with_workflow_service(db, scenario)
        self.assertEqual(released, 1)
        assignment = db.questions.find_one({"_id": question_id})["review_assignment"]
        self.assertEqual(assignment["status"], "UNASSIGNED")
        self.assertEqual(assignment["release_reason"], "reviewer_deactivated")
        self.assertEqual(db.audit_logs.records[-1]["action"], "QUESTION_REVIEW_RELEASED")

    def test_admin_dashboard_breaks_down_reviewers_and_excludes_bulk_from_calibration(self):
        now = datetime.now(timezone.utc)
        admin = _current_user("Admin")
        busy_reviewer = ObjectId()
        idle_reviewer = ObjectId()
        late = now - timedelta(hours=settings.review_sla_hours + 5)
        base_question = {
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
        }
        version_ids = [ObjectId() for _ in range(6)]
        reviews = [
            {
                "_id": ObjectId(),
                "question_version_id": version_id,
                "reviewer_user_id": busy_reviewer,
                "decision": "APPROVED",
                "override": {"applied": index < 2},
                "revision_issues": [],
                "bulk": index == 5,
                "reviewed_at": now - timedelta(days=1),
            }
            for index, version_id in enumerate(version_ids)
        ]

        class FakeDashboardDatabase:
            def __init__(self):
                self.users = InMemoryCollection(
                    [
                        {"_id": admin.id, "role": "Admin", "is_active": True, "display_name": "Admin"},
                        {"_id": busy_reviewer, "role": "Reviewer", "is_active": True, "display_name": "Bận"},
                        {"_id": idle_reviewer, "role": "Reviewer", "is_active": True, "display_name": "Rảnh"},
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            **base_question,
                            "_id": ObjectId(),
                            "review_assignment": {"status": "IN_REVIEW", "reviewer_user_id": busy_reviewer},
                            "review_submission": {"submitted_at": late},
                        },
                        {
                            **base_question,
                            "_id": ObjectId(),
                            "review_assignment": {"status": "UNASSIGNED"},
                            "review_submission": {"submitted_at": now - timedelta(hours=1)},
                        },
                    ]
                )
                self.question_reviews = InMemoryCollection(reviews)
                # AI failed every version: only the non-bulk approvals count as disagreements.
                self.question_evaluations = InMemoryCollection(
                    [
                        {"_id": ObjectId(), "question_version_id": version_id, "passed": False, "created_at": now}
                        for version_id in version_ids
                    ]
                )
                self.audit_logs = InMemoryCollection()
                self.question_versions = InMemoryCollection()
                self.subjects = InMemoryCollection()

        dashboard = QuestionWorkflowService(FakeDashboardDatabase()).review_dashboard(admin)

        self.assertEqual(dashboard["workload"]["sla_breached"], 1)
        self.assertEqual(dashboard["workload"]["sla_hours"], settings.review_sla_hours)
        self.assertEqual(dashboard["performance"]["reviews_30d"], 6)
        self.assertEqual(dashboard["performance"]["bulk_count"], 1)
        self.assertEqual(dashboard["calibration"]["sample_size"], 5)
        rows = {row["user_id"]: row for row in dashboard["reviewers"]}
        busy = rows[str(busy_reviewer)]
        self.assertEqual(busy["reviews_30d"], 6)
        self.assertEqual(busy["override_count"], 2)
        self.assertEqual(busy["holding"], 1)
        self.assertEqual(busy["holding_sla_breached"], 1)
        self.assertEqual(busy["ai_sample_size"], 5)
        self.assertEqual(busy["ai_agreement_rate"], 0.0)
        self.assertEqual(busy["flags"], ["HIGH_OVERRIDE", "SLA_BREACHED", "NO_SUBJECTS"])
        idle = rows[str(idle_reviewer)]
        self.assertEqual(idle["reviews_30d"], 0)
        self.assertEqual(idle["flags"], ["NO_SUBJECTS"])
        self.assertEqual(dashboard["reviewers"][0]["user_id"], str(busy_reviewer))

        reviewer_view = QuestionWorkflowService(FakeDashboardDatabase()).review_dashboard(
            _current_user("Reviewer", busy_reviewer)
        )
        self.assertEqual(reviewer_view["reviewers"], [])

    def test_review_sla_reminders_notify_assignee_or_admins_once_per_submission(self):
        now = datetime.now(timezone.utc)
        admin_id = ObjectId()
        inactive_admin_id = ObjectId()
        reviewer_id = ObjectId()
        late = now - timedelta(hours=settings.review_sla_hours + 1)
        base_question = {
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
            "current_version_id": ObjectId(),
        }
        assigned_id, unassigned_id, fresh_id = ObjectId(), ObjectId(), ObjectId()

        class FakeReminderDatabase:
            def __init__(self):
                self.users = InMemoryCollection(
                    [
                        {"_id": admin_id, "role": "Admin", "is_active": True},
                        {"_id": inactive_admin_id, "role": "Admin", "is_active": False},
                        {"_id": reviewer_id, "role": "Reviewer", "is_active": True},
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            **base_question,
                            "_id": assigned_id,
                            "question_code": "Q-LATE-1",
                            "review_assignment": {"status": "ASSIGNED", "reviewer_user_id": reviewer_id},
                            "review_submission": {"submitted_at": late},
                        },
                        {
                            **base_question,
                            "_id": unassigned_id,
                            "question_code": "Q-LATE-2",
                            "review_assignment": {"status": "UNASSIGNED"},
                            "review_submission": {"submitted_at": late},
                        },
                        {
                            **base_question,
                            "_id": fresh_id,
                            "question_code": "Q-FRESH",
                            "review_assignment": {"status": "UNASSIGNED"},
                            "review_submission": {"submitted_at": now},
                        },
                    ]
                )
                self.notifications = InMemoryCollection()

        db = FakeReminderDatabase()
        service = QuestionWorkflowService(db)

        self.assertEqual(service.send_review_sla_reminders(now), 2)
        self.assertEqual(
            sorted((str(item["recipient_user_id"]), item["type"]) for item in db.notifications.records),
            sorted(
                [
                    (str(reviewer_id), "QUESTION_REVIEW_SLA_BREACHED"),
                    (str(admin_id), "QUESTION_REVIEW_SLA_BREACHED"),
                ]
            ),
        )
        self.assertEqual(service.send_review_sla_reminders(now), 0)

        # A resubmission that is late again earns a fresh reminder.
        db.questions.find_one({"_id": unassigned_id})["review_submission"]["submitted_at"] = late + timedelta(minutes=1)
        self.assertEqual(service.send_review_sla_reminders(now), 1)
        self.assertEqual(len(db.notifications.records), 3)

    def test_auto_assign_prefers_subject_specialists_and_balances_load(self):
        now = datetime.now(timezone.utc)
        admin = _current_user("Admin")
        teacher = ObjectId()
        subject_ctdl, subject_mmt = ObjectId(), ObjectId()
        specialist = ObjectId()  # CTDL only
        generalist_a = ObjectId()  # MMT only
        generalist_b = ObjectId()  # MMT only, but authored one question
        inactive = ObjectId()

        def question(code, subject_id, minutes_ago, *, author=teacher, **extra):
            return _pending_question_pair(
                code, subject_id, now - timedelta(minutes=minutes_ago), author, now, **extra
            )

        pairs = [
            question("Q-CTDL-1", subject_ctdl, 50),
            question("Q-CTDL-2", subject_ctdl, 40),
            question("Q-MMT-1", subject_mmt, 30),
            question("Q-MMT-OWN", subject_mmt, 20, author=generalist_b),
            question(
                "Q-HELD",
                subject_mmt,
                10,
                review_assignment={
                    "status": "IN_REVIEW",
                    "reviewer_user_id": generalist_a,
                    "lock_expires_at": now + timedelta(minutes=20),
                },
            ),
        ]

        class FakeAssignDatabase:
            def __init__(self):
                self.questions = InMemoryCollection([item[0] for item in pairs])
                self.question_versions = InMemoryCollection([item[1] for item in pairs])
                self.users = InMemoryCollection(
                    [
                        {"_id": specialist, "role": "Reviewer", "is_active": True, "display_name": "CTDL",
                         "review_subject_ids": [subject_ctdl]},
                        {"_id": generalist_a, "role": "Reviewer", "is_active": True, "display_name": "A",
                         "review_subject_ids": [subject_mmt]},
                        {"_id": generalist_b, "role": "Reviewer", "is_active": True, "display_name": "B",
                         "review_subject_ids": [subject_mmt]},
                        {"_id": inactive, "role": "Reviewer", "is_active": False, "display_name": "Off"},
                    ]
                )
                self.audit_logs = InMemoryCollection()
                self.notifications = InMemoryCollection()

        db = FakeAssignDatabase()
        result = QuestionWorkflowService(db).auto_assign_reviews(
            AutoAssignRequest(max_load_per_reviewer=5),
            admin,
        )

        by_code = {item["question_code"]: item["reviewer_user_id"] for item in result["assigned"]}
        # Specialist takes every CTDL question even while carrying load.
        self.assertEqual(by_code["Q-CTDL-1"], str(specialist))
        self.assertEqual(by_code["Q-CTDL-2"], str(specialist))
        # A already holds a live question, so the lighter generalist B gets MMT-1.
        self.assertEqual(by_code["Q-MMT-1"], str(generalist_b))
        # B authored this one, so it falls to A.
        self.assertEqual(by_code["Q-MMT-OWN"], str(generalist_a))
        self.assertNotIn("Q-HELD", by_code)
        self.assertNotIn(str(inactive), by_code.values())
        self.assertEqual(result["skipped"], [])
        stored = db.questions.find_one({"question_code": "Q-CTDL-1"})["review_assignment"]
        self.assertEqual(stored["status"], "ASSIGNED")
        self.assertEqual(stored["reviewer_user_id"], specialist)

        # Capacity caps stop assignment instead of overloading reviewers.
        db = FakeAssignDatabase()
        capped = QuestionWorkflowService(db).auto_assign_reviews(
            AutoAssignRequest(max_load_per_reviewer=1),
            admin,
        )
        self.assertEqual(
            sorted(item["reason"] for item in capped["skipped"]),
            ["NO_ELIGIBLE_REVIEWER"] * len(capped["skipped"]),
        )
        self.assertLessEqual(
            max(
                sum(1 for item in capped["assigned"] if item["reviewer_user_id"] == reviewer)
                for reviewer in {item["reviewer_user_id"] for item in capped["assigned"]}
            ),
            1,
        )

    def test_auto_assign_strict_subject_mode_and_optional_admin_pool(self):
        now = datetime.now(timezone.utc)
        admin_user = _current_user("Admin")
        teacher = ObjectId()
        subject_ctdl, subject_mmt = ObjectId(), ObjectId()
        admin_reviewer = ObjectId()  # created first, so it wins load ties
        specialist = ObjectId()
        generalist = ObjectId()
        pairs = [
            _pending_question_pair("Q-CTDL", subject_ctdl, now - timedelta(minutes=30), teacher, now),
            _pending_question_pair("Q-MMT", subject_mmt, now - timedelta(minutes=20), teacher, now),
            _pending_question_pair("Q-MMT-ADMIN", subject_mmt, now - timedelta(minutes=10), admin_reviewer, now),
        ]

        def database():
            class FakeAssignDatabase:
                questions = InMemoryCollection([item[0] for item in pairs])
                question_versions = InMemoryCollection([item[1] for item in pairs])
                users = InMemoryCollection(
                    [
                        {"_id": admin_reviewer, "role": "Admin", "is_active": True, "display_name": "Admin"},
                        {"_id": specialist, "role": "Reviewer", "is_active": True, "display_name": "CTDL",
                         "review_subject_ids": [subject_ctdl]},
                        {"_id": generalist, "role": "Reviewer", "is_active": True, "display_name": "Chung"},
                    ]
                )
                audit_logs = InMemoryCollection()
                notifications = InMemoryCollection()

            return FakeAssignDatabase()

        strict = QuestionWorkflowService(database()).auto_assign_reviews(
            AutoAssignRequest(subject_mode="strict"),
            admin_user,
        )
        self.assertEqual(
            [(item["question_code"], item["reviewer_user_id"]) for item in strict["assigned"]],
            [("Q-CTDL", str(specialist))],
        )
        self.assertEqual(
            {item["question_code"]: item["reason"] for item in strict["skipped"]},
            {"Q-MMT": "NO_SUBJECT_SPECIALIST", "Q-MMT-ADMIN": "NO_SUBJECT_SPECIALIST"},
        )

        with_admins = QuestionWorkflowService(database()).auto_assign_reviews(
            AutoAssignRequest(include_admins=True),
            admin_user,
        )
        by_code = {item["question_code"]: item["reviewer_user_id"] for item in with_admins["assigned"]}
        self.assertEqual(by_code["Q-CTDL"], str(specialist))
        self.assertEqual(by_code["Q-MMT"], str(admin_reviewer))
        # The Admin authored this one and the reviewer without subjects reviews nothing.
        self.assertNotIn("Q-MMT-ADMIN", by_code)
        self.assertEqual(
            {item["question_code"]: item["reason"] for item in with_admins["skipped"]},
            {"Q-MMT-ADMIN": "NO_ELIGIBLE_REVIEWER"},
        )

    def test_review_policy_forces_secondary_review_and_notifies_teacher(self):
        admin = _current_user("Admin")
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[admin, teacher, reviewer],
        )
        # Real Mongo upserts the _id from the equality filter; the in-memory fake needs it seeded.
        db.review_settings = InMemoryCollection([{"_id": "review_policy"}])

        def scenario(service):
            policy = service.update_review_policy(
                ReviewPolicyPayload(secondary_on_override=True, secondary_below_score=0.95),
                admin,
            )
            service.claim_review(str(question_id), reviewer)
            review = service.review(
                str(question_id),
                ReviewCreateRequest(
                    expected_version=1,
                    decision="APPROVED",
                    override={"applied": True, "reason": "Nguồn đủ, AI chấm sai"},
                ),
                reviewer,
            )
            return policy, review

        policy, review = self._with_workflow_service(db, scenario)
        self.assertTrue(policy["secondary_on_override"])
        self.assertEqual(policy["secondary_below_score"], 0.95)
        self.assertEqual(review["resulting_status"], "PENDING")
        self.assertIn("override", review["secondary_reason"])
        self.assertIn("dưới ngưỡng 0.95", review["secondary_reason"])
        stored = db.questions.find_one({"_id": question_id})
        self.assertEqual(stored["secondary_review"]["status"], "AWAITING_SECONDARY")
        self.assertEqual(stored["secondary_review"]["primary_reviewer_user_id"], reviewer.id)
        teacher_notes = [item for item in db.notifications.records if item["recipient_user_id"] == teacher.id]
        self.assertEqual([item["type"] for item in teacher_notes], ["QUESTION_SECONDARY_REVIEW_PENDING"])
        self.assertIn("REVIEW_POLICY_UPDATED", [event["action"] for event in db.audit_logs.records])

    def test_score_policy_ignores_stale_score_without_valid_ai_verdict(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        # Người duyệt vừa dừng AI: câu "chưa đánh giá" nhưng còn điểm cũ trong quality_summary.
        db, question_id, _ = self._review_guard_fixture(
            author=teacher,
            users=[teacher, reviewer],
            evaluation_status="NOT_STARTED",
            quality_summary={"overall_score": 0.64, "color": "YELLOW"},
        )
        db.review_settings = InMemoryCollection([{"_id": "review_policy", "secondary_below_score": 0.7}])

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            return service.review(
                str(question_id),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                reviewer,
            )

        review = self._with_workflow_service(db, scenario)
        self.assertEqual(review["resulting_status"], "APPROVED")
        self.assertNotIn("dưới ngưỡng", review["secondary_reason"] or "")

    def test_review_policy_exposes_configured_hold_durations(self):
        teacher = _current_user("Teacher")
        db, _question_id, _ = self._review_guard_fixture(author=teacher, users=[teacher])
        db.review_settings = InMemoryCollection([])
        policy = self._with_workflow_service(db, lambda service: service.get_review_policy())
        self.assertEqual(policy["lock_timeout_minutes"], settings.review_lock_timeout_minutes)
        self.assertEqual(policy["assignment_timeout_hours"], settings.review_assignment_timeout_hours)

    def test_review_subject_suggestions_rank_reviewed_subjects(self):
        now = datetime.now(timezone.utc)
        reviewer_id = ObjectId()
        subject_a, subject_b = ObjectId(), ObjectId()
        versions = [ObjectId() for _ in range(4)]

        class FakeSuggestionDatabase:
            question_reviews = InMemoryCollection(
                [
                    {"_id": ObjectId(), "reviewer_user_id": reviewer_id, "question_version_id": version_id,
                     "reviewed_at": now - timedelta(days=3)}
                    for version_id in versions
                ]
                + [
                    {"_id": ObjectId(), "reviewer_user_id": reviewer_id, "question_version_id": ObjectId(),
                     "reviewed_at": now - timedelta(days=400)},
                ]
            )
            question_versions = InMemoryCollection(
                [
                    {"_id": versions[0], "classification": {"subject": {"id": subject_a}}},
                    {"_id": versions[1], "classification": {"subject": {"id": subject_a}}},
                    {"_id": versions[2], "classification": {"subject": {"id": subject_a}}},
                    {"_id": versions[3], "classification": {"subject": {"id": subject_b}}},
                ]
            )
            subjects = InMemoryCollection(
                [
                    {"_id": subject_a, "subject_code": "CTDL", "subject_name": "Cấu trúc dữ liệu"},
                    {"_id": subject_b, "subject_code": "MMT", "subject_name": "Mạng máy tính"},
                ]
            )

        result = QuestionWorkflowService(FakeSuggestionDatabase()).suggest_review_subjects(str(reviewer_id))
        self.assertEqual(
            [(item["subject_code"], item["reviews"]) for item in result["items"]],
            [("CTDL", 3), ("MMT", 1)],
        )

    def test_editing_question_notifies_owners_of_open_exams(self):
        question_id = ObjectId()
        editor = ObjectId()
        owner = ObjectId()

        class FakeExamDatabase:
            exams = InMemoryCollection(
                [
                    {"_id": ObjectId(), "name": "Giữa kỳ", "status": "DRAFT", "created_by_user_id": owner,
                     "questions": [{"question_id": question_id}]},
                    {"_id": ObjectId(), "name": "Đã chốt", "status": "FINALIZED", "created_by_user_id": owner,
                     "questions": [{"question_id": question_id}]},
                    {"_id": ObjectId(), "name": "Của người sửa", "status": "READY", "created_by_user_id": editor,
                     "questions": [{"question_id": question_id}]},
                    {"_id": ObjectId(), "name": "Không liên quan", "status": "DRAFT", "created_by_user_id": owner,
                     "questions": [{"question_id": ObjectId()}]},
                ]
            )
            notifications = InMemoryCollection()

        db = FakeExamDatabase()
        created = NotificationService(db).notify_exam_owners_question_reopened(
            question_id=question_id,
            question_code="Q-EDIT",
            actor_user_id=editor,
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(db.notifications.records[0]["recipient_user_id"], owner)
        self.assertEqual(db.notifications.records[0]["type"], "EXAM_QUESTION_NEEDS_REVIEW")
        self.assertIn("Giữa kỳ", db.notifications.records[0]["title"])

    def test_question_repository_override_filter_uses_latest_review(self):
        overridden = ObjectId()

        class FakeQuestionsCollection:
            def __init__(self):
                self.pipelines = []

            def aggregate(self, pipeline):
                self.pipelines.append(pipeline)
                return [{"items": [], "count": [{"total": 0}]}]

        class FakeDatabase:
            questions = FakeQuestionsCollection()
            question_reviews = InMemoryCollection(
                [
                    {"_id": overridden, "override": {"applied": True}},
                    {"_id": ObjectId(), "override": {"applied": False}},
                ]
            )

        db = FakeDatabase()
        MongoQuestionRepository(db).list(1, 10, "APPROVED", None, override_only=True)
        self.assertEqual(db.questions.pipelines[0][0]["$match"]["latest_review_id"], {"$in": [overridden]})

        # Never-assigned questions have no review_assignment.status; "UNASSIGNED" must include them.
        MongoQuestionRepository(db).list(1, 10, "PENDING", None, assignment_status="UNASSIGNED")
        self.assertEqual(
            db.questions.pipelines[1][0]["$match"]["review_assignment.status"],
            {"$in": ["UNASSIGNED", None]},
        )

    def test_audit_writers_share_one_canonical_shape(self):
        from core import audit as audit_module

        actor_id = ObjectId()
        target_id = ObjectId()
        db = type("FakeAuditDb", (), {"audit_logs": InMemoryCollection()})()
        original_db = audit_module.get_rag_db
        try:
            audit_module.get_rag_db = lambda: db
            audit_module.record_audit_event(
                action="user.admin_update",
                entity_type="user",
                entity_id=str(target_id),
                actor_user_id=str(actor_id),
                actor_role="Admin",
                before={"role": "Reviewer", "is_active": True},
                after={"role": "Teacher", "is_active": True},
            )
        finally:
            audit_module.get_rag_db = original_db
        audit_module.write_audit_event(
            db,
            action="QUESTION_COMMENT_ADDED",
            entity_type="QUESTION",
            entity_id=target_id,
            actor_user_id=actor_id,
            actor_role="Reviewer",
            metadata={"comment_id": "c1"},
        )

        flat_style, workflow_style = db.audit_logs.records
        for event in (flat_style, workflow_style):
            self.assertEqual(
                set(event),
                {"schema_version", "action", "actor", "entity", "changes", "before", "after",
                 "before_hash", "after_hash", "metadata", "created_at"},
            )
            self.assertEqual(event["actor"]["user_id"], actor_id)
            self.assertEqual(event["entity"]["id"], target_id)
            self.assertNotIn("actor_user_id", event)
        self.assertEqual(flat_style["entity"]["type"], "user")
        self.assertEqual(workflow_style["entity"]["type"], "question")
        # Flat-style callers get a field-level change list derived from before/after.
        self.assertEqual(
            flat_style["changes"],
            [{"path": "role", "old_value": "Reviewer", "new_value": "Teacher"}],
        )
        self.assertEqual(flat_style["actor"]["role"], "Admin")

    def test_admin_can_set_reviewer_subjects(self):
        admin_doc = _user_doc("Admin", True)
        reviewer_doc = _user_doc("Reviewer", True)
        service = UserService(FakeUserRepository([admin_doc, reviewer_doc]), FakeIdentityGateway(), FakeSessions())
        actor = _current_user("Admin", admin_doc["_id"])
        subject_id = ObjectId()

        updated = service.update_admin(
            str(reviewer_doc["_id"]),
            UserAdminUpdateRequest(review_subject_ids=[str(subject_id), str(subject_id)]),
            actor,
        )
        self.assertEqual(updated["review_subject_ids"], [str(subject_id)])
        with self.assertRaises(ValueError):
            service.update_admin(
                str(reviewer_doc["_id"]),
                UserAdminUpdateRequest(review_subject_ids=["not-an-id"]),
                actor,
            )

    def test_user_service_releases_reviewer_work_on_deactivate_and_role_change(self):
        admin_doc = _user_doc("Admin", True)
        reviewer_doc = _user_doc("Reviewer", True)
        other_reviewer_doc = _user_doc("Reviewer", True)
        calls = []
        service = UserService(
            FakeUserRepository([admin_doc, reviewer_doc, other_reviewer_doc]),
            FakeIdentityGateway(),
            FakeSessions(),
            release_review_assignments=lambda user_id, actor, reason: calls.append((user_id, reason)) or 0,
        )
        actor = _current_user("Admin", admin_doc["_id"])

        service.deactivate(str(reviewer_doc["_id"]), actor)
        service.update_admin(
            str(other_reviewer_doc["_id"]),
            UserAdminUpdateRequest(role="Teacher"),
            actor,
        )

        self.assertEqual(
            calls,
            [
                (reviewer_doc["_id"], "reviewer_deactivated"),
                (other_reviewer_doc["_id"], "reviewer_role_removed"),
            ],
        )

    def test_removing_reviewer_subject_releases_held_questions_in_that_subject(self):
        admin_doc = _user_doc("Admin", True)
        kept, removed = ObjectId(), ObjectId()
        reviewer_doc = {**_user_doc("Reviewer", True), "review_subject_ids": [kept, removed]}
        calls = []
        service = UserService(
            FakeUserRepository([admin_doc, reviewer_doc]),
            FakeIdentityGateway(),
            FakeSessions(),
            release_review_assignments=lambda user_id, actor, reason, subject_ids=None: (
                calls.append((user_id, reason, subject_ids)) or 0
            ),
        )
        actor = _current_user("Admin", admin_doc["_id"])
        service.update_admin(
            str(reviewer_doc["_id"]), UserAdminUpdateRequest(review_subject_ids=[str(kept)]), actor,
        )
        self.assertEqual(calls, [(reviewer_doc["_id"], "reviewer_subject_removed", [str(removed)])])

    def test_release_assignments_for_reviewer_respects_subject_filter(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        db, question_id, _ = self._review_guard_fixture(author=teacher, users=[teacher, reviewer])

        def scenario(service):
            service.claim_review(str(question_id), reviewer)
            untouched = service.release_assignments_for_reviewer(
                reviewer.id, None, "reviewer_subject_removed", subject_ids=[str(ObjectId())],
            )
            subject_id = db.questions.find_one({"_id": question_id})["subject_id"]
            released = service.release_assignments_for_reviewer(
                reviewer.id, None, "reviewer_subject_removed", subject_ids=[str(subject_id)],
            )
            return untouched, released

        untouched, released = self._with_workflow_service(db, scenario)
        self.assertEqual((untouched, released), (0, 1))
        self.assertEqual(db.questions.find_one({"_id": question_id})["review_assignment"]["status"], "UNASSIGNED")

    def test_user_service_cancels_generation_jobs_when_user_is_deactivated(self):
        admin_doc = _user_doc("Admin", True)
        teacher_doc = _user_doc("Teacher", True)
        other_teacher_doc = _user_doc("Teacher", True)
        cancelled = []
        service = UserService(
            FakeUserRepository([admin_doc, teacher_doc, other_teacher_doc]),
            FakeIdentityGateway(),
            FakeSessions(),
            cancel_generation_jobs=lambda user_id: cancelled.append(user_id) or 2,
        )
        actor = _current_user("Admin", admin_doc["_id"])

        service.deactivate(str(teacher_doc["_id"]), actor)
        service.update_admin(str(other_teacher_doc["_id"]), UserAdminUpdateRequest(is_active=False), actor)
        service.update_admin(str(admin_doc["_id"]), UserAdminUpdateRequest(display_name="Admin mới"), actor)

        self.assertEqual(cancelled, [teacher_doc["_id"], other_teacher_doc["_id"]])

    def test_role_change_and_deactivation_write_audit_with_the_update(self):
        admin_doc = _user_doc("Admin", True)
        teacher_doc = _user_doc("Teacher", True)
        repository = FakeUserRepository([admin_doc, teacher_doc])
        service = UserService(repository, FakeIdentityGateway(), FakeSessions())
        actor = _current_user("Admin", admin_doc["_id"])

        service.update_admin(str(teacher_doc["_id"]), UserAdminUpdateRequest(role="Reviewer"), actor)
        service.deactivate(str(teacher_doc["_id"]), actor)

        self.assertEqual([event["action"] for event in repository.audit_events],
                         ["user.admin_update", "user.deactivate"])
        self.assertEqual(repository.audit_events[0]["after"]["role"], "Reviewer")

    def test_failed_required_audit_aborts_deactivation_and_restores_firebase(self):
        admin_doc = _user_doc("Admin", True)
        teacher_doc = _user_doc("Teacher", True)
        repository = FakeUserRepository([admin_doc, teacher_doc])
        repository.fail_audit = True
        identity = FakeIdentityGateway()
        cancelled = []
        service = UserService(repository, identity, FakeSessions(),
                              cancel_generation_jobs=lambda user_id: cancelled.append(user_id) or 0)

        with self.assertRaises(RuntimeError):
            service.deactivate(str(teacher_doc["_id"]), _current_user("Admin", admin_doc["_id"]))

        self.assertTrue(repository.find_by_id(teacher_doc["_id"])["is_active"])
        self.assertEqual(identity.disabled_calls[-1], (teacher_doc["firebase_uid"], False))
        self.assertEqual(cancelled, [])

    def test_exam_finalize_and_delete_carry_required_audit(self):
        subject_id = ObjectId()
        owner = _current_user("Teacher", ObjectId())
        exam = {
            "_id": ObjectId(), "name": "Đề 1", "exam_title": "Đề 1", "subject_id": subject_id,
            "question_count": 0, "header": {}, "matrix": [], "questions": [], "status": "READY",
            "created_by_user_id": owner.id, "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        repository = FakeExamRepository([exam])
        service = ExamService(repository, FakeExamQuestionRepository())
        service._validate_ready_payload = lambda _exam, _actor=None: None

        service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="FINALIZED"), owner)

        self.assertEqual(repository.audit_events[-1]["action"], "exam.finalized")
        self.assertEqual(repository.audit_events[-1]["after"], {"status": "FINALIZED"})

        draft = {**exam, "_id": ObjectId(), "status": "DRAFT"}
        repository.create(draft)
        service.delete_exam(str(draft["_id"]), owner)
        self.assertEqual(repository.audit_events[-1]["action"], "exam.deleted")

    def test_auto_evaluation_requires_expected_version(self):
        with self.assertRaises(ValidationError):
            AutoEvaluationRequest()

        payload = AutoEvaluationRequest(expected_version=1)
        self.assertEqual(payload.evaluator_model_code, settings.evaluation_model_provider)
        self.assertFalse(payload.fallback_to_heuristic)

    def test_ai_reject_guardrail_cannot_be_recorded_as_passed(self):
        question_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-GUARDRAIL",
            "current_version": 1,
            "current_version_id": version_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PROCESSING",
            "review_status": "PENDING",
            "quality_summary": {},
            "created_at": now,
            "updated_at": now,
        }
        version = {
            "_id": version_id,
            "question_id": question_id,
            "version": 1,
            "content_hash": "guardrail-hash",
            "generation_run_id": None,
        }
        db = FakeCatalogDatabase(questions=[question], question_versions=[version])
        db.question_evaluations = InMemoryCollection([])
        db.audit_logs = InMemoryCollection([])
        original_transaction = question_workflow_module.mongo_transaction
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            evaluation = QuestionWorkflowService(db).evaluate(
                str(question_id),
                EvaluationCreateRequest(
                    expected_version=1,
                    scores=EvaluationScores(
                        faithfulness=0.9,
                        contextual_relevancy=0.9,
                        answer_relevancy=0.9,
                        bloom_alignment=0.9,
                        clo_alignment=0.9,
                    ),
                    feedback={"action": "REJECT", "severity": "HIGH"},
                    model_snapshot={"model_code": "test", "model_name": "Test"},
                    policy_snapshot={
                        "version": 1,
                        "weights": {
                            "faithfulness": 0.35,
                            "contextual_relevancy": 0.20,
                            "answer_relevancy": 0.15,
                            "bloom_alignment": 0.15,
                            "clo_alignment": 0.15,
                        },
                        "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
                    },
                ),
                ObjectId(),
            )
        finally:
            question_workflow_module.mongo_transaction = original_transaction

        self.assertFalse(evaluation["passed"])
        self.assertEqual(evaluation["color"], "RED")
        self.assertTrue(evaluation["evidence"]["decision_guardrail"]["blocked_pass"])
        self.assertEqual(db.questions.find_one({"_id": question_id})["evaluation_status"], "FAILED")

    def test_evaluation_without_clo_leaves_clo_out_of_overall_score(self):
        question_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-NO-CLO",
            "current_version": 1,
            "current_version_id": version_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PROCESSING",
            "review_status": "PENDING",
            "quality_summary": {},
            "created_at": now,
            "updated_at": now,
        }
        version = {
            "_id": version_id,
            "question_id": question_id,
            "version": 1,
            "content_hash": "no-clo-hash",
            "generation_run_id": None,
        }
        policy = {
            "version": 1,
            "weights": {
                "faithfulness": 0.35,
                "contextual_relevancy": 0.20,
                "answer_relevancy": 0.15,
                "bloom_alignment": 0.15,
                "clo_alignment": 0.15,
            },
            "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
        }
        scores = EvaluationScores(
            faithfulness=0.7,
            contextual_relevancy=0.7,
            answer_relevancy=0.7,
            bloom_alignment=0.7,
            clo_alignment=0.0,
        )

        def evaluate(evidence):
            db = FakeCatalogDatabase(questions=[dict(question)], question_versions=[version])
            db.question_evaluations = InMemoryCollection([])
            db.audit_logs = InMemoryCollection([])
            original_transaction = question_workflow_module.mongo_transaction
            try:
                question_workflow_module.mongo_transaction = lambda: nullcontext(None)
                return QuestionWorkflowService(db).evaluate(
                    str(question_id),
                    EvaluationCreateRequest(
                        expected_version=1,
                        scores=scores,
                        feedback={"action": "APPROVE", "severity": "LOW"},
                        evidence=evidence,
                        model_snapshot={"model_code": "test", "model_name": "Test"},
                        policy_snapshot=policy,
                    ),
                    ObjectId(),
                )
            finally:
                question_workflow_module.mongo_transaction = original_transaction

        without_clo = evaluate({"metadata_guardrail": {"not_applicable": ["clo_alignment"]}})
        self.assertTrue(without_clo["passed"])
        self.assertAlmostEqual(without_clo["scores"]["overall"], 0.7)
        self.assertEqual(without_clo["policy"]["not_applicable"], ["clo_alignment"])
        self.assertEqual(without_clo["policy"]["effective_weights"]["clo_alignment"], 0.0)
        self.assertAlmostEqual(sum(without_clo["policy"]["effective_weights"].values()), 1.0)

        # Câu có gắn CLO mà CLO không đạt thì tiêu chí vẫn được tính như cũ.
        with_clo = evaluate({})
        self.assertFalse(with_clo["passed"])
        self.assertAlmostEqual(with_clo["scores"]["overall"], 0.595)
        self.assertNotIn("effective_weights", with_clo["policy"])

    def test_heuristic_evaluation_is_never_recorded_as_passed(self):
        question_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id,
            "schema_version": SCHEMA_VERSION,
            "question_code": "Q-HEURISTIC",
            "current_version": 1,
            "current_version_id": version_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PROCESSING",
            "review_status": "PENDING",
            "quality_summary": {},
            "created_at": now,
            "updated_at": now,
        }
        version = {
            "_id": version_id,
            "question_id": question_id,
            "version": 1,
            "content_hash": "heuristic-hash",
            "generation_run_id": None,
        }
        db = FakeCatalogDatabase(questions=[question], question_versions=[version])
        db.question_evaluations = InMemoryCollection([])
        db.audit_logs = InMemoryCollection([])
        original_transaction = question_workflow_module.mongo_transaction
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            evaluation = QuestionWorkflowService(db).evaluate(
                str(question_id),
                EvaluationCreateRequest(
                    expected_version=1,
                    scores=EvaluationScores(
                        faithfulness=0.95,
                        contextual_relevancy=0.95,
                        answer_relevancy=0.95,
                        bloom_alignment=0.95,
                        clo_alignment=0.95,
                    ),
                    # Even a stale heuristic payload without an action must not pass.
                    feedback={"summary": "Heuristic"},
                    evidence={"mode": "heuristic_fallback"},
                    model_snapshot={"model_code": "test", "model_name": "Test"},
                    policy_snapshot={
                        "version": 1,
                        "weights": {
                            "faithfulness": 0.35,
                            "contextual_relevancy": 0.20,
                            "answer_relevancy": 0.15,
                            "bloom_alignment": 0.15,
                            "clo_alignment": 0.15,
                        },
                        "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
                    },
                ),
                ObjectId(),
            )
        finally:
            question_workflow_module.mongo_transaction = original_transaction

        self.assertFalse(evaluation["passed"])
        self.assertTrue(evaluation["evidence"]["decision_guardrail"]["heuristic_only"])
        self.assertEqual(db.questions.find_one({"_id": question_id})["evaluation_status"], "FAILED")

    def test_heuristic_scores_always_ask_for_human_review(self):
        version = {
            "content": "Hàng đợi hoạt động theo nguyên tắc _____.",
            "classification": {"assessment_type": "DIEN_KHUYET", "bloom": {"level": 1}},
            "question_data": {"correct_answer": "FIFO", "explanation": "Hàng đợi là FIFO."},
            "clos": [{"code": "CLO1", "description": "Nêu nguyên tắc của hàng đợi."}],
            "sources": [{"citation_order": 1, "context_excerpt": "Hàng đợi theo nguyên tắc FIFO."}],
        }

        _, feedback, _ = QuestionWorkflowService(None)._auto_scores({}, version)

        self.assertEqual(feedback["action"], "NEEDS_REVISION")
        self.assertEqual(feedback["severity"], "MEDIUM")

    def test_ai_parser_normalizes_root_decision_and_known_score_typo(self):
        raw = json.dumps(
            {
                "scores": {
                    "faithfulness": 0.8,
                    "contextual_relevancy": 0.7,
                    "answer_relevality": 0.6,
                    "bloom_alignment": 0.9,
                    "clo_alignment": 0.5,
                },
                "feedback": {"summary": "Cần chỉnh nhẹ"},
                "evidence": {
                    "reasoning": "Đáp án cần được đối chiếu thêm.",
                    "moodle_readiness": "needs_fix",
                },
                "action": "needs_revision",
                "severity": "medium",
            }
        )

        scores, feedback, evidence = QuestionWorkflowService._parse_llm_evaluation(raw)

        self.assertEqual(scores.answer_relevancy, 0.6)
        self.assertEqual(feedback["action"], "NEEDS_REVISION")
        self.assertEqual(feedback["severity"], "MEDIUM")
        self.assertEqual(evidence["moodle_readiness"], "NEEDS_FIX")

    def test_ai_parser_normalizes_option_level_evidence(self):
        raw = json.dumps(
            {
                "scores": {
                    "faithfulness": 0.8,
                    "contextual_relevancy": 0.8,
                    "answer_relevancy": 0.8,
                    "bloom_alignment": 0.8,
                    "clo_alignment": 0.8,
                },
                "feedback": {
                    "summary": "Đã kiểm tra từng phương án",
                    "action": "APPROVE",
                    "severity": "LOW",
                },
                "evidence": {
                    "reasoning": "Đáp án có căn cứ.",
                    "moodle_readiness": "READY",
                    "question_polarity": "negative",
                    "option_checks": [
                        {
                            "key": "a",
                            "verdict": "supported",
                            "source_label": "s1",
                            "supporting_excerpt": "Nguồn xác nhận A",
                        }
                    ],
                },
            }
        )

        _, _, evidence = QuestionWorkflowService._parse_llm_evaluation(raw)

        self.assertEqual(evidence["question_polarity"], "NEGATIVE")
        self.assertEqual(evidence["option_checks"][0]["key"], "A")
        self.assertEqual(evidence["option_checks"][0]["verdict"], "SUPPORTED")
        self.assertEqual(evidence["option_checks"][0]["source_label"], "S1")

    def test_answer_guardrail_blocks_negative_question_when_all_options_are_supported(self):
        scores = EvaluationScores(
            faithfulness=0.85,
            contextual_relevancy=0.90,
            answer_relevancy=0.95,
            bloom_alignment=0.85,
            clo_alignment=0.85,
        )
        feedback = {
            "summary": "Câu hỏi rõ ràng.",
            "missing": [],
            "action": "APPROVE",
            "severity": "LOW",
        }
        evidence = {
            "moodle_readiness": "READY",
            "risks": [],
            "option_checks": [
                {
                    "key": key,
                    "verdict": "SUPPORTED",
                    "source_label": "S1",
                    "supporting_excerpt": text,
                }
                for key, text in {
                    "A": "Tính kết thúc",
                    "B": "Tính xác định",
                    "C": "Tính phổ dụng",
                    "D": "Tính hiệu quả",
                }.items()
            ],
        }
        version = {
            "content": "Tính chất nào sau đây không phải là đặc trưng của giải thuật?",
            "classification": {"assessment_type": "TRAC_NGHIEM"},
            "question_data": {
                "options": {
                    "A": "Tính kết thúc",
                    "B": "Tính xác định",
                    "C": "Tính phổ dụng",
                    "D": "Tính hiệu quả",
                },
                "correct_answer": "D",
            },
            "sources": [
                {
                    "citation_order": 1,
                    "context_excerpt": (
                        "Các đặc trưng của giải thuật gồm tính kết thúc, tính xác định, "
                        "tính phổ dụng và tính hiệu quả."
                    ),
                }
            ],
        }

        guarded_scores, guarded_feedback, guarded_evidence = (
            QuestionWorkflowService._apply_answer_guardrail(
                scores,
                feedback,
                evidence,
                version,
            )
        )

        self.assertEqual(guarded_feedback["action"], "NEEDS_REVISION")
        self.assertEqual(guarded_feedback["severity"], "HIGH")
        self.assertLessEqual(guarded_scores.faithfulness, 0.35)
        self.assertLessEqual(guarded_scores.answer_relevancy, 0.30)
        self.assertEqual(guarded_evidence["moodle_readiness"], "NEEDS_FIX")
        self.assertTrue(guarded_evidence["answer_guardrail"]["applied"])
        self.assertIn(
            "Câu phủ định phải có đúng một phương án",
            " ".join(guarded_evidence["answer_guardrail"]["issues"]),
        )

    def test_negative_question_detection_does_not_misread_domain_terms(self):
        self.assertFalse(
            QuestionWorkflowService._is_negative_selection_question(
                "TRAC_NGHIEM",
                "Lặp xác định và lặp không xác định được phân biệt dựa trên điều kiện nào?",
            )
        )
        self.assertTrue(
            QuestionWorkflowService._is_negative_selection_question(
                "TRAC_NGHIEM",
                "Tính chất nào sau đây không phải là đặc trưng của giải thuật?",
            )
        )
        self.assertTrue(
            QuestionWorkflowService._is_negative_selection_question(
                "TRAC_NGHIEM",
                "Phương án nào không thuộc nhóm thao tác của stack?",
            )
        )
        self.assertFalse(
            QuestionWorkflowService._is_negative_selection_question(
                "DUNG_SAI",
                "Stack không tuân theo nguyên tắc FIFO.",
            )
        )

    def test_answer_guardrail_allows_one_grounded_positive_answer(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        evidence = {
            "moodle_readiness": "READY",
            "option_checks": [
                {"key": "A", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack là LIFO"},
                {"key": "B", "verdict": "CONTRADICTED", "supporting_excerpt": "Stack không FIFO"},
            ],
        }
        version = {
            "content": "Stack tuân theo nguyên tắc nào?",
            "classification": {"assessment_type": "TRAC_NGHIEM"},
            "question_data": {
                "options": {"A": "LIFO", "B": "FIFO"},
                "correct_answer": "A",
            },
            "sources": [{"citation_order": 1, "context_excerpt": "Stack tuân theo LIFO."}],
        }

        guarded_scores, guarded_feedback, guarded_evidence = (
            QuestionWorkflowService._apply_answer_guardrail(
                scores,
                feedback,
                evidence,
                version,
            )
        )

        self.assertEqual(guarded_scores, scores)
        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])

    def test_answer_guardrail_allows_relation_distractors_that_are_named_in_source(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        source = (
            "Tính kết thúc yêu cầu giải thuật dừng sau hữu hạn bước. "
            "Tính xác định yêu cầu các thao tác cho cùng kết quả. "
            "Tính phổ dụng áp dụng cho một loạt bài toán. "
            "Tính hiệu quả yêu cầu ít thời gian và tài nguyên."
        )
        evidence = {
            "moodle_readiness": "READY",
            "question_polarity": "POSITIVE",
            "option_checks": [
                {"key": "A", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Tính kết thúc yêu cầu giải thuật dừng sau hữu hạn bước"},
                {"key": "B", "verdict": "CONTRADICTED", "source_label": "S1", "supporting_excerpt": "Tính xác định yêu cầu các thao tác cho cùng kết quả"},
                {"key": "C", "verdict": "CONTRADICTED", "source_label": "S1", "supporting_excerpt": "Tính phổ dụng áp dụng cho một loạt bài toán"},
                {"key": "D", "verdict": "CONTRADICTED", "source_label": "S1", "supporting_excerpt": "Tính hiệu quả yêu cầu ít thời gian và tài nguyên"},
            ],
        }
        version = {
            "content": "Đặc trưng nào yêu cầu giải thuật dừng sau hữu hạn bước?",
            "classification": {"assessment_type": "TRAC_NGHIEM"},
            "question_data": {
                "options": {
                    "A": "Tính kết thúc",
                    "B": "Tính xác định",
                    "C": "Tính phổ dụng",
                    "D": "Tính hiệu quả",
                },
                "correct_answer": "A",
            },
            "sources": [{"citation_order": 1, "context_excerpt": source}],
        }

        guarded_scores, guarded_feedback, guarded_evidence = (
            QuestionWorkflowService._apply_answer_guardrail(scores, feedback, evidence, version)
        )

        self.assertEqual(guarded_scores, scores)
        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])

    def test_answer_guardrail_allows_grounded_multiple_answers(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        evidence = {
            "moodle_readiness": "READY",
            "question_polarity": "POSITIVE",
            "option_checks": [
                {"key": "A", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack hỗ trợ push"},
                {"key": "B", "verdict": "NOT_IN_SOURCE", "supporting_excerpt": ""},
                {"key": "C", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack hỗ trợ pop"},
            ],
        }
        version = {
            "content": "Stack hỗ trợ những thao tác nào?",
            "classification": {"assessment_type": "NHIEU_LUA_CHON"},
            "question_data": {
                "options": {"A": "push", "B": "enqueue", "C": "pop"},
                "correct_answer": "A,C",
            },
            "sources": [{"citation_order": 1, "context_excerpt": "Stack hỗ trợ push và pop."}],
        }

        _, guarded_feedback, guarded_evidence = QuestionWorkflowService._apply_answer_guardrail(
            scores, feedback, evidence, version
        )

        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertEqual(guarded_evidence["answer_guardrail"]["answer_mode"], "MULTIPLE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])

    def test_answer_guardrail_blocks_multiple_answer_mismatch(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        evidence = {
            "moodle_readiness": "READY",
            "option_checks": [
                {"key": "A", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack hỗ trợ push"},
                {"key": "B", "verdict": "NOT_IN_SOURCE", "supporting_excerpt": ""},
                {"key": "C", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack hỗ trợ pop"},
            ],
        }
        version = {
            "content": "Stack hỗ trợ những thao tác nào?",
            "classification": {"assessment_type": "NHIEU_LUA_CHON"},
            "question_data": {
                "options": {"A": "push", "B": "enqueue", "C": "pop"},
                "correct_answer": "A",
            },
            "sources": [{"citation_order": 1, "context_excerpt": "Stack hỗ trợ push và pop."}],
        }

        _, guarded_feedback, guarded_evidence = QuestionWorkflowService._apply_answer_guardrail(
            scores, feedback, evidence, version
        )

        self.assertEqual(guarded_feedback["action"], "NEEDS_REVISION")
        self.assertIn(
            "Tập đáp án khai báo không khớp",
            " ".join(guarded_evidence["answer_guardrail"]["issues"]),
        )

    def test_answer_guardrail_allows_grounded_true_false_answer(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        evidence = {
            "moodle_readiness": "READY",
            "option_checks": [
                {"key": "A", "verdict": "SUPPORTED", "source_label": "S1", "supporting_excerpt": "Stack tuân theo LIFO"},
                {"key": "B", "verdict": "CONTRADICTED", "supporting_excerpt": "Stack không phải FIFO"},
            ],
        }
        version = {
            "content": "Stack tuân theo LIFO là đúng hay sai?",
            "classification": {"assessment_type": "DUNG_SAI"},
            "question_data": {
                "options": {"A": "Đúng", "B": "Sai"},
                "correct_answer": "Đúng",
            },
            "sources": [{"citation_order": 1, "context_excerpt": "Stack tuân theo LIFO."}],
        }

        _, guarded_feedback, guarded_evidence = QuestionWorkflowService._apply_answer_guardrail(
            scores, feedback, evidence, version
        )

        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])

    def test_answer_guardrail_infers_missing_complementary_true_false_check(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        evidence = {
            "moodle_readiness": "READY",
            "question_polarity": "POSITIVE",
            "option_checks": [
                {
                    "key": "A",
                    "verdict": "SUPPORTED",
                    "source_label": "S1",
                    "supporting_excerpt": "Giải thuật tốt thực hiện nhanh và ít tài nguyên",
                }
            ],
        }
        version = {
            "content": "Giải thuật tốt thực hiện nhanh và dùng ít tài nguyên.",
            "classification": {"assessment_type": "DUNG_SAI"},
            "question_data": {
                "options": {"A": "Đúng", "B": "Sai"},
                "correct_answer": "A",
            },
            "sources": [
                {
                    "citation_order": 1,
                    "context_excerpt": "Giải thuật tốt thực hiện nhanh và ít tài nguyên.",
                }
            ],
        }

        _, guarded_feedback, guarded_evidence = QuestionWorkflowService._apply_answer_guardrail(
            scores, feedback, evidence, version
        )

        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])
        inferred = guarded_evidence["option_checks"][1]
        self.assertEqual(inferred["key"], "B")
        self.assertEqual(inferred["verdict"], "CONTRADICTED")
        self.assertEqual(inferred["inferred_from_complement"], "A")

    def test_answer_guardrail_skips_non_option_question_types(self):
        scores = EvaluationScores(
            faithfulness=0.9,
            contextual_relevancy=0.9,
            answer_relevancy=0.9,
            bloom_alignment=0.8,
            clo_alignment=0.8,
        )
        feedback = {"summary": "Đạt", "missing": [], "action": "APPROVE", "severity": "LOW"}
        version = {
            "content": "Sắp xếp các thao tác.",
            "classification": {"assessment_type": "SAP_XEP"},
            "question_data": {
                "options": ["Bước một", "Bước hai"],
                "correct_answer": "Bước một, Bước hai",
            },
        }

        guarded_scores, guarded_feedback, guarded_evidence = QuestionWorkflowService._apply_answer_guardrail(
            scores, feedback, {"moodle_readiness": "READY"}, version
        )

        self.assertEqual(guarded_scores, scores)
        self.assertEqual(guarded_feedback["action"], "APPROVE")
        self.assertFalse(guarded_evidence["answer_guardrail"]["applied"])
        self.assertEqual(
            guarded_evidence["answer_guardrail"]["reason"],
            "QUESTION_TYPE_NOT_OPTION_BASED",
        )

    def test_evaluation_guardrail_golden_cases(self):
        cases = json.loads(
            (Path(__file__).parent / "fixtures" / "evaluation_guardrail_golden.json").read_text(
                encoding="utf-8"
            )
        )
        for case in cases:
            with self.subTest(case=case["name"]):
                scores = EvaluationScores(
                    faithfulness=0.9,
                    contextual_relevancy=0.9,
                    answer_relevancy=0.9,
                    bloom_alignment=0.9,
                    clo_alignment=0.9,
                )
                feedback = {
                    "summary": "Đạt",
                    "missing": [],
                    "action": "APPROVE",
                    "severity": "LOW",
                }
                answer_scores, answer_feedback, answer_evidence = (
                    QuestionWorkflowService._apply_answer_guardrail(
                        scores,
                        feedback,
                        case["evidence"],
                        case["version"],
                    )
                )
                _, _, final_evidence = QuestionWorkflowService._apply_metadata_guardrail(
                    answer_scores,
                    answer_feedback,
                    answer_evidence,
                    case["version"],
                )

                self.assertEqual(
                    final_evidence["answer_guardrail"]["applied"],
                    case["expect_answer_block"],
                )
                self.assertEqual(
                    final_evidence["metadata_guardrail"]["applied"],
                    case["expect_metadata_block"],
                )
                self.assertEqual(
                    final_evidence["metadata_guardrail"]["not_applicable"],
                    case.get("expect_not_applicable", []),
                )

    def test_answer_guardrail_blocks_mcq_when_fallback_has_no_option_checks(self):
        service = QuestionWorkflowService(None)
        version = {
            "content": "Stack tuân theo nguyên tắc nào?",
            "classification": {"assessment_type": "TRAC_NGHIEM", "bloom": {"level": 2}},
            "question_data": {
                "options": {"A": "LIFO", "B": "FIFO"},
                "correct_answer": "A",
                "explanation": "Stack theo LIFO.",
            },
            "clos": [],
            "sources": [{"citation_order": 1, "context_excerpt": "Stack tuân theo LIFO."}],
        }
        scores, feedback, evidence = service._auto_scores({}, version)

        guarded_scores, guarded_feedback, guarded_evidence = service._apply_answer_guardrail(
            scores,
            feedback,
            evidence,
            version,
        )

        self.assertEqual(guarded_feedback["action"], "NEEDS_REVISION")
        self.assertEqual(guarded_feedback["severity"], "HIGH")
        self.assertLessEqual(guarded_scores.answer_relevancy, 0.30)
        self.assertIn(
            "AI chưa kiểm tra phương án",
            guarded_evidence["answer_guardrail"]["issues"][0],
        )

    def test_ai_parser_rejects_missing_required_score(self):
        raw = json.dumps(
            {
                "scores": {
                    "faithfulness": 0.8,
                    "contextual_relevancy": 0.7,
                    "bloom_alignment": 0.9,
                    "clo_alignment": 0.5,
                },
                "feedback": {"action": "APPROVE", "severity": "LOW"},
                "evidence": {},
            }
        )

        with self.assertRaisesRegex(ValueError, "answer_relevancy"):
            QuestionWorkflowService._parse_llm_evaluation(raw)

    def test_ai_consistency_rejects_approve_below_pass_threshold(self):
        scores = EvaluationScores(
            faithfulness=0.5,
            contextual_relevancy=0.5,
            answer_relevancy=0.5,
            bloom_alignment=0.5,
            clo_alignment=0.5,
        )

        with self.assertRaisesRegex(ValueError, "APPROVE.*dưới ngưỡng"):
            QuestionWorkflowService._validate_llm_evaluation_consistency(
                scores,
                {"action": "APPROVE", "severity": "LOW"},
                {"moodle_readiness": "READY"},
                {
                    "weights": {
                        "faithfulness": 0.35,
                        "contextual_relevancy": 0.20,
                        "answer_relevancy": 0.15,
                        "bloom_alignment": 0.15,
                        "clo_alignment": 0.15,
                    },
                    "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
                },
            )

    def test_ai_consistency_flags_uniform_scores_without_rejecting(self):
        scores = EvaluationScores(
            faithfulness=0.85,
            contextual_relevancy=0.85,
            answer_relevancy=0.85,
            bloom_alignment=0.85,
            clo_alignment=0.85,
        )

        result = QuestionWorkflowService._validate_llm_evaluation_consistency(
            scores,
            {"action": "APPROVE", "severity": "LOW"},
            {"moodle_readiness": "READY"},
            {
                "weights": {
                    "faithfulness": 0.35,
                    "contextual_relevancy": 0.20,
                    "answer_relevancy": 0.15,
                    "bloom_alignment": 0.15,
                    "clo_alignment": 0.15,
                },
                "thresholds": {"yellow_min": 0.5, "green_min": 0.75, "pass_min": 0.65},
            },
        )

        self.assertTrue(result["uniform_scores"])
        self.assertEqual(result["score_spread"], 0)
        self.assertEqual(result["weak_criteria"], [])

    def test_review_rejects_question_outside_pending_state(self):
        service = QuestionWorkflowService(None)
        service._pair = lambda _question_id: (
            {
                "current_version": 1,
                "review_status": "DRAFT",
                "evaluation_status": "PASSED",
            },
            {"version": 1},
        )

        with self.assertRaisesRegex(ValueError, "đang chờ duyệt"):
            service.review(
                str(ObjectId()),
                ReviewCreateRequest(expected_version=1, decision="APPROVED"),
                _current_user("Admin"),
            )

    def test_manual_question_with_document_requires_verified_excerpt_and_starts_draft(self):
        owner = _current_user("Teacher")
        document_id = ObjectId()

        class References:
            def find_document(self, _document_id):
                return {
                    "_id": document_id,
                    "schema_version": SCHEMA_VERSION,
                    "uploaded_by_user_id": owner.id,
                    "current_ocr_job_id": None,
                    "archived_at": None,
                }

            def find_chunk(self, _chunk_id):
                return None

            def find_subject(self, _subject_id):
                return None

            def document_contains_text(self, _document_id, _ocr_job_id, text):
                return text == "Queue tuân theo nguyên tắc FIFO."

        class Repository:
            def create(self, aggregate, version):
                self.aggregate = aggregate
                self.version = version
                return aggregate, version

        repository = Repository()
        created = QuestionService(repository, References()).create(
            QuestionCreateRequest(
                content="Queue tuân theo nguyên tắc nào?",
                document_id=str(document_id),
                source_context="Queue tuân theo nguyên tắc FIFO.",
                question_data={"correct_answer": "FIFO"},
            ),
            owner.id,
            current_user=owner,
        )

        self.assertEqual(created["review_status"], "DRAFT")
        self.assertEqual(created["sources"][0]["source_type"], "MANUAL_EXCERPT")
        self.assertEqual(
            created["question_data"]["model_source_context"],
            "Queue tuân theo nguyên tắc FIFO.",
        )

    def test_evaluation_prompt_includes_persisted_generation_context(self):
        question_id = ObjectId()
        version_id = ObjectId()
        generation_context = "Queue xử lý phần tử theo nguyên tắc vào trước ra trước (FIFO)."
        question = {"_id": question_id, "question_code": "Q-CONTEXT"}
        version = {
            "_id": version_id,
            "question_id": question_id,
            "document_id": ObjectId(),
            "classification": {
                "assessment_type": "TRAC_NGHIEM",
                "bloom": {"level": 2},
                "difficulty": "de",
            },
            "content": "Queue xử lý phần tử theo nguyên tắc nào?",
            "question_data": {
                "options": {"A": "FIFO", "B": "LIFO"},
                "correct_answer": "A",
                "explanation": "Queue tuân theo FIFO.",
                "model_source_context": generation_context,
            },
            "clos": [],
            "sources": [],
        }
        service = QuestionWorkflowService(None)

        prompt, _snapshot, _sources = service._build_evaluation_prompt(
            question,
            version,
            service._policy(),
        )
        heuristic_context = service._source_context(version)

        self.assertIn(generation_context, prompt)
        self.assertIn("generation_source_context", prompt)
        self.assertIn(f"[MODEL_CONTEXT] {generation_context}", heuristic_context)

    def test_llm_factory_accepts_ollama_model_alias(self):
        provider = get_llm_service("ollama:qwen3:8b")
        self.assertIsInstance(provider, ConcurrencyLimitedProvider)
        self.assertIsInstance(provider.wrapped, OllamaProvider)
        self.assertEqual(provider.model_name, "qwen3:8b")

    def test_moodle_publication_request_has_demo_defaults(self):
        payload = MoodlePublicationRequest(expected_version=1)
        self.assertEqual(payload.moodle_site_id, "demo-moodle")
        self.assertIsNone(payload.target_id)
        self.assertTrue(payload.mock)

    def test_moodle_publication_item_marks_mock_record(self):
        record = {
            "_id": ObjectId(),
            "question_id": ObjectId(),
            "question_version_id": ObjectId(),
            "question_version": 2,
            "publisher_user_id": ObjectId(),
            "target": {
                "moodle_site_id": "demo-moodle",
                "site_name": "Demo Moodle",
                "mode": "REST_API",
                "configured_mode": "REST_API",
            },
            "status": "PUBLISHED",
            "moodle_question_ref_id": "mock-demo-version",
            "request_payload": {
                "question_code": "Q-001",
                "export_format": "BOTH",
                "mock": True,
            },
            "response_payload": {
                "message": "Mô phỏng Moodle: chỉ ghi nhận publication cục bộ, chưa gửi dữ liệu sang Moodle thật.",
                "export_formats": ["gift", "xml"],
            },
        }

        item = _safe_publication_item(record)

        self.assertEqual(item["publication_mode"], "MOCK")
        self.assertEqual(item["configured_mode"], "REST_API")
        self.assertFalse(item["external_sync"])
        self.assertEqual(item["status_detail"], "SIMULATED_LOCAL_RECORD")
        self.assertEqual(item["status_label"], "Đã ghi mô phỏng")
        self.assertIn("chưa gửi", item["message"])

    def test_moodle_publish_rejects_real_sync_until_configured(self):
        service = QuestionWorkflowService(object())
        payload = MoodlePublicationRequest(expected_version=1, mock=False)

        with self.assertRaises(ValueError) as ctx:
            service.publish_to_moodle(str(ObjectId()), payload, ObjectId())

        self.assertIn("chưa được cấu hình", str(ctx.exception))

    def test_moodle_mock_publish_is_disabled_in_production_non_demo(self):
        original_app_env = settings.app_env
        original_demo_mode = settings.demo_mode
        try:
            settings.app_env = "production"
            settings.demo_mode = False
            service = QuestionWorkflowService(object())
            payload = MoodlePublicationRequest(expected_version=1, mock=True)

            with self.assertRaises(ValueError) as ctx:
                service.publish_to_moodle(str(ObjectId()), payload, ObjectId())
        finally:
            settings.app_env = original_app_env
            settings.demo_mode = original_demo_mode

        self.assertIn("Mô phỏng Moodle bị tắt trong production", str(ctx.exception))

    def _moodle_retry_database(self, *, current_version=1, failed_version=1):
        question_id = ObjectId()
        failed_version_id = ObjectId()
        current_version_id = failed_version_id if current_version == failed_version else ObjectId()
        target_id = ObjectId()
        failed_id = ObjectId()
        now = datetime.now(timezone.utc)
        moodle_site_id = "demo-moodle"
        course_id = "ctdl"
        category_id = "qbank"
        failed_hash = "hash-v1"
        idempotency_key = hashlib.sha256(
            "|".join(
                [
                    moodle_site_id,
                    course_id,
                    category_id,
                    str(failed_version_id),
                    failed_hash,
                ]
            ).encode("utf-8")
        ).hexdigest()
        versions = [
            {
                "_id": failed_version_id,
                "question_id": question_id,
                "version": failed_version,
                "document_id": None,
                "content": "Queue xử lý phần tử theo nguyên tắc nào?",
                "question_data": {
                    "options": {"A": "FIFO", "B": "LIFO"},
                    "correct_answer": "A",
                    "explanation": "Queue là hàng đợi FIFO.",
                },
                "classification": {"assessment_type": "TRAC_NGHIEM"},
                "clos": [],
                "sources": [],
                "content_hash": failed_hash,
            }
        ]
        if current_version_id != failed_version_id:
            versions.append(
                {
                    **versions[0],
                    "_id": current_version_id,
                    "version": current_version,
                    "content_hash": "hash-current",
                }
            )

        class FakeRetryDatabase:
            def __init__(self):
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "schema_version": SCHEMA_VERSION,
                            "question_code": "Q-001",
                            "current_version": current_version,
                            "current_version_id": current_version_id,
                            "approved_version_id": current_version_id,
                            "lifecycle_status": "ACTIVE",
                            "evaluation_status": "PASSED",
                            "review_status": "APPROVED",
                            "publication_status": "FAILED",
                            "quality_summary": {},
                            "review_assignment": {"status": "UNASSIGNED"},
                            "latest_review_id": None,
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.question_versions = InMemoryCollection(versions)
                self.moodle_targets = InMemoryCollection(
                    [
                        {
                            "_id": target_id,
                            "site_key": moodle_site_id,
                            "site_name": "Demo Moodle",
                            "mode": "REST_API",
                            "default_course_id": course_id,
                            "default_category_id": category_id,
                            "is_active": True,
                        }
                    ]
                )
                self.moodle_publications = InMemoryCollection(
                    [
                        {
                            "_id": failed_id,
                            "schema_version": SCHEMA_VERSION,
                            "question_id": question_id,
                            "question_version_id": failed_version_id,
                            "question_version": failed_version,
                            "publisher_user_id": ObjectId(),
                            "target": {
                                "target_id": target_id,
                                "moodle_site_id": moodle_site_id,
                                "site_name": "Demo Moodle",
                                "mode": "MOCK",
                                "configured_mode": "REST_API",
                                "course_id": course_id,
                                "category_id": category_id,
                            },
                            "publication_mode": "MOCK",
                            "external_sync": False,
                            "status_detail": None,
                            "published_content_hash": failed_hash,
                            "idempotency_key": idempotency_key,
                            "status": "FAILED",
                            "attempt_no": 1,
                            "moodle_question_ref_id": None,
                            "request_payload": {
                                "question_code": "Q-001",
                                "export_format": "BOTH",
                                "mock": True,
                            },
                            "response_payload": {},
                            "error": {"message": "Moodle timeout"},
                            "created_at": now,
                            "updated_at": now,
                            "published_at": None,
                        }
                    ]
                )

        return FakeRetryDatabase(), failed_id, question_id

    def test_moodle_publish_is_idempotent_for_same_version_and_target(self):
        question_id = ObjectId()
        version_id = ObjectId()
        target_id = ObjectId()
        publisher_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakePublicationDatabase:
            def __init__(self):
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "schema_version": SCHEMA_VERSION,
                            "question_code": "Q-001",
                            "current_version": 1,
                            "current_version_id": version_id,
                            "approved_version_id": version_id,
                            "lifecycle_status": "ACTIVE",
                            "evaluation_status": "PASSED",
                            "review_status": "APPROVED",
                            "publication_status": "NOT_PUBLISHED",
                            "quality_summary": {},
                            "review_assignment": {"status": "UNASSIGNED"},
                            "latest_review_id": None,
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.question_versions = InMemoryCollection(
                    [
                        {
                            "_id": version_id,
                            "question_id": question_id,
                            "version": 1,
                            "document_id": None,
                            "content": "Queue xử lý phần tử theo nguyên tắc nào?",
                            "question_data": {
                                "options": {"A": "FIFO", "B": "LIFO"},
                                "correct_answer": "A",
                                "explanation": "Queue là hàng đợi FIFO.",
                            },
                            "classification": {"assessment_type": "TRAC_NGHIEM"},
                            "clos": [],
                            "sources": [],
                            "content_hash": "hash-v1",
                        }
                    ]
                )
                self.moodle_targets = InMemoryCollection(
                    [
                        {
                            "_id": target_id,
                            "site_key": "demo-moodle",
                            "site_name": "Demo Moodle",
                            "mode": "REST_API",
                            "default_course_id": "ctdl",
                            "default_category_id": "qbank",
                            "is_active": True,
                        }
                    ]
                )
                self.moodle_publications = InMemoryCollection()

        db = FakePublicationDatabase()
        original_transaction = question_workflow_module.mongo_transaction
        original_app_env = settings.app_env
        original_demo_mode = settings.demo_mode
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            settings.app_env = "demo"
            settings.demo_mode = True
            service = QuestionWorkflowService(db)
            payload = MoodlePublicationRequest(expected_version=1)

            first = service.publish_to_moodle(str(question_id), payload, publisher_id, "Reviewer")
            second = service.publish_to_moodle(str(question_id), payload, publisher_id, "Reviewer")
        finally:
            question_workflow_module.mongo_transaction = original_transaction
            settings.app_env = original_app_env
            settings.demo_mode = original_demo_mode

        self.assertEqual(len(db.moodle_publications.records), 1)
        self.assertEqual(first["_id"], second["_id"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(db.questions.find_one({"_id": question_id})["publication_status"], "PUBLISHED")
        publication = db.moodle_publications.records[0]
        self.assertEqual(publication["publication_mode"], "MOCK")
        self.assertFalse(publication["external_sync"])
        self.assertEqual(publication["status_detail"], "SIMULATED_LOCAL_RECORD")
        self.assertEqual(publication["target"]["configured_mode"], "REST_API")
        self.assertEqual(publication["target"]["course_id"], "ctdl")
        self.assertEqual(publication["target"]["category_id"], "qbank")
        self.assertEqual(publication["target"]["allowed_roles"], ["Admin", "Reviewer"])
        self.assertIn("gift", publication["request_payload"]["exports"])
        self.assertIn("xml", publication["request_payload"]["exports"])
        self.assertIn("chưa gửi dữ liệu sang Moodle thật", publication["response_payload"]["message"])

    def test_moodle_publish_enforces_target_allowed_roles(self):
        question_id = ObjectId()
        version_id = ObjectId()
        target_id = ObjectId()
        reviewer_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakePublicationDatabase:
            def __init__(self):
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "schema_version": SCHEMA_VERSION,
                            "question_code": "Q-001",
                            "current_version": 1,
                            "current_version_id": version_id,
                            "approved_version_id": version_id,
                            "lifecycle_status": "ACTIVE",
                            "evaluation_status": "PASSED",
                            "review_status": "APPROVED",
                            "publication_status": "NOT_PUBLISHED",
                            "quality_summary": {},
                            "review_assignment": {"status": "UNASSIGNED"},
                            "latest_review_id": None,
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.question_versions = InMemoryCollection(
                    [
                        {
                            "_id": version_id,
                            "question_id": question_id,
                            "version": 1,
                            "document_id": None,
                            "content": "Queue xử lý phần tử theo nguyên tắc nào?",
                            "question_data": {
                                "options": {"A": "FIFO", "B": "LIFO"},
                                "correct_answer": "A",
                                "explanation": "Queue là hàng đợi FIFO.",
                            },
                            "classification": {"assessment_type": "TRAC_NGHIEM"},
                            "clos": [],
                            "sources": [],
                            "content_hash": "hash-v1",
                        }
                    ]
                )
                self.moodle_targets = InMemoryCollection(
                    [
                        {
                            "_id": target_id,
                            "site_key": "admin-only",
                            "site_name": "Admin Moodle",
                            "mode": "MOCK",
                            "default_course_id": "ctdl",
                            "default_category_id": "qbank",
                            "allowed_roles": ["Admin"],
                            "is_active": True,
                        }
                    ]
                )
                self.moodle_publications = InMemoryCollection()

        service = QuestionWorkflowService(FakePublicationDatabase())
        payload = MoodlePublicationRequest(expected_version=1, target_id=str(target_id))
        original_app_env = settings.app_env
        original_demo_mode = settings.demo_mode
        try:
            settings.app_env = "demo"
            settings.demo_mode = True

            with self.assertRaises(PermissionError):
                service.publish_to_moodle(str(question_id), payload, reviewer_id, "Reviewer")
        finally:
            settings.app_env = original_app_env
            settings.demo_mode = original_demo_mode

    def test_admin_moodle_retry_failed_publication_updates_existing_attempt(self):
        db, failed_id, question_id = self._moodle_retry_database()
        admin = _current_user("Admin")
        original_transaction = question_workflow_module.mongo_transaction
        original_app_env = settings.app_env
        original_demo_mode = settings.demo_mode
        original_audit = MoodleTargetService._audit
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            settings.app_env = "demo"
            settings.demo_mode = True
            MoodleTargetService._audit = staticmethod(lambda *_args, **_kwargs: None)

            result = MoodleTargetService(db).retry_publication(str(failed_id), admin)
        finally:
            question_workflow_module.mongo_transaction = original_transaction
            settings.app_env = original_app_env
            settings.demo_mode = original_demo_mode
            MoodleTargetService._audit = staticmethod(original_audit)

        self.assertEqual(result["id"], str(failed_id))
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(result["attempt_no"], 2)
        self.assertEqual(len(db.moodle_publications.records), 1)
        self.assertEqual(db.moodle_publications.records[0]["status"], "PUBLISHED")
        self.assertIsNone(db.moodle_publications.records[0]["error"])
        self.assertEqual(
            db.questions.find_one({"_id": question_id})["publication_status"],
            "PUBLISHED",
        )

    def test_admin_moodle_retry_rejects_changed_question_version(self):
        db, failed_id, question_id = self._moodle_retry_database(current_version=2)
        admin = _current_user("Admin")
        original_transaction = question_workflow_module.mongo_transaction
        original_app_env = settings.app_env
        original_demo_mode = settings.demo_mode
        original_audit = MoodleTargetService._audit
        try:
            question_workflow_module.mongo_transaction = lambda: nullcontext(None)
            settings.app_env = "demo"
            settings.demo_mode = True
            MoodleTargetService._audit = staticmethod(lambda *_args, **_kwargs: None)

            with self.assertRaises(RuntimeError) as ctx:
                MoodleTargetService(db).retry_publication(str(failed_id), admin)
        finally:
            question_workflow_module.mongo_transaction = original_transaction
            settings.app_env = original_app_env
            settings.demo_mode = original_demo_mode
            MoodleTargetService._audit = staticmethod(original_audit)

        self.assertEqual(str(ctx.exception), "VERSION_CONFLICT")
        self.assertEqual(db.moodle_publications.records[0]["status"], "FAILED")
        self.assertEqual(
            db.questions.find_one({"_id": question_id})["publication_status"],
            "FAILED",
        )

    def test_moodle_target_payload_requires_real_api_config(self):
        with self.assertRaises(ValidationError):
            MoodleTargetPayload(
                site_key="ctu",
                site_name="CTU Moodle",
                mode="REST_API",
                default_course_id="ctdl",
                default_category_id="qbank",
            )

        payload = MoodleTargetPayload(
            site_key="ctu",
            site_name="CTU Moodle",
            mode="REST_API",
            base_url="https://moodle.example.edu/",
            token_env_var="MOODLE_TOKEN",
            default_course_id="ctdl",
            default_category_id="qbank",
        )
        self.assertEqual(payload.base_url, "https://moodle.example.edu")

        with self.assertRaises(ValidationError):
            MoodleTargetPayload(
                site_key="ctu",
                site_name="CTU Moodle",
                default_course_id="ctdl",
                default_category_id="qbank",
                allowed_roles=[],
            )

    def test_demo_login_route_registration_follows_demo_mode(self):
        route_paths = {route.path for route in auth_login.router.routes}
        self.assertEqual("/demo-login" in route_paths, settings.demo_mode)

    def test_review_dashboard_route_precedes_question_id_route(self):
        from modules.questions.workflow_service import get_workflow_service

        # Exercise dispatch: recent FastAPI versions include routers lazily,
        # so inspecting app.routes does not expose their individual paths.
        current_user = _current_user("Reviewer")
        service = Mock(spec=QuestionWorkflowService)
        service.review_dashboard.return_value = {"scope": "current_reviewer"}
        previous_overrides = app.dependency_overrides.copy()
        app.dependency_overrides[require_reviewer_or_admin] = lambda: current_user
        app.dependency_overrides[get_workflow_service] = lambda: service
        client = TestClient(app)
        try:
            response = client.get(f"{settings.api_prefix}/questions/review-dashboard")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"scope": "current_reviewer"})
            service.review_dashboard.assert_called_once_with(current_user)
        finally:
            client.close()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)

    def test_demo_login_does_not_reenable_disabled_firebase_user(self):
        class FakeFirebaseUser:
            uid = "demo-admin-uid"
            disabled = True

        class FakeAuth:
            class UserNotFoundError(Exception):
                pass

            update_calls = []

            @staticmethod
            def get_user_by_email(_email):
                return FakeFirebaseUser()

            @classmethod
            def update_user(cls, *args, **kwargs):
                cls.update_calls.append((args, kwargs))

        original_auth = auth_login.auth
        try:
            auth_login.auth = FakeAuth
            with self.assertRaises(HTTPException) as ctx:
                auth_login._ensure_demo_firebase_user("admin@example.com", "Admin Demo")
        finally:
            auth_login.auth = original_auth

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(FakeAuth.update_calls, [])

    def test_demo_login_does_not_reactivate_disabled_app_user(self):
        user_id = ObjectId()

        class FakeDemoDatabase:
            def __init__(self):
                self.users = InMemoryCollection(
                    [
                        {
                            "_id": user_id,
                            "schema_version": SCHEMA_VERSION,
                            "firebase_uid": "demo-admin-uid",
                            "email": "old-admin@example.com",
                            "display_name": "Old Admin",
                            "role": "Admin",
                            "profile": {},
                            "is_active": False,
                            "created_at": datetime.now(timezone.utc),
                            "updated_at": datetime.now(timezone.utc),
                        }
                    ]
                )

        fake_db = FakeDemoDatabase()
        original_get_rag_db = auth_login.get_rag_db
        try:
            auth_login.get_rag_db = lambda: fake_db
            user = auth_login._ensure_demo_app_user(
                "demo-admin-uid",
                {
                    "email": "admin@qbankctu.edu.vn",
                    "display_name": "Admin Demo",
                    "role": "Admin",
                },
            )
        finally:
            auth_login.get_rag_db = original_get_rag_db

        self.assertFalse(user["is_active"])
        self.assertEqual(user["email"], "admin@qbankctu.edu.vn")
        self.assertEqual(user["display_name"], "Admin Demo")

    def test_teacher_cannot_access_another_teachers_exam(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        service = ExamService(FakeExamRepository([exam]), question_repository=None)

        with self.assertRaises(LookupError):
            service.get_exam(str(exam["_id"]), other_teacher)

        result = service.get_exam(str(exam["_id"]), owner)
        self.assertEqual(result["id"], str(exam["_id"]))

    def test_document_job_history_enforces_document_ownership(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        document_id = ObjectId()
        job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentJobRepository:
            def find_by_id(self, _document_id):
                return {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "schema_version": SCHEMA_VERSION,
                    "archived_at": None,
                }

            def list_jobs(self, _document_id, *, limit=20):
                return [
                    {
                        "_id": job_id,
                        "document_id": document_id,
                        "document_version": 1,
                        "job_type": "OCR",
                        "attempt_no": 2,
                        "status": "FAILED",
                        "progress": 40,
                        "stats": {"pages": 3},
                        "error": {"message": "OCR timeout", "at": now},
                        "queued_at": now,
                        "started_at": now,
                        "finished_at": now,
                    }
                ][:limit]

        service = DocumentService(FakeDocumentJobRepository())

        with self.assertRaises(LookupError):
            service.list_jobs(str(document_id), other_teacher)

        result = service.list_jobs(str(document_id), owner)
        self.assertEqual(result["items"][0]["id"], str(job_id))
        self.assertEqual(result["items"][0]["document_id"], str(document_id))
        self.assertEqual(result["items"][0]["error"]["message"], "OCR timeout")
        self.assertTrue(result["items"][0]["can_retry"])
        self.assertFalse(result["items"][0]["can_cancel"])

    def test_document_sharing_allows_read_and_limits_transfer_to_admin(self):
        owner = _current_user("Teacher")
        shared_teacher = _current_user("Teacher")
        new_owner = _current_user("Teacher")
        admin = _current_user("Admin", permissions=effective_permissions({"role": "Admin"}))
        document_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentSharingRepository:
            def __init__(self):
                self.document = {
                    "_id": document_id,
                    "title": "Shared source",
                    "original_filename": "shared.pdf",
                    "status": "READY",
                    "subject_id": None,
                    "chapter_id": None,
                    "uploaded_by_user_id": owner.id,
                    "shared_with_user_ids": [],
                    "shared_scope": "PRIVATE",
                    "current_version": 1,
                    "page_count": 1,
                    "artifacts": [],
                    "current_processing": {},
                    "pipeline_summary": {},
                    "latest_error": None,
                    "created_at": now,
                    "updated_at": now,
                    "archived_at": None,
                }
                self.fields = None

            def find_by_id(self, _document_id):
                return self.document

            def update(self, _document_id, fields, *, audit=None):
                self.audit = audit
                normalized = dict(fields)
                if normalized.get("owner_user_id"):
                    normalized["uploaded_by_user_id"] = ObjectId(normalized.pop("owner_user_id"))
                if normalized.get("uploaded_by_user_id"):
                    normalized["uploaded_by_user_id"] = ObjectId(normalized["uploaded_by_user_id"])
                if "shared_with_user_ids" in normalized:
                    normalized["shared_with_user_ids"] = [
                        ObjectId(user_id) for user_id in normalized.get("shared_with_user_ids") or []
                    ]
                self.fields = normalized
                self.document.update(normalized)
                return self.document

        repository = FakeDocumentSharingRepository()
        service = DocumentService(repository)

        with self.assertRaises(PermissionError):
            service.update_sharing(
                str(document_id),
                DocumentSharingRequest(owner_user_id=str(new_owner.id)),
                owner,
            )
        shared = service.update_sharing(
            str(document_id),
            DocumentSharingRequest(
                shared_with_user_ids=[str(shared_teacher.id)],
                shared_scope="SUBJECT",
            ),
            owner,
        )
        self.assertEqual(shared["shared_scope"], "SUBJECT")
        self.assertEqual(service.get(str(document_id), shared_teacher)["id"], str(document_id))

        # Re-sending the current owner is not a transfer, so the owner may still save sharing.
        service.update_sharing(
            str(document_id),
            DocumentSharingRequest(owner_user_id=str(owner.id), shared_with_user_ids=[str(shared_teacher.id)],
                                   shared_scope="SUBJECT"),
            owner,
        )
        self.assertEqual(repository.audit["action"], "document.sharing_update")

        transfer = DocumentSharingRequest(
            owner_user_id=str(new_owner.id),
            shared_with_user_ids=[str(shared_teacher.id), str(new_owner.id)],
            shared_scope="SUBJECT",
        )
        with self.assertRaisesRegex(ValueError, "lý do"):
            service.update_sharing(str(document_id), transfer, admin)
        transfer.owner_transfer_reason = "Giảng viên cũ nghỉ"
        with patch("modules.documents.service.users_by_ids", return_value=[]):
            with self.assertRaisesRegex(ValueError, "Người nhận"):
                service.update_sharing(str(document_id), transfer, admin)
        with patch("modules.documents.service.users_by_ids", return_value=[{"_id": new_owner.id, "role": "Reviewer"}]):
            with self.assertRaisesRegex(ValueError, "giảng viên"):
                service.update_sharing(str(document_id), transfer, admin)
        with patch("modules.documents.service.users_by_ids", return_value=[{"_id": new_owner.id, "role": "Teacher"}]):
            transferred = service.update_sharing(str(document_id), transfer, admin)
        self.assertEqual(transferred["uploaded_by_user_id"], str(new_owner.id))
        self.assertEqual(transferred["shared_with_user_ids"], [str(shared_teacher.id)])
        self.assertEqual(repository.audit["action"], "document.owner_transfer")
        self.assertEqual(repository.audit["metadata"]["reason"], "Giảng viên cũ nghỉ")
        self.assertEqual(repository.audit["before"]["uploaded_by_user_id"], owner.id)

    def test_document_pages_enforce_document_ownership(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        document_id = ObjectId()
        page_id = ObjectId()
        ocr_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentPageRepository:
            def find_by_id(self, _document_id):
                return {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "current_version": 2,
                    "schema_version": SCHEMA_VERSION,
                    "archived_at": None,
                }

            def list_pages(self, _document_id, *, document_version=None, limit=100):
                self.requested_version = document_version
                return [
                    {
                        "_id": page_id,
                        "document_id": document_id,
                        "document_version": 2,
                        "ocr_job_id": ocr_job_id,
                        "page_number": 1,
                        "raw_text": "raw OCR",
                        "cleaned_text": "clean OCR",
                        "formula_blocks": [{"latex": "x^2"}],
                        "created_at": now,
                    }
                ][:limit]

        repository = FakeDocumentPageRepository()
        service = DocumentService(repository)

        with self.assertRaises(LookupError):
            service.list_pages(str(document_id), other_teacher)

        result = service.list_pages(str(document_id), owner)
        self.assertEqual(repository.requested_version, 2)
        self.assertEqual(result["items"][0]["id"], str(page_id))
        self.assertEqual(result["items"][0]["document_id"], str(document_id))
        self.assertEqual(result["items"][0]["cleaned_text"], "clean OCR")

    def test_document_page_update_requires_owner_and_pre_chunk_state(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        document_id = ObjectId()
        page_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentPageUpdateRepository:
            def __init__(self):
                self.document = {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "current_version": 1,
                    "pipeline_summary": {"chunk_status": "NOT_STARTED", "index_status": "NOT_STARTED"},
                }
                self.page = {
                    "_id": page_id,
                    "document_id": document_id,
                    "document_version": 1,
                    "ocr_job_id": ObjectId(),
                    "page_number": 1,
                    "raw_text": "raw",
                    "cleaned_text": "old",
                    "formula_blocks": [],
                    "created_at": now,
                }

            def find_by_id(self, _document_id):
                return self.document

            def update_page(self, _document_id, _page_id, *, document_version, cleaned_text):
                self.requested_version = document_version
                self.page["cleaned_text"] = cleaned_text
                return self.page

        repository = FakeDocumentPageUpdateRepository()
        service = DocumentService(repository)
        payload = DocumentPageUpdateRequest(cleaned_text="new OCR")

        with self.assertRaises(LookupError):
            service.update_page(str(document_id), str(page_id), payload, other_teacher)

        result = service.update_page(str(document_id), str(page_id), payload, owner)
        self.assertEqual(repository.requested_version, 1)
        self.assertEqual(result["cleaned_text"], "new OCR")

        repository.document["pipeline_summary"]["chunk_status"] = "COMPLETED"
        with self.assertRaises(ValueError):
            service.update_page(str(document_id), str(page_id), payload, owner)

    def test_document_job_retry_and_cancel_enforce_document_ownership(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        document_id = ObjectId()
        failed_job_id = ObjectId()
        active_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeBackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, func, *args, **kwargs):
                self.tasks.append({"func": func, "args": args, "kwargs": kwargs})

        class FakeDocumentActionRepository:
            def __init__(self):
                self.document = {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "title": "Owner PDF",
                    "original_filename": "owner.pdf",
                    "current_version": 1,
                    "artifacts": [
                        {
                            "type": "ORIGINAL_PDF",
                            "is_current": True,
                            "storage": {"provider": "LOCAL", "uri": "backend/data/uploads/owner.pdf"},
                        }
                    ],
                }
                self.jobs = {
                    str(failed_job_id): {
                        "_id": failed_job_id,
                        "document_id": document_id,
                        "document_version": 1,
                        "job_type": "OCR",
                        "attempt_no": 1,
                        "status": "FAILED",
                        "progress": 20,
                        "stats": None,
                        "error": {"message": "timeout", "at": now},
                        "config": {"lang": "vi"},
                        "queued_at": now,
                        "started_at": now,
                        "finished_at": now,
                    },
                    str(active_job_id): {
                        "_id": active_job_id,
                        "document_id": document_id,
                        "document_version": 1,
                        "job_type": "OCR",
                        "attempt_no": 1,
                        "status": "PROCESSING",
                        "progress": 30,
                        "stats": None,
                        "error": None,
                        "queued_at": now,
                        "started_at": now,
                        "finished_at": None,
                    },
                }

            def find_by_id(self, _document_id):
                return self.document

            def list_jobs(self, _document_id, *, limit=20):
                return list(self.jobs.values())[:limit]

            def find_job(self, job_id):
                return self.jobs.get(str(job_id))

            def create_job(self, document_id_arg, job_type, config=None):
                new_id = ObjectId()
                job = {
                    "_id": new_id,
                    "document_id": document_id_arg,
                    "document_version": 1,
                    "job_type": job_type,
                    "attempt_no": 2,
                    "status": "QUEUED",
                    "progress": 0,
                    "stats": None,
                    "error": None,
                    "config": config or {},
                    "queued_at": now,
                    "started_at": None,
                    "finished_at": None,
                }
                self.jobs[str(new_id)] = job
                return job

            def update_job(self, job_id, status, *, progress=None, stats=None, error_message=None):
                job = self.jobs[str(job_id)]
                job["status"] = status
                job["progress"] = progress if progress is not None else job.get("progress")
                job["stats"] = stats if stats is not None else job.get("stats")
                job["error"] = {"message": error_message, "at": now} if error_message else job.get("error")
                job["finished_at"] = now
                return job

        background_tasks = FakeBackgroundTasks()
        service = DocumentService(FakeDocumentActionRepository())

        with self.assertRaises(LookupError):
            service.retry_job(str(document_id), str(failed_job_id), background_tasks, other_teacher)
        with self.assertRaisesRegex(ValueError, "đang có job chạy"):
            service.retry_job(str(document_id), str(failed_job_id), background_tasks, owner)

        with self.assertRaises(LookupError):
            service.cancel_job(str(document_id), str(active_job_id), other_teacher)

        cancelled = service.cancel_job(str(document_id), str(active_job_id), owner)
        self.assertEqual(cancelled["job"]["status"], "CANCELLED")
        self.assertIn("Cancelled by Teacher", cancelled["job"]["error"]["message"])

        retried = service.retry_job(str(document_id), str(failed_job_id), background_tasks, owner)
        self.assertEqual(retried["job"]["attempt_no"], 2)
        self.assertEqual(retried["job"]["status"], "QUEUED")
        self.assertEqual(background_tasks.tasks[0]["kwargs"]["document_id"], str(document_id))
        with self.assertRaisesRegex(ValueError, "đang có job chạy"):
            service.retry_job(str(document_id), str(failed_job_id), background_tasks, owner)

    def test_document_chunk_job_retry_uses_failed_job_config(self):
        owner = _current_user("Teacher")
        document_id = ObjectId()
        failed_job_id = ObjectId()
        new_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeBackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, func, *args, **kwargs):
                self.tasks.append({"func": func, "args": args, "kwargs": kwargs})

        class FakeChunkRetryRepository:
            def __init__(self):
                self.jobs = {
                    str(failed_job_id): {
                        "_id": failed_job_id,
                        "document_id": document_id,
                        "document_version": 1,
                        "job_type": "CHUNK",
                        "attempt_no": 1,
                        "status": "FAILED",
                        "progress": 80,
                        "stats": None,
                        "error": {"message": "chunk failed", "at": now},
                        "config": {"chunk_size": 700, "chunk_overlap": 120, "dry_run": True},
                        "queued_at": now,
                        "started_at": now,
                        "finished_at": now,
                    }
                }

            def find_by_id(self, _document_id):
                return {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "current_version": 1,
                }

            def find_job(self, job_id):
                return self.jobs.get(str(job_id))

            def list_jobs(self, _document_id, *, limit=20):
                return list(self.jobs.values())[:limit]

        repository = FakeChunkRetryRepository()
        captured = {}

        def fake_queue_chunk_retry(background_tasks, document_id, config, *, promote_actor=None):
            captured["document_id"] = document_id
            captured["config"] = config
            captured["promote_actor"] = promote_actor
            repository.jobs[str(new_job_id)] = {
                "_id": new_job_id,
                "document_id": ObjectId(document_id),
                "document_version": 1,
                "job_type": "CHUNK",
                "attempt_no": 2,
                "status": "QUEUED",
                "progress": 0,
                "stats": None,
                "error": None,
                "queued_at": now,
                "started_at": None,
                "finished_at": None,
            }
            background_tasks.add_task(lambda: None)
            return {"chunk_job_id": str(new_job_id), "chunk_set_id": str(ObjectId())}

        import modules.rag.chunking as chunking_module

        original_queue = chunking_module.queue_chunk_retry
        try:
            chunking_module.queue_chunk_retry = fake_queue_chunk_retry
            result = DocumentService(repository).retry_job(
                str(document_id),
                str(failed_job_id),
                FakeBackgroundTasks(),
                owner,
            )
        finally:
            chunking_module.queue_chunk_retry = original_queue

        self.assertEqual(captured["document_id"], str(document_id))
        self.assertEqual(captured["config"]["chunk_size"], 700)
        self.assertEqual(captured["promote_actor"], owner.email or str(owner.id))
        self.assertEqual(result["job"]["id"], str(new_job_id))
        self.assertTrue(result["job"]["can_cancel"])

    def test_document_retry_requires_latest_job_and_caps_owner_retries(self):
        owner = _current_user("Teacher")
        admin = _current_user("Admin")
        document_id = ObjectId()

        def job(attempt, status="FAILED"):
            return {"_id": ObjectId(), "document_id": document_id, "document_version": 1,
                    "job_type": "OCR", "attempt_no": attempt, "status": status}

        class FakeRetryGuardRepository:
            def __init__(self, jobs):
                self.jobs = {str(item["_id"]): item for item in jobs}

            def find_by_id(self, _document_id):
                return {"_id": document_id, "uploaded_by_user_id": owner.id, "current_version": 1}

            def find_job(self, job_id):
                return self.jobs.get(str(job_id))

            def list_jobs(self, _document_id, *, limit=20):
                return list(self.jobs.values())[:limit]

        old, latest = job(1), job(2)
        service = DocumentService(FakeRetryGuardRepository([old, latest]))
        with self.assertRaisesRegex(ValueError, "lần chạy mới hơn"):
            service.retry_job(str(document_id), str(old["_id"]), None, owner)

        fourth = job(4)
        service = DocumentService(FakeRetryGuardRepository([job(1), job(2), job(3), fourth]))
        with self.assertRaisesRegex(PermissionError, "quản trị viên"):
            service.retry_job(str(document_id), str(fourth["_id"]), None, owner)
        service._ensure_retry_allowed(
            {"_id": document_id}, fourth, admin,
        )

    def test_cancel_chunk_job_marks_chunk_set_and_pending_embeddings_cancelled(self):
        document_id = ObjectId()
        chunk_job_id = ObjectId()
        chunk_set_id = ObjectId()
        embedding_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentDatabase:
            def __init__(self):
                self.documents = InMemoryCollection(
                    [
                        {
                            "_id": document_id,
                            "archived_at": None,
                            "status": "PROCESSING",
                            "pipeline_summary": {"chunk_status": "PROCESSING"},
                            "updated_at": now,
                        }
                    ]
                )
                self.document_jobs = InMemoryCollection(
                    [
                        {
                            "_id": chunk_job_id,
                            "document_id": document_id,
                            "job_type": "CHUNK",
                            "status": "PROCESSING",
                            "queued_at": now,
                            "started_at": now,
                        }
                    ]
                )
                self.chunk_sets = InMemoryCollection(
                    [{"_id": chunk_set_id, "chunk_job_id": chunk_job_id, "status": "PROCESSING"}]
                )
                self.chunk_embeddings = InMemoryCollection(
                    [{"_id": embedding_id, "chunk_set_id": chunk_set_id, "status": "PENDING"}]
                )

        db = FakeDocumentDatabase()
        updated = MongoDocumentRepository(db).update_job(
            str(chunk_job_id),
            "CANCELLED",
            error_message="Cancelled by test",
        )

        self.assertEqual(updated["status"], "CANCELLED")
        self.assertEqual(db.chunk_sets.find_one({"_id": chunk_set_id})["status"], "CANCELLED")
        self.assertEqual(db.chunk_embeddings.find_one({"_id": embedding_id})["status"], "CANCELLED")
        document = db.documents.find_one({"_id": document_id})
        self.assertEqual(document["pipeline_summary"]["chunk_status"], "CANCELLED")
        self.assertEqual(document["status"], "FAILED")

    def test_failed_replacement_chunk_job_preserves_active_ready_pipeline(self):
        document_id = ObjectId()
        active_chunk_set_id = ObjectId()
        replacement_job_id = ObjectId()
        replacement_chunk_set_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeDocumentDatabase:
            def __init__(self):
                self.documents = InMemoryCollection(
                    [
                        {
                            "_id": document_id,
                            "archived_at": None,
                            "status": "READY",
                            "current_processing": {"chunk_set_id": active_chunk_set_id},
                            "pipeline_summary": {"chunk_status": "COMPLETED", "index_status": "COMPLETED"},
                            "updated_at": now,
                        }
                    ]
                )
                self.document_jobs = InMemoryCollection(
                    [
                        {
                            "_id": replacement_job_id,
                            "document_id": document_id,
                            "job_type": "CHUNK",
                            "status": "PROCESSING",
                            "preserves_active_pipeline": True,
                            "previous_document_state": {"status": "READY", "pipeline_status": "COMPLETED"},
                            "queued_at": now,
                            "started_at": now,
                        }
                    ]
                )
                self.chunk_sets = InMemoryCollection(
                    [{"_id": replacement_chunk_set_id, "chunk_job_id": replacement_job_id, "status": "PROCESSING"}]
                )
                self.chunk_embeddings = InMemoryCollection([])

        db = FakeDocumentDatabase()
        MongoDocumentRepository(db).update_job(
            replacement_job_id,
            "CANCELLED",
            error_message="Replacement cancelled",
        )

        document = db.documents.find_one({"_id": document_id})
        self.assertEqual(document["status"], "READY")
        self.assertEqual(document["current_processing"]["chunk_set_id"], active_chunk_set_id)
        self.assertEqual(document["pipeline_summary"]["chunk_status"], "COMPLETED")
        self.assertEqual(document["pipeline_attempts"]["chunk"]["status"], "CANCELLED")

    def test_document_reindex_requires_owner_and_completed_chunks(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        document_id = ObjectId()
        chunk_set_id = ObjectId()
        new_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeBackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, func, *args, **kwargs):
                self.tasks.append({"func": func, "args": args, "kwargs": kwargs})

        class FakeReindexRepository:
            def __init__(self):
                self.document = {
                    "_id": document_id,
                    "uploaded_by_user_id": owner.id,
                    "current_version": 1,
                    "current_processing": {"chunk_set_id": chunk_set_id},
                    "pipeline_summary": {"chunk_status": "COMPLETED"},
                }
                self.jobs = {}

            def find_by_id(self, _document_id):
                return self.document

            def find_job(self, job_id):
                return self.jobs.get(str(job_id))

        repository = FakeReindexRepository()

        def fake_queue_document_reindex(background_tasks, document_id, collection_name=None):
            repository.jobs[str(new_job_id)] = {
                "_id": new_job_id,
                "document_id": ObjectId(document_id),
                "document_version": 1,
                "job_type": "INDEX",
                "attempt_no": 1,
                "status": "QUEUED",
                "progress": 0,
                "stats": None,
                "error": None,
                "queued_at": now,
                "started_at": None,
                "finished_at": None,
            }
            background_tasks.add_task(lambda: None)
            return {"index_job_id": str(new_job_id)}

        import modules.rag.chunking as chunking_module

        original_queue = chunking_module.queue_document_reindex
        try:
            chunking_module.queue_document_reindex = fake_queue_document_reindex
            service = DocumentService(repository)
            with self.assertRaises(LookupError):
                service.reindex(str(document_id), FakeBackgroundTasks(), other_teacher)

            result = service.reindex(str(document_id), FakeBackgroundTasks(), owner)
        finally:
            chunking_module.queue_document_reindex = original_queue

        self.assertEqual(result["job"]["id"], str(new_job_id))
        self.assertTrue(result["job"]["can_cancel"])

        repository.document["pipeline_summary"]["chunk_status"] = "FAILED"
        with self.assertRaises(ValueError):
            DocumentService(repository).reindex(str(document_id), FakeBackgroundTasks(), owner)

    def test_docx_upload_extractor_builds_pages_from_paragraphs_and_tables(self):
        with TemporaryDirectory() as temp_dir:
            docx_path = Path(temp_dir) / "source.docx"
            document = Document()
            document.add_paragraph("Chương 1. Tổng quan cấu trúc dữ liệu")
            document.add_paragraph("Hàng đợi sử dụng chính sách FIFO.")
            table = document.add_table(rows=2, cols=2)
            table.cell(0, 0).text = "Thuật ngữ"
            table.cell(0, 1).text = "Ý nghĩa"
            table.cell(1, 0).text = "Stack"
            table.cell(1, 1).text = "LIFO"
            document.save(docx_path)

            pages, stats = extract_docx_pages(docx_path, page_char_limit=80)

        joined_text = "\n".join(page["text"] for page in pages)
        self.assertGreaterEqual(len(pages), 1)
        self.assertEqual(stats["source_format"], "docx")
        self.assertEqual(stats["paragraph_count"], 2)
        self.assertEqual(stats["table_count"], 1)
        self.assertIn("FIFO", joined_text)
        self.assertIn("Stack | LIFO", joined_text)

    def test_admin_can_access_any_teacher_exam(self):
        owner = _current_user("Teacher")
        admin = _current_user("Admin")
        exam = _exam_doc(owner.id)
        service = ExamService(FakeExamRepository([exam]), question_repository=None)

        result = service.get_exam(str(exam["_id"]), admin)
        self.assertEqual(result["created_by_user_id"], str(owner.id))

    def test_exam_docx_export_contains_questions_and_answers(self):
        docx_bytes = render_exam_docx(
            {
                "school_name": "CTU",
                "faculty_name": "College of ICT",
                "exam_name": "Midterm",
                "subject_name": "Data Structures",
                "duration_minutes": 90,
            },
            "A01",
            [
                {
                    "order": 1,
                    "content_snapshot": {
                        "content": "Queue uses which policy?",
                        "question_data": {
                            "options": {"A": "FIFO", "B": "LIFO"},
                            "correct_answer": "A",
                        },
                    },
                }
            ],
            "de_dapan",
        )

        document = Document(BytesIO(docx_bytes))
        paragraph_text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        table_text = "\n".join(
            cell.text
            for table in document.tables
            for row in table.rows
            for cell in row.cells
        )

        self.assertGreater(len(docx_bytes), 1000)
        self.assertIn("Midterm", paragraph_text)
        self.assertIn("Queue uses which policy?", paragraph_text)
        self.assertIn("FIFO", paragraph_text)
        self.assertIn("A", table_text)

    def test_exam_lifecycle_requires_exact_current_approved_questions(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id = ObjectId()
        version_id = ObjectId()
        question = {
            "_id": question_id,
            "question_code": "Q-1",
            "created_by_user_id": owner.id,
            "current_version": 1,
            "current_version_id": version_id,
            "approved_version_id": version_id,
            "lifecycle_status": "ACTIVE",
            "evaluation_status": "PASSED",
            "review_status": "APPROVED",
            "publication_status": "NOT_PUBLISHED",
            "quality_summary": {},
            "review_assignment": {"status": "UNASSIGNED"},
            "latest_review_id": None,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        version = {
            "_id": version_id,
            "question_id": question_id,
            "document_id": None,
            "content": "Queue uses FIFO",
            "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
            "classification": {"subject": {"id": exam["subject_id"]}, "assessment_type": "TRAC_NGHIEM"},
            "clos": [],
            "sources": [],
            "content_hash": "hash",
        }
        service = ExamService(
            FakeExamRepository([exam]),
            FakeExamQuestionRepository([(question, version)]),
        )

        with self.assertRaises(ValueError):
            service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="READY"), owner)

        exam["questions"] = [
            {
                "question_id": question_id,
                "version_id": version_id,
                "content_snapshot": {"content": "Queue uses FIFO"},
            }
        ]
        ready = service.update_status(
            str(exam["_id"]),
            ExamStatusUpdateRequest(status="READY"),
            owner,
        )
        result = service.update_status(
            str(exam["_id"]),
            ExamStatusUpdateRequest(status="FINALIZED"),
            owner,
        )

        self.assertEqual(ready["status"], "READY")
        self.assertEqual(result["status"], "FINALIZED")
        self.assertIn("finalized_snapshot", exam)

    def test_exam_lifecycle_requires_approved_version_and_subject(self):
        owner = _current_user("Teacher")

        def make_pair(*, approved_version_id=True, subject_id=None):
            question_id = ObjectId()
            version_id = ObjectId()
            question = {
                "_id": question_id,
                "question_code": "Q-1",
                "current_version": 1,
                "current_version_id": version_id,
                "approved_version_id": version_id if approved_version_id is True else approved_version_id,
                "lifecycle_status": "ACTIVE",
                "evaluation_status": "PASSED",
                "review_status": "APPROVED",
                "publication_status": "NOT_PUBLISHED",
                "quality_summary": {},
                "review_assignment": {"status": "UNASSIGNED"},
                "latest_review_id": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            subject = {} if subject_id is None else {"id": subject_id}
            version = {
                "_id": version_id,
                "question_id": question_id,
                "document_id": None,
                "content": "Queue uses FIFO",
                "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
                "classification": {"subject": subject, "assessment_type": "TRAC_NGHIEM"},
                "clos": [],
                "sources": [],
                "content_hash": "hash",
            }
            return question, version

        for question, version in (
            make_pair(approved_version_id=None, subject_id=ObjectId()),
            make_pair(approved_version_id=True, subject_id=None),
        ):
            exam = _exam_doc(owner.id)
            if (version.get("classification") or {}).get("subject"):
                version["classification"]["subject"]["id"] = exam["subject_id"]
            exam["questions"] = [
                {
                    "question_id": question["_id"],
                    "version_id": version["_id"],
                    "content_snapshot": {"content": version["content"]},
                }
            ]
            service = ExamService(
                FakeExamRepository([exam]),
                FakeExamQuestionRepository([(question, version)]),
            )

            with self.assertRaises(ValueError):
                service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="READY"), owner)

    def test_matrix_availability_counts_only_usable_approved_questions(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        exam["matrix"] = [
            {
                "chapter_id": None,
                "cognitive_level": "nhan_biet",
                "difficulty": "de",
                "count": 2,
            }
        ]

        def make_pair(approved_version_id):
            question_id = ObjectId()
            version_id = ObjectId()
            question = {
                "_id": question_id,
                "question_code": f"Q-{question_id}",
                "current_version": 1,
                "current_version_id": version_id,
                "approved_version_id": version_id if approved_version_id else ObjectId(),
                "lifecycle_status": "ACTIVE",
                "evaluation_status": "PASSED",
                "review_status": "APPROVED",
                "publication_status": "NOT_PUBLISHED",
                "quality_summary": {},
                "review_assignment": {"status": "UNASSIGNED"},
                "latest_review_id": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            version = {
                "_id": version_id,
                "question_id": question_id,
                "document_id": None,
                "content": "Queue uses FIFO",
                "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
                "classification": {"subject": {"id": exam["subject_id"]}},
                "clos": [],
                "sources": [],
                "content_hash": "hash",
            }
            return question, version

        service = ExamService(
            FakeExamRepository([exam]),
            FakeExamQuestionRepository([
                make_pair(True),
                make_pair(False),
            ]),
        )

        availability = service.matrix_availability(str(exam["_id"]), owner)

        self.assertEqual(availability[0]["available"], 1)
        self.assertFalse(availability[0]["sufficient"])

    def test_finalized_exam_cannot_be_hard_deleted_or_edited(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id = ObjectId()
        version_id = ObjectId()
        snapshot = {
            "id": str(question_id),
            "question_code": "Q-1",
            "content": "Queue uses FIFO",
            "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
            "classification": {"subject": {"id": str(exam["subject_id"])}},
        }
        exam.update(
            {
                "status": "FINALIZED",
                "question_count": 1,
                "matrix": [],
                "questions": [
                    {
                        "question_id": question_id,
                        "version_id": version_id,
                        "content_snapshot": snapshot,
                    }
                ],
                "finalized_snapshot": {
                    "subject_id": exam["subject_id"],
                    "question_count": 1,
                    "matrix": [],
                    "questions": [
                        {
                            "question_id": question_id,
                            "version_id": version_id,
                            "content_snapshot": snapshot,
                        }
                    ],
                },
            }
        )
        repository = FakeExamRepository([exam])
        service = ExamService(repository, FakeExamQuestionRepository())

        with self.assertRaises(ValueError):
            service.update_exam(str(exam["_id"]), ExamUpdateRequest(name="Edited"), owner)
        with self.assertRaises(ValueError):
            service.save_matrix(str(exam["_id"]), ExamMatrixRequest(cells=[]), owner)
        with self.assertRaises(ValueError):
            service.auto_generate_pool(str(exam["_id"]), owner)
        with self.assertRaises(ValueError):
            service.add_questions_manual(
                str(exam["_id"]),
                AddQuestionsManualRequest(question_ids=[str(ObjectId())]),
                owner,
            )
        with self.assertRaises(ValueError):
            service.remove_question(str(exam["_id"]), str(question_id), owner)
        with self.assertRaises(ValueError):
            service.delete_exam(str(exam["_id"]), owner)

        self.assertIsNotNone(repository.find(exam["_id"]))

    def test_owner_delete_exam_is_soft_and_hides_exam(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        repository = FakeExamRepository([exam])
        service = ExamService(repository, FakeExamQuestionRepository())

        service.delete_exam(str(exam["_id"]), owner)

        stored = repository.exams[str(exam["_id"])]
        self.assertIsNotNone(stored["deleted_at"])
        self.assertEqual(str(stored["deleted_by_user_id"]), str(owner.id))
        self.assertIsNone(repository.find(exam["_id"]))
        with self.assertRaises(LookupError):
            service.get_exam(str(exam["_id"]), owner)

    def test_exam_work_requires_teaching_the_subject(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        service = ExamService(FakeExamRepository([exam]), FakeExamQuestionRepository())

        with patch("modules.users.store.teaching_subject_ids", return_value=set()):
            with self.assertRaises(PermissionError):
                service.duplicate_exam(str(exam["_id"]), owner)
            with self.assertRaises(PermissionError):
                service.add_questions_manual(
                    str(exam["_id"]), AddQuestionsManualRequest(question_ids=[str(ObjectId())]), owner,
                )
        with patch("modules.users.store.teaching_subject_ids", return_value={str(exam["subject_id"])}):
            self.assertEqual(service.duplicate_exam(str(exam["_id"]), owner)["status"], "DRAFT")

    @patch("modules.exams.service.ensure_teaches_subjects", new=lambda *args, **kwargs: None)
    def test_duplicate_exam_creates_editable_draft_without_variants(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id = ObjectId()
        version_id = ObjectId()
        snapshot = {
            "id": str(question_id),
            "question_code": "Q-1",
            "content": "Queue uses FIFO",
            "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
            "classification": {"subject": {"id": str(exam["subject_id"])}},
        }
        exam.update(
            {
                "status": "FINALIZED",
                "question_count": 1,
                "matrix": [
                    {
                        "chapter_id": None,
                        "cognitive_level": "nhan_biet",
                        "difficulty": "de",
                        "count": 1,
                    }
                ],
                "questions": [
                    {
                        "question_id": question_id,
                        "version_id": version_id,
                        "content_snapshot": snapshot,
                    }
                ],
                "finalized_snapshot": {
                    "subject_id": exam["subject_id"],
                    "question_count": 1,
                    "header": {"school": "CTU", "duration_minutes": 90},
                    "matrix": [
                        {
                            "chapter_id": None,
                            "cognitive_level": "nhan_biet",
                            "difficulty": "de",
                            "count": 1,
                        }
                    ],
                    "questions": [
                        {
                            "question_id": question_id,
                            "version_id": version_id,
                            "content_snapshot": snapshot,
                        }
                    ],
                },
            }
        )
        repository = FakeExamRepository(
            [exam],
            variants=[{"_id": ObjectId(), "exam_id": exam["_id"]}],
        )
        service = ExamService(repository, FakeExamQuestionRepository())

        duplicated = service.duplicate_exam(str(exam["_id"]), owner)
        stored_duplicate = repository.find(duplicated["id"])

        self.assertNotEqual(duplicated["id"], str(exam["_id"]))
        self.assertEqual(duplicated["status"], "DRAFT")
        self.assertEqual(duplicated["variant_count"], 0)
        self.assertEqual(duplicated["questions"][0]["question_id"], str(question_id))
        self.assertEqual(duplicated["matrix"][0]["count"], 1)
        self.assertEqual(stored_duplicate["created_by_user_id"], owner.id)
        self.assertNotIn("finalized_snapshot", stored_duplicate)
        self.assertEqual(exam["status"], "FINALIZED")

    def test_exam_question_pool_uses_server_side_filters(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        chapter_id = ObjectId()

        class RecordingQuestionRepository:
            def __init__(self):
                self.calls = []

            def list(self, page, page_size, review_status, search, **filters):
                self.calls.append(
                    {
                        "page": page,
                        "page_size": page_size,
                        "review_status": review_status,
                        "search": search,
                        "filters": filters,
                    }
                )
                return [], 0

        question_repository = RecordingQuestionRepository()
        service = ExamService(FakeExamRepository([exam]), question_repository)

        result = service.question_pool(
            str(exam["_id"]),
            owner,
            page=3,
            page_size=15,
            search="queue",
            question_type="trac_nghiem",
            bloom_level=2,
            chapter_id=str(chapter_id),
            difficulty="de",
        )

        self.assertEqual(result["page"], 3)
        self.assertEqual(result["page_size"], 15)
        self.assertEqual(result["total"], 0)
        call = question_repository.calls[0]
        self.assertIsNone(call["review_status"])
        self.assertEqual(call["search"], "queue")
        self.assertEqual(call["filters"]["question_type"], "trac_nghiem")
        self.assertEqual(call["filters"]["bloom_level"], 2)
        self.assertEqual(call["filters"]["subject_id"], str(exam["subject_id"]))
        self.assertEqual(call["filters"]["chapter_id"], str(chapter_id))
        self.assertEqual(call["filters"]["difficulty"], "de")
        self.assertEqual(call["filters"]["visible_to_user_id"], owner.id)
        self.assertEqual(call["filters"]["shared_subject_ids"], [str(exam["subject_id"])])
        self.assertTrue(call["filters"]["approved_version_join"])

    def test_exam_question_pool_hides_stale_approved_versions(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)

        def make_pair(current_is_approved):
            question_id = ObjectId()
            version_id = ObjectId()
            question = {
                "_id": question_id,
                "question_code": f"Q-{question_id}",
                "current_version": 1,
                "current_version_id": version_id,
                "approved_version_id": version_id if current_is_approved else ObjectId(),
                "lifecycle_status": "ACTIVE",
                "evaluation_status": "PASSED",
                "review_status": "APPROVED",
                "publication_status": "NOT_PUBLISHED",
                "quality_summary": {},
                "review_assignment": {"status": "UNASSIGNED"},
                "latest_review_id": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            version = {
                "_id": version_id,
                "question_id": question_id,
                "document_id": None,
                "content": "Queue uses FIFO",
                "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
                "classification": {"subject": {"id": exam["subject_id"]}},
                "clos": [],
                "sources": [],
                "content_hash": "hash",
            }
            return question, version

        usable, _usable_version = make_pair(True)
        stale, _stale_version = make_pair(False)
        service = ExamService(
            FakeExamRepository([exam]),
            FakeExamQuestionRepository([
                (usable, _usable_version),
                (stale, _stale_version),
            ]),
        )

        result = service.question_pool(str(exam["_id"]), owner)

        self.assertEqual(result["total"], 1)
        self.assertEqual([item["id"] for item in result["items"]], [str(usable["_id"])])

    @patch("modules.exams.service.ensure_teaches_subjects", new=lambda *args, **kwargs: None)
    def test_exam_keeps_approved_version_while_question_is_edited(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id, approved_id, draft_id = ObjectId(), ObjectId(), ObjectId()
        now = datetime.now(timezone.utc)
        question = {
            "_id": question_id, "question_code": "Q-EDIT", "current_version": 2, "created_by_user_id": owner.id,
            "current_version_id": draft_id, "approved_version_id": approved_id,
            "lifecycle_status": "ACTIVE", "evaluation_status": "NOT_STARTED",
            "review_status": "PENDING", "publication_status": "NOT_PUBLISHED",
            "quality_summary": {}, "review_assignment": {"status": "UNASSIGNED"},
            "latest_review_id": None, "created_at": now, "updated_at": now,
        }
        approved = {
            "_id": approved_id, "question_id": question_id, "version": 1, "document_id": None,
            "content": "Queue uses FIFO", "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
            "classification": {"subject": {"id": exam["subject_id"]}}, "clos": [], "sources": [],
            "content_hash": "hash-v1",
        }
        service = ExamService(FakeExamRepository([exam]), FakeExamQuestionRepository([(question, approved)]))

        pool = service.question_pool(str(exam["_id"]), owner)
        self.assertEqual([item["current_version_id"] for item in pool["items"]], [str(draft_id)])
        self.assertEqual(pool["items"][0]["content"], "Queue uses FIFO")
        updated = service.add_questions_manual(
            str(exam["_id"]), AddQuestionsManualRequest(question_ids=[str(question_id)]), owner,
        )
        self.assertEqual(updated["questions"][0]["version_id"], str(approved_id))
        ready = service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="READY"), owner)
        self.assertEqual(ready["status"], "READY")

        question["approved_version_id"] = None
        with self.assertRaises(ValueError):
            service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="FINALIZED"), owner)

    def test_variant_creation_requires_finalized_exam(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id = ObjectId()
        version_id = ObjectId()
        exam["questions"] = [
            {
                "question_id": question_id,
                "version_id": version_id,
                "content_snapshot": {
                    "content": "Queue uses FIFO",
                    "question_data": {"options": {"A": "FIFO"}, "correct_answer": "A"},
                },
            }
        ]
        exams = FakeExamRepository([exam])
        service = ExamVariantService(exams, FakeExamVariantRepository())

        with self.assertRaises(ValueError):
            service.create_variant(
                str(exam["_id"]),
                ExamVariantCreateRequest(exam_code="A01"),
                owner,
            )

        exam["status"] = "FINALIZED"
        exam["finalized_snapshot"] = {"questions": exam["questions"]}
        result = service.create_variant(
            str(exam["_id"]),
            ExamVariantCreateRequest(exam_code="A01"),
            owner,
        )
        self.assertEqual(result["exam_code"], "A01")

    @staticmethod
    def _approved_exam_question(exam, owner_id, code="Q-1"):
        question_id, version_id = ObjectId(), ObjectId()
        question = {
            "_id": question_id, "question_code": code, "created_by_user_id": owner_id,
            "current_version": 1, "current_version_id": version_id, "approved_version_id": version_id,
            "lifecycle_status": "ACTIVE", "review_status": "APPROVED", "evaluation_status": "PASSED",
            "publication_status": "NOT_PUBLISHED",
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        }
        version = {
            "_id": version_id, "question_id": question_id, "created_by_user_id": owner_id,
            "content": f"{code} content", "content_hash": f"hash-{code}",
            "question_data": {"options": {"A": "x", "B": "y", "C": "z", "D": "w"}, "correct_answer": "A"},
            "classification": {"subject": {"id": exam["subject_id"]}},
        }
        ref = {"question_id": question_id, "version_id": version_id,
               "content_snapshot": {"question_code": code, "content": version["content"],
                                    "question_data": version["question_data"]}}
        return question, version, ref

    def test_reopen_finalized_exam_requires_owner_reason_and_no_variants(self):
        owner = _current_user("Teacher")
        admin = _current_user("Admin")
        exam = _exam_doc(owner.id)
        exam["status"] = "FINALIZED"
        exam["finalized_snapshot"] = {"questions": [], "created_at": datetime.now(timezone.utc)}
        repository = FakeExamRepository([exam])
        service = ExamService(repository, FakeExamQuestionRepository())

        with self.assertRaises(ValueError):
            service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="DRAFT"), owner)
        with self.assertRaises(PermissionError):
            service.reopen_exam(str(exam["_id"]), "Sửa câu 3 sai đáp án", admin)
        repository.variants.append({"_id": ObjectId(), "exam_id": exam["_id"]})
        with self.assertRaises(ValueError):
            service.reopen_exam(str(exam["_id"]), "Sửa câu 3 sai đáp án", owner)
        repository.variants.clear()
        with self.assertRaises(ValueError):
            service.reopen_exam(str(exam["_id"]), "   ", owner)

        reopened = service.reopen_exam(str(exam["_id"]), "Sửa câu 3 sai đáp án", owner)

        self.assertEqual(reopened["status"], "DRAFT")
        self.assertIsNone(exam["finalized_snapshot"])
        self.assertEqual(exam["voided_snapshots"][0]["void_reason"], "Sửa câu 3 sai đáp án")
        self.assertEqual(reopened["reopen_history"][0]["reason"], "Sửa câu 3 sai đáp án")
        self.assertEqual(repository.audit_events[-1]["action"], "exam.reopened")

    def test_publish_needs_a_variant_and_freezes_variants(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        exam["status"] = "FINALIZED"
        exams = FakeExamRepository([exam])
        service = ExamService(exams, FakeExamQuestionRepository())
        variants = FakeExamVariantRepository()
        variant_service = ExamVariantService(exams, variants)

        with self.assertRaises(ValueError):
            service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="PUBLISHED"), owner)
        variant = {"_id": ObjectId(), "exam_id": exam["_id"], "exam_code": "A01", "questions": [],
                   "answer_key": {}, "created_at": datetime.now(timezone.utc)}
        variants.create(variant)
        exams.variants.append(variant)

        published = service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="PUBLISHED"), owner)

        self.assertEqual(published["status"], "PUBLISHED")
        self.assertIsNotNone(published["published_at"])
        self.assertEqual(exams.audit_events[-1]["action"], "exam.published")
        with self.assertRaises(ValueError):
            variant_service.create_variant(str(exam["_id"]), ExamVariantCreateRequest(exam_code="B02"), owner)
        with self.assertRaises(ValueError):
            variant_service.delete_variant(str(exam["_id"]), str(variant["_id"]), owner)
        with self.assertRaises(ValueError):
            service.reopen_exam(str(exam["_id"]), "Phát hiện lỗi", owner)
        archived = service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="ARCHIVED"), owner)
        self.assertEqual(archived["status"], "ARCHIVED")

    def test_draft_exam_item_flags_block_finalize(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        exam["question_count"] = 3
        archived_q, archived_v, archived_ref = self._approved_exam_question(exam, owner.id, "Q-ARC")
        changed_q, changed_v, changed_ref = self._approved_exam_question(exam, owner.id, "Q-VER")
        revoked_q, revoked_v, revoked_ref = self._approved_exam_question(exam, owner.id, "Q-ACC")
        exam["questions"] = [archived_ref, changed_ref, revoked_ref]
        service = ExamService(
            FakeExamRepository([exam]),
            FakeExamQuestionRepository([(archived_q, archived_v), (changed_q, changed_v), (revoked_q, revoked_v)]),
        )
        archived_q["lifecycle_status"] = "ARCHIVED"
        changed_ref["version_id"] = ObjectId()
        other = ObjectId()
        revoked_q["created_by_user_id"] = other
        revoked_v["created_by_user_id"] = other

        flags = {item["question_code"]: item["flags"] for item in service.get_exam(str(exam["_id"]), owner)["item_flags"]}

        self.assertEqual(flags, {"Q-ARC": ["archived_item"], "Q-VER": ["version_changed"],
                                 "Q-ACC": ["access_revoked"]})
        with self.assertRaisesRegex(ValueError, "gắn cờ"):
            service.update_status(str(exam["_id"]), ExamStatusUpdateRequest(status="READY"), owner)
        revoked_q["shared_with_user_ids"] = [owner.id]
        flags = service.get_exam(str(exam["_id"]), owner)["item_flags"]
        self.assertNotIn("Q-ACC", {item["question_code"] for item in flags})

    def test_exam_selection_and_variant_shuffle_reproduce_from_seed(self):
        owner = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        exam["question_count"] = 3
        exam["matrix"] = [{"chapter_id": None, "cognitive_level": "nhan_biet", "difficulty": "de", "count": 3}]
        pairs = []
        for index in range(8):
            question, version, _ref = self._approved_exam_question(exam, owner.id, f"Q-{index}")
            pairs.append((question, version))
        service = ExamService(FakeExamRepository([exam]), FakeExamQuestionRepository(pairs))

        with patch.object(service, "_assert_teaches_subject", lambda *_args: None):
            first = service.auto_generate_pool(str(exam["_id"]), owner, seed=42)
            again = service.auto_generate_pool(str(exam["_id"]), owner, seed=42)
            unseeded = service.auto_generate_pool(str(exam["_id"]), owner)

        self.assertEqual(first["selection_seed"], 42)
        self.assertEqual([item["question_id"] for item in first["questions"]],
                         [item["question_id"] for item in again["questions"]])
        self.assertIsInstance(unseeded["selection_seed"], int)

        exam["status"] = "FINALIZED"
        exam["finalized_snapshot"] = {"questions": exam["questions"]}
        variants = FakeExamVariantRepository()
        variant_service = ExamVariantService(FakeExamRepository([exam]), variants)
        one = variant_service.create_variant(str(exam["_id"]), ExamVariantCreateRequest(exam_code="A", seed=7), owner)
        two = variant_service.create_variant(str(exam["_id"]), ExamVariantCreateRequest(exam_code="B", seed=7), owner)
        self.assertEqual(one["shuffle_seed"], 7)
        self.assertEqual(one["answer_key"], two["answer_key"])
        self.assertEqual([q["question_id"] for q in one["questions"]], [q["question_id"] for q in two["questions"]])

    def test_shared_exam_is_view_only_and_owner_transfer_is_admin_only(self):
        owner = _current_user("Teacher")
        viewer = _current_user("Teacher")
        admin = _current_user("Admin")
        exam = _exam_doc(owner.id)
        repository = FakeExamRepository([exam])
        service = ExamService(repository, FakeExamQuestionRepository())
        active_users = {viewer.id: {"_id": viewer.id, "role": "Teacher"}}

        def fake_users(_db, ids, active_only=False):
            return [active_users[item] for item in ids if item in active_users]

        with patch("modules.exams.service.get_database", return_value=None), \
                patch("modules.exams.service.users_by_ids", side_effect=fake_users):
            with self.assertRaises(LookupError):
                service.get_exam(str(exam["_id"]), viewer)
            with self.assertRaises(ValueError):
                service.update_sharing(str(exam["_id"]), ExamSharingRequest(shared_with_user_ids=[str(ObjectId())]),
                                       owner)
            shared = service.update_sharing(str(exam["_id"]),
                                            ExamSharingRequest(shared_with_user_ids=[str(viewer.id)]), owner)
            self.assertEqual(shared["shared_with_user_ids"], [str(viewer.id)])
            self.assertEqual(repository.audit_events[-1]["action"], "exam.sharing_update")

            view = service.get_exam(str(exam["_id"]), viewer)
            self.assertFalse(view["can_edit"])
            self.assertEqual([item["id"] for item in service.list_exams(1, 20, viewer)["items"]], [str(exam["_id"])])
            with self.assertRaises(PermissionError):
                service.update_exam(str(exam["_id"]), ExamUpdateRequest(name="Đổi tên"), viewer)
            with self.assertRaises(PermissionError):
                service.update_sharing(str(exam["_id"]), ExamSharingRequest(shared_with_user_ids=[]), viewer)

            transfer = ExamOwnerTransferRequest(new_owner_user_id=str(viewer.id), reason="Giảng viên nghỉ hưu")
            with self.assertRaises(PermissionError):
                service.transfer_owner(str(exam["_id"]), transfer, owner)
            moved = service.transfer_owner(str(exam["_id"]), transfer, admin)

        self.assertEqual(moved["created_by_user_id"], str(viewer.id))
        self.assertEqual(moved["shared_with_user_ids"], [])
        self.assertEqual(repository.audit_events[-1]["action"], "exam.owner_transferred")
        self.assertEqual(repository.audit_events[-1]["metadata"]["reason"], "Giảng viên nghỉ hưu")

    def test_variant_preview_enforces_exam_ownership(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        variant = {
            "_id": ObjectId(),
            "exam_id": exam["_id"],
            "exam_code": "A01",
            "questions": [],
            "answer_key": {},
            "created_at": datetime.now(timezone.utc),
        }
        service = ExamVariantService(
            FakeExamRepository([exam]),
            FakeExamVariantRepository([variant]),
        )

        with self.assertRaises(LookupError):
            service.build_preview(str(exam["_id"]), str(variant["_id"]), other_teacher)

    def test_exam_ownership_is_enforced_across_mutation_and_variant_methods(self):
        owner = _current_user("Teacher")
        other_teacher = _current_user("Teacher")
        exam = _exam_doc(owner.id)
        question_id = ObjectId()
        variant = {
            "_id": ObjectId(),
            "exam_id": exam["_id"],
            "exam_code": "A01",
            "questions": [],
            "answer_key": {},
            "created_at": datetime.now(timezone.utc),
        }
        exam_repository = FakeExamRepository([exam], variants=[variant])
        service = ExamService(exam_repository, FakeExamQuestionRepository())
        variant_service = ExamVariantService(exam_repository, FakeExamVariantRepository([variant]))

        exam_method_calls = [
            lambda: service.update_status(
                str(exam["_id"]),
                ExamStatusUpdateRequest(status="READY"),
                other_teacher,
            ),
            lambda: service.update_exam(
                str(exam["_id"]),
                ExamUpdateRequest(name="Edited"),
                other_teacher,
            ),
            lambda: service.delete_exam(str(exam["_id"]), other_teacher),
            lambda: service.save_matrix(
                str(exam["_id"]),
                ExamMatrixRequest(cells=[]),
                other_teacher,
            ),
            lambda: service.matrix_availability(str(exam["_id"]), other_teacher),
            lambda: service.question_pool(str(exam["_id"]), other_teacher),
            lambda: service.auto_generate_pool(str(exam["_id"]), other_teacher),
            lambda: service.add_questions_manual(
                str(exam["_id"]),
                AddQuestionsManualRequest(question_ids=[str(question_id)]),
                other_teacher,
            ),
            lambda: service.remove_question(str(exam["_id"]), str(question_id), other_teacher),
        ]
        variant_method_calls = [
            lambda: variant_service.create_variant(
                str(exam["_id"]),
                ExamVariantCreateRequest(exam_code="B01"),
                other_teacher,
            ),
            lambda: variant_service.list_variants(str(exam["_id"]), other_teacher),
            lambda: variant_service.get_variant(str(exam["_id"]), str(variant["_id"]), other_teacher),
            lambda: variant_service.delete_variant(str(exam["_id"]), str(variant["_id"]), other_teacher),
            lambda: variant_service.build_preview(str(exam["_id"]), str(variant["_id"]), other_teacher),
        ]

        for call in [*exam_method_calls, *variant_method_calls]:
            with self.assertRaises(LookupError):
                call()

    def _exported_exam_fixture(self):
        owner = _current_user("Teacher")
        viewer = _current_user("Teacher")
        exam = {**_exam_doc(owner.id), "status": "FINALIZED", "shared_with_user_ids": [ObjectId(viewer.id)]}
        snapshot = {"content": "Câu 1?", "question_data": {
            "options": {"A": "x", "B": "y"}, "correct_answer": "A", "explanation": "Vì x",
        }}
        variant = {
            "_id": ObjectId(), "exam_id": exam["_id"], "exam_code": "A01",
            "questions": [{"order": 1, "question_id": ObjectId(), "content_snapshot": snapshot}],
            "answer_key": {"q": "A"}, "created_at": datetime.now(timezone.utc),
        }
        exams = FakeExamRepository([exam], variants=[variant])
        variants = FakeExamVariantRepository([variant])
        return owner, viewer, exam, variant, exams, variants

    def test_shared_viewer_gets_watermarked_preview_without_answers(self):
        owner, viewer, exam, variant, exams, variants = self._exported_exam_fixture()
        service = ExamVariantService(exams, variants)

        with self.assertRaises(PermissionError):
            service.export_access(str(exam["_id"]), str(variant["_id"]), "de_dapan", viewer)
        _exam, preview, official = service.export_access(str(exam["_id"]), str(variant["_id"]), "de", viewer)
        self.assertFalse(official)
        question_data = preview["questions"][0]["content_snapshot"]["question_data"]
        self.assertNotIn("correct_answer", question_data)
        self.assertNotIn("explanation", question_data)
        shown = service.get_variant(str(exam["_id"]), str(variant["_id"]), viewer)
        self.assertEqual(shown["answer_key"], {})
        self.assertNotIn("correct_answer", shown["questions"][0]["content_snapshot"]["question_data"])
        self.assertEqual(variant["answer_key"], {"q": "A"})

        _exam, full, official = service.export_access(str(exam["_id"]), str(variant["_id"]), "de_dapan", owner)
        self.assertTrue(official)
        self.assertEqual(full["questions"][0]["content_snapshot"]["question_data"]["correct_answer"], "A")

    def test_official_export_is_recorded_and_blocks_variant_delete(self):
        owner, _viewer, exam, variant, exams, variants = self._exported_exam_fixture()
        service = ExamVariantService(exams, variants)

        service.record_official_export(exam, variant, export_type="de", file_format="pdf", current_user=owner)

        self.assertIsNotNone(exam["official_exported_at"])
        self.assertEqual(exams.audit_events[-1]["action"], "exam.official_exported")
        with self.assertRaisesRegex(ValueError, "xuất bản chính thức"):
            service.delete_variant(str(exam["_id"]), str(variant["_id"]), owner)
        self.assertIn(str(variant["_id"]), variants.variants)

    def test_variant_delete_before_export_is_audited(self):
        owner, _viewer, exam, variant, exams, variants = self._exported_exam_fixture()
        service = ExamVariantService(exams, variants)

        service.delete_variant(str(exam["_id"]), str(variant["_id"]), owner)

        self.assertNotIn(str(variant["_id"]), variants.variants)
        self.assertEqual(variants.deleted_audit["action"], "exam.variant_deleted")

    def test_cannot_deactivate_last_active_admin(self):
        admin_doc = _user_doc("Admin", True)
        service = UserService(FakeUserRepository([admin_doc]), FakeIdentityGateway(), FakeSessions())
        actor = _current_user("Admin", admin_doc["_id"])

        with self.assertRaises(ValueError):
            service.deactivate(str(admin_doc["_id"]), actor)

    def test_cannot_downgrade_last_active_admin(self):
        admin_doc = _user_doc("Admin", True)
        identity = FakeIdentityGateway()
        service = UserService(FakeUserRepository([admin_doc]), identity, FakeSessions())
        actor = _current_user("Admin", admin_doc["_id"])

        with self.assertRaises(ValueError):
            service.update_admin(
                str(admin_doc["_id"]),
                UserAdminUpdateRequest(role="Teacher"),
                actor,
            )
        self.assertEqual(identity.disabled_calls, [])

    def test_user_service_lists_teacher_options_for_review_filters(self):
        teacher = _user_doc("Teacher", True)
        inactive_teacher = _user_doc("Teacher", False)
        reviewer = _user_doc("Reviewer", True)
        service = UserService(
            FakeUserRepository([teacher, inactive_teacher, reviewer]),
            FakeIdentityGateway(),
            FakeSessions(),
        )

        result = service.list_teacher_options()

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {item["id"] for item in result["items"]},
            {str(teacher["_id"]), str(inactive_teacher["_id"])},
        )
        self.assertNotIn(str(reviewer["_id"]), {item["id"] for item in result["items"]})

    def test_user_service_lists_active_reviewer_options_for_assignment(self):
        teacher = _user_doc("Teacher", True)
        reviewer = _user_doc("Reviewer", True)
        inactive_reviewer = _user_doc("Reviewer", False)
        admin = _user_doc("Admin", True)
        service = UserService(
            FakeUserRepository([teacher, reviewer, inactive_reviewer, admin]),
            FakeIdentityGateway(),
            FakeSessions(),
        )

        result = service.list_reviewer_options()

        self.assertEqual(result["total"], 2)
        self.assertEqual(
            {item["id"] for item in result["items"]},
            {str(reviewer["_id"]), str(admin["_id"])},
        )
        self.assertNotIn(str(teacher["_id"]), {item["id"] for item in result["items"]})
        self.assertNotIn(str(inactive_reviewer["_id"]), {item["id"] for item in result["items"]})

    def test_user_service_invites_resets_and_imports_users(self):
        admin = _current_user("Admin")
        repository = FakeUserRepository([])
        identity = FakeIdentityGateway()
        service = UserService(repository, identity, FakeSessions())

        invited = service.invite_user(
            UserInviteRequest(
                email="new.teacher@example.com",
                display_name="New Teacher",
                role="Teacher",
                permissions=["questions.generate", "questions.generate", ""],
            ),
            admin,
        )

        self.assertEqual(invited["user"]["email"], "new.teacher@example.com")
        self.assertIn("questions.generate", invited["user"]["permissions"])
        self.assertEqual(invited["reset_link"], "https://reset.example/new.teacher@example.com")
        self.assertEqual(len(identity.created), 1)

        reset = service.reset_password(invited["user"]["id"], admin)
        self.assertEqual(reset["reset_link"], "https://reset.example/new.teacher@example.com")

        imported = service.import_users(
            UserImportRequest(
                users=[
                    {
                        "email": "reviewer.import@example.com",
                        "display_name": "Reviewer Import",
                        "role": "Reviewer",
                    }
                ]
            ),
            admin,
        )
        self.assertEqual(imported["created"], 1)
        self.assertEqual(imported["failed"], 0)

        # Email đã có: báo lỗi dễ hiểu và không tạo tài khoản Firebase nào.
        created_before = len(identity.created)
        duplicate = service.import_users(
            UserImportRequest(users=[{"email": "NEW.teacher@example.com", "display_name": "Dup", "role": "Teacher"}]),
            admin,
        )
        self.assertEqual(duplicate["failed"], 1)
        self.assertIn("đã có tài khoản", duplicate["items"][0]["error"])
        self.assertEqual(len(identity.created), created_before)

    def test_user_admin_update_persists_fine_grained_permissions(self):
        teacher = _user_doc("Teacher", True)
        identity = FakeIdentityGateway()
        service = UserService(FakeUserRepository([teacher]), identity, FakeSessions())

        updated = service.update_admin(
            str(teacher["_id"]),
            UserAdminUpdateRequest(permissions=["reviews.manage", "reviews.manage", ""]),
            _current_user("Admin"),
        )

        self.assertIn("reviews.manage", updated["permissions"])
        self.assertEqual(identity.update_calls, [])

    def test_catalog_lifecycle_counts_usage_and_blocks_duplicate_codes(self):
        subject_id = ObjectId()
        other_subject_id = ObjectId()
        chapter_id = ObjectId()
        clo_id = ObjectId()
        version_id = ObjectId()
        subject = {
            "_id": subject_id,
            "subject_code": "CTDL",
            "subject_name": "Cấu trúc dữ liệu",
            "description": "",
            "is_active": True,
            "chapters": [
                {
                    "_id": chapter_id,
                    "chapter_code": "CH01",
                    "chapter_name": "Stack",
                    "sequence_no": 1,
                    "is_active": True,
                }
            ],
            "learning_outcomes": [
                {
                    "_id": clo_id,
                    "clo_code": "CLO1",
                    "description": "Hiểu cấu trúc dữ liệu tuyến tính",
                    "target_weight": 0.5,
                    "is_active": True,
                }
            ],
        }
        db = FakeCatalogDatabase(
            subjects=[
                subject,
                {
                    "_id": other_subject_id,
                    "subject_code": "MMT",
                    "subject_name": "Mạng máy tính",
                    "chapters": [],
                    "learning_outcomes": [],
                    "is_active": True,
                },
            ],
            documents=[{"subject_id": subject_id, "chapter_id": chapter_id, "archived_at": None}],
            question_versions=[
                {
                    "_id": version_id,
                    "classification": {
                        "subject": {"id": subject_id},
                        "chapter": {"id": chapter_id},
                    },
                    "clos": [{"id": clo_id}],
                }
            ],
            questions=[
                {
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "current_version_id": version_id,
                }
            ],
            exams=[{"subject_id": subject_id, "matrix": [{"chapter_id": chapter_id}]}],
        )
        service = CatalogService(db)

        created = service.create_subject(
            SubjectPayload(
                subject_code="  HDH  ",
                subject_name="  Hệ điều hành  ",
                description="  Môn cơ sở ngành  ",
            ),
            _current_user("Admin"),
        )
        self.assertEqual(created["subject_code"], "HDH")
        self.assertEqual(created["subject_name"], "Hệ điều hành")
        self.assertIsNotNone(db.subjects.find_one({"subject_code": "HDH"}))
        with self.assertRaises(ValueError):
            service.create_subject(
                SubjectPayload(subject_code="hdh", subject_name="Mã bị trùng"),
                _current_user("Admin"),
            )

        result = service.list_subjects()[0]
        self.assertEqual(result["usage_counts"]["documents"], 1)
        self.assertEqual(result["usage_counts"]["questions"], 1)
        self.assertEqual(result["usage_counts"]["exams"], 1)
        self.assertEqual(result["chapters"][0]["usage_counts"]["documents"], 1)
        self.assertEqual(result["learning_outcomes"][0]["usage_counts"]["questions"], 1)

        with self.assertRaises(ValueError):
            service.add_chapter(
                str(subject_id),
                ChapterPayload(chapter_code="CH01", chapter_name="Duplicate"),
            )
        with self.assertRaises(ValueError):
            service.add_learning_outcome(
                str(subject_id),
                LearningOutcomePayload(clo_code="CLO1", description="Duplicate"),
            )
        with self.assertRaises(ValueError):
            service.update_subject(
                str(subject_id),
                SubjectUpdatePayload(subject_code="MMT"),
            )

        updated = service.update_chapter(
            str(subject_id),
            str(chapter_id),
            ChapterUpdatePayload(chapter_code="CH02", is_active=False),
        )
        self.assertEqual(updated["chapters"][0]["chapter_code"], "CH02")
        self.assertFalse(updated["chapters"][0]["is_active"])

        updated = service.update_learning_outcome(
            str(subject_id),
            str(clo_id),
            LearningOutcomeUpdatePayload(target_weight=0.75, is_active=False),
        )
        self.assertEqual(updated["learning_outcomes"][0]["target_weight"], 0.75)
        self.assertFalse(updated["learning_outcomes"][0]["is_active"])

    def test_changing_code_of_subject_in_use_requires_confirmation(self):
        subject_id = ObjectId()
        now = datetime.now(timezone.utc)
        db = FakeCatalogDatabase(
            subjects=[{
                "_id": subject_id, "subject_code": "CTDL", "subject_name": "Cấu trúc dữ liệu",
                "chapters": [], "learning_outcomes": [], "is_active": True,
                "created_at": now, "updated_at": now,
            }],
            documents=[{"_id": ObjectId(), "subject_id": subject_id, "archived_at": None}],
        )
        service = CatalogService(db)
        with self.assertRaisesRegex(ValueError, "xác nhận"):
            service.update_subject(str(subject_id), SubjectUpdatePayload(subject_code="CTDL2"))
        self.assertEqual(db.subjects.find_one({"_id": subject_id})["subject_code"], "CTDL")

        # Giữ nguyên mã (chỉ đổi tên) không cần xác nhận.
        service.update_subject(str(subject_id), SubjectUpdatePayload(subject_code="CTDL", subject_name="CTDL mới"))
        updated = service.update_subject(
            str(subject_id), SubjectUpdatePayload(subject_code="CTDL2", confirm_code_change=True)
        )
        self.assertEqual(updated["subject_code"], "CTDL2")

    def test_legacy_question_without_submitter_falls_back_to_creator(self):
        creator_id, question_id, version_id = ObjectId(), ObjectId(), ObjectId()
        base = {
            "_id": question_id, "question_code": "Q-OLD", "current_version": 1, "current_version_id": version_id,
            "lifecycle_status": "ACTIVE", "evaluation_status": "NOT_STARTED", "publication_status": "NOT_PUBLISHED",
            "created_by_user_id": creator_id, "review_submission": {},
            "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
        }
        version = {"_id": version_id, "question_id": question_id, "version": 1, "content": "x",
                   "question_data": {}, "classification": {}, "created_by_user_id": creator_id,
                   "content_hash": "hash-old", "created_at": datetime.now(timezone.utc)}
        pending = serialize_question({**base, "review_status": "PENDING"}, version)
        self.assertEqual(pending["submitted_by_user_id"], str(creator_id))
        self.assertEqual(pending["author_user_ids"], [str(creator_id)])
        draft = serialize_question({**base, "review_status": "DRAFT"}, version)
        self.assertIsNone(draft["submitted_by_user_id"])

    def test_ai_model_create_rejects_existing_code_without_overwriting(self):
        db = FakeCatalogDatabase()
        service = CatalogService(db)
        payload = AiModelPayload(
            model_code="qa-model", model_name="Original", runtime="OLLAMA",
            capabilities=["QUESTION_GENERATION"],
        )
        created = service.save_ai_model(payload, create=True)
        self.assertEqual(created["model_name"], "Original")

        duplicate = payload.model_copy(update={"model_name": "Replacement"})
        with self.assertRaisesRegex(ValueError, "Mã mô hình đã tồn tại"):
            service.save_ai_model(duplicate, create=True)
        self.assertEqual(db.ai_models.find_one({"model_code": "qa-model"})["model_name"], "Original")

        updated = service.save_ai_model(duplicate, create=False)
        self.assertEqual(updated["model_name"], "Replacement")
        self.assertEqual(len(db.ai_models.records), 1)

    def test_catalog_mutations_record_actor_and_field_changes(self):
        db = FakeCatalogDatabase()
        service = CatalogService(db)
        admin = _current_user("Admin")
        subject = service.create_subject(SubjectPayload(subject_code="CTDL", subject_name="Cấu trúc dữ liệu"), admin)
        subject_id = subject["id"]
        service.update_subject(subject_id, SubjectUpdatePayload(subject_name="CTDL mới"), admin)
        chapter = service.add_chapter(subject_id, ChapterPayload(chapter_code="CH1", chapter_name="Ngăn xếp"), admin)
        chapter_id = chapter["chapters"][0]["id"]
        service.update_chapter(subject_id, chapter_id, ChapterUpdatePayload(chapter_name="Stack"), admin)
        outcome = service.add_learning_outcome(subject_id, LearningOutcomePayload(clo_code="CLO1", description="Hiểu stack"), admin)
        clo_id = outcome["learning_outcomes"][0]["id"]
        service.update_learning_outcome(subject_id, clo_id, LearningOutcomeUpdatePayload(description="Hiểu queue"), admin)
        service.deactivate_subject(subject_id, admin)

        actions = [item["action"] for item in db.audit_logs.records]
        self.assertEqual(actions, [
            "catalog.subject_create", "catalog.subject_update", "catalog.chapter_create",
            "catalog.chapter_update", "catalog.clo_create", "catalog.clo_update",
            "catalog.subject_deactivate",
        ])
        changed = db.audit_logs.records[1]
        self.assertEqual(changed["actor"]["user_id"], admin.id)
        self.assertEqual(changed["before"]["subject_name"], "Cấu trúc dữ liệu")
        self.assertEqual(changed["after"]["subject_name"], "CTDL mới")
        self.assertEqual(db.audit_logs.records[3]["entity"]["id"], ObjectId(chapter_id))
        self.assertEqual(db.audit_logs.records[5]["entity"]["id"], ObjectId(clo_id))

    def test_teacher_subject_waits_for_admin_approval_and_stays_private(self):
        db = FakeCatalogDatabase()
        service = CatalogService(db)
        owner = _current_user("Teacher")
        other = _current_user("Teacher")
        admin = _current_user("Admin")

        created = service.create_subject(SubjectPayload(subject_code="ML", subject_name="Học máy"), owner)
        self.assertEqual(created["approval_status"], "PENDING_APPROVAL")
        self.assertEqual([item["id"] for item in service.list_subjects(owner)], [created["id"]])
        self.assertEqual(service.list_subjects(other), [])
        self.assertEqual(len(service.list_subjects(admin)), 1)
        with self.assertRaises(LookupError):
            service.update_subject(created["id"], SubjectUpdatePayload(subject_name="Khác"), other)
        with self.assertRaises(PermissionError):
            service.approve_subject(created["id"], owner)

        rejected = service.reject_subject(created["id"], "Trùng học phần có sẵn", admin)
        self.assertEqual(rejected["approval_status"], "REJECTED")
        self.assertEqual(rejected["approval_reason"], "Trùng học phần có sẵn")
        self.assertEqual(service.list_subjects(other), [])
        with self.assertRaises(ValueError):
            service.approve_subject(created["id"], admin)

        resubmitted = service.resubmit_subject(created["id"], owner)
        self.assertEqual(resubmitted["approval_status"], "PENDING_APPROVAL")
        approved = service.approve_subject(created["id"], admin)
        self.assertEqual(approved["approval_status"], "APPROVED")
        self.assertEqual(len(service.list_subjects(other)), 1)

        actions = [item["action"] for item in db.audit_logs.records]
        self.assertEqual(actions, [
            "catalog.subject_create", "catalog.subject_reject",
            "catalog.subject_resubmit", "catalog.subject_approve",
        ])

    def test_admin_created_subject_is_approved_immediately(self):
        service = CatalogService(FakeCatalogDatabase())
        created = service.create_subject(
            SubjectPayload(subject_code="CSDL", subject_name="Cơ sở dữ liệu"), _current_user("Admin"),
        )
        self.assertEqual(created["approval_status"], "APPROVED")

    def test_ai_prompt_and_evaluation_policy_audit_tracks_versions_without_secrets(self):
        db = FakeCatalogDatabase()
        service = CatalogService(db)
        admin = _current_user("Admin")
        model = AiModelPayload(
            model_code="qa-model", model_name="QA", runtime="OLLAMA",
            capabilities=["QUESTION_GENERATION"], config={"api_key": "hidden-secret"},
        )
        service.save_ai_model(model, create=True, viewer=admin)
        service.save_ai_model(model.model_copy(update={"model_name": "QA 2"}), create=False, viewer=admin)
        service.set_ai_model_active(AiModelActivationPayload(model_code="qa-model", is_active=False), admin)
        prompt = PromptTemplatePayload(template_key="system", kind="SYSTEM", name="System", prompt_body="private prompt")
        service.save_prompt_template(prompt, admin)
        service.save_prompt_template(prompt.model_copy(update={"prompt_body": "new private prompt"}), admin)
        service.activate_prompt_template(PromptTemplateActivationPayload(template_key="system", version=1), admin)
        policy = EvaluationPolicyPayload(
            policy_name="QA", weights={"faithfulness": 1.0},
            thresholds={"yellow_min": 0.5, "pass_min": 0.65, "green_min": 0.75},
        )
        service.save_evaluation_policy(policy, admin)
        service.save_evaluation_policy(policy.model_copy(update={"weights": {"faithfulness": 0.8}}), admin)
        service.activate_evaluation_policy(EvaluationPolicyActivationPayload(policy_name="QA", version=1), admin)

        actions = [item["action"] for item in db.audit_logs.records]
        for action in (
            "catalog.ai_model_create", "catalog.ai_model_update", "catalog.ai_model_deactivate",
            "catalog.prompt_create", "catalog.prompt_activate", "catalog.prompt_deactivate",
            "catalog.evaluation_policy_create", "catalog.evaluation_policy_activate",
            "catalog.evaluation_policy_deactivate",
        ):
            self.assertIn(action, actions)
        serialized = str(db.audit_logs.records)
        self.assertNotIn("hidden-secret", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertEqual(actions.count("catalog.prompt_deactivate"), 2)
        self.assertEqual(actions.count("catalog.evaluation_policy_deactivate"), 2)

    def test_prompt_preview_uses_selected_version(self):
        db = FakeCatalogDatabase(prompt_templates=[
            {
                "_id": ObjectId(), "template_key": "system", "version": 1,
                "prompt_body": "SELECTED_SYSTEM_V1", "is_active": False,
            },
            {
                "_id": ObjectId(), "template_key": "system", "version": 2,
                "prompt_body": "ACTIVE_SYSTEM_V2", "is_active": True,
            },
            {
                "_id": ObjectId(), "template_key": "evaluation:question_quality", "version": 1,
                "prompt_body": "SELECTED_EVALUATION", "is_active": True,
            },
        ])
        service = CatalogService(db)
        preview = service.test_prompt_template(PromptTemplateTestPayload(template_key="system", version=1))
        self.assertIn("SELECTED_SYSTEM_V1", preview["rendered_prompt"])
        self.assertNotIn("ACTIVE_SYSTEM_V2", preview["rendered_prompt"])
        self.assertEqual(preview["effective_sources"]["system"], "selected")

        evaluation = service.test_prompt_template(
            PromptTemplateTestPayload(template_key="evaluation:question_quality", version=1)
        )
        self.assertEqual(evaluation["rendered_prompt"], "SELECTED_EVALUATION")
        self.assertEqual(evaluation["preview_mode"], "template")

    def test_evaluation_policy_rejects_out_of_order_thresholds(self):
        weights = {"faithfulness": 1.0}
        valid = {"yellow_min": 0.5, "pass_min": 0.65, "green_min": 0.75}
        self.assertEqual(
            EvaluationPolicyPayload(policy_name="QA", weights=weights, thresholds=valid).thresholds,
            valid,
        )
        with self.assertRaises(ValidationError):
            EvaluationPolicyPayload(
                policy_name="QA", weights=weights,
                thresholds={"yellow_min": 0.7, "pass_min": 0.65, "green_min": 0.75},
            )
        with self.assertRaises(ValidationError):
            EvaluationPolicyPayload(
                policy_name="QA", weights=weights,
                thresholds={"yellow_min": 0.5, "pass_min": 0.8, "green_min": 0.75},
            )

    def test_catalog_runtime_controls_prompt_policy_and_model_state(self):
        db = FakeCatalogDatabase(
            ai_models=[
                {
                    "_id": ObjectId(),
                    "model_code": "qwen",
                    "model_name": "Qwen local",
                    "runtime": "OLLAMA",
                    "kind": "CHAT",
                    "revision": "local",
                    "capabilities": ["QUESTION_GENERATION"],
                    "priority": 1,
                    "is_local": True,
                    "is_active": True,
                    "config": {},
                },
                {
                    "_id": ObjectId(),
                    "model_code": "unknown-provider",
                    "model_name": "Unknown",
                    "runtime": "CUSTOM",
                    "kind": "CHAT",
                    "revision": "local",
                    "capabilities": [],
                    "priority": 2,
                    "is_local": True,
                    "is_active": True,
                    "config": {},
                },
            ],
            prompt_templates=[
                {
                    "_id": ObjectId(),
                    "template_key": "system",
                    "version": 1,
                    "kind": "SYSTEM",
                    "name": "System v1",
                    "prompt_body": "v1",
                    "is_active": False,
                },
                {
                    "_id": ObjectId(),
                    "template_key": "system",
                    "version": 2,
                    "kind": "SYSTEM",
                    "name": "System v2",
                    "prompt_body": "v2",
                    "is_active": True,
                },
            ],
            evaluation_policies=[
                {
                    "_id": ObjectId(),
                    "policy_name": "Default",
                    "version": 1,
                    "weights": {"faithfulness": 1},
                    "thresholds": {"pass_min": 0.7},
                    "is_active": False,
                },
                {
                    "_id": ObjectId(),
                    "policy_name": "Default",
                    "version": 2,
                    "weights": {"faithfulness": 1},
                    "thresholds": {"pass_min": 0.8},
                    "is_active": True,
                },
            ],
        )
        service = CatalogService(db)

        models = service.list_ai_models()
        self.assertTrue(models[0]["factory_status"]["supported"])
        self.assertFalse(models[1]["factory_status"]["supported"])
        available = service.available_ai_models("QUESTION_GENERATION")
        available_codes = [item["code"] for item in available["items"]]
        self.assertIn("qwen", available_codes)
        self.assertNotIn("unknown-provider", available_codes)

        model = service.set_ai_model_active(
            AiModelActivationPayload(model_code="qwen", is_active=False)
        )
        self.assertFalse(model["is_active"])

        prompt = service.activate_prompt_template(
            PromptTemplateActivationPayload(template_key="system", version=1)
        )
        self.assertTrue(prompt["is_active"])
        self.assertFalse(db.prompt_templates.find_one({"template_key": "system", "version": 2})["is_active"])

        policy = service.activate_evaluation_policy(
            EvaluationPolicyActivationPayload(policy_name="Default", version=1)
        )
        self.assertTrue(policy["is_active"])
        self.assertFalse(db.evaluation_policies.find_one({"policy_name": "Default", "version": 2})["is_active"])

        runtime = service.runtime_config()
        self.assertIn("prompt_source", runtime)
        self.assertIn("supported_provider_patterns", runtime)
        if settings.model_provider == "qwen":
            self.assertTrue(
                any("Model sinh c" in warning and "inactive" in warning for warning in runtime["warnings"])
            )

    def test_admin_job_summary_tracks_operational_states(self):
        jobs = [
            {"status": "queued", "is_long_running": False},
            {"status": "PROCESSING", "is_long_running": True},
            {"status": "ERROR", "is_long_running": False},
            {"status": "STALE", "is_long_running": False},
            {"status": "COMPLETED", "is_long_running": False},
        ]

        summary = AdminJobService._summary(jobs)

        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["active"], 2)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["long_running"], 1)

    def test_admin_job_search_matches_entity_and_error(self):
        job = {
            "id": "job-1",
            "kind": "evaluation",
            "type": "Evaluation",
            "status": "ERROR",
            "entity": {"id": "question-1", "label": "Binary tree"},
            "error_message": "Model timeout",
        }

        self.assertTrue(AdminJobService._matches_search(job, "binary"))
        self.assertTrue(AdminJobService._matches_search(job, "timeout"))
        self.assertFalse(AdminJobService._matches_search(job, "moodle"))

    def test_admin_job_rejects_invalid_object_id(self):
        with self.assertRaises(ValueError):
            _parse_object_id("not-an-object-id", "job_id")

    def test_admin_job_status_filters_support_rollups(self):
        self.assertEqual(_generation_status_filter("active"), {"$in": ["queued", "processing"]})
        self.assertEqual(_generation_status_filter("retryable"), {"$in": ["failed"]})
        self.assertEqual(_uppercase_status_filter("active"), {"$in": ["QUEUED", "PROCESSING"]})
        self.assertEqual(_uppercase_status_filter("retryable"), {"$in": ["FAILED", "ERROR", "STALE", "BLOCKED"]})

    def test_admin_audit_list_filters_legacy_and_nested_records(self):
        actor_id = ObjectId()
        entity_id = ObjectId()
        version_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeAuditDatabase:
            def __init__(self):
                self.users = InMemoryCollection([])
                self.audit_logs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "action": "admin.job_cancel",
                            "actor_user_id": str(actor_id),
                            "actor_role": "Admin",
                            "entity_type": "generation",
                            "entity_id": str(entity_id),
                            "before": {"status": "processing"},
                            "after": {"status": "failed"},
                            "metadata": {"reason": "manual cancel"},
                            "created_at": now,
                        },
                        {
                            "_id": ObjectId(),
                            "action": "user.update",
                            "actor": {"user_id": actor_id, "role": "Admin"},
                            "entity": {"type": "user", "id": entity_id, "version_id": version_id},
                            "before": {"role": "Teacher"},
                            "after": {"role": "Reviewer"},
                            "changes": [{"field": "role"}],
                            "created_at": now - timedelta(minutes=5),
                        },
                        {
                            "_id": ObjectId(),
                            "action": "auth.demo_login",
                            "actor_user_id": str(ObjectId()),
                            "actor_role": "Teacher",
                            "entity_type": "auth",
                            "entity_id": "demo",
                            "created_at": now - timedelta(days=2),
                        },
                    ]
                )

        service = AdminAuditService(FakeAuditDatabase())

        actor_result = service.list(page=1, page_size=10, actor_user_id=str(actor_id))
        entity_result = service.list(
            page=1,
            page_size=10,
            entity_type="user",
            entity_id=str(entity_id),
        )
        search_result = service.list(page=1, page_size=10, search="manual cancel")
        date_result = service.list(
            page=1,
            page_size=10,
            date_from=now - timedelta(hours=1),
            date_to=now + timedelta(seconds=1),
        )
        paged = service.list(page=2, page_size=1)

        self.assertEqual(actor_result["total"], 2)
        self.assertEqual([item["action"] for item in actor_result["items"]], ["admin.job_cancel", "user.update"])
        self.assertEqual(entity_result["total"], 1)
        self.assertEqual(entity_result["items"][0]["entity"]["version_id"], str(version_id))
        self.assertEqual(search_result["total"], 1)
        self.assertEqual(search_result["items"][0]["metadata"]["reason"], "manual cancel")
        self.assertEqual(date_result["total"], 2)
        self.assertEqual(paged["items"][0]["action"], "user.update")

    def test_admin_job_list_filters_user_status_and_stale_jobs(self):
        owner_id = ObjectId()
        other_id = ObjectId()
        owner_document_id = ObjectId()
        other_document_id = ObjectId()
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=settings.job_recovery_timeout_minutes + 10)

        class FakeJobDatabase:
            def __init__(self):
                self.users = InMemoryCollection([])
                self.question_versions = InMemoryCollection([])
                self.subjects = InMemoryCollection([])
                self.generation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "processing",
                            "requested_by_user_id": owner_id,
                            "request": {"document_id": str(owner_document_id)},
                            "created_at": old,
                            "updated_at": old,
                        },
                        {
                            "_id": ObjectId(),
                            "status": "failed",
                            "requested_by_user_id": other_id,
                            "request": {"document_id": str(other_document_id)},
                            "error_message": "Model timeout",
                            "created_at": now,
                            "updated_at": now,
                        },
                    ]
                )
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "ERROR",
                            "requested_by_user_id": owner_id,
                            "question_id": ObjectId(),
                            "question_version": 2,
                            "error": {"message": "Evaluator failed"},
                            "queued_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.documents = InMemoryCollection(
                    [
                        {"_id": owner_document_id, "uploaded_by_user_id": owner_id, "title": "Owner PDF"},
                        {"_id": other_document_id, "uploaded_by_user_id": other_id, "title": "Other PDF"},
                    ]
                )
                self.document_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "document_id": owner_document_id,
                            "job_type": "OCR",
                            "status": "PROCESSING",
                            "queued_at": old,
                            "updated_at": old,
                        },
                        {
                            "_id": ObjectId(),
                            "document_id": other_document_id,
                            "job_type": "OCR",
                            "status": "FAILED",
                            "queued_at": now,
                            "updated_at": now,
                        },
                    ]
                )

        service = AdminJobService(FakeJobDatabase())

        active = service.list_jobs(
            page=1,
            page_size=10,
            status="active",
            user_id=str(owner_id),
        )
        stale = service.list_jobs(page=1, page_size=10, stale_only=True)
        retryable = service.list_jobs(page=1, page_size=10, status="retryable")
        recent = service.list_jobs(
            page=1,
            page_size=10,
            date_from=now - timedelta(minutes=1),
            date_to=now + timedelta(seconds=1),
        )

        self.assertEqual(active["summary"]["total"], 2)
        self.assertEqual(active["total"], 2)
        self.assertFalse(active["total_is_estimate"])
        self.assertEqual(active["summary"]["long_running"], 2)
        self.assertEqual({item["kind"] for item in active["items"]}, {"generation", "document"})
        self.assertEqual(stale["summary"]["long_running"], 2)
        self.assertEqual(retryable["summary"]["failed"], 3)
        self.assertEqual(recent["summary"]["total"], 3)
        self.assertEqual(recent["total"], 3)
        self.assertEqual({item["kind"] for item in recent["items"]}, {"generation", "evaluation", "document"})
        with self.assertRaises(ValueError):
            service.list_jobs(
                page=1,
                page_size=10,
                date_from=now + timedelta(days=1),
                date_to=now,
            )

    def test_admin_job_cancel_updates_jobs_questions_and_audit(self):
        admin = _current_user("Admin")
        generation_job_id = ObjectId()
        evaluation_job_id = ObjectId()
        question_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeJobDatabase:
            def __init__(self):
                self.generation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": generation_job_id,
                            "status": "processing",
                            "requested_by_user_id": ObjectId(),
                            "request": {},
                            "created_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": evaluation_job_id,
                            "status": "PROCESSING",
                            "question_id": question_id,
                            "requested_by_user_id": ObjectId(),
                            "queued_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "evaluation_status": "PROCESSING",
                            "quality_summary": {
                                "latest_evaluation_job_id": evaluation_job_id,
                                "overall_score": 0.64,
                                "color": "YELLOW",
                            },
                            "updated_at": now,
                        }
                    ]
                )

        db = FakeJobDatabase()
        audit_events = []
        original_audit = admin_jobs_module.record_audit_event
        try:
            admin_jobs_module.record_audit_event = lambda **kwargs: audit_events.append(kwargs)
            service = AdminJobService(db)
            generation_result = service.cancel_job("generation", str(generation_job_id), admin)
            evaluation_result = service.cancel_job("evaluation", str(evaluation_job_id), admin)
        finally:
            admin_jobs_module.record_audit_event = original_audit

        self.assertEqual(generation_result["job"]["status"], "failed")
        self.assertIn("Cancelled by admin", generation_result["job"]["error_message"])
        self.assertEqual(evaluation_result["job"]["status"], "CANCELLED")
        question = db.questions.find_one({"_id": question_id})
        self.assertEqual(question["evaluation_status"], "NOT_STARTED")
        self.assertNotIn("overall_score", question["quality_summary"])
        self.assertNotIn("color", question["quality_summary"])
        self.assertIn("Cancelled by admin", question["quality_summary"]["error"]["message"])
        self.assertEqual([event["action"] for event in audit_events], ["admin.job_cancel", "admin.job_cancel"])
        self.assertEqual({event["entity_type"] for event in audit_events}, {"generation", "evaluation"})

        legacy = {
            "_id": ObjectId(), "status": "STALE",
            "error": {"message": "Cancelled by admin old@qbankctu.edu.vn"},
            "updated_at": now,
        }
        legacy_db = type("LegacyJobDatabase", (), {"evaluation_jobs": InMemoryCollection([legacy])})()
        legacy_service = AdminJobService(legacy_db)
        self.assertEqual(legacy_service._evaluation_jobs("CANCELLED", None)[0]["status"], "CANCELLED")
        self.assertEqual(legacy_service._evaluation_jobs("STALE", None), [])
        self.assertEqual(legacy_service._evaluation_jobs("retryable", None), [])

    def test_admin_job_retry_evaluation_queues_for_worker_and_audit(self):
        admin = _current_user("Admin")
        requester_id = ObjectId()
        job_id = ObjectId()
        question_id = ObjectId()
        queued_job_id = ObjectId()
        now = datetime.now(timezone.utc)

        class FakeBackgroundTasks:
            def __init__(self):
                self.tasks = []

            def add_task(self, fn, *args, **kwargs):
                self.tasks.append((fn, args, kwargs))

        class FakeWorkflowService:
            calls = []

            def __init__(self, database):
                self.db = database

            def enqueue_auto_evaluation(self, question_id_arg, **kwargs):
                self.__class__.calls.append((question_id_arg, kwargs))
                return {"_id": queued_job_id, "status": "QUEUED"}

        class FakeJobDatabase:
            def __init__(self):
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": job_id,
                            "status": "ERROR",
                            "question_id": question_id,
                            "requested_by_user_id": requester_id,
                            "evaluator_model_code": "qwen",
                            "queued_at": now,
                            "updated_at": now,
                        }
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "current_version": 3,
                        }
                    ]
                )

        db = FakeJobDatabase()
        background_tasks = FakeBackgroundTasks()
        audit_events = []
        original_workflow_service = admin_jobs_module.QuestionWorkflowService
        original_audit = admin_jobs_module.record_audit_event
        try:
            admin_jobs_module.QuestionWorkflowService = FakeWorkflowService
            admin_jobs_module.record_audit_event = lambda **kwargs: audit_events.append(kwargs)
            result = AdminJobService(db).retry_job(
                "evaluation",
                str(job_id),
                background_tasks,
                admin,
            )
        finally:
            admin_jobs_module.QuestionWorkflowService = original_workflow_service
            admin_jobs_module.record_audit_event = original_audit

        self.assertEqual(result["job"]["_id"], str(queued_job_id))
        self.assertEqual(FakeWorkflowService.calls[0][0], str(question_id))
        self.assertEqual(FakeWorkflowService.calls[0][1]["expected_version"], 3)
        self.assertEqual(FakeWorkflowService.calls[0][1]["requested_by_user_id"], requester_id)
        self.assertEqual(FakeWorkflowService.calls[0][1]["evaluator_model_code"], "qwen")
        self.assertEqual(FakeWorkflowService.calls[0][1]["trigger"], "ADMIN_RETRY")
        self.assertEqual(background_tasks.tasks, [])
        self.assertEqual(audit_events[0]["action"], "admin.job_retry")
        self.assertEqual(audit_events[0]["entity_type"], "evaluation")
        self.assertEqual(audit_events[0]["metadata"]["new_job_id"], queued_job_id)
        self.assertFalse(result["already_queued"])

        # Bấm chạy lại lần nữa khi lượt trước còn đang chờ: không tạo lượt mới, không ghi nhật ký lặp.
        db.evaluation_jobs.insert_one(
            {"_id": queued_job_id, "status": "QUEUED", "question_id": question_id, "updated_at": now}
        )
        try:
            admin_jobs_module.QuestionWorkflowService = FakeWorkflowService
            admin_jobs_module.record_audit_event = lambda **kwargs: audit_events.append(kwargs)
            again = AdminJobService(db).retry_job("evaluation", str(job_id), background_tasks, admin)
        finally:
            admin_jobs_module.QuestionWorkflowService = original_workflow_service
            admin_jobs_module.record_audit_event = original_audit
        self.assertTrue(again["already_queued"])
        self.assertEqual(len(audit_events), 1)

    def test_admin_overview_summarizes_operational_state(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=settings.job_recovery_timeout_minutes + 5)
        processing_document_id = ObjectId()
        failed_document_id = ObjectId()

        class FakeOverviewDatabase:
            def __init__(self):
                self.subjects = InMemoryCollection([])
                self.ai_models = InMemoryCollection([])
                self.users = InMemoryCollection(
                    [
                        {"_id": ObjectId(), "role": "Admin", "is_active": True},
                        {"_id": ObjectId(), "role": "Teacher", "is_active": True},
                        {"_id": ObjectId(), "role": "Reviewer", "is_active": True},
                        {"_id": ObjectId(), "role": "Teacher", "is_active": False},
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "lifecycle_status": "ACTIVE",
                            "review_status": "PENDING",
                            "publication_status": "NOT_PUBLISHED",
                            "quality_summary": {"color": "GREEN"},
                        },
                        {
                            "_id": ObjectId(),
                            "lifecycle_status": "ACTIVE",
                            "review_status": "APPROVED",
                            "publication_status": "PUBLISHED",
                            "quality_summary": {"color": "YELLOW"},
                        },
                        {
                            "_id": ObjectId(),
                            "lifecycle_status": "ACTIVE",
                            "review_status": "REJECTED",
                            "publication_status": "FAILED",
                            "quality_summary": {"color": "RED"},
                        },
                        {
                            "_id": ObjectId(),
                            "lifecycle_status": "ARCHIVED",
                            "review_status": "PENDING",
                            "publication_status": "NOT_PUBLISHED",
                        },
                    ]
                )
                self.documents = InMemoryCollection(
                    [
                        {"_id": processing_document_id, "archived_at": None, "status": "PROCESSING"},
                        {"_id": failed_document_id, "archived_at": None, "status": "FAILED"},
                        {"_id": ObjectId(), "archived_at": now, "status": "FAILED"},
                    ]
                )
                self.generation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "failed",
                            "request": {"document_id": "doc-1"},
                            "error_message": "Model timeout",
                            "created_at": old,
                            "updated_at": old,
                        },
                        {
                            "_id": ObjectId(),
                            "status": "processing",
                            "request": {"document_id": "doc-2"},
                            "error_message": None,
                            "created_at": old,
                            "updated_at": old,
                        },
                    ]
                )
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "COMPLETED",
                            "evaluator_model_code": "qwen",
                            "duration_ms": 1200,
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 40,
                                "total_tokens": 140,
                                "cost_usd": 0.012,
                            },
                            "queued_at": now,
                            "started_at": now,
                            "finished_at": now,
                            "updated_at": now,
                        },
                        {
                            "_id": ObjectId(),
                            "status": "ERROR",
                            "evaluator_model_code": "qwen",
                            "duration_ms": 800,
                            "token_usage": {
                                "prompt_tokens": 50,
                                "completion_tokens": 10,
                                "total_tokens": 60,
                            },
                            "billing": {"cost_usd": 0.004},
                            "queued_at": now,
                            "started_at": now,
                            "finished_at": now,
                            "updated_at": now,
                            "error": {"message": "Evaluator failed"},
                        },
                    ]
                )
                self.document_jobs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "document_id": processing_document_id,
                            "job_type": "OCR",
                            "status": "PROCESSING",
                            "queued_at": old,
                            "started_at": old,
                            "updated_at": old,
                        },
                        {
                            "_id": ObjectId(),
                            "document_id": failed_document_id,
                            "job_type": "CHUNK",
                            "status": "FAILED",
                            "queued_at": now,
                            "started_at": now,
                            "finished_at": now,
                            "updated_at": now,
                            "error": {"message": "Chunk failed"},
                        },
                    ]
                )
                self.generation_runs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "COMPLETED",
                            "model": {"model_code": "deepseek"},
                            "execution": {
                                "latency_ms": 2000,
                                "usage": {
                                    "prompt_tokens": 300,
                                    "completion_tokens": 120,
                                    "total_tokens": 420,
                                    "cost_usd": 0.03,
                                },
                            },
                            "created_at": now,
                            "finished_at": now,
                            "updated_at": now,
                        },
                        {
                            "_id": ObjectId(),
                            "status": "FAILED",
                            "model": {
                                "provider": "deepseek",
                                "config": {"input_cost_per_1k": 0.05, "output_cost_per_1k": 0.3},
                            },
                            "execution": {
                                "latency_ms": 4000,
                                "prompt_tokens": 80,
                                "completion_tokens": 20,
                            },
                            "created_at": now,
                            "finished_at": now,
                            "updated_at": now,
                            "error": {"message": "Generation failed"},
                        },
                    ]
                )
                self.moodle_targets = InMemoryCollection(
                    [
                        {"_id": ObjectId(), "site_key": "demo", "is_active": True},
                        {"_id": ObjectId(), "site_key": "old", "is_active": False},
                    ]
                )
                self.moodle_publications = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "status": "PUBLISHED",
                            "publication_mode": "MOCK",
                            "request_payload": {"mock": True},
                        },
                        {"_id": ObjectId(), "status": "FAILED", "request_payload": {"mock": True}},
                    ]
                )
                self.audit_logs = InMemoryCollection(
                    [
                        {
                            "_id": ObjectId(),
                            "action": "admin.job_retry",
                            "actor_user_id": str(ObjectId()),
                            "entity_type": "job",
                            "entity_id": "job-1",
                            "created_at": now,
                        }
                    ]
                )

        overview = AdminOverviewService(FakeOverviewDatabase()).overview()

        self.assertEqual(overview["users"]["active"], 3)
        self.assertEqual(overview["questions"]["pending"], 1)
        self.assertEqual(overview["questions"]["published"], 1)
        self.assertEqual(overview["questions"]["quality"]["green"], 1)
        self.assertEqual(overview["questions"]["quality"]["yellow"], 1)
        self.assertEqual(overview["questions"]["quality"]["red"], 1)
        self.assertEqual(overview["documents"]["failed"], 1)
        self.assertEqual(overview["jobs"]["failed"], 3)
        self.assertEqual(overview["jobs"]["long_running"], 2)
        job_breakdown = {item["key"]: item for item in overview["jobs"]["breakdown"]}
        self.assertEqual(job_breakdown["generation"]["failed"], 1)
        self.assertEqual(job_breakdown["evaluation"]["failed"], 1)
        self.assertEqual(job_breakdown["ocr"]["active"], 1)
        self.assertEqual(job_breakdown["chunk"]["failed"], 1)
        self.assertEqual(overview["moodle"]["active_targets"], 1)
        self.assertEqual(overview["moodle"]["publications"]["simulated"], 2)
        self.assertEqual(overview["moodle"]["publications"]["failed"], 1)
        model_rows = {item["key"]: item for item in overview["model_performance"]}
        self.assertEqual(model_rows["evaluation:qwen"]["error_rate"], 0.5)
        self.assertEqual(model_rows["evaluation:qwen"]["avg_latency_ms"], 1000)
        self.assertEqual(model_rows["evaluation:qwen"]["total_tokens"], 200)
        self.assertEqual(model_rows["evaluation:qwen"]["prompt_tokens"], 150)
        self.assertEqual(model_rows["evaluation:qwen"]["completion_tokens"], 50)
        self.assertEqual(model_rows["evaluation:qwen"]["cost_usd"], 0.016)
        self.assertEqual(model_rows["generation:deepseek"]["error_rate"], 0.5)
        self.assertEqual(model_rows["generation:deepseek"]["avg_latency_ms"], 3000)
        self.assertEqual(model_rows["generation:deepseek"]["total_tokens"], 520)
        self.assertEqual(model_rows["generation:deepseek"]["cost_usd"], 0.04)
        self.assertEqual(overview["model_usage_summary"]["total_requests"], 4)
        self.assertEqual(overview["model_usage_summary"]["total_tokens"], 720)
        self.assertEqual(overview["model_usage_summary"]["cost_usd"], 0.056)
        self.assertEqual(overview["model_usage_summary"]["avg_latency_ms"], 2000)
        self.assertIn("Model timeout", [item["error_message"] for item in overview["recent_jobs"]])
        self.assertEqual(overview["recent_audit"][0]["action"], "admin.job_retry")
        attention = {item["key"]: item for item in overview["attention"]}
        self.assertEqual(attention["retryable_jobs"]["severity"], "danger")
        self.assertEqual(attention["failed_documents"]["count"], 1)
        self.assertEqual(attention["failed_documents"]["path"], "/quan-ly-job?kind=document&status=retryable")

    def test_job_recovery_marks_only_stale_active_jobs(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(minutes=180)
        fresh = now - timedelta(minutes=5)
        question_id = ObjectId()
        evaluation_job_id = ObjectId()
        document_id = ObjectId()
        chunk_job_id = ObjectId()
        chunk_set_id = ObjectId()
        fresh_generation_id = ObjectId()
        stale_generation_id = ObjectId()
        queued_generation_id = ObjectId()

        class FakeRecoveryDatabase:
            def __init__(self):
                self.generation_jobs = InMemoryCollection(
                    [
                        {"_id": stale_generation_id, "status": "processing", "updated_at": old,
                         "lease_expires_at": old, "attempt_count": 1, "max_attempts": 3},
                        {"_id": queued_generation_id, "status": "queued", "updated_at": old},
                        {"_id": fresh_generation_id, "status": "processing", "updated_at": fresh,
                         "lease_expires_at": now + timedelta(minutes=5)},
                    ]
                )
                self.evaluation_jobs = InMemoryCollection(
                    [
                        {
                            "_id": evaluation_job_id,
                            "status": "PROCESSING",
                            "question_id": question_id,
                            "updated_at": old,
                        }
                    ]
                )
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "evaluation_status": "PROCESSING",
                            "quality_summary": {"latest_evaluation_job_id": evaluation_job_id},
                        }
                    ]
                )
                self.document_jobs = InMemoryCollection(
                    [
                        {
                            "_id": chunk_job_id,
                            "document_id": document_id,
                            "job_type": "CHUNK",
                            "status": "PROCESSING",
                            "updated_at": old,
                        }
                    ]
                )
                self.documents = InMemoryCollection(
                    [
                        {
                            "_id": document_id,
                            "archived_at": None,
                            "status": "PROCESSING",
                            "pipeline_summary": {"chunk_status": "PROCESSING"},
                        }
                    ]
                )
                self.chunk_sets = InMemoryCollection(
                    [{"_id": chunk_set_id, "chunk_job_id": chunk_job_id, "status": "PROCESSING"}]
                )
                self.chunk_embeddings = InMemoryCollection(
                    [{"_id": ObjectId(), "chunk_set_id": chunk_set_id, "status": "PENDING"}]
                )

        db = FakeRecoveryDatabase()
        original_get_database = job_recovery.get_database
        try:
            job_recovery.get_database = lambda: db
            result = job_recovery.recover_stale_jobs(timeout_minutes=60)
        finally:
            job_recovery.get_database = original_get_database

        self.assertEqual(
            result,
            {"generation_requeued": 1, "evaluation_stale": 1, "document_failed": 1},
        )
        self.assertEqual(db.generation_jobs.find_one({"_id": fresh_generation_id})["status"], "processing")
        self.assertEqual(db.generation_jobs.find_one({"_id": stale_generation_id})["status"], "queued")
        self.assertEqual(db.generation_jobs.find_one({"_id": queued_generation_id})["status"], "queued")
        self.assertEqual(db.evaluation_jobs.find_one({"_id": evaluation_job_id})["status"], "STALE")
        question = db.questions.find_one({"_id": question_id})
        self.assertEqual(question["evaluation_status"], "STALE")
        self.assertIn("exceeded 60 minute recovery timeout", question["quality_summary"]["error"]["message"])
        document = db.documents.find_one({"_id": document_id})
        self.assertEqual(document["status"], "FAILED")
        self.assertEqual(document["pipeline_summary"]["chunk_status"], "FAILED")
        self.assertEqual(db.chunk_sets.find_one({"_id": chunk_set_id})["status"], "FAILED")
        self.assertEqual(db.chunk_embeddings.find_one({"chunk_set_id": chunk_set_id})["status"], "FAILED")

    def test_admin_audit_service_normalizes_flat_and_nested_records(self):
        class FakeCursor(list):
            def sort(self, *_args):
                return self

            def skip(self, *_args):
                return self

            def limit(self, *_args):
                return self

        class FakeAuditLogs:
            def __init__(self, records):
                self.records = records
                self.match = None

            def count_documents(self, match):
                self.match = match
                return len(self.records)

            def find(self, match):
                self.match = match
                return FakeCursor(self.records)

        class FakeDatabase:
            def __init__(self, records):
                self.users = InMemoryCollection([])
                self.audit_logs = FakeAuditLogs(records)

        now = datetime.now(timezone.utc)
        actor_id = ObjectId()
        question_id = ObjectId()
        review_id = ObjectId()
        records = [
            {
                "_id": ObjectId(),
                "action": "user.admin_update",
                "actor_user_id": str(actor_id),
                "actor_role": "Admin",
                "entity_type": "user",
                "entity_id": "user-1",
                "before": {"role": "Teacher"},
                "after": {"role": "Reviewer"},
                "created_at": now,
            },
            {
                "_id": ObjectId(),
                "action": "QUESTION_APPROVED",
                "actor": {"type": "USER", "user_id": actor_id},
                "entity": {"type": "QUESTION", "id": question_id, "version_id": ObjectId()},
                "changes": [{"path": "review_status", "old_value": "PENDING", "new_value": "APPROVED"}],
                "metadata": {"review_id": review_id},
                "created_at": now,
            },
        ]
        db = FakeDatabase(records)
        service = AdminAuditService(db)

        result = service.list(
            page=1,
            page_size=10,
            actor_user_id=str(actor_id),
            entity_type="question",
            entity_id=str(question_id),
            action="QUESTION_APPROVED",
            search="approved",
        )

        self.assertEqual(result["total"], 2)
        self.assertIn("$and", db.audit_logs.match)
        self.assertEqual(result["items"][0]["actor"]["role"], "Admin")
        self.assertEqual(result["items"][0]["before"], {"role": "Teacher"})
        self.assertEqual(result["items"][1]["actor"]["user_id"], str(actor_id))
        self.assertEqual(result["items"][1]["entity"]["id"], str(question_id))
        self.assertEqual(result["items"][1]["metadata"]["review_id"], str(review_id))

    def test_reviewer_dashboard_summarizes_workload_and_reviews(self):
        def get_path(record, path):
            current = record
            for part in path.split("."):
                if not isinstance(current, dict) or part not in current:
                    return None
                current = current[part]
            return current

        def matches(record, match):
            if "$and" in match:
                return all(matches(record, clause) for clause in match["$and"])
            if "$or" in match:
                return any(matches(record, clause) for clause in match["$or"])
            for key, expected in match.items():
                actual = get_path(record, key)
                if isinstance(expected, dict):
                    if "$exists" in expected and (actual is not None) != expected["$exists"]:
                        return False
                    if "$in" in expected and actual not in expected["$in"]:
                        return False
                    if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                        return False
                    if "$lte" in expected and (actual is None or actual > expected["$lte"]):
                        return False
                    continue
                if actual != expected:
                    return False
            return True

        class FakeCursor(list):
            def sort(self, *_args):
                return self

            def limit(self, count):
                return FakeCursor(self[:count])

        class FakeCollection:
            def __init__(self, records):
                self.records = records

            def count_documents(self, match):
                return sum(1 for record in self.records if matches(record, match))

            def find(self, match=None, *_args):
                match = match or {}
                return FakeCursor([record for record in self.records if matches(record, match)])

        class FakeDatabase:
            def __init__(self, **collections):
                for name, records in collections.items():
                    setattr(self, name, FakeCollection(records))

        now = datetime.now(timezone.utc)
        reviewer_id = ObjectId()
        other_reviewer_id = ObjectId()
        subject_id = ObjectId()
        version_ids = [ObjectId(), ObjectId()]
        base_question = {
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
        }
        db = FakeDatabase(
            questions=[
                {**base_question, "_id": ObjectId(), "review_assignment": {"status": "UNASSIGNED"}},
                {
                    **base_question,
                    "_id": ObjectId(),
                    "review_assignment": {"status": "ASSIGNED", "reviewer_user_id": reviewer_id},
                },
                {
                    **base_question,
                    "_id": ObjectId(),
                    "review_assignment": {
                        "status": "IN_REVIEW",
                        "reviewer_user_id": reviewer_id,
                        "lock_expires_at": now - timedelta(minutes=5),
                    },
                },
                {
                    **base_question,
                    "_id": ObjectId(),
                    "review_assignment": {
                        "status": "IN_REVIEW",
                        "reviewer_user_id": other_reviewer_id,
                        "lock_expires_at": now + timedelta(minutes=30),
                    },
                },
            ],
            question_reviews=[
                {
                    "_id": ObjectId(),
                    "question_version_id": version_ids[0],
                    "reviewer_user_id": reviewer_id,
                    "decision": "APPROVED",
                    "override": {"applied": True},
                    "revision_issues": [],
                    "reviewed_at": now - timedelta(days=1),
                },
                {
                    "_id": ObjectId(),
                    "question_version_id": version_ids[1],
                    "reviewer_user_id": reviewer_id,
                    "decision": "NEEDS_REVISION",
                    "override": {"applied": False},
                    "revision_issues": [{"title": "Sửa đáp án"}],
                    "reviewed_at": now - timedelta(days=2),
                },
            ],
            question_evaluations=[
                {
                    "_id": ObjectId(),
                    "question_version_id": version_ids[0],
                    "passed": True,
                    "created_at": now - timedelta(hours=1),
                },
                {
                    "_id": ObjectId(),
                    "question_version_id": version_ids[1],
                    "passed": True,
                    "created_at": now - timedelta(hours=2),
                },
            ],
            audit_logs=[
                {
                    "action": "QUESTION_APPROVED",
                    "actor": {"user_id": reviewer_id},
                    "metadata": {"review_assignment": {"claimed_at": now - timedelta(hours=2)}},
                    "created_at": now,
                },
                {
                    "action": "QUESTION_NEEDS_REVISION",
                    "actor": {"user_id": reviewer_id},
                    "metadata": {"review_assignment": {"assigned_at": now - timedelta(hours=4)}},
                    "created_at": now,
                },
            ],
            question_versions=[
                {"_id": version_ids[0], "classification": {"subject": {"id": subject_id}}},
                {"_id": version_ids[1], "classification": {"subject": {"id": subject_id}}},
            ],
            subjects=[
                {"_id": subject_id, "subject_code": "CTDL", "subject_name": "Cấu trúc dữ liệu"},
            ],
        )
        dashboard = QuestionWorkflowService(db).review_dashboard(_current_user("Reviewer", reviewer_id))

        self.assertEqual(dashboard["workload"]["pending"], 4)
        self.assertEqual(dashboard["workload"]["unassigned"], 1)
        self.assertEqual(dashboard["workload"]["assigned"], 1)
        self.assertEqual(dashboard["workload"]["in_review"], 2)
        self.assertEqual(dashboard["workload"]["lock_expired"], 1)
        self.assertEqual(dashboard["workload"]["mine"], 2)
        self.assertEqual(dashboard["performance"]["reviews_30d"], 2)
        self.assertEqual(dashboard["performance"]["approval_rate"], 0.5)
        self.assertEqual(dashboard["performance"]["override_count"], 1)
        self.assertEqual(dashboard["performance"]["revision_issues"], 1)
        self.assertEqual(dashboard["performance"]["average_review_hours"], 3.0)
        self.assertEqual(dashboard["calibration"]["sample_size"], 2)
        self.assertEqual(dashboard["calibration"]["agreements"], 1)
        self.assertEqual(dashboard["calibration"]["disagreements"], 1)
        self.assertEqual(dashboard["calibration"]["ai_passed_but_not_approved"], 1)
        self.assertEqual(dashboard["subjects"][0]["label"], "CTDL")
        self.assertEqual(dashboard["subjects"][0]["reviewed"], 2)

    def test_notification_service_creates_and_marks_read(self):
        class FakeCursor(list):
            def sort(self, *_args):
                return self

            def skip(self, count):
                return FakeCursor(self[count:])

            def limit(self, count):
                return FakeCursor(self[:count])

        class FakeUpdateResult:
            def __init__(self, modified_count):
                self.modified_count = modified_count

        class FakeNotifications:
            def __init__(self):
                self.records = []

            @staticmethod
            def _matches(record, query):
                return all(record.get(key) == value for key, value in query.items())

            def insert_one(self, record):
                self.records.append(record)

            def count_documents(self, query):
                return sum(1 for record in self.records if self._matches(record, query))

            def find(self, query):
                return FakeCursor([record for record in self.records if self._matches(record, query)])

            def find_one_and_update(self, query, update, return_document=None):
                for record in self.records:
                    if self._matches(record, query):
                        record.update(update.get("$set", {}))
                        return record
                return None

            def update_many(self, query, update):
                modified = 0
                for record in self.records:
                    if self._matches(record, query):
                        record.update(update.get("$set", {}))
                        modified += 1
                return FakeUpdateResult(modified)

        class FakeDatabase:
            def __init__(self):
                self.notifications = FakeNotifications()

        db = FakeDatabase()
        service = NotificationService(db)
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        question_id = ObjectId()
        version_id = ObjectId()
        notification = service.notify_review_decision(
            question={
                "_id": question_id,
                "question_code": "Q-1",
                "created_by_user_id": teacher.id,
            },
            version={"_id": version_id, "created_by_user_id": teacher.id},
            review={"decision": "NEEDS_REVISION", "note": "Cần sửa đáp án"},
            actor_user_id=reviewer.id,
        )

        self.assertIsNotNone(notification)
        self.assertEqual(notification["type"], "QUESTION_NEEDS_REVISION")
        self.assertEqual(notification["entity"]["id"], str(question_id))
        self.assertEqual(service.unread_count(teacher), 1)
        page = service.list(teacher, 1, 10)
        self.assertEqual(page["total"], 1)
        self.assertFalse(page["items"][0]["is_read"])

        marked = service.mark_read(notification["id"], teacher)
        self.assertTrue(marked["is_read"])
        self.assertEqual(service.unread_count(teacher), 0)

    def test_notification_service_notifies_reviewer_when_question_resubmitted(self):
        teacher = _current_user("Teacher")
        reviewer = _current_user("Reviewer")
        question_id = ObjectId()
        version_id = ObjectId()
        review_id = ObjectId()

        class FakeDatabase:
            def __init__(self):
                self.notifications = InMemoryCollection()
                self.questions = InMemoryCollection(
                    [
                        {
                            "_id": question_id,
                            "schema_version": SCHEMA_VERSION,
                            "lifecycle_status": "ACTIVE",
                            "current_version_id": version_id,
                            "latest_review_id": review_id,
                            "question_code": "Q-2",
                            "created_by_user_id": teacher.id,
                        }
                    ]
                )
                self.question_versions = InMemoryCollection(
                    [{"_id": version_id, "created_by_user_id": teacher.id}]
                )
                self.question_reviews = InMemoryCollection(
                    [
                        {
                            "_id": review_id,
                            "question_id": question_id,
                            "reviewer_user_id": reviewer.id,
                            "decision": "NEEDS_REVISION",
                            "reviewed_at": datetime.now(timezone.utc),
                        }
                    ]
                )

        db = FakeDatabase()
        service = NotificationService(db)

        already_pending = service.notify_question_resubmitted(
            question_id=question_id,
            previous_review_status="PENDING",
            actor_user_id=teacher.id,
        )
        # The usual path: the teacher edits (status becomes DRAFT) and then resubmits.
        notifications = service.notify_question_resubmitted(
            question_id=question_id,
            previous_review_status="DRAFT",
            actor_user_id=teacher.id,
        )

        self.assertEqual(already_pending, [])
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0]["type"], "QUESTION_RESUBMITTED")
        self.assertEqual(notifications[0]["link"], f"/kiem-duyet?questionId={question_id}")
        self.assertEqual(notifications[0]["entity"]["version_id"], str(version_id))
        self.assertEqual(service.unread_count(reviewer), 1)

        db.question_reviews.records[0]["decision"] = "REJECTED"
        rejected = service.notify_question_resubmitted(
            question_id=question_id,
            previous_review_status="DRAFT",
            actor_user_id=teacher.id,
        )
        self.assertIn("sau khi bị từ chối", rejected[0]["title"])

        db.question_reviews.records[0]["decision"] = "APPROVED"
        self.assertEqual(
            service.notify_question_resubmitted(
                question_id=question_id,
                previous_review_status="DRAFT",
                actor_user_id=teacher.id,
            ),
            [],
        )

    def test_question_hash_is_order_independent(self):
        self.assertEqual(
            stable_hash({"content": "Q", "data": {"a": 1, "b": 2}}),
            stable_hash({"data": {"b": 2, "a": 1}, "content": "Q"}),
        )

    def test_document_has_archive_state(self):
        self.assertIn(DocumentStatus.ARCHIVED, set(DocumentStatus))

    def test_services_depend_on_abstractions_via_constructor(self):
        self.assertIn("repository", inspect.signature(DocumentService).parameters)
        self.assertIn("repository", inspect.signature(QuestionService).parameters)
        self.assertIn("references", inspect.signature(QuestionService).parameters)

    def test_auth_and_rag_databases_are_separate(self):
        self.assertEqual(settings.auth_db_name, "NCKH")
        self.assertEqual(settings.rag_db_name, "rag_database")
        self.assertNotEqual(settings.auth_db_name, settings.rag_db_name)

    def test_generation_request_accepts_question_plan(self):
        req = QuestionGenerateRequest(
            document_id="507f1f77bcf86cd799439011",
            bloom_level=BloomLevel.HIEU,
            question_plan=[
                {"question_type": QuestionType.TRAC_NGHIEM, "bloom_level": BloomLevel.HIEU, "num_questions": 3},
                {"question_type": QuestionType.DUNG_SAI, "bloom_level": BloomLevel.VAN_DUNG, "num_questions": 4},
            ],
            instruction="Tập trung vào cây nhị phân tìm kiếm.",
        )

        self.assertEqual(sum(item.num_questions for item in req.effective_plan()), 7)
        self.assertEqual(req.effective_plan()[1].bloom_level, BloomLevel.VAN_DUNG)
        self.assertEqual(req.instruction, "Tập trung vào cây nhị phân tìm kiếm.")

    def test_generation_request_rejects_oversized_plan(self):
        with self.assertRaises(ValidationError):
            QuestionGenerateRequest(
                document_id="507f1f77bcf86cd799439011",
                bloom_level=BloomLevel.HIEU,
                question_plan=[
                    {"question_type": QuestionType.TRAC_NGHIEM, "num_questions": 26},
                    {"question_type": QuestionType.DUNG_SAI, "num_questions": 25},
                ],
            )

    def test_generated_question_can_return_persisted_metadata(self):
        question = GeneratedQuestion(
            question="Cấu trúc dữ liệu nào dùng nguyên tắc FIFO?",
            options={"A": "Stack", "B": "Queue", "C": "Tree", "D": "Graph"},
            correct_answer="B",
            explanation="Queue xử lý phần tử vào trước ra trước.",
            question_type="trac_nghiem",
            bloom_level="2_hieu",
            source_context="Queue là hàng đợi FIFO.",
            question_id="507f1f77bcf86cd799439011",
            question_code="Q-507F1F77BCF86CD799439011",
            current_version=1,
            current_version_id="507f1f77bcf86cd799439012",
            difficulty="trung_binh",
        )

        self.assertEqual(question.question_id, "507f1f77bcf86cd799439011")
        self.assertEqual(question.current_version, 1)
        self.assertEqual(question.difficulty, "trung_binh")

    def test_generation_plan_summary_reports_shortfall(self):
        summary = GenerationPlanSummary(
            plan_index=2,
            question_type="trac_nghiem",
            bloom_level="4_phan_tich",
            requested_count=5,
            parsed_count=5,
            valid_count=4,
            duplicate_count=1,
            saved_count=3,
            skipped_count=2,
            warnings=["Bỏ 1 câu trùng nội dung.", "Lưu thiếu 2 câu so với yêu cầu."],
        )

        self.assertEqual(summary.plan_index, 2)
        self.assertEqual(summary.skipped_count, 2)
        self.assertIn("Lưu thiếu 2 câu so với yêu cầu.", summary.warnings)

    def test_key_validators_require_schema_version(self):
        for collection in ("users", "documents", "questions", "question_versions", "evaluation_jobs", "question_review_drafts", "question_comments", "moodle_targets"):
            required = VALIDATORS[collection]["$jsonSchema"]["required"]
            self.assertIn("schema_version", required)

    def test_reviewer_form_accepts_shared_ai_human_criteria(self):
        payload = ReviewCreateRequest(
            expected_version=1,
            decision="APPROVED",
            review_form={
                "criterion_assessments": [
                    {
                        "key": "faithfulness",
                        "label": "Bám sát nguồn",
                        "rating": "PASS",
                        "source_chunk_id": "chunk-1",
                        "page_number": 3,
                    }
                ]
            },
        )
        criterion = payload.review_form.criterion_assessments[0]
        self.assertEqual(criterion.key, "faithfulness")
        self.assertEqual(criterion.rating, "PASS")

    def test_review_draft_keeps_version_and_structured_payload(self):
        payload = ReviewDraftUpsertRequest(
            expected_version=2,
            decision="NEEDS_REVISION",
            draft={"overallNote": "Cần bổ sung nguồn"},
        )
        self.assertEqual(payload.expected_version, 2)
        self.assertEqual(payload.draft["overallNote"], "Cần bổ sung nguồn")

    def test_user_validator_accepts_reviewer_role(self):
        role_enum = set(VALIDATORS["users"]["$jsonSchema"]["properties"]["role"]["enum"])
        self.assertEqual(role_enum, {role.value for role in RoleEnum})

    def test_question_validator_accepts_ai_draft_status(self):
        review_status_enum = set(
            VALIDATORS["questions"]["$jsonSchema"]["properties"]["review_status"]["enum"]
        )
        self.assertIn("DRAFT", review_status_enum)

    def test_question_validator_accepts_evaluation_queue_statuses(self):
        evaluation_status_enum = set(
            VALIDATORS["questions"]["$jsonSchema"]["properties"]["evaluation_status"]["enum"]
        )
        self.assertIn("QUEUED", evaluation_status_enum)
        self.assertIn("ERROR", evaluation_status_enum)
        self.assertIn("STALE", evaluation_status_enum)

    def test_evaluation_job_validator_tracks_worker_statuses(self):
        status_enum = set(VALIDATORS["evaluation_jobs"]["$jsonSchema"]["properties"]["status"]["enum"])
        self.assertTrue({"QUEUED", "PROCESSING", "COMPLETED", "ERROR", "STALE"}.issubset(status_enum))

    def test_question_validator_allows_aggregate_owner(self):
        question_properties = VALIDATORS["questions"]["$jsonSchema"]["properties"]
        self.assertEqual(question_properties["created_by_user_id"]["bsonType"], ["objectId", "null"])

    def test_question_validator_allows_review_assignment_state(self):
        question_properties = VALIDATORS["questions"]["$jsonSchema"]["properties"]
        self.assertEqual(question_properties["review_assignment"]["bsonType"], "object")

    def test_auth_user_is_minimal_uid_and_token_link(self):
        schema = VALIDATORS["User"]["$jsonSchema"]
        self.assertEqual(set(schema["required"]), {"uid", "token"})
        self.assertEqual(set(schema["properties"]), {"_id", "uid", "token"})
        self.assertFalse(schema["additionalProperties"])

    def test_question_sources_cannot_mix_documents(self):
        first_document_id = ObjectId()
        second_document_id = ObjectId()
        chunks = {
            ObjectId(): {
                "_id": ObjectId(),
                "document_id": first_document_id,
                "chunk_set_id": ObjectId(),
                "content": "A",
                "content_hash": "a",
            },
            ObjectId(): {
                "_id": ObjectId(),
                "document_id": second_document_id,
                "chunk_set_id": ObjectId(),
                "content": "B",
                "content_hash": "b",
            },
        }

        class References:
            def find_chunk(self, chunk_id):
                return chunks.get(chunk_id)

            def find_document(self, _document_id):
                return None

            def find_subject(self, _subject_id):
                return None

        service = QuestionService(repository=None, references=References())
        with self.assertRaises(ValueError):
            service._sources([str(chunk_id) for chunk_id in chunks])

    def test_rag_heading_match_normalizes_vietnamese_text(self):
        normalized_target = _normalize_heading_text("do thi")
        self.assertTrue(
            _heading_matches_target({"heading": "Đồ thị"}, normalized_target)
        )

        normalized_target = _normalize_heading_text("cay nhi phan")
        self.assertTrue(
            _heading_matches_target(
                {"heading_path_text": "Cấu trúc dữ liệu > Cây nhị phân tìm kiếm"},
                normalized_target,
            )
        )
        self.assertFalse(
            _heading_matches_target({"heading": "Hàng đợi"}, normalized_target)
        )

    def test_prompt_includes_target_difficulty_when_requested(self):
        prompt = PromptBuilder().build(
            context="Stack hoạt động theo nguyên tắc LIFO.",
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=1,
            difficulty="kho",
        )

        self.assertIn("TARGET ESTIMATED DIFFICULTY:", prompt)
        self.assertIn("Generate questions whose `difficulty` is exactly `kho`.", prompt)

    def test_normalize_difficulty_accepts_known_labels(self):
        self.assertEqual(_normalize_difficulty("de"), "de")
        self.assertEqual(_normalize_difficulty("Khó"), "kho")
        self.assertEqual(_normalize_difficulty("trung bình"), "trung_binh")
        self.assertIsNone(_normalize_difficulty("siêu khó"))
        self.assertIsNone(_normalize_difficulty(None))

    def test_evaluation_prompt_includes_difficulty_rule(self):
        service = QuestionWorkflowService(database=None)
        prompt, snapshot, _ = service._build_evaluation_prompt(
            {"question_code": "Q-1", "_id": ObjectId()},
            {
                "_id": ObjectId(),
                "document_id": None,
                "content": "Nguyên tắc hoạt động của stack là gì?",
                "question_data": {
                    "options": {"A": "FIFO", "B": "LIFO", "C": "Random", "D": "Hash"},
                    "correct_answer": "B",
                    "explanation": "Stack theo LIFO.",
                },
                "classification": {
                    "assessment_type": "TRAC_NGHIEM",
                    "bloom": {"level": 1, "name": "Nhớ"},
                    "difficulty": "de",
                },
                "clos": [],
                "sources": [],
            },
        )

        self.assertIn("QUY ĐỊNH ĐỘ KHÓ ƯỚC LƯỢNG", prompt)
        self.assertIn("current_difficulty", prompt)
        self.assertIn('"de"', prompt)
        self.assertIn("correct_answer và explanation là khẳng định CHƯA ĐƯỢC TIN CẬY", prompt)
        self.assertIn('"option_checks"', prompt)
        self.assertIn("SUPPORTED | CONTRADICTED | NOT_IN_SOURCE | AMBIGUOUS", prompt)
        self.assertTrue(
            any(part.get("template_key") == "quy_dinh_do_kho" for part in snapshot["template_parts"])
        )

    def test_mcq_validation_rejects_two_option_shape(self):
        error = _check_type_format(
            {
                "question": "Câu hỏi mẫu?",
                "options": {"A": "Một", "B": "Hai"},
                "correct_answer": "A",
            },
            "trac_nghiem",
        )
        self.assertIn("4 lựa chọn", error)

        self.assertIsNone(
            _check_type_format(
                {
                    "question": "Câu hỏi mẫu?",
                    "options": {"A": "Một", "B": "Hai", "C": "Ba", "D": "Bốn"},
                    "correct_answer": "A",
                },
                "trac_nghiem",
            )
        )

    def test_true_false_validation_rejects_incomplete_statement(self):
        error = _check_type_format(
            {
                "question": "Mặt trời là",
                "options": {"A": "Đúng", "B": "Sai"},
                "correct_answer": "A",
            },
            "dung_sai",
        )
        self.assertIn("mệnh đề hoàn chỉnh", error)

        self.assertIsNone(
            _check_type_format(
                {
                    "question": "Mặt Trời là một ngôi sao.",
                    "options": {"A": "Đúng", "B": "Sai"},
                    "correct_answer": "A",
                },
                "dung_sai",
            )
        )

    def test_retry_prompt_reinforces_mcq_option_shape(self):
        prompt = _build_retry_prompt(
            original_prompt="ORIGINAL",
            question_type="trac_nghiem",
            bloom_level="2_hieu",
            missing_count=2,
            validation_errors=["trac_nghiem phải có đúng 4 lựa chọn A/B/C/D"],
            avoid_questions=["Câu đã nhận"],
        )
        self.assertIn('exactly "A", "B", "C", "D"', prompt)
        self.assertIn("Generate exactly 2 additional questions", prompt)

    def test_multi_answer_validation_allows_six_options(self):
        self.assertIsNone(
            _check_type_format(
                {
                    "question": "Câu hỏi mẫu?",
                    "options": {
                        "A": "Một",
                        "B": "Hai",
                        "C": "Ba",
                        "D": "Bốn",
                        "E": "Năm",
                        "F": "Sáu",
                    },
                    "correct_answer": "A, C, F",
                },
                "nhieu_lua_chon",
            )
        )

    def test_multi_answer_validation_rejects_all_options_correct(self):
        error = _check_type_format(
            {
                "question": "Câu hỏi mẫu?",
                "options": {"A": "Một", "B": "Hai", "C": "Ba", "D": "Bốn", "E": "Năm"},
                "correct_answer": "A, B, C, D, E",
            },
            "nhieu_lua_chon",
        )
        self.assertIn("không được chọn tất cả", error)

    def test_retry_prompt_allows_variable_multi_answer_shape(self):
        prompt = _build_retry_prompt(
            original_prompt="ORIGINAL",
            question_type="nhieu_lua_chon",
            bloom_level="4_phan_tich",
            missing_count=3,
            validation_errors=["nhieu_lua_chon phải có 4 đến 6 lựa chọn"],
            avoid_questions=[],
        )
        self.assertIn("4 to 6 consecutive keys", prompt)
        self.assertIn("Generate exactly 3 additional questions", prompt)

    def test_generation_preset_payload_limits_plan_rows(self):
        payload = GenerationPresetPayload(
            name="Ôn tập demo",
            planItems=[
                {"questionTypeId": "mcq", "bloomId": "understand", "count": 2},
            ],
            instruction="Tập trung vào cây nhị phân.",
        )
        self.assertEqual(payload.planItems[0].count, 2)

        with self.assertRaises(ValidationError):
            GenerationPresetPayload(
                name="Sai số lượng",
                planItems=[
                    {"questionTypeId": "mcq", "bloomId": "understand", "count": 11},
                ],
            )

    def test_user_service_caps_generation_presets_per_user(self):
        class FakeUserRepository:
            def __init__(self):
                self.user = {
                    "_id": ObjectId(),
                    "generation_presets": [],
                }

            def find_by_id(self, _user_id):
                return self.user

            def update(self, _user_id, fields):
                self.user.update(fields)
                return self.user

        repository = FakeUserRepository()
        service = UserService(repository=repository, identity=None, sessions=None)
        user_id = str(repository.user["_id"])

        for index in range(13):
            service.save_generation_preset(
                user_id,
                GenerationPresetPayload(
                    name=f"Preset {index}",
                    planItems=[
                        {"questionTypeId": "mcq", "bloomId": "understand", "count": 1},
                    ],
                ),
            )

        presets = service.list_generation_presets(user_id)["items"]
        self.assertEqual(len(presets), 12)
        self.assertEqual(presets[0]["name"], "Preset 12")
        self.assertNotIn("Preset 0", {preset["name"] for preset in presets})

        self.assertTrue(service.delete_generation_preset(user_id, presets[0]["id"]))
        self.assertEqual(len(service.list_generation_presets(user_id)["items"]), 11)

    def test_question_repository_list_pushes_reviewer_filters_to_mongo(self):
        class FakeQuestionsCollection:
            def __init__(self):
                self.pipelines = []

            def aggregate(self, pipeline):
                self.pipelines.append(pipeline)
                if "$group" in pipeline[-1]:
                    return [{"_id": "PENDING", "count": 12}]
                return [
                    {
                        "items": [],
                        "count": [{"total": 0}],
                    }
                ]

        class FakeDatabase:
            def __init__(self):
                self.questions = FakeQuestionsCollection()

        fake_db = FakeDatabase()
        repo = MongoQuestionRepository(fake_db)
        document_id = ObjectId()
        subject_id = ObjectId()
        chapter_id = ObjectId()
        clo_id = ObjectId()
        reviewer_id = ObjectId()
        creator_id = ObjectId()
        waiting_since = datetime.now(timezone.utc) - timedelta(hours=24)
        overdue_at = datetime.now(timezone.utc)
        created_from = datetime.now(timezone.utc) - timedelta(days=30)
        created_to = datetime.now(timezone.utc)
        submitted_from = datetime.now(timezone.utc) - timedelta(days=7)
        submitted_to = datetime.now(timezone.utc)

        pairs, total, status_counts = repo.list(
            2,
            10,
            "PENDING",
            "queue",
            question_type="trac_nghiem",
            bloom_level=3,
            document_id=str(document_id),
            subject_id=str(subject_id),
            chapter_id=str(chapter_id),
            clo_id=str(clo_id),
            difficulty="kho",
            quality_color="green",
            min_score=0.8,
            publication_status="NOT_PUBLISHED",
            evaluation_status="PASSED",
            assignment_status="IN_REVIEW",
            assigned_reviewer_user_id=reviewer_id,
            creator_user_id=creator_id,
            waiting_since=waiting_since,
            overdue_at=overdue_at,
            created_from=created_from,
            created_to=created_to,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            include_status_counts=True,
        )

        self.assertEqual(pairs, [])
        self.assertEqual(total, 0)
        self.assertEqual(status_counts, {"PENDING": 12})
        self.assertEqual(len(fake_db.questions.pipelines), 2)
        pipeline = fake_db.questions.pipelines[0]
        status_counts_pipeline = fake_db.questions.pipelines[1]
        self.assertIsNotNone(pipeline)
        question_match = pipeline[0]["$match"]
        self.assertEqual(question_match["review_status"], "PENDING")
        self.assertNotIn("review_status", status_counts_pipeline[0]["$match"])
        self.assertEqual(question_match["subject_id"], subject_id)
        self.assertEqual(question_match["publication_status"], "NOT_PUBLISHED")
        self.assertEqual(question_match["evaluation_status"], "PASSED")
        self.assertEqual(question_match["review_assignment.status"], "IN_REVIEW")
        self.assertEqual(question_match["review_assignment.reviewer_user_id"], reviewer_id)
        self.assertEqual(question_match["quality_summary.color"], "GREEN")
        self.assertEqual(question_match["quality_summary.overall_score"], {"$gte": 0.8})
        self.assertEqual(question_match["updated_at"], {"$lte": waiting_since})
        self.assertEqual(question_match["review_assignment.lock_expires_at"], {"$lte": overdue_at})
        self.assertEqual(
            question_match["created_at"],
            {"$gte": created_from, "$lte": created_to},
        )
        self.assertEqual(
            question_match["review_submission.submitted_at"],
            {"$gte": submitted_from, "$lte": submitted_to},
        )

        creator_match = pipeline[3]["$match"]
        self.assertIn({"created_by_user_id": creator_id}, creator_match["$or"])
        self.assertIn({"version.created_by_user_id": creator_id}, creator_match["$or"])

        version_match = pipeline[4]["$match"]
        self.assertEqual(version_match["version.classification.assessment_type"], "TRAC_NGHIEM")
        self.assertEqual(version_match["version.classification.bloom.level"], 3)
        self.assertEqual(version_match["version.document_id"], document_id)
        self.assertNotIn("version.classification.subject.id", version_match)
        self.assertEqual(version_match["version.classification.chapter.id"], chapter_id)
        self.assertEqual(version_match["version.clos.id"], clo_id)
        self.assertEqual(version_match["version.classification.difficulty"], "kho")

        search_match = pipeline[5]["$match"]
        self.assertIn({"question_code": {"$regex": "queue", "$options": "i"}}, search_match["$or"])
        self.assertIn({"version.content": {"$regex": "queue", "$options": "i"}}, search_match["$or"])
        facet = pipeline[-1]["$facet"]
        self.assertEqual(facet["items"][1], {"$skip": 10})
        self.assertEqual(facet["items"][2], {"$limit": 10})
        self.assertEqual(
            status_counts_pipeline[-1],
            {"$group": {"_id": "$review_status", "count": {"$sum": 1}}},
        )

    def test_question_repository_can_filter_approved_current_version_in_mongo(self):
        class FakeQuestionsCollection:
            def __init__(self):
                self.pipeline = None

            def aggregate(self, pipeline):
                self.pipeline = pipeline
                return [{"items": [], "count": [{"total": 0}]}]

        class FakeDatabase:
            def __init__(self):
                self.questions = FakeQuestionsCollection()

        fake_db = FakeDatabase()
        repo = MongoQuestionRepository(fake_db)

        repo.list(
            1,
            20,
            "APPROVED",
            None,
            approved_current_only=True,
        )

        self.assertIn(
            {"$match": {"$expr": {"$eq": ["$approved_version_id", "$current_version_id"]}}},
            fake_db.questions.pipeline,
        )


if __name__ == "__main__":
    unittest.main()
