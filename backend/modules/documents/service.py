from pathlib import Path

from fastapi import BackgroundTasks

from core.config import resolve_path, settings
from core.database import get_database
from core.dependencies import CurrentUser, has_permission
from modules.catalog.postgres_subject_repository import subject_records
from modules.catalog.subject_approval import ensure_subject_approved, subject_usable_by
from modules.documents.repository import (
    ACTIVE_DOCUMENT_JOB_STATUSES,
    DocumentRepository,
    RETRYABLE_DOCUMENT_JOB_STATUSES,
    RETRYABLE_DOCUMENT_JOB_TYPES,
    object_id,
    serialize_document,
    serialize_document_job,
    serialize_document_page,
)
from modules.documents.store import get_document_repository
from modules.users.store import ensure_teaches_subjects, teaching_subject_ids, users_by_ids
from modules.documents.schemas import (
    DocumentCreateRequest,
    DocumentPageUpdateRequest,
    DocumentSharingRequest,
    DocumentUpdateRequest,
)

MAX_OWNER_DOCUMENT_RETRIES = 3
MIN_OWNER_TRANSFER_REASON_LENGTH = 5


def _subject_ids_of(values) -> list:
    return [object_id(value, "subject_id") for value in dict.fromkeys(values or []) if value]


def ensure_document_subjects_usable(subject_ids, current_user: CurrentUser | None, *,
                                    require_approved_for: str | None = None) -> None:
    """Pending subjects belong to their owner until an admin approves them.

    Non-admin users may only attach documents to subjects they teach.
    """
    ids = _subject_ids_of(subject_ids)
    if not ids or current_user is None:
        return
    is_admin = getattr(current_user, "role", None) == "Admin" or has_permission(current_user, "admin.catalog")
    for subject in subject_records(get_database(), ids=ids):
        if not subject_usable_by(subject, current_user.id, is_admin=is_admin):
            raise ValueError("Học phần không tồn tại hoặc đã ngừng hoạt động")
        if require_approved_for:
            ensure_subject_approved(subject, require_approved_for)
    if not is_admin:
        ensure_teaches_subjects(get_database(), current_user, ids, require_approved_for or "dùng tài liệu ở học phần này")


def _document_subject_ids(record: dict | None) -> list:
    if not record:
        return []
    return [record.get("subject_id"), *(record.get("subject_ids") or [])]


