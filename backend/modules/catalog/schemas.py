import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, model_validator


class ChapterPayload(BaseModel):
    chapter_code: str = Field(..., min_length=1, max_length=40)
    chapter_name: str = Field(..., min_length=1, max_length=200)
    sequence_no: int = Field(1, ge=1)
    is_active: bool = True


class LearningOutcomePayload(BaseModel):
    clo_code: str = Field(..., min_length=1, max_length=40)
    description: str = Field(..., min_length=1, max_length=500)
    target_weight: float = Field(1.0, ge=0, le=1)
    is_active: bool = True


class SubjectPayload(BaseModel):
    subject_code: str = Field(..., min_length=1, max_length=40)
    subject_name: str = Field(..., min_length=1, max_length=200)
    description: str = ""
    is_active: bool = True

    @model_validator(mode="after")
    def normalize_subject_fields(self):
        self.subject_code = self.subject_code.strip()
        self.subject_name = self.subject_name.strip()
        self.description = self.description.strip()
        if not self.subject_code:
            raise ValueError("Mã môn học không được để trống")
        if not self.subject_name:
            raise ValueError("Tên môn học không được để trống")
        return self


class SubjectUpdatePayload(BaseModel):
    subject_code: str | None = Field(None, min_length=1, max_length=40)
    subject_name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    is_active: bool | None = None
    # Bắt buộc khi đổi mã học phần đang được dùng: bản lưu cũ của câu hỏi vẫn giữ mã cũ.
    confirm_code_change: bool = False


class ChapterUpdatePayload(BaseModel):
    chapter_code: str | None = Field(None, min_length=1, max_length=40)
    chapter_name: str | None = Field(None, min_length=1, max_length=200)
    sequence_no: int | None = Field(None, ge=1)
    is_active: bool | None = None


class LearningOutcomeUpdatePayload(BaseModel):
    clo_code: str | None = Field(None, min_length=1, max_length=40)
    description: str | None = Field(None, min_length=1, max_length=500)
    target_weight: float | None = Field(None, ge=0, le=1)
    is_active: bool | None = None


class SubjectResponse(BaseModel):
    id: str
    subject_code: str
    subject_name: str
    description: str = ""
    chapters: list[dict[str, Any]] = Field(default_factory=list)
    learning_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    is_active: bool
    usage_counts: dict[str, Any] = Field(default_factory=dict)
    owner_id: str | None = None
    owner_email: str = ""
    can_manage: bool = True
    approval_status: str = "APPROVED"
    approval_reason: str = ""
    approval_reviewed_at: datetime | None = None
    approval_reviewed_by_user_id: str | None = None


class SubjectRejectPayload(BaseModel):
    reason: str = Field(..., min_length=3, max_length=1000)

    @model_validator(mode="after")
    def normalize_reason(self):
        self.reason = self.reason.strip()
        if len(self.reason) < 3:
            raise ValueError("Cần nêu lý do từ chối")
        return self


class AiModelPayload(BaseModel):
    model_code: str = Field(..., min_length=1, max_length=80)
    model_name: str = Field(..., min_length=1, max_length=160)
    display_name: str = Field("", max_length=160)
    description: str = Field("", max_length=300)
    runtime: str = Field("OLLAMA", min_length=1, max_length=80)
    kind: str = Field("CHAT", min_length=1, max_length=80)
    revision: str = "local"
    capabilities: list[str] = Field(default_factory=list)
    priority: int = Field(10, ge=0)
    is_local: bool = True
    is_active: bool = True
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_runtime_config(self):
        self.model_code = self.model_code.strip().lower()
        self.model_name = self.model_name.strip()
        self.display_name = self.display_name.strip()
        self.description = self.description.strip()
        self.runtime = self.runtime.strip().upper()
        if self.runtime not in {"OLLAMA", "GEMINI"}:
            raise ValueError("Nền tảng model phải là Ollama hoặc Gemini")
        allowed_capabilities = {"QUESTION_GENERATION", "QUESTION_EVALUATION"}
        self.capabilities = list(dict.fromkeys(item.strip().upper() for item in self.capabilities if item.strip()))
        if not self.capabilities or any(item not in allowed_capabilities for item in self.capabilities):
            raise ValueError("Hãy chọn ít nhất một mục sử dụng model")
        numeric_rules = {
            "timeout_seconds": (1, 1800),
            "temperature": (0, 2),
            "num_predict": (1, 32768),
            "max_output_tokens": (1, 65536),
        }
        for key, (minimum, maximum) in numeric_rules.items():
            if key not in self.config:
                continue
            try:
                value = float(self.config[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Giá trị {key} không hợp lệ") from exc
            if not math.isfinite(value) or value < minimum or value > maximum:
                raise ValueError(f"Giá trị {key} phải từ {minimum} đến {maximum}")
        endpoint = str(self.config.get("endpoint") or "").strip()
        if endpoint and not endpoint.startswith(("http://", "https://")):
            raise ValueError("Địa chỉ Ollama phải bắt đầu bằng http:// hoặc https://")
        return self


class AiModelActivationPayload(BaseModel):
    model_code: str = Field(..., min_length=1, max_length=80)
    is_active: bool = True


class AiModelVersionActivationPayload(BaseModel):
    model_code: str = Field(..., min_length=1, max_length=80)
    version: int = Field(..., ge=1)


class AiModelHealthCheckPayload(BaseModel):
    model_code: str = Field(..., min_length=1, max_length=80)
    prompt: str = Field(
        'Return JSON only: {"ok": true}',
        min_length=1,
        max_length=1000,
    )
    timeout_seconds: float = Field(30, ge=1, le=120)


class PromptTemplatePayload(BaseModel):
    template_key: str = Field(..., min_length=1, max_length=120)
    kind: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=160)
    prompt_body: str = Field(..., min_length=1)
    create_new_version: bool = True
    is_active: bool = True


class PromptTemplateActivationPayload(BaseModel):
    template_key: str = Field(..., min_length=1, max_length=120)
    version: int = Field(..., ge=1)
    is_active: bool = True


class PromptTemplateTestPayload(BaseModel):
    template_key: str | None = Field(None, min_length=1, max_length=120)
    version: int | None = Field(None, ge=1)
    context: str = Field(
        "Stack uses LIFO, queue uses FIFO.",
        min_length=1,
        max_length=4000,
    )
    bloom_level: str = Field("2_hieu", min_length=1, max_length=80)
    question_type: str = Field("trac_nghiem", min_length=1, max_length=80)
    num_questions: int = Field(1, ge=1, le=10)
    instruction: str | None = Field(None, max_length=1000)


class EvaluationPolicyPayload(BaseModel):
    policy_name: str = Field(..., min_length=1, max_length=160)
    weights: dict[str, float]
    thresholds: dict[str, float]
    create_new_version: bool = True
    is_active: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self):
        keys = ("yellow_min", "pass_min", "green_min")
        values = [self.thresholds.get(key) for key in keys]
        if any(value is None or not math.isfinite(value) or value < 0 or value > 1 for value in values):
            raise ValueError("Ba ngưỡng đánh giá phải là số từ 0 đến 1")
        if not (values[0] <= values[1] <= values[2]):
            raise ValueError("Ngưỡng phải theo thứ tự: Cần xem lại ≤ Điểm đạt ≤ Đạt tốt")
        return self


class EvaluationPolicyActivationPayload(BaseModel):
    policy_name: str = Field(..., min_length=1, max_length=160)
    version: int = Field(..., ge=1)
    is_active: bool = True
