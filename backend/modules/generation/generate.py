import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from core.config import settings
from core.database import get_database
from core.dependencies import CurrentUser, effective_permissions, has_permission, require_question_generator
from core.job_worker import maintain_lease
from modules.documents.service import DocumentService, get_document_service
from modules.users.store import find_user_record
from modules.generation.mongodb import (
    ActiveJobLimitError,
    DocumentNotIndexedError,
    cancel_generation_job,
    claim_generation_job,
    count_active_generation_jobs,
    create_generation_job,
    generation_index_snapshot,
    get_generation_job,
    get_generation_job_by_idempotency,
    heartbeat_generation_job,
    retry_or_dead_letter_generation_job,
    update_generation_job,
    update_generation_progress,
    validate_generation_scope,
)
from modules.generation.job_outcome import generation_job_outcome
from modules.generation.question import (
    GenerationAccessRevoked,
    GenerationDataError,
    GenerationOutputError,
    generate_questions_rag,
)
from modules.generation.llm.model_registry import GENERATION_CAPABILITY, resolve_model_snapshot
from modules.generation.schemas import (
    GenerationJobStatus,
    GenerationJobStatusResponse,
    GenerationPlanSummary,
    GeneratedQuestion,
    JobAcceptedResponse,
    QuestionGenerateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix=f"{settings.api_prefix}/generate", tags=["generation"])

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def elapsed_ms(start: datetime | None, end: datetime | None = None) -> int | None:
    if not start:
        return None
    end = end or utc_now()
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0, int((end - start).total_seconds() * 1000))


def build_generation_metrics(
    job: dict,
    *,
    processing_started_at: datetime | None,
    finished_at: datetime,
    client_telemetry: dict | None,
) -> dict:
    created_at = job.get("created_at")
    server = {
        "queued_ms": elapsed_ms(created_at, processing_started_at),
        "processing_ms": elapsed_ms(processing_started_at, finished_at),
        "total_ms": elapsed_ms(created_at, finished_at),
        "created_at": created_at,
        "processing_started_at": processing_started_at,
        "finished_at": finished_at,
    }
    client = client_telemetry or {}
    client_pre_generate = client.get("elapsed_before_generate_ms")
    estimated_total = None
    if isinstance(client_pre_generate, int) and server["processing_ms"] is not None:
        estimated_total = client_pre_generate + server["processing_ms"]
    return {
        "server": server,
        "client": client,
        "summary": {
            "estimated_frontend_total_ms": estimated_total,
            "has_client_pipeline_timings": bool(client),
        },
    }


def generation_request_hash(req: QuestionGenerateRequest) -> str:
    """Fingerprint of what the request asks for; client timings are not part of it."""
    payload = req.model_dump(mode="json", exclude={"client_telemetry"})
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _active_limit_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Bạn đã đạt giới hạn generation job đang chờ hoặc đang xử lý.",
        headers={"Retry-After": "15"},
    )


MEMBERSHIP_REVOKED = "MEMBERSHIP_REVOKED"
SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


def generation_access_problem(requested_by_user_id, req: QuestionGenerateRequest) -> tuple[str, str] | None:
    """Re-check, while the job runs, that the requester may still generate from every source."""
    record = find_user_record(get_database(), requested_by_user_id)
    if not record or not record.get("is_active", True):
        return MEMBERSHIP_REVOKED, "Tài khoản người yêu cầu không còn hoạt động"
    user = CurrentUser(
        id=record["_id"], firebase_uid=record.get("firebase_uid", ""), email=record.get("email", ""),
        role=record.get("role", ""), is_active=True, permissions=effective_permissions(record),
    )
    if not has_permission(user, "questions.generate"):
        return MEMBERSHIP_REVOKED, "Người yêu cầu không còn quyền sinh câu hỏi"
    documents = get_document_service()
    for document_id in req.all_document_ids():
        try:
            allowed = documents.can_generate_from(document_id, user)
        except PermissionError:
            return MEMBERSHIP_REVOKED, "Người yêu cầu không còn quyền dùng tài liệu hoặc học phần này"
        except LookupError:
            allowed = False
        if not allowed:
            return SOURCE_UNAVAILABLE, "Tài liệu nguồn không còn khả dụng"
    return None


def stopped_generation_result(progress: dict, code: str, message: str) -> dict:
    """PARTIAL result of a job stopped because access or a source was lost; saved drafts are kept."""
    data = list(progress.get("data") or [])
    run_ids = sorted({str(item["generation_run_id"]) for item in data if item.get("generation_run_id")})
    return {
        "status": "partial",
        "stop_code": code,
        "data": data,
        "summary": list(progress.get("summary") or []),
        "warnings": [
            *(progress.get("warnings") or []),
            f"Đã dừng sinh câu hỏi: {message}. Đã lưu {len(data)} câu ở trạng thái nháp.",
        ],
        "generation_run_ids": run_ids,
    }


