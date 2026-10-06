import asyncio

import pytest

from modules.generation import generate as generate_module
from modules.generation.job_outcome import generation_job_outcome
from modules.generation.schemas import GeneratedQuestion

DOCUMENT_ID = "6650f0f0f0f0f0f0f0f0f0f0"
REQUEST = {"document_id": DOCUMENT_ID, "bloom_level": "1_nho", "num_questions": 3}


def _draft(index):
    return GeneratedQuestion(
        question=f"Câu {index}?",
        correct_answer="A",
        explanation="",
        question_type="trac_nghiem",
        bloom_level="1_nho",
        source_context="Nguồn",
        question_id=f"q{index}",
        generation_run_id=f"run{index}",
    ).model_dump()


@pytest.fixture
def worker(monkeypatch):
    calls = {"update": [], "retry": []}
    job = {
        "job_id": "job-1", "request": REQUEST, "requested_by_user_id": "u1", "status": "processing",
        "attempt_count": 3, "max_attempts": 3,
    }
    calls["job"] = job

    async def no_lease(_heartbeat, stop):
        await stop.wait()

    monkeypatch.setattr(generate_module, "claim_generation_job", lambda *_args: dict(job))
    monkeypatch.setattr(generate_module, "maintain_lease", no_lease)
    monkeypatch.setattr(generate_module, "update_generation_progress", lambda *_args: True)
    monkeypatch.setattr(
        generate_module, "update_generation_job",
        lambda job_id, **kwargs: calls["update"].append(kwargs) or True,
    )
    monkeypatch.setattr(
        generate_module, "retry_or_dead_letter_generation_job",
        lambda *args, **kwargs: calls["retry"].append(kwargs) or "queued",
    )
    return calls


def _crash_after(saved_count):
    async def fake_generate(_req, *, progress_callback, **_kwargs):
        if saved_count:
            await progress_callback({
                "stage": "generating", "completed": saved_count, "total": 3,
                "data": [_draft(index) for index in range(saved_count)],
                "summary": [], "warnings": ["cảnh báo ngữ cảnh"],
            })
        raise RuntimeError("Ollama timeout")
    return fake_generate


def test_failure_with_attempts_left_retries_and_resumes_later(worker, monkeypatch):
    worker["job"]["attempt_count"] = 1
    captured = {}

    async def fake_generate(_req, *, progress_callback, generation_job_id=None, **_kwargs):
        captured["job_id"] = generation_job_id
        await progress_callback({"stage": "generating", "completed": 1, "total": 3,
                                 "data": [_draft(0)], "summary": [], "warnings": []})
        raise RuntimeError("Ollama timeout")

    monkeypatch.setattr(generate_module, "generate_questions_rag", fake_generate)

    asyncio.run(generate_module.process_generate_background("job-1", "w1"))

    assert captured["job_id"] == "job-1"
    assert worker["update"] == []
    assert len(worker["retry"]) == 1


def test_failure_after_saved_drafts_on_last_attempt_completes_as_partial(worker, monkeypatch):
    monkeypatch.setattr(generate_module, "generate_questions_rag", _crash_after(2))

    asyncio.run(generate_module.process_generate_background("job-1", "w1"))

    assert worker["retry"] == []
    [update] = worker["update"]
    assert update["status"] == "completed"
    result = update["result"]
    assert result["status"] == "partial"
    assert [item["question_id"] for item in result["data"]] == ["q0", "q1"]
    assert result["generation_run_ids"] == ["run0", "run1"]
    assert result["warnings"][0] == "cảnh báo ngữ cảnh"
    assert "đã lưu 2/3 câu" in result["warnings"][-1]
    assert "Ollama" not in result["warnings"][-1]


def test_failure_before_any_draft_still_retries(worker, monkeypatch):
    monkeypatch.setattr(generate_module, "generate_questions_rag", _crash_after(0))

    asyncio.run(generate_module.process_generate_background("job-1", "w1"))

    assert worker["update"] == []
    assert len(worker["retry"]) == 1


def test_lost_access_stops_job_as_partial_with_code(worker, monkeypatch):
    async def fake_generate(_req, *, progress_callback, access_check=None, **_kwargs):
        await progress_callback({"stage": "generating", "completed": 1, "total": 3,
                                 "data": [_draft(0)], "summary": [], "warnings": []})
        await access_check()

    monkeypatch.setattr(generate_module, "generate_questions_rag", fake_generate)
    monkeypatch.setattr(
        generate_module, "generation_access_problem",
        lambda *_args: ("MEMBERSHIP_REVOKED", "Người yêu cầu không còn quyền dùng tài liệu"),
    )
    asyncio.run(generate_module.process_generate_background("job-1", "w1"))

    assert worker["retry"] == []
    [update] = worker["update"]
    assert update["status"] == "completed"
    assert update["result"]["status"] == "partial"
    assert update["result"]["stop_code"] == "MEMBERSHIP_REVOKED"
    assert [item["question_id"] for item in update["result"]["data"]] == ["q0"]
    response = generate_module._build_generation_status_response(
        {"job_id": "job-1", "status": "completed", "result": update["result"]}
    )
    assert response.outcome == "PARTIAL" and response.stop_code == "MEMBERSHIP_REVOKED"


def test_data_error_fails_without_retry(worker, monkeypatch):
    from modules.generation.question import GenerationDataError

    worker["job"]["attempt_count"] = 1

    async def fake_generate(_req, **_kwargs):
        raise GenerationDataError("Không tìm thấy đủ dữ liệu tri thức để sinh câu hỏi.")

    monkeypatch.setattr(generate_module, "generate_questions_rag", fake_generate)
    asyncio.run(generate_module.process_generate_background("job-1", "w1"))

    assert worker["retry"] == []
    [update] = worker["update"]
    assert update["status"] == "failed"


