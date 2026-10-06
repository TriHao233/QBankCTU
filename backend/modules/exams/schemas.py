from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

# Mapping từ 4 mức nhận thức VN sang bloom_level (1-6) đã có sẵn trên câu hỏi.
COGNITIVE_LEVEL_TO_BLOOM = {
    "nhan_biet": 1,
    "thong_hieu": 2,
    "van_dung": 3,
    "van_dung_cao": 4,
}

MAX_VARIANTS_PER_EXAM = 4


class CognitiveLevel(str, Enum):
    NHAN_BIET = "nhan_biet"
    THONG_HIEU = "thong_hieu"
    VAN_DUNG = "van_dung"
    VAN_DUNG_CAO = "van_dung_cao"


class QuestionDifficulty(str, Enum):
    DE = "de"
    TRUNG_BINH = "trung_binh"
    KHO = "kho"


class ExamStatus(str, Enum):
    DRAFT = "DRAFT"
    READY = "READY"
    FINALIZED = "FINALIZED"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


MAX_SEED = 2**31 - 1


class ExamHeaderConfig(BaseModel):
    school_name: str = Field("", max_length=300)
    faculty_name: str = Field("", max_length=300)
    exam_name: str = Field("", max_length=300)
    subject_name: str = Field("", max_length=300)
    duration_minutes: int = Field(60, ge=1, le=600)
    class_name: str | None = None
    room: str | None = None
    exam_date: str | None = None


class MatrixCell(BaseModel):
    chapter_id: str | None = None
    cognitive_level: CognitiveLevel
    difficulty: QuestionDifficulty
    count: int = Field(..., ge=1)


class ExamMatrixRequest(BaseModel):
    cells: list[MatrixCell] = Field(default_factory=list)


class MatrixCellAvailability(BaseModel):
    chapter_id: str | None
    cognitive_level: CognitiveLevel
    difficulty: QuestionDifficulty
    requested: int
    available: int
    sufficient: bool


class ExamCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=300)
    exam_title: str = Field(..., min_length=1, max_length=300)
    subject_id: str = Field(..., min_length=1)
    question_count: int = Field(..., ge=1, le=200)
    header: ExamHeaderConfig = Field(default_factory=ExamHeaderConfig)


class ExamUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=300)
    exam_title: str | None = Field(None, min_length=1, max_length=300)
    question_count: int | None = Field(None, ge=1, le=200)
    header: ExamHeaderConfig | None = None


class ExamStatusUpdateRequest(BaseModel):
    status: ExamStatus


class ExamReopenRequest(BaseModel):
    reason: str = Field(..., min_length=5, max_length=1000)


class ExamSharingRequest(BaseModel):
    shared_with_user_ids: list[str] = Field(default_factory=list, max_length=100)


class ExamOwnerTransferRequest(BaseModel):
    new_owner_user_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=5, max_length=1000)


class AddQuestionsManualRequest(BaseModel):
    question_ids: list[str] = Field(..., min_length=1)


class ExamQuestionRef(BaseModel):
    question_id: str
    version_id: str
    content_snapshot: dict[str, Any]


class ExamResponse(BaseModel):
    id: str
    name: str
    exam_title: str
    subject_id: str
    question_count: int
    header: ExamHeaderConfig
    matrix: list[MatrixCell] = Field(default_factory=list)
    questions: list[ExamQuestionRef] = Field(default_factory=list)
    status: str
    variant_count: int = 0
    delivery_mode: str = "paper"
    time_limit_seconds: int | None = None
    scoring_config: dict[str, Any] | None = None
    lms_export_status: str = "not_exported"
    created_by_user_id: str | None = None
    shared_with_user_ids: list[str] = Field(default_factory=list)
    can_edit: bool = True
    can_export_official: bool = True
    selection_seed: int | None = None
    item_flags: list[dict[str, Any]] = Field(default_factory=list)
    reopen_history: list[dict[str, Any]] = Field(default_factory=list)
    published_at: datetime | None = None
    official_exported_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ExamListResponse(BaseModel):
    items: list[ExamResponse]
    total: int
    page: int
    page_size: int


class ExamQuestionPoolResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class ExamVariantCreateRequest(BaseModel):
    exam_code: str = Field(..., min_length=1, max_length=40)
    shuffle: bool = True
    seed: int | None = Field(None, ge=0, le=MAX_SEED)


class ExamVariantQuestionEntry(BaseModel):
    order: int
    question_id: str
    content_snapshot: dict[str, Any]
    option_order: list[int] | None = None


class ExamVariantResponse(BaseModel):
    id: str
    exam_id: str
    exam_code: str
    questions: list[ExamVariantQuestionEntry]
    answer_key: dict[str, Any]
    shuffle_seed: int | None = None
    created_at: datetime


class ExamPreviewQuestion(BaseModel):
    number: int
    content: str
    question_type: str
    options: list[dict[str, Any]]


class ExamPreviewResponse(BaseModel):
    header: ExamHeaderConfig
    exam_code: str
    questions: list[ExamPreviewQuestion]
