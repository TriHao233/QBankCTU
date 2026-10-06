from fastapi import APIRouter, Depends, HTTPException, status

from core.config import settings
from core.dependencies import (
    CurrentUser,
    require_any_permission,
    require_permissions,
    require_teacher_reviewer_or_admin,
)
from modules.catalog.schemas import (
    AiModelActivationPayload,
    AiModelVersionActivationPayload,
    AiModelHealthCheckPayload,
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
    SubjectRejectPayload,
    SubjectResponse,
    SubjectUpdatePayload,
)
from modules.catalog.service import CatalogConflictError, CatalogService, get_catalog_service

router = APIRouter(prefix=f"{settings.api_prefix}/catalog", tags=["Catalog"])


def _translate(exc: Exception):
    if isinstance(exc, CatalogConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


# Giáo viên tự quản lý học phần của mình; quản trị viên catalog giám sát và quản trị dữ liệu.
require_subject_manager = require_any_permission(
    "admin.catalog",
    "catalog.subjects.manage_own",
)

require_subject_reader = require_any_permission(
    "admin.catalog",
    "catalog.subjects.manage_own",
    "questions.generate",
    "questions.manage_own",
    "questions.read_review_queue",
    "reviews.manage",
    "exams.manage_own",
)


@router.get("/overview")
def catalog_overview(
    user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: CatalogService = Depends(get_catalog_service),
):
    return service.overview(user)


@router.get("/runtime-config")
def runtime_config(
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return service.runtime_config()


@router.get("/subjects", response_model=list[SubjectResponse])
def list_subjects(
    user: CurrentUser = Depends(require_subject_reader),
    service: CatalogService = Depends(get_catalog_service),
):
    return service.list_subjects(user)


@router.post("/subjects", response_model=SubjectResponse, status_code=status.HTTP_201_CREATED)
def create_subject(
    payload: SubjectPayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.create_subject(payload, user)
    except Exception as exc:
        _translate(exc)


@router.patch("/subjects/{subject_id}", response_model=SubjectResponse)
def update_subject(
    subject_id: str,
    payload: SubjectUpdatePayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.update_subject(subject_id, payload, user)
    except Exception as exc:
        _translate(exc)


@router.post("/subjects/{subject_id}/approve", response_model=SubjectResponse)
def approve_subject(
    subject_id: str,
    user: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.approve_subject(subject_id, user)
    except Exception as exc:
        _translate(exc)


@router.post("/subjects/{subject_id}/reject", response_model=SubjectResponse)
def reject_subject(
    subject_id: str,
    payload: SubjectRejectPayload,
    user: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.reject_subject(subject_id, payload.reason, user)
    except Exception as exc:
        _translate(exc)


@router.post("/subjects/{subject_id}/resubmit", response_model=SubjectResponse)
def resubmit_subject(
    subject_id: str,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.resubmit_subject(subject_id, user)
    except Exception as exc:
        _translate(exc)


@router.delete("/subjects/{subject_id}", response_model=SubjectResponse)
def deactivate_subject(
    subject_id: str,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    """Xoá mềm: học phần bị ẩn khỏi danh sách chọn nhưng dữ liệu liên quan vẫn giữ nguyên."""
    try:
        return service.deactivate_subject(subject_id, user)
    except Exception as exc:
        _translate(exc)


@router.post("/subjects/{subject_id}/chapters", response_model=SubjectResponse, status_code=status.HTTP_201_CREATED)
def add_chapter(
    subject_id: str,
    payload: ChapterPayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.add_chapter(subject_id, payload, user)
    except Exception as exc:
        _translate(exc)


@router.patch("/subjects/{subject_id}/chapters/{chapter_id}", response_model=SubjectResponse)
def update_chapter(
    subject_id: str,
    chapter_id: str,
    payload: ChapterUpdatePayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.update_chapter(subject_id, chapter_id, payload, user)
    except Exception as exc:
        _translate(exc)


@router.post("/subjects/{subject_id}/learning-outcomes", response_model=SubjectResponse, status_code=status.HTTP_201_CREATED)
def add_learning_outcome(
    subject_id: str,
    payload: LearningOutcomePayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.add_learning_outcome(subject_id, payload, user)
    except Exception as exc:
        _translate(exc)


@router.patch("/subjects/{subject_id}/learning-outcomes/{clo_id}", response_model=SubjectResponse)
def update_learning_outcome(
    subject_id: str,
    clo_id: str,
    payload: LearningOutcomeUpdatePayload,
    user: CurrentUser = Depends(require_subject_manager),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.update_learning_outcome(subject_id, clo_id, payload, user)
    except Exception as exc:
        _translate(exc)


@router.get("/ai-models")
def list_ai_models(
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return {"items": service.list_ai_models()}


@router.get("/ai-models/available")
def list_available_ai_models(
    capability: str = "QUESTION_GENERATION",
    _user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.available_ai_models(capability)
    except Exception as exc:
        _translate(exc)


@router.post("/ai-models", status_code=status.HTTP_201_CREATED)
def create_ai_model(
    payload: AiModelPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.save_ai_model(payload, create=True, viewer=admin)
    except Exception as exc:
        _translate(exc)


@router.put("/ai-models/{model_code}")
def update_ai_model(
    model_code: str,
    payload: AiModelPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    if model_code.strip().lower() != payload.model_code:
        raise HTTPException(status_code=400, detail="Mã mô hình không khớp")
    try:
        return service.save_ai_model(payload, create=False, viewer=admin)
    except Exception as exc:
        _translate(exc)


@router.post("/ai-models/active")
def set_ai_model_active(
    payload: AiModelActivationPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.set_ai_model_active(payload, viewer=admin)
    except Exception as exc:
        _translate(exc)


@router.get("/ai-models/{model_code}/versions")
def list_ai_model_versions(
    model_code: str,
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return {"items": service.list_ai_model_versions(model_code)}
    except Exception as exc:
        _translate(exc)


@router.post("/ai-models/version/active")
def activate_ai_model_version(
    payload: AiModelVersionActivationPayload,
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.activate_ai_model_version(payload, actor_id=_admin.id)
    except Exception as exc:
        _translate(exc)


@router.post("/ai-models/health-check")
async def check_ai_model_health(
    payload: AiModelHealthCheckPayload,
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return await service.check_ai_model_health(payload)


@router.get("/prompt-templates")
def list_prompt_templates(
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return {"items": service.list_prompt_templates()}


@router.post("/prompt-templates", status_code=status.HTTP_201_CREATED)
def save_prompt_template(
    payload: PromptTemplatePayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return service.save_prompt_template(payload, viewer=admin)


@router.post("/prompt-templates/active")
def activate_prompt_template(
    payload: PromptTemplateActivationPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.activate_prompt_template(payload, viewer=admin)
    except Exception as exc:
        _translate(exc)


@router.post("/prompt-templates/test-build")
def test_prompt_template(
    payload: PromptTemplateTestPayload,
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.test_prompt_template(payload)
    except Exception as exc:
        _translate(exc)


@router.get("/evaluation-policies")
def list_evaluation_policies(
    _admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return {"items": service.list_evaluation_policies()}


@router.post("/evaluation-policies", status_code=status.HTTP_201_CREATED)
def save_evaluation_policy(
    payload: EvaluationPolicyPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    return service.save_evaluation_policy(payload, viewer=admin)


@router.post("/evaluation-policies/active")
def activate_evaluation_policy(
    payload: EvaluationPolicyActivationPayload,
    admin: CurrentUser = Depends(require_permissions("admin.catalog")),
    service: CatalogService = Depends(get_catalog_service),
):
    try:
        return service.activate_evaluation_policy(payload, viewer=admin)
    except Exception as exc:
        _translate(exc)
