import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from firebase_admin import auth
from pydantic import BaseModel
from pymongo import ReturnDocument

from core.bootstrap import SCHEMA_VERSION
from core.audit import record_audit_event
from core.config import settings
from core.database import get_rag_db
from core.demo_auth import create_demo_session_token
from core.dependencies import PENDING_APPROVAL_DETAIL, CurrentUser, get_current_user
from core.firebase_auth import verify_firebase_id_token
from modules.auth.session_repository import get_firebase_session_repository
from modules.users.repository import serialize_user
from modules.users.service import get_user_service
from core.limiter import limiter

router = APIRouter()
logger = logging.getLogger(__name__)

# Tolerate small clock drift between this host and Google's servers when
# checking token iat/exp/auth_time, to avoid intermittent# Keep in sync with core/dependencies.py's TOKEN_CLOCK_SKEW_SECONDS.
TOKEN_CLOCK_SKEW_SECONDS = 60


class TokenRequest(BaseModel):
    id_token: str


class DemoLoginRequest(BaseModel):
    username: str
    password: str


DEMO_USERS = {
    "admin": {
        "password": settings.demo_admin_password,
        "email": settings.demo_admin_email,
        "display_name": "Admin Demo",
        "role": "Admin",
    },
    "reviewer": {
        "password": settings.demo_reviewer_password,
        "email": settings.demo_reviewer_email,
        "display_name": "Reviewer Demo",
        "role": "Reviewer",
    },
    "teacher": {
        "password": settings.demo_teacher_password,
        "email": settings.demo_teacher_email,
        "display_name": "Teacher Demo",
        "role": "Teacher",
    },
}


def _ensure_demo_firebase_user(email: str, display_name: str):
    try:
        firebase_user = auth.get_user_by_email(email)
        if getattr(firebase_user, "disabled", False):
            raise HTTPException(status_code=403, detail="Tài khoản demo đã bị khóa")
        return auth.update_user(
            firebase_user.uid,
            display_name=display_name,
            email_verified=True,
        )
    except auth.UserNotFoundError:
        return auth.create_user(
            email=email,
            display_name=display_name,
            disabled=False,
            email_verified=True,
        )


def _ensure_demo_app_user(firebase_uid: str, demo_user: dict) -> dict:
    if settings.user_store == "postgres":
        repository = get_user_service().repository
        user = repository.sync_identity({
            "uid": firebase_uid,
            "email": demo_user["email"],
            "name": demo_user["display_name"],
        })
        return repository.update(user["_id"], {
            "role": demo_user["role"],
            "profile": {"school": "", "address": "", "avatar": ""},
        })
    now = datetime.now(timezone.utc)
    return get_rag_db().users.find_one_and_update(
        {"firebase_uid": firebase_uid},
        {
            "$set": {
                "schema_version": SCHEMA_VERSION,
                "firebase_uid": firebase_uid,
                "email": demo_user["email"].lower(),
                "display_name": demo_user["display_name"],
                "role": demo_user["role"],
                "profile": {"school": "", "address": "", "avatar": ""},
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now, "is_active": True},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


if settings.demo_mode:
    @router.post("/demo-login")
    @limiter.limit("5/minute")
    def demo_login(request: Request, body: DemoLoginRequest):
        username = body.username.strip().lower()
        demo_user = DEMO_USERS.get(username)
        if not demo_user or not demo_user["password"]:
            raise HTTPException(status_code=503, detail="Demo login chưa được cấu hình")
        if body.password != demo_user["password"]:
            raise HTTPException(status_code=401, detail="Tài khoản demo không chính xác")
        firebase_user = _ensure_demo_firebase_user(
            demo_user["email"],
            demo_user["display_name"],
        )
        app_user_record = _ensure_demo_app_user(firebase_user.uid, demo_user)
        if not app_user_record.get("is_active", True):
            raise HTTPException(status_code=403, detail="Tài khoản đã bị khóa")
        app_user = serialize_user(app_user_record)
        demo_token = create_demo_session_token(app_user)
        get_firebase_session_repository().upsert(firebase_user.uid, demo_token)
        record_audit_event(
            action="auth.demo_login",
            entity_type="user",
            entity_id=app_user_record["_id"],
            actor_user_id=app_user_record["_id"],
            actor_role=app_user_record["role"],
            metadata={"username": username, "email": demo_user["email"].lower()},
        )
        return {"demo_token": demo_token, "user": app_user}


@router.post("/login")
@limiter.limit("5/minute")
def login_user(request: Request, body: TokenRequest):
    """Verify Firebase identity and synchronize the MongoDB application profile."""
    try:
        claims = verify_firebase_id_token(
            body.id_token,
            clock_skew_seconds=TOKEN_CLOCK_SKEW_SECONDS,
        )
    except Exception as exc:
        logger.warning("Firebase ID token verification failed on /auth/login: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Firebase ID token không hợp lệ hoặc đã hết hạn",
        ) from exc

    try:
        user = get_user_service().sync_from_claims(claims)
        if user.get("approval_status") == "PENDING":
            raise HTTPException(status_code=403, detail=PENDING_APPROVAL_DETAIL)
        if not user["is_active"]:
            raise HTTPException(status_code=403, detail="Tài khoản đã bị khóa")
        get_firebase_session_repository().upsert(claims["uid"], body.id_token)
        return {"message": "Đăng nhập thành công", "user": user}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Backend session sync failed for Firebase uid %s", claims.get("uid"))
        raise HTTPException(
            status_code=500,
            detail="Không thể đồng bộ phiên đăng nhập",
        ) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout_user(
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    get_firebase_session_repository().upsert(current_user.firebase_uid, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