def partial_generation_result(progress: dict) -> dict:
    """Job result for a run interrupted after some drafts were already saved."""
    data = list(progress.get("data") or [])
    total = progress.get("total") or len(data)
    run_ids = sorted({str(item["generation_run_id"]) for item in data if item.get("generation_run_id")})
    warning = (
        f"Quá trình sinh bị dừng giữa chừng do lỗi hệ thống; đã lưu {len(data)}/{total} câu ở trạng thái nháp. "
        "Các câu này vẫn nằm trong ngân hàng câu hỏi, có thể chỉnh sửa và gửi duyệt hoặc sinh thêm phần còn thiếu."
    )
    return {
        "status": "partial",
        "data": data,
        "summary": list(progress.get("summary") or []),
        "warnings": [*(progress.get("warnings") or []), warning],
        "generation_run_ids": run_ids,
    }


async def process_generate_background(job_id: str, worker_id: str):
    """Worker xử lý sinh câu hỏi ngầm."""
    job = await asyncio.to_thread(claim_generation_job, job_id, worker_id)
    if not job:
        logger.info("Job [%s] không còn ở trạng thái queued", job_id)
        return
    requested_by_user_id = job.get("requested_by_user_id")
    processing_started_at = None
    last_progress: dict = {}
    client_telemetry = (job.get("request") or {}).get("client_telemetry") or {}
    lease_stop = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        maintain_lease(
            lambda: heartbeat_generation_job(job_id, worker_id),
            lease_stop,
        )
    )

    try:
        logger.info("Job [%s] đang đợi cấp phát tài nguyên sinh câu hỏi...", job_id)

        processing_started_at = utc_now()
        req = QuestionGenerateRequest(**job["request"])
        client_telemetry = (
            req.client_telemetry.model_dump(exclude_none=True)
            if req.client_telemetry
            else {}
        )

        async def report_progress(progress: dict) -> None:
            updated = await asyncio.to_thread(update_generation_progress, job_id, worker_id, progress)
            if not updated:
                raise asyncio.CancelledError(f"Job {job_id} không còn được phép tiếp tục")
            last_progress.clear()
            last_progress.update(progress)

        async def access_check() -> None:
            problem = await asyncio.to_thread(generation_access_problem, requested_by_user_id, req)
            if problem:
                raise GenerationAccessRevoked(*problem)

        result = await generate_questions_rag(
            req,
            requested_by_user_id=requested_by_user_id,
            progress_callback=report_progress,
            model_snapshot=job.get("model_snapshot"),
            code_model_snapshot=job.get("code_model_snapshot"),
            fallback_model_snapshot=job.get("fallback_model_snapshot"),
            generation_job_id=job_id,
            index_snapshot=job.get("index_snapshot"),
            access_check=access_check if requested_by_user_id is not None else None,
        )
        finished_at = utc_now()
        metrics = build_generation_metrics(
            job,
            processing_started_at=processing_started_at,
            finished_at=finished_at,
            client_telemetry=client_telemetry,
        )
        completed = await asyncio.to_thread(
            update_generation_job,
            job_id,
            status="completed",
            result={
                "status": result.status,
                "data": [item.model_dump() for item in result.data],
                "summary": [item.model_dump() for item in result.summary],
                "warnings": list(result.warnings),
            },
            metrics=metrics,
            worker_id=worker_id,
        )
        if not completed:
            logger.info("Job [%s] đã bị hủy trước khi lưu kết quả hoàn tất", job_id)
            return
        logger.info(
            "Job [%s] hoàn tất thành công: model=%s, questions=%s",
            job_id,
            (job.get("model_snapshot") or {}).get("model_name") or req.model_provider,
            len(result.data),
        )
    except GenerationAccessRevoked as ex:
        logger.warning("Job [%s] dừng vì mất quyền/nguồn (%s): %s", job_id, ex.code, ex)
        await asyncio.to_thread(
            update_generation_job,
            job_id,
            status="completed",
            result=stopped_generation_result(last_progress, ex.code, str(ex)),
            metrics=build_generation_metrics(
                job,
                processing_started_at=processing_started_at,
                finished_at=utc_now(),
                client_telemetry=client_telemetry,
            ),
            worker_id=worker_id,
        )
    except (GenerationOutputError, GenerationDataError) as ex:
        logger.error("Job [%s] thất bại, không thử lại: %s", job_id, ex)
        metrics = build_generation_metrics(
            job,
            processing_started_at=processing_started_at,
            finished_at=utc_now(),
            client_telemetry=client_telemetry,
        )
        await asyncio.to_thread(
            update_generation_job,
            job_id,
            status="failed",
            error_message=str(ex),
            metrics=metrics,
            worker_id=worker_id,
        )
    except Exception as ex:
        logger.exception("Job [%s] thất bại: %s", job_id, ex)
        metrics = build_generation_metrics(
            job,
            processing_started_at=processing_started_at,
            finished_at=utc_now(),
            client_telemetry=client_telemetry,
        )
        saved_drafts = list(last_progress.get("data") or [])
        attempts_left = int(job.get("attempt_count") or 1) < int(job.get("max_attempts") or settings.job_max_attempts)
        if saved_drafts and not attempts_left:
            # Out of retries: keep the drafts already in the bank as a partial result.
            completed = await asyncio.to_thread(
                update_generation_job,
                job_id,
                status="completed",
                result=partial_generation_result(last_progress),
                metrics=metrics,
                worker_id=worker_id,
            )
            logger.warning(
                "Job [%s] dừng giữa chừng; giữ %s câu nháp đã lưu (lưu kết quả: %s)",
                job_id, len(saved_drafts), bool(completed),
            )
            return
        next_status = await asyncio.to_thread(
            retry_or_dead_letter_generation_job,
            job,
            worker_id,
            error_message=str(ex),
            metrics=metrics,
        )
        logger.info("Job [%s] moved to %s", job_id, next_status)
    finally:
        lease_stop.set()
        await heartbeat_task


