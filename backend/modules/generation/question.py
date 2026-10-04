import asyncio
import json
import re
import logging
import time
from collections.abc import Awaitable, Callable
from typing import List

from core.config import settings
from modules.generation.schemas import (
    QuestionPlanItem,
    QuestionGenerateRequest,
    QuestionGenerateResponse,
    GeneratedQuestion,
    GenerationRejection,
    GenerationPlanSummary,
)
from modules.generation.postprocessing import (
    POSTPROCESSOR_VERSION,
    DuplicateStats,
    filter_duplicate_questions,
    normalize_exact_text,
    question_fingerprint,
    rejection_counts,
    validate_source_grounding,
    validate_question_code,
    validate_true_false_clarity,
)
from modules.generation.prompt_builder import PromptBuilder
from modules.rag.search import get_context_snapshot
from modules.generation.llm.factory import (
    get_llm_execution_snapshot,
    get_llm_service,
    reset_llm_execution_tracking,
)
from modules.generation.llm.model_registry import (
    GENERATION_CAPABILITY,
    resolve_model_snapshot,
)
from modules.generation.mongodb import (
    create_generation_run,
    finish_generation_run,
    get_existing_question_texts,
    get_document_learning_outcomes,
    save_generated_questions,
)

logger = logging.getLogger(__name__)
MAX_FORMAT_RETRY_ATTEMPTS = 1
MAX_JSON_REPAIR_ATTEMPTS = 2
VALID_DIFFICULTIES = {"de", "trung_binh", "kho"}
CODE_SIGNAL_PATTERNS = (
    r"```",
    r"\b(?:def|class|function|struct|typedef|return|while|for|if)\b",
    r"#include\s*[<\"]",
    r"\b(?:int|float|double|char|bool|void)\s+[A-Za-z_]\w*\s*[\(;=]",
    r"(?:->|::|\{\s*$|;\s*$)",
)
DIFFICULTY_ALIASES = {
    "de": "de",
    "dễ": "de",
    "easy": "de",
    "trung_binh": "trung_binh",
    "trung binh": "trung_binh",
    "trung bình": "trung_binh",
    "medium": "trung_binh",
    "kho": "kho",
    "khó": "kho",
    "hard": "kho",
}

QUESTION_TYPE_RETRY_RULES = {
    "trac_nghiem": 'options must contain exactly "A", "B", "C", "D"; correct_answer must be one key.',
    "tinh_huong": 'options must contain exactly "A", "B", "C", "D"; correct_answer must be one key.',
    "dung_sai": 'options must contain exactly {"A": "Đúng", "B": "Sai"}; question must be one complete true/false statement; source_context must be a verbatim CONTEXT quote; source_keywords must occur exactly in both source_context and question; false answers require one false_mutation.',
    "nhieu_lua_chon": 'options must contain 4 to 6 consecutive keys from "A"; correct_answer must contain at least two comma-separated keys but not every option key.',
    "dien_khuyet": 'options must be null; question text must contain "_____".',
    "ghep_cot": "options must be a matching object with numbered keys and extra lettered distractors.",
    "sap_xep": "options must contain ordered step keys; correct_answer must list every key in the correct order.",
}


class GenerationOutputError(RuntimeError):
    """The model completed, but its output cannot produce valid questions."""


def _llm_hit_output_limit(llm) -> bool:
    provider = llm
    if getattr(provider, "last_used", None) in {"primary", "fallback"}:
        provider = (
            provider.fallback
            if provider.last_used == "fallback"
            else provider.primary
        )
    metadata = getattr(provider, "last_response_metadata", {}) or {}
    return metadata.get("done_reason") == "length"


def _response_excerpt(text: str, limit: int = 2000) -> str:
    value = str(text or "")
    return value if len(value) <= limit else f"{value[:limit]}... [truncated {len(value) - limit} chars]"


def _content_mode(plan_item: QuestionPlanItem, context_text: str, instruction: str | None) -> str:
    if plan_item.content_mode != "auto":
        return plan_item.content_mode
    sample = f"{instruction or ''}\n{context_text}"
    signal_count = sum(bool(re.search(pattern, sample, flags=re.IGNORECASE | re.MULTILINE)) for pattern in CODE_SIGNAL_PATTERNS)
    return "code" if signal_count >= 2 else "general"


def _context_sections(context_text: str) -> list[str]:
    return [
        section.strip()
        for section in re.split(r"\n\s*---\s*\n", str(context_text or ""))
        if section.strip()
    ]


