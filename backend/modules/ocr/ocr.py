import asyncio
import logging
import os
import threading
import time
from pathlib import Path

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from core.config import resolve_path, settings
from core.dependencies import CurrentUser, require_document_manager
from modules.documents.service import DocumentService, get_document_service
from modules.documents.storage import artifact_storage, storage_for_provider
from modules.documents.ingest.base import UnsupportedDocumentError
from modules.ocr.mongodb import (
    attach_original_artifact,
    attach_processing_artifact,
    create_document_record,
    create_ocr_job,
    get_document_status,
    save_document_pages,
    update_document_status,
)
from modules.ocr.pipeline import DocumentQualityError, run_ocr_pipeline

router = APIRouter(prefix=f"{settings.api_prefix}/ocr", tags=["OCR"])
logger = logging.getLogger(__name__)
gpu_semaphore = asyncio.Semaphore(1)

_UPLOAD_DIR = resolve_path(settings.upload_dir)
_OUTPUT_DIR = resolve_path(settings.ocr_output_dir)
_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
SUPPORTED_UPLOAD_TYPES = {
    ".pdf": {
        "artifact_type": "ORIGINAL_PDF",
        "mime_type": "application/pdf",
        "source_format": "pdf",
    },
    ".docx": {
        "artifact_type": "ORIGINAL_DOCX",
        "mime_type": DOCX_MIME_TYPE,
        "source_format": "docx",
    },
    ".doc": {
        "artifact_type": "ORIGINAL_DOC",
        "mime_type": "application/msword",
        "source_format": "doc",
    },
    ".md": {
        "artifact_type": "ORIGINAL_MARKDOWN",
        "mime_type": "text/markdown",
        "source_format": "md",
    },
    ".markdown": {
        "artifact_type": "ORIGINAL_MARKDOWN",
        "mime_type": "text/markdown",
        "source_format": "md",
    },
    ".txt": {
        "artifact_type": "ORIGINAL_TEXT",
        "mime_type": "text/plain",
        "source_format": "txt",
    },
}
DOCX_PAGE_CHAR_LIMIT = 12_000


def _ocr_job_cancelled(job_id: str) -> bool:
    status = get_document_status(job_id)
    return ((status or {}).get("job") or {}).get("status") == "CANCELLED"


# Serialize chunk/embed so concurrent OCR completions do not peg the API process.
_auto_chunk_lock = threading.Lock()


def _run_auto_chunk_sync(document_id: str) -> None:
    """Heavy imports + embedding must stay off the asyncio event loop."""
    with _auto_chunk_lock:
        try:
            # Lazy import inside the worker thread (not on the event loop).
            from modules.dictionary.dictionary import run_dictionary_auto_learning
            from modules.rag.chunking import chunk_document_and_store

            result = chunk_document_and_store(
                document_id=document_id,
                chunk_size=settings.chunk_size_default,
                chunk_overlap=settings.chunk_overlap_default,
            )
            if result.total_chunks > 0:
                logger.info("Auto-learning dictionary keywords for document %s", document_id)
                run_dictionary_auto_learning(
                    document_id=document_id,
                    course_id="it_fundamentals",
                )
        except Exception:
            logger.exception("Auto-chunk after OCR failed for document %s", document_id)


def _auto_chunk_after_ocr(document_id: str) -> None:
    """Fire-and-forget chunk/index so OCR completion does not block API requests."""
    thread = threading.Thread(
        target=_run_auto_chunk_sync,
        args=(document_id,),
        name=f"auto-chunk-{document_id[:8]}",
        daemon=True,
    )
    thread.start()


