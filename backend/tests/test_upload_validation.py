from __future__ import annotations

import asyncio
import hashlib
import zipfile
from io import BytesIO
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import BackgroundTasks, HTTPException, UploadFile

from modules.documents.upload_validation import UploadContentError, inspect_upload
from modules.ocr import ocr as ocr_module


def _docx_bytes(include_document: bool = True) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        if include_document:
            archive.writestr("word/document.xml", "<document/>")
    return buffer.getvalue()


@pytest.mark.parametrize(("payload", "source_format"), [
    (b"%PDF-1.7\n...", "pdf"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "doc"),
    (b"{\\rtf1 hello}", "doc"),
    ("Bài học tiếng Việt".encode("utf-8-sig"), "txt"),
    ("# Tiêu đề".encode(), "md"),
])
def test_valid_uploads_return_size_and_hash(payload, source_format):
    stream = BytesIO(payload)
    result = inspect_upload(stream, source_format)
    assert result == {"size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    assert stream.tell() == 0


def test_valid_docx_is_accepted():
    payload = _docx_bytes()
    assert inspect_upload(BytesIO(payload), "docx")["size_bytes"] == len(payload)


@pytest.mark.parametrize(("payload", "source_format"), [
    (b"", "pdf"),
    (b"", "txt"),
    (b"not a pdf", "pdf"),
    (b"PK\x03\x04garbage", "docx"),
    (_docx_bytes(include_document=False), "docx"),
    (b"plain text", "doc"),
    (b"text\x00binary", "txt"),
    (b"\xff\xfe\x00latin", "md"),
    ("xin chào".encode("cp1258", errors="ignore") + b"\xe0\xe1", "txt"),
])
def test_invalid_uploads_are_rejected(payload, source_format):
    with pytest.raises(UploadContentError):
        inspect_upload(BytesIO(payload), source_format)


def test_multibyte_character_split_across_chunks_is_accepted(monkeypatch):
    from modules.documents import upload_validation

    monkeypatch.setattr(upload_validation, "_CHUNK_SIZE", 3)
    payload = "ạạạạ".encode()
    assert inspect_upload(BytesIO(payload), "txt")["size_bytes"] == len(payload)


def test_empty_upload_returns_400_before_any_record(monkeypatch):
    monkeypatch.setattr(ocr_module, "create_document_record", lambda **_: pytest.fail("must not create"))
    upload = UploadFile(file=BytesIO(b""), filename="empty.pdf")
    with pytest.raises(HTTPException) as error:
        asyncio.run(ocr_module.queue_document_upload(BackgroundTasks(), upload, SimpleNamespace(id=ObjectId())))
    assert error.value.status_code == 400
    assert upload.file.closed


def test_duplicate_upload_by_same_user_returns_409_with_existing_id(monkeypatch):
    existing_id = ObjectId()
    user = SimpleNamespace(id=ObjectId())
    payload = b"%PDF-1.4 same content"
    seen = {}

    def find_duplicate(user_id, sha256):
        seen.update(user_id=user_id, sha256=sha256)
        return {"_id": existing_id, "title": "Bài 1"}

    monkeypatch.setattr(ocr_module, "ensure_document_subjects_usable", lambda *_a, **_k: None)
    monkeypatch.setattr(ocr_module, "find_duplicate_upload", find_duplicate)
    monkeypatch.setattr(ocr_module, "create_document_record", lambda **_: pytest.fail("must not create"))
    upload = UploadFile(file=BytesIO(payload), filename="bai1.pdf")

    with pytest.raises(HTTPException) as error:
        asyncio.run(ocr_module.queue_document_upload(BackgroundTasks(), upload, user))

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "DUPLICATE_DOCUMENT"
    assert error.value.detail["document_id"] == str(existing_id)
    assert seen == {"user_id": user.id, "sha256": hashlib.sha256(payload).hexdigest()}
    assert upload.file.closed


def test_mongo_duplicate_lookup_scopes_to_uploader_and_ignores_archived_or_failed():
    from modules.documents.repository import MongoDocumentRepository

    captured = {}

    class Collection:
        def find_one(self, query, projection):
            captured["query"] = query
            return None

    repository = MongoDocumentRepository.__new__(MongoDocumentRepository)
    repository.collection = Collection()
    user_id = ObjectId()
    assert repository.find_duplicate_original(user_id, "a" * 64) is None
    query = captured["query"]
    assert query["uploaded_by_user_id"] == user_id
    assert query["archived_at"] is None
    assert set(query["status"]["$nin"]) == {"ARCHIVED", "FAILED"}
    assert query["artifacts"]["$elemMatch"]["sha256"] == "a" * 64