def _focused_context_snapshot(context_snapshot: dict, question_offset: int) -> tuple[dict, str]:
    sections = _context_sections(context_snapshot.get("context_text") or "")
    results = context_snapshot.get("results") or []
    if len(sections) <= 1:
        return context_snapshot, "Use the available CONTEXT, but choose a different tested idea from any prior question."

    section_index = question_offset % len(sections)
    focused_snapshot = {
        **context_snapshot,
        "context_text": sections[section_index],
        "results": [results[section_index]] if section_index < len(results) else results,
    }
    return (
        focused_snapshot,
        f"Use primarily context section {section_index + 1} of {len(sections)} for this question.",
    )

async def generate_questions_rag(
    req: QuestionGenerateRequest,
    requested_by_user_id=None,
    progress_callback: Callable[[dict], Awaitable[None]] | None = None,
    model_snapshot: dict | None = None,
    code_model_snapshot: dict | None = None,
    fallback_model_snapshot: dict | None = None,
) -> QuestionGenerateResponse:
    plan = req.effective_plan()
    plan_log = ", ".join(
        (
            f"{item.question_type.value}/{(item.bloom_level or req.bloom_level).value}"
            f"/{item.difficulty or req.difficulty or 'auto'}:{item.num_questions}"
        )
        for item in plan
    )
    logger.info("Sinh câu hỏi [Doc: %s | Plan: %s]", req.document_id, plan_log)

    # 1. Truy xuất ngữ cảnh (RAG)
    # Retrieval can perform synchronous embedding work. Keep it off the event
    # loop so a Gemini lane remains dispatchable while Ollama is streaming (and
    # vice versa).
    model_runtime = str((model_snapshot or {}).get("runtime") or "").upper()
    code_runtime = str((code_model_snapshot or model_snapshot or {}).get("runtime") or "").upper()
    gemini_only_request = model_runtime == "GEMINI" and code_runtime == "GEMINI"
    context_snapshot = await asyncio.to_thread(
        get_context_snapshot,
        document_id=req.document_id,
        collection_name=req.collection_name,
        target_heading=req.target_heading,
        query_text=req.instruction,
        avoid_ollama_gpu_wait=gemini_only_request,
    )
    context_text = context_snapshot["context_text"]

    if not context_text:
        raise ValueError("Không tìm thấy đủ dữ liệu tri thức để sinh câu hỏi.")

    prompt_builder = PromptBuilder()
    learning_outcomes = get_document_learning_outcomes(req.document_id)
    if req.clo_ids:
        selected = {str(item).strip() for item in req.clo_ids if str(item).strip()}
        filtered = [
            item for item in learning_outcomes
            if str(item.get("id") or item.get("_id") or "").strip() in selected
        ]
        if not filtered:
            raise ValueError("CLO đã chọn không thuộc học phần của tài liệu này.")
        learning_outcomes = filtered
    generated_questions: List[GeneratedQuestion] = []
    summaries: List[GenerationPlanSummary] = []
    try:
        existing_questions = get_existing_question_texts(req.document_id)
    except Exception as exc:
        logger.warning("Không tải được câu hỏi hiện có để near-dedup: %s", exc)
        existing_questions = []
    seen_question_fingerprints = {
        fingerprint
        for question in existing_questions
        if (fingerprint := question_fingerprint(question))
    }

    total_questions = sum(item.num_questions for item in plan)
    completed_questions = 0
    if progress_callback:
        await progress_callback(
            {
                "stage": "generating",
                "completed": 0,
                "total": total_questions,
                "generated_questions": 0,
                "data": [],
                "summary": [],
            }
        )

    for plan_index, plan_item in enumerate(plan, start=1):
        content_mode = _content_mode(plan_item, context_text, req.instruction)
        selected_provider = req.model_provider
        selected_snapshot = model_snapshot or resolve_model_snapshot(
            selected_provider, capability=GENERATION_CAPABILITY
        )
        llm = get_llm_service(
            selected_provider,
            None,
            model_snapshot=selected_snapshot,
            fallback_model_snapshot=None,
        )
        item_summaries: list[GenerationPlanSummary] = []
        # Local 7B models are more reliable when each response contains one question.
        # Remote providers keep the configured batching to reduce API requests.
        configured_batch_size = (
            settings.ollama_generation_batch_size
            if str((selected_snapshot or {}).get("runtime") or "").upper() == "OLLAMA"
            else settings.generation_batch_size
        )
        batch_size = max(1, min(configured_batch_size, plan_item.num_questions))
        for batch_start in range(0, plan_item.num_questions, batch_size):
            batch_count = min(batch_size, plan_item.num_questions - batch_start)
            focused_snapshot, focus_directive = _focused_context_snapshot(
                context_snapshot,
                completed_questions,
            )
            batch_item = plan_item.model_copy(update={"num_questions": batch_count})
            questions, batch_summary = await _generate_questions_for_plan_item(
                req,
                batch_item,
                plan_index=plan_index,
                avoid_questions=[
                    *existing_questions[:12],
                    *(question.question for question in generated_questions),
                ],
                avoid_source_contexts=[
                    question.source_context for question in generated_questions
                ],
                seen_question_fingerprints=seen_question_fingerprints,
                context_snapshot=focused_snapshot,
                context_text=focused_snapshot["context_text"],
                prompt_builder=prompt_builder,
                llm=llm,
                model_snapshot=selected_snapshot,
                model_provider=selected_provider,
                content_mode=content_mode,
                focus_directive=focus_directive,
                learning_outcomes=learning_outcomes,
                requested_by_user_id=requested_by_user_id,
            )
            for question in questions:
                seen_question_fingerprints.add(question_fingerprint(question.question))
            generated_questions.extend(questions)
            item_summaries.append(batch_summary)
            completed_questions += batch_count

            current_summary = _aggregate_plan_summaries(
                item_summaries,
                plan_index=plan_index,
                question_type=plan_item.question_type.value,
                bloom_level=(plan_item.bloom_level or req.bloom_level).value,
                difficulty=plan_item.difficulty or req.difficulty,
                requested_count=plan_item.num_questions,
                model_provider=selected_provider,
                content_mode=content_mode,
            )
            if progress_callback:
                await progress_callback(
                    {
                        "stage": "generating",
                        "completed": completed_questions,
                        "total": total_questions,
                        "generated_questions": len(generated_questions),
                        "data": [question.model_dump() for question in generated_questions],
                        "summary": [
                            *(item.model_dump() for item in summaries),
                            current_summary.model_dump(),
                        ],
                    }
                )

        summaries.append(
            _aggregate_plan_summaries(
                item_summaries,
                plan_index=plan_index,
                question_type=plan_item.question_type.value,
                bloom_level=(plan_item.bloom_level or req.bloom_level).value,
                difficulty=plan_item.difficulty or req.difficulty,
                requested_count=plan_item.num_questions,
                model_provider=selected_provider,
                content_mode=content_mode,
            )
        )

    return QuestionGenerateResponse(
        status="success",
        data=generated_questions,
        summary=summaries,
    )


