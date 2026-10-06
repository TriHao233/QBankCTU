import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from core.audit import build_audit_event, write_audit_event
from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import get_database, mongo_transaction
from core.dependencies import CurrentUser, effective_permissions, has_permission
from modules.admin.moodle_service import MoodleTargetService
from modules.catalog.postgres_subject_repository import subject_records
from modules.generation.llm.factory import get_llm_execution_snapshot, get_llm_service
from modules.generation.llm.model_registry import EVALUATION_CAPABILITY, resolve_model_snapshot
from modules.generation.prompt_builder import PromptBuilder
from modules.notifications.service import (
    NotificationService,
    safe_notify_review_assigned,
    safe_notify_review_decision,
    safe_notify_review_taken_over,
    safe_notify_secondary_review_pending,
    safe_notify_secondary_review_available,
)
from modules.questions.repository import MongoQuestionRepository, json_safe, object_id, serialize_question, utc_now
from modules.rag.search import get_evaluation_evidence
from modules.questions.review_limits import (
    MIN_TAKEOVER_REASON_LENGTH,
    active_holder_id,
    carried_renewal_count,
    released_assignment,
    review_hold_limit_message,
    review_renewal_limit_message,
)
from modules.questions.review_scope import ReviewerScope, reviewer_can_read
from modules.questions.workflow_schemas import (
    AutoAssignRequest,
    AutoEvaluationRequest,
    EvaluationCreateRequest,
    EvaluationScores,
    MoodlePublicationRequest,
    QuestionCommentCreateRequest,
    QuestionCommentUpdateRequest,
    ReviewAssignmentRequest,
    ReviewCreateRequest,
    ReviewDraftUpsertRequest,
    ReviewPolicyPayload,
    SecondaryReviewRequest,
)
from modules.users.store import (
    active_admin_ids, find_user_record, review_candidate_users, teaching_subject_ids, users_by_ids,
)

DEFAULT_WEIGHTS = {
    "faithfulness": 0.35,
    "contextual_relevancy": 0.20,
    "answer_relevancy": 0.15,
    "bloom_alignment": 0.15,
    "clo_alignment": 0.15,
}
DEFAULT_THRESHOLDS = {"yellow_min": 0.50, "green_min": 0.75, "pass_min": 0.65}
CLO_CRITERION = "clo_alignment"
# Phương án được coi là "có trong nguồn" khi ít nhất 80% từ của nó xuất hiện trong nguồn.
OPTION_TEXT_SUPPORT_MIN = 0.8
# Nhận diện ý "thiếu CLO" trong danh sách cần sửa mà mô hình trả về.
CLO_MENTION_PATTERN = re.compile(r"\bCLO\b|chuẩn đầu ra", re.IGNORECASE)


def not_applicable_criteria(evidence: dict | None) -> set[str]:
    """Tiêu chí không áp dụng cho câu hỏi này (hiện chỉ có CLO khi câu không gắn CLO)."""
    guardrail = (evidence or {}).get("metadata_guardrail") or {}
    return {str(item) for item in guardrail.get("not_applicable") or []} & set(DEFAULT_WEIGHTS)


def effective_weights(policy: dict | None, evidence: dict | None = None) -> dict:
    """Trọng số dùng để tính tổng điểm: bỏ tiêu chí không áp dụng, chia lại theo tỉ lệ."""
    configured = (policy or {}).get("weights") or DEFAULT_WEIGHTS
    weights = {key: configured.get(key, DEFAULT_WEIGHTS[key]) for key in DEFAULT_WEIGHTS}
    skipped = not_applicable_criteria(evidence)
    if not skipped:
        return weights
    kept_total = sum(value for key, value in weights.items() if key not in skipped)
    if kept_total <= 0:
        return weights
    full_total = sum(weights.values())
    return {
        key: 0.0 if key in skipped else value * full_total / kept_total
        for key, value in weights.items()
    }
EVALUATION_PROMPT_KEY = "evaluation:question_quality"
EVALUATION_PROMPT_PATH = "evaluation/question_quality.txt"
EVALUATION_SCORING_PROMPT_KEY = "evaluation:scoring_policy"
EVALUATION_SCORING_PROMPT_PATH = "evaluation/scoring_policy.txt"
EVALUATION_OUTPUT_PROMPT_KEY = "evaluation:output_contract"
EVALUATION_OUTPUT_PROMPT_PATH = "evaluation/output_contract.txt"
DIFFICULTY_RULE_PROMPT_KEY = "quy_dinh_do_kho"
DIFFICULTY_RULE_PROMPT_PATH = "quy_dinh_do_kho.txt"
EVALUATION_TYPE_PROMPT_PREFIX = "evaluation:question_type"
EVALUATION_TYPE_PROMPT_DIR = "evaluation/question_type"
DEFAULT_EVALUATOR_MODEL_CODE = settings.evaluation_model_provider
EVALUATION_ACTIVE_STATUSES = {"QUEUED", "PROCESSING"}
MAX_MANUAL_EVALUATIONS_PER_VERSION = 3
# Ai có thể kiểm duyệt được xác định bằng quyền reviews.manage (không chỉ theo vai trò).
REVIEWER_CANDIDATE_FILTER = [
    {"role": {"$in": ["Reviewer", "Admin"]}},
    {"permission_grants": "reviews.manage"},
    {"permissions": "reviews.manage"},
]
REVIEWER_PERMISSION_FIELDS = {"role": 1, "permission_grants": 1, "permission_revokes": 1, "permissions": 1}


def _user_can_review(user: dict) -> bool:
    return bool(user.get("is_active", True)) and "reviews.manage" in effective_permissions(user)


def secondary_review_recipients(users: list[dict], *, question: dict, version: dict, primary_reviewer_user_id) -> list:
    """Prefer subject specialists, excluding all authors and the primary reviewer."""
    excluded = {str(primary_reviewer_user_id), str(question.get("created_by_user_id")), str(version.get("created_by_user_id"))}
    excluded.update(str(item) for item in question.get("author_user_ids") or [])
    excluded.add(str((question.get("review_submission") or {}).get("submitted_by_user_id")))
    eligible = [user for user in users if str(user["_id"]) not in excluded and _user_can_review(user)]
    subject = (version.get("classification") or {}).get("subject") or {}
    subject_id = str(question.get("subject_id") or (subject.get("id") if isinstance(subject, dict) else subject) or "")
    # Only reviewers assigned to the subject can claim the second review.
    specialists = [
        user for user in eligible
        if user.get("role") != "Admin"
        and subject_id in {str(item) for item in user.get("review_subject_ids") or []}
    ]
    recipients = specialists or [user for user in eligible if user.get("role") == "Admin"]
    return list(dict.fromkeys(user["_id"] for user in sorted(recipients, key=lambda item: str(item["_id"]))))[:20]


POLICY_AUDIT_KEYS = ("secondary_on_override", "secondary_below_score", "secondary_subject_ids")


def _valid_ai_score(question: dict) -> float | None:
    """Điểm AI chỉ có giá trị khi AI đã có kết luận hợp lệ cho phiên bản hiện tại.

    Khớp với giao diện: khi AI chưa chấm, đang chấm, bị dừng hoặc lỗi thì không
    hiển thị điểm, nên cũng không dùng điểm cũ để áp chính sách duyệt vòng 2.
    """
    if question.get("evaluation_status") not in {"PASSED", "FAILED"}:
        return None
    summary = question.get("quality_summary") or {}
    if summary.get("error"):
        return None
    score = summary.get("overall_score")
    return score if isinstance(score, (int, float)) else None
EVALUATION_RETRYABLE_STATUSES = {
    "NOT_STARTED",
    "FAILED",
    "ERROR",
    "STALE",
    "INSUFFICIENT_EVIDENCE",
    "EVIDENCE_VALIDATION_FAILED",
}
EVALUATION_SOURCE_LIMIT = 3
EVALUATION_RETRIEVAL_LIMIT = 5
EVALUATION_MIN_SIMILARITY = 0.20
EVALUATION_PROMPT_SOURCE_LIMIT = 3
# Reviewer health flags on the Admin dashboard only fire once a reviewer has
# enough decisions in the 30-day window for the rate to mean something.
REVIEWER_FLAG_MIN_REVIEWS = 5
HIGH_OVERRIDE_RATE = 0.3
HIGH_BULK_RATE = 0.5
SLA_REMINDER_BATCH_SIZE = 200
REVIEW_POLICY_ID = "review_policy"
SUBJECT_SUGGESTION_WINDOW_DAYS = 180
OPTION_CHECK_VERDICTS = {"SUPPORTED", "CONTRADICTED", "NOT_IN_SOURCE", "AMBIGUOUS"}
SINGLE_ANSWER_TYPES = {"TRAC_NGHIEM", "DUNG_SAI"}
MULTIPLE_ANSWER_TYPES = {"NHIEU_LUA_CHON"}
NON_OPTION_ANSWER_TYPES = {"DIEN_KHUYET", "GHEP_COT", "SAP_XEP", "TINH_HUONG"}
NEGATIVE_QUESTION_PATTERN = re.compile(
    r"\b(không\s+(?:phải|đúng|chính\s+xác|phù\s+hợp)|chưa\s+đúng|ngoại\s+trừ|sai)\b"
    r"|\b(?:nào|đâu)\b[^?.]{0,80}\bkhông\b",
    flags=re.IGNORECASE,
)
MOODLE_MOCK_STATUS_DETAIL = "SIMULATED_LOCAL_RECORD"
MOODLE_MOCK_MESSAGE = (
    "Mô phỏng Moodle: hệ thống chỉ ghi nhận publication cục bộ kèm payload "
    "export GIFT/XML, chưa gửi dữ liệu sang Moodle thật."
)
EVALUATION_SOURCE_EXCERPT_CHARS = 700
MISSING_ANSWER_CITATION_CLAIM = "Đáp án đúng chưa có citation SUPPORTED từ nguồn dữ liệu"
# Local models often fill unsupported_claims with a "nothing to report" sentence
# instead of leaving the list empty; those must not block approval.
PLACEHOLDER_CLAIM_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:không\s+có|không\s+phát\s+hiện)"
    r"(?:\s+(?:nhận\s+định|vấn\s+đề|lỗi|nội\s+dung)\b[^.]*\bnào\b"
    r"(?![^.]*\b(?:nhưng|tuy|trừ)\b)[^.]*)?"
    r"|none|n/?a|không"
    r")\s*\.?\s*$",
    flags=re.IGNORECASE,
)
EVALUATION_RETRY_INSTRUCTION = """
LẦN THỬ LẠI: Phản hồi trước không phải JSON hoàn chỉnh hoặc chưa kiểm tra đủ các phương án.
- Chỉ trả về đúng một object JSON và phải đóng đủ mọi dấu ngoặc.
- summary, reasoning và answer_diagnostics: tối đa 120 ký tự mỗi trường.
- supporting_excerpt của từng option: tối đa 18 từ, không lặp lại đoạn nguồn dài.
- option_checks phải có đủ từng phương án; mỗi mục có đủ key, verdict, source_label và supporting_excerpt.
- Không thêm trường ngoài schema OUTPUT.
""".strip()
evaluation_semaphore = asyncio.Semaphore(1)
logger = logging.getLogger(__name__)

REVIEW_CRITERION_KEYS = tuple(DEFAULT_WEIGHTS)
LEGACY_REVIEW_CRITERION_MAP = {
    "source_alignment": "faithfulness",
    "answer_correctness": "answer_relevancy",
    "bloom_clo_alignment": "bloom_alignment",
}


def _is_thinking_evaluator(snapshot: dict) -> bool:
    parameters = snapshot.get("parameters") or {}
    if parameters.get("think") is False:
        return False
    model_name = str(snapshot.get("model_name") or "").strip().lower()
    return parameters.get("think") is True or model_name.startswith("deepseek-r1")


def _limit_evaluation_output(snapshot: dict | None) -> dict | None:
    if not snapshot or str(snapshot.get("runtime") or "").upper() != "OLLAMA":
        return snapshot
    parameters = dict(snapshot.get("parameters") or {})
    if _is_thinking_evaluator(snapshot):
        configured = int(parameters.get("num_predict") or 0)
        parameters["num_predict"] = max(configured, settings.evaluation_thinking_num_predict)
        parameters["num_ctx"] = max(
            int(parameters.get("num_ctx") or 0),
            settings.evaluation_thinking_num_ctx,
        )
        return {**snapshot, "parameters": parameters}
    model_code = str(snapshot.get("model_code") or "").strip().lower()
    if model_code != "qwen3-8b":
        return snapshot
    configured = int(parameters.get("num_predict") or settings.evaluation_num_predict)
    parameters["num_predict"] = min(configured, settings.evaluation_num_predict)
    return {**snapshot, "parameters": parameters}


def _prepare_evaluation_attempt(
    prompt: str,
    prompt_snapshot: dict,
    model_snapshot: dict | None,
    attempt: int,
) -> tuple[str, dict, dict | None]:
    if attempt <= 1:
        return prompt, prompt_snapshot, model_snapshot

    retry_prompt = f"{prompt}\n\n{EVALUATION_RETRY_INSTRUCTION}"
    retry_prompt_snapshot = {
        **prompt_snapshot,
        "rendered_prompt_hash": hashlib.sha256(retry_prompt.encode("utf-8")).hexdigest(),
        "rendered_prompt_chars": len(retry_prompt),
        "retry_attempt": attempt,
        "retry_instruction_applied": True,
    }
    if not model_snapshot or str(model_snapshot.get("runtime") or "").upper() != "OLLAMA":
        return retry_prompt, retry_prompt_snapshot, model_snapshot

    effective_model_snapshot = {**model_snapshot}
    parameters = dict(model_snapshot.get("parameters") or {})
    configured = int(parameters.get("num_predict") or 0)
    parameters["num_predict"] = max(configured, settings.evaluation_retry_num_predict)
    effective_model_snapshot["parameters"] = parameters
    return retry_prompt, retry_prompt_snapshot, effective_model_snapshot


