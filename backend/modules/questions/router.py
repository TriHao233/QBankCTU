import logging
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse

from core.config import settings
from core.dependencies import (
    CurrentUser,
    require_bank_sharing,
    require_question_author,
    require_roles,
    require_teacher_reviewer_or_admin,
)
from modules.questions.schemas import (
    QuestionCreateRequest,
    QuestionListResponse,
    QuestionResponse,
    QuestionOwnerTransferRequest,
    QuestionSharingRequest,
    QuestionSourceViewerResponse,
    QuestionSubmitRequest,
    QuestionVersionResponse,
    QuestionUpdateRequest,
)
from modules.documents.storage import storage_for_provider
from modules.questions.repository import REVIEW_IN_PROGRESS
from modules.questions.service import QuestionService, get_question_service
from modules.notifications.service import (
    NotificationService,
    safe_notify_exam_owners_question_reopened,
    safe_notify_question_resubmitted,
    safe_notify_question_submitted,
)
from modules.questions.workflow_service import (
    QuestionWorkflowService,
    get_workflow_service,
    notification_outbox,
)

router = APIRouter(prefix=f"{settings.api_prefix}/questions", tags=["Questions"])
logger = logging.getLogger(__name__)


def _content_disposition(filename: str) -> str:
    # Same rule as FileResponse: RFC 5987 encoding for non-ASCII names.
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def _collect_notifications(workflow_service: QuestionWorkflowService, build) -> list[dict] | None:
    """Build notifications to store with the question change (PostgreSQL only).

    Returns None when they must be sent after the change instead. A failure
    while building never blocks the teacher's edit or submission.
    """
    outbox = notification_outbox()
    if outbox is not None:
        try:
            build(NotificationService(workflow_service.db, sink=outbox))
        except Exception as exc:
            logger.warning("Failed to prepare question notifications: %s", exc)
            outbox.clear()
    return outbox


def _after_pending_question_edited(question_id: str, question: dict, current_user: CurrentUser,
                                   workflow_service: QuestionWorkflowService) -> None:
    """A question edited while PENDING keeps its place in the queue: re-run AI and tell its reviewer."""
    try:
        workflow_service.enqueue_auto_evaluation(
            question_id,
            expected_version=question["current_version"],
            requested_by_user_id=current_user.id,
            evaluator_model_code=settings.evaluation_model_provider,
            trigger="PENDING_EDIT",
        )
    except Exception:
        logger.exception("Could not enqueue evaluation after editing pending question %s", question_id)
    try:
        pair = workflow_service.questions.find_pair(question_id)
        if pair:
            NotificationService(workflow_service.db).notify_pending_question_edited(
                question=pair[0], version=pair[1], actor_user_id=current_user.id,
            )
    except Exception as exc:
        logger.warning("Failed to notify reviewer about edited pending question: %s", exc)