async def _generate_questions_for_plan_item(
    req: QuestionGenerateRequest,
    plan_item: QuestionPlanItem,
    *,
    plan_index: int,
    avoid_questions: list[str],
    avoid_source_contexts: list[str] | None = None,
    seen_question_fingerprints: set[str],
    context_snapshot: dict,
    context_text: str,
    prompt_builder: PromptBuilder,
    llm,
    model_snapshot: dict | None,
    model_provider: str,
    content_mode: str,
    focus_directive: str | None = None,
    learning_outcomes: list[dict],
    requested_by_user_id=None,
) -> tuple[List[GeneratedQuestion], GenerationPlanSummary]:
    reset_llm_execution_tracking(llm)
    bloom_level = plan_item.bloom_level or req.bloom_level
    difficulty = plan_item.difficulty or req.difficulty
    # 2. Xây dựng Prompt thông qua hệ thống file-based cho từng dạng câu hỏi
    chat_prompt = prompt_builder.build_chat(
        context=context_text,
        bloom_level=bloom_level.value,
        question_type=plan_item.question_type.value,
        num_questions=plan_item.num_questions,
        difficulty=difficulty,
        instruction=req.instruction,
        avoid_questions=avoid_questions,
        avoid_source_contexts=avoid_source_contexts or [],
        learning_outcomes=learning_outcomes,
        content_mode=content_mode,
        focus_directive=focus_directive,
    )
    full_prompt = chat_prompt.rendered_snapshot()
    request_snapshot = req.model_dump(mode="json")
    request_snapshot["active_plan_item"] = {
        **plan_item.model_dump(mode="json"),
        "effective_bloom_level": bloom_level.value,
        "effective_difficulty": difficulty,
    }
    generation_run_id = create_generation_run(
        document_id=req.document_id,
        requested_by_user_id=requested_by_user_id,
        request_snapshot=request_snapshot,
        model_snapshot=model_snapshot or {"provider": req.model_provider},
        rendered_prompt=full_prompt,
        context_text=context_text,
        retrieval_results=context_snapshot["results"],
        chunk_set_id=context_snapshot["chunk_set_id"],
        vector_collection_id=context_snapshot["vector_collection_id"],
    )

    # 3. Gọi LLM
    started_at = time.perf_counter()
    try:
        raw_response = await llm.generate_chat(
            system_prompt=chat_prompt.system_prompt,
            user_prompt=chat_prompt.user_prompt,
            output_schema=chat_prompt.output_schema,
        )
    except Exception as exc:
        finish_generation_run(
            generation_run_id,
            status="FAILED",
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            error_message=str(exc),
            model_execution=get_llm_execution_snapshot(llm),
        )
        raise

    # 4. Làm sạch và Parse JSON
    clean_json_str = _clean_llm_output(raw_response)
    repair_attempt_count = 0
    retry_attempt_count = 0

    try:
        for json_attempt in range(MAX_JSON_REPAIR_ATTEMPTS + 1):
            try:
                parsed_data = json.loads(clean_json_str)
                break
            except json.JSONDecodeError as json_error:
                if _llm_hit_output_limit(llm):
                    logger.error(
                        "%s đã chạm giới hạn đầu ra; không lặp lại yêu cầu JSON lỗi",
                        model_provider,
                    )
                    raise
                if json_attempt >= MAX_JSON_REPAIR_ATTEMPTS:
                    raise
                # A local model can reach its token boundary after producing most
                # of the object. Ask for a fresh response before rejecting it.
                repair_attempt_count += 1
                retry_attempt_count += 1
                logger.warning(
                    "JSON không hợp lệ; yêu cầu %s sinh lại (%s/%s): %s",
                    model_provider,
                    json_attempt + 1,
                    MAX_JSON_REPAIR_ATTEMPTS,
                    json_error,
                )
                retry_prompt = _build_retry_prompt(
                    original_prompt=chat_prompt.user_prompt,
                    question_type=plan_item.question_type.value,
                    bloom_level=bloom_level.value,
                    difficulty=difficulty,
                    missing_count=plan_item.num_questions,
                    validation_errors=[
                        GenerationRejection(
                            code="INVALID_JSON_RESPONSE",
                            message=(
                                "Phản hồi trước bị cắt hoặc không phải JSON hợp lệ. "
                                "Hãy sinh lại toàn bộ và chỉ trả về một JSON object hoàn chỉnh."
                            ),
                            repairable=True,
                        )
                    ],
                    avoid_questions=avoid_questions,
                )
                retry_raw_response = await llm.generate_chat(
                    system_prompt=chat_prompt.system_prompt,
                    user_prompt=retry_prompt,
                    output_schema=chat_prompt.output_schema,
                )
                raw_response = (
                    f"{raw_response}\n\n--- JSON REPAIR {json_attempt + 1} ---\n"
                    f"{retry_raw_response}"
                )
                clean_json_str = _clean_llm_output(retry_raw_response)
        questions_list = _extract_questions_list(parsed_data)
        parsed_count = len(questions_list)

        # Validate và đóng gói. Nếu AI trả dư, phần dư được dùng để bù câu sai format/trùng.
        validated_data, validation_errors = _validate_and_format(
            questions_list,
            question_type=plan_item.question_type.value,
            bloom_level=bloom_level.value,
            requested_difficulty=difficulty,
            context_text=context_text,
        )
        postprocessed_count = len(validated_data)
        deduped_data, duplicate_stats = filter_duplicate_questions(
            validated_data,
            seen_question_fingerprints,
            limit=plan_item.num_questions,
        )
        if len(deduped_data) < plan_item.num_questions:
            missing_count = plan_item.num_questions - len(deduped_data)
            fallback_candidates: list[GeneratedQuestion] = [
                question
                for question in validated_data
                if question not in deduped_data
            ]
            for retry_index in range(1, MAX_FORMAT_RETRY_ATTEMPTS + 1):
                repair_attempt_count += 1
                retry_attempt_count += 1
                retry_started_at = time.perf_counter()
                retry_prompt = _build_retry_prompt(
                    original_prompt=chat_prompt.user_prompt,
                    question_type=plan_item.question_type.value,
                    bloom_level=bloom_level.value,
                    difficulty=difficulty,
                    missing_count=missing_count,
                    validation_errors=validation_errors,
                    avoid_questions=[
                        *avoid_questions,
                        *(question.question for question in deduped_data),
                    ],
                )
                try:
                    retry_raw_response = await llm.generate_chat(
                        system_prompt=chat_prompt.system_prompt,
                        user_prompt=retry_prompt,
                        output_schema=chat_prompt.output_schema,
                    )
                    raw_response = (
                        f"{raw_response}\n\n--- FORMAT RETRY {retry_index} ---\n"
                        f"{retry_raw_response}"
                    )
                    retry_data = json.loads(_clean_llm_output(retry_raw_response))
                    retry_questions = _extract_questions_list(retry_data)
                    parsed_count += len(retry_questions)
                    retry_validated, retry_errors = _validate_and_format(
                        retry_questions,
                        question_type=plan_item.question_type.value,
                        bloom_level=bloom_level.value,
                        requested_difficulty=difficulty,
                        context_text=context_text,
                    )
                    postprocessed_count += len(retry_validated)
                    fallback_candidates.extend(retry_validated)
                    retry_deduped, retry_duplicate_stats = filter_duplicate_questions(
                        retry_validated,
                        seen_question_fingerprints,
                        limit=missing_count,
                    )
                    deduped_data.extend(retry_deduped)
                    validation_errors.extend(retry_errors)
                    duplicate_stats = DuplicateStats(
                        exact=duplicate_stats.exact + retry_duplicate_stats.exact,
                        near=duplicate_stats.near + retry_duplicate_stats.near,
                    )
                    missing_count = plan_item.num_questions - len(deduped_data)
                    if missing_count <= 0:
                        break
                except Exception as retry_exc:
                    validation_errors.append(
                        GenerationRejection(
                            code="REPAIR_ATTEMPT_FAILED",
                            message=(
                                f"Retry {retry_index} failed after "
                                f"{int((time.perf_counter() - retry_started_at) * 1000)}ms: "
                                f"{retry_exc}"
                            ),
                            repairable=False,
                        )
                    )
                    break
            if missing_count > 0 and fallback_candidates:
                fallback_deduped, _fallback_duplicate_stats = filter_duplicate_questions(
                    fallback_candidates,
                    seen_question_fingerprints,
                    limit=missing_count,
                    keep_duplicates=True,
                )
                deduped_data.extend(fallback_deduped)

        # 5. Chuẩn bị bản nháp tạm; chỉ ghi vào ngân hàng khi giảng viên gửi duyệt.
        saved_data = save_generated_questions(
            req.document_id,
            [q.model_dump() for q in deduped_data],
            generation_run_id=generation_run_id,
            requested_by_user_id=requested_by_user_id,
            source_chunk_ids=[
                result["chunk_id"]
                for result in context_snapshot["results"]
                if result.get("chunk_id")
            ],
            learning_outcomes=learning_outcomes,
        )
        summary = _build_plan_summary(
            plan_index=plan_index,
            question_type=plan_item.question_type.value,
            bloom_level=bloom_level.value,
            difficulty=difficulty,
            requested_count=plan_item.num_questions,
            parsed_count=parsed_count,
            valid_count=postprocessed_count,
            retry_attempt_count=retry_attempt_count,
            duplicate_stats=duplicate_stats,
            saved_count=len(saved_data),
            validation_errors=validation_errors,
            model_provider=model_provider,
            content_mode=content_mode,
        )
        if not saved_data:
            raise GenerationOutputError(
                "Mô hình không tạo được câu hỏi hợp lệ sau bước kiểm tra đầu ra."
            )
        finish_generation_run(
            generation_run_id,
            status="COMPLETED",
            raw_model_response=raw_response,
            generated_count=len(saved_data),
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            validation_errors=[item.model_dump() for item in validation_errors],
            post_processing={
                "validator_version": POSTPROCESSOR_VERSION,
                "parsed_count": parsed_count,
                "validated_count": postprocessed_count,
                "accepted_count": len(saved_data),
                "repair_attempt_count": repair_attempt_count,
                "retry_attempt_count": retry_attempt_count,
                "duplicate_count": duplicate_stats.total,
                "exact_duplicate_count": duplicate_stats.exact,
                "near_duplicate_count": duplicate_stats.near,
            },
            model_execution=get_llm_execution_snapshot(llm),
        )

        return [GeneratedQuestion(**question) for question in saved_data], summary

    except json.JSONDecodeError as exc:
        logger.error(
            "Parse JSON lỗi (response_chars=%s): %s",
            len(clean_json_str),
            _response_excerpt(clean_json_str),
        )
        validation_errors = [
            GenerationRejection(
                code="INVALID_JSON_RESPONSE",
                message=f"LLM trả về JSON không hoàn chỉnh: {exc.msg}.",
                repairable=False,
            )
        ]
        finish_generation_run(
            generation_run_id,
            status="FAILED",
            raw_model_response=_response_excerpt(raw_response, limit=12000),
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            error_message="Invalid JSON response",
            validation_errors=[item.model_dump() for item in validation_errors],
            post_processing={
                "validator_version": POSTPROCESSOR_VERSION,
                "parsed_count": 0,
                "validated_count": 0,
                "accepted_count": 0,
                "repair_attempt_count": repair_attempt_count,
                "retry_attempt_count": retry_attempt_count,
                "duplicate_count": 0,
                "exact_duplicate_count": 0,
                "near_duplicate_count": 0,
            },
            model_execution=get_llm_execution_snapshot(llm),
        )
        raise GenerationOutputError(
            "Mô hình trả về JSON không hoàn chỉnh nên không tạo được câu hỏi hợp lệ."
        ) from exc
    except Exception as exc:
        finish_generation_run(
            generation_run_id,
            status="FAILED",
            raw_model_response=_response_excerpt(raw_response, limit=12000),
            latency_ms=int((time.perf_counter() - started_at) * 1000),
            error_message=str(exc),
            model_execution=get_llm_execution_snapshot(llm),
        )
        raise