@router.post(
    "/questions",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Đưa yêu cầu sinh câu hỏi vào hàng đợi",
)
async def api_generate_questions(
    req: QuestionGenerateRequest,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key", max_length=128),
    current_user: CurrentUser = Depends(require_question_generator),
    document_service: DocumentService = Depends(get_document_service),
):
    normalized_key = idempotency_key.strip() if idempotency_key else None
    request_hash = generation_request_hash(req)
    if normalized_key:
        existing = await asyncio.to_thread(
            get_generation_job_by_idempotency,
            current_user.id,
            normalized_key,
        )
        if existing:
            if existing.get("request_hash") and existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=422,
                    detail="Idempotency-Key đã được dùng cho một yêu cầu sinh câu hỏi khác.",
                )
            return JobAcceptedResponse(
                job_id=existing["job_id"],
                status=existing["status"],
                message="Yêu cầu trùng đã được ánh xạ về job hiện có.",
            )
    try:
        for document_id in req.all_document_ids():
            if not await asyncio.to_thread(document_service.can_generate_from, document_id, current_user):
                raise HTTPException(status_code=404, detail="Không tìm thấy tài liệu")
        await asyncio.to_thread(validate_generation_scope, req.all_document_ids(), req.target_clo_ids)
        index_snapshot = await asyncio.to_thread(generation_index_snapshot, req.all_document_ids())
    except DocumentNotIndexedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    active_count = await asyncio.to_thread(count_active_generation_jobs, current_user.id)
    if active_count >= settings.max_active_generation_jobs_per_user:
        raise _active_limit_error()
    try:
        model_snapshot = await asyncio.to_thread(
            resolve_model_snapshot,
            req.model_provider,
            capability=GENERATION_CAPABILITY,
        )
        code_provider = req.code_model_provider
        if code_provider == req.model_provider:
            code_model_snapshot = model_snapshot
        else:
            try:
                code_model_snapshot = await asyncio.to_thread(
                    resolve_model_snapshot, code_provider, capability=GENERATION_CAPABILITY,
                )
            except ValueError as exc:
                # Without a usable code model, code questions use the chosen model.
                logger.warning("Code model %s unavailable, using %s: %s", code_provider, req.model_provider, exc)
                code_provider, code_model_snapshot = req.model_provider, model_snapshot
        fallback_snapshot = None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        job_id = await asyncio.to_thread(
            create_generation_job,
            req.model_copy(update={"code_model_provider": code_provider}).model_dump(mode="json"),
            current_user.id,
            normalized_key,
            model_snapshot=model_snapshot,
            code_model_snapshot=code_model_snapshot,
            fallback_model_snapshot=fallback_snapshot,
            index_snapshot=index_snapshot,
            request_hash=request_hash,
            max_active=settings.max_active_generation_jobs_per_user,
        )
    except ActiveJobLimitError as exc:
        raise _active_limit_error() from exc

    return JobAcceptedResponse(
        job_id=job_id,
        status=GenerationJobStatus.QUEUED,
        message="Yêu cầu sinh câu hỏi đã được đưa vào hàng đợi.",
    )


