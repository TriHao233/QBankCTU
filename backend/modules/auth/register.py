import logging

from fastapi import APIRouter, HTTPException, status
from firebase_admin import auth

from core.config import settings
from modules.users.repository import UnverifiedEmailLinkError
from modules.users.schemas import PublicRegisterRequest, UserResponse
from modules.users.service import PublicRegistrationDisabledError, get_user_service

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/registration-status")
def registration_status():
    return {"enabled": settings.allow_public_registration}


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(payload: PublicRegisterRequest):
    """Public Firebase registration; application role is always Teacher."""
    try:
        return get_user_service().register_teacher(payload)
    except PublicRegistrationDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (auth.EmailAlreadyExistsError, UnverifiedEmailLinkError) as exc:
        raise HTTPException(status_code=409, detail="Email này đã được sử dụng") from exc
    except Exception as exc:
        logger.exception("Public registration failed for %s", payload.email)
        raise HTTPException(status_code=500, detail="Không thể đăng ký tài khoản") from exc