def _clean_llm_output(text: str) -> str:
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # Strip only the response wrapper; preserve code fences inside JSON strings.
    text = re.sub(r'^\s*```(?:json)?\s*\n', '', text, count=1, flags=re.IGNORECASE)
    text = re.sub(r'\n\s*```\s*$', '', text, count=1)
    match = re.search(r'(\{.*\}|\[.*\])', text, re.DOTALL)
    return match.group(1) if match else text.strip()

def _extract_questions_list(data: dict | list) -> list:
    if isinstance(data, list): return data
    return data.get("questions") or data.get("data") or []


def _looks_incomplete_true_false_statement(question: str) -> bool:
    normalized = re.sub(r"\s+", " ", (question or "").strip().lower())
    if not normalized:
        return True
    if normalized.endswith("?"):
        return True
    dangling_patterns = (
        r"\b(là|gồm|bao gồm|có|được|bị|khi|nếu|vì|do|của|trong|với|để|bằng|từ|và|hoặc)$",
        r"\b(là một|là các|là những|được gọi là|được sử dụng để)$",
        r"[:;,]$",
    )
    return any(re.search(pattern, normalized) for pattern in dangling_patterns)


def _build_retry_prompt(
    *,
    original_prompt: str,
    question_type: str,
    bloom_level: str,
    missing_count: int,
    validation_errors: list[str | GenerationRejection],
    avoid_questions: list[str],
    difficulty: str | None = None,
) -> str:
    error_messages = [
        error.message if isinstance(error, GenerationRejection) else str(error)
        for error in validation_errors[-5:]
        if error
    ]
    errors = "\n".join(f"- {error}" for error in error_messages) or "- Not enough valid questions were produced."
    rejected_questions = [
        error.question_excerpt
        for error in validation_errors
        if isinstance(error, GenerationRejection) and error.question_excerpt
    ]
    avoid_list = "\n".join(
        f"- {question.strip()}"
        for question in [*avoid_questions, *rejected_questions][-12:]
        if question and question.strip()
    ) or "- None"
    grounding_rule = (
        "- source_context: copy one continuous passage exactly from after Nội dung: in CONTEXT. "
        "Do not include the Nội dung: label, ellipses, or paraphrases; preserve every word. "
        "Choose a different source passage and question if the previous quote cannot be found verbatim."
        if any(
            isinstance(error, GenerationRejection) and error.code == "SOURCE_CONTEXT_NOT_IN_CONTENT"
            for error in validation_errors
        )
        else ""
    )
    type_rule = QUESTION_TYPE_RETRY_RULES.get(question_type, "Follow the QUESTION TYPE rules exactly.")
    difficulty_rule = f"- difficulty: {difficulty}" if difficulty else "- difficulty: apply the difficulty rule"
    return f"""
{original_prompt}

FORMAT REPAIR REQUEST:
The previous response did not produce enough valid questions.
Generate exactly {missing_count} additional questions.

STRICT TARGET:
- question_type: {question_type}
- bloom_level: {bloom_level}
{difficulty_rule}
- required structure: {type_rule}
{grounding_rule}

RECENT VALIDATION ERRORS TO FIX:
{errors}

DO NOT DUPLICATE THESE ACCEPTED/PREVIOUS QUESTIONS:
{avoid_list}

Return ONLY the same raw JSON object shape:
{{"questions": [{{"question": "...", "options": ..., "correct_answer": "...", "explanation": "...", "question_type": "{question_type}", "bloom_level": "{bloom_level}", "difficulty": "de|trung_binh|kho", "source_context": "...", "source_keywords": ["..."], "false_mutation": null}}]}}
"""