@router.get("", response_model=QuestionListResponse)
def list_questions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    review_status: str | None = Query(None),
    search: str | None = Query(None),
    question_type: str | None = Query(None),
    bloom_level: int | None = Query(None, ge=1, le=6),
    document_id: str | None = Query(None),
    subject_id: str | None = Query(None),
    chapter_id: str | None = Query(None),
    clo_id: str | None = Query(None),
    difficulty: str | None = Query(None),
    quality_color: str | None = Query(None),
    min_score: float | None = Query(None, ge=0, le=1),
    publication_status: str | None = Query(None),
    evaluation_status: str | None = Query(None),
    assignment_status: str | None = Query(None),
    assigned_to: str | None = Query(None),
    creator_user_id: str | None = Query(None),
    waiting_hours_min: float | None = Query(None, ge=0),
    overdue_only: bool = Query(False),
    sla_breached_only: bool = Query(False),
    override_only: bool = Query(False),
    created_from: datetime | None = Query(None),
    created_to: datetime | None = Query(None),
    submitted_from: datetime | None = Query(None),
    submitted_to: datetime | None = Query(None),
    include_status_counts: bool = Query(False),
    sort_by: str = Query("priority"),
    source_presence: str | None = Query(None),
    secondary_status: str | None = Query(None),
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionService = Depends(get_question_service),
):
    try:
        return service.list(
            page,
            page_size,
            review_status,
            search,
            question_type=question_type,
            bloom_level=bloom_level,
            document_id=document_id,
            subject_id=subject_id,
            chapter_id=chapter_id,
            clo_id=clo_id,
            difficulty=difficulty,
            quality_color=quality_color,
            min_score=min_score,
            publication_status=publication_status,
            evaluation_status=evaluation_status,
            assignment_status=assignment_status,
            assigned_to=assigned_to,
            creator_user_id=creator_user_id,
            waiting_hours_min=waiting_hours_min,
            overdue_only=overdue_only,
            sla_breached_only=sla_breached_only,
            override_only=override_only,
            created_from=created_from,
            created_to=created_to,
            submitted_from=submitted_from,
            submitted_to=submitted_to,
            include_status_counts=include_status_counts,
            sort_by=sort_by,
            source_presence=source_presence,
            secondary_status=secondary_status,
            current_user=current_user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("", response_model=QuestionResponse, status_code=status.HTTP_201_CREATED)
def create_question(
    payload: QuestionCreateRequest,
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
):
    try:
        return service.create(
            payload,
            current_user.id,
            actor_role=current_user.role,
            current_user=current_user,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/archived", response_model=QuestionListResponse)
def list_archived_questions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
):
    return service.list_archived(page, page_size, current_user)


def _raise_question_error(exc: Exception):
    if isinstance(exc, RuntimeError) and str(exc) == "VERSION_CONFLICT":
        raise HTTPException(status_code=409, detail="Câu hỏi đã được cập nhật bởi người khác") from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.post("/{question_id}/restore", response_model=QuestionResponse)
def restore_question(
    question_id: str,
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.restore(question_id, current_user)
    except Exception as exc:
        _raise_question_error(exc)
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi đã lưu trữ")
    return question


@router.post("/{question_id}/withdraw", response_model=QuestionResponse)
def withdraw_question_submission(
    question_id: str,
    expected_version: int | None = Query(None, ge=1),
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.withdraw_submission(question_id, expected_version, current_user)
    except Exception as exc:
        _raise_question_error(exc)
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.get("/{question_id}", response_model=QuestionResponse)
def get_question(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.get(question_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.post(
    "/{question_id}/duplicate",
    response_model=QuestionResponse,
    status_code=status.HTTP_201_CREATED,
)
def duplicate_question(
    question_id: str,
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.duplicate(question_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.get("/{question_id}/versions", response_model=list[QuestionVersionResponse])
def list_question_versions(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionService = Depends(get_question_service),
):
    try:
        versions = service.versions(question_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if versions is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return versions


@router.get("/{question_id}/sources", response_model=QuestionSourceViewerResponse)
def get_question_sources(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionService = Depends(get_question_service),
):
    try:
        sources = service.source_viewer(question_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not sources:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return sources


@router.get("/{question_id}/source-pdf")
def get_question_source_pdf(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionService = Depends(get_question_service),
):
    try:
        artifact = service.source_pdf_artifact(question_id, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not artifact:
        raise HTTPException(status_code=404, detail="Không tìm thấy PDF nguồn")
    storage = storage_for_provider(artifact["provider"])
    if not storage.exists(artifact["uri"]):
        raise HTTPException(status_code=404, detail="File PDF nguồn không còn tồn tại")
    if artifact["provider"] == "LOCAL":
        return FileResponse(
            artifact["uri"],
            media_type=artifact["mime_type"],
            filename=artifact["filename"],
        )
    # Object storage is streamed through the API so access checks and CORS stay here.
    return StreamingResponse(
        storage.iter_bytes(artifact["uri"]),
        media_type=artifact["mime_type"],
        headers={"Content-Disposition": _content_disposition(artifact["filename"])},
    )


@router.patch("/{question_id}", response_model=QuestionResponse)
def update_question(
    question_id: str,
    payload: QuestionUpdateRequest,
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
    workflow_service: QuestionWorkflowService = Depends(get_workflow_service),
):
    outbox = _collect_notifications(
        workflow_service,
        lambda notifier: notifier.notify_exam_owners_question_reopened(
            question_id=question_id,
            question_code=(workflow_service.questions.find_pair(question_id) or [{}])[0]
            .get("question_code") or "Câu hỏi",
            actor_user_id=current_user.id,
        ),
    )
    try:
        question = service.update(
            question_id,
            payload,
            current_user.id,
            actor_role=current_user.role,
            current_user=current_user,
            notifications=outbox,
        )
    except RuntimeError as exc:
        if str(exc) == "VERSION_CONFLICT":
            raise HTTPException(status_code=409, detail="Câu hỏi đã được cập nhật bởi người khác") from exc
        if str(exc) == REVIEW_IN_PROGRESS:
            raise HTTPException(status_code=409, detail={
                "code": REVIEW_IN_PROGRESS,
                "message": "Reviewer đang duyệt câu hỏi này; chờ họ quyết định hoặc trả lại rồi sửa.",
            }) from exc
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    if question.get("review_status") == "PENDING":
        _after_pending_question_edited(question_id, question, current_user, workflow_service)
        question = service.get(question_id, current_user) or question
    # Open exams keep the approved version; warn their owners that a newer draft exists.
    if outbox is None:
        safe_notify_exam_owners_question_reopened(
            database=workflow_service.db,
            question_id=question_id,
            question_code=question.get("question_code") or "Câu hỏi",
            actor_user_id=current_user.id,
        )
    return question


@router.patch("/{question_id}/sharing", response_model=QuestionResponse)
def update_question_sharing(
    question_id: str,
    payload: QuestionSharingRequest,
    current_user: CurrentUser = Depends(require_bank_sharing),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.update_sharing(question_id, payload, current_user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.post("/{question_id}/transfer-owner", response_model=QuestionResponse)
def transfer_question_owner(
    question_id: str,
    payload: QuestionOwnerTransferRequest,
    current_user: CurrentUser = Depends(require_roles("Admin")),
    service: QuestionService = Depends(get_question_service),
):
    try:
        question = service.transfer_owner(question_id, payload, current_user)
    except Exception as exc:
        _raise_question_error(exc)
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.post("/{question_id}/submit-review", response_model=QuestionResponse)
def submit_question_for_review(
    question_id: str,
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
    workflow_service: QuestionWorkflowService = Depends(get_workflow_service),
    payload: QuestionSubmitRequest | None = None,
):
    try:
        previous_question = service.get(question_id, current_user)
        previous_review_status = previous_question.get("review_status") if previous_question else None
        outbox = _collect_notifications(
            workflow_service,
            lambda notifier: notifier.notify_question_submitted(
                question_id=question_id, previous_review_status=previous_review_status,
                actor_user_id=current_user.id,
            ),
        )
        question = service.submit_for_review(
            question_id, current_user, notifications=outbox,
            response_note=payload.response_note if payload else "",
        )
        if question:
            if (
                previous_review_status != "PENDING"
                and question.get("evaluation_status") != "PASSED"
            ):
                try:
                    workflow_service.enqueue_auto_evaluation(
                        question_id,
                        expected_version=question["current_version"],
                        requested_by_user_id=current_user.id,
                        evaluator_model_code=settings.evaluation_model_provider,
                        trigger="REVIEW_SUBMISSION",
                    )
                except Exception as evaluation_exc:
                    logger.exception(
                        "Could not enqueue evaluation after review submission for %s",
                        question_id,
                    )
                    try:
                        workflow_service.mark_evaluation_enqueue_error(
                            question_id,
                            expected_version=question["current_version"],
                            evaluator_model_code=settings.evaluation_model_provider,
                            message=str(evaluation_exc),
                        )
                    except Exception:
                        # Submission already succeeded.  A secondary failure
                        # while recording AI state must not turn it into a 500.
                        logger.exception(
                            "Could not persist evaluation enqueue error for %s",
                            question_id,
                        )
                question = service.get(question_id, current_user)
            if outbox is None:
                safe_notify_question_submitted(
                    database=workflow_service.db,
                    question_id=question_id,
                    previous_review_status=previous_review_status,
                    actor_user_id=current_user.id,
                )
    except RuntimeError as exc:
        if str(exc) == "VERSION_CONFLICT":
            raise HTTPException(status_code=409, detail="Câu hỏi đã được cập nhật bởi người khác") from exc
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not question:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
    return question


@router.delete("/{question_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_question(
    question_id: str,
    reason: str | None = Query(None, max_length=500),
    current_user: CurrentUser = Depends(require_question_author),
    service: QuestionService = Depends(get_question_service),
    workflow_service: QuestionWorkflowService = Depends(get_workflow_service),
):
    def notify_displaced(question, version, reviewer_user_id, archive_reason):
        workflow_service.notify_takeover(
            question, version, reviewer_user_id, current_user, archive_reason, archived=True,
        )

    try:
        deleted = service.archive(
            question_id, current_user, reason=reason, on_reviewer_displaced=notify_displaced,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if str(exc) == "VERSION_CONFLICT":
            raise HTTPException(status_code=409, detail="Trạng thái câu hỏi vừa thay đổi, vui lòng tải lại") from exc
        raise
    if not deleted:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi")