@router.get(
    "/status/{job_id}",
    response_model=GenerationJobStatusResponse,
    summary="Kiểm tra trạng thái job sinh câu hỏi",
)
async def get_generation_job_status(
    job_id: str,
    current_user: CurrentUser = Depends(require_question_generator),
):
    can_manage_all = current_user.role == "Admin" or has_permission(current_user, "questions.manage_all")
    owner_id = None if can_manage_all else current_user.id
    job = await asyncio.to_thread(get_generation_job, job_id, requested_by_user_id=owner_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy Job ID này trong hệ thống",
        )

    return _build_generation_status_response(job)


@router.post("/status/{job_id}/cancel", response_model=GenerationJobStatusResponse)
async def cancel_generation_job_request(
    job_id: str,
    current_user: CurrentUser = Depends(require_question_generator),
):
    can_manage_all = current_user.role == "Admin" or has_permission(current_user, "questions.manage_all")
    owner_id = None if can_manage_all else current_user.id
    job = await asyncio.to_thread(get_generation_job, job_id, requested_by_user_id=owner_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy Job ID này trong hệ thống")
    await asyncio.to_thread(cancel_generation_job, job_id, requested_by_user_id=owner_id)
    job = await asyncio.to_thread(get_generation_job, job_id, requested_by_user_id=owner_id)
    return _build_generation_status_response(job)


def _build_generation_status_response(job: dict) -> GenerationJobStatusResponse:
    response_kwargs = {
        "job_id": job["job_id"],
        "status": job["status"],
        "outcome": generation_job_outcome(job),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }

    if job["status"] == GenerationJobStatus.COMPLETED.value and job.get("result"):
        result_data = job["result"].get("data", [])
        response_kwargs["data"] = [GeneratedQuestion(**item) for item in result_data]
        result_summary = job["result"].get("summary", [])
        response_kwargs["summary"] = [GenerationPlanSummary(**item) for item in result_summary]
        response_kwargs["warnings"] = [str(item) for item in job["result"].get("warnings") or []]
        response_kwargs["stop_code"] = job["result"].get("stop_code")

    if job.get("metrics"):
        response_kwargs["metrics"] = job["metrics"]
    if job.get("progress"):
        response_kwargs["progress"] = job["progress"]
    if job.get("model_snapshot"):
        snapshot = job["model_snapshot"]
        response_kwargs["model"] = {
            "code": snapshot.get("model_code"),
            "name": snapshot.get("display_name") or snapshot.get("model_name"),
            "version": snapshot.get("model_name"),
            "runtime": snapshot.get("runtime"),
        }

    if job["status"] == GenerationJobStatus.FAILED.value and job.get("error_message"):
        response_kwargs["error_message"] = job["error_message"]

    return GenerationJobStatusResponse(**response_kwargs)


@router.get(
    "/status/{job_id}/events",
    summary="Theo dõi tiến độ job sinh câu hỏi qua Server-Sent Events",
)
async def stream_generation_job_status(
    job_id: str,
    request: Request,
    current_user: CurrentUser = Depends(require_question_generator),
):
    can_manage_all = current_user.role == "Admin" or has_permission(current_user, "questions.manage_all")
    owner_id = None if can_manage_all else current_user.id
    initial_job = await asyncio.to_thread(get_generation_job, job_id, requested_by_user_id=owner_id)
    if not initial_job:
        raise HTTPException(status_code=404, detail="Không tìm thấy Job ID này trong hệ thống")

    async def event_stream():
        last_version = None
        heartbeat_deadline = asyncio.get_running_loop().time() + 15
        while not await request.is_disconnected():
            job = await asyncio.to_thread(get_generation_job, job_id, requested_by_user_id=owner_id)
            if not job:
                yield 'event: error\ndata: {"detail":"Job không còn tồn tại"}\n\n'
                return
            version = (job.get("updated_at"), job.get("status"), json.dumps(job.get("progress"), sort_keys=True))
            now = asyncio.get_running_loop().time()
            if version != last_version:
                payload = _build_generation_status_response(job).model_dump_json()
                yield f"event: status\ndata: {payload}\n\n"
                last_version = version
                heartbeat_deadline = now + 15
            elif now >= heartbeat_deadline:
                yield ": keep-alive\n\n"
                heartbeat_deadline = now + 15
            if job.get("status") in {"completed", "failed"}:
                return
            await asyncio.sleep(1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