def _aggregate_plan_summaries(
    summaries: list[GenerationPlanSummary],
    *,
    plan_index: int,
    question_type: str,
    bloom_level: str,
    difficulty: str | None,
    requested_count: int,
    model_provider: str,
    content_mode: str,
) -> GenerationPlanSummary:
    validation_errors = [
        reason
        for summary in summaries
        for reason in summary.rejection_reasons
    ]
    return _build_plan_summary(
        plan_index=plan_index,
        question_type=question_type,
        bloom_level=bloom_level,
        difficulty=difficulty,
        requested_count=requested_count,
        parsed_count=sum(summary.parsed_count for summary in summaries),
        valid_count=sum(summary.valid_count for summary in summaries),
        retry_attempt_count=sum(summary.retry_attempt_count for summary in summaries),
        duplicate_stats=DuplicateStats(
            exact=sum(summary.exact_duplicate_count for summary in summaries),
            near=sum(summary.near_duplicate_count for summary in summaries),
        ),
        saved_count=sum(summary.saved_count for summary in summaries),
        validation_errors=validation_errors,
        model_provider=model_provider,
        content_mode=content_mode,
    )


def _build_plan_summary(
    *,
    plan_index: int,
    question_type: str,
    bloom_level: str,
    difficulty: str | None,
    requested_count: int,
    parsed_count: int,
    valid_count: int,
    retry_attempt_count: int,
    duplicate_stats: DuplicateStats,
    saved_count: int,
    validation_errors: list[GenerationRejection],
    model_provider: str,
    content_mode: str,
) -> GenerationPlanSummary:
    skipped_count = max(0, requested_count - saved_count)
    warnings = []
    invalid_count = len(validation_errors)
    rejection_summary = rejection_counts(validation_errors)
    if parsed_count < requested_count:
        warnings.append(f"LLM chỉ trả {parsed_count}/{requested_count} câu.")
    if invalid_count:
        warnings.append(f"Ghi nhận {invalid_count} lỗi trong bước sàng lọc sau sinh.")
    if duplicate_stats.total:
        warnings.append(
            f"Ghi nhận {duplicate_stats.total} câu nghi trùng nội dung qua các lượt sinh "
            f"({duplicate_stats.exact} trùng tuyệt đối, {duplicate_stats.near} gần trùng); đã giữ lại để người tạo tự duyệt."
        )
    if retry_attempt_count:
        warnings.append(f"Đã thử sinh bù {retry_attempt_count} lượt để đủ số câu yêu cầu.")
    if skipped_count:
        warnings.append(f"Lưu thiếu {skipped_count} câu so với yêu cầu.")
    warnings.extend(item.message for item in validation_errors[:3])

    return GenerationPlanSummary(
        plan_index=plan_index,
        question_type=question_type,
        bloom_level=bloom_level,
        difficulty=difficulty,
        model_provider=model_provider,
        content_mode=content_mode,
        requested_count=requested_count,
        parsed_count=parsed_count,
        valid_count=valid_count,
        retry_attempt_count=retry_attempt_count,
        duplicate_count=duplicate_stats.total,
        exact_duplicate_count=duplicate_stats.exact,
        near_duplicate_count=duplicate_stats.near,
        format_rejected_count=rejection_summary["format"],
        grounding_rejected_count=rejection_summary["grounding"],
        clarity_rejected_count=rejection_summary["clarity"],
        saved_count=saved_count,
        skipped_count=skipped_count,
        warnings=warnings,
        rejection_reasons=validation_errors,
    )


