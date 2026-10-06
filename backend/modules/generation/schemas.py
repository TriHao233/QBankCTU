from datetime import datetime
from enum import Enum
from typing import List, Optional, Any, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

from core.config import settings

class BloomLevel(str, Enum):
    NHO = "1_nho"
    HIEU = "2_hieu"
    VAN_DUNG = "3_van_dung"
    PHAN_TICH = "4_phan_tich"
    DANH_GIA = "5_danh_gia"
    SANG_TAO = "6_sang_tao"

class QuestionType(str, Enum):
    TRAC_NGHIEM = "trac_nghiem"
    DUNG_SAI = "dung_sai"
    DIEN_KHUYET = "dien_khuyet"
    GHEP_COT = "ghep_cot"
    TINH_HUONG = "tinh_huong"
    SAP_XEP = "sap_xep"
    NHIEU_LUA_CHON = "nhieu_lua_chon"


def _bloom_range(first: int, last: int) -> frozenset[BloomLevel]:
    return frozenset(level for level in BloomLevel if first <= int(level.value.split("_", 1)[0]) <= last)


# Must stay in sync with QUESTION_TYPE_BLOOM_MATRIX in frontend/src/constants/generationEnums.js.
QUESTION_TYPE_BLOOM_LEVELS: dict[QuestionType, frozenset[BloomLevel]] = {
    QuestionType.TRAC_NGHIEM: _bloom_range(1, 5),
    QuestionType.NHIEU_LUA_CHON: _bloom_range(1, 5),
    QuestionType.DUNG_SAI: _bloom_range(1, 3),
    QuestionType.DIEN_KHUYET: _bloom_range(1, 3),
    QuestionType.GHEP_COT: _bloom_range(1, 2),
    QuestionType.SAP_XEP: _bloom_range(1, 3),
    QuestionType.TINH_HUONG: _bloom_range(3, 6),
}


def is_bloom_allowed(question_type: QuestionType, bloom_level: BloomLevel) -> bool:
    return bloom_level in QUESTION_TYPE_BLOOM_LEVELS.get(question_type, frozenset())


QuestionDifficulty = Literal["de", "trung_binh", "kho"]

MAX_QUESTIONS_PER_JOB = 50
# Every question format at every difficulty level.
MAX_PLAN_ITEMS = len(QuestionType) * 3


class QuestionPlanItem(BaseModel):
    question_type: QuestionType
    bloom_level: Optional[BloomLevel] = None
    difficulty: Optional[QuestionDifficulty] = None
    num_questions: int = Field(default=1, ge=1, le=MAX_QUESTIONS_PER_JOB)
    content_mode: Literal["auto", "code", "general"] = "auto"


MAX_EXTRA_DOCUMENTS = 4
MAX_TARGET_CLOS = 20


class GenerationDistribution(BaseModel):
    """Ratio-based structure expanded server-side into a question_plan."""

    total_questions: int = Field(..., ge=1, le=MAX_QUESTIONS_PER_JOB)
    question_types: List[QuestionType] = Field(..., min_length=1, max_length=len(QuestionType))
    bloom_ratios: dict[BloomLevel, float] = Field(..., min_length=1)
    difficulty_ratios: dict[QuestionDifficulty, float] = Field(default_factory=dict)

    @field_validator("bloom_ratios", "difficulty_ratios")
    @classmethod
    def validate_ratios(cls, value: dict) -> dict:
        if any(ratio < 0 for ratio in value.values()):
            raise ValueError("Tỷ lệ phân bố không được âm.")
        if value and sum(value.values()) <= 0:
            raise ValueError("Tổng tỷ lệ phân bố phải lớn hơn 0.")
        return {key: ratio for key, ratio in value.items() if ratio > 0}


def _largest_remainder(total: int, ratios: dict) -> dict:
    weight = sum(ratios.values())
    exact = {key: total * ratio / weight for key, ratio in ratios.items()}
    counts = {key: int(value) for key, value in exact.items()}
    remaining = total - sum(counts.values())
    order = list(ratios)
    for key in sorted(order, key=lambda item: (-(exact[item] - counts[item]), order.index(item)))[:remaining]:
        counts[key] += 1
    return {key: count for key, count in counts.items() if count > 0}