class DocumentService:
    def __init__(self, repository: DocumentRepository):
        self.repository = repository

    @staticmethod
    def _can_manage_all(current_user: CurrentUser) -> bool:
        return current_user.role == "Admin" or has_permission(current_user, "documents.manage_all")

    @staticmethod
    def _is_owner(record: dict, current_user: CurrentUser) -> bool:
        return record.get("uploaded_by_user_id") == current_user.id

    @staticmethod
    def _ensure_access(record: dict | None, current_user: CurrentUser) -> None:
        if not record or DocumentService._can_manage_all(current_user):
            return
        shared_with = set(record.get("shared_with_user_ids") or [])
        can_use_shared = has_permission(current_user, "questions.use_shared_bank")
        if DocumentService._is_owner(record, current_user) or (
            can_use_shared
            and (current_user.id in shared_with or DocumentService._shared_with_subject(record, current_user))
        ):
            return
        # Documents the user cannot see look missing rather than forbidden.
        raise LookupError("Không tìm thấy tài liệu")

    @staticmethod
    def _shared_with_subject(record: dict, current_user: CurrentUser) -> bool:
        if record.get("shared_scope") != "SUBJECT":
            return False
        subjects = {str(item) for item in _document_subject_ids(record) if item}
        return bool(subjects & teaching_subject_ids(get_database(), current_user.id))

    @staticmethod
    def _ensure_manage_access(record: dict | None, current_user: CurrentUser) -> None:
        if not record or DocumentService._can_manage_all(current_user):
            return
        if not DocumentService._is_owner(record, current_user):
            DocumentService._ensure_access(record, current_user)
            raise PermissionError("Chỉ chủ sở hữu được thay đổi tài liệu này")

    def create(self, payload: DocumentCreateRequest, uploaded_by_user_id, *, actor: CurrentUser | None = None) -> dict:
        if actor is not None:
            ensure_document_subjects_usable(
                [payload.subject_id, *payload.subject_ids], actor,
            )
        return serialize_document(
            self.repository.create(payload.model_dump(), uploaded_by_user_id)
        )

    def get(self, document_id: str, current_user: CurrentUser | None = None) -> dict | None:
        record = self.repository.find_by_id(document_id)
        if current_user:
            self._ensure_access(record, current_user)
        return serialize_document(record) if record else None

    def list(
        self,
        page: int,
        page_size: int,
        status: str | None,
        search: str | None,
        current_user: CurrentUser | None = None,
        subject_id: str | None = None,
    ) -> dict:
        visible_to_user_id = (
            None
            if not current_user or self._can_manage_all(current_user)
            else current_user.id
        )
        list_options = {"visible_to_user_id": visible_to_user_id}
        if visible_to_user_id is not None and not has_permission(current_user, "questions.use_shared_bank"):
            list_options = {"uploaded_by_user_id": visible_to_user_id}
        elif visible_to_user_id is not None:
            list_options["shared_subject_ids"] = sorted(teaching_subject_ids(get_database(), visible_to_user_id))
        if subject_id:
            list_options["subject_id"] = object_id(subject_id, "subject_id")
        records, total = self.repository.list(page, page_size, status, search, **list_options)
        return {
            "items": [serialize_document(item) for item in records],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def update(
        self,
        document_id: str,
        payload: DocumentUpdateRequest,
        current_user: CurrentUser | None = None,
    ) -> dict | None:
        if current_user:
            self._ensure_manage_access(self.repository.find_by_id(document_id), current_user)
        fields = payload.model_dump(exclude_none=True)
        ensure_document_subjects_usable(
            [fields.get("subject_id"), *(fields.get("subject_ids") or [])], current_user,
        )
        record = self.repository.update(document_id, fields)
        return serialize_document(record) if record else None

    def archive(self, document_id: str, current_user: CurrentUser | None = None) -> bool:
        record = self.repository.find_by_id(document_id)
        if current_user:
            self._ensure_manage_access(record, current_user)
        if not record:
            return False
        return self.repository.archive(document_id, audit={
            "action": "document.archived",
            "entity_type": "document",
            "entity_id": record["_id"],
            "actor_user_id": current_user.id if current_user else None,
            "actor_role": current_user.role if current_user else None,
            "before": {"status": record.get("status"), "pending_processing": record.get("pending_processing") or {}},
            "after": {"status": "ARCHIVED", "pending_processing": {}},
            "metadata": {"title": record.get("title"),
                         "pending_cancelled": bool(record.get("pending_processing"))},
        })

    def restore(self, document_id: str, current_user: CurrentUser) -> dict | None:
        record = self.repository.find_archived(document_id)
        if not record:
            return None
        if not (self._can_manage_all(current_user) or self._is_owner(record, current_user)):
            raise LookupError("Không tìm thấy tài liệu")
        has_index = bool((record.get("current_processing") or {}).get("chunk_set_id"))
        # Without an active index the owner must reprocess, so the document comes back as FAILED.
        status = "READY" if has_index else "FAILED"
        restored = self.repository.restore(document_id, status=status, audit={
            "action": "document.restored",
            "entity_type": "document",
            "entity_id": record["_id"],
            "actor_user_id": current_user.id,
            "actor_role": current_user.role,
            "before": {"status": "ARCHIVED"},
            "after": {"status": status},
            "metadata": {"title": record.get("title")},
        })
        return serialize_document(restored) if restored else None

    def update_sharing(
        self,
        document_id: str,
        payload: DocumentSharingRequest,
        current_user: CurrentUser,
    ) -> dict | None:
        record = self.repository.find_by_id(document_id)
        if not record:
            return None
        self._ensure_manage_access(record, current_user)
        current_owner = record.get("uploaded_by_user_id")
        new_owner = None
        if payload.owner_user_id and str(payload.owner_user_id) != str(current_owner):
            new_owner = object_id(payload.owner_user_id, "owner_user_id")
        reason = (payload.owner_transfer_reason or "").strip()
        if new_owner is not None:
            if not self._can_manage_all(current_user):
                raise PermissionError("Chỉ Admin được chuyển chủ sở hữu tài liệu")
            if len(reason) < MIN_OWNER_TRANSFER_REASON_LENGTH:
                raise ValueError("Cần nêu lý do chuyển chủ sở hữu tài liệu (ít nhất 5 ký tự)")
            users = users_by_ids(get_database(), [new_owner], active_only=True)
            if not users:
                raise ValueError("Người nhận không tồn tại hoặc đã bị vô hiệu hóa")
            if users[0].get("role") not in {"Teacher", "Admin"}:
                raise ValueError("Chỉ chuyển tài liệu cho giảng viên hoặc Admin")
        if payload.shared_scope != "PRIVATE" or payload.shared_with_user_ids:
            ensure_document_subjects_usable(
                _document_subject_ids(record), current_user, require_approved_for="chia sẻ tài liệu",
            )
        shared_with = list(dict.fromkeys(str(item) for item in payload.shared_with_user_ids if item))
        if new_owner is not None:
            # Existing shares survive a transfer; the new owner no longer needs one.
            shared_with = [item for item in shared_with if item != str(new_owner)]
        fields = {
            "shared_with_user_ids": shared_with,
            "shared_scope": payload.shared_scope,
        }
        if new_owner is not None:
            fields["uploaded_by_user_id"] = new_owner
        before = {
            "uploaded_by_user_id": current_owner,
            "shared_with_user_ids": record.get("shared_with_user_ids") or [],
            "shared_scope": record.get("shared_scope") or "PRIVATE",
        }
        audit = {
            "action": "document.owner_transfer" if new_owner is not None else "document.sharing_update",
            "entity_type": "document",
            "entity_id": record["_id"],
            "actor_user_id": current_user.id,
            "actor_role": current_user.role,
            "before": before,
            "after": {
                "uploaded_by_user_id": new_owner if new_owner is not None else current_owner,
                "shared_with_user_ids": shared_with,
                "shared_scope": payload.shared_scope,
            },
            "metadata": {"reason": reason} if new_owner is not None else {},
        }
        updated = self.repository.update(document_id, fields, audit=audit)
        return serialize_document(updated) if updated else None

    def list_jobs(
        self,
        document_id: str,
        current_user: CurrentUser,
        *,
        limit: int = 20,
    ) -> dict | None:
        record = self.repository.find_by_id(document_id)
        if not record:
            return None
        self._ensure_access(record, current_user)
        jobs = self.repository.list_jobs(document_id, limit=limit)
        return {"items": [serialize_document_job(job) for job in jobs]}

    def list_pages(
        self,
        document_id: str,
        current_user: CurrentUser,
        *,
        limit: int = 100,
    ) -> dict | None:
        record = self.repository.find_by_id(document_id)
        if not record:
            return None
        self._ensure_access(record, current_user)
        pages = self.repository.list_pages(
            document_id,
            document_version=record.get("current_version"),
            limit=limit,
        )
        return {"items": [serialize_document_page(page) for page in pages]}

    def update_page(
        self,
        document_id: str,
        page_id: str,
        payload: DocumentPageUpdateRequest,
        current_user: CurrentUser,
    ) -> dict | None:
        record = self.repository.find_by_id(document_id)
        if not record:
            return None
        self._ensure_manage_access(record, current_user)
        self._ensure_ocr_editable(record)
        page = self.repository.update_page(
            document_id,
            page_id,
            document_version=record.get("current_version", 1),
            cleaned_text=payload.cleaned_text,
        )
        return serialize_document_page(page) if page else None

    def retry_job(
        self,
        document_id: str,
        job_id: str,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser,
    ) -> dict:
        document, job = self._accessible_job(document_id, job_id, current_user)
        if job.get("status") not in RETRYABLE_DOCUMENT_JOB_STATUSES:
            raise ValueError("Chỉ hỗ trợ retry job đã lỗi")
        if job.get("job_type") not in RETRYABLE_DOCUMENT_JOB_TYPES:
            raise ValueError("Hiện chỉ hỗ trợ retry OCR/CHUNK job")
        self._ensure_retry_allowed(document, job, current_user)
        if job.get("job_type") == "CHUNK":
            from modules.rag.chunking import queue_chunk_retry

            queued = queue_chunk_retry(
                background_tasks,
                document_id=str(document["_id"]),
                config=job.get("config") or {},
                promote_actor=current_user.email or str(current_user.id),
            )
            new_job = self.repository.find_job(queued["chunk_job_id"])
            return {"job": serialize_document_job(new_job)}
        if job.get("job_type") == "INDEX":
            from modules.rag.chunking import queue_document_reindex

            queued = queue_document_reindex(
                background_tasks,
                document_id=str(document["_id"]),
                collection_name=(job.get("config") or {}).get("collection_name"),
            )
            new_job = self.repository.find_job(queued["index_job_id"])
            return {"job": serialize_document_job(new_job)}

        artifact = self._original_source_artifact(document)
        upload_path = ((artifact.get("storage") or {}).get("uri"))
        source_format = {
            "ORIGINAL_PDF": "pdf",
            "ORIGINAL_DOCX": "docx",
            "ORIGINAL_DOC": "doc",
            "ORIGINAL_MARKDOWN": "md",
            "ORIGINAL_TEXT": "txt",
        }.get(artifact.get("type"), Path(upload_path).suffix.lower().lstrip("."))
        config = {
            **(job.get("config") or {}),
            "source_format": source_format,
            "auto_chunk": settings.auto_chunk_after_extraction,
        }
        new_job = self.repository.create_job(document["_id"], "OCR", config=config)

        from modules.ocr.ocr import process_ocr_background

        output_path = resolve_path(settings.ocr_output_dir) / f"{document['_id']}_{new_job['_id']}_result.md"
        background_tasks.add_task(
            process_ocr_background,
            document_id=str(document["_id"]),
            job_id=str(new_job["_id"]),
            upload_path=str(upload_path),
            output_path=str(output_path),
            document_title=document.get("title") or document.get("original_filename") or "Document",
            source_file_name=document.get("original_filename") or Path(upload_path).name,
            mime_type=artifact.get("mime_type"),
            source_provider=(artifact.get("storage") or {}).get("provider") or "LOCAL",
            auto_chunk=settings.auto_chunk_after_extraction,
            promote_actor=current_user.email or str(current_user.id),
        )
        return {"job": serialize_document_job(new_job)}

    def cancel_job(
        self,
        document_id: str,
        job_id: str,
        current_user: CurrentUser,
    ) -> dict:
        _document, job = self._accessible_job(document_id, job_id, current_user)
        if job.get("status") not in ACTIVE_DOCUMENT_JOB_STATUSES:
            raise ValueError("Job không ở trạng thái có thể hủy")
        updated = self.repository.update_job(
            job_id,
            "CANCELLED",
            error_message=f"Cancelled by {current_user.role} {current_user.email}",
        )
        return {"job": serialize_document_job(updated)}

    def reindex(
        self,
        document_id: str,
        background_tasks: BackgroundTasks,
        current_user: CurrentUser,
    ) -> dict:
        record = self.repository.find_by_id(document_id)
        if not record:
            raise LookupError("Không tìm thấy tài liệu")
        self._ensure_manage_access(record, current_user)
        summary = record.get("pipeline_summary") or {}
        current_processing = record.get("current_processing") or {}
        if not current_processing.get("chunk_set_id") or summary.get("chunk_status") != "COMPLETED":
            raise ValueError("Tài liệu cần chunk thành công trước khi re-index")
        from modules.rag.chunking import queue_document_reindex

        queued = queue_document_reindex(background_tasks, document_id=str(record["_id"]))
        new_job = self.repository.find_job(queued["index_job_id"])
        return {"job": serialize_document_job(new_job)}

    def can_use(self, document_id: str, current_user: CurrentUser) -> bool:
        record = self.repository.find_by_id(document_id)
        if not record:
            return False
        self._ensure_access(record, current_user)
        return True

    def can_manage(self, document_id: str, current_user: CurrentUser) -> bool:
        """Owner (or document admin) only: re-chunking replaces the document's retrieval index."""
        record = self.repository.find_by_id(document_id)
        if not record:
            return False
        self._ensure_manage_access(record, current_user)
        return True

    def can_generate_from(self, document_id: str, current_user: CurrentUser) -> bool:
        """Generation also requires teaching every subject the document belongs to."""
        record = self.repository.find_by_id(document_id)
        if not record:
            return False
        self._ensure_access(record, current_user)
        if not self._can_manage_all(current_user):
            ensure_teaches_subjects(
                get_database(), current_user, _document_subject_ids(record), "sinh câu hỏi từ tài liệu này",
            )
        return True

    def _ensure_retry_allowed(self, document: dict, job: dict, current_user: CurrentUser) -> None:
        """Only the latest job of a step may be retried, never while another run of it is active;
        the owner gets MAX_OWNER_DOCUMENT_RETRIES retries, after which only an admin can retry."""
        siblings = [
            item for item in self.repository.list_jobs(document["_id"], limit=500)
            if item.get("job_type") == job.get("job_type")
            and item.get("document_version") == job.get("document_version")
        ]
        if any(item.get("status") in ACTIVE_DOCUMENT_JOB_STATUSES for item in siblings):
            raise ValueError("Bước này đang có job chạy; hãy chờ job hiện tại kết thúc")
        attempt = int(job.get("attempt_no") or 1)
        latest_attempt = max((int(item.get("attempt_no") or 1) for item in siblings), default=attempt)
        if latest_attempt > attempt:
            raise ValueError("Đã có lần chạy mới hơn cho bước này; chỉ retry job mới nhất")
        if latest_attempt - 1 >= MAX_OWNER_DOCUMENT_RETRIES and not self._can_manage_all(current_user):
            raise PermissionError(
                f"Bước này đã retry {MAX_OWNER_DOCUMENT_RETRIES} lần; liên hệ quản trị viên để chạy lại"
            )

    def _accessible_job(
        self,
        document_id: str,
        job_id: str,
        current_user: CurrentUser,
    ) -> tuple[dict, dict]:
        document = self.repository.find_by_id(document_id)
        if not document:
            raise LookupError("Không tìm thấy tài liệu")
        self._ensure_manage_access(document, current_user)
        job = self.repository.find_job(job_id)
        if not job or str(job.get("document_id")) != str(document["_id"]):
            raise LookupError("Không tìm thấy job tài liệu")
        return document, job

    @staticmethod
    def _original_source_artifact(document: dict) -> dict:
        artifact = next(
            (
                item for item in document.get("artifacts", [])
                if item.get("type") in {
                    "ORIGINAL_PDF",
                    "ORIGINAL_DOCX",
                    "ORIGINAL_DOC",
                    "ORIGINAL_MARKDOWN",
                    "ORIGINAL_TEXT",
                }
                and item.get("is_current", True)
            ),
            None,
        )
        upload_path = ((artifact or {}).get("storage") or {}).get("uri")
        if not upload_path:
            raise ValueError("Không tìm thấy file gốc để retry xử lý tài liệu")
        return artifact

    @staticmethod
    def _ensure_ocr_editable(document: dict) -> None:
        summary = document.get("pipeline_summary") or {}
        blocking_statuses = {"QUEUED", "PROCESSING", "COMPLETED"}
        if summary.get("chunk_status") in blocking_statuses or summary.get("index_status") in blocking_statuses:
            raise ValueError("Chỉ sửa OCR trước khi chunk/index hoặc sau khi chunk/index lỗi/hủy")


def get_document_service() -> DocumentService:
    return DocumentService(get_document_repository())
