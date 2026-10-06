import re
from enum import Enum
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RoleEnum(str, Enum):
    ADMIN = "Admin"
    TEACHER = "Teacher"
    REVIEWER = "Reviewer"


_URL_PATTERN = re.compile(r"^(?:https?://[^\s]+|/api/[^\s]+)$", re.IGNORECASE)


class UserProfile(BaseModel):
    school: str = Field("", max_length=200)
    address: str = Field("", max_length=300)
    avatar: str = Field("", max_length=500)
    # Địa chỉ nhận email thông báo (rỗng = dùng users.email). Bật/tắt riêng cho email.
    notification_email: str = Field("", max_length=320)
    email_notifications_enabled: bool = True

    @field_validator("school", "address", mode="before")
    @classmethod
    def _trim(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("avatar", mode="before")
    @classmethod
    def _trim_avatar(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("notification_email", mode="before")
    @classmethod
    def _trim_notification_email(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("notification_email")
    @classmethod
    def _validate_notification_email(cls, value: str) -> str:
        if value and "@" not in value:
            raise ValueError("Email nhận thông báo không hợp lệ")
        return value

    @field_validator("avatar")
    @classmethod
    def _validate_avatar_url(cls, value: str) -> str:
        if value and not _URL_PATTERN.match(value):
            raise ValueError("Ảnh đại diện phải là URL http/https, đường dẫn /api/... hoặc để trống")
        return value


class UserCreateRequest(BaseModel):
    email: str = Field(
        ...,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    password: str = Field(..., min_length=6, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=120)
    role: RoleEnum = RoleEnum.TEACHER
    profile: UserProfile = Field(default_factory=UserProfile)
    permissions: list[str] = Field(default_factory=list, max_length=50)


class UserInviteRequest(BaseModel):
    email: str = Field(
        ...,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    display_name: str = Field(..., min_length=1, max_length=120)
    role: RoleEnum = RoleEnum.TEACHER
    profile: UserProfile = Field(default_factory=UserProfile)
    permissions: list[str] = Field(default_factory=list, max_length=50)


class UserImportRequest(BaseModel):
    users: list[UserInviteRequest] = Field(..., min_length=1, max_length=200)


class PublicRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(
        ...,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    password: str = Field(..., min_length=6, max_length=128)
    full_name: str = Field(..., min_length=1, max_length=120)


class UserSelfUpdateRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=120)
    profile: Optional[UserProfile] = None

    @field_validator("display_name", mode="before")
    @classmethod
    def _trim_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("Họ và tên không được để trống")
        return value


class UserAdminUpdateRequest(UserSelfUpdateRequest):
    role: Optional[RoleEnum] = None
    is_active: Optional[bool] = None
    permissions: Optional[list[str]] = Field(None, max_length=50)
    # Subjects a reviewer specialises in; empty means "can review any subject".
    review_subject_ids: Optional[list[str]] = Field(None, max_length=100)
    # Subjects a teacher teaches; SUBJECT-scoped shares are visible only within these.
    teaching_subject_ids: Optional[list[str]] = Field(None, max_length=100)


class PasswordResetResponse(BaseModel):
    user_id: str
    email: str
    reset_link: str


class UserInviteResponse(BaseModel):
    user: "UserResponse"
    reset_link: str


class UserImportItemResponse(BaseModel):
    email: str
    ok: bool
    user: Optional["UserResponse"] = None
    reset_link: Optional[str] = None
    error: Optional[str] = None


class UserImportResponse(BaseModel):
    items: list[UserImportItemResponse]
    created: int
    failed: int


class TeacherOptionResponse(BaseModel):
    id: str
    email: str
    display_name: str
    is_active: bool = True
    review_subject_ids: list[str] = Field(default_factory=list)


class TeacherOptionListResponse(BaseModel):
    items: list[TeacherOptionResponse]
    total: int


class GenerationPresetPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questionTypeId: str = Field(..., min_length=1, max_length=50)
    bloomId: str = Field(..., min_length=1, max_length=50)
    difficulty: Literal["de", "trung_binh", "kho"] = "trung_binh"
    count: int = Field(..., ge=1, le=10)
    contentMode: Literal["auto", "code", "general"] = "auto"


class GenerationPresetPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80)
    planItems: list[GenerationPresetPlanItem] = Field(..., min_length=1, max_length=10)
    instruction: str = Field("", max_length=1200)
    targetHeading: Optional[str] = Field(None, max_length=300)


class GenerationPresetResponse(GenerationPresetPayload):
    id: str
    createdAt: datetime
    updatedAt: datetime


class GenerationPresetListResponse(BaseModel):
    items: list[GenerationPresetResponse]


class TaskCalendarPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field("", max_length=1000)
    related_entity_type: Literal["question", "document", "none"] = "none"
    related_entity_id: Optional[str] = Field(None, max_length=50)
    priority: Literal["low", "medium", "high"] = "medium"
    due_date: Optional[datetime] = None

    @field_validator("title", mode="before")
    @classmethod
    def _trim_title(cls, value: str | None) -> str:
        return (value or "").strip()

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str) -> str:
        if not value:
            raise ValueError("Tiêu đề việc cần làm không được để trống")
        return value


class TaskCalendarUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=1000)
    priority: Optional[Literal["low", "medium", "high"]] = None
    due_date: Optional[datetime] = None
    status: Optional[Literal["todo", "done"]] = None

    @field_validator("title", mode="before")
    @classmethod
    def _trim_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip()

    @field_validator("title")
    @classmethod
    def _validate_title(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise ValueError("Tiêu đề việc cần làm không được để trống")
        return value


class CalendarEventItem(BaseModel):
    id: str
    title: str
    description: str = ""
    event_type: str
    source: Literal["system", "manual"]
    related_entity_type: Literal["question", "document", "review_queue", "none"] = "none"
    related_entity_id: Optional[str] = None
    status: Literal["todo", "done", "overdue"]
    priority: Literal["low", "medium", "high"] = "medium"
    date: datetime
    due_date: Optional[datetime] = None


class CalendarSummary(BaseModel):
    todo: int
    overdue: int
    done: int
    documents_waiting: int
    questions_need_revision: int
    questions_pending_review: int


class CalendarResponse(BaseModel):
    summary: CalendarSummary
    items: list[CalendarEventItem]


class UserResponse(BaseModel):
    id: str
    firebase_uid: str
    email: str
    display_name: str
    role: RoleEnum
    permissions: list[str] = Field(default_factory=list)
    # Khác biệt so với mặc định của vai trò, để giao diện hiện rõ ngoại lệ.
    permission_grants: list[str] = Field(default_factory=list)
    permission_revokes: list[str] = Field(default_factory=list)
    review_subject_ids: list[str] = Field(default_factory=list)
    teaching_subject_ids: list[str] = Field(default_factory=list)
    profile: UserProfile
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    page: int
    page_size: int