def expand_distribution(distribution: GenerationDistribution) -> List["QuestionPlanItem"]:
    bloom_counts = _largest_remainder(distribution.total_questions, distribution.bloom_ratios)
    counts: dict[tuple[QuestionType, BloomLevel, Optional[str]], int] = {}
    rotation = 0
    for bloom_level, bloom_count in bloom_counts.items():
        allowed = [item for item in distribution.question_types if is_bloom_allowed(item, bloom_level)]
        if not allowed:
            raise ValueError(
                f"Không có dạng câu hỏi đã chọn nào phù hợp với mức Bloom '{bloom_level.value}'."
            )
        cells = (
            _largest_remainder(bloom_count, distribution.difficulty_ratios)
            if distribution.difficulty_ratios else {None: bloom_count}
        )
        for difficulty, cell_count in cells.items():
            for _ in range(cell_count):
                key = (allowed[rotation % len(allowed)], bloom_level, difficulty)
                counts[key] = counts.get(key, 0) + 1
                rotation += 1
    if len(counts) > MAX_PLAN_ITEMS:
        raise ValueError(f"Phân bố tạo quá {MAX_PLAN_ITEMS} dòng cơ cấu; hãy giảm số dạng câu hoặc mức tỷ lệ.")
    return [
        QuestionPlanItem(question_type=question_type, bloom_level=bloom_level, difficulty=difficulty, num_questions=count)
        for (question_type, bloom_level, difficulty), count in counts.items()
    ]


class GenerationClientTelemetry(BaseModel):
    source_mode: Optional[str] = None
    document_reused: bool = False
    upload_ms: Optional[int] = Field(None, ge=0)
    ocr_ms: Optional[int] = Field(None, ge=0)
    chunk_ms: Optional[int] = Field(None, ge=0)
    elapsed_before_generate_ms: Optional[int] = Field(None, ge=0)


class QuestionGenerateRequest(BaseModel):
    # Xóa context: str, thay bằng document_id để gọi ChromaDB
    document_id: str = Field(..., description="ID của giáo trình trong DB")
    document_ids: List[str] = Field(
        default_factory=list,
        max_length=MAX_EXTRA_DOCUMENTS,
        description="Tài liệu bổ sung cùng học phần với document_id",
    )
    target_clo_ids: List[str] = Field(
        default_factory=list,
        max_length=MAX_TARGET_CLOS,
        description="Chỉ sinh câu hỏi đánh giá các CLO này",
    )
    distribution: Optional[GenerationDistribution] = Field(
        None,
        description="Phân bố Bloom/độ khó theo tỷ lệ; được tách thành question_plan",
    )
    collection_name: str = settings.chromadb_collection_name
    target_heading: Optional[str] = Field(None, description="Tên mục lục muốn giới hạn sinh câu hỏi")
    instruction: Optional[str] = Field(
        None,
        max_length=1200,
        description="Yêu cầu/chủ đề cụ thể từ giảng viên khi sinh câu hỏi",
    )
    bloom_level: BloomLevel
    difficulty: Optional[QuestionDifficulty] = Field(
        None,
        description="Độ khó ước lượng mong muốn; mỗi dòng trong question_plan có thể ghi đè giá trị này.",
    )
    question_type: QuestionType = QuestionType.TRAC_NGHIEM
    num_questions: int = Field(default=1, ge=1, le=MAX_QUESTIONS_PER_JOB)
    model_provider: str = Field(default_factory=lambda: settings.model_provider, min_length=1, max_length=160)
    code_model_provider: str = Field(
        default_factory=lambda: settings.code_generation_model_provider,
        min_length=1,
        max_length=160,
    )
    question_plan: Optional[List[QuestionPlanItem]] = Field(
        None,
        min_length=1,
        max_length=MAX_PLAN_ITEMS,
        description="Cơ cấu số lượng câu hỏi theo từng dạng",
    )
    client_telemetry: Optional[GenerationClientTelemetry] = Field(
        None,
        description="Timing đo ở frontend trước khi enqueue job sinh câu hỏi",
    )

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("document_ids", "target_clo_ids")
    @classmethod
    def dedupe_ids(cls, value: List[str]) -> List[str]:
        return list(dict.fromkeys(item.strip() for item in value if item and item.strip()))

    @model_validator(mode="after")
    def expand_ratio_distribution(self):
        self.document_ids = [item for item in self.document_ids if item != self.document_id]
        if self.distribution is None:
            return self
        expanded = expand_distribution(self.distribution)
        # A persisted job snapshot carries both fields; accept it only when the plan is the expansion.
        if self.question_plan and [item.model_dump() for item in self.question_plan] != [
            item.model_dump() for item in expanded
        ]:
            raise ValueError("Chỉ dùng một trong hai: question_plan hoặc distribution.")
        self.question_plan = expanded
        return self

    def all_document_ids(self) -> List[str]:
        return [self.document_id, *self.document_ids]

    @model_validator(mode="after")
    def validate_total_questions(self):
        total = sum(item.num_questions for item in self.question_plan) if self.question_plan else self.num_questions
        if total < 1 or total > MAX_QUESTIONS_PER_JOB:
            raise ValueError(f"Tổng số câu hỏi phải từ 1 đến {MAX_QUESTIONS_PER_JOB}.")
        return self

    @model_validator(mode="after")
    def validate_bloom_matrix(self):
        for item in self.effective_plan():
            bloom_level = item.bloom_level or self.bloom_level
            if not is_bloom_allowed(item.question_type, bloom_level):
                raise ValueError(
                    f"Mức Bloom '{bloom_level.value}' không phù hợp với dạng câu hỏi "
                    f"'{item.question_type.value}'."
                )
        return self

    def effective_plan(self) -> List[QuestionPlanItem]:
        if self.question_plan:
            return self.question_plan
        return [
            QuestionPlanItem(
                question_type=self.question_type,
                num_questions=self.num_questions,
            )
        ]