class EvidenceGateError(ValueError):
    def __init__(self, message: str, *, code: str = "INSUFFICIENT_EVIDENCE", evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


class EvaluationInterruptedError(Exception):
    """Tác vụ AI đánh giá đã bị dừng (ví dụ người duyệt đã chốt kết quả) nên bỏ kết quả."""


def _empty_review_assignment(now=None, reason: str | None = None) -> dict:
    return {
        "status": "UNASSIGNED",
        "reviewer_user_id": None,
        "assigned_by_user_id": None,
        "assigned_at": None,
        "claimed_at": None,
        "lock_expires_at": None,
        "last_released_at": now,
        "release_reason": reason,
    }


def _as_aware_utc(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def notification_outbox() -> list[dict] | None:
    """Collect notifications to write in the same PostgreSQL transaction.

    Returns None when questions or notifications are not both in PostgreSQL;
    callers then notify after the change as before.
    """
    if settings.question_store == "postgres" and settings.notification_store == "postgres":
        return []
    return None


class QuestionWorkflowService:
    def __init__(self, database):
        self.db = database
        self.evaluation_jobs = None
        if settings.question_store == "postgres":
            from modules.questions.postgres_evaluation_jobs import PostgresEvaluationJobs
            from modules.questions.postgres_repository import PostgresQuestionRepository
            self.questions = PostgresQuestionRepository()
            self.evaluation_jobs = PostgresEvaluationJobs()
        else:
            self.questions = MongoQuestionRepository(database)

    def _pair(self, question_id: str) -> tuple[dict, dict]:
        pair = self.questions.find_pair(question_id)
        if not pair:
            raise LookupError("Không tìm thấy câu hỏi")
        return pair

    @staticmethod
    def _owns_question(question: dict, version: dict, user_id: ObjectId) -> bool:
        """Only the question owner; version authors and document uploaders gain nothing."""
        owner = question.get("created_by_user_id")
        if owner is not None:
            return owner == user_id
        return version.get("created_by_user_id") == user_id

    @staticmethod
    def _can_review_all(current_user: CurrentUser) -> bool:
        return has_permission(current_user, "reviews.manage")

    @staticmethod
    def _can_manage_all(current_user: CurrentUser) -> bool:
        return current_user.role == "Admin" or has_permission(current_user, "questions.manage_all")

    def _is_shared_question(self, question: dict, current_user: CurrentUser, version: dict | None = None) -> bool:
        if not has_permission(current_user, "questions.use_shared_bank"):
            return False
        shared_with = set(question.get("shared_with_user_ids") or [])
        if current_user.id in shared_with:
            return True
        if question.get("shared_scope") != "SUBJECT":
            return False
        subject_id = question.get("subject_id") or (
            ((version or {}).get("classification") or {}).get("subject") or {}
        ).get("id")
        return bool(subject_id) and str(subject_id) in teaching_subject_ids(self.db, current_user.id)

    def _ensure_read_access(
        self,
        question: dict,
        version: dict,
        current_user: CurrentUser | None,
    ) -> None:
        if not current_user or self._can_manage_all(current_user):
            return
        if self._owns_question(question, version, current_user.id) or self._is_shared_question(
            question, current_user, version,
        ):
            return
        if self._can_review_all(current_user):
            record = find_user_record(self.db, current_user.id) or {}
            scope = ReviewerScope(current_user.id, record.get("review_subject_ids") or [])
            if not reviewer_can_read(
                question, version, scope,
                lambda: self._reviewer_has_decided(question["_id"], current_user.id),
            ):
                # Out-of-scope questions look missing rather than forbidden.
                raise LookupError("Không tìm thấy câu hỏi")
            return
        raise LookupError("Không tìm thấy câu hỏi")

    def _reviewer_has_decided(self, question_id, reviewer_user_id) -> bool:
        if settings.question_store == "postgres":
            return self.questions.reviewer_has_decided(question_id, reviewer_user_id)
        if self.db is None:
            return False
        return self.db.question_reviews.find_one(
            {"question_id": question_id, "reviewer_user_id": reviewer_user_id}, {"_id": 1},
        ) is not None

    def _policy(self) -> dict:
        if settings.ai_config_store == "postgres":
            from modules.catalog.postgres_ai_repository import PostgresAiRepository
            return PostgresAiRepository().policy(active_only=True) or {
                "_id": None, "policy_name": "Default fallback", "version": 1,
                "weights": DEFAULT_WEIGHTS, "thresholds": DEFAULT_THRESHOLDS,
            }
        if self.db is None:
            return {
                "_id": None,
                "policy_name": "Default fallback",
                "version": 1,
                "weights": DEFAULT_WEIGHTS,
                "thresholds": DEFAULT_THRESHOLDS,
            }
        return self.db.evaluation_policies.find_one(
            {"is_active": True},
            sort=[("version", -1)],
        ) or {
            "_id": None,
            "policy_name": "Default fallback",
            "version": 1,
            "weights": DEFAULT_WEIGHTS,
            "thresholds": DEFAULT_THRESHOLDS,
        }

    def _model_snapshot(self, model_code: str) -> dict:
        if settings.ai_config_store == "postgres":
            from modules.catalog.postgres_ai_repository import PostgresAiRepository
            model = PostgresAiRepository().model(model_code)
            if model and not model["is_active"]:
                raise ValueError("Mô hình AI này đang tạm dừng")
        else:
            model = self.db.ai_models.find_one({"model_code": model_code, "is_active": True})
        if not model:
            return {
                "id": None,
                "model_code": model_code,
                "model_name": model_code,
                "config": {},
            }
        return {
            "id": model["_id"],
            "model_code": model["model_code"],
            "model_name": model.get("model_name"),
            "runtime": model.get("runtime"),
            "revision": model.get("revision"),
            "capabilities": model.get("capabilities") or [],
            "config": model.get("config") or {},
        }

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = (text or "").lower()
        return {
            token
            for token in re.findall(r"[\wÀ-ỹ]+", normalized, flags=re.UNICODE)
            if len(token) >= 3
        }

    @classmethod
    def _overlap_score(cls, text: str, context: str) -> float:
        text_tokens = cls._tokens(text)
        if not text_tokens:
            return 0.0
        context_tokens = cls._tokens(context)
        if not context_tokens:
            return 0.0
        return len(text_tokens & context_tokens) / len(text_tokens)

    @staticmethod
    def _is_negative_selection_question(question_type: str, question: str) -> bool:
        if question_type == "DUNG_SAI":
            return False
        return bool(NEGATIVE_QUESTION_PATTERN.search(question or ""))

    @staticmethod
    def _clamp(score: float) -> float:
        return round(max(0.0, min(1.0, score)), 4)

    @staticmethod
    def _clean_json(text: str) -> str:
        cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
        cleaned = re.sub(r"```json|```", "", cleaned).strip()
        match = re.search(r"(\{.*\})", cleaned, flags=re.DOTALL)
        return match.group(1) if match else cleaned

    @staticmethod
    def _compact_text(text: str, limit: int) -> str:
        compacted = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(compacted) <= limit:
            return compacted
        return compacted[: max(0, limit - 3)].rstrip() + "..."

    @classmethod
    def _compact_sources(
        cls,
        version: dict,
        *,
        max_sources: int = EVALUATION_SOURCE_LIMIT,
        excerpt_chars: int = EVALUATION_SOURCE_EXCERPT_CHARS,
    ) -> list[dict]:
        sources = sorted(
            version.get("sources") or [],
            key=lambda source: source.get("citation_order") or 999,
        )
        compacted = []
        for index, source in enumerate(sources[:max_sources], start=1):
            excerpt = cls._compact_text(source.get("context_excerpt") or "", excerpt_chars)
            compacted.append(
                {
                    "label": f"S{index}",
                    "chunk_id": source.get("chunk_id"),
                    "content_hash": source.get("chunk_content_hash"),
                    "citation_order": source.get("citation_order") or index,
                    "is_primary": bool(source.get("is_primary")),
                    "excerpt": excerpt,
                    "excerpt_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                }
            )
        return compacted

    @classmethod
    def _evaluation_query(cls, version: dict) -> str:
        question_data = version.get("question_data") or {}
        classification = version.get("classification") or {}
        parts = [
            version.get("content") or "",
            json.dumps(question_data.get("options") or {}, ensure_ascii=False, default=str),
            str(question_data.get("correct_answer") or ""),
            str(question_data.get("explanation") or ""),
            json.dumps(classification.get("bloom") or {}, ensure_ascii=False, default=str),
            " ".join(
                str(clo.get("description") or clo.get("code") or "")
                for clo in (version.get("clos") or [])
            ),
        ]
        return cls._compact_text(" ".join(part for part in parts if part), 3000)

    def _retrieve_evaluation_sources(self, version: dict) -> tuple[list[dict], dict]:
        document_id = version.get("document_id")
        if not document_id:
            raise EvidenceGateError(
                "Câu hỏi chưa gắn tài liệu nguồn nên AI không được phép chấm",
                evidence={"retrieval": {"status": "BLOCKED", "result_count": 0}},
            )

        query = self._evaluation_query(version)
        try:
            retrieval = get_evaluation_evidence(
                str(document_id),
                query,
                settings.chromadb_collection_name,
                limit=EVALUATION_RETRIEVAL_LIMIT,
                preferred_chunk_ids=[
                    str(source.get("chunk_id"))
                    for source in (version.get("sources") or [])
                    if source.get("chunk_id")
                ],
            )
        except Exception as exc:
            raise EvidenceGateError(
                f"Không truy xuất được nguồn dữ liệu hiện hành: {exc}",
                evidence={
                    "retrieval": {
                        "status": "FAILED",
                        "query": query,
                        "result_count": 0,
                    }
                },
            ) from exc

        retrieved = retrieval.get("results") or []
        eligible = [
            source
            for source in retrieved
            if source.get("excerpt")
            and (
                source.get("similarity") is None
                or float(source.get("similarity")) >= EVALUATION_MIN_SIMILARITY
            )
        ]
        retrieval_snapshot = {
            **retrieval,
            "status": "SUFFICIENT" if eligible else "INSUFFICIENT",
            "result_count": len(retrieved),
            "eligible_count": len(eligible),
            "min_similarity": EVALUATION_MIN_SIMILARITY,
            "results": [
                {key: value for key, value in source.items() if key != "excerpt"}
                for source in retrieved
            ],
        }
        if not eligible:
            raise EvidenceGateError(
                "Không tìm thấy chunk hiện hành đủ liên quan để AI chấm điểm",
                evidence={"retrieval": retrieval_snapshot},
            )

        sources = []
        for index, source in enumerate(eligible, start=1):
            sources.append(
                {
                    **source,
                    "label": f"S{index}",
                    "citation_order": index,
                    "is_primary": index == 1,
                    "excerpt_hash": hashlib.sha256(
                        source["excerpt"].encode("utf-8")
                    ).hexdigest(),
                }
            )
        return sources, retrieval_snapshot

    @staticmethod
    def _normalize_quote(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "")).strip().casefold()

    @classmethod
    def _validate_model_evidence(cls, evidence: dict, sources: list[dict]) -> dict:
        citations = evidence.get("citations")
        if not isinstance(citations, list) or not citations:
            raise EvidenceGateError(
                "AI không trả về citation có cấu trúc nên kết quả không được ghi nhận",
                code="EVIDENCE_VALIDATION_FAILED",
            )

        source_by_id = {str(source.get("chunk_id")): source for source in sources}
        verified = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            chunk_id = str(citation.get("chunk_id") or "")
            source = source_by_id.get(chunk_id)
            quote = cls._normalize_quote(citation.get("exact_quote"))
            if not source or len(quote) < 8:
                continue
            source_text = cls._normalize_quote(source.get("excerpt"))
            quote_tokens = cls._tokens(quote)
            source_tokens = cls._tokens(source_text)
            token_coverage = (
                len(quote_tokens & source_tokens) / len(quote_tokens)
                if quote_tokens
                else 0.0
            )
            exact_match = quote in source_text
            fuzzy_ocr_match = len(quote_tokens) >= 6 and token_coverage >= 0.75
            if not exact_match and not fuzzy_ocr_match:
                continue
            verdict = str(citation.get("entailment") or "SUPPORTED").upper()
            verified.append(
                {
                    **citation,
                    "chunk_id": chunk_id,
                    "content_hash": source.get("content_hash"),
                    "page_start": source.get("page_start"),
                    "page_end": source.get("page_end"),
                    "entailment": verdict,
                    "verified": True,
                    "match_mode": "EXACT" if exact_match else "FUZZY_OCR",
                    "match_score": round(1.0 if exact_match else token_coverage, 4),
                }
            )

        # A verified CONTRADICTED quote is evidence too: for a wrong answer it is
        # often the only citation, and grounding turns it into a REJECT.
        if not verified or not any(
            item.get("entailment") in {"SUPPORTED", "CONTRADICTED"} for item in verified
        ):
            raise EvidenceGateError(
                "Không xác minh được trích dẫn của AI trong các chunk vừa truy xuất",
                code="EVIDENCE_VALIDATION_FAILED",
                evidence={"citation_validation": {"submitted": len(citations), "verified": 0}},
            )
        supported_answer = any(
            item.get("entailment") == "SUPPORTED"
            and str(item.get("claim_type") or "").upper() == "ANSWER"
            for item in verified
        )
        unsupported_claims = [
            claim
            for claim in (evidence.get("unsupported_claims") or [])
            if str(claim or "").strip() and not PLACEHOLDER_CLAIM_PATTERN.match(str(claim))
        ]
        if not supported_answer:
            unsupported_claims.append(MISSING_ANSWER_CITATION_CLAIM)
        return {
            **evidence,
            "citations": verified,
            "unsupported_claims": list(dict.fromkeys(unsupported_claims)),
            "citation_validation": {
                "status": "VERIFIED",
                "submitted": len(citations),
                "verified": len(verified),
                "answer_supported": supported_answer,
            },
        }

    @classmethod
    def _enforce_grounding_policy(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
    ) -> tuple[EvaluationScores, dict]:
        unsupported = evidence.get("unsupported_claims") or []
        citations = evidence.get("citations") or []
        contradicted = any(
            str(item.get("entailment") or "").upper() == "CONTRADICTED"
            and item.get("scope") != "DISTRACTOR"
            for item in citations
            if isinstance(item, dict)
        )
        updated_feedback = dict(feedback or {})
        if contradicted:
            scores.faithfulness = min(scores.faithfulness, 0.20)
            updated_feedback["action"] = "REJECT"
            updated_feedback["severity"] = "HIGH"
        elif unsupported:
            scores.faithfulness = min(scores.faithfulness, 0.40)
            if str(updated_feedback.get("action") or "").upper() == "APPROVE":
                updated_feedback["action"] = "NEEDS_REVISION"
            updated_feedback["severity"] = "HIGH"
        return scores, updated_feedback

    @staticmethod
    def _source_context(version: dict) -> str:
        model_source_context = QuestionWorkflowService._compact_text(
            (version.get("question_data") or {}).get("model_source_context") or "",
            1400,
        )
        source_parts = [
            f"[{source['label']}] {source['excerpt']}"
            for source in QuestionWorkflowService._compact_sources(version)
            if source.get("excerpt")
        ]
        if model_source_context:
            source_parts.insert(0, f"[MODEL_CONTEXT] {model_source_context}")
        return "\n\n".join(source_parts)

    @staticmethod
    def _normalized_question_type(question_type: str | None) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(question_type or "").strip().lower()).strip("_")
        return normalized or "general"

    def _evaluation_prompt_template(self, question_type: str | None) -> tuple[str, list[dict]]:
        normalized_type = self._normalized_question_type(question_type)
        specs = [
            (EVALUATION_PROMPT_KEY, EVALUATION_PROMPT_PATH),
            (EVALUATION_SCORING_PROMPT_KEY, EVALUATION_SCORING_PROMPT_PATH),
            (DIFFICULTY_RULE_PROMPT_KEY, DIFFICULTY_RULE_PROMPT_PATH),
            (
                f"{EVALUATION_TYPE_PROMPT_PREFIX}:{normalized_type}",
                f"{EVALUATION_TYPE_PROMPT_DIR}/{normalized_type}.txt",
            ),
            (EVALUATION_OUTPUT_PROMPT_KEY, EVALUATION_OUTPUT_PROMPT_PATH),
        ]
        parts = []
        loaded = []
        for key, path in specs:
            try:
                body = PromptBuilder._load_template(key, path)
            except FileNotFoundError:
                if key != f"{EVALUATION_TYPE_PROMPT_PREFIX}:{normalized_type}":
                    raise
                fallback_key = f"{EVALUATION_TYPE_PROMPT_PREFIX}:general"
                fallback_path = f"{EVALUATION_TYPE_PROMPT_DIR}/general.txt"
                body = PromptBuilder._load_template(fallback_key, fallback_path)
                key, path = fallback_key, fallback_path
            parts.append(body)
            loaded.append(
                {
                    "template_key": key,
                    "template_path": path,
                    "template_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                }
            )
        return "\n\n".join(parts), loaded

    def _build_evaluation_prompt(
        self,
        question: dict,
        version: dict,
        policy: dict | None = None,
        *,
        source_chunks: list[dict] | None = None,
    ) -> tuple[str, dict, list[dict]]:
        question_data = version.get("question_data") or {}
        classification = version.get("classification") or {}
        if source_chunks is None:
            source_chunks = self._compact_sources(version)
        else:
            prompt_sources = []
            for source in source_chunks[:EVALUATION_PROMPT_SOURCE_LIMIT]:
                excerpt = self._compact_text(
                    source.get("excerpt") or "",
                    EVALUATION_SOURCE_EXCERPT_CHARS,
                )
                prompt_sources.append(
                    {
                        **source,
                        "excerpt": excerpt,
                        "excerpt_hash": hashlib.sha256(
                            excerpt.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            source_chunks = prompt_sources
        policy = policy or self._policy()
        clos = [
            {
                "code": clo.get("code") or clo.get("clo_code"),
                "description": self._compact_text(clo.get("description") or "", 300),
            }
            for clo in (version.get("clos") or [])
        ]
        payload = {
            "question_code": question.get("question_code"),
            "question_id": question.get("_id"),
            "question_version_id": version.get("_id"),
            "document_id": version.get("document_id"),
            "question_type": classification.get("assessment_type"),
            "question": version.get("content"),
            "options": question_data.get("options"),
            "correct_answer": question_data.get("correct_answer"),
            "explanation": question_data.get("explanation"),
            # This is the verbatim evidence selected by the generation model
            # and persisted with the question version.  Keep the immutable
            # chunk snapshots below as the authoritative source provenance.
            "generation_source_context": self._compact_text(
                question_data.get("model_source_context") or "",
                1400,
            ),
            "requested_bloom": classification.get("bloom"),
            "current_difficulty": classification.get("difficulty"),
            # Nói thẳng câu hỏi có gắn CLO hay không để mô hình không phải tự suy ra.
            "clo_status": "ATTACHED" if clos else "NOT_ATTACHED",
            "clos": clos,
            "source_chunks": source_chunks,
        }
        policy_payload = {
            "name": policy.get("policy_name") or policy.get("name"),
            "version": policy.get("version"),
            "weights": policy.get("weights") or DEFAULT_WEIGHTS,
            "thresholds": policy.get("thresholds") or DEFAULT_THRESHOLDS,
        }
        template, prompt_parts = self._evaluation_prompt_template(payload["question_type"])
        prompt = template.format(
            evaluation_policy=json.dumps(policy_payload, ensure_ascii=False, default=str),
            question_payload=json.dumps(payload, ensure_ascii=False, default=str),
        )
        prompt_snapshot = {
            "template_key": EVALUATION_PROMPT_KEY,
            "template_path": EVALUATION_PROMPT_PATH,
            "template_hash": hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "template_parts": prompt_parts,
            "question_type_prompt": self._normalized_question_type(payload["question_type"]),
            "rendered_prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "rendered_prompt_chars": len(prompt),
            "source_limit": len(source_chunks),
            "source_excerpt_chars": EVALUATION_SOURCE_EXCERPT_CHARS,
        }
        return prompt, prompt_snapshot, source_chunks

    def _evaluation_prompt(self, question: dict, version: dict, policy: dict | None = None) -> str:
        prompt, _, _ = self._build_evaluation_prompt(question, version, policy)
        return prompt

    @classmethod
    def _parse_llm_evaluation(cls, raw_response: str) -> tuple[EvaluationScores, dict, dict]:
        parsed = json.loads(cls._clean_json(raw_response))
        raw_scores = parsed.get("scores") or {}

        def score_value(key: str, *aliases: str) -> float:
            for candidate in (key, *aliases):
                if candidate in raw_scores:
                    return cls._clamp(float(raw_scores[candidate]))
            raise ValueError(f"AI evaluation thiếu điểm bắt buộc: {key}")

        scores = EvaluationScores(
            faithfulness=score_value("faithfulness"),
            contextual_relevancy=score_value("contextual_relevancy"),
            # Local models occasionally misspell this field. Accept only the
            # observed typo; all other missing scores remain a retryable error.
            answer_relevancy=score_value("answer_relevancy", "answer_relevality"),
            bloom_alignment=score_value("bloom_alignment"),
            clo_alignment=score_value("clo_alignment"),
        )
        feedback = dict(parsed.get("feedback")) if isinstance(parsed.get("feedback"), dict) else {}
        action = str(feedback.get("action") or parsed.get("action") or "").strip().upper()
        severity = str(feedback.get("severity") or parsed.get("severity") or "").strip().upper()
        if action not in {"APPROVE", "NEEDS_REVISION", "REJECT"}:
            raise ValueError("AI evaluation thiếu action hợp lệ")
        if severity not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("AI evaluation thiếu severity hợp lệ")
        summary = str(feedback.get("summary") or "").strip()
        if not summary:
            raise ValueError("AI evaluation thiếu summary")
        feedback["action"] = action
        feedback["severity"] = severity
        feedback["summary"] = summary
        missing = feedback.get("missing")
        feedback["missing"] = missing if isinstance(missing, list) else ([missing] if missing else [])
        evidence = parsed.get("evidence") if isinstance(parsed.get("evidence"), dict) else {}
        moodle_readiness = str(evidence.get("moodle_readiness") or "").strip().upper()
        if moodle_readiness not in {"READY", "NEEDS_FIX"}:
            raise ValueError("AI evaluation thiếu moodle_readiness hợp lệ")
        if not str(evidence.get("reasoning") or evidence.get("supporting_excerpt") or "").strip():
            raise ValueError("AI evaluation thiếu minh chứng hoặc lý do chấm điểm")
        evidence["moodle_readiness"] = moodle_readiness
        risks = evidence.get("risks")
        evidence["risks"] = risks if isinstance(risks, list) else ([risks] if risks else [])
        assessed = str(evidence.get("assessed_difficulty") or "").strip().lower().replace("-", "_")
        assessed = assessed.replace(" ", "_")
        evidence["assessed_difficulty"] = assessed if assessed in {"de", "trung_binh", "kho"} else None
        polarity = str(evidence.get("question_polarity") or "").strip().upper()
        evidence["question_polarity"] = polarity if polarity in {"POSITIVE", "NEGATIVE"} else None
        option_checks = evidence.get("option_checks")
        normalized_checks = []
        if isinstance(option_checks, list):
            for check in option_checks:
                if not isinstance(check, dict):
                    continue
                # Local models sometimes insert punctuation into a field name
                # (qwen3:8b writes "ver,dict" for the fourth option). Read the
                # field by its letters so a typo in the evaluator's own output is
                # not reported as a defect of the question.
                fields = {
                    re.sub(r"[^a-z_]", "", str(name).lower()): value
                    for name, value in check.items()
                }
                normalized_checks.append(
                    {
                        "key": str(fields.get("key") or "").strip().upper(),
                        "verdict": str(fields.get("verdict") or "").strip().upper(),
                        "source_label": str(fields.get("source_label") or "").strip().upper(),
                        "supporting_excerpt": cls._compact_text(
                            fields.get("supporting_excerpt") or "",
                            300,
                        ),
                    }
                )
        evidence["option_checks"] = normalized_checks
        return scores, feedback, evidence

    @staticmethod
    def _question_options(version: dict) -> dict[str, str]:
        options = (version.get("question_data") or {}).get("options")
        if isinstance(options, dict):
            return {
                str(key).strip().upper(): str(value or "").strip()
                for key, value in options.items()
                if str(key).strip() and str(value or "").strip()
            }
        if isinstance(options, list):
            return {
                chr(65 + index): str(value or "").strip()
                for index, value in enumerate(options)
                if index < 26 and str(value or "").strip()
            }
        return {}

    @classmethod
    def _declared_answer_keys(cls, version: dict, options: dict[str, str]) -> set[str]:
        raw_answer = (version.get("question_data") or {}).get("correct_answer")
        values = raw_answer if isinstance(raw_answer, list) else re.split(r"[;,|]", str(raw_answer or ""))
        normalized_options = {
            cls._compact_text(value, 500).casefold(): key
            for key, value in options.items()
        }
        answer_keys = set()
        for value in values:
            normalized = str(value or "").strip()
            key = normalized.upper()
            if key in options:
                answer_keys.add(key)
                continue
            option_key = normalized_options.get(cls._compact_text(normalized, 500).casefold())
            if option_key:
                answer_keys.add(option_key)
        return answer_keys

    @classmethod
    def _apply_answer_guardrail(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        version: dict,
    ) -> tuple[EvaluationScores, dict, dict]:
        options = cls._question_options(version)
        question_type = str(
            (version.get("classification") or {}).get("assessment_type") or ""
        ).strip().upper()
        if question_type in NON_OPTION_ANSWER_TYPES:
            return scores, feedback, {
                **evidence,
                "answer_guardrail": {
                    "applied": False,
                    "reason": "QUESTION_TYPE_NOT_OPTION_BASED",
                    "question_type": question_type,
                },
            }
        if len(options) < 2:
            return scores, feedback, {
                **evidence,
                "answer_guardrail": {"applied": False, "reason": "NO_OPTION_SET"},
            }

        question = str(version.get("content") or "")
        # With true/false items the stem is a proposition; words such as "không"
        # belong to that proposition and do not turn the answer format into a
        # negative-selection MCQ.
        negative = cls._is_negative_selection_question(question_type, question)
        declared_keys = cls._declared_answer_keys(version, options)
        checks = evidence.get("option_checks") if isinstance(evidence.get("option_checks"), list) else []
        checks_by_key = {
            str(check.get("key") or "").strip().upper(): check
            for check in checks
            if isinstance(check, dict) and str(check.get("key") or "").strip()
        }
        if question_type == "DUNG_SAI" and len(options) == 2 and len(checks_by_key) == 1:
            normalized_values = {
                key: re.sub(r"\s+", " ", value).strip().casefold()
                for key, value in options.items()
            }
            truth_key = next(
                (key for key, value in normalized_values.items() if value in {"đúng", "dung", "true"}),
                None,
            )
            false_key = next(
                (key for key, value in normalized_values.items() if value in {"sai", "false"}),
                None,
            )
            checked_key, checked = next(iter(checks_by_key.items()))
            if (
                truth_key
                and false_key
                and checked_key in {truth_key, false_key}
                and checked.get("verdict") in {"SUPPORTED", "CONTRADICTED"}
            ):
                inferred_key = false_key if checked_key == truth_key else truth_key
                inferred = {
                    "key": inferred_key,
                    "verdict": (
                        "CONTRADICTED"
                        if checked.get("verdict") == "SUPPORTED"
                        else "SUPPORTED"
                    ),
                    "source_label": checked.get("source_label") or "",
                    "supporting_excerpt": checked.get("supporting_excerpt") or "",
                    "inferred_from_complement": checked_key,
                }
                checks.append(inferred)
                checks_by_key[inferred_key] = inferred
                evidence = {**evidence, "option_checks": checks}
        issues = []
        reported_polarity = str(evidence.get("question_polarity") or "").strip().upper()
        detected_polarity = "NEGATIVE" if negative else "POSITIVE"
        if reported_polarity and reported_polarity != detected_polarity:
            issues.append(
                f"AI nhận diện sai dạng câu: báo {reported_polarity}, thực tế {detected_polarity}"
            )
        # Thiếu sót của chính kết quả chấm (không phải lỗi của câu hỏi): được ghi riêng
        # để lượt chấm được thử lại thay vì kết luận câu hỏi cần sửa.
        evaluator_incomplete = []
        missing_keys = [key for key in options if key not in checks_by_key]
        if missing_keys:
            evaluator_incomplete.append(f"AI chưa kiểm tra phương án: {', '.join(missing_keys)}")

        invalid_keys = [
            key
            for key, check in checks_by_key.items()
            if key not in options or check.get("verdict") not in OPTION_CHECK_VERDICTS
        ]
        if invalid_keys:
            evaluator_incomplete.append(f"option_checks không hợp lệ: {', '.join(sorted(invalid_keys))}")
        issues.extend(evaluator_incomplete)

        supported = {
            key
            for key, check in checks_by_key.items()
            if key in options and check.get("verdict") == "SUPPORTED"
        }
        unsupported = {
            key
            for key, check in checks_by_key.items()
            if key in options and check.get("verdict") in {"CONTRADICTED", "NOT_IN_SOURCE"}
        }
        ambiguous = {
            key
            for key, check in checks_by_key.items()
            if key in options and check.get("verdict") == "AMBIGUOUS"
        }
        if not declared_keys:
            issues.append("Không xác định được đáp án đã khai báo")

        source_context = cls._source_context(version)
        lexical_support = {
            key: round(cls._overlap_score(text, source_context), 4)
            for key, text in options.items()
        }
        answer_mode = (
            "SINGLE"
            if question_type in SINGLE_ANSWER_TYPES
            else "MULTIPLE"
            if question_type in MULTIPLE_ANSWER_TYPES
            else "SINGLE"
            if len(declared_keys) <= 1
            else "MULTIPLE"
        )
        if negative:
            if len(unsupported) != 1:
                issues.append(
                    "Câu phủ định phải có đúng một phương án không được nguồn hỗ trợ"
                )
            elif declared_keys != unsupported:
                issues.append("Đáp án khai báo không khớp phương án phủ định duy nhất")
            if options and all(score >= 0.8 for score in lexical_support.values()):
                issues.append(
                    "Tất cả phương án đều xuất hiện rõ trong nguồn; không được tự động duyệt câu phủ định"
                )
        else:
            if answer_mode == "SINGLE":
                if not declared_keys.issubset(supported):
                    issues.append("Đáp án khai báo không được option_checks xác nhận")
                if len(supported) != 1:
                    issues.append("Câu một đáp án không có đúng một phương án được xác nhận")
            elif declared_keys and supported != declared_keys:
                issues.append("Tập đáp án khai báo không khớp các phương án được xác nhận")

        if ambiguous:
            issues.append(f"Phương án còn mơ hồ: {', '.join(sorted(ambiguous))}")
        compact_sources = cls._compact_sources(version)
        source_by_label = {
            str(source.get("label") or "").upper(): str(source.get("excerpt") or "")
            for source in compact_sources
        }
        verified_by_option_text = []
        for key in sorted(supported):
            check = checks_by_key[key]
            excerpt = str(check.get("supporting_excerpt") or "").strip()
            source_label = str(check.get("source_label") or "").strip().upper()
            if not excerpt:
                # Mô hình quên kèm trích dẫn cho phương án nó xác nhận. Tự đối chiếu nội
                # dung phương án với nguồn; chỉ chặn khi nguồn không chứa phương án đó.
                if lexical_support.get(key, 0.0) >= OPTION_TEXT_SUPPORT_MIN:
                    verified_by_option_text.append(key)
                else:
                    issues.append(f"Phương án {key} thiếu trích dẫn nguồn")
            if not source_label or source_label not in source_by_label:
                issues.append(f"Phương án {key} không trỏ tới nguồn S hợp lệ")
            elif excerpt and cls._overlap_score(excerpt, source_by_label[source_label]) < 0.5:
                issues.append(f"Trích dẫn của phương án {key} không khớp nguồn {source_label}")

        guardrail = {
            "applied": bool(issues),
            "question_type": question_type or "UNKNOWN",
            "answer_mode": answer_mode,
            "question_polarity": detected_polarity,
            "reported_question_polarity": reported_polarity or None,
            "declared_answer_keys": sorted(declared_keys),
            "supported_option_keys": sorted(supported),
            "unsupported_option_keys": sorted(unsupported),
            "ambiguous_option_keys": sorted(ambiguous),
            "option_source_overlap": lexical_support,
            "verified_by_option_text": verified_by_option_text,
            "evaluator_incomplete": evaluator_incomplete,
            "issues": list(dict.fromkeys(issues)),
        }
        if not issues:
            return scores, feedback, {**evidence, "answer_guardrail": guardrail}

        guarded_scores = EvaluationScores(
            **{
                **scores.model_dump(),
                "faithfulness": min(scores.faithfulness, 0.35),
                "answer_relevancy": min(scores.answer_relevancy, 0.30),
            }
        )
        already_rejected = str(feedback.get("action") or "").strip().upper() == "REJECT"
        guarded_feedback = {
            **feedback,
            # A guardrail only ever tightens the verdict; it must not soften a REJECT.
            "action": "REJECT" if already_rejected else "NEEDS_REVISION",
            "severity": "HIGH",
            "summary": "Guardrail đáp án chặn tự động duyệt: " + guardrail["issues"][0],
            "missing": list(
                dict.fromkeys([*(feedback.get("missing") or []), *guardrail["issues"]])
            ),
        }
        guarded_evidence = {
            **evidence,
            "moodle_readiness": "NEEDS_FIX",
            "risks": list(
                dict.fromkeys(
                    [
                        *(evidence.get("risks") or []),
                        "Đáp án chưa vượt qua kiểm chứng từng phương án",
                    ]
                )
            ),
            "answer_guardrail": guardrail,
        }
        return guarded_scores, guarded_feedback, guarded_evidence

    @classmethod
    def _apply_metadata_guardrail(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        version: dict,
    ) -> tuple[EvaluationScores, dict, dict]:
        """Thiếu Bloom thì chặn; không gắn CLO thì tiêu chí CLO chỉ là "không áp dụng".

        Không phải câu hỏi nào cũng gắn CLO, nên việc thiếu CLO không được làm câu rớt:
        tiêu chí clo_alignment bị loại khỏi tổng điểm (xem effective_weights) và một kết
        luận NEEDS_REVISION mà lý do duy nhất là thiếu CLO được trả về APPROVE.
        """
        classification = version.get("classification") or {}
        bloom = classification.get("bloom") or {}
        clos = version.get("clos") if isinstance(version.get("clos"), list) else []
        missing_fields = []
        issues = []
        notes = []
        not_applicable = []
        score_values = scores.model_dump()

        if not bloom.get("level"):
            missing_fields.append("bloom")
            issues.append("Câu hỏi chưa được gắn mức Bloom")
            score_values["bloom_alignment"] = min(scores.bloom_alignment, 0.35)
        valid_clos = [
            clo
            for clo in clos
            if isinstance(clo, dict)
            and str(clo.get("code") or clo.get("clo_code") or "").strip()
            and str(clo.get("description") or "").strip()
        ]
        if not valid_clos:
            not_applicable.append(CLO_CRITERION)
            notes.append("Câu hỏi không gắn CLO nên tiêu chí Đúng CLO không tính vào tổng điểm")

        guardrail = {
            "applied": bool(issues),
            "missing_fields": missing_fields,
            "issues": issues,
            "not_applicable": not_applicable,
            "notes": notes,
        }
        guarded_feedback = feedback
        if not_applicable:
            guarded_feedback, cleared = cls._clear_clo_only_revision(scores, feedback, evidence)
            if cleared:
                guardrail["cleared_missing"] = cleared
        if not issues:
            return scores, guarded_feedback, {**evidence, "metadata_guardrail": guardrail}

        current_action = str(guarded_feedback.get("action") or "").strip().upper()
        current_severity = str(guarded_feedback.get("severity") or "").strip().upper()
        guarded_feedback = {
            **guarded_feedback,
            "action": "REJECT" if current_action == "REJECT" else "NEEDS_REVISION",
            "severity": "HIGH" if current_severity == "HIGH" else "MEDIUM",
            "summary": (
                guarded_feedback.get("summary")
                if current_action in {"NEEDS_REVISION", "REJECT"}
                else "Guardrail metadata yêu cầu bổ sung mức Bloom trước khi duyệt."
            ),
            "missing": list(
                dict.fromkeys([*(guarded_feedback.get("missing") or []), *issues])
            ),
        }
        guarded_evidence = {
            **evidence,
            "risks": list(
                dict.fromkeys(
                    [
                        *(evidence.get("risks") or []),
                        "Thiếu metadata sư phạm bắt buộc để kiểm duyệt nhất quán",
                    ]
                )
            ),
            "metadata_guardrail": guardrail,
        }
        return EvaluationScores(**score_values), guarded_feedback, guarded_evidence

    @staticmethod
    def _clear_clo_only_revision(
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
    ) -> tuple[dict, list[str]]:
        """Với câu không gắn CLO: bỏ các mục "thiếu CLO" khỏi danh sách cần sửa.

        Nếu sau đó không còn mục nào và mô hình chỉ yêu cầu sửa vì thiếu CLO thì trả
        kết luận về APPROVE. Việc này chỉ làm khi mọi điều kiện đều rõ ràng: mức lỗi
        không phải HIGH, không có nhận định thiếu căn cứ và bốn tiêu chí còn lại đều
        đạt ngưỡng tối thiểu. Thiếu một điều kiện thì giữ nguyên kết luận.
        """
        missing = [str(item) for item in feedback.get("missing") or [] if str(item or "").strip()]
        cleared = [item for item in missing if CLO_MENTION_PATTERN.search(item)]
        if not cleared:
            return feedback, []
        remaining = [item for item in missing if item not in cleared]
        updated = {**feedback, "missing": remaining}

        action = str(feedback.get("action") or "").strip().upper()
        severity = str(feedback.get("severity") or "").strip().upper()
        other_scores = [
            value for key, value in scores.model_dump().items() if key != CLO_CRITERION
        ]
        if (
            action == "NEEDS_REVISION"
            and severity != "HIGH"
            and not remaining
            and not evidence.get("unsupported_claims")
            and min(other_scores) >= DEFAULT_THRESHOLDS["pass_min"]
        ):
            updated.update({"action": "APPROVE", "severity": "LOW"})
        return updated, cleared

    @classmethod
    def _apply_evaluation_guardrails(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        version: dict,
    ) -> tuple[EvaluationScores, dict, dict]:
        scores, feedback, evidence = cls._apply_answer_guardrail(
            scores,
            feedback,
            evidence,
            version,
        )
        return cls._apply_metadata_guardrail(
            scores,
            feedback,
            evidence,
            version,
        )

    @staticmethod
    def _overall_and_pass_min(
        scores: EvaluationScores,
        policy: dict,
        evidence: dict | None = None,
    ) -> tuple[float, float]:
        score_values = scores.model_dump()
        weights = effective_weights(policy, evidence)
        thresholds = policy.get("thresholds") or DEFAULT_THRESHOLDS
        pass_min = thresholds.get("pass_min", DEFAULT_THRESHOLDS["pass_min"])
        overall = round(sum(score_values[key] * weights[key] for key in DEFAULT_WEIGHTS), 4)
        return overall, pass_min

    @classmethod
    def _decision_contradictions(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        policy: dict,
    ) -> list[str]:
        overall, pass_min = cls._overall_and_pass_min(scores, policy, evidence)
        action = str(feedback.get("action") or "").strip().upper()
        severity = str(feedback.get("severity") or "").strip().upper()
        moodle_readiness = str(evidence.get("moodle_readiness") or "").strip().upper()
        contradictions = []
        if action == "APPROVE" and overall < pass_min:
            contradictions.append("APPROVE nhưng tổng điểm dưới ngưỡng đạt")
        if action == "APPROVE" and severity == "HIGH":
            contradictions.append("APPROVE nhưng mức độ lỗi là HIGH")
        if action == "APPROVE" and moodle_readiness == "NEEDS_FIX":
            contradictions.append("APPROVE nhưng cấu trúc Moodle vẫn NEEDS_FIX")
        if action == "REJECT" and severity == "LOW":
            contradictions.append("REJECT nhưng mức độ lỗi chỉ là LOW")
        if action == "REJECT" and moodle_readiness == "READY":
            contradictions.append("REJECT nhưng kết quả lại ghi Moodle READY")
        return contradictions

    @classmethod
    def _normalize_decision(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        policy: dict,
    ) -> tuple[dict, dict]:
        """Resolve action/severity/readiness conflicts toward the stricter side.

        Grounding and guardrails may override the model's action (for example a
        CONTRADICTED citation forces REJECT) without touching its other fields.
        The saved result must still be internally consistent, and a conflict must
        never be settled by approving the question.
        """
        contradictions = cls._decision_contradictions(scores, feedback, evidence, policy)
        if not contradictions:
            return feedback, evidence

        feedback = dict(feedback)
        evidence = dict(evidence)
        action = str(feedback.get("action") or "").strip().upper()
        before = {
            "action": action,
            "severity": str(feedback.get("severity") or "").strip().upper(),
            "moodle_readiness": str(evidence.get("moodle_readiness") or "").strip().upper(),
        }
        if action == "APPROVE":
            feedback["action"] = "NEEDS_REVISION"
            if feedback.get("severity") == "LOW":
                feedback["severity"] = "MEDIUM"
        elif action == "REJECT":
            if feedback.get("severity") == "LOW":
                feedback["severity"] = "MEDIUM"
            evidence["moodle_readiness"] = "NEEDS_FIX"
        evidence["decision_normalization"] = {
            "applied": True,
            "contradictions": contradictions,
            "before": before,
            "after": {
                "action": feedback.get("action"),
                "severity": feedback.get("severity"),
                "moodle_readiness": evidence.get("moodle_readiness"),
            },
        }
        return feedback, evidence

    @classmethod
    def _validate_llm_evaluation_consistency(
        cls,
        scores: EvaluationScores,
        feedback: dict,
        evidence: dict,
        policy: dict,
    ) -> dict:
        score_values = scores.model_dump()
        overall, pass_min = cls._overall_and_pass_min(scores, policy, evidence)
        contradictions = cls._decision_contradictions(scores, feedback, evidence, policy)
        if contradictions:
            raise ValueError("AI evaluation tự mâu thuẫn: " + "; ".join(contradictions))

        values = list(score_values.values())
        score_spread = round(max(values) - min(values), 4)
        return {
            "validated": True,
            "calculated_overall": overall,
            "score_spread": score_spread,
            "uniform_scores": score_spread <= 0.02,
            "weak_criteria": [
                key
                for key, value in score_values.items()
                if value < pass_min and key not in not_applicable_criteria(evidence)
            ],
        }

    @staticmethod
    def _llm_output_metadata(llm, raw_response: str | None) -> dict:
        provider = llm
        if getattr(provider, "last_used", None) in {"primary", "fallback"}:
            provider = provider.fallback if provider.last_used == "fallback" else provider.primary
        metadata = getattr(provider, "last_response_metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        return {
            "chars": len(raw_response or ""),
            **{
                key: metadata[key]
                for key in ("done_reason", "eval_count", "prompt_eval_count")
                if metadata.get(key) is not None
            },
            "hit_output_limit": metadata.get("done_reason") == "length",
        }

    @classmethod
    def _option_checks_confirm_declared_answer(cls, evidence: dict, version: dict) -> bool:
        """True when option_checks verify every declared answer key against a source."""
        options = cls._question_options(version)
        question_type = str(
            (version.get("classification") or {}).get("assessment_type") or ""
        ).strip().upper()
        if question_type in NON_OPTION_ANSWER_TYPES or len(options) < 2:
            return False
        # In a negative question the declared answer is the unsupported option,
        # so option support cannot stand in for answer support.
        if cls._is_negative_selection_question(question_type, str(version.get("content") or "")):
            return False
        declared_keys = cls._declared_answer_keys(version, options)
        if not declared_keys:
            return False
        checks = {
            str(check.get("key") or "").strip().upper(): check
            for check in (evidence.get("option_checks") or [])
            if isinstance(check, dict)
        }
        source_by_label = {
            str(source.get("label") or "").upper(): str(source.get("excerpt") or "")
            for source in cls._compact_sources(version)
        }
        for key in declared_keys:
            check = checks.get(key) or {}
            label = str(check.get("source_label") or "").strip().upper()
            excerpt = str(check.get("supporting_excerpt") or "").strip()
            if (
                check.get("verdict") != "SUPPORTED"
                or label not in source_by_label
                or not excerpt
                or cls._overlap_score(excerpt, source_by_label[label]) < 0.5
            ):
                return False
        return True

    @classmethod
    def _claim_names_only_distractors(
        cls,
        claim,
        options: dict[str, str],
        declared_keys: set[str],
    ) -> bool:
        text = str(claim or "")
        folded = cls._compact_text(text, 1000).casefold()
        referenced = {
            key
            for key in options
            if re.search(rf"(?<![\w]){re.escape(key)}(?![\w])", text)
        }
        referenced |= {
            key
            for key, value in options.items()
            if len(value.strip()) >= 2
            and cls._compact_text(value, 500).casefold() in folded
        }
        return bool(referenced) and not (referenced & declared_keys)

    @classmethod
    def _reconcile_answer_evidence(cls, evidence: dict, version: dict) -> dict:
        """Use verified option_checks to interpret answer citations.

        Models write citations such as "Đáp án C sai" marked CONTRADICTED, which
        is evidence against a distractor, not against the declared answer. When
        the declared answer is itself verified option by option, an ANSWER
        citation whose claim names only non-declared options is scoped to
        distractors, and the verified option check also counts as the answer
        citation. A contradicted claim that names the declared answer, or names
        no option at all, still counts against the answer.
        """
        if not cls._option_checks_confirm_declared_answer(evidence, version):
            return evidence
        options = cls._question_options(version)
        declared_keys = cls._declared_answer_keys(version, options)
        citations = [
            {**item, "scope": "DISTRACTOR"}
            if isinstance(item, dict)
            and str(item.get("entailment") or "").upper() == "CONTRADICTED"
            and str(item.get("claim_type") or "").upper() == "ANSWER"
            and cls._claim_names_only_distractors(item.get("claim"), options, declared_keys)
            else item
            for item in (evidence.get("citations") or [])
        ]
        validation = dict(evidence.get("citation_validation") or {})
        unsupported = list(evidence.get("unsupported_claims") or [])
        if not validation.get("answer_supported"):
            validation["answer_supported"] = True
            validation["answer_support_source"] = "OPTION_CHECKS"
            unsupported = [claim for claim in unsupported if claim != MISSING_ANSWER_CITATION_CLAIM]
        return {
            **evidence,
            "citations": citations,
            "unsupported_claims": unsupported,
            "citation_validation": validation,
        }

    def _finalize_llm_evaluation(
        self,
        raw_response: str,
        source_chunks: list[dict],
        grounded_version: dict,
        policy: dict,
        *,
        llm=None,
    ) -> tuple[EvaluationScores, dict, dict]:
        output = self._llm_output_metadata(llm, raw_response)
        try:
            scores, feedback, evidence = self._parse_llm_evaluation(raw_response)
        except ValueError as exc:
            if output["hit_output_limit"]:
                raise ValueError(
                    "AI evaluation bị cắt ngang do chạm giới hạn độ dài output "
                    f"(num_predict): {exc}"
                ) from exc
            raise
        evidence = self._validate_model_evidence(evidence, source_chunks)
        evidence = self._reconcile_answer_evidence(evidence, grounded_version)
        # Recorded before any override so the Admin page can tell how often the
        # model itself returns a self-contradictory verdict.
        model_contradictions = self._decision_contradictions(scores, feedback, evidence, policy)
        scores, feedback = self._enforce_grounding_policy(scores, feedback, evidence)
        scores, feedback, evidence = self._apply_evaluation_guardrails(
            scores,
            feedback,
            evidence,
            grounded_version,
        )
        incomplete = (evidence.get("answer_guardrail") or {}).get("evaluator_incomplete") or []
        if incomplete:
            # Kết quả chấm thiếu sót chưa nói được gì về câu hỏi: báo lỗi để tác vụ chấm
            # lại; hết lượt thử thì câu ở trạng thái "Chưa đánh giá được", không phải "xem lại".
            raise ValueError("AI evaluation chưa kiểm tra đủ các phương án: " + "; ".join(incomplete))
        feedback, evidence = self._normalize_decision(scores, feedback, evidence, policy)
        consistency = self._validate_llm_evaluation_consistency(scores, feedback, evidence, policy)
        return scores, feedback, {
            **evidence,
            "consistency": {**consistency, "model_contradictions": model_contradictions},
            "model_output": output,
        }

    def _auto_scores(self, question: dict, version: dict) -> tuple[EvaluationScores, dict, dict]:
        question_data = version.get("question_data") or {}
        sources = version.get("sources") or []
        source_context = self._source_context(version)
        content = version.get("content") or ""
        answer = str(question_data.get("correct_answer") or "")
        explanation = str(question_data.get("explanation") or "")
        answer_block = " ".join([content, answer, explanation]).strip()

        question_overlap = self._overlap_score(content, source_context)
        answer_overlap = self._overlap_score(answer_block, source_context)
        has_answer = bool(answer.strip())
        has_explanation = bool(explanation.strip())
        has_context = bool(source_context.strip())
        has_bloom = bool((version.get("classification") or {}).get("bloom", {}).get("level"))
        has_clo = bool(version.get("clos") or [])

        scores = EvaluationScores(
            faithfulness=self._clamp(0.50 + 0.50 * answer_overlap if has_context else 0.45),
            contextual_relevancy=self._clamp(0.45 + 0.45 * question_overlap + min(len(sources), 3) * 0.03 if has_context else 0.35),
            answer_relevancy=self._clamp(0.50 + (0.25 if has_answer else 0.0) + (0.20 if has_explanation else 0.0) + 0.05 * min(len(answer), 40) / 40),
            bloom_alignment=0.86 if has_bloom else 0.50,
            clo_alignment=0.86 if has_clo else 0.62,
        )
        feedback = {
            "summary": (
                "Đánh giá tự động bằng heuristic nội bộ phục vụ demo P0; "
                "cần thay bằng local evaluator model ở bản production."
            ),
            # Word overlap cannot verify that the answer is correct, so a
            # heuristic result always hands the decision to a human reviewer.
            "action": "NEEDS_REVISION",
            "severity": "MEDIUM",
            "missing": [
                label
                for label, missing in (
                    ("Không có source context", not has_context),
                    ("Thiếu đáp án đúng", not has_answer),
                    ("Thiếu giải thích", not has_explanation),
                    ("Chưa gắn CLO", not has_clo),
                )
                if missing
            ],
        }
        evidence = {
            "source_count": len(sources),
            "question_context_overlap": round(question_overlap, 4),
            "answer_context_overlap": round(answer_overlap, 4),
            "source_excerpt": source_context[:1200],
            "checks": {
                "has_context": has_context,
                "has_answer": has_answer,
                "has_explanation": has_explanation,
                "has_bloom": has_bloom,
                "has_clo": has_clo,
            },
        }
        return scores, feedback, evidence

    def evaluate(
        self,
        question_id: str,
        payload: EvaluationCreateRequest,
        user_id,
        *,
        require_active_job: bool = False,
        update_question: bool = True,
    ) -> dict:
        question, version = self._pair(question_id)
        if question["current_version"] != payload.expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        policy = payload.policy_snapshot or self._policy()
        scores = payload.scores.model_dump()
        # Tiêu chí không áp dụng (câu không gắn CLO) không tính vào tổng điểm.
        weights = effective_weights(policy, payload.evidence)
        skipped_criteria = sorted(not_applicable_criteria(payload.evidence))
        overall = round(sum(scores[key] * weights[key] for key in DEFAULT_WEIGHTS), 4)
        thresholds = policy["thresholds"]
        color = (
            "GREEN"
            if overall >= thresholds["green_min"]
            else "YELLOW"
            if overall >= thresholds["yellow_min"]
            else "RED"
        )
        feedback_action = str(payload.feedback.get("action") or "").strip().upper()
        feedback_severity = str(payload.feedback.get("severity") or "").strip().upper()
        unsupported_claims = (payload.evidence or {}).get("unsupported_claims") or []
        action_requires_review = feedback_action in {"NEEDS_REVISION", "REJECT"}
        severe_issue = feedback_severity == "HIGH"
        grounding_blocks_pass = bool(unsupported_claims)
        heuristic_only = str((payload.evidence or {}).get("mode") or "").startswith("heuristic")
        passed = (
            overall >= thresholds["pass_min"]
            and not action_requires_review
            and not severe_issue
            and not grounding_blocks_pass
            and not heuristic_only
        )
        if feedback_action == "REJECT" or severe_issue:
            color = "RED"
        elif action_requires_review and color == "GREEN":
            color = "YELLOW"
        evidence = {
            **payload.evidence,
            "decision_guardrail": {
                "score_passed": overall >= thresholds["pass_min"],
                "feedback_action": feedback_action or None,
                "feedback_severity": feedback_severity or None,
                "unsupported_claim_count": len(unsupported_claims),
                "heuristic_only": heuristic_only,
                "blocked_pass": (
                    action_requires_review or severe_issue or grounding_blocks_pass or heuristic_only
                ),
            },
        }
        now = utc_now()
        evaluation_job_id = (
            object_id(payload.evaluation_job_id, "evaluation_job_id")
            if payload.evaluation_job_id
            else None
        )
        evaluation = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "question_id": question["_id"],
            "question_version_id": version["_id"],
            "question_version": version["version"],
            "question_snapshot_hash": version["content_hash"],
            "generation_run_id": version.get("generation_run_id"),
            "evaluator_model": payload.model_snapshot or self._model_snapshot(payload.evaluator_model_code),
            "model_execution": payload.model_execution,
            "requested_by_user_id": user_id,
            "policy": {
                "id": policy.get("_id") or policy.get("id"),
                "name": policy.get("policy_name") or policy.get("name"),
                "version": policy["version"],
                "weights": policy["weights"],
                "thresholds": thresholds,
                **(
                    {"effective_weights": weights, "not_applicable": skipped_criteria}
                    if skipped_criteria
                    else {}
                ),
            },
            "scores": {**scores, "overall": overall},
            "color": color,
            "passed": passed,
            "feedback": payload.feedback,
            "evidence": evidence,
            "raw_model_response": payload.raw_model_response,
            "prompt_snapshot": payload.prompt_snapshot,
            "duration_ms": payload.duration_ms,
            "evaluation_job_id": evaluation_job_id,
            "trigger": payload.trigger,
            "parser_version": "evaluation-json-v1",
            "created_at": now,
        }
        audit = build_audit_event(
            action="QUESTION_EVALUATED",
            entity_type="question",
            entity_id=question["_id"],
            entity_version_id=version["_id"],
            actor_user_id=user_id,
            model_id=evaluation["evaluator_model"].get("id"),
            service_name="question_evaluation",
            changes=[
                {
                    "path": "quality_summary",
                    "old_value": question.get("quality_summary") or {},
                    "new_value": {
                        "latest_evaluation_id": evaluation["_id"],
                        "overall_score": overall,
                        "color": color,
                    },
                }
            ],
            before_hash=version["content_hash"],
            after_hash=version["content_hash"],
            metadata={
                "evaluation_id": evaluation["_id"],
                "correlation_id": str(evaluation["_id"]),
            },
            created_at=now,
        )
        quality_summary = {
            "latest_evaluation_id": evaluation["_id"],
            "latest_evaluation_job_id": evaluation_job_id,
            "evaluated_version_id": version["_id"],
            "overall_score": overall,
            "color": color,
            "evaluated_at": now,
            "evaluator_model_code": evaluation["evaluator_model"].get("model_code"),
            "feedback": payload.feedback,
            "evidence": payload.evidence,
        }
        if settings.question_store == "postgres":
            self.questions.record_evaluation(
                evaluation, expected_version_id=version["_id"],
                evaluation_status="PASSED" if passed else "FAILED",
                quality_summary=quality_summary,
                require_active_job=require_active_job,
                update_question=update_question,
            )
        else:
            with mongo_transaction() as session:
                if require_active_job:
                    # Ghi vào job trong cùng transaction để xung đột với lúc người duyệt
                    # dừng AI; job không còn PROCESSING thì bỏ kết quả.
                    job_touch = self.db.evaluation_jobs.update_one(
                        {"_id": evaluation_job_id, "status": "PROCESSING"},
                        {"$set": {"updated_at": now}},
                        session=session,
                    )
                    if not job_touch.matched_count:
                        raise EvaluationInterruptedError("Tác vụ AI đánh giá đã bị dừng")
                self.db.question_evaluations.insert_one(evaluation, session=session)
                current = {
                    "_id": question["_id"],
                    "current_version_id": version["_id"],
                    "lifecycle_status": "ACTIVE",
                }
                if update_question:
                    matched = self.db.questions.update_one(
                        current,
                        {
                            "$set": {
                                "evaluation_status": "PASSED" if passed else "FAILED",
                                "quality_summary": quality_summary,
                                "updated_at": now,
                            }
                        },
                        session=session,
                    ).matched_count
                else:
                    matched = self.db.questions.count_documents(current, session=session)
                if not matched:
                    raise RuntimeError("VERSION_CONFLICT")
                self.db.audit_logs.insert_one(audit, session=session)
        return json_safe(evaluation)

    def ensure_question_readable(self, question_id: str, current_user: CurrentUser) -> None:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)

    def evaluate_manual(self, question_id: str, payload: EvaluationCreateRequest,
                        current_user: CurrentUser) -> dict:
        """A person's own scoring; recorded in the history without touching the AI evaluation state."""
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if not self._may_evaluate_manually(question, version, current_user):
            raise PermissionError(
                "Chỉ chủ câu hỏi, người được chia sẻ quyền sửa hoặc reviewer đang giữ câu được tự đánh giá"
            )
        if self._manual_evaluation_count(version["_id"]) >= MAX_MANUAL_EVALUATIONS_PER_VERSION:
            raise ValueError(
                f"Mỗi phiên bản chỉ được tự đánh giá tối đa {MAX_MANUAL_EVALUATIONS_PER_VERSION} lần"
            )
        manual = payload.model_copy(update={
            "trigger": "MANUAL",
            "evaluator_model_code": "human",
            "model_snapshot": {"model_code": "human", "provider": "HUMAN",
                               "evaluated_by_user_id": str(current_user.id)},
            "model_execution": {},
            "raw_model_response": None,
            "prompt_snapshot": {},
            "policy_snapshot": {},
            "evaluation_job_id": None,
        })
        return self.evaluate(question_id, manual, current_user.id, update_question=False)

    def _may_evaluate_manually(self, question: dict, version: dict, current_user: CurrentUser) -> bool:
        if current_user.role == "Admin" or self._owns_question(question, version, current_user.id):
            return True
        if current_user.id in set(question.get("shared_edit_user_ids") or []):
            return True
        assignment = question.get("review_assignment") or {}
        return (
            assignment.get("status") == "IN_REVIEW"
            and assignment.get("reviewer_user_id") == current_user.id
            and self._is_claimed(assignment, utc_now())
        )

    def _manual_evaluation_count(self, version_id: ObjectId) -> int:
        if settings.question_store == "postgres":
            return self.questions.manual_evaluation_count(version_id)
        if self.db is None:
            return 0
        return self.db.question_evaluations.count_documents(
            {"question_version_id": version_id, "trigger": "MANUAL"},
        )

    async def auto_evaluate(self, question_id: str, payload: AutoEvaluationRequest, user_id) -> dict:
        question, version = await asyncio.to_thread(self._pair, question_id)
        if question["current_version"] != payload.expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        source_chunks, retrieval_snapshot = await asyncio.to_thread(self._retrieve_evaluation_sources, version)
        grounded_version = {
            **version,
            "sources": [
                {
                    "chunk_id": source.get("chunk_id"),
                    "chunk_content_hash": source.get("content_hash"),
                    "citation_order": source.get("citation_order"),
                    "is_primary": source.get("is_primary"),
                    "context_excerpt": source.get("excerpt"),
                }
                for source in source_chunks
            ],
        }
        raw_model_response = None
        evaluator_code = payload.evaluator_model_code.strip()
        prompt_snapshot: dict = {}
        policy_snapshot: dict = {}
        duration_ms = None
        model_snapshot: dict = {}
        llm = None
        if evaluator_code.lower() in {"local-heuristic-evaluator-v1", "heuristic"}:
            scores, feedback, evidence = self._auto_scores(question, grounded_version)
            scores, feedback, evidence = self._apply_evaluation_guardrails(
                scores,
                feedback,
                evidence,
                grounded_version,
            )
            first_source = source_chunks[0]
            evidence.update(
                {
                    "mode": "heuristic",
                    "retrieval": retrieval_snapshot,
                    "citations": [
                        {
                            "claim": "Nguồn được dùng cho đánh giá heuristic",
                            "claim_type": "ANSWER",
                            "chunk_id": first_source.get("chunk_id"),
                            "exact_quote": first_source.get("excerpt", "")[:300],
                            "entailment": "SUPPORTED",
                            "verified": True,
                        }
                    ],
                }
            )
            evaluator_code = "local-heuristic-evaluator-v1"
        else:
            try:
                model_snapshot = await asyncio.to_thread(
                    resolve_model_snapshot,
                    evaluator_code,
                    capability=EVALUATION_CAPABILITY,
                    database=self.db,
                )
                model_snapshot = _limit_evaluation_output(model_snapshot)
                llm = get_llm_service(evaluator_code, model_snapshot=model_snapshot)
                policy = await asyncio.to_thread(self._policy)
                policy_snapshot = self._policy_snapshot(policy)
                prompt, prompt_snapshot, _ = await asyncio.to_thread(
                    self._build_evaluation_prompt,
                    question,
                    version,
                    policy,
                    source_chunks=source_chunks,
                )
                started = time.perf_counter()
                raw_model_response = await llm.generate_text(prompt)
                duration_ms = int((time.perf_counter() - started) * 1000)
                scores, feedback, evidence = await asyncio.to_thread(
                    self._finalize_llm_evaluation,
                    raw_model_response,
                    source_chunks,
                    grounded_version,
                    policy_snapshot,
                    llm=llm,
                )
                evidence = {
                    **evidence,
                    "mode": "local_llm",
                    "retrieval": retrieval_snapshot,
                }
            except Exception as exc:
                if not payload.fallback_to_heuristic:
                    raise ValueError(f"Local evaluator failed: {exc}") from exc
                scores, feedback, evidence = self._auto_scores(question, grounded_version)
                scores, feedback, evidence = self._apply_evaluation_guardrails(
                    scores,
                    feedback,
                    evidence,
                    grounded_version,
                )
                evaluator_code = "local-heuristic-evaluator-v1"
                feedback = {
                    **feedback,
                    "summary": (
                        "Local evaluator không chạy hoặc trả JSON không hợp lệ; "
                        "đã fallback sang heuristic nội bộ."
                    ),
                }
                evidence = {
                    **evidence,
                    "mode": "heuristic_fallback",
                    "retrieval": retrieval_snapshot,
                    "citations": [
                        {
                            "claim": "Nguồn được dùng cho đánh giá heuristic dự phòng",
                            "claim_type": "ANSWER",
                            "chunk_id": source_chunks[0].get("chunk_id"),
                            "exact_quote": source_chunks[0].get("excerpt", "")[:300],
                            "entailment": "SUPPORTED",
                            "verified": True,
                        }
                    ],
                    "fallback_reason": str(exc),
                    "raw_model_response": raw_model_response,
                }
        return await asyncio.to_thread(
            self.evaluate,
            question_id,
            EvaluationCreateRequest(
                expected_version=payload.expected_version,
                scores=scores,
                feedback=feedback,
                evidence=evidence,
                evaluator_model_code=evaluator_code,
                raw_model_response=raw_model_response,
                policy_snapshot=policy_snapshot,
                prompt_snapshot=prompt_snapshot,
                duration_ms=duration_ms,
                trigger="DIRECT_AUTO_EVALUATION",
                model_snapshot=model_snapshot,
                model_execution=(
                    get_llm_execution_snapshot(llm)
                    if llm is not None and evaluator_code != "local-heuristic-evaluator-v1"
                    else {}
                ),
            ),
            user_id,
        )

    @staticmethod
    def _policy_snapshot(policy: dict) -> dict:
        return {
            "id": policy.get("_id"),
            "name": policy.get("policy_name"),
            "version": policy.get("version"),
            "weights": policy.get("weights") or DEFAULT_WEIGHTS,
            "thresholds": policy.get("thresholds") or DEFAULT_THRESHOLDS,
        }

    @staticmethod
    def _evaluation_dedupe_key(version: dict, evaluator_model_code: str) -> str:
        material = "|".join(
            [
                str(version.get("_id")),
                str(version.get("content_hash") or ""),
                evaluator_model_code.strip().lower(),
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _supersede_previous_evaluation_jobs(
        self,
        question_id: ObjectId,
        current_version_id: ObjectId,
        superseding_job_id: ObjectId,
        now,
        *,
        session=None,
    ) -> int:
        error = {
            "message": "Evaluation được thay thế bởi phiên bản câu hỏi mới hơn",
            "stage": "SUPERSEDED",
            "at": now,
        }
        result = self.db.evaluation_jobs.update_many(
            {
                "question_id": question_id,
                "question_version_id": {"$ne": current_version_id},
                "status": {"$in": list(EVALUATION_ACTIVE_STATUSES)},
            },
            {
                "$set": {
                    "status": "STALE",
                    "error": error,
                    "superseded_by_job_id": superseding_job_id,
                    "finished_at": now,
                    "expires_at": now + timedelta(days=settings.job_retention_days),
                    "updated_at": now,
                },
                "$unset": {
                    "locked_by": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "next_attempt_at": "",
                },
            },
            session=session,
        )
        return result.modified_count

    def enqueue_auto_evaluation(
        self,
        question_id: str,
        *,
        expected_version: int,
        requested_by_user_id,
        evaluator_model_code: str = DEFAULT_EVALUATOR_MODEL_CODE,
        trigger: str = "REVIEWER_REQUEST",
        model_snapshot: dict | None = None,
        fallback_model_snapshot: dict | None = None,
        fallback_to_heuristic: bool = False,
    ) -> dict:
        question, version = self._pair(question_id)
        if question["current_version"] != expected_version:
            raise RuntimeError("VERSION_CONFLICT")

        evaluator_model_code = evaluator_model_code.strip() or DEFAULT_EVALUATOR_MODEL_CODE
        model_snapshot = model_snapshot or resolve_model_snapshot(
            evaluator_model_code,
            capability=EVALUATION_CAPABILITY,
            database=self.db,
        )
        model_snapshot = _limit_evaluation_output(model_snapshot)
        if settings.evaluation_fallback_provider and fallback_model_snapshot is None:
            fallback_model_snapshot = resolve_model_snapshot(
                settings.evaluation_fallback_provider,
                capability=EVALUATION_CAPABILITY,
                database=self.db,
            )
        fallback_model_snapshot = _limit_evaluation_output(fallback_model_snapshot)
        if question.get("evaluation_status") == "PASSED":
            raise ValueError("Câu hỏi đã có kết quả AI đạt cho phiên bản hiện tại")

        dedupe_key = self._evaluation_dedupe_key(version, evaluator_model_code)
        if self.evaluation_jobs is None:
            active_job = self.db.evaluation_jobs.find_one(
                {"dedupe_key": dedupe_key, "status": {"$in": list(EVALUATION_ACTIVE_STATUSES)}}
            )
            if active_job:
                return json_safe(active_job)

        policy = self._policy()
        _, prompt_snapshot, source_chunks = self._build_evaluation_prompt(question, version, policy)
        now = utc_now()
        # PostgreSQL tính attempt_no trong transaction enqueue.
        attempt_no = 0 if self.evaluation_jobs is not None else (
            self.db.evaluation_jobs.count_documents(
                {
                    "question_version_id": version["_id"],
                    "evaluator_model_code": evaluator_model_code,
                }
            )
            + 1
        )
        job = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "question_id": question["_id"],
            "question_version_id": version["_id"],
            "question_version": version["version"],
            "question_snapshot_hash": version.get("content_hash"),
            "dedupe_key": dedupe_key,
            "status": "QUEUED",
            "trigger": trigger,
            "requested_by_user_id": requested_by_user_id,
            "evaluator_model_code": evaluator_model_code,
            "model_snapshot": model_snapshot,
            "fallback_model_snapshot": fallback_model_snapshot,
            "fallback_to_heuristic": bool(fallback_to_heuristic),
            "policy_snapshot": self._policy_snapshot(policy),
            "prompt_snapshot": prompt_snapshot,
            "source_snapshot": [
                {
                    "label": source.get("label"),
                    "chunk_id": source.get("chunk_id"),
                    "content_hash": source.get("content_hash"),
                    "citation_order": source.get("citation_order"),
                    "excerpt_hash": source.get("excerpt_hash"),
                }
                for source in source_chunks
            ],
            "attempt_no": attempt_no,
            "processing_attempt_count": 0,
            "max_attempts": settings.job_max_attempts,
            "result": None,
            "error": None,
            "queued_at": now,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "duration_ms": None,
            "updated_at": now,
        }

        if self.evaluation_jobs is not None:
            # Enqueue khóa câu hỏi, dedupe job active và supersede job của version cũ.
            return json_safe(self.evaluation_jobs.enqueue(question["_id"], version["_id"], job))
        try:
            with mongo_transaction() as session:
                self.db.evaluation_jobs.insert_one(job, session=session)
                result = self.db.questions.update_one(
                    {
                        "_id": question["_id"],
                        "current_version_id": version["_id"],
                        "lifecycle_status": "ACTIVE",
                        "evaluation_status": {"$in": list(EVALUATION_RETRYABLE_STATUSES)},
                    },
                    {
                        "$set": {
                            "evaluation_status": "QUEUED",
                            "quality_summary": {
                                "latest_evaluation_job_id": job["_id"],
                                "evaluated_version_id": version["_id"],
                                "evaluation_queued_at": now,
                                "evaluator_model_code": evaluator_model_code,
                            },
                            "updated_at": now,
                        }
                    },
                    session=session,
                )
                if not result.matched_count:
                    self.db.evaluation_jobs.update_one(
                        {"_id": job["_id"]},
                        {
                            "$set": {
                                "status": "STALE",
                                "error": {
                                    "message": "Phiên bản câu hỏi đã đổi trước khi enqueue evaluation",
                                    "at": utc_now(),
                                },
                                "updated_at": utc_now(),
                            }
                        },
                        session=session,
                    )
                    raise RuntimeError("VERSION_CONFLICT")
                self._supersede_previous_evaluation_jobs(
                    question["_id"],
                    version["_id"],
                    job["_id"],
                    now,
                    session=session,
                )
        except DuplicateKeyError:
            active_job = self.db.evaluation_jobs.find_one(
                {"dedupe_key": dedupe_key, "status": {"$in": list(EVALUATION_ACTIVE_STATUSES)}}
            )
            if active_job:
                return json_safe(active_job)
            raise
        return json_safe(job)

    def mark_evaluation_enqueue_error(
        self,
        question_id: str,
        *,
        expected_version: int,
        evaluator_model_code: str,
        message: str,
    ) -> dict | None:
        if self.evaluation_jobs is not None:
            updated = self.evaluation_jobs.mark_enqueue_error(
                question_id, expected_version=expected_version,
                evaluator_model_code=evaluator_model_code, message=message,
            )
            return json_safe(updated) if updated else None
        now = utc_now()
        error = {"message": message, "at": now, "stage": "ENQUEUE"}
        updated = self.db.questions.find_one_and_update(
            {
                "_id": object_id(question_id, "question_id"),
                "current_version": expected_version,
                "lifecycle_status": "ACTIVE",
                "evaluation_status": {"$in": list(EVALUATION_RETRYABLE_STATUSES)},
            },
            {
                "$set": {
                    "evaluation_status": "ERROR",
                    "quality_summary": {
                        "evaluated_version_id": None,
                        "evaluator_model_code": evaluator_model_code,
                        "error": error,
                    },
                    "updated_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        return json_safe(updated) if updated else None

    def _mark_evaluation_job_error(
        self,
        job: dict,
        message: str,
        *,
        status: str = "ERROR",
        question_status: str = "ERROR",
        code: str = "EVALUATION_ERROR",
        evidence: dict | None = None,
        raw_model_response: str | None = None,
        duration_ms: int | None = None,
        dead_lettered: bool = False,
    ) -> dict:
        if self.evaluation_jobs is not None:
            saved = self.evaluation_jobs.mark_error(
                job["_id"], worker_id=job.get("locked_by"), message=message,
                status=status, question_status=question_status, code=code,
                evidence=evidence, raw_response_excerpt=raw_model_response,
                duration_ms=duration_ms, dead_lettered=dead_lettered,
            )
            if saved:
                return json_safe({**saved, "evidence": evidence or {}})
            # Mất lease hoặc job đã kết thúc ở nơi khác: giữ nguyên trạng thái đã lưu.
            return json_safe({**job, "status": status, "error": {"code": code, "message": message},
                              "evidence": evidence or {}})
        now = utc_now()
        error = {
            "code": code,
            "message": message,
            "raw_model_response_excerpt": (raw_model_response or "")[:1200] or None,
            "at": now,
        }
        with mongo_transaction() as session:
            job_fields = {
                "status": status,
                "error": error,
                "finished_at": now,
                "duration_ms": duration_ms,
                "updated_at": now,
            }
            if dead_lettered:
                job_fields["dead_lettered_at"] = now
            if status in {"ERROR", "STALE", "CANCELLED"}:
                job_fields["expires_at"] = now + timedelta(days=settings.job_retention_days)
            job_query = {"_id": job["_id"], "status": {"$in": ["QUEUED", "PROCESSING"]}}
            if job.get("locked_by"):
                job_query["locked_by"] = job["locked_by"]
            self.db.evaluation_jobs.update_one(
                job_query,
                {
                    "$set": job_fields,
                    "$unset": {"locked_by": "", "lease_expires_at": "", "heartbeat_at": ""},
                },
                session=session,
            )
            if status != "STALE":
                self.db.questions.update_one(
                    {
                        "_id": job["question_id"],
                        "current_version_id": job["question_version_id"],
                        "lifecycle_status": "ACTIVE",
                    },
                    {
                        "$set": {
                            "evaluation_status": question_status,
                            "quality_summary": {
                                "latest_evaluation_job_id": job["_id"],
                                "evaluated_version_id": job["question_version_id"],
                                "evaluator_model_code": job.get("evaluator_model_code"),
                                "error": error,
                                "evidence": evidence or {},
                            },
                            "updated_at": now,
                        }
                    },
                    session=session,
                )
        return json_safe(
            {
                **job,
                "status": status,
                "error": error,
                "evidence": evidence or {},
                "finished_at": now,
            }
        )

    def heartbeat_evaluation_job(self, job_id: str, worker_id: str) -> bool:
        if self.evaluation_jobs is not None:
            return self.evaluation_jobs.heartbeat(job_id, worker_id)
        now = utc_now()
        result = self.db.evaluation_jobs.update_one(
            {
                "_id": object_id(job_id, "evaluation_job_id"),
                "status": "PROCESSING",
                "locked_by": worker_id,
            },
            {
                "$set": {
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=settings.job_lease_seconds),
                    "updated_at": now,
                }
            },
        )
        return result.modified_count == 1

    def _retry_or_dead_letter_evaluation_job(
        self,
        job: dict,
        message: str,
        *,
        raw_model_response: str | None,
        duration_ms: int,
    ) -> dict:
        if self.evaluation_jobs is not None:
            saved = self.evaluation_jobs.retry_or_dead_letter(
                job["_id"], job.get("locked_by"), message,
                duration_ms=duration_ms, raw_response_excerpt=raw_model_response,
            )
            return json_safe(saved or {**job, "error": {"message": message}})
        attempts = int(job.get("processing_attempt_count") or 1)
        max_attempts = int(job.get("max_attempts") or settings.job_max_attempts)
        if attempts >= max_attempts:
            return self._mark_evaluation_job_error(
                job,
                message,
                raw_model_response=raw_model_response,
                duration_ms=duration_ms,
                dead_lettered=True,
            )
        now = utc_now()
        delay = min(
            settings.job_retry_base_seconds * (2 ** max(0, attempts - 1)),
            settings.job_retry_max_seconds,
        )
        error = {
            "message": message,
            "raw_model_response_excerpt": (raw_model_response or "")[:1200] or None,
            "at": now,
        }
        with mongo_transaction() as session:
            self.db.evaluation_jobs.update_one(
                {
                    "_id": job["_id"],
                    "status": "PROCESSING",
                    "locked_by": job.get("locked_by"),
                },
                {
                    "$set": {
                        "status": "QUEUED",
                        "error": error,
                        "next_attempt_at": now + timedelta(seconds=delay),
                        "updated_at": now,
                    },
                    "$unset": {"locked_by": "", "lease_expires_at": "", "heartbeat_at": ""},
                },
                session=session,
            )
            self.db.questions.update_one(
                {"_id": job["question_id"], "current_version_id": job["question_version_id"]},
                {
                    "$set": {
                        "evaluation_status": "QUEUED",
                        "quality_summary.last_retry_error": error,
                        "updated_at": now,
                    }
                },
                session=session,
            )
        return json_safe({**job, "status": "QUEUED", "error": error})

    def _claim_evaluation_job_postgres(
        self, job_id: str, worker_id: str,
    ) -> tuple[dict, dict, dict] | None:
        # Claim đã kiểm tra version, đánh STALE nếu lệch và chuyển câu hỏi sang PROCESSING.
        job = self.evaluation_jobs.claim(job_id, worker_id)
        if not job:
            return None
        pair = self.questions.find_pair(job["question_id"])
        if not pair or pair[1]["_id"] != job["question_version_id"]:
            self._mark_evaluation_job_error(
                job, "Phiên bản câu hỏi đã thay đổi trước khi AI đánh giá", status="STALE",
            )
            return None
        return job, pair[0], pair[1]

    def _save_evaluation_job_snapshots(self, job_id: ObjectId, fields: dict) -> None:
        if self.evaluation_jobs is not None:
            self.evaluation_jobs.update_snapshots(job_id, fields)
            return
        self.db.evaluation_jobs.update_one(
            {"_id": job_id, "status": "PROCESSING"},
            {"$set": {**fields, "updated_at": utc_now()}},
        )

    def _finish_evaluation_job(self, job: dict, worker_id: str, result: dict, **fields) -> None:
        if self.evaluation_jobs is not None:
            self.evaluation_jobs.finish(job["_id"], worker_id, result, **fields)
            return
        finished_at = utc_now()
        self.db.evaluation_jobs.update_one(
            {"_id": job["_id"], "status": "PROCESSING", "locked_by": worker_id},
            {
                "$set": {
                    "status": "COMPLETED",
                    "result": result,
                    **fields,
                    "finished_at": finished_at,
                    "expires_at": finished_at + timedelta(days=settings.job_retention_days),
                    "updated_at": finished_at,
                },
                "$unset": {
                    "locked_by": "",
                    "lease_expires_at": "",
                    "heartbeat_at": "",
                    "next_attempt_at": "",
                },
            },
        )

    def evaluation_job_state(self, job_id: str) -> dict | None:
        """Trạng thái tối thiểu để worker biết job còn active hay đã bị dừng."""
        if self.evaluation_jobs is not None:
            return self.evaluation_jobs.get(job_id)
        return self.db.evaluation_jobs.find_one(
            {"_id": object_id(job_id, "evaluation_job_id")},
            {"status": 1, "locked_by": 1, "error.stage": 1},
        )

    async def process_evaluation_job(self, job_id: str, worker_id: str) -> dict | None:
        if self.evaluation_jobs is not None:
            claimed = await asyncio.to_thread(
                self._claim_evaluation_job_postgres, job_id, worker_id,
            )
            if not claimed:
                return None
            job, question, version = claimed
            return await self._run_evaluation_job(job, question, version, worker_id)

        job_oid = object_id(job_id, "evaluation_job_id")
        now = utc_now()
        job = await asyncio.to_thread(
            self.db.evaluation_jobs.find_one_and_update,
            {
                "_id": job_oid,
                "$or": [
                    {
                        "status": "QUEUED",
                        "$or": [
                            {"next_attempt_at": {"$exists": False}},
                            {"next_attempt_at": {"$lte": now}},
                        ],
                    },
                    {"status": "PROCESSING", "lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "status": "PROCESSING",
                    "locked_by": worker_id,
                    "started_at": now,
                    "heartbeat_at": now,
                    "lease_expires_at": now + timedelta(seconds=settings.job_lease_seconds),
                    "updated_at": now,
                },
                "$inc": {"processing_attempt_count": 1},
                "$unset": {"next_attempt_at": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if not job:
            return None

        question, version = await asyncio.gather(
            asyncio.to_thread(
                self.db.questions.find_one,
                {
                    "_id": job["question_id"],
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                },
            ),
            asyncio.to_thread(
                self.db.question_versions.find_one,
                {"_id": job["question_version_id"]},
            ),
        )
        if not question or not version or question.get("current_version_id") != job["question_version_id"]:
            return await asyncio.to_thread(
                self._mark_evaluation_job_error,
                job,
                "Phiên bản câu hỏi đã thay đổi trước khi AI đánh giá",
                status="STALE",
            )

        await asyncio.to_thread(
            self.db.questions.update_one,
            {
                "_id": question["_id"],
                "current_version_id": version["_id"],
                "evaluation_status": {"$in": ["QUEUED", "PROCESSING"]},
            },
            {
                "$set": {
                    "evaluation_status": "PROCESSING",
                    "quality_summary.latest_evaluation_job_id": job["_id"],
                    "quality_summary.evaluation_started_at": now,
                    "updated_at": now,
                }
            },
        )
        return await self._run_evaluation_job(job, question, version, worker_id)

    async def _run_evaluation_job(
        self, job: dict, question: dict, version: dict, worker_id: str,
    ) -> dict:
        raw_model_response = None
        started = time.perf_counter()
        retrieval_snapshot: dict = {}
        try:
            policy_snapshot = job.get("policy_snapshot")
            if not policy_snapshot:
                policy_snapshot = await asyncio.to_thread(self._policy)
            source_chunks, retrieval_snapshot = await asyncio.to_thread(
                self._retrieve_evaluation_sources,
                version,
            )
            grounded_version = {
                **version,
                "sources": [
                    {
                        "chunk_id": source.get("chunk_id"),
                        "chunk_content_hash": source.get("content_hash"),
                        "citation_order": source.get("citation_order"),
                        "is_primary": source.get("is_primary"),
                        "context_excerpt": source.get("excerpt"),
                    }
                    for source in source_chunks
                ],
            }
            prompt, prompt_snapshot, _ = await asyncio.to_thread(
                self._build_evaluation_prompt,
                question,
                version,
                policy_snapshot,
                source_chunks=source_chunks,
            )
            await asyncio.to_thread(
                self._save_evaluation_job_snapshots,
                job["_id"],
                {
                    "prompt_snapshot": prompt_snapshot,
                    "retrieval_snapshot": retrieval_snapshot,
                    "source_snapshot": [
                        {
                            key: value
                            for key, value in source.items()
                            if key != "excerpt"
                        }
                        for source in source_chunks
                    ],
                },
            )
            prompt, prompt_snapshot, effective_model_snapshot = _prepare_evaluation_attempt(
                prompt,
                prompt_snapshot,
                job.get("model_snapshot"),
                int(job.get("processing_attempt_count") or 1),
            )
            llm = get_llm_service(
                job.get("evaluator_model_code") or DEFAULT_EVALUATOR_MODEL_CODE,
                settings.evaluation_fallback_provider,
                model_snapshot=effective_model_snapshot,
                fallback_model_snapshot=job.get("fallback_model_snapshot"),
            )
            heuristic_fallback = False
            try:
                raw_model_response = await llm.generate_text(prompt)
                scores, feedback, evidence = await asyncio.to_thread(
                    self._finalize_llm_evaluation,
                    raw_model_response,
                    source_chunks,
                    grounded_version,
                    policy_snapshot,
                    llm=llm,
                )
            except Exception as exc:
                if not job.get("fallback_to_heuristic"):
                    raise
                heuristic_fallback = True
                scores, feedback, evidence = self._auto_scores(question, grounded_version)
                scores, feedback, evidence = self._apply_evaluation_guardrails(
                    scores,
                    feedback,
                    evidence,
                    grounded_version,
                )
                feedback = {
                    **feedback,
                    "summary": "AI đánh giá lỗi; hệ thống đã dùng heuristic dự phòng.",
                }
                evidence = {**evidence, "fallback_reason": str(exc)}
            duration_ms = int((time.perf_counter() - started) * 1000)
            evidence = {
                **evidence,
                "mode": "heuristic_fallback" if heuristic_fallback else "local_llm",
                "evaluation_job_id": str(job["_id"]),
                "retrieval": json_safe(retrieval_snapshot),
                "source_snapshot": json_safe(
                    [
                        {key: value for key, value in source.items() if key != "excerpt"}
                        for source in source_chunks
                    ]
                ),
            }
            evaluation_payload = EvaluationCreateRequest(
                expected_version=version["version"],
                scores=scores,
                feedback=feedback,
                evidence=evidence,
                evaluator_model_code=job.get("evaluator_model_code") or DEFAULT_EVALUATOR_MODEL_CODE,
                raw_model_response=raw_model_response,
                policy_snapshot=policy_snapshot,
                prompt_snapshot=prompt_snapshot,
                duration_ms=duration_ms,
                evaluation_job_id=str(job["_id"]),
                trigger=job.get("trigger"),
                model_snapshot=(
                    {
                        "model_code": "local-heuristic-evaluator-v1",
                        "model_name": "Local heuristic evaluator",
                        "runtime": "INTERNAL",
                    }
                    if heuristic_fallback
                    else effective_model_snapshot or {}
                ),
                model_execution=get_llm_execution_snapshot(llm),
            )
            evaluation = await asyncio.to_thread(
                self.evaluate,
                str(question["_id"]),
                evaluation_payload,
                job.get("requested_by_user_id"),
                require_active_job=True,
            )
            await asyncio.to_thread(
                self._finish_evaluation_job,
                job,
                worker_id,
                {
                    "evaluation_id": object_id(evaluation.get("_id"), "evaluation_id"),
                    "passed": evaluation.get("passed"),
                    "overall_score": (evaluation.get("scores") or {}).get("overall"),
                    "color": evaluation.get("color"),
                },
                duration_ms=duration_ms,
                model_execution=get_llm_execution_snapshot(llm),
            )
            return evaluation
        except EvaluationInterruptedError:
            # Job đã được đánh dấu dừng ở nơi khác; không ghi đè trạng thái.
            return json_safe({**job, "status": "CANCELLED"})
        except EvidenceGateError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            blocked_evidence = dict(exc.evidence)
            if retrieval_snapshot:
                blocked_evidence["retrieval"] = json_safe(retrieval_snapshot)
            return await asyncio.to_thread(
                self._mark_evaluation_job_error,
                job,
                str(exc),
                status="BLOCKED",
                question_status=(
                    "EVIDENCE_VALIDATION_FAILED"
                    if exc.code == "EVIDENCE_VALIDATION_FAILED"
                    else "INSUFFICIENT_EVIDENCE"
                ),
                code=exc.code,
                evidence=blocked_evidence,
                raw_model_response=raw_model_response,
                duration_ms=duration_ms,
            )
        except RuntimeError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            if str(exc) == "VERSION_CONFLICT":
                return await asyncio.to_thread(
                    self._mark_evaluation_job_error,
                    job,
                    "Phiên bản câu hỏi đã thay đổi trong lúc AI đánh giá",
                    status="STALE",
                    raw_model_response=raw_model_response,
                    duration_ms=duration_ms,
                )
            return await asyncio.to_thread(
                self._retry_or_dead_letter_evaluation_job,
                job,
                str(exc),
                raw_model_response=raw_model_response,
                duration_ms=duration_ms,
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            return await asyncio.to_thread(
                self._retry_or_dead_letter_evaluation_job,
                job,
                str(exc),
                raw_model_response=raw_model_response,
                duration_ms=duration_ms,
            )

    def _lock_expires_at(self, now) -> object:
        return now + timedelta(minutes=max(1, settings.review_lock_timeout_minutes))

    def _assignment_expires_at(self, now) -> object:
        return now + timedelta(hours=max(1, settings.review_assignment_timeout_hours))

    def _review_author_ids(self, question: dict, version: dict) -> set[str]:
        """Everyone who created, edited any version of, submitted, or may edit the question."""
        submission = question.get("review_submission") or {}
        ids = {
            question.get("created_by_user_id"),
            version.get("created_by_user_id"),
            submission.get("submitted_by_user_id"),
            question.get("submitted_by_user_id"),
            *(question.get("author_user_ids") or []),
            *(question.get("shared_edit_user_ids") or []),
        }
        list_versions = getattr(self.questions, "list_versions", None)
        if list_versions is not None and question.get("_id") is not None:
            ids.update(item.get("created_by_user_id") for item in list_versions(question["_id"]) or [])
        return {str(item) for item in ids if item}

    def _ensure_not_author(self, question: dict, version: dict, current_user: CurrentUser) -> None:
        if str(current_user.id) in self._review_author_ids(question, version):
            raise PermissionError("Bạn không thể kiểm duyệt câu hỏi do chính mình tạo, chỉnh sửa hoặc gửi duyệt")

    @staticmethod
    def _reviewer_covers(question: dict, version: dict, reviewer: dict) -> bool:
        if reviewer.get("role") == "Admin":
            return True
        scope = ReviewerScope(reviewer["_id"], reviewer.get("review_subject_ids") or [])
        return scope.covers_question(question, version)

    def _ensure_reviewer_covers(self, question: dict, version: dict, current_user: CurrentUser) -> None:
        """Reviewers act only on subjects in review_subject_ids (empty means none)."""
        if current_user.role == "Admin":
            return
        record = find_user_record(self.db, current_user.id) or {"_id": current_user.id}
        if not self._reviewer_covers(question, version, record):
            raise PermissionError("Câu hỏi không thuộc học phần bạn được phân công kiểm duyệt")

    def _assignment_available_filter(self, current_user: CurrentUser, now) -> list[dict]:
        return [
            {"review_assignment": {"$exists": False}},
            {"review_assignment.status": "UNASSIGNED"},
            {"review_assignment.reviewer_user_id": current_user.id},
            {"review_assignment.lock_expires_at": None},
            {"review_assignment.lock_expires_at": {"$lte": now}},
        ]

    def _other_eligible_reviewer_exists(self, question: dict, version: dict, current_user: CurrentUser) -> bool:
        excluded = self._review_author_ids(question, version) | {str(current_user.id)}
        for user in review_candidate_users(self.db):
            if str(user["_id"]) in excluded or not _user_can_review(user):
                continue
            if self._reviewer_covers(question, version, user):
                return True
        return False

    def _ensure_break_glass_allowed(
        self, question: dict, version: dict, current_user: CurrentUser, reason: str,
    ) -> None:
        """An Admin may decide on their own question only when nobody else can."""
        if not settings.admin_self_review_break_glass:
            raise PermissionError("Bạn là người tạo/sửa câu này nên không thể tự kiểm duyệt")
        if self._other_eligible_reviewer_exists(question, version, current_user):
            raise PermissionError("Còn reviewer khác phụ trách câu này; hãy phân công cho họ thay vì tự duyệt")
        if len(reason) < 10:
            raise ValueError("Tự duyệt khẩn cấp cần ghi lý do tối thiểu 10 ký tự")

    @staticmethod
    def _break_glass_audit_fields(question: dict, version: dict, review: dict, current_user: CurrentUser) -> dict:
        return {
            "action": "QUESTION_BREAK_GLASS_REVIEW",
            "entity_type": "question",
            "entity_id": question["_id"],
            "entity_version_id": version["_id"],
            "actor_user_id": current_user.id,
            "actor_role": current_user.role,
            "metadata": {
                "review_id": review["_id"],
                "decision": review["decision"],
                "reason": review["self_review_reason"],
                "severity": "HIGH",
            },
            "created_at": review["reviewed_at"],
        }

    def _notify_admins_break_glass(
        self, question: dict, version: dict, review: dict, current_user: CurrentUser,
    ) -> None:
        try:
            recipients = [item for item in active_admin_ids(self.db) if item != current_user.id]
            code = question.get("question_code", "Câu hỏi")
            NotificationService(self.db).create_many([
                {
                    "recipient_user_id": admin_id,
                    "actor_user_id": current_user.id,
                    "type": "QUESTION_BREAK_GLASS_REVIEW",
                    "title": f"{code}: quản trị viên tự duyệt khẩn cấp",
                    "body": f"Lý do: {review['self_review_reason']}",
                    "link": f"/kiem-duyet?questionId={question['_id']}",
                    "entity": {"type": "question", "id": str(question["_id"]), "version_id": str(version["_id"])},
                }
                for admin_id in recipients
            ])
        except Exception as exc:  # noqa: BLE001 - notification is best-effort
            logger.warning("Break-glass notification failed for %s: %s", question["_id"], exc)

    def _ensure_review_lock(self, question: dict, current_user: CurrentUser, now) -> None:
        """Every decision, Admin included, requires holding the claim."""
        assignment = question.get("review_assignment") or {}
        if assignment.get("status") != "IN_REVIEW":
            raise PermissionError("Bạn cần claim câu hỏi trước khi kiểm duyệt")
        if assignment.get("reviewer_user_id") != current_user.id:
            raise PermissionError("Câu hỏi đang được Reviewer khác xử lý")
        lock_expires_at = _as_aware_utc(assignment.get("lock_expires_at"))
        if lock_expires_at and lock_expires_at <= _as_aware_utc(now):
            raise ValueError("Review lock đã hết hạn; vui lòng claim lại")

    def _find_assignable_reviewer(self, reviewer_user_id: str) -> dict:
        reviewer_oid = object_id(reviewer_user_id, "reviewer_user_id")
        reviewer = find_user_record(self.db, reviewer_oid, active_only=True)
        if not reviewer or not _user_can_review(reviewer):
            raise ValueError("Reviewer không tồn tại hoặc không còn hoạt động")
        return reviewer

    def _assignment_audit(
        self,
        *,
        action: str,
        question: dict,
        version: dict,
        current_user: CurrentUser,
        before: dict | None = None,
        after: dict | None = None,
        metadata: dict | None = None,
        path: str = "review_assignment",
    ) -> None:
        write_audit_event(
            self.db,
            action=action,
            entity_type="question",
            entity_id=question["_id"],
            entity_version_id=version["_id"],
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            changes=[{"path": path, "old_value": before or {}, "new_value": after or {}}],
            before_hash=version["content_hash"],
            after_hash=version["content_hash"],
            metadata=metadata or {},
        )

    def _takeover_reason(self, question: dict, current_user: CurrentUser, reason: str | None, now):
        """Return (holder, reason) when an admin takes a question another reviewer holds."""
        holder = active_holder_id(question.get("review_assignment") or {}, now)
        if holder is None or holder == current_user.id:
            return None, None
        if current_user.role != "Admin":
            raise PermissionError("Câu hỏi đang được Reviewer khác xử lý")
        reason = (reason or "").strip()
        if len(reason) < MIN_TAKEOVER_REASON_LENGTH:
            raise ValueError("Câu hỏi đang được Reviewer khác giữ; cần nêu lý do để tiếp nhận")
        return holder, reason

    def notify_takeover(self, question: dict, version: dict, holder, current_user: CurrentUser,
                         reason: str, *, archived: bool = False) -> None:
        safe_notify_review_taken_over(
            database=self.db, question=question, version=version, reviewer_user_id=holder,
            actor_user_id=current_user.id, reason=reason, archived=archived,
        )

    def claim_review(self, question_id: str, current_user: CurrentUser, reason: str | None = None) -> dict:
        question, version = self._pair(question_id)
        if current_user.role != "Admin":
            self._ensure_reviewer_covers(question, version, current_user)
            self._ensure_not_author(question, version, current_user)
        holder, takeover_reason = self._takeover_reason(question, current_user, reason, utc_now())
        if settings.question_store == "postgres":
            now = utc_now()
            updated = self.questions.claim_review(
                question_id, actor_user_id=current_user.id,
                actor_role=current_user.role,
                lock_expires_at=self._lock_expires_at(now), now=now,
                max_held=max(1, settings.review_max_held_questions),
                takeover_reason=takeover_reason,
            )
            if holder is not None:
                self.notify_takeover(updated[0], updated[1], holder, current_user, takeover_reason)
            return serialize_question(*updated)
        if question["review_status"] != "PENDING":
            raise ValueError("Chỉ câu hỏi đang chờ duyệt mới có thể claim")
        if current_user.role != "Admin":
            max_held = max(1, settings.review_max_held_questions)
            held = self.db.questions.count_documents({
                "_id": {"$ne": question["_id"]},
                "lifecycle_status": "ACTIVE",
                "review_status": "PENDING",
                "review_assignment.status": "IN_REVIEW",
                "review_assignment.reviewer_user_id": current_user.id,
                "review_assignment.lock_expires_at": {"$gt": utc_now()},
            })
            if held >= max_held:
                raise ValueError(review_hold_limit_message(max_held))
        secondary = question.get("secondary_review") or {}
        if (
            secondary.get("status") == "AWAITING_SECONDARY"
            and secondary.get("primary_reviewer_user_id") == current_user.id
        ):
            raise PermissionError("Người duyệt lần đầu không được nhận lượt duyệt lần hai")
        now = utc_now()
        previous_assignment = question.get("review_assignment") or {}
        assignment = {
            "status": "IN_REVIEW",
            "reviewer_user_id": current_user.id,
            "assigned_by_user_id": previous_assignment.get("assigned_by_user_id")
            or current_user.id,
            "assigned_at": previous_assignment.get("assigned_at") or now,
            "claimed_at": now,
            "lock_expires_at": self._lock_expires_at(now),
            "renewal_count": carried_renewal_count(previous_assignment, current_user.id),
            "last_released_at": None,
            "release_reason": None,
        }
        query = {
            "_id": question["_id"],
            "current_version_id": version["_id"],
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
        }
        if holder is not None:
            # The takeover applies only to the holder the admin saw.
            query["review_assignment.reviewer_user_id"] = holder
        else:
            query["$or"] = self._assignment_available_filter(current_user, now)
        updated = self.db.questions.find_one_and_update(
            query,
            {"$set": {"review_assignment": assignment, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            raise PermissionError("Câu hỏi đang được Reviewer khác xử lý")
        self._assignment_audit(
            action="QUESTION_REVIEW_TAKEN_OVER" if holder is not None else "QUESTION_REVIEW_CLAIMED",
            question=updated,
            version=version,
            current_user=current_user,
            before=question.get("review_assignment"),
            after=assignment,
            metadata={"reason": takeover_reason, "previous_reviewer_user_id": str(holder)} if holder else None,
        )
        if holder is not None:
            self.notify_takeover(updated, version, holder, current_user, takeover_reason)
        return serialize_question(updated, version)

    def release_review(self, question_id: str, current_user: CurrentUser) -> dict:
        if settings.question_store == "postgres":
            now = utc_now()
            updated = self.questions.release_review(
                question_id, actor_user_id=current_user.id,
                actor_role=current_user.role,
                assignment=_empty_review_assignment(now, "released"), now=now,
            )
            return serialize_question(*updated)
        pair = self._pair(question_id)
        question, version = pair
        now = utc_now()
        query = {
            "_id": question["_id"],
            "current_version_id": version["_id"],
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
        }
        if current_user.role != "Admin":
            query["review_assignment.reviewer_user_id"] = current_user.id
        assignment = released_assignment(
            _empty_review_assignment(now, "released"), question.get("review_assignment") or {},
        )
        updated = self.db.questions.find_one_and_update(
            query,
            {"$set": {"review_assignment": assignment, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            raise PermissionError("Bạn không thể release assignment này")
        self._assignment_audit(
            action="QUESTION_REVIEW_RELEASED",
            question=updated,
            version=version,
            current_user=current_user,
            before=question.get("review_assignment"),
            after=assignment,
        )
        return serialize_question(updated, version)

    def renew_review(self, question_id: str, current_user: CurrentUser) -> dict:
        """Extend the caller's review lock while the review desk stays open.

        An expired lock can still be renewed as long as nobody else has
        claimed the question in the meantime. Each claim allows at most
        ``REVIEW_MAX_LOCK_RENEWALS`` renewals.
        """
        max_renewals = max(0, settings.review_max_lock_renewals)
        if settings.question_store == "postgres":
            now = utc_now()
            updated = self.questions.renew_review(
                question_id, actor_user_id=current_user.id,
                lock_expires_at=self._lock_expires_at(now), now=now,
                max_renewals=max_renewals,
            )
            return serialize_question(*updated)
        question, version = self._pair(question_id)
        now = utc_now()
        held_by_caller = {
            "_id": question["_id"],
            "current_version_id": version["_id"],
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
            "review_assignment.status": "IN_REVIEW",
            "review_assignment.reviewer_user_id": current_user.id,
        }
        updated = self.db.questions.find_one_and_update(
            {
                **held_by_caller,
                "$or": [
                    {"review_assignment.renewal_count": {"$exists": False}},
                    {"review_assignment.renewal_count": {"$lt": max_renewals}},
                ],
            },
            {
                "$set": {
                    "review_assignment.lock_expires_at": self._lock_expires_at(now),
                    "updated_at": now,
                },
                "$inc": {"review_assignment.renewal_count": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            if self.db.questions.find_one(held_by_caller, {"_id": 1}):
                raise ValueError(review_renewal_limit_message(max_renewals))
            raise PermissionError("Bạn không còn giữ khóa kiểm duyệt câu hỏi này")
        return serialize_question(updated, version)

    def release_assignments_for_reviewer(
        self,
        reviewer_user_id,
        current_user: CurrentUser | None,
        reason: str,
        subject_ids: list | None = None,
    ) -> int:
        """Return pending questions held by a reviewer to the shared queue.

        ``subject_ids`` limits the release to those subjects (None means all).
        """
        now = utc_now()
        if settings.question_store == "postgres":
            return self.questions.release_reviewer_assignments(
                reviewer_user_id, reason=reason,
                actor_user_id=current_user.id if current_user else None,
                actor_role=current_user.role if current_user else None, now=now,
                subject_ids=subject_ids,
            )
        query = {
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
            "review_assignment.status": {"$in": ["ASSIGNED", "IN_REVIEW"]},
            "review_assignment.reviewer_user_id": reviewer_user_id,
        }
        if subject_ids is not None:
            if not subject_ids:
                return 0
            values = [str(item) for item in subject_ids]
            query["subject_id"] = {"$in": [*values, *(ObjectId(item) for item in values if ObjectId.is_valid(item))]}
        held = list(self.db.questions.find(query))
        released = 0
        for question in held:
            assignment = _empty_review_assignment(now, reason)
            updated = self.db.questions.find_one_and_update(
                {
                    "_id": question["_id"],
                    "review_status": "PENDING",
                    "review_assignment.reviewer_user_id": reviewer_user_id,
                },
                {"$set": {"review_assignment": assignment, "updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            if not updated:
                continue
            released += 1
            write_audit_event(
                self.db,
                action="QUESTION_REVIEW_RELEASED",
                entity_type="question",
                entity_id=question["_id"],
                entity_version_id=question.get("current_version_id"),
                actor_user_id=current_user.id if current_user else None,
                actor_role=current_user.role if current_user else None,
                service_name=None if current_user else "user_management",
                changes=[
                    {
                        "path": "review_assignment",
                        "old_value": question.get("review_assignment") or {},
                        "new_value": assignment,
                    }
                ],
                metadata={"reason": reason, "reviewer_user_id": reviewer_user_id},
                created_at=now,
            )
        return released

    def assign_review(
        self,
        question_id: str,
        payload: ReviewAssignmentRequest,
        current_user: CurrentUser,
    ) -> dict:
        pair = self._pair(question_id)
        question, version = pair
        if question["review_status"] != "PENDING":
            raise ValueError("Chỉ câu hỏi đang chờ duyệt mới có thể phân công")
        now = utc_now()
        if payload.reviewer_user_id:
            reviewer = self._find_assignable_reviewer(payload.reviewer_user_id)
            if str(reviewer["_id"]) in self._review_author_ids(question, version):
                raise ValueError("Không thể giao câu hỏi cho người đã tạo, chỉnh sửa hoặc gửi duyệt câu này")
            if not self._reviewer_covers(question, version, reviewer):
                raise ValueError("Reviewer không được phân công kiểm duyệt học phần của câu hỏi này")
            secondary = question.get("secondary_review") or {}
            if (
                secondary.get("status") == "AWAITING_SECONDARY"
                and reviewer["_id"] == secondary.get("primary_reviewer_user_id")
            ):
                raise ValueError("Không thể giao lượt duyệt lần hai cho người duyệt lần đầu")
            assignment = {
                "status": "ASSIGNED",
                "reviewer_user_id": reviewer["_id"],
                "assigned_by_user_id": current_user.id,
                "assigned_at": now,
                "claimed_at": None,
                # Reserve the question for the assignee for the assignment
                # window, not the short interactive review-lock window.
                "lock_expires_at": self._assignment_expires_at(now),
                "last_released_at": None,
                "release_reason": payload.note or None,
            }
            action = "QUESTION_REVIEW_ASSIGNED"
        else:
            assignment = _empty_review_assignment(now, payload.note or "unassigned")
            action = "QUESTION_REVIEW_UNASSIGNED"
        outbox = notification_outbox()
        if outbox is not None and payload.reviewer_user_id:
            NotificationService(self.db, sink=outbox).notify_review_assigned(
                question=question, version=version,
                reviewer_user_id=assignment["reviewer_user_id"],
                actor_user_id=current_user.id,
            )
        if settings.question_store == "postgres":
            updated, version = self.questions.assign_review(
                question_id, expected_version_id=version["_id"],
                assignment=assignment, actor_user_id=current_user.id,
                actor_role=current_user.role, action=action, now=now,
                note=payload.note, notifications=outbox,
            )
        else:
            updated = self.db.questions.find_one_and_update(
                {
                    "_id": question["_id"],
                    "current_version_id": version["_id"],
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                },
                {"$set": {"review_assignment": assignment, "updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
        if not updated:
            raise RuntimeError("VERSION_CONFLICT")
        if settings.question_store != "postgres":
            self._assignment_audit(
                action=action,
                question=updated,
                version=version,
                current_user=current_user,
                before=question.get("review_assignment"),
                after=assignment,
                metadata={"note": payload.note},
            )
        if payload.reviewer_user_id and outbox is None:
            safe_notify_review_assigned(
                database=self.db,
                question=updated,
                version=version,
                reviewer_user_id=assignment["reviewer_user_id"],
                actor_user_id=current_user.id,
            )
        return serialize_question(updated, version)

    def auto_assign_reviews(self, payload: AutoAssignRequest, current_user: CurrentUser) -> dict:
        """Distribute open pending questions across active reviewers (and Admins on request).

        Open means unassigned, or held past its lock/assignment window. Each
        question goes to an eligible reviewer (not its author, not the primary
        reviewer of a pending second review), preferring reviewers whose
        review_subject_ids cover the question's subject, then the lightest
        current load. Non-Admin reviewers only take subjects in their
        review_subject_ids; with none assigned they take nothing.
        """
        now = utc_now()
        question_ids = (
            [object_id(item, "question_id") for item in payload.question_ids]
            if payload.question_ids
            else None
        )
        if settings.question_store == "postgres":
            open_pairs = self.questions.open_review_pairs(
                now, question_ids=question_ids, limit=payload.limit,
            )
            questions = [question for question, _version in open_pairs]
            version_authors = {
                version["_id"]: version.get("created_by_user_id")
                for _question, version in open_pairs
            }
        else:
            query: dict = {
                "schema_version": SCHEMA_VERSION,
                "lifecycle_status": "ACTIVE",
                "review_status": "PENDING",
                "$or": [
                    {"review_assignment": {"$exists": False}},
                    {"review_assignment.status": {"$exists": False}},
                    {"review_assignment.status": "UNASSIGNED"},
                    {"review_assignment.lock_expires_at": {"$lte": now}},
                ],
            }
            if question_ids is not None:
                query["_id"] = {"$in": question_ids}
            questions = list(
                self.db.questions.find(query)
                .sort("review_submission.submitted_at", 1)
                .limit(payload.limit)
            )
            version_ids = [question["current_version_id"] for question in questions]
            version_authors = {
                version["_id"]: version.get("created_by_user_id")
                for version in self.db.question_versions.find(
                    {"_id": {"$in": version_ids}},
                    {"created_by_user_id": 1},
                )
            } if version_ids else {}

        reviewers = [
            reviewer
            for reviewer in review_candidate_users(self.db)
            if _user_can_review(reviewer) and (payload.include_admins or reviewer.get("role") != "Admin")
        ]
        strict = payload.subject_mode == "strict"
        covered_subjects = {
            str(subject_id)
            for reviewer in reviewers
            for subject_id in reviewer.get("review_subject_ids") or []
        }
        loads = {reviewer["_id"]: 0 for reviewer in reviewers}
        if settings.question_store == "postgres":
            held_questions = self.questions.held_reviews(active_at=now)
        else:
            held_questions = self.db.questions.find(
                {
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                    "review_assignment.status": {"$in": ["ASSIGNED", "IN_REVIEW"]},
                    "review_assignment.lock_expires_at": {"$gt": now},
                },
                {"review_assignment.reviewer_user_id": 1},
            )
        for held in held_questions:
            reviewer_id = (held.get("review_assignment") or {}).get("reviewer_user_id")
            if reviewer_id in loads:
                loads[reviewer_id] += 1

        assigned: list[dict] = []
        skipped: list[dict] = []
        for question in questions:
            code = question.get("question_code")
            if not reviewers:
                skipped.append({"question_id": question["_id"], "question_code": code, "reason": "NO_REVIEWERS"})
                continue
            excluded = self._review_author_ids(
                question, {"created_by_user_id": version_authors.get(question.get("current_version_id"))},
            )
            secondary = question.get("secondary_review") or {}
            if secondary.get("status") == "AWAITING_SECONDARY" and secondary.get("primary_reviewer_user_id"):
                excluded.add(str(secondary["primary_reviewer_user_id"]))
            subject_id = str(question["subject_id"]) if question.get("subject_id") else None
            candidates = []
            for reviewer in reviewers:
                if str(reviewer["_id"]) in excluded or loads[reviewer["_id"]] >= payload.max_load_per_reviewer:
                    continue
                specialist = subject_id is not None and subject_id in {
                    str(item) for item in reviewer.get("review_subject_ids") or []
                }
                if not specialist and reviewer.get("role") != "Admin":
                    continue
                if strict and not specialist:
                    continue
                candidates.append((0 if specialist else 1, loads[reviewer["_id"]], str(reviewer["_id"]), reviewer))
            if not candidates:
                reason = (
                    "NO_SUBJECT_SPECIALIST"
                    if strict and subject_id not in covered_subjects
                    else "NO_ELIGIBLE_REVIEWER"
                )
                skipped.append({"question_id": question["_id"], "question_code": code, "reason": reason})
                continue
            reviewer = min(candidates, key=lambda item: item[:3])[3]
            try:
                self.assign_review(
                    str(question["_id"]),
                    ReviewAssignmentRequest(reviewer_user_id=str(reviewer["_id"]), note="auto_assign"),
                    current_user,
                )
            except KeyError:
                # A data bug, not a business rule: do not hide it as a skip.
                raise
            except (LookupError, PermissionError, RuntimeError, ValueError) as exc:
                skipped.append({"question_id": question["_id"], "question_code": code, "reason": str(exc)})
                continue
            loads[reviewer["_id"]] += 1
            assigned.append(
                {
                    "question_id": question["_id"],
                    "question_code": code,
                    "reviewer_user_id": reviewer["_id"],
                    "reviewer_name": reviewer.get("display_name"),
                }
            )
        return json_safe({"assigned": assigned, "skipped": skipped})

    def _reviews_since(self, since, reviewer_user_id=None) -> list[dict]:
        if settings.question_store == "postgres":
            return self.questions.reviews_since(since, reviewer_user_id=reviewer_user_id)
        match: dict = {"reviewed_at": {"$gte": since}}
        if reviewer_user_id is not None:
            match["reviewer_user_id"] = reviewer_user_id
        return list(
            self.db.question_reviews.find(
                match,
                {
                    "question_version_id": 1,
                    "reviewer_user_id": 1,
                    "decision": 1,
                    "override.applied": 1,
                    "revision_issues": 1,
                    "reason_codes": 1,
                    "review_form.criterion_assessments": 1,
                    "review_form.checklist": 1,
                    "bulk": 1,
                    "reviewed_at": 1,
                },
            ).sort("reviewed_at", -1)
        )

    def _version_subjects(self, version_ids: list) -> dict:
        """Map version_id -> classification.subject cho các version đã duyệt."""
        if not version_ids:
            return {}
        if settings.question_store == "postgres":
            return self.questions.version_subjects(version_ids)
        return {
            version["_id"]: (version.get("classification") or {}).get("subject") or {}
            for version in self.db.question_versions.find(
                {"_id": {"$in": version_ids}},
                {"classification.subject": 1},
            )
        }

    def suggest_review_subjects(self, reviewer_user_id: str, limit: int = 5) -> dict:
        """Subjects a reviewer has actually reviewed recently, most frequent first."""
        reviewer_oid = object_id(reviewer_user_id, "reviewer_user_id")
        since = utc_now() - timedelta(days=SUBJECT_SUGGESTION_WINDOW_DAYS)
        version_ids = [
            review.get("question_version_id")
            for review in self._reviews_since(since, reviewer_oid)
            if review.get("question_version_id")
        ]
        counts: dict = {}
        for subject in self._version_subjects(version_ids).values():
            subject_id = subject.get("id") if isinstance(subject, dict) else None
            if subject_id:
                counts[subject_id] = counts.get(subject_id, 0) + 1
        top = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:limit]
        labels = {
            record["_id"]: record
            for record in subject_records(self.db, ids=[subject_id for subject_id, _count in top])
        } if top else {}
        return json_safe(
            {
                "items": [
                    {
                        "subject_id": subject_id,
                        "subject_code": (labels.get(subject_id) or {}).get("subject_code", ""),
                        "subject_name": (labels.get(subject_id) or {}).get("subject_name", ""),
                        "reviews": count,
                    }
                    for subject_id, count in top
                ],
                "window_days": SUBJECT_SUGGESTION_WINDOW_DAYS,
            }
        )

    def get_review_policy(self) -> dict:
        if settings.review_policy_store == "postgres":
            from modules.questions.postgres_review_policy import PostgresReviewPolicyRepository
            stored = PostgresReviewPolicyRepository().get()
        else:
            stored = self.db.review_settings.find_one({"_id": REVIEW_POLICY_ID}) or {}
        policy = ReviewPolicyPayload(
            secondary_on_override=bool(stored.get("secondary_on_override", False)),
            secondary_below_score=stored.get("secondary_below_score"),
            secondary_subject_ids=[str(item) for item in stored.get("secondary_subject_ids") or []],
        ).model_dump()
        policy["updated_at"] = stored.get("updated_at")
        policy["updated_by_user_id"] = stored.get("updated_by_user_id")
        # Thời hạn giữ câu lấy từ cấu hình để giao diện không phải viết cứng.
        policy["lock_timeout_minutes"] = settings.review_lock_timeout_minutes
        policy["max_held_questions"] = settings.review_max_held_questions
        policy["max_lock_renewals"] = settings.review_max_lock_renewals
        policy["assignment_timeout_hours"] = settings.review_assignment_timeout_hours
        return json_safe(policy)

    def update_review_policy(self, payload: ReviewPolicyPayload, current_user: CurrentUser) -> dict:
        if settings.review_policy_store == "postgres":
            from modules.questions.postgres_review_policy import PostgresReviewPolicyRepository
            PostgresReviewPolicyRepository().update(payload, current_user.id,
                                                    current_user.role)
            return self.get_review_policy()
        before = self.get_review_policy()
        now = utc_now()
        subject_ids = []
        for value in payload.secondary_subject_ids:
            oid = object_id(value, "secondary_subject_ids")
            if oid not in subject_ids:
                subject_ids.append(oid)
        self.db.review_settings.update_one(
            {"_id": REVIEW_POLICY_ID},
            {
                "$set": {
                    "secondary_on_override": payload.secondary_on_override,
                    "secondary_below_score": payload.secondary_below_score,
                    "secondary_subject_ids": subject_ids,
                    "updated_at": now,
                    "updated_by_user_id": current_user.id,
                }
            },
            upsert=True,
        )
        after = self.get_review_policy()
        write_audit_event(
            self.db,
            action="REVIEW_POLICY_UPDATED",
            entity_type="review_policy",
            entity_id=REVIEW_POLICY_ID,
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            before={key: value for key, value in before.items() if key in POLICY_AUDIT_KEYS},
            after={key: value for key, value in after.items() if key in POLICY_AUDIT_KEYS},
            created_at=now,
        )
        return after

    def _policy_secondary_reasons(self, question: dict, payload: ReviewCreateRequest) -> list[str]:
        """Why the review policy forces this approval into a second review, if at all."""
        if settings.review_policy_store == "postgres":
            from modules.questions.postgres_review_policy import PostgresReviewPolicyRepository
            policy = PostgresReviewPolicyRepository().get()
        else:
            if not hasattr(self.db, "review_settings"):
                return []
            policy = self.db.review_settings.find_one({"_id": REVIEW_POLICY_ID}) or {}
        reasons = []
        if policy.get("secondary_on_override") and payload.override.applied:
            reasons.append("duyệt khác gợi ý AI (override)")
        threshold = policy.get("secondary_below_score")
        score = _valid_ai_score(question)
        if isinstance(threshold, (int, float)) and isinstance(score, (int, float)) and score < threshold:
            reasons.append(f"điểm AI {score:.2f} dưới ngưỡng {threshold:.2f}")
        if question.get("subject_id") and question.get("subject_id") in (policy.get("secondary_subject_ids") or []):
            reasons.append("học phần bắt buộc duyệt hai lần")
        return reasons

    def get_review_draft(self, question_id: str, current_user: CurrentUser) -> dict | None:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            draft = self.questions.get_review_draft(question["_id"], current_user.id)
        else:
            draft = self.db.question_review_drafts.find_one(
                {"question_id": question["_id"], "reviewer_user_id": current_user.id}
            )
        if not draft:
            return None
        draft["is_stale"] = draft.get("question_version_id") != version["_id"]
        return json_safe(draft)

    def save_review_draft(
        self,
        question_id: str,
        payload: ReviewDraftUpsertRequest,
        current_user: CurrentUser,
    ) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if question["current_version"] != payload.expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        if settings.question_store == "postgres":
            return json_safe(self.questions.save_review_draft(
                question["_id"], current_user.id,
                expected_version=payload.expected_version,
                decision=payload.decision, draft=payload.draft,
            ))
        now = utc_now()
        draft = self.db.question_review_drafts.find_one_and_update(
            {
                "question_id": question["_id"],
                "reviewer_user_id": current_user.id,
            },
            {
                "$set": {
                    "schema_version": SCHEMA_VERSION,
                    "question_version_id": version["_id"],
                    "question_version": version["version"],
                    "decision": payload.decision,
                    "draft": payload.draft,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "question_id": question["_id"],
                    "reviewer_user_id": current_user.id,
                    "created_at": now,
                },
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return json_safe(draft)

    def delete_review_draft(self, question_id: str, current_user: CurrentUser) -> bool:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            return self.questions.delete_review_draft(question["_id"], current_user.id)
        result = self.db.question_review_drafts.delete_one(
            {
                "question_id": question["_id"],
                "reviewer_user_id": current_user.id,
            }
        )
        return bool(result.deleted_count)

    def review(self, question_id: str, payload: ReviewCreateRequest, current_user: CurrentUser) -> dict:
        question, version = self._pair(question_id)
        if question["current_version"] != payload.expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        if question.get("review_status") != "PENDING":
            raise ValueError("Chỉ câu hỏi đang chờ duyệt mới có thể được kiểm duyệt")
        # AI chỉ hỗ trợ: người duyệt là người quyết định. Chỉ khi AI đã kết luận
        # "xem lại" mà người duyệt vẫn duyệt thì mới tính là override và cần lý do;
        # câu chưa có gợi ý AI hợp lệ thì người duyệt tự đánh giá.
        if (
            payload.decision == "APPROVED"
            and question.get("evaluation_status") == "FAILED"
            and not payload.override.applied
        ):
            raise ValueError(
                "AI đề xuất xem lại: phải ghi rõ lý do (override) khi vẫn duyệt"
            )
        submission_flags = list(dict.fromkeys(
            str(flag) for flag in (((question.get("review_submission") or {}).get("checks") or {}).get("flags") or [])
        ))
        if payload.decision == "APPROVED":
            unacknowledged = [flag for flag in submission_flags if flag not in payload.acknowledged_flags]
            if unacknowledged:
                raise ValueError(
                    "Phải xác nhận đã xem từng cảnh báo tự động trước khi duyệt: " + ", ".join(unacknowledged)
                )
        self_review_reason = payload.self_review_reason.strip()
        is_author = str(current_user.id) in self._review_author_ids(question, version)
        break_glass = is_author and current_user.role == "Admin"
        if break_glass:
            self._ensure_break_glass_allowed(question, version, current_user, self_review_reason)
        else:
            self._ensure_not_author(question, version, current_user)
        self._ensure_reviewer_covers(question, version, current_user)
        now = utc_now()
        self._ensure_review_lock(question, current_user, now)
        secondary = question.get("secondary_review") or {}
        awaiting_secondary = (
            payload.decision == "APPROVED"
            and secondary.get("status") == "AWAITING_SECONDARY"
        )
        if awaiting_secondary and secondary.get("primary_reviewer_user_id") == current_user.id:
            raise ValueError("Reviewer duyệt lần đầu không được tự duyệt lần hai")
        # "REQUESTED" means secondary review was configured before any primary
        # approval: the next approval becomes the primary one.
        request_secondary = (
            payload.decision == "APPROVED"
            and (payload.secondary_required or secondary.get("status") == "REQUESTED" or break_glass)
            and not awaiting_secondary
        )
        secondary_reason = payload.secondary_reason or secondary.get("reason") or ""
        if break_glass and payload.decision == "APPROVED" and not awaiting_secondary:
            break_glass_text = "Quản trị viên tự duyệt khẩn cấp (break-glass): cần reviewer khác duyệt vòng 2"
            secondary_reason = f"{secondary_reason}. {break_glass_text}" if secondary_reason else break_glass_text
        if payload.decision == "APPROVED" and not awaiting_secondary:
            policy_reasons = self._policy_secondary_reasons(question, payload)
            if policy_reasons:
                request_secondary = True
                policy_text = "Theo chính sách kiểm duyệt: " + "; ".join(policy_reasons)
                secondary_reason = f"{secondary_reason}. {policy_text}" if secondary_reason else policy_text
        review_form = payload.review_form.model_dump()
        review_note = payload.note or payload.review_form.overall_note
        review = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "question_id": question["_id"],
            "question_version_id": version["_id"],
            "question_version": version["version"],
            "reviewer_user_id": current_user.id,
            "decision": payload.decision,
            "note": review_note,
            "override": payload.override.model_dump(),
            "review_form": review_form,
            "self_review_reason": self_review_reason if break_glass else "",
            "break_glass": break_glass,
            "revision_issues": review_form.get("revision_issues", []),
            "reason_codes": list(payload.reason_codes),
            "acknowledged_flags": submission_flags if payload.decision == "APPROVED" else [],
            "review_stage": "SECONDARY" if awaiting_secondary else "PRIMARY",
            "bulk": bool(payload.bulk),
            "secondary_required": bool(request_secondary or awaiting_secondary),
            "secondary_reason": secondary_reason,
            "supersedes_review_id": question.get("latest_review_id"),
            "previous_status": question["review_status"],
            "resulting_status": "PENDING" if request_secondary else payload.decision,
            "reviewed_at": now,
        }
        question_fields = {
            "latest_review_id": review["_id"],
            "updated_at": now,
                "review_assignment": _empty_review_assignment(
                now,
                f"review_{payload.decision.lower()}",
            ),
        }
        if break_glass:
            question_fields["break_glass_review"] = {
                "review_id": review["_id"],
                "reviewer_user_id": current_user.id,
                "decision": payload.decision,
                "reason": self_review_reason,
                "reviewed_at": now,
            }
        if request_secondary:
            designated_secondary = (
                secondary.get("secondary_reviewer_user_id")
                if secondary.get("status") == "REQUESTED"
                else None
            )
            if designated_secondary == current_user.id:
                designated_secondary = None
            question_fields["review_status"] = "PENDING"
            question_fields["secondary_review"] = {
                "required": True,
                "status": "AWAITING_SECONDARY",
                "reason": secondary_reason or review_note,
                "primary_review_id": review["_id"],
                "primary_reviewer_user_id": current_user.id,
                "secondary_review_id": None,
                "secondary_reviewer_user_id": designated_secondary,
                "requested_at": secondary.get("requested_at") or now,
                "completed_at": None,
            }
            if designated_secondary:
                question_fields["review_assignment"] = {
                    "status": "ASSIGNED",
                    "reviewer_user_id": designated_secondary,
                    "assigned_by_user_id": secondary.get("requested_by_user_id") or current_user.id,
                    "assigned_at": now,
                    "claimed_at": None,
                    "lock_expires_at": self._assignment_expires_at(now),
                    "last_released_at": None,
                    "release_reason": "secondary_review",
                }
        else:
            question_fields["review_status"] = payload.decision
        if payload.decision == "APPROVED" and not request_secondary:
            question_fields["approved_version_id"] = version["_id"]
            if awaiting_secondary:
                question_fields["secondary_review"] = {
                    **secondary,
                    "required": True,
                    "status": "COMPLETED",
                    "secondary_review_id": review["_id"],
                    "secondary_reviewer_user_id": current_user.id,
                    "completed_at": now,
                }
        elif question.get("approved_version_id") == version["_id"]:
            question_fields["approved_version_id"] = None
        if payload.decision != "APPROVED" and secondary.get("status") != "REQUESTED":
            # A standing "REQUESTED" requirement survives revisions so the
            # resubmitted version still goes through two reviewers.
            question_fields["secondary_review"] = {
                **secondary,
                "status": "CANCELLED" if secondary else "NOT_REQUIRED",
                "completed_at": now if secondary else None,
            }
        # Người duyệt là người quyết định: chốt kết quả khi AI còn đang chạy thì
        # dừng lượt đánh giá đó thay vì bắt người duyệt chờ.
        if settings.question_store == "postgres":
            interrupted_job_ids = self.questions.active_evaluation_job_ids(question["_id"])
        else:
            interrupted_job_ids = [
                job["_id"]
                for job in self.db.evaluation_jobs.find(
                    {
                        "question_id": question["_id"],
                        "status": {"$in": list(EVALUATION_ACTIVE_STATUSES)},
                    },
                    {"_id": 1},
                )
            ]
        if interrupted_job_ids or question.get("evaluation_status") in EVALUATION_ACTIVE_STATUSES:
            question_fields["evaluation_status"] = "NOT_STARTED"
            review["interrupted_evaluation_job_ids"] = interrupted_job_ids
        audit_action = (
            "QUESTION_SECONDARY_REVIEW_REQUESTED"
            if request_secondary
            else f"QUESTION_{payload.decision}"
        )
        audit = build_audit_event(
            action=audit_action,
            entity_type="question",
            entity_id=question["_id"],
            entity_version_id=version["_id"],
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            changes=[
                {
                    "path": "review_status",
                    "old_value": question["review_status"],
                    "new_value": question_fields["review_status"],
                }
            ],
            before_hash=version["content_hash"],
            after_hash=version["content_hash"],
            metadata={
                "review_id": review["_id"],
                "correlation_id": str(review["_id"]),
                "review_assignment": json_safe(question.get("review_assignment") or {}),
                "review_form": review_form,
                "secondary_review": json_safe(question_fields.get("secondary_review") or secondary or {}),
                "interrupted_evaluation_job_ids": interrupted_job_ids,
                "self_review_reason": review["self_review_reason"],
            },
            created_at=now,
        )
        secondary_recipients = secondary_review_recipients(
            review_candidate_users(self.db), question=question, version=version,
            primary_reviewer_user_id=current_user.id,
        ) if request_secondary else []

        def notify_review(notifications: NotificationService) -> None:
            if not request_secondary:
                notifications.notify_review_decision(
                    question=question, version=version, review=review,
                    actor_user_id=current_user.id,
                )
                return
            notifications.notify_secondary_review_pending(
                question=question, version=version,
                reason=question_fields["secondary_review"].get("reason") or "",
                actor_user_id=current_user.id,
            )
            notifications.notify_secondary_review_available(
                question=question, version=version, primary_reviewer_user_id=current_user.id,
                actor_user_id=current_user.id, recipients=secondary_recipients,
            )
            if question_fields["review_assignment"].get("status") == "ASSIGNED":
                notifications.notify_review_assigned(
                    question=question, version=version,
                    reviewer_user_id=question_fields["review_assignment"]["reviewer_user_id"],
                    actor_user_id=current_user.id,
                )

        outbox = notification_outbox()
        if outbox is not None:
            notify_review(NotificationService(self.db, sink=outbox))
        if settings.question_store == "postgres":
            self.questions.record_review(
                review, question_fields,
                expected_version_id=version["_id"],
                expected_latest_review_id=question.get("latest_review_id"),
                actor_role=current_user.role, audit_action=audit_action,
                notifications=outbox,
                **({"extra_audit": [self._break_glass_audit_fields(question, version, review, current_user)]}
                   if break_glass else {}),
            )
        else:
            with mongo_transaction() as session:
                if interrupted_job_ids:
                    self.db.evaluation_jobs.update_many(
                        {
                            "_id": {"$in": interrupted_job_ids},
                            "status": {"$in": list(EVALUATION_ACTIVE_STATUSES)},
                        },
                        {
                            "$set": {
                                "status": "CANCELLED",
                                "error": {
                                    "message": "Người duyệt đã chốt kết quả nên dừng AI đánh giá",
                                    "stage": "REVIEWER_DECIDED",
                                    "at": now,
                                },
                                "finished_at": now,
                                "expires_at": now + timedelta(days=settings.job_retention_days),
                                "updated_at": now,
                            },
                            "$unset": {
                                "locked_by": "",
                                "lease_expires_at": "",
                                "heartbeat_at": "",
                                "next_attempt_at": "",
                            },
                        },
                        session=session,
                    )
                self.db.question_reviews.insert_one(review, session=session)
                # Re-check state at write time: a concurrent withdraw, archive or takeover must win.
                result = self.db.questions.update_one(
                    {
                        "_id": question["_id"],
                        "current_version_id": version["_id"],
                        "lifecycle_status": "ACTIVE",
                        "review_status": "PENDING",
                        "latest_review_id": question.get("latest_review_id"),
                        "review_assignment.status": "IN_REVIEW",
                        "review_assignment.reviewer_user_id": current_user.id,
                    },
                    {"$set": question_fields},
                    session=session,
                )
                if not result.matched_count:
                    raise RuntimeError("VERSION_CONFLICT")
                self.db.audit_logs.insert_one(audit, session=session)
                if break_glass:
                    self.db.audit_logs.insert_one(
                        build_audit_event(**self._break_glass_audit_fields(question, version, review, current_user)),
                        session=session,
                    )
        if break_glass:
            self._notify_admins_break_glass(question, version, review, current_user)
        if outbox is None:
            if not request_secondary:
                safe_notify_review_decision(
                    database=self.db,
                    question=question,
                    version=version,
                    review=review,
                    actor_user_id=current_user.id,
                )
            else:
                safe_notify_secondary_review_pending(
                    database=self.db,
                    question=question,
                    version=version,
                    reason=question_fields["secondary_review"].get("reason") or "",
                    actor_user_id=current_user.id,
                )
                safe_notify_secondary_review_available(
                    database=self.db, question=question, version=version,
                    primary_reviewer_user_id=current_user.id, actor_user_id=current_user.id,
                    recipients=secondary_recipients,
                )
                if question_fields["review_assignment"].get("status") == "ASSIGNED":
                    safe_notify_review_assigned(
                        database=self.db,
                        question=question,
                        version=version,
                        reviewer_user_id=question_fields["review_assignment"]["reviewer_user_id"],
                        actor_user_id=current_user.id,
                    )
        if settings.question_store != "postgres" and hasattr(self.db, "question_review_drafts"):
            self.db.question_review_drafts.delete_one(
                {
                    "question_id": question["_id"],
                    "reviewer_user_id": current_user.id,
                }
            )
        return json_safe(review)

    def _comment_audit(
        self,
        action: str,
        question: dict,
        version: dict,
        current_user: CurrentUser,
        metadata: dict,
        now,
    ) -> None:
        write_audit_event(
            self.db,
            action=action,
            entity_type="question",
            entity_id=question["_id"],
            entity_version_id=version["_id"],
            actor_user_id=current_user.id,
            actor_role=current_user.role,
            before_hash=version["content_hash"],
            after_hash=version["content_hash"],
            metadata=metadata,
            created_at=now,
        )

    def list_comments(self, question_id: str, current_user: CurrentUser) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            return {"items": [json_safe(item) for item in self.questions.list_comments(question["_id"])]}
        comments = list(
            self.db.question_comments.find({"question_id": question["_id"], "deleted_at": None})
            .sort("created_at", 1)
        )
        return {"items": [json_safe(comment) for comment in comments]}

    def add_comment(
        self,
        question_id: str,
        payload: QuestionCommentCreateRequest,
        current_user: CurrentUser,
    ) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        mention_ids = [
            object_id(user_id, "mention_user_id")
            for user_id in dict.fromkeys(payload.mention_user_ids)
        ]
        mentioned_users = users_by_ids(self.db, mention_ids, active_only=True)
        if len(mentioned_users) != len(mention_ids):
            raise ValueError("Một hoặc nhiều người được mention không hợp lệ")
        now = utc_now()
        comment = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "question_id": question["_id"],
            "question_version_id": version["_id"],
            "question_version": version["version"],
            "author_user_id": current_user.id,
            "author_role": current_user.role,
            "body": payload.body.strip(),
            "mention_user_ids": mention_ids,
            "created_at": now,
            "updated_at": now,
        }
        mentions = [
            {
                "recipient_user_id": user["_id"],
                "actor_user_id": current_user.id,
                "type": "QUESTION_MENTION",
                "title": f"{question.get('question_code', 'Câu hỏi')} có mention mới",
                "body": comment["body"][:200],
                "link": (
                    f"/kiem-duyet?questionId={question['_id']}"
                    if _user_can_review(user)
                    else f"/quan-ly?questionId={question['_id']}"
                ),
                "entity": {
                    "type": "QUESTION",
                    "id": str(question["_id"]),
                    "version_id": str(version["_id"]),
                    "comment_id": str(comment["_id"]),
                },
            }
            for user in mentioned_users
            if user["_id"] != current_user.id
        ]
        outbox = notification_outbox()
        if outbox is not None:
            NotificationService(self.db, sink=outbox).create_many(mentions)
        if settings.question_store == "postgres":
            self.questions.add_comment(comment, actor_role=current_user.role,
                                       notifications=outbox)
        else:
            self.db.question_comments.insert_one(comment)
            self._comment_audit(
                "QUESTION_COMMENT_ADDED", question, version, current_user,
                {"comment_id": comment["_id"], "mentions": json_safe(mention_ids)}, now,
            )
        if outbox is None:
            NotificationService(self.db).create_many(mentions)
        return json_safe(comment)

    def update_comment(
        self,
        question_id: str,
        comment_id: str,
        payload: QuestionCommentUpdateRequest,
        current_user: CurrentUser,
    ) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            return json_safe(self.questions.change_comment(
                question["_id"], comment_id, actor_user_id=current_user.id,
                actor_role=current_user.role, body=payload.body.strip(),
            ))
        query = {
            "_id": object_id(comment_id, "comment_id"),
            "question_id": question["_id"],
            "deleted_at": None,
        }
        if current_user.role != "Admin":
            query["author_user_id"] = current_user.id
        now = utc_now()
        updated = self.db.question_comments.find_one_and_update(
            query,
            {"$set": {"body": payload.body.strip(), "updated_at": now, "edited_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            raise PermissionError("Bạn chỉ có thể sửa bình luận của mình")
        self._comment_audit("QUESTION_COMMENT_UPDATED", question, version, current_user, {"comment_id": updated["_id"]}, now)
        return json_safe(updated)

    def delete_comment(
        self,
        question_id: str,
        comment_id: str,
        current_user: CurrentUser,
    ) -> bool:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            self.questions.change_comment(
                question["_id"], comment_id, actor_user_id=current_user.id,
                actor_role=current_user.role, delete=True,
            )
            return True
        query = {
            "_id": object_id(comment_id, "comment_id"),
            "question_id": question["_id"],
            "deleted_at": None,
        }
        if current_user.role != "Admin":
            query["author_user_id"] = current_user.id
        now = utc_now()
        updated = self.db.question_comments.find_one_and_update(
            query,
            {"$set": {"body": "", "deleted_at": now, "deleted_by_user_id": current_user.id, "updated_at": now}},
            return_document=ReturnDocument.AFTER,
        )
        if not updated:
            raise PermissionError("Bạn chỉ có thể xóa bình luận của mình")
        self._comment_audit("QUESTION_COMMENT_DELETED", question, version, current_user, {"comment_id": updated["_id"]}, now)
        return True

    def set_secondary_review(
        self,
        question_id: str,
        payload: SecondaryReviewRequest,
        current_user: CurrentUser,
    ) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        review_status = question["review_status"]
        if review_status not in {"PENDING", "APPROVED"}:
            raise ValueError("Chỉ cấu hình duyệt lần hai cho câu đang chờ hoặc đã duyệt")
        is_admin = current_user.role == "Admin"
        if not is_admin and (review_status == "APPROVED" or not payload.required):
            raise PermissionError(
                "Chỉ Admin được mở lại câu đã duyệt hoặc bỏ yêu cầu duyệt lần hai"
            )
        current_secondary = question.get("secondary_review") or {}
        now = utc_now()
        assignment = question.get("review_assignment") or {}
        claimed_by_other = (
            self._is_claimed(assignment, now) and assignment.get("reviewer_user_id") != current_user.id
        )
        if not is_admin:
            self._ensure_reviewer_covers(question, version, current_user)
            if current_secondary.get("status") == "AWAITING_SECONDARY" or claimed_by_other or not (
                self._is_claimed(assignment, now) and assignment.get("reviewer_user_id") == current_user.id
            ):
                raise PermissionError("Chỉ reviewer đang giữ câu ở vòng duyệt đầu được yêu cầu duyệt lần hai")

        if not payload.required:
            secondary = {
                "required": False,
                "status": "NOT_REQUIRED",
                "reason": payload.reason,
                "requested_at": now,
                "completed_at": now,
            }
            fields = {"secondary_review": secondary, "updated_at": now}
            reviewer = None
        else:
            if review_status == "APPROVED":
                # The approval being re-opened becomes the primary review.
                primary_review_id = question.get("latest_review_id")
                primary_review = (
                    (self.questions.find_review(primary_review_id)
                     if settings.question_store == "postgres"
                     else self.db.question_reviews.find_one({"_id": primary_review_id}))
                    if primary_review_id
                    else None
                ) or {}
                primary_reviewer_user_id = primary_review.get("reviewer_user_id")
                status = "AWAITING_SECONDARY"
            elif current_secondary.get("status") == "AWAITING_SECONDARY":
                primary_review_id = current_secondary.get("primary_review_id")
                primary_reviewer_user_id = current_secondary.get("primary_reviewer_user_id")
                status = "AWAITING_SECONDARY"
            else:
                # No primary approval yet: the next approval becomes primary
                # and automatically hands over to the secondary stage.
                primary_review_id = None
                primary_reviewer_user_id = None
                status = "REQUESTED"

            reviewer = None
            if payload.reviewer_user_id:
                reviewer = self._find_assignable_reviewer(payload.reviewer_user_id)
                if reviewer["_id"] == primary_reviewer_user_id:
                    raise ValueError("Người duyệt lần hai phải khác người duyệt lần đầu")
                if str(reviewer["_id"]) in self._review_author_ids(question, version):
                    raise ValueError("Không thể giao duyệt lần hai cho tác giả, người sửa hoặc người gửi duyệt")
                if not self._reviewer_covers(question, version, reviewer):
                    raise ValueError("Người duyệt lần hai không phụ trách học phần của câu hỏi")
                if not is_admin and reviewer["_id"] == current_user.id:
                    raise ValueError("Người duyệt lần hai phải khác người đang duyệt")

            secondary = {
                "required": True,
                "status": status,
                "reason": payload.reason,
                "requested_by_user_id": current_user.id,
                "primary_review_id": primary_review_id,
                "primary_reviewer_user_id": primary_reviewer_user_id,
                "secondary_review_id": None,
                "secondary_reviewer_user_id": reviewer["_id"] if reviewer else None,
                "requested_at": now,
                "completed_at": None,
            }
            fields = {"secondary_review": secondary, "updated_at": now}
            if review_status == "APPROVED":
                # The approved pointer stays: only a new human decision may move or clear it.
                fields["review_status"] = "PENDING"
                fields["review_assignment"] = _empty_review_assignment(
                    now,
                    payload.reason or "secondary_review",
                )
            if status == "AWAITING_SECONDARY" and reviewer:
                if claimed_by_other:
                    raise ValueError("Câu đang được reviewer khác giữ; chờ họ nhả câu trước khi giao lại")
                fields["review_assignment"] = {
                    "status": "ASSIGNED",
                    "reviewer_user_id": reviewer["_id"],
                    "assigned_by_user_id": current_user.id,
                    "assigned_at": now,
                    "claimed_at": None,
                    "lock_expires_at": self._assignment_expires_at(now),
                    "last_released_at": None,
                    "release_reason": payload.reason or None,
                }
        assigns_reviewer = bool(
            reviewer and (fields.get("review_assignment") or {}).get("status") == "ASSIGNED"
        )
        outbox = notification_outbox()
        if outbox is not None and assigns_reviewer:
            NotificationService(self.db, sink=outbox).notify_review_assigned(
                question=question, version=version,
                reviewer_user_id=reviewer["_id"], actor_user_id=current_user.id,
            )
        if settings.question_store == "postgres":
            updated, version = self.questions.set_secondary_review(
                question_id, expected_version_id=version["_id"],
                expected_review_status=review_status, fields=fields,
                actor_user_id=current_user.id, actor_role=current_user.role,
                reason=payload.reason, notifications=outbox,
            )
        else:
            updated = self.db.questions.find_one_and_update(
                {
                    "_id": question["_id"],
                    "current_version_id": version["_id"],
                    "lifecycle_status": "ACTIVE",
                    "review_status": review_status,
                },
                {"$set": fields},
                return_document=ReturnDocument.AFTER,
            )
        if not updated:
            raise RuntimeError("VERSION_CONFLICT")
        if settings.question_store != "postgres":
            self._assignment_audit(
                action="QUESTION_SECONDARY_REVIEW_SET", question=updated,
                version=version, current_user=current_user,
                before=question.get("secondary_review") or {}, after=secondary,
                metadata={"reason": payload.reason}, path="secondary_review",
            )
        if assigns_reviewer and outbox is None:
            safe_notify_review_assigned(
                database=self.db,
                question=updated,
                version=version,
                reviewer_user_id=reviewer["_id"],
                actor_user_id=current_user.id,
            )
        return serialize_question(updated, version)

    @staticmethod
    def _question_type(version: dict) -> str:
        classification = version.get("classification") or {}
        return str(classification.get("assessment_type") or "").lower()

    @staticmethod
    def _option_text(options, key: str) -> str:
        if isinstance(options, dict):
            return str(options.get(key) or options.get(str(key)) or key)
        return str(key)

    @staticmethod
    def _answer_keys(correct_answer: str) -> list[str]:
        return [
            item.strip().upper()
            for item in str(correct_answer or "").split(",")
            if item.strip()
        ]

    @staticmethod
    def _gift_escape(value) -> str:
        text = str(value or "")
        for source, replacement in (
            ("\\", "\\\\"),
            ("~", "\\~"),
            ("=", "\\="),
            ("#", "\\#"),
            ("{", "\\{"),
            ("}", "\\}"),
            (":", "\\:"),
        ):
            text = text.replace(source, replacement)
        return text

    @staticmethod
    def _xml_text(value) -> str:
        return escape(str(value or ""), quote=False)

    @classmethod
    def _moodle_gift(cls, question: dict, version: dict) -> str:
        question_data = version.get("question_data") or {}
        options = question_data.get("options")
        correct_answer = question_data.get("correct_answer")
        explanation = question_data.get("explanation")
        qtype = cls._question_type(version)
        code = cls._gift_escape(question.get("question_code"))
        content = cls._gift_escape(version.get("content"))
        feedback = f" # {cls._gift_escape(explanation)}" if explanation else ""

        if qtype == "dung_sai":
            value = "TRUE" if str(correct_answer).strip().upper() in {"A", "TRUE", "ĐÚNG", "DUNG"} else "FALSE"
            return f"::{code}:: {content} {{{value}{feedback}}}\n"

        if qtype == "dien_khuyet":
            answer = cls._gift_escape(correct_answer)
            if "_____" in content:
                content = content.replace("_____", f"{{={answer}{feedback}}}", 1)
                return f"::{code}:: {content}\n"
            return f"::{code}:: {content} {{={answer}{feedback}}}\n"

        if qtype == "ghep_cot" and isinstance(options, dict):
            pairs = re.findall(r"([A-Za-z0-9]+)\s*-\s*([A-Za-z0-9]+)", str(correct_answer or ""))
            answers = [
                f"={cls._gift_escape(cls._option_text(options, left))} -> {cls._gift_escape(cls._option_text(options, right))}"
                for left, right in pairs
            ]
            if answers:
                return f"::{code}:: {content} {{{' '.join(answers)}{feedback}}}\n"

        if isinstance(options, dict):
            correct_keys = set(cls._answer_keys(correct_answer))
            multiple = qtype == "nhieu_lua_chon" and len(correct_keys) > 1
            correct_fraction = round(100 / len(correct_keys)) if multiple else 100
            wrong_fraction = -100 if multiple else None
            answers = []
            for key, value in options.items():
                key_upper = str(key).strip().upper()
                text = cls._gift_escape(value)
                if key_upper in correct_keys:
                    prefix = f"~%{correct_fraction}%" if multiple else "="
                elif multiple:
                    prefix = f"~%{wrong_fraction}%"
                else:
                    prefix = "~"
                answers.append(f"{prefix}{text}")
            return f"::{code}:: {content} {{{' '.join(answers)}{feedback}}}\n"

        return f"::{code}:: {content} {{={cls._gift_escape(correct_answer)}{feedback}}}\n"

    @classmethod
    def _moodle_xml_question(cls, question: dict, version: dict) -> str:
        question_data = version.get("question_data") or {}
        options = question_data.get("options")
        correct_answer = question_data.get("correct_answer")
        explanation = cls._xml_text(question_data.get("explanation"))
        qtype = cls._question_type(version)
        code = cls._xml_text(question.get("question_code"))
        content = cls._xml_text(version.get("content"))

        def answer_node(text, fraction):
            return (
                f'    <answer fraction="{fraction}">\n'
                f"      <text>{cls._xml_text(text)}</text>\n"
                f"      <feedback><text>{explanation}</text></feedback>\n"
                f"    </answer>"
            )

        if qtype == "dung_sai":
            is_true = str(correct_answer).strip().upper() in {"A", "TRUE", "ĐÚNG", "DUNG"}
            return f"""  <question type="truefalse">
    <name><text>{code}</text></name>
    <questiontext format="html"><text>{content}</text></questiontext>
{answer_node("true", 100 if is_true else 0)}
{answer_node("false", 0 if is_true else 100)}
  </question>"""

        if qtype == "dien_khuyet":
            return f"""  <question type="shortanswer">
    <name><text>{code}</text></name>
    <questiontext format="html"><text>{content}</text></questiontext>
    <usecase>0</usecase>
{answer_node(correct_answer, 100)}
  </question>"""

        if qtype == "ghep_cot" and isinstance(options, dict):
            pairs = re.findall(r"([A-Za-z0-9]+)\s*-\s*([A-Za-z0-9]+)", str(correct_answer or ""))
            subquestions = "\n".join(
                f"""    <subquestion format="html">
      <text>{cls._xml_text(cls._option_text(options, left))}</text>
      <answer><text>{cls._xml_text(cls._option_text(options, right))}</text></answer>
    </subquestion>"""
                for left, right in pairs
            )
            if subquestions:
                return f"""  <question type="matching">
    <name><text>{code}</text></name>
    <questiontext format="html"><text>{content}</text></questiontext>
{subquestions}
  </question>"""

        if isinstance(options, dict):
            correct_keys = set(cls._answer_keys(correct_answer))
            multiple = qtype == "nhieu_lua_chon" and len(correct_keys) > 1
            correct_fraction = round(100 / len(correct_keys), 4) if multiple else 100
            answers = "\n".join(
                answer_node(value, correct_fraction if str(key).strip().upper() in correct_keys else 0)
                for key, value in options.items()
            )
            single = "false" if multiple else "true"
            return f"""  <question type="multichoice">
    <name><text>{code}</text></name>
    <questiontext format="html"><text>{content}</text></questiontext>
    <single>{single}</single>
    <shuffleanswers>true</shuffleanswers>
{answers}
  </question>"""

        return f"""  <question type="shortanswer">
    <name><text>{code}</text></name>
    <questiontext format="html"><text>{content}</text></questiontext>
    <usecase>0</usecase>
{answer_node(correct_answer, 100)}
  </question>"""

    @classmethod
    def _moodle_xml(cls, question: dict, version: dict) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<quiz>
{cls._moodle_xml_question(question, version)}
</quiz>
"""

    @classmethod
    def _moodle_exports(cls, question: dict, version: dict, export_format: str = "BOTH") -> dict:
        export_format = (export_format or "BOTH").upper()
        exports = {}
        if export_format in {"GIFT", "BOTH"}:
            exports["gift"] = cls._moodle_gift(question, version)
        if export_format in {"XML", "BOTH"}:
            exports["xml"] = cls._moodle_xml(question, version)
        return exports

    def export_moodle(
        self,
        question_id: str,
        export_format: str = "gift",
        current_user: CurrentUser | None = None,
    ) -> dict:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if question["review_status"] != "APPROVED" or question.get("approved_version_id") != version["_id"]:
            raise ValueError("Chỉ câu hỏi đã được duyệt ở phiên bản hiện tại mới được export Moodle")
        normalized = export_format.lower()
        if normalized not in {"gift", "xml"}:
            raise ValueError("Định dạng export Moodle phải là gift hoặc xml")
        exports = self._moodle_exports(question, version, normalized.upper())
        content = exports[normalized]
        return {
            "filename": f"{question['question_code']}.{normalized}",
            "content": content,
            "media_type": "application/xml" if normalized == "xml" else "text/plain",
        }

    def publish_to_moodle(
        self,
        question_id: str,
        payload: MoodlePublicationRequest,
        user_id,
        publisher_role: str | None = None,
    ) -> dict:
        if not payload.mock:
            raise ValueError("Tích hợp Moodle thật chưa được cấu hình; hãy dùng export GIFT/XML")
        if settings.app_env == "production" and not settings.demo_mode:
            raise ValueError("Mô phỏng Moodle bị tắt trong production; hãy dùng export GIFT/XML")
        question, version = self._pair(question_id)
        if question["current_version"] != payload.expected_version:
            raise RuntimeError("VERSION_CONFLICT")
        if question["review_status"] != "APPROVED" or question.get("approved_version_id") != version["_id"]:
            raise ValueError("Chỉ câu hỏi đã được duyệt ở phiên bản hiện tại mới được xuất Moodle")

        target_service = MoodleTargetService(self.db)
        target_config = target_service.find_target(payload.target_id or payload.moodle_site_id)
        if payload.target_id and not target_config:
            raise LookupError("Không tìm thấy Moodle target")
        if target_config and not target_config.get("is_active", True):
            raise ValueError("Moodle target đang bị khóa")
        allowed_roles = (target_config or {}).get("allowed_roles") or ["Admin", "Reviewer"]
        if target_config and publisher_role and publisher_role not in allowed_roles:
            raise PermissionError("Role hiện tại không được phép publish tới Moodle target này")

        moodle_site_id = (target_config or {}).get("site_key") or payload.moodle_site_id
        course_id = payload.course_id
        category_id = payload.category_id
        if target_config:
            if payload.course_id == "ctdl-demo":
                course_id = target_config.get("default_course_id") or course_id
            if payload.category_id == "qbank-demo":
                category_id = target_config.get("default_category_id") or category_id
        configured_mode = (target_config or {}).get("mode", "MOCK" if payload.mock else "REST_API")
        publication_mode = "MOCK" if payload.mock else configured_mode
        target = {
            "target_id": target_config.get("_id") if target_config else None,
            "moodle_site_id": moodle_site_id,
            "site_name": (target_config or {}).get("site_name"),
            "mode": publication_mode,
            "configured_mode": configured_mode,
            "course_id": course_id,
            "category_id": category_id,
            "allowed_roles": allowed_roles,
        }
        published_content_hash = version["content_hash"]
        idempotency_material = "|".join(
            [
                moodle_site_id,
                course_id,
                category_id,
                str(version["_id"]),
                published_content_hash,
            ]
        )
        idempotency_key = hashlib.sha256(idempotency_material.encode("utf-8")).hexdigest()
        now = utc_now()
        exports = self._moodle_exports(question, version, payload.export_format)
        publication = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "question_id": question["_id"],
            "question_version_id": version["_id"],
            "question_version": version["version"],
            "publisher_user_id": user_id,
            "target": target,
            "publication_mode": publication_mode,
            "external_sync": False,
            "status_detail": MOODLE_MOCK_STATUS_DETAIL,
            "published_content_hash": published_content_hash,
            "idempotency_key": idempotency_key,
            "status": "PUBLISHED",
            "attempt_no": 1,
            "moodle_question_ref_id": f"mock-{moodle_site_id}-{str(version['_id'])}",
            "request_payload": {
                "question_code": question["question_code"],
                "content": version["content"],
                "question_data": version.get("question_data") or {},
                "classification": version.get("classification") or {},
                "export_format": payload.export_format,
                "exports": exports,
                "mock": payload.mock,
                "publication_mode": publication_mode,
                "external_sync": False,
                "target": json_safe(target),
            },
            "response_payload": {
                "mock": payload.mock,
                "publication_mode": publication_mode,
                "external_sync": False,
                "status_detail": MOODLE_MOCK_STATUS_DETAIL,
                "message": MOODLE_MOCK_MESSAGE,
                "export_formats": list(exports.keys()),
            },
            "error": None,
            "created_at": now,
            "updated_at": now,
            "published_at": now,
        }
        if settings.question_store == "postgres":
            saved = json_safe(self.questions.record_moodle_publication(
                publication, expected_version_id=version["_id"],
            ))
            self._email_publication_result(saved, question, version, user_id)
            return saved
        with mongo_transaction() as session:
            existing = self.db.moodle_publications.find_one(
                {"idempotency_key": idempotency_key},
                session=session,
            )
            if existing and existing.get("status") == "FAILED":
                publication["_id"] = existing["_id"]
                publication["created_at"] = existing.get("created_at") or now
                publication["attempt_no"] = int(existing.get("attempt_no") or 1) + 1
                update_fields = {key: value for key, value in publication.items() if key != "_id"}
                self.db.moodle_publications.update_one(
                    {"_id": existing["_id"], "status": "FAILED"},
                    {"$set": update_fields},
                    session=session,
                )
                saved = self.db.moodle_publications.find_one(
                    {"_id": existing["_id"]},
                    session=session,
                )
            elif existing:
                saved = existing
            else:
                self.db.moodle_publications.update_one(
                    {"idempotency_key": idempotency_key},
                    {"$setOnInsert": publication},
                    upsert=True,
                    session=session,
                )
                saved = self.db.moodle_publications.find_one(
                    {"idempotency_key": idempotency_key},
                    session=session,
                )
            if (saved or publication).get("status") == "PUBLISHED":
                self.db.questions.update_one(
                    {
                        "_id": question["_id"],
                        "current_version_id": version["_id"],
                        "review_status": "APPROVED",
                    },
                    {
                        "$set": {
                            "publication_status": "PUBLISHED",
                            "updated_at": now,
                        }
                    },
                    session=session,
                )
        result = json_safe(saved or publication)
        self._email_publication_result(result, question, version, user_id)
        return result

    def _email_publication_result(self, publication, question, version, user_id) -> None:
        """Send a best-effort email when a publication finishes.

        The Moodle target is a MOCK today, so the wording must not claim the
        question has landed on a real Moodle site. Only PUBLISHED / FAILED
        transitions are notified; anything else is skipped.
        """
        try:
            from core.config import settings as _settings
            from core.email_service import build_email_html, get_email_service
            from core.postgres import postgres_connection
        except Exception:  # pragma: no cover
            return
        status = (publication or {}).get("status")
        if status not in ("PUBLISHED", "FAILED"):
            return
        if not user_id:
            return
        try:
            with postgres_connection() as conn:
                row = conn.execute(
                    "SELECT email, display_name, profile FROM users WHERE id = %s",
                    (str(user_id),),
                ).fetchone()
        except Exception as exc:
            logger.warning("Publication email: user lookup failed: %s", exc)
            return
        if not row:
            return
        profile = row.get("profile") or {}
        if profile.get("email_notifications_enabled", True) is False:
            return
        recipient = (profile.get("notification_email") or "").strip() or row.get("email")
        if not recipient:
            return
        question_code = (question or {}).get("question_code", "Câu hỏi")
        version_no = (version or {}).get("version", "")
        if status == "PUBLISHED":
            title = f"Xuất bản câu hỏi {question_code}: hoàn tất"
            paragraphs = [
                f"Quy trình xuất bản đã hoàn tất trong QBankCTU cho câu hỏi {question_code}"
                f"{f' phiên bản {version_no}' if version_no else ''}.",
                "Lưu ý: hệ thống hiện đang ở chế độ mô phỏng Moodle, dữ liệu chưa được gửi lên Moodle thật.",
            ]
        else:
            title = f"Xuất bản câu hỏi {question_code}: gặp sự cố"
            error = (publication or {}).get("error") or {}
            error_msg = error.get("message") if isinstance(error, dict) else str(error)
            paragraphs = [
                f"Quy trình xuất bản câu hỏi {question_code} trong QBankCTU không thành công.",
                f"Lý do: {error_msg or 'không rõ'}",
                "Vui lòng đăng nhập để kiểm tra chi tiết.",
            ]
        html_body = build_email_html(
            title=title,
            paragraphs=paragraphs,
            meta={"Mã câu hỏi": question_code, "Trạng thái": status},
        )
        get_email_service().send(
            to_email=recipient,
            to_name=row.get("display_name") or "",
            subject=title,
            html_body=html_body,
        )

    def history(
        self,
        question_id: str,
        kind: str,
        current_user: CurrentUser | None = None,
    ) -> list[dict]:
        question, version = self._pair(question_id)
        self._ensure_read_access(question, version, current_user)
        if settings.question_store == "postgres":
            return [json_safe(item) for item in self.questions.history(question["_id"], kind)]
        if kind == "evaluations":
            cursor = self.db.question_evaluations.find(
                {"question_id": question["_id"]}
            ).sort("created_at", -1)
        elif kind == "publications":
            cursor = self.db.moodle_publications.find(
                {"question_id": question["_id"]}
            ).sort("created_at", -1)
        else:
            cursor = self.db.question_reviews.find(
                {"question_id": question["_id"]}
            ).sort("reviewed_at", -1)
        return [json_safe(item) for item in cursor]

    def _mongo_review_workload(self, pending_count, now, current_user: CurrentUser) -> dict:
        return {
            "pending": pending_count(),
            "unassigned": pending_count(
                {
                    "$or": [
                        {"review_assignment.status": {"$exists": False}},
                        {"review_assignment.status": "UNASSIGNED"},
                    ]
                }
            ),
            "assigned": pending_count({"review_assignment.status": "ASSIGNED"}),
            "in_review": pending_count({"review_assignment.status": "IN_REVIEW"}),
            "lock_expired": pending_count(
                {
                    "review_assignment.status": "IN_REVIEW",
                    "review_assignment.lock_expires_at": {"$lte": now},
                }
            ),
            "mine": pending_count(
                {
                    "review_assignment.status": {"$in": ["ASSIGNED", "IN_REVIEW"]},
                    "review_assignment.reviewer_user_id": current_user.id,
                }
            ),
            "sla_breached": pending_count(
                {"review_submission.submitted_at": {"$lte": self._sla_cutoff(now)}}
            ),
            "sla_hours": settings.review_sla_hours,
        }

    def review_dashboard(self, current_user: CurrentUser) -> dict:
        now = utc_now()
        since_7d = now - timedelta(days=7)
        since_30d = now - timedelta(days=30)
        is_admin = current_user.role == "Admin"
        pending_base = {
            "schema_version": SCHEMA_VERSION,
            "lifecycle_status": "ACTIVE",
            "review_status": "PENDING",
        }

        def pending_count(extra: dict | None = None) -> int:
            if not extra:
                return self.db.questions.count_documents(pending_base)
            return self.db.questions.count_documents({"$and": [pending_base, extra]})

        if settings.question_store == "postgres":
            workload = {
                **self.questions.review_workload(
                    now, reviewer_user_id=current_user.id, sla_cutoff=self._sla_cutoff(now),
                ),
                "sla_hours": settings.review_sla_hours,
            }
        else:
            workload = self._mongo_review_workload(pending_count, now, current_user)

        reviews = self._reviews_since(since_30d, None if is_admin else current_user.id)
        decision_counts = {"APPROVED": 0, "NEEDS_REVISION": 0, "REJECTED": 0}
        override_count = 0
        bulk_count = 0
        revision_issues = 0
        reason_counts: dict[str, int] = {}
        reviews_last_7d = 0
        per_reviewer: dict = {}

        def reviewer_bucket(reviewer_id) -> dict:
            return per_reviewer.setdefault(
                reviewer_id,
                {
                    "reviews_30d": 0,
                    "reviews_7d": 0,
                    "decisions": {"APPROVED": 0, "NEEDS_REVISION": 0, "REJECTED": 0},
                    "override_count": 0,
                    "bulk_count": 0,
                    "calibration_sample": 0,
                    "calibration_agreements": 0,
                    "durations": [],
                },
            )

        for review in reviews:
            decision = review.get("decision")
            overridden = bool((review.get("override") or {}).get("applied"))
            bucket = reviewer_bucket(review.get("reviewer_user_id")) if is_admin else None
            if decision in decision_counts:
                decision_counts[decision] += 1
                if bucket is not None:
                    bucket["decisions"][decision] += 1
            if overridden:
                override_count += 1
            if review.get("bulk"):
                bulk_count += 1
            revision_issues += len(review.get("revision_issues") or [])
            for code in review.get("reason_codes") or []:
                reason_counts[code] = reason_counts.get(code, 0) + 1
            reviewed_at = _as_aware_utc(review.get("reviewed_at"))
            recent = bool(reviewed_at and reviewed_at >= since_7d)
            if recent:
                reviews_last_7d += 1
            if bucket is not None:
                bucket["reviews_30d"] += 1
                bucket["reviews_7d"] += int(recent)
                bucket["override_count"] += int(overridden)
                bucket["bulk_count"] += int(bool(review.get("bulk")))

        version_ids = [
            review.get("question_version_id")
            for review in reviews
            if review.get("question_version_id")
        ]
        evaluation_map: dict[ObjectId, dict] = {}
        if version_ids and settings.question_store == "postgres":
            evaluation_map = self.questions.latest_evaluations(version_ids)
        elif version_ids and hasattr(self.db, "question_evaluations"):
            evaluations = list(
                self.db.question_evaluations.find(
                    {"question_version_id": {"$in": version_ids}},
                ).sort("created_at", -1)
            )
            for evaluation in evaluations:
                version_id = evaluation.get("question_version_id")
                if version_id not in evaluation_map:
                    evaluation_map[version_id] = evaluation
        calibration_sample = 0
        calibration_agreements = 0
        calibration_disagreements = 0
        ai_failed_but_approved = 0
        ai_passed_but_not_approved = 0
        criterion_calibration = {
            key: {"sample_size": 0, "agreements": 0, "disagreements": 0}
            for key in REVIEW_CRITERION_KEYS
        }
        for review in reviews:
            evaluation = evaluation_map.get(review.get("question_version_id"))
            # Bulk approvals mark every criterion as passed without a
            # per-item check, so they would inflate human/AI agreement.
            if not evaluation or review.get("bulk"):
                continue
            calibration_sample += 1
            ai_positive = bool(evaluation.get("passed"))
            human_positive = review.get("decision") == "APPROVED"
            bucket = per_reviewer.get(review.get("reviewer_user_id")) if is_admin else None
            if bucket is not None:
                bucket["calibration_sample"] += 1
                bucket["calibration_agreements"] += int(ai_positive == human_positive)
            if ai_positive == human_positive:
                calibration_agreements += 1
            else:
                calibration_disagreements += 1
                if human_positive:
                    ai_failed_but_approved += 1
                else:
                    ai_passed_but_not_approved += 1
            review_form = review.get("review_form") or {}
            human_by_key = {
                item.get("key"): item.get("rating")
                for item in review_form.get("criterion_assessments") or []
                if item.get("key") in criterion_calibration
            }
            if not human_by_key:
                for item in review_form.get("checklist") or []:
                    key = LEGACY_REVIEW_CRITERION_MAP.get(item.get("key"), item.get("key"))
                    if key in criterion_calibration:
                        human_by_key[key] = "PASS" if item.get("passed") else "FAIL"
            evaluation_scores = evaluation.get("scores") or {}
            skipped_keys = not_applicable_criteria(evaluation.get("evidence"))
            for key, human_rating in human_by_key.items():
                ai_score = evaluation_scores.get(key)
                if (
                    not isinstance(ai_score, (int, float))
                    or human_rating == "NO_DATA"
                    or key in skipped_keys
                ):
                    continue
                ai_positive = ai_score >= DEFAULT_THRESHOLDS["pass_min"]
                human_positive = human_rating == "PASS"
                item = criterion_calibration[key]
                item["sample_size"] += 1
                if ai_positive == human_positive:
                    item["agreements"] += 1
                else:
                    item["disagreements"] += 1

        audit_match: dict = {
            "action": {"$in": ["QUESTION_APPROVED", "QUESTION_REJECTED", "QUESTION_NEEDS_REVISION"]},
            "created_at": {"$gte": since_30d},
        }
        if not is_admin:
            audit_match["actor.user_id"] = current_user.id
        durations: list[float] = []
        decision_audits = (
            self.questions.review_decision_audits(
                since_30d, actor_user_id=None if is_admin else current_user.id,
            )
            if settings.question_store == "postgres"
            else self.db.audit_logs.find(
                audit_match,
                {"metadata.review_assignment": 1, "created_at": 1, "actor.user_id": 1},
            )
        )
        for audit in decision_audits:
            assignment = ((audit.get("metadata") or {}).get("review_assignment") or {})
            start = _as_aware_utc(assignment.get("claimed_at") or assignment.get("assigned_at"))
            end = _as_aware_utc(audit.get("created_at"))
            if start and end and end >= start:
                hours = (end - start).total_seconds() / 3600
                durations.append(hours)
                if is_admin:
                    actor_id = (audit.get("actor") or {}).get("user_id")
                    if actor_id in per_reviewer:
                        per_reviewer[actor_id]["durations"].append(hours)
        average_review_hours = (
            round(sum(durations) / len(durations), 6)
            if durations
            else None
        )

        version_subjects: dict[ObjectId, str] = {}
        for version_id, subject in self._version_subjects(version_ids).items():
            subject_id = subject.get("id") if isinstance(subject, dict) else None
            version_subjects[version_id] = str(subject_id) if subject_id else "unknown"
        subject_counts: dict[str, int] = {}
        for review in reviews:
            key = version_subjects.get(review.get("question_version_id"))
            if key is None:
                continue
            subject_counts[key] = subject_counts.get(key, 0) + 1
        subject_oids = [
            ObjectId(subject_id)
            for subject_id in subject_counts
            if subject_id != "unknown" and ObjectId.is_valid(subject_id)
        ]
        subject_rows = subject_records(self.db, ids=subject_oids) if subject_oids else []
        subject_labels = {
            str(record["_id"]): (
                record.get("subject_code")
                or record.get("subject_name")
                or str(record["_id"])
            )
            for record in subject_rows
        }
        subjects = [
            {
                "subject_id": None if subject_id == "unknown" else subject_id,
                "label": subject_labels.get(subject_id, "Chưa gắn môn"),
                "reviewed": count,
            }
            for subject_id, count in sorted(subject_counts.items(), key=lambda item: item[1], reverse=True)[:6]
        ]

        total_reviews = len(reviews)
        approved = decision_counts["APPROVED"]
        performance = {
            "reviews_7d": reviews_last_7d,
            "reviews_30d": total_reviews,
            "approval_rate": round(approved / total_reviews, 3) if total_reviews else None,
            "override_count": override_count,
            "bulk_count": bulk_count,
            "revision_issues": revision_issues,
            "top_reasons": [
                {"code": code, "count": count}
                for code, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:6]
            ],
            "average_review_hours": average_review_hours,
            "duration_sample_size": len(durations),
        }
        calibration = {
            "sample_size": calibration_sample,
            "agreement_rate": (
                round(calibration_agreements / calibration_sample, 3)
                if calibration_sample
                else None
            ),
            "agreements": calibration_agreements,
            "disagreements": calibration_disagreements,
            "ai_failed_but_approved": ai_failed_but_approved,
            "ai_passed_but_not_approved": ai_passed_but_not_approved,
            "criteria": {
                key: {
                    **value,
                    "agreement_rate": (
                        round(value["agreements"] / value["sample_size"], 3)
                        if value["sample_size"]
                        else None
                    ),
                }
                for key, value in criterion_calibration.items()
            },
        }
        return json_safe(
            {
                "workload": workload,
                "performance": performance,
                "calibration": calibration,
                "decisions": decision_counts,
                "subjects": subjects,
                "reviewers": self._reviewer_breakdown(per_reviewer, now) if is_admin else [],
                "generated_at": now,
                "scope": "all_reviewers" if is_admin else "current_reviewer",
            }
        )

    def _sla_cutoff(self, now) -> object:
        return now - timedelta(hours=max(1, settings.review_sla_hours))

    def _reviewer_breakdown(self, per_reviewer: dict, now) -> list[dict]:
        """Per-reviewer workload and quality signals for the Admin dashboard."""
        sla_cutoff = _as_aware_utc(self._sla_cutoff(now))
        holding: dict = {}
        held_questions = (
            self.questions.held_reviews()
            if settings.question_store == "postgres"
            else self.db.questions.find(
                {
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                    "review_assignment.status": {"$in": ["ASSIGNED", "IN_REVIEW"]},
                },
                {"review_assignment": 1, "review_submission.submitted_at": 1},
            )
        )
        for question in held_questions:
            reviewer_id = (question.get("review_assignment") or {}).get("reviewer_user_id")
            if reviewer_id is None:
                continue
            counts = holding.setdefault(reviewer_id, {"holding": 0, "holding_sla_breached": 0})
            counts["holding"] += 1
            submitted_at = _as_aware_utc((question.get("review_submission") or {}).get("submitted_at"))
            if submitted_at and submitted_at <= sla_cutoff:
                counts["holding_sla_breached"] += 1

        users = {
            user["_id"]: user
            for user in review_candidate_users(
                self.db,
                include_ids=[key for key in {*per_reviewer, *holding} if key is not None],
            )
        }
        # Admin chỉ hiện khi có hoạt động duyệt; người có quyền duyệt khác luôn hiện.
        candidate_ids = {
            user_id
            for user_id, user in users.items()
            if user.get("role") != "Admin" and _user_can_review(user)
        }
        rows = []
        for reviewer_id in {*candidate_ids, *per_reviewer, *holding}:
            if reviewer_id is None:
                continue
            user = users.get(reviewer_id) or {}
            stats = per_reviewer.get(reviewer_id) or {}
            load = holding.get(reviewer_id) or {}
            total = stats.get("reviews_30d", 0)
            sample = stats.get("calibration_sample", 0)
            durations = stats.get("durations") or []
            override_rate = round(stats.get("override_count", 0) / total, 3) if total else None
            bulk_rate = round(stats.get("bulk_count", 0) / total, 3) if total else None
            flags = []
            if total >= REVIEWER_FLAG_MIN_REVIEWS and (override_rate or 0) >= HIGH_OVERRIDE_RATE:
                flags.append("HIGH_OVERRIDE")
            if total >= REVIEWER_FLAG_MIN_REVIEWS and (bulk_rate or 0) >= HIGH_BULK_RATE:
                flags.append("HIGH_BULK")
            if load.get("holding_sla_breached"):
                flags.append("SLA_BREACHED")
            subject_ids = user.get("review_subject_ids") or []
            if user and user.get("role") != "Admin" and _user_can_review(user) and not subject_ids:
                flags.append("NO_SUBJECTS")
            rows.append(
                {
                    "user_id": reviewer_id,
                    "display_name": user.get("display_name") or user.get("email") or str(reviewer_id),
                    "email": user.get("email"),
                    "role": user.get("role"),
                    "is_active": user.get("is_active", False),
                    "review_subject_ids": subject_ids,
                    "reviews_7d": stats.get("reviews_7d", 0),
                    "reviews_30d": total,
                    "decisions": stats.get(
                        "decisions",
                        {"APPROVED": 0, "NEEDS_REVISION": 0, "REJECTED": 0},
                    ),
                    "approval_rate": (
                        round(stats["decisions"]["APPROVED"] / total, 3) if total else None
                    ),
                    "override_count": stats.get("override_count", 0),
                    "override_rate": override_rate,
                    "bulk_count": stats.get("bulk_count", 0),
                    "ai_agreement_rate": (
                        round(stats["calibration_agreements"] / sample, 3) if sample else None
                    ),
                    "ai_sample_size": sample,
                    "average_review_hours": (
                        round(sum(durations) / len(durations), 6) if durations else None
                    ),
                    "holding": load.get("holding", 0),
                    "holding_sla_breached": load.get("holding_sla_breached", 0),
                    "flags": flags,
                }
            )
        rows.sort(key=lambda row: (-len(row["flags"]), -row["holding"], -row["reviews_30d"], row["display_name"]))
        return rows

    def _mark_mongo_sla_reminded(self, question: dict, submitted_at, now) -> bool:
        marked = self.db.questions.update_one(
            {
                "_id": question["_id"],
                "review_status": "PENDING",
                "review_sla.reminded_submission_at": {"$ne": submitted_at},
            },
            {
                "$set": {
                    "review_sla": {
                        "reminded_submission_at": submitted_at,
                        "reminded_at": now,
                    }
                }
            },
        )
        return bool(marked.matched_count)

    def send_review_sla_reminders(self, now=None) -> int:
        """Notify once per submission when a pending question breaches the review SLA.

        The current assignee is reminded; unassigned questions go to active Admins.
        """
        now = now or utc_now()
        cutoff = self._sla_cutoff(now)
        postgres = settings.question_store == "postgres"
        candidates = self.questions.sla_breach_candidates(
            cutoff, SLA_REMINDER_BATCH_SIZE,
        ) if postgres else list(
            self.db.questions.find(
                {
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                    "review_submission.submitted_at": {"$lte": cutoff},
                },
                {
                    "question_code": 1,
                    "current_version_id": 1,
                    "review_assignment": 1,
                    "review_submission.submitted_at": 1,
                    "review_sla": 1,
                },
            ).limit(SLA_REMINDER_BATCH_SIZE)
        )
        admin_ids: list | None = None
        notifications = NotificationService(self.db)
        sent = 0
        for question in candidates:
            submitted_at = (question.get("review_submission") or {}).get("submitted_at")
            if (question.get("review_sla") or {}).get("reminded_submission_at") == submitted_at:
                continue
            assignment = question.get("review_assignment") or {}
            if assignment.get("status") in {"ASSIGNED", "IN_REVIEW"} and assignment.get("reviewer_user_id"):
                recipients = [assignment["reviewer_user_id"]]
            else:
                if admin_ids is None:
                    admin_ids = active_admin_ids(self.db)
                recipients = admin_ids
            question_code = question.get("question_code", "Câu hỏi")
            reminders = [
                {
                    "recipient_user_id": recipient,
                    "type": "QUESTION_REVIEW_SLA_BREACHED",
                    "title": f"{question_code} đã chờ duyệt quá {settings.review_sla_hours} giờ",
                    "body": "Câu hỏi đã vượt hạn kiểm duyệt, cần được xử lý hoặc phân công lại.",
                    "link": f"/kiem-duyet?questionId={question['_id']}",
                    "entity": json_safe(
                        {
                            "type": "QUESTION",
                            "id": question["_id"],
                            "version_id": question.get("current_version_id"),
                            "question_code": question_code,
                        }
                    ),
                }
                for recipient in recipients
            ]
            outbox = notification_outbox()
            if outbox is not None:
                NotificationService(self.db, sink=outbox).create_many(reminders)
            if postgres:
                if not self.questions.mark_sla_reminded(question["_id"], submitted_at, now,
                                                        notifications=outbox):
                    continue
            elif not self._mark_mongo_sla_reminded(question, submitted_at, now):
                continue
            if outbox is None:
                notifications.create_many(reminders)
            sent += 1
        return sent

    @staticmethod
    def _is_claimed(assignment: dict, now) -> bool:
        expires = _as_aware_utc(assignment.get("lock_expires_at"))
        return assignment.get("status") == "IN_REVIEW" and expires is not None and expires > now

    def _mark_mongo_escalation(self, question: dict, submitted_at, escalation: dict, *,
                               assignment: dict | None, audit: dict | None) -> bool:
        step = next(key for key in escalation if key != "submitted_at")
        query: dict = {
            "_id": question["_id"],
            "review_status": "PENDING",
            "review_submission.submitted_at": submitted_at,
            "$or": [
                {"review_escalation.submitted_at": {"$ne": submitted_at}},
                {f"review_escalation.{step}": None},
            ],
        }
        updates = {f"review_escalation.{key}": value for key, value in escalation.items()}
        if assignment is not None:
            query["$nor"] = [{
                "review_assignment.status": "IN_REVIEW",
                "review_assignment.lock_expires_at": {"$gt": utc_now()},
            }]
            updates["review_assignment"] = assignment
        if (question.get("review_escalation") or {}).get("submitted_at") != submitted_at:
            updates = {"review_escalation": escalation, **{
                key: value for key, value in updates.items() if not key.startswith("review_escalation.")
            }}
        with mongo_transaction() as session:
            marked = self.db.questions.update_one(query, {"$set": updates}, session=session)
            if marked.matched_count and audit:
                write_audit_event(self.db, session=session, **audit)
        return bool(marked.matched_count)

    def escalate_overdue_reviews(self, now=None) -> dict:
        """Escalate pending questions nobody has claimed.

        After ``REVIEW_ESCALATION_HOURS`` active Admins are notified once per
        submission; after ``REVIEW_REQUEUE_DAYS`` an assignment that was never
        claimed is removed and the question returns to the shared queue.
        """
        now = now or utc_now()
        notify_cutoff = now - timedelta(hours=max(1, settings.review_escalation_hours))
        requeue_cutoff = now - timedelta(days=max(1, settings.review_requeue_days))
        postgres = settings.question_store == "postgres"
        # Only questions with an escalation step still due, so handled ones cannot fill the batch forever.
        candidates = self.questions.escalation_candidates(
            notify_cutoff, SLA_REMINDER_BATCH_SIZE, requeue_cutoff=requeue_cutoff, now=now,
        ) if postgres else list(
            self.db.questions.find(
                {
                    "schema_version": SCHEMA_VERSION,
                    "lifecycle_status": "ACTIVE",
                    "review_status": "PENDING",
                    "review_submission.submitted_at": {"$lte": notify_cutoff},
                    "$nor": [{"review_assignment.status": "IN_REVIEW",
                              "review_assignment.lock_expires_at": {"$gt": now}}],
                    "$or": [
                        {"$expr": {"$ne": ["$review_escalation.submitted_at", "$review_submission.submitted_at"]}},
                        {"review_escalation.notified_at": None},
                        {
                            "review_submission.submitted_at": {"$lte": requeue_cutoff},
                            "review_assignment.reviewer_user_id": {"$ne": None},
                            "review_escalation.requeued_at": None,
                        },
                    ],
                },
            ).sort([("review_submission.submitted_at", 1), ("_id", 1)]).limit(SLA_REMINDER_BATCH_SIZE)
        )
        admin_ids: list | None = None
        result = {"notified": 0, "requeued": 0}
        for question in candidates:
            submitted_at = (question.get("review_submission") or {}).get("submitted_at")
            assignment = question.get("review_assignment") or {}
            if submitted_at is None or self._is_claimed(assignment, now):
                continue
            escalation = question.get("review_escalation") or {}
            same_submission = escalation.get("submitted_at") == submitted_at
            question_code = question.get("question_code", "Câu hỏi")
            if (
                _as_aware_utc(submitted_at) <= requeue_cutoff
                and assignment.get("reviewer_user_id")
                and not (same_submission and escalation.get("requeued_at"))
            ):
                released = _empty_review_assignment(now, "escalation_requeued")
                audit = {
                    "action": "QUESTION_REVIEW_RELEASED",
                    "entity_type": "question",
                    "entity_id": question["_id"],
                    "entity_version_id": question.get("current_version_id"),
                    "actor_type": "SYSTEM",
                    "service_name": "review_escalation",
                    "before": {"review_assignment": json_safe(assignment)},
                    "after": {"review_assignment": json_safe(released)},
                    "metadata": {"reason": "escalation_requeued"},
                }
                step = {"submitted_at": submitted_at, "requeued_at": now}
                applied = (
                    self.questions.mark_review_escalation(question["_id"], submitted_at, step,
                                                          assignment=released, audit=audit)
                    if postgres else
                    self._mark_mongo_escalation(question, submitted_at, step, assignment=released, audit=audit)
                )
                if applied:
                    result["requeued"] += 1
                continue
            if same_submission and escalation.get("notified_at"):
                continue
            if admin_ids is None:
                admin_ids = active_admin_ids(self.db)
            alerts = [
                {
                    "recipient_user_id": admin_id,
                    "type": "QUESTION_REVIEW_ESCALATED",
                    "title": f"{question_code} chờ duyệt quá {settings.review_escalation_hours} giờ chưa ai nhận",
                    "body": "Hãy phân công reviewer hoặc tự xử lý câu hỏi này.",
                    "link": f"/kiem-duyet?questionId={question['_id']}",
                    "entity": json_safe({
                        "type": "QUESTION",
                        "id": question["_id"],
                        "version_id": question.get("current_version_id"),
                        "question_code": question_code,
                    }),
                }
                for admin_id in admin_ids
            ]
            step = {"submitted_at": submitted_at, "notified_at": now}
            outbox = notification_outbox()
            if outbox is not None:
                NotificationService(self.db, sink=outbox).create_many(alerts)
            applied = (
                self.questions.mark_review_escalation(question["_id"], submitted_at, step, notifications=outbox)
                if postgres else
                self._mark_mongo_escalation(question, submitted_at, step, assignment=None, audit=None)
            )
            if not applied:
                continue
            if outbox is None:
                NotificationService(self.db).create_many(alerts)
            result["notified"] += 1
        return result


def get_workflow_service() -> QuestionWorkflowService:
    return QuestionWorkflowService(get_database())


async def _wait_for_evaluation_job_superseded(
    service: QuestionWorkflowService,
    job_id: str,
    worker_id: str,
    stop_event: asyncio.Event,
) -> bool:
    """Return True as soon as this worker's evaluation job is no longer active."""
    poll_seconds = max(0.1, min(float(settings.job_worker_poll_seconds), 1.0))
    while not stop_event.is_set():
        job = await asyncio.to_thread(service.evaluation_job_state, job_id)
        if not job:
            return True
        status = job.get("status")
        if status not in EVALUATION_ACTIVE_STATUSES:
            return True
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            continue
    return False


async def process_evaluation_job_background(job_id: str, worker_id: str) -> None:
    from core.job_worker import maintain_lease

    service = get_workflow_service()
    lease_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        maintain_lease(lambda: service.heartbeat_evaluation_job(job_id, worker_id), lease_stop)
    )
    async with evaluation_semaphore:
        processing_task = asyncio.create_task(service.process_evaluation_job(job_id, worker_id))
        superseded_task = asyncio.create_task(
            _wait_for_evaluation_job_superseded(
                service,
                job_id,
                worker_id,
                lease_stop,
            )
        )
        try:
            done, _ = await asyncio.wait(
                {processing_task, superseded_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            superseded = (
                superseded_task in done
                and superseded_task.result()
                and not processing_task.done()
            )
            if superseded:
                processing_task.cancel()
                try:
                    await processing_task
                except asyncio.CancelledError:
                    stopped = await asyncio.to_thread(
                        service.evaluation_job_state, job_id,
                    ) or {}
                    logger.info(
                        "Evaluation job %s stopped early: status=%s reason=%s",
                        job_id,
                        stopped.get("status"),
                        (stopped.get("error") or {}).get("stage") or "superseded",
                    )
            else:
                await processing_task
        finally:
            lease_stop.set()
            if not processing_task.done():
                processing_task.cancel()
                try:
                    await processing_task
                except asyncio.CancelledError:
                    pass
            if not superseded_task.done():
                superseded_task.cancel()
                try:
                    await superseded_task
                except asyncio.CancelledError:
                    pass
            await heartbeat_task
