import logging
from dataclasses import dataclass
from typing import Callable

from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.database import get_rag_db
from core.config import settings
from core.demo_auth import is_demo_session_token, verify_demo_session_token
from core.firebase_auth import verify_firebase_id_token
from modules.auth.session_repository import get_firebase_session_repository

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)

# Keep in sync with modules/auth/login.py's TOKEN_CLOCK_SKEW_SECONDS.
TOKEN_CLOCK_SKEW_SECONDS = 60

# Mô hình quyền:
# - Vai trò quyết định bộ quyền mặc định.
# - Admin luôn có toàn bộ quyền; quyền quản trị chỉ thuộc vai trò Admin.
# - Giảng viên/Người duyệt có thể được THÊM hoặc BỎ từng quyền nghiệp vụ so với
#   mặc định của vai trò (lưu ở permission_grants / permission_revokes).
# Mỗi quyền dưới đây đều được kiểm tra thật ở backend.
TEACHER_PERMISSIONS = frozenset(
    {
        "documents.manage_own",
        "questions.generate",
        "questions.manage_own",
        "questions.share_bank",
        "questions.use_shared_bank",
        "exams.manage_own",
        "catalog.subjects.manage_own",
        # Theo thuyết minh: giảng viên là người duyệt human-in-the-loop.
        "reviews.manage",
        "questions.export_moodle",
    }
)
REVIEW_PERMISSIONS = frozenset({"reviews.manage", "questions.export_moodle"})
ADMIN_ONLY_PERMISSIONS = frozenset(
    {
        "admin.overview",
        "admin.users",
        "admin.catalog",
        "admin.audit",
        "admin.jobs",
        "admin.moodle",
        "documents.manage_all",
        "questions.manage_all",
    }
)
# Quyền có thể thêm/bỏ riêng cho Giảng viên (role Reviewer đã ngừng dùng).
ASSIGNABLE_PERMISSIONS = TEACHER_PERMISSIONS | REVIEW_PERMISSIONS
ALL_PERMISSIONS = ASSIGNABLE_PERMISSIONS | ADMIN_ONLY_PERMISSIONS
# Admin quản trị hệ thống; không duyệt câu hỏi (khớp thuyết minh).
ADMIN_PERMISSIONS = frozenset(ALL_PERMISSIONS - {"reviews.manage"})

DEFAULT_ROLE_PERMISSIONS = {
    "Admin": set(ADMIN_PERMISSIONS),
    "Teacher": set(TEACHER_PERMISSIONS),
    # Giữ để tương thích dữ liệu cũ; UI không cho tạo mới.
    "Reviewer": set(REVIEW_PERMISSIONS),
}


PERMISSION_LABELS = {
    "documents.manage_own": "Quản lý tài liệu của mình",
    "questions.generate": "Sinh câu hỏi bằng AI",
    "questions.manage_own": "Soạn và quản lý câu hỏi của mình",
    "questions.share_bank": "Chia sẻ câu hỏi và tài liệu",
    "questions.use_shared_bank": "Dùng câu hỏi, tài liệu được chia sẻ",
    "exams.manage_own": "Làm đề thi",
    "catalog.subjects.manage_own": "Tạo và sửa học phần của mình",
    "reviews.manage": "Kiểm duyệt câu hỏi",
    "questions.export_moodle": "Đưa câu đã duyệt lên Moodle",
    "admin.overview": "Xem tổng quan",
    "admin.users": "Quản lý người dùng",
    "admin.catalog": "Quản lý học phần và cấu hình AI",
    "admin.audit": "Xem nhật ký",
    "admin.jobs": "Quản lý tác vụ",
    "admin.moodle": "Quản lý Moodle",
    "documents.manage_all": "Quản lý mọi tài liệu",
    "questions.manage_all": "Quản lý mọi câu hỏi",
}


def _permission_text(permissions) -> str:
    return ", ".join(PERMISSION_LABELS.get(item, item) for item in sorted(permissions))


def _permission_set(values) -> set[str]:
    return {value.strip() for value in values or [] if isinstance(value, str) and value.strip()}


def effective_permissions(user: dict) -> tuple[str, ...]:
    role = user.get("role")
    if role == "Admin":
        return tuple(sorted(ADMIN_PERMISSIONS))
    permissions = set(DEFAULT_ROLE_PERMISSIONS.get(role, set()))
    # `permissions` là dữ liệu cũ (chỉ cộng thêm); ghi mới dùng grants/revokes.
    grants = _permission_set(user.get("permission_grants")) | _permission_set(user.get("permissions"))
    permissions -= _permission_set(user.get("permission_revokes"))
    permissions |= grants
    return tuple(sorted(permissions & ASSIGNABLE_PERMISSIONS))


