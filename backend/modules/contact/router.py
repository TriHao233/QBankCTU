from fastapi import APIRouter, Depends, HTTPException, Query, status

from core.config import settings
from core.dependencies import (
    CurrentUser,
    require_authenticated,
)
from modules.contact.schemas import (
    ContactMessageCreate,
    ContactMessageResponse,
    ContactRequestCreate,
    ContactRequestUpdate,
    ContactRequestDetail,
    ContactRequestListResponse,
    ContactRequestResponse,
    ContactResponseSubmit,
    ContactStatus,
    ContactStatusUpdate,
)
from modules.contact.service import ContactConflictError, ContactService, get_contact_service

router = APIRouter(prefix=f"{settings.api_prefix}/contact", tags=["Contact"])


def _translate(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, ContactConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post(
    "/requests",
    response_model=ContactRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_request(
    payload: ContactRequestCreate,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.create_request(payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.get("/requests", response_model=ContactRequestListResponse)
def list_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: ContactStatus | None = Query(None, alias="status"),
    category: str | None = Query(None),
    search: str | None = Query(None),
    scope: str = Query("mine", pattern="^(mine|all|deleted)$"),
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    all_tickets = scope == "all" or (scope == "deleted" and current_user.role == "Admin")
    return service.list_for_user(
        current_user,
        page=page,
        page_size=page_size,
        status=status_filter.value if status_filter else None,
        category=category,
        search=search,
        all_tickets=all_tickets,
        deleted=scope == "deleted",
    )


@router.get("/requests/{request_id}", response_model=ContactRequestDetail)
def get_request(
    request_id: str,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.get_detail(request_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.patch("/requests/{request_id}", response_model=ContactRequestResponse)
def edit_request(
    request_id: str,
    payload: ContactRequestUpdate,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.update_request(request_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/requests/{request_id}/withdraw", response_model=ContactRequestResponse)
def withdraw_request(
    request_id: str,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.withdraw_request(request_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.delete("/requests/{request_id}", response_model=ContactRequestResponse)
def delete_request(
    request_id: str,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.delete_request(request_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/requests/{request_id}/restore", response_model=ContactRequestResponse)
def restore_request(
    request_id: str,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.restore_request(request_id, current_user)
    except Exception as exc:
        _translate(exc)


@router.post(
    "/requests/{request_id}/messages",
    response_model=ContactMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_message(
    request_id: str,
    payload: ContactMessageCreate,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.add_message(request_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.post("/requests/{request_id}/respond", response_model=ContactRequestResponse)
def respond_to_request(
    request_id: str,
    payload: ContactResponseSubmit,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.respond_to_request(request_id, payload, current_user)
    except Exception as exc:
        _translate(exc)


@router.patch(
    "/requests/{request_id}/status",
    response_model=ContactRequestResponse,
)
def update_status(
    request_id: str,
    payload: ContactStatusUpdate,
    current_user: CurrentUser = Depends(require_authenticated),
    service: ContactService = Depends(get_contact_service),
):
    try:
        return service.update_status(request_id, payload.status, current_user)
    except Exception as exc:
        _translate(exc)