def _check_type_format(item: dict, question_type: str) -> str | None:
    """Kiểm tra cấu trúc bắt buộc theo từng loại câu hỏi (xem app/prompts/question_structure/*.txt).
    Trả về lý do lỗi nếu vi phạm, None nếu hợp lệ."""
    options = item.get("options")
    correct_answer = item.get("correct_answer") or ""

    if question_type == "dien_khuyet":
        if "_____" not in (item.get("question") or ""):
            return "dien_khuyet thiếu placeholder '_____' trong câu hỏi"
        if options is not None:
            return "dien_khuyet phải có options = null"

    elif question_type == "dung_sai":
        if _looks_incomplete_true_false_statement(item.get("question") or ""):
            return "dung_sai phải là một mệnh đề hoàn chỉnh, không được bỏ lửng như 'X là'"
        if not isinstance(options, dict) or set(options.keys()) != {"A", "B"}:
            return "dung_sai phải có đúng 2 lựa chọn A/B"
        if normalize_exact_text(options.get("A")) != "đúng" or normalize_exact_text(options.get("B")) != "sai":
            return 'dung_sai: options bắt buộc là {"A": "Đúng", "B": "Sai"}'
        if str(correct_answer).strip() not in {"A", "B"}:
            return "dung_sai: correct_answer phải là A hoặc B"

    elif question_type in ("trac_nghiem", "tinh_huong"):
        if not isinstance(options, dict) or set(options.keys()) != {"A", "B", "C", "D"}:
            return f"{question_type} phải có đúng 4 lựa chọn A/B/C/D"

    elif question_type == "nhieu_lua_chon":
        valid_option_sets = (
            {"A", "B", "C", "D"},
            {"A", "B", "C", "D", "E"},
            {"A", "B", "C", "D", "E", "F"},
        )
        if not isinstance(options, dict) or set(options.keys()) not in valid_option_sets:
            return "nhieu_lua_chon phải có 4 đến 6 lựa chọn liên tiếp từ A"
        correct_keys = [c.strip() for c in correct_answer.split(",") if c.strip()]
        if len(correct_keys) < 2:
            return "nhieu_lua_chon phải có ít nhất 2 đáp án đúng"
        if len(correct_keys) >= len(options):
            return "nhieu_lua_chon không được chọn tất cả lựa chọn làm đáp án đúng"
        if any(key not in options for key in correct_keys):
            return "nhieu_lua_chon có đáp án đúng không tồn tại trong options"

    elif question_type == "ghep_cot":
        if not isinstance(options, dict):
            return "ghep_cot phải có options dạng object"
        numeric_keys = [k for k in options if str(k).isdigit()]
        alpha_keys = [k for k in options if str(k).isalpha()]
        if len(numeric_keys) < 3 or len(alpha_keys) < len(numeric_keys) + 1:
            return "ghep_cot cần tối thiểu 3 mục đánh số và số mục chữ phải nhiều hơn số mục số ít nhất 1 (distractor)"
        if not re.search(r"\d+\s*-\s*[a-zA-Z]", correct_answer):
            return "ghep_cot: correct_answer phải theo định dạng '1-b, 2-a, ...'"

    elif question_type == "sap_xep":
        if not isinstance(options, dict) or len(options) < 4:
            return "sap_xep cần tối thiểu 4 bước trong options"
        correct_keys = [c.strip() for c in correct_answer.split(",") if c.strip()]
        if sorted(correct_keys) != sorted(str(k) for k in options.keys()):
            return "sap_xep: correct_answer phải liệt kê đủ và đúng các khóa trong options theo thứ tự"

    return None


