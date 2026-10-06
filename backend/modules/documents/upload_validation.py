"""Content checks for uploaded source files, run before any document record exists."""

from __future__ import annotations

import codecs
import hashlib
import zipfile
from typing import BinaryIO

__all__ = ["UploadContentError", "inspect_upload"]

_CHUNK_SIZE = 1024 * 1024
_PDF_HEADER_WINDOW = 1024
_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_RTF_HEADER = b"{\\rtf"
_TEXT_FORMATS = {"md", "txt"}


class UploadContentError(ValueError):
    pass


def _check_docx(file_obj: BinaryIO) -> None:
    try:
        with zipfile.ZipFile(file_obj) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise UploadContentError("File DOCX không hợp lệ hoặc bị hỏng") from exc
    if "word/document.xml" not in names:
        raise UploadContentError("File DOCX không hợp lệ hoặc bị hỏng")


def inspect_upload(file_obj: BinaryIO, source_format: str) -> dict:
    """Validate the file signature and return its size and SHA-256; rewinds the stream."""
    file_obj.seek(0)
    head = file_obj.read(_PDF_HEADER_WINDOW)
    if not head:
        raise UploadContentError("File rỗng, không có nội dung để xử lý")
    if source_format == "pdf" and b"%PDF-" not in head:
        raise UploadContentError("Nội dung file không phải PDF hợp lệ")
    if source_format == "doc" and not (head.startswith(_OLE_HEADER) or head.lstrip().startswith(_RTF_HEADER)):
        raise UploadContentError("Nội dung file không phải DOC hợp lệ")
    if source_format == "docx":
        file_obj.seek(0)
        _check_docx(file_obj)

    file_obj.seek(0)
    digest = hashlib.sha256()
    size = 0
    decoder = codecs.getincrementaldecoder("utf-8-sig")() if source_format in _TEXT_FORMATS else None
    while chunk := file_obj.read(_CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
        if decoder is not None:
            if b"\x00" in chunk:
                raise UploadContentError("File văn bản chứa dữ liệu nhị phân")
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError as exc:
                raise UploadContentError("File văn bản phải được mã hóa UTF-8") from exc
    if decoder is not None:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise UploadContentError("File văn bản phải được mã hóa UTF-8") from exc
    file_obj.seek(0)
    return {"size_bytes": size, "sha256": digest.hexdigest()}
