from enum import Enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class QuestionOrigin(str, Enum):
    AI = "AI"
    MANUAL = "MANUAL"
    IMPORT = "IMPORT"


class QuestionDifficulty(str, Enum):
    DE = "de"
    TRUNG_BINH = "trung_binh"
    KHO = "kho"


class QuestionCreateRequest(BaseModel):
    content: str = Field(..., min_length=1)
    question_type: str = Field("trac_nghiem", min_length=1)
    bloom_level: int | None = Field(None, ge=1, le=6)
    difficulty: QuestionDifficulty | None = None
    question_data: dict[str, Any] = Field(default_factory=dict)
    subject_id: str | None = None
    chapter_id: str | None = None
    document_id: str | None = None
    source_context: str | None = Field(None, min_length=1, max_length=4000)
    chunk_id: str | None = None
    source_chunk_ids: list[str] = Field(default_factory=list)
    clo_ids: list[str] = Field(default_factory=list)


class QuestionUpdateRequest(BaseModel):
    expected_version: int = Field(..., ge=1)
    content: str | None = Field(None, min_length=1)
    question_type: str | None = Field(None, min_length=1)
    bloom_level: int | None = Field(None, ge=1, le=6)
    difficulty: QuestionDifficulty | None = None
    question_data: dict[str, Any] | None = None
    subject_id: str | None = None
    chapter_id: str | None = None
    chunk_id: str | None = None
    source_chunk_ids: list[str] | None = None
    clo_ids: list[str] | None = None
    change_note: str = Field("Question edited", min_length=1, max_length=500)


class QuestionSubmitRequest(BaseModel):
    response_note: str = Field("", max_length=2000)


class QuestionSharingRequest(BaseModel):
    shared_with_user_ids: list[str] = Field(default_factory=list, max_length=100)
    # Edit-level sharing: may change content and create versions, never archive or reshare.
    shared_edit_user_ids: list[str] = Field(default_factory=list, max_length=100)
    shared_scope: str = Field("PRIVATE", pattern="^(PRIVATE|SUBJECT)$")


class QuestionOwnerTransferRequest(BaseModel):
    new_owner_user_id: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=5, max_length=1000)


class QuestionResponse(BaseModel):
    id: str
    question_code: str
    author_user_ids: list[str] = Field(default_factory=list)
    current_version: int
    current_version_id: str
    approved_version_id: str | None
    document_id: str | None
    subject_id: str | None = None
    subject: dict[str, Any] = Field(default_factory=dict)
    review_submission: dict[str, Any] = Field(default_factory=dict)
    submitted_by_user_id: str | None = None
    submitted_at: datetime | None = None
    lifecycle_status: str
    evaluation_status: str
    review_status: str
    publication_status: str
    content: str
    question_data: dict[str, Any]
    classification: dict[str, Any]
    clos: list[Any]
    sources: list[dict[str, Any]]
    content_hash: str
    quality_summary: dict[str, Any]
    review_assignment: dict[str, Any] = Field(default_factory=dict)
    shared_with_user_ids: list[str] = Field(default_factory=list)
    shared_edit_user_ids: list[str] = Field(default_factory=list)
    shared_scope: str = "PRIVATE"
    secondary_review: dict[str, Any] = Field(default_factory=dict)
    latest_review_id: str | None
    created_at: datetime
    updated_at: datetime


class QuestionVersionResponse(BaseModel):
    id: str
    version: int
    origin: QuestionOrigin
    generation_run_id: str | None
    document_id: str | None
    created_by_user_id: str | None
    generated_by_model_id: str | None
    classification: dict[str, Any]
    clos: list[Any]
    content: str
    question_data: dict[str, Any]
    sources: list[dict[str, Any]]
    keywords: list[Any]
    content_hash: str
    change_note: str
    created_at: datetime


class QuestionSourcePage(BaseModel):
    page_number: int
    text: str = ""
    formula_blocks: list[Any] = Field(default_factory=list)


class QuestionSourceDocument(BaseModel):
    id: str
    title: str
    original_filename: str
    page_count: int | None = None
    current_ocr_job_id: str | None = None
    current_chunk_set_id: str | None = None
    pdf_available: bool = False
    pdf_url: str | None = None


class QuestionSourceItem(BaseModel):
    citation_order: int
    source_type: str
    is_primary: bool
    chunk_id: str | None = None
    chunk_no: int | None = None
    chunk_set_id: str | None = None
    current_chunk_set_id: str | None = None
    is_current_chunk_set: bool | None = None
    chunk_content_hash: str | None = None
    current_content_hash: str | None = None
    content_hash_matches: bool | None = None
    page_range: dict[str, Any] = Field(default_factory=dict)
    heading: dict[str, Any] = Field(default_factory=dict)
    content_type: str | None = None
    semantic_type: str | None = None
    information_density: float | int | None = None
    context_excerpt: str = ""
    chunk_text: str = ""
    pages: list[QuestionSourcePage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class QuestionSourceViewerResponse(BaseModel):
    question_id: str
    question_code: str
    version_id: str
    version: int
    document: QuestionSourceDocument | None = None
    items: list[QuestionSourceItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class QuestionListResponse(BaseModel):
    items: list[QuestionResponse]
    total: int
    page: int
    page_size: int
    status_counts: dict[str, int] = Field(default_factory=dict)
