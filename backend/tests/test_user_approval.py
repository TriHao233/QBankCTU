from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from pydantic import ValidationError

from modules.users import service as users_service
from modules.users.schemas import PublicRegisterRequest, UserAdminUpdateRequest
from modules.users.service import UserService


def _user(**overrides):
    now = datetime.now(timezone.utc)
    user = {
        "_id": ObjectId(),
        "firebase_uid": "uid-new",
        "email": "gv@ctu.edu.vn",
        "display_name": "Giảng viên mới",
        "role": "Teacher",
        "is_active": True,
        "approval_status": "APPROVED",
        "created_at": now,
        "updated_at": now,
    }
    user.update(overrides)
    return user


class FakeRepository:
    def __init__(self, existing=None, admins=None):
        self.users = {str(u["_id"]): u for u in (existing or [])}
        self.admins = admins or []
        self.sync_calls = []

    def find_by_firebase_uid(self, uid):
        return next((u for u in self.users.values() if u["firebase_uid"] == uid), None)

    def find_by_id(self, user_id):
        return self.users.get(str(user_id))

    def sync_identity(self, claims, *, self_signup=False):
        self.sync_calls.append(self_signup)
        existing = self.find_by_firebase_uid(claims["uid"])
        if existing:
            return existing
        user = _user(
            firebase_uid=claims["uid"],
            email=claims["email"],
            display_name=claims.get("name") or claims["email"],
            is_active=not self_signup,
            approval_status="PENDING" if self_signup else "APPROVED",
        )
        self.users[str(user["_id"])] = user
        return user

    def list(self, page, page_size, role, search):
        return self.admins, len(self.admins)

    def update(self, user_id, fields):
        self.users[str(user_id)].update(fields)
        return self.users[str(user_id)]

    def delete_by_id(self, user_id):
        self.users.pop(str(user_id), None)

    def count_active_admins(self):
        return len(self.admins)


class FakeIdentity:
    def __init__(self):
        self.disabled = {}

    def create_user(self, *, email, password, display_name):
        return SimpleNamespace(uid="uid-registered")

    def delete_user(self, uid):
        pass

    def update_user(self, uid, **kwargs):
        pass

    def set_user_disabled(self, uid, disabled):
        self.disabled[uid] = disabled


class FakeSessions:
    def upsert(self, uid, token):
        pass


@pytest.fixture
def notifications(monkeypatch):
    sent = []

    class FakeNotificationService:
        def create_many(self, items):
            sent.extend(items)

    import modules.notifications.service as notification_service

    monkeypatch.setattr(notification_service, "get_notification_service", lambda: FakeNotificationService())
    return sent


def _service(repository):
    return UserService(repository, FakeIdentity(), FakeSessions())


def test_self_registration_creates_pending_account_and_notifies_admins(notifications):
    admin = _user(role="Admin", email="admin@ctu.edu.vn", firebase_uid="uid-admin")
    repository = FakeRepository(admins=[admin])

    result = _service(repository).register_teacher(
        PublicRegisterRequest(email="gv@ctu.edu.vn", password="matkhau123", full_name="  Giảng viên  ")
    )

    assert result["approval_status"] == "PENDING"
    assert result["is_active"] is False
    assert repository.sync_calls == [True]
    assert [item["recipient_user_id"] for item in notifications] == [admin["_id"]]
    assert notifications[0]["link"] == "/quan-ly-nguoi-dung?approval=PENDING"


def test_google_sign_in_notifies_admins_only_for_first_login(notifications):
    admin = _user(role="Admin", firebase_uid="uid-admin")
    repository = FakeRepository(admins=[admin])
    service = _service(repository)
    claims = {"uid": "uid-google", "email": "gv@gmail.com", "name": "GV"}

    first = service.sync_from_claims(claims)
    second = service.sync_from_claims(claims)

    assert first["approval_status"] == second["approval_status"] == "PENDING"
    assert len(notifications) == 1


def test_activating_pending_account_approves_it(monkeypatch):
    monkeypatch.setattr(users_service, "record_audit_event", lambda **kwargs: None)
    pending = _user(is_active=False, approval_status="PENDING")
    repository = FakeRepository(existing=[pending])

    result = _service(repository).update_admin(str(pending["_id"]), UserAdminUpdateRequest(is_active=True))

    assert result["approval_status"] == "APPROVED"
    assert result["is_active"] is True


@pytest.mark.parametrize("password", ["abc12", "chicochu", "12345678"])
def test_public_registration_requires_letters_and_digits(password):
    with pytest.raises(ValidationError):
        PublicRegisterRequest(email="gv@ctu.edu.vn", password=password, full_name="GV")


def test_public_registration_rejects_blank_name():
    with pytest.raises(ValidationError):
        PublicRegisterRequest(email="gv@ctu.edu.vn", password="matkhau123", full_name="   ")
