import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from core.config import settings
from core.dependencies import CurrentUser, require_exam_manager, require_roles
from modules.exams.pdf_service import (
    PREVIEW_WATERMARK,
    VALID_EXPORT_TYPES,
    render_exam_docx,
    render_exam_pdf,
)
from modules.exams.schemas import (
    MAX_SEED,
    AddQuestionsManualRequest,
    ExamCreateRequest,
    ExamQuestionPoolResponse,
    ExamListResponse,
    ExamMatrixRequest,
    ExamOwnerTransferRequest,
    ExamPreviewResponse,
    ExamReopenRequest,
    ExamResponse,
    ExamSharingRequest,
    ExamStatusUpdateRequest,
    ExamUpdateRequest,
    ExamVariantCreateRequest,
    ExamVariantResponse,
    MatrixCellAvailability,
)
from modules.exams.service import (
    ExamService,
    ExamVariantService,
    get_exam_service,
    get_exam_variant_service,
)

router = APIRouter(prefix=f"{settings.api_prefix}/exams", tags=["Exams"])


def _translate(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("", response_model=ExamResponse, status_code=status.HTTP_201_CREATED)
def create_exam(
    payload: ExamCreateRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.create_exam(payload, current_user.id, actor=current_user)
    except Exception as exc:
        _translate(exc)


@router.get("", response_model=ExamListResponse)
def list_exams(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    return service.list_exams(page, page_size, current_user)


@router.get("/{exam_id}", response_model=ExamResponse)
def get_exam(
    exam_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.get_exam(exam_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.post(
    "/{exam_id}/duplicate",
    response_model=ExamResponse,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_exam(
    exam_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.duplicate_exam(exam_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.patch("/{exam_id}", response_model=ExamResponse)
def update_exam(
    exam_id: str,
    payload: ExamUpdateRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.update_exam(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/{exam_id}/status", response_model=ExamResponse)
def update_exam_status(
    exam_id: str,
    payload: ExamStatusUpdateRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.update_status(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/{exam_id}/reopen", response_model=ExamResponse)
def reopen_exam(
    exam_id: str,
    payload: ExamReopenRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.reopen_exam(exam_id, payload.reason, current_user)
    except Exception as exc:
        _translate(exc)


@router.patch("/{exam_id}/sharing", response_model=ExamResponse)
def update_exam_sharing(
    exam_id: str,
    payload: ExamSharingRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.update_sharing(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/{exam_id}/transfer-owner", response_model=ExamResponse)
def transfer_exam_owner(
    exam_id: str,
    payload: ExamOwnerTransferRequest,
    current_user: CurrentUser = Depends(require_roles("Admin")),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.transfer_owner(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.delete("/{exam_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exam(
    exam_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        service.delete_exam(exam_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/question-pool", response_model=ExamQuestionPoolResponse)
def question_pool(
    exam_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = Query(None),
    question_type: str | None = Query(None),
    bloom_level: int | None = Query(None, ge=1, le=6),
    chapter_id: str | None = Query(None),
    difficulty: str | None = Query(None),
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.question_pool(
            exam_id,
            current_user,
            page=page,
            page_size=page_size,
            search=search,
            question_type=question_type,
            bloom_level=bloom_level,
            chapter_id=chapter_id,
            difficulty=difficulty,
        )
    except Exception as exc:
        _translate(exc)


@router.put("/{exam_id}/matrix", response_model=ExamResponse)
def save_matrix(
    exam_id: str,
    payload: ExamMatrixRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.save_matrix(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/matrix/availability", response_model=list[MatrixCellAvailability])
def matrix_availability(
    exam_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.matrix_availability(exam_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/{exam_id}/questions/auto-generate", response_model=ExamResponse)
def auto_generate_questions(
    exam_id: str,
    seed: int | None = Query(None, ge=0, le=MAX_SEED),
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.auto_generate_pool(exam_id, current_user, seed)
    except Exception as exc:
        _translate(exc)


@router.post("/{exam_id}/questions", response_model=ExamResponse)
def add_questions_manual(
    exam_id: str,
    payload: AddQuestionsManualRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.add_questions_manual(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.delete("/{exam_id}/questions/{question_id}", response_model=ExamResponse)
def remove_question(
    exam_id: str,
    question_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamService = Depends(get_exam_service),
):
    try:
        return service.remove_question(exam_id, question_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.post(
    "/{exam_id}/variants",
    response_model=ExamVariantResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_variant(
    exam_id: str,
    payload: ExamVariantCreateRequest,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    try:
        return service.create_variant(exam_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/variants", response_model=list[ExamVariantResponse])
def list_variants(
    exam_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    try:
        return service.list_variants(exam_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/variants/{variant_id}", response_model=ExamVariantResponse)
def get_variant(
    exam_id: str,
    variant_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    try:
        return service.get_variant(exam_id, variant_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.delete(
    "/{exam_id}/variants/{variant_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_variant(
    exam_id: str,
    variant_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    try:
        service.delete_variant(exam_id, variant_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/variants/{variant_id}/preview", response_model=ExamPreviewResponse)
def preview_variant(
    exam_id: str,
    variant_id: str,
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    try:
        return service.build_preview(exam_id, variant_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/{exam_id}/variants/{variant_id}/export/pdf")
async def export_variant_pdf(
    exam_id: str,
    variant_id: str,
    export_type: str = Query("de", alias="type"),
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    if export_type not in VALID_EXPORT_TYPES:
        raise HTTPException(status_code=400, detail="type phải là de, dapan hoặc de_dapan")
    try:
        exam, variant, official = await asyncio.to_thread(
            service.export_access, exam_id, variant_id, export_type, current_user,
        )
    except Exception as exc:
        _translate(exc)
    if not variant["questions"]:
        raise HTTPException(status_code=400, detail="Mã đề chưa có câu hỏi, không thể xuất PDF")
    pdf_bytes = await render_exam_pdf(
        exam["header"], variant["exam_code"], variant["questions"], export_type,
        None if official else PREVIEW_WATERMARK,
    )
    if official:
        try:
            await asyncio.to_thread(
                service.record_official_export, exam, variant,
                export_type=export_type, file_format="pdf", current_user=current_user,
            )
        except Exception as exc:
            _translate(exc)
    filename = f"{variant['exam_code']}_{export_type}{'' if official else '_xem_truoc'}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{exam_id}/variants/{variant_id}/export/docx")
def export_variant_docx(
    exam_id: str,
    variant_id: str,
    export_type: str = Query("de", alias="type"),
    current_user: CurrentUser = Depends(require_exam_manager),
    service: ExamVariantService = Depends(get_exam_variant_service),
):
    if export_type not in VALID_EXPORT_TYPES:
        raise HTTPException(status_code=400, detail="type phải là de, dapan hoặc de_dapan")
    try:
        exam, variant, official = service.export_access(exam_id, variant_id, export_type, current_user)
    except Exception as exc:
        _translate(exc)
    if not variant["questions"]:
        raise HTTPException(status_code=400, detail="Mã đề chưa có câu hỏi, không thể xuất DOCX")
    docx_bytes = render_exam_docx(
        exam["header"], variant["exam_code"], variant["questions"], export_type,
        None if official else PREVIEW_WATERMARK,
    )
    if official:
        try:
            service.record_official_export(
                exam, variant, export_type=export_type, file_format="docx", current_user=current_user,
            )
        except Exception as exc:
            _translate(exc)
    filename = f"{variant['exam_code']}_{export_type}{'' if official else '_xem_truoc'}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