async def process_ocr_background(
    document_id: str,
    job_id: str,
    upload_path: str,
    output_path: str,
    document_title: str,
    source_file_name: str | None = None,
    mime_type: str | None = None,
    source_provider: str = "LOCAL",
):
    ocr_completed = False
    try:
        async with gpu_semaphore:
            if _ocr_job_cancelled(job_id):
                return
            update_document_status(document_id, job_id, status="processing")
            started_at = time.time()
            started_perf = time.perf_counter()
            # OCR libraries need a real file; object storage is copied locally
            # for the duration of the pipeline only.
            with storage_for_provider(source_provider).local_copy(upload_path) as local_source:
                result = await run_in_threadpool(
                    run_ocr_pipeline,
                    pdf_path=str(local_source),
                    output_path=output_path,
                    document_title=document_title,
                    document_id=document_id,
                    source_file_name=source_file_name or Path(upload_path).name,
                    source_uri=upload_path,
                    mime_type=mime_type,
                )
            if _ocr_job_cancelled(job_id):
                return
            stats = result["stats"]
            persist_started = time.perf_counter()
            save_document_pages(document_id, job_id, result["pages"])
            for artifact_type, artifact_path, artifact_mime in (
                (
                    "RAW_EXTRACTION_JSON",
                    result.get("raw_extraction_file"),
                    result.get("raw_extraction_mime_type") or "application/json",
                ),
                ("EXTRACTION_MARKDOWN", result.get("output_file"), "text/markdown"),
            ):
                if not artifact_path:
                    continue
                blob = artifact_storage("artifact_blobs").save_content_addressed(
                    Path(artifact_path), content_type=artifact_mime,
                )
                attach_processing_artifact(
                    document_id,
                    job_id,
                    uri=blob["uri"],
                    size_bytes=blob["size_bytes"],
                    sha256=blob["sha256"],
                    artifact_type=artifact_type,
                    mime_type=artifact_mime,
                    provider=blob["provider"],
                )
            timings_ms = stats.setdefault("timings_ms", {})
            timings_ms["mongo_page_persist"] = round((time.perf_counter() - persist_started) * 1000, 2)
            timings_ms["background_total"] = round((time.perf_counter() - started_perf) * 1000, 2)
            stats["processing_time"] = round(time.time() - started_at, 1)
            if _ocr_job_cancelled(job_id):
                return
            update_document_status(document_id, job_id, status="completed", stats=stats)
            ocr_completed = True
    except DocumentQualityError as exc:
        logger.error("OCR job %s failed quality gate: %s", job_id, exc)
        if _ocr_job_cancelled(job_id):
            return
        update_document_status(
            document_id,
            job_id,
            status="failed",
            stats={"quality_status": "quality_failed", "quality": exc.report},
            error_message=str(exc),
        )
    except UnsupportedDocumentError as exc:
        logger.error("OCR job %s failed closed at document adapter: %s", job_id, exc)
        if _ocr_job_cancelled(job_id):
            return
        update_document_status(
            document_id,
            job_id,
            status="failed",
            stats={"quality_status": "quality_failed", "adapter_error": exc.to_dict()},
            error_message=str(exc),
        )
    except Exception as exc:
        logger.exception("OCR job %s failed", job_id)
        if _ocr_job_cancelled(job_id):
            return
        update_document_status(
            document_id,
            job_id,
            status="failed",
            error_message=str(exc),
        )

    if ocr_completed and not _ocr_job_cancelled(job_id):
        # Release GPU first; chunk/index runs in a background thread.
        _auto_chunk_after_ocr(document_id)