class GeneratedQuestion(BaseModel):
    question: str
    # Đổi sang Any để hỗ trợ JSON linh hoạt cho câu Ghép cột/Sắp xếp
    options: Optional[Any] = None
    correct_answer: str
    explanation: str
    question_type: str
    bloom_level: str
    difficulty: Optional[str] = None
    source_context: str
    source_keywords: List[str] = Field(default_factory=list)
    validation_warnings: List[str] = Field(default_factory=list)
    clo_codes: List[str] = Field(default_factory=list)
    false_mutation: Optional[dict[str, Any]] = None
    question_id: Optional[str] = None
    question_code: Optional[str] = None
    current_version: Optional[int] = None
    current_version_id: Optional[str] = None
    review_status: Optional[str] = None
    document_id: Optional[str] = None
    generation_run_id: Optional[str] = None
    source_chunk_ids: List[str] = Field(default_factory=list)
    clo_ids: List[str] = Field(default_factory=list)


class GenerationRejection(BaseModel):
    code: str
    message: str
    candidate_index: Optional[int] = None
    question_excerpt: str = ""
    repairable: bool = False


class GenerationPlanSummary(BaseModel):
    plan_index: int
    question_type: str
    bloom_level: str
    difficulty: Optional[str] = None
    model_provider: str = ""
    content_mode: str = "general"
    requested_count: int
    parsed_count: int = 0
    valid_count: int = 0
    retry_attempt_count: int = 0
    duplicate_count: int = 0
    exact_duplicate_count: int = 0
    near_duplicate_count: int = 0
    format_rejected_count: int = 0
    grounding_rejected_count: int = 0
    clarity_rejected_count: int = 0
    saved_count: int = 0
    skipped_count: int = 0
    warnings: List[str] = Field(default_factory=list)
    rejection_reasons: List[GenerationRejection] = Field(default_factory=list)


class QuestionGenerateResponse(BaseModel):
    status: str
    data: List[GeneratedQuestion]
    summary: List[GenerationPlanSummary] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class GenerationJobMetrics(BaseModel):
    server: dict[str, Any] = Field(default_factory=dict)
    client: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)


class GenerationJobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: GenerationJobStatus = GenerationJobStatus.QUEUED
    message: Optional[str] = None


class GenerationJobOutcome(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"


class GenerationJobStatusResponse(BaseModel):
    job_id: str
    status: GenerationJobStatus
    # Terminal result: PARTIAL means fewer questions were saved than requested.
    outcome: Optional[GenerationJobOutcome] = None
    # Why a PARTIAL job stopped early: MEMBERSHIP_REVOKED or SOURCE_UNAVAILABLE.
    stop_code: Optional[str] = None
    data: Optional[List[GeneratedQuestion]] = None
    summary: Optional[List[GenerationPlanSummary]] = None
    warnings: List[str] = Field(default_factory=list)
    metrics: Optional[GenerationJobMetrics] = None
    progress: Optional[dict[str, Any]] = None
    model: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