def _stub_generation_inputs(monkeypatch, question_module, context="Nội dung"):
    monkeypatch.setattr(question_module, "get_document_learning_outcomes", lambda _doc: [])
    monkeypatch.setattr(question_module, "get_context_snapshot", lambda **_kwargs: {
        "context_text": context, "results": [], "chunk_set_id": None, "vector_collection_id": None,
    })
    monkeypatch.setattr(question_module, "document_subject_id", lambda _doc: None)
    monkeypatch.setattr(question_module, "get_existing_question_texts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(question_module, "teaching_subject_ids_for", lambda _user: set())
    monkeypatch.setattr(question_module, "get_llm_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(question_module, "completed_generation_runs", lambda _job_id: [])


def test_access_is_rechecked_before_every_batch_and_code_uses_code_model(monkeypatch):
    from modules.generation import question as question_module
    from modules.generation.schemas import GenerationPlanSummary, QuestionGenerateRequest

    _stub_generation_inputs(monkeypatch, question_module)
    used = []

    async def fake_item(_req, plan_item, *, plan_index, model_snapshot=None, **_kwargs):
        used.append(model_snapshot["model_code"])
        return [GeneratedQuestion(**_draft(len(used)))], GenerationPlanSummary(
            plan_index=plan_index, question_type="trac_nghiem", bloom_level="1_nho",
            requested_count=plan_item.num_questions, saved_count=1,
        )

    monkeypatch.setattr(question_module, "_generate_questions_for_plan_item", fake_item)
    checks = []

    async def access_check():
        checks.append(1)

    request = QuestionGenerateRequest(
        document_id=DOCUMENT_ID, bloom_level="1_nho",
        question_plan=[
            {"question_type": "trac_nghiem", "num_questions": 1, "content_mode": "general"},
            {"question_type": "trac_nghiem", "num_questions": 1, "content_mode": "code"},
        ],
    )
    asyncio.run(question_module.generate_questions_rag(
        request, requested_by_user_id="u1",
        model_snapshot={"model_code": "qwen"}, code_model_snapshot={"model_code": "deepseek"},
        access_check=access_check,
    ))
    assert used == ["qwen", "deepseek"]
    assert len(checks) == 2


def test_resumed_job_only_generates_missing_slots(monkeypatch):
    from modules.generation import question as question_module
    from modules.generation.schemas import GenerationPlanSummary, QuestionGenerateRequest

    monkeypatch.setattr(question_module, "get_document_learning_outcomes", lambda _doc: [])
    monkeypatch.setattr(question_module, "get_context_snapshot", lambda **_kwargs: {
        "context_text": "Nội dung", "results": [], "chunk_set_id": None, "vector_collection_id": None,
    })
    monkeypatch.setattr(question_module, "document_subject_id", lambda _doc: None)
    monkeypatch.setattr(question_module, "get_existing_question_texts", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(question_module, "teaching_subject_ids_for", lambda _user: set())
    monkeypatch.setattr(question_module, "get_llm_service", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(question_module, "resolve_model_snapshot", lambda *_args, **_kwargs: {})
    requested_runs = []
    monkeypatch.setattr(question_module, "completed_generation_runs", lambda job_id: requested_runs.append(job_id) or [
        {"plan_index": 1, "accepted_questions": [_draft(0), _draft(1)]},
    ])
    batches = []

    async def fake_item(_req, plan_item, *, plan_index, generation_job_id=None, **_kwargs):
        batches.append((plan_index, plan_item.num_questions, generation_job_id))
        return [GeneratedQuestion(**_draft(2))], GenerationPlanSummary(
            plan_index=plan_index, question_type="trac_nghiem", bloom_level="1_nho",
            requested_count=plan_item.num_questions, saved_count=1,
        )

    monkeypatch.setattr(question_module, "_generate_questions_for_plan_item", fake_item)
    response = asyncio.run(question_module.generate_questions_rag(
        QuestionGenerateRequest(**REQUEST), requested_by_user_id="u1", generation_job_id="job-1",
    ))

    assert requested_runs == ["job-1"]
    assert batches == [(1, 1, "job-1")]
    assert [item.question_id for item in response.data] == ["q0", "q1", "q2"]
    assert response.summary[0].saved_count == 3


def test_partial_result_is_served_by_status_endpoint():
    result = generate_module.partial_generation_result(
        {"total": 3, "data": [_draft(0)], "summary": [], "warnings": []}
    )
    response = generate_module._build_generation_status_response(
        {"job_id": "job-1", "status": "completed", "result": result}
    )
    assert [item.question_id for item in response.data] == ["q0"]
    assert "đã lưu 1/3 câu" in response.warnings[-1]
    assert response.outcome == "PARTIAL"


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        ({"status": "processing"}, None),
        ({"status": "cancelled"}, None),
        ({"status": "failed", "cancelled_at": "2026-01-01T00:00:00Z"}, "CANCELLED"),
        ({"status": "failed"}, "FAILED"),
        ({"status": "failed", "dead_lettered_at": "2026-01-01T00:00:00Z"}, "DEAD_LETTER"),
        ({"status": "completed", "result": {"summary": [{"requested_count": 3, "saved_count": 3}]}}, "SUCCEEDED"),
        ({"status": "completed", "result": {"summary": [{"requested_count": 3, "saved_count": 2}]}}, "PARTIAL"),
        ({"status": "completed", "result": {"status": "partial"}}, "PARTIAL"),
    ],
)
def test_generation_job_outcome(job, expected):
    assert generation_job_outcome(job) == expected