def _iter_docx_blocks(document: Document):
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _table_to_markdown(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_docx_pages(docx_path: str | Path, *, page_char_limit: int = DOCX_PAGE_CHAR_LIMIT) -> tuple[list[dict], dict]:
    document = Document(str(docx_path))
    blocks: list[str] = []
    paragraph_count = 0
    table_count = 0

    for block in _iter_docx_blocks(document):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if text:
                blocks.append(text)
                paragraph_count += 1
        elif isinstance(block, Table):
            text = _table_to_markdown(block).strip()
            if text:
                blocks.append(text)
                table_count += 1

    if not blocks:
        raise ValueError("DOCX không có nội dung văn bản để xử lý")

    pages: list[dict] = []
    current: list[str] = []
    current_length = 0
    for block in blocks:
        block_length = len(block)
        if current and current_length + block_length + 2 > page_char_limit:
            text = "\n\n".join(current).strip()
            pages.append({"page_number": len(pages) + 1, "text": text, "original_text": text, "formula_blocks": []})
            current = []
            current_length = 0
        current.append(block)
        current_length += block_length + 2

    if current:
        text = "\n\n".join(current).strip()
        pages.append({"page_number": len(pages) + 1, "text": text, "original_text": text, "formula_blocks": []})

    stats = {
        "source_format": "docx",
        "page_count": len(pages),
        "paragraph_count": paragraph_count,
        "table_count": table_count,
        "char_count": sum(len(page["text"]) for page in pages),
    }
    return pages, stats


async def process_docx_background(
    document_id: str,
    job_id: str,
    upload_path: str,
    output_path: str,
    document_title: str,
    source_file_name: str | None = None,
    mime_type: str | None = DOCX_MIME_TYPE,
):
    await process_ocr_background(
        document_id=document_id,
        job_id=job_id,
        upload_path=upload_path,
        output_path=output_path,
        document_title=document_title,
        source_file_name=source_file_name,
        mime_type=mime_type,
    )


async def queue_document_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    current_user: CurrentUser,
    *,
    subject_id: str | None = None,
    chapter_id: str | None = None,
) -> dict:
    safe_filename = Path(file.filename or "").name
    extension = Path(safe_filename).suffix.lower()
    upload_type = SUPPORTED_UPLOAD_TYPES.get(extension)
    if not safe_filename or not upload_type:
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận file PDF, DOC, DOCX, Markdown hoặc TXT")

    file.file.seek(0, os.SEEK_END)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dung lượng tối đa 50 MB")

    title = Path(safe_filename).stem.replace("_", " ")
    try:
        document_id = create_document_record(
            filename=safe_filename,
            title=title,
            uploaded_by_user_id=current_user.id,
            subject_id=subject_id,
            chapter_id=chapter_id,
        )
    except ValueError as exc:
        file.file.close()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_id = create_ocr_job(document_id, config={"source_format": upload_type["source_format"]})
    upload_path = _UPLOAD_DIR / f"{document_id}_{safe_filename}"
    output_path = _OUTPUT_DIR / f"{document_id}_{job_id}_result.md"

    try:
        saved = artifact_storage("uploads").save_stream(
            upload_path.name, file.file, content_type=upload_type["mime_type"],
        )
        attach_original_artifact(
            document_id,
            uri=saved["uri"],
            size_bytes=saved["size_bytes"],
            sha256=saved["sha256"],
            artifact_type=upload_type["artifact_type"],
            mime_type=upload_type["mime_type"],
            provider=saved["provider"],
        )
    except Exception as exc:
        update_document_status(
            document_id,
            job_id,
            status="failed",
            error_message="Lỗi lưu file vật lý",
        )
        raise HTTPException(status_code=500, detail="Lỗi lưu file") from exc
    finally:
        file.file.close()

    background_tasks.add_task(
        process_ocr_background,
        document_id=document_id,
        job_id=job_id,
        upload_path=saved["uri"],
        output_path=str(output_path),
        document_title=title,
        source_file_name=safe_filename,
        mime_type=upload_type["mime_type"],
        source_provider=saved["provider"],
    )
    return {
        "message": "File đã được tiếp nhận và đang xử lý nền",
        "document_id": document_id,
        "job_id": job_id,
        "status": "QUEUED",
    }


async def queue_pdf_ocr_upload(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    current_user: CurrentUser,
    *,
    subject_id: str | None = None,
    chapter_id: str | None = None,
) -> dict:
    """Compatibility alias for the format-independent upload pipeline."""
    return await queue_document_upload(
        background_tasks,
        file,
        current_user,
        subject_id=subject_id,
        chapter_id=chapter_id,
    )

@router.post("/upload", summary="Compatibility upload route; prefer /documents/upload")
async def upload_pdf(
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


@router.get("/status/{job_id}", summary="Get OCR job status")
def check_job_status(
    job_id: str,
    current_user: CurrentUser = Depends(require_document_manager),
    document_service: DocumentService = Depends(get_document_service),
):
    try:
        result = get_document_status(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="Không tìm thấy OCR job")
    document = result.get("document")
    if document:
        try:
            document_service.can_use(document["id"], current_user)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
    from core.status_enums import normalize_job_status

    job = result["job"]
    return {
        "job_id": job["_id"],
        "document_id": job["document_id"],
        "status": normalize_job_status(job["status"]),
        "progress": job.get("progress"),
        "error_message": (job.get("error") or {}).get("message"),
        "stats": job.get("stats"),
    }
