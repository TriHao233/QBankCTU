from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from bson import ObjectId

from core.config import settings
from modules.users.repository import (
    MongoUserRepository,
    ProfileNotProvisionedError,
    UnverifiedEmailLinkError,
)
from modules.users.schemas import PublicRegisterRequest
from modules.users.service import PublicRegistrationDisabledError, UserService


class FakeUsersCollection:
    def __init__(self, users):
        self.users = users
        self.updates = []

    def find_one(self, query, projection=None):
        for user in self.users:
            if user["email"] != query["email"]:
                continue
            excluded_uid = query.get("firebase_uid", {}).get("$ne")
            if user.get("firebase_uid") != excluded_uid:
                return user
        return None

    def find_one_and_update(self, query, update, **kwargs):
        self.updates.append(query)
        return {"_id": ObjectId(), "query": query}


def _repository(users):
    collection = FakeUsersCollection(users)
    return MongoUserRepository(SimpleNamespace(users=collection)), collection


def test_unverified_email_cannot_claim_profile_of_another_identity():
    repository, collection = _repository([
        {"_id": ObjectId(), "email": "lecturer@ctu.edu.vn", "firebase_uid": "deleted-uid", "role": "Admin"},
    ])

    with pytest.raises(UnverifiedEmailLinkError):
        repository.sync_identity({"uid": "attacker-uid", "email": "Lecturer@ctu.edu.vn", "email_verified": False})

    assert collection.updates == []


def test_unverified_email_only_matches_by_firebase_uid():
    repository, collection = _repository([
        {"_id": ObjectId(), "email": "lecturer@ctu.edu.vn", "firebase_uid": "same-uid"},
    ])

    repository.sync_identity({"uid": "same-uid", "email": "lecturer@ctu.edu.vn"})

    assert collection.updates == [{"firebase_uid": "same-uid"}]


def test_verified_email_may_link_existing_profile():
    repository, collection = _repository([
        {"_id": ObjectId(), "email": "lecturer@ctu.edu.vn", "firebase_uid": "old-uid"},
    ])

    repository.sync_identity({"uid": "new-uid", "email": "lecturer@ctu.edu.vn", "email_verified": True})

    assert collection.updates == [
        {"$or": [{"firebase_uid": "new-uid"}, {"email": "lecturer@ctu.edu.vn"}]}
    ]


def test_public_registration_does_not_take_over_existing_profile(monkeypatch):
    monkeypatch.setattr(settings, "allow_public_registration", True)
    repository = Mock()
    repository.sync_identity.side_effect = UnverifiedEmailLinkError()
    identity = Mock()
    identity.create_user.return_value = SimpleNamespace(uid="new-uid")
    service = UserService(repository, identity, Mock())

    with pytest.raises(UnverifiedEmailLinkError):
        service.register_teacher(PublicRegisterRequest(
            full_name="Nguyễn Văn A",
            email="lecturer@ctu.edu.vn",
            password="Matkhau123!",
        ))

    identity.delete_user.assert_called_once_with("new-uid")


class _ProvisioningCollection:
    def __init__(self, users):
        self.users = users
        self.upserts = []

    def find_one(self, query, projection=None):
        if "firebase_uid" in query and isinstance(query["firebase_uid"], str):
            return next((u for u in self.users if u["firebase_uid"] == query["firebase_uid"]), None)
        return None

    def find_one_and_update(self, query, update, *, upsert, **kwargs):
        self.upserts.append(upsert)
        return self.find_one(query) or ({"_id": ObjectId()} if upsert else None)


def test_login_without_profile_is_refused_when_self_provisioning_disabled():
    collection = _ProvisioningCollection([])
    repository = MongoUserRepository(SimpleNamespace(users=collection))

    with pytest.raises(ProfileNotProvisionedError):
        repository.sync_identity({"uid": "stranger"}, allow_create=False)

    assert collection.upserts == []


def test_login_with_invited_profile_still_syncs_when_self_provisioning_disabled():
    collection = _ProvisioningCollection([{"_id": ObjectId(), "firebase_uid": "invited", "email": "a@ctu.edu.vn"}])
    repository = MongoUserRepository(SimpleNamespace(users=collection))

    assert repository.sync_identity({"uid": "invited"}, allow_create=False)["firebase_uid"] == "invited"
    assert collection.upserts == [False]


def test_sync_from_claims_follows_public_registration_flag(monkeypatch):
    repository = Mock()
    repository.sync_identity.side_effect = ProfileNotProvisionedError()
    service = UserService(repository, Mock(), Mock())

    monkeypatch.setattr(settings, "allow_public_registration", False)
    with pytest.raises(ProfileNotProvisionedError):
        service.sync_from_claims({"uid": "stranger"})
    repository.sync_identity.assert_called_once_with({"uid": "stranger"}, allow_create=False)


def test_public_registration_disabled_creates_no_firebase_user(monkeypatch):
    monkeypatch.setattr(settings, "allow_public_registration", False)
    identity = Mock()
    service = UserService(Mock(), identity, Mock())

    with pytest.raises(PublicRegistrationDisabledError):
        service.register_teacher(PublicRegisterRequest(
            full_name="Nguyễn Văn A",
            email="lecturer@ctu.edu.vn",
            password="Matkhau123!",
        ))

    identity.create_user.assert_not_called()
