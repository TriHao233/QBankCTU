from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.config import settings
from modules.questions.review_reasons import review_reason_errors


class EvaluationScores(BaseModel):
    faithfulness: float = Field(..., ge=0, le=1)
    contextual_relevancy: float = Field(..., ge=0, le=1)
    answer_relevancy: float = Field(..., ge=0, le=1)
    bloom_alignment: float = Field(..., ge=0, le=1)
    clo_alignment: float = Field(..., ge=0, le=1)


class EvaluationCreateRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    scores: EvaluationScores
    feedback: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    evaluator_model_code: str = "manual-or-external-evaluator"
    model_snapshot: dict[str, Any] = Field(default_factory=dict)
    model_execution: dict[str, Any] = Field(default_factory=dict)
    raw_model_response: str | None = None
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    prompt_snapshot: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = Field(None, ge=0)
    evaluation_job_id: str | None = None
    trigger: str | None = None


class ReviewOverride(BaseModel):
    applied: bool = False
    score: float | None = Field(None, ge=0, le=1)
    color: Literal["RED", "YELLOW", "GREEN"] | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def require_reason(self):
        if self.applied and not (self.reason or "").strip():
            raise ValueError("Lý do ghi đè (override reason) là bắt buộc")
        return self


class ReviewRubricItem(BaseModel):
    key: str = Field(..., min_length=1, max_length=80)
    label: str = Field(..., min_length=1, max_length=160)
    passed: bool
    note: str = Field("", max_length=500)


class ReviewCriterionAssessment(BaseModel):
    key: Literal[
        "faithfulness",
        "contextual_relevancy",
        "answer_relevancy",
        "bloom_alignment",
        "clo_alignment",
    ]
    label: str = Field(..., min_length=1, max_length=160)
    rating: Literal["PASS", "REVIEW", "FAIL", "NO_DATA"]
    note: str = Field("", max_length=1000)
    source_chunk_id: str | None = Field(None, max_length=120)
    page_number: int | None = Field(None, ge=1)


class ReviewRevisionIssue(BaseModel):
    title: str = Field(..., min_length=1, max_length=160)
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"
    detail: str = Field("", max_length=1000)
    source_chunk_id: str | None = Field(None, max_length=120)
    page_number: int | None = Field(None, ge=1)


class StructuredReviewForm(BaseModel):
    checklist: list[ReviewRubricItem] = Field(default_factory=list, max_length=12)
    criterion_assessments: list[ReviewCriterionAssessment] = Field(default_factory=list, max_length=5)
    overall_note: str = Field("", max_length=2000)
    revision_issues: list[ReviewRevisionIssue] = Field(default_factory=list, max_length=20)

    def has_reason(self) -> bool:
        if self.overall_note.strip():
            return True
        return any(
            issue.title.strip() or issue.detail.strip()
            for issue in self.revision_issues
        )


class ReviewDraftUpsertRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    decision: Literal["APPROVED", "REJECTED", "NEEDS_REVISION"] | None = None
    draft: dict[str, Any] = Field(default_factory=dict)


class AutoEvaluationRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    evaluator_model_code: str = Field(
        default_factory=lambda: settings.evaluation_model_provider,
        min_length=1,
        max_length=80,
    )
    fallback_to_heuristic: bool = False


class ReviewCreateRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    decision: Literal["APPROVED", "REJECTED", "NEEDS_REVISION"]
    note: str = Field("", max_length=2000)
    override: ReviewOverride = Field(default_factory=ReviewOverride)
    review_form: StructuredReviewForm = Field(default_factory=StructuredReviewForm)
    secondary_required: bool = False
    secondary_reason: str = Field("", max_length=500)
    # Set by list-level bulk approval: the checklist was not filled per item,
    # so these reviews are kept out of human-vs-AI calibration.
    bulk: bool = False
    # Quản trị viên tự duyệt câu do chính mình tạo/sửa phải ghi lý do (được lưu và ghi nhật ký).
    self_review_reason: str = Field("", max_length=500)
    # Codes from REVIEW_REASON_CATALOG; at least one is required to reject or ask for revision.
    reason_codes: list[str] = Field(default_factory=list, max_length=12)
    # Submission-check flags the reviewer confirmed seeing; approving requires every current flag.
    acknowledged_flags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def require_structured_reason(self):
        self.acknowledged_flags = list(dict.fromkeys(
            flag.strip() for flag in self.acknowledged_flags if flag.strip()
        ))
        has_reason = bool(self.note.strip()) or self.review_form.has_reason()
        if self.decision == "REJECTED" and not has_reason:
            raise ValueError("Reject cần ghi rõ lý do")
        if self.decision == "NEEDS_REVISION" and not self.review_form.revision_issues:
            raise ValueError("Cần ít nhất một lỗi cần Teacher sửa")
        self.reason_codes = list(dict.fromkeys(code.strip().upper() for code in self.reason_codes if code.strip()))
        has_note = bool(self.note.strip() or self.review_form.overall_note.strip())
        errors = review_reason_errors(self.decision, self.reason_codes, has_note=has_note)
        if errors:
            raise ValueError(errors[0])
        return self


class QuestionCommentCreateRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)
    mention_user_ids: list[str] = Field(default_factory=list, max_length=20)


class QuestionCommentUpdateRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=2000)


class SecondaryReviewRequest(BaseModel):
    required: bool = True
    reason: str = Field("", max_length=500)
    reviewer_user_id: str | None = Field(None, min_length=1, max_length=120)


class ReviewAssignmentRequest(BaseModel):
    reviewer_user_id: str | None = Field(None, min_length=1, max_length=120)
    note: str = Field("", max_length=500)


class ReviewClaimRequest(BaseModel):
    # Required only when an Admin takes a question another reviewer still holds.
    reason: str = Field("", max_length=500)


class AutoAssignRequest(BaseModel):
    # Restrict the run to these questions; empty means every open question.
    question_ids: list[str] = Field(default_factory=list, max_length=500)
    limit: int = Field(100, ge=1, le=500)
    max_load_per_reviewer: int = Field(20, ge=1, le=500)
    # "prefer": specialists first, reviewers without subjects as fallback.
    # "strict": only reviewers whose review_subject_ids cover the question.
    subject_mode: Literal["prefer", "strict"] = "prefer"
    include_admins: bool = False


class ReviewPolicyPayload(BaseModel):
    """Rules that force a second review on approval, set by Admin."""

    secondary_on_override: bool = False
    # Approvals of questions whose AI overall score is below this need a second reviewer.
    secondary_below_score: float | None = Field(None, ge=0, le=1)
    secondary_subject_ids: list[str] = Field(default_factory=list, max_length=200)


class MoodlePublicationRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    target_id: str | None = Field(None, min_length=1, max_length=120)
    moodle_site_id: str = Field("demo-moodle", min_length=1, max_length=120)
    course_id: str = Field("ctdl-demo", min_length=1, max_length=120)
    category_id: str = Field("qbank-demo", min_length=1, max_length=120)
    export_format: Literal["GIFT", "XML", "BOTH"] = "BOTH"
    mock: bool = True