def _normalize_difficulty(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_")
    normalized = re.sub(r"\s+", " ", normalized)
    mapped = DIFFICULTY_ALIASES.get(normalized) or DIFFICULTY_ALIASES.get(normalized.replace(" ", "_"))
    if mapped in VALID_DIFFICULTIES:
        return mapped
    return None


def _validate_and_format(
    questions: list,
    *,
    question_type: str,
    bloom_level: str,
    requested_difficulty: str | None = None,
    context_text: str,
) -> tuple[List[GeneratedQuestion], list[GenerationRejection]]:
    """Validate dữ liệu và ép kiểu về model chuẩn. Loại bỏ các câu hỏi không đúng
    cấu trúc bắt buộc của question_structure thay vì lưu dữ liệu hỏng vào ngân hàng câu hỏi."""
    formatted = []
    validation_errors = []
    for candidate_index, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            validation_errors.append(
                GenerationRejection(
                    code="INVALID_CANDIDATE_TYPE",
                    message="Candidate phải là JSON object.",
                    candidate_index=candidate_index,
                    question_excerpt=str(item)[:180],
                )
            )
            continue
        missing_field = next(
            (
                field
                for field in ("question", "correct_answer", "explanation", "source_context")
                if not isinstance(item.get(field), str) or not item[field].strip()
            ),
            None,
        )
        if missing_field:
            validation_errors.append(
                GenerationRejection(
                    code="REQUIRED_FIELD_MISSING",
                    message=f"Thiếu hoặc rỗng trường bắt buộc '{missing_field}'.",
                    candidate_index=candidate_index,
                    question_excerpt=str(item.get("question") or "")[:180],
                    repairable=True,
                )
            )
            continue
        error = _check_type_format(item, question_type)
        if error:
            logger.warning(f"Bỏ qua câu hỏi sai định dạng ({question_type}): {error} | item={item}")
            validation_errors.append(
                GenerationRejection(
                    code="INVALID_TYPE_FORMAT",
                    message=error,
                    candidate_index=candidate_index,
                    question_excerpt=str(item.get("question") or "")[:180],
                    repairable=True,
                )
            )
            continue

        candidate_errors = validate_source_grounding(
            item,
            context_text=context_text,
            question_type=question_type,
            candidate_index=candidate_index,
        )
        candidate_errors.extend(validate_question_code(item, candidate_index=candidate_index))
        if question_type == "dung_sai":
            candidate_errors.extend(
                validate_true_false_clarity(item, candidate_index=candidate_index)
            )
        if candidate_errors:
            for candidate_error in candidate_errors:
                logger.warning(
                    "Bỏ candidate sau post-processing (%s): %s | item=%s",
                    candidate_error.code,
                    candidate_error.message,
                    item,
                )
            validation_errors.extend(candidate_errors)
            continue

        raw_difficulty = item.get("difficulty")
        difficulty = requested_difficulty or _normalize_difficulty(raw_difficulty)
        if raw_difficulty not in (None, "") and difficulty is None:
            logger.warning("Bỏ qua difficulty không hợp lệ: %s", raw_difficulty)

        # Cập nhật metadata đảm bảo nhất quán
        item.update({
            "question_type": question_type,
            "bloom_level": bloom_level,
            "difficulty": difficulty,
        })
        try:
            formatted.append(GeneratedQuestion(**item))
        except Exception as exc:
            error_message = f"{question_type} không khớp schema GeneratedQuestion: {exc}"
            logger.warning("Bỏ qua câu hỏi lỗi schema: %s | item=%s", error_message, item)
            validation_errors.append(
                GenerationRejection(
                    code="GENERATED_QUESTION_SCHEMA_ERROR",
                    message=error_message,
                    candidate_index=candidate_index,
                    question_excerpt=str(item.get("question") or "")[:180],
                )
            )

    return formatted, validation_errors