def permission_overrides(role: str, desired: list[str] | None) -> dict[str, list[str]]:
    """Chuyển danh sách quyền mong muốn thành phần thêm/bỏ so với mặc định của vai trò."""
    if role == "Admin" or desired is None:
        return {"permission_grants": [], "permission_revokes": []}
    wanted = _permission_set(desired)
    not_allowed = wanted - ASSIGNABLE_PERMISSIONS
    if not_allowed:
        raise ValueError(
            "Quyền quản trị chỉ dành cho vai trò Quản trị viên: " + ", ".join(sorted(not_allowed))
        )
    defaults = DEFAULT_ROLE_PERMISSIONS.get(role, set())
    return {
        "permission_grants": sorted(wanted - defaults),
        "permission_revokes": sorted(defaults - wanted),
    }


def has_permission(current_user: "CurrentUser", permission: str) -> bool:
    return permission in set(current_user.permissions)


@dataclass(frozen=True)
class CurrentUser:
    id: ObjectId
    firebase_uid: str
    email: str
    role: str
    is_active: bool
    permissions: tuple[str, ...] = ()
    display_name: str = ""


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Thiếu Firebase ID token",
        )
    try:
        bearer_token = credentials.credentials
        if is_demo_session_token(bearer_token):
            claims = verify_demo_session_token(bearer_token)
            session = get_firebase_session_repository().find_by_uid(claims["uid"])
            if settings.user_store == "postgres":
                from modules.auth.postgres_session_repository import token_hash

                if session is None or session.get("demo_token_hash") != token_hash(bearer_token):
                    raise ValueError("Demo token has been revoked")
            elif session is not None and session.get("token") != bearer_token:
                raise ValueError("Demo token has been revoked")
        else:
            claims = verify_firebase_id_token(
                bearer_token,
                clock_skew_seconds=TOKEN_CLOCK_SKEW_SECONDS,
            )
    except Exception as exc:
        logger.warning("Firebase ID token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Firebase ID token không hợp lệ hoặc đã hết hạn",
        ) from exc

    if settings.user_store == "postgres":
        from modules.users.postgres_repository import PostgresUserRepository

        user = PostgresUserRepository().find_by_firebase_uid(claims["uid"])
    else:
        user = get_rag_db().users.find_one({"firebase_uid": claims["uid"]})
    if not user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản chưa được đồng bộ với hệ thống",
        )
    if not user.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tài khoản đã bị khóa")
    get_firebase_session_repository().upsert(
        claims["uid"],
        bearer_token,
    )
    return CurrentUser(
        id=user["_id"],
        firebase_uid=user["firebase_uid"],
        email=user.get("email", ""),
        role=user["role"],
        is_active=user.get("is_active", True),
        permissions=effective_permissions(user),
        display_name=user.get("display_name", ""),
    )


def require_roles(*roles: str) -> Callable:
    """Chỉ kiểm tra vai trò. Không suy vai trò từ quyền để tránh leo quyền ngầm."""
    allowed = set(roles)

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if current_user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Yêu cầu role: {', '.join(sorted(allowed))}",
            )
        return current_user

    return dependency


def require_permissions(*permissions: str) -> Callable:
    allowed = set(permissions)

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not allowed.issubset(set(current_user.permissions)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Yêu cầu quyền: {_permission_text(allowed)}",
            )
        return current_user

    return dependency


def require_any_permission(*permissions: str) -> Callable:
    allowed = set(permissions)

    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not allowed & set(current_user.permissions):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Yêu cầu một trong các quyền: {_permission_text(allowed)}",
            )
        return current_user

    return dependency


require_admin = require_roles("Admin")
require_authenticated = require_roles("Admin", "Teacher", "Reviewer")
require_teacher_reviewer_or_admin = require_authenticated
# Kiểm duyệt: theo quyền reviews.manage (Teacher mặc định có; Admin không duyệt HITL).
require_reviewer_or_admin = require_permissions("reviews.manage")
# Không gian giảng viên: theo từng quyền nghiệp vụ.
require_teacher_or_admin = require_roles("Admin", "Teacher")
require_document_manager = require_any_permission("documents.manage_own", "documents.manage_all")
require_question_generator = require_permissions("questions.generate")
require_question_author = require_any_permission("questions.manage_own", "questions.manage_all")
require_bank_sharing = require_permissions("questions.share_bank")
require_exam_manager = require_permissions("exams.manage_own")
