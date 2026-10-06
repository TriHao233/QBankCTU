from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status

from core.config import settings
from core.dependencies import CurrentUser, require_bank_sharing, require_document_manager
from modules.documents.repository import DocumentBusyError
from modules.documents.schemas import (
    DocumentCreateRequest,
    DocumentJobActionResponse,
    DocumentJobListResponse,
    DocumentListResponse,
    DocumentPageListResponse,
    DocumentPageResponse,
    DocumentPageUpdateRequest,
    DocumentResponse,
    DocumentSharingRequest,
    DocumentStatus,
    DocumentUpdateRequest,
)
from modules.documents.service import DocumentService, get_document_service
from modules.ocr.ocr import queue_pdf_ocr_upload

router = APIRouter(prefix=f"{settings.api_prefix}/documents", tags=["Documents"])


@router.get("", response_model=DocumentListResponse)
def list_documents(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    document_status: DocumentStatus | None = Query(None, alias="status"),
    search: str | None = None,
    subject_id: str | None = None,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    return service.list(
        page,
        page_size,
        document_status.value if document_status else None,
        search,
        current_user,
        subject_id,
    )


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
def create_document(
    payload: DocumentCreateRequest,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        return service.create(payload, current_user.id, actor=current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/upload", status_code=status.HTTP_202_ACCEPTED)
async def upload_document_source(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    subject_id: str | None = Form(None),
    chapter_id: str | None = Form(None),
    current_user: CurrentUser = Depends(require_document_manager),
):
    return await queue_pdf_ocr_upload(
        background_tasks,
        file,
        current_user,
        subject_id=subject_id,
        chapter_id=chapter_id,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: str,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        document = service.get(document_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    return document


@router.get("/{document_id}/jobs", response_model=DocumentJobListResponse)
def list_document_jobs(
    document_id: str,
    limit: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        result = service.list_jobs(document_id, current_user, limit=limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    return result


@router.get("/{document_id}/pages", response_model=DocumentPageListResponse)
def list_document_pages(
    document_id: str,
    limit: int = Query(100, ge=1, le=300),
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        result = service.list_pages(document_id, current_user, limit=limit)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    return result


@router.patch("/{document_id}/pages/{page_id}", response_model=DocumentPageResponse)
def update_document_page(
    document_id: str,
    page_id: str,
    payload: DocumentPageUpdateRequest,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        page = service.update_page(document_id, page_id, payload, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not page:
        raise HTTPException(status_code=404, detail="Không tìm thấy OCR page")
    return page


@router.post(
    "/{document_id}/jobs/{job_id}/retry",
    response_model=DocumentJobActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_document_job(
    document_id: str,
    job_id: str,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        return service.retry_job(document_id, job_id, background_tasks, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{document_id}/jobs/{job_id}/cancel", response_model=DocumentJobActionResponse)
def cancel_document_job(
    document_id: str,
    job_id: str,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        return service.cancel_job(document_id, job_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/{document_id}/reindex",
    response_model=DocumentJobActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reindex_document(
    document_id: str,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        return service.reindex(document_id, background_tasks, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/{document_id}", response_model=DocumentResponse)
def update_document(
    document_id: str,
    payload: DocumentUpdateRequest,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        document = service.update(document_id, payload, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    return document


@router.patch("/{document_id}/sharing", response_model=DocumentResponse)
def update_document_sharing(
    document_id: str,
    payload: DocumentSharingRequest,
    current_user: CurrentUser = Depends(require_bank_sharing),
    service: DocumentService = Depends(get_document_service),
):
    try:
        document = service.update_sharing(document_id, payload, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(
    document_id: str,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        deleted = service.archive(document_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")


@router.post("/{document_id}/restore", response_model=DocumentResponse)
def restore_document(
    document_id: str,
    current_user: CurrentUser = Depends(require_document_manager),
    service: DocumentService = Depends(get_document_service),
):
    try:
        document = service.restore(document_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not document:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu đã lưu trữ")
    return document
