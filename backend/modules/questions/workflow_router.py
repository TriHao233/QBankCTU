import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from core.config import settings
from core.dependencies import (
    CurrentUser,
    require_admin,
    require_permissions,
    require_reviewer_or_admin,
    require_teacher_reviewer_or_admin,
)
from modules.questions.review_reasons import REVIEW_REASON_CATALOG
from modules.questions.workflow_schemas import (
    AutoAssignRequest,
    AutoEvaluationRequest,
    EvaluationCreateRequest,
    MoodlePublicationRequest,
    QuestionCommentCreateRequest,
    QuestionCommentUpdateRequest,
    ReviewAssignmentRequest,
    ReviewClaimRequest,
    ReviewCreateRequest,
    ReviewDraftUpsertRequest,
    ReviewPolicyPayload,
    SecondaryReviewRequest,
)
from modules.questions.workflow_service import (
    QuestionWorkflowService,
    get_workflow_service,
)

require_moodle_publisher = require_permissions("reviews.manage", "questions.export_moodle")
router = APIRouter(prefix=f"{settings.api_prefix}/questions", tags=["Question workflow"])


def _translate_workflow_error(exc: Exception):
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError) and str(exc) == "VERSION_CONFLICT":
        raise HTTPException(status_code=409, detail="Phiên bản câu hỏi đã thay đổi") from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise exc


@router.get("/review-dashboard")
def review_dashboard(
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.review_dashboard(current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/review-assignments/auto")
def auto_assign_reviews(
    payload: AutoAssignRequest,
    current_user: CurrentUser = Depends(require_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.auto_assign_reviews(payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/review-subject-suggestions")
def review_subject_suggestions(
    reviewer_user_id: str = Query(..., min_length=1, max_length=120),
    current_user: CurrentUser = Depends(require_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.suggest_review_subjects(reviewer_user_id)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/review-reasons")
def list_review_reasons(
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
):
    return {"items": list(REVIEW_REASON_CATALOG)}


@router.get("/review-policy")
def get_review_policy(
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    return service.get_review_policy()


@router.put("/review-policy")
def update_review_policy(
    payload: ReviewPolicyPayload,
    current_user: CurrentUser = Depends(require_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.update_review_policy(payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/review-draft")
def get_review_draft(
    question_id: str,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"item": service.get_review_draft(question_id, current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.put("/{question_id}/review-draft")
def save_review_draft(
    question_id: str,
    payload: ReviewDraftUpsertRequest,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.save_review_draft(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.delete("/{question_id}/review-draft")
def delete_review_draft(
    question_id: str,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"deleted": service.delete_review_draft(question_id, current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/evaluations", status_code=201)
def evaluate_question(
    question_id: str,
    payload: EvaluationCreateRequest,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.evaluate_manual(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/evaluations/auto", status_code=status.HTTP_202_ACCEPTED)
async def auto_evaluate_question(
    question_id: str,
    payload: AutoEvaluationRequest,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        await asyncio.to_thread(service.ensure_question_readable, question_id, current_user)
        job = await asyncio.to_thread(
            service.enqueue_auto_evaluation,
            question_id,
            expected_version=payload.expected_version,
            requested_by_user_id=current_user.id,
            evaluator_model_code=payload.evaluator_model_code,
            fallback_to_heuristic=payload.fallback_to_heuristic,
            trigger="REVIEWER_REQUEST",
        )
        return job
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/evaluations")
def evaluation_history(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"items": service.history(question_id, "evaluations", current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/reviews", status_code=201)
def review_question(
    question_id: str,
    payload: ReviewCreateRequest,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.review(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/review-assignment/claim")
def claim_review_question(
    question_id: str,
    payload: ReviewClaimRequest | None = None,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.claim_review(question_id, current_user, reason=payload.reason if payload else None)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/review-assignment/release")
def release_review_question(
    question_id: str,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.release_review(question_id, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/review-assignment/renew")
def renew_review_question(
    question_id: str,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.renew_review(question_id, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/review-assignment")
def assign_review_question(
    question_id: str,
    payload: ReviewAssignmentRequest,
    current_user: CurrentUser = Depends(require_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.assign_review(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/reviews")
def review_history(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"items": service.history(question_id, "reviews", current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/comments")
def comment_thread(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.list_comments(question_id, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/comments", status_code=201)
def add_question_comment(
    question_id: str,
    payload: QuestionCommentCreateRequest,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.add_comment(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.patch("/{question_id}/comments/{comment_id}")
def update_question_comment(
    question_id: str,
    comment_id: str,
    payload: QuestionCommentUpdateRequest,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.update_comment(question_id, comment_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.delete("/{question_id}/comments/{comment_id}")
def delete_question_comment(
    question_id: str,
    comment_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"deleted": service.delete_comment(question_id, comment_id, current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/secondary-review")
def set_secondary_review(
    question_id: str,
    payload: SecondaryReviewRequest,
    current_user: CurrentUser = Depends(require_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return service.set_secondary_review(question_id, payload, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.post("/{question_id}/moodle-publications", status_code=201)
def publish_question_to_moodle(
    question_id: str,
    payload: MoodlePublicationRequest,
    current_user: CurrentUser = Depends(require_moodle_publisher),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        service.ensure_question_readable(question_id, current_user)
        return service.publish_to_moodle(question_id, payload, current_user.id, current_user.role)
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/moodle-publications")
def moodle_publication_history(
    question_id: str,
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        return {"items": service.history(question_id, "publications", current_user)}
    except Exception as exc:
        _translate_workflow_error(exc)


@router.get("/{question_id}/moodle-export")
def export_question_to_moodle(
    question_id: str,
    format: str = Query("gift", pattern="^(gift|xml)$"),
    current_user: CurrentUser = Depends(require_teacher_reviewer_or_admin),
    service: QuestionWorkflowService = Depends(get_workflow_service),
):
    try:
        export = service.export_moodle(question_id, format, current_user)
    except Exception as exc:
        _translate_workflow_error(exc)
    return Response(
        content=export["content"],
        media_type=export["media_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{export["filename"]}"',
        },
    )
