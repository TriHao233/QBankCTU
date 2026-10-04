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


QuestionDifficulty = Literal["de", "trung_binh", "kho"]


class QuestionPlanItem(BaseModel):
    question_type: QuestionType
    bloom_level: Optional[BloomLevel] = None
    difficulty: Optional[QuestionDifficulty] = None
    num_questions: int = Field(default=1, ge=1, le=7)
    content_mode: Literal["auto", "code", "general"] = "auto"


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
    num_questions: int = Field(default=1, ge=1, le=7)
    model_provider: str = Field(default_factory=lambda: settings.model_provider, min_length=1, max_length=160)
    code_model_provider: str = Field(
        default_factory=lambda: settings.code_generation_model_provider,
        min_length=1,
        max_length=160,
    )
    question_plan: Optional[List[QuestionPlanItem]] = Field(
        None,
        min_length=1,
        max_length=7,
        description="Cơ cấu số lượng câu hỏi theo từng dạng",
    )
    client_telemetry: Optional[GenerationClientTelemetry] = Field(
        None,
        description="Timing đo ở frontend trước khi enqueue job sinh câu hỏi",
    )
    clo_ids: Optional[List[str]] = Field(
        None,
        max_length=20,
        description="Danh sách CLO do giảng viên chọn để giới hạn sinh câu hỏi",
    )

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("clo_ids")
    @classmethod
    def normalize_clo_ids(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        if not value:
            return None
        normalized = []
        seen = set()
        for item in value:
            clo_id = str(item or "").strip()
            if not clo_id or clo_id in seen:
                continue
            seen.add(clo_id)
            normalized.append(clo_id)
        return normalized or None

    @model_validator(mode="after")
    def validate_total_questions(self):
        total = sum(item.num_questions for item in self.question_plan) if self.question_plan else self.num_questions
        if total < 1 or total > 7:
            raise ValueError("Tổng số câu hỏi phải từ 1 đến 7.")
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


class GenerationJobStatusResponse(BaseModel):
    job_id: str
    status: GenerationJobStatus
    data: Optional[List[GeneratedQuestion]] = None
    summary: Optional[List[GenerationPlanSummary]] = None
    metrics: Optional[GenerationJobMetrics] = None
    progress: Optional[dict[str, Any]] = None
    model: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
