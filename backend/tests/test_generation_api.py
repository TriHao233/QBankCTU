import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from core.dependencies import CurrentUser, require_question_generator
from modules.documents.service import get_document_service
from modules.generation.generate import router
from modules.generation.mongodb import _resolve_clo_ids, validate_generation_scope
from modules.generation.prompt_builder import ChatPromptPackage, PromptBuilder
from modules.generation.question import (
    GenerationOutputError,
    _content_mode,
    _context_section_limit,
    _focused_context_snapshot,
    _generate_questions_for_plan_item,
    generate_questions_rag,
)
from modules.generation.schemas import (
    QUESTION_TYPE_BLOOM_LEVELS,
    GeneratedQuestion,
    GenerationPlanSummary,
    QuestionGenerateRequest,
    QuestionPlanItem,
    QuestionType,
    is_bloom_allowed,
)
from modules.generation.postprocessing import question_fingerprint
from modules.rag.search import _hybrid_score, _keyword_tokens, get_context_snapshot


def user(role: str = "Teacher") -> CurrentUser:
    oid = ObjectId()
    return CurrentUser(
        id=oid,
        firebase_uid=f"firebase-{oid}",
        email="teacher@example.com",
        role=role,
        is_active=True,
        permissions=(),
    )


class GenerationStatusApiTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.current_user = user()
        self.app.dependency_overrides[require_question_generator] = lambda: self.current_user
        self.app.dependency_overrides[get_document_service] = lambda: type(
            "DocumentServiceStub",
            (),
            {"can_generate_from": staticmethod(lambda _document_id, _user: True)},
        )()
        self.client = TestClient(self.app)
        self.job_id = str(ObjectId())
        index_patch = patch("modules.generation.generate.generation_index_snapshot", return_value={})
        self.index_snapshot = index_patch.start()
        self.addCleanup(index_patch.stop)

    def tearDown(self):
        self.app.dependency_overrides.clear()

    @staticmethod
    def queued_job(job_id: str) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "updated_at": now,
        }

    def test_teacher_status_lookup_is_scoped_to_owner(self):
        with patch("modules.generation.generate.get_generation_job") as lookup:
            lookup.return_value = self.queued_job(self.job_id)
            response = self.client.get(f"/api/v1/generate/status/{self.job_id}")

        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(self.job_id, requested_by_user_id=self.current_user.id)

    def test_other_teacher_receives_not_found(self):
        with patch("modules.generation.generate.get_generation_job", return_value=None) as lookup:
            response = self.client.get(f"/api/v1/generate/status/{self.job_id}")

        self.assertEqual(response.status_code, 404)
        lookup.assert_called_once_with(self.job_id, requested_by_user_id=self.current_user.id)

    def test_teacher_can_cancel_own_generation_job(self):
        cancelled = self.queued_job(self.job_id)
        cancelled["status"] = "failed"
        cancelled["error_message"] = "Đã dừng theo yêu cầu của người dùng"
        with (
            patch("modules.generation.generate.get_generation_job", side_effect=[self.queued_job(self.job_id), cancelled]),
            patch("modules.generation.generate.cancel_generation_job", return_value=True) as cancel,
        ):
            response = self.client.post(f"/api/v1/generate/status/{self.job_id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        cancel.assert_called_once_with(self.job_id, requested_by_user_id=self.current_user.id)

    def test_admin_can_inspect_any_generation_job(self):
        self.current_user = user("Admin")
        with patch("modules.generation.generate.get_generation_job") as lookup:
            lookup.return_value = self.queued_job(self.job_id)
            response = self.client.get(f"/api/v1/generate/status/{self.job_id}")

        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(self.job_id, requested_by_user_id=None)

    def test_status_event_stream_is_scoped_and_finishes_on_terminal_status(self):
        completed = self.queued_job(self.job_id)
        completed["status"] = "completed"
        completed["result"] = {"data": [], "summary": []}
        completed["progress"] = {"stage": "completed", "completed": 1, "total": 1}
        with patch("modules.generation.generate.get_generation_job", return_value=completed) as lookup:
            response = self.client.get(f"/api/v1/generate/status/{self.job_id}/events")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
        self.assertIn("event: status", response.text)
        self.assertIn('"status":"completed"', response.text)
        self.assertTrue(
            all(
                call.kwargs.get("requested_by_user_id") == self.current_user.id
                for call in lookup.call_args_list
            )
        )

    @staticmethod
    def generation_payload() -> dict:
        return {
            "document_id": str(ObjectId()),
            "bloom_level": "2_hieu",
            "question_type": "dung_sai",
            "num_questions": 1,
        }

    def test_enqueue_reuses_job_for_same_idempotency_key(self):
        existing = self.queued_job(self.job_id)
        with (
            patch("modules.generation.generate.get_generation_job_by_idempotency", return_value=existing),
            patch("modules.generation.generate.count_active_generation_jobs") as count_active,
            patch("modules.generation.generate.create_generation_job") as create_job,
        ):
            response = self.client.post(
                "/api/v1/generate/questions",
                json=self.generation_payload(),
                headers={"Idempotency-Key": "request-123"},
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], self.job_id)
        count_active.assert_not_called()
        create_job.assert_not_called()

    def test_enqueue_rejects_reused_key_for_different_request(self):
        existing = {**self.queued_job(self.job_id), "request_hash": "other-request"}
        with (
            patch("modules.generation.generate.get_generation_job_by_idempotency", return_value=existing),
            patch("modules.generation.generate.create_generation_job") as create_job,
        ):
            response = self.client.post(
                "/api/v1/generate/questions",
                json=self.generation_payload(),
                headers={"Idempotency-Key": "request-123"},
            )
        self.assertEqual(response.status_code, 422)
        create_job.assert_not_called()

    def test_enqueue_returns_conflict_for_unindexed_document(self):
        from modules.generation.mongodb import DocumentNotIndexedError

        self.index_snapshot.side_effect = DocumentNotIndexedError("Tài liệu chưa được index xong")
        with patch("modules.generation.generate.create_generation_job") as create_job:
            response = self.client.post("/api/v1/generate/questions", json=self.generation_payload())
        self.assertEqual(response.status_code, 409)
        create_job.assert_not_called()

    def test_enqueue_pins_index_snapshot_and_enforces_limit_atomically(self):
        from modules.generation.mongodb import ActiveJobLimitError

        pinned = {"doc": {"chunk_set_id": "c1", "vector_collection_id": "v1"}}
        self.index_snapshot.return_value = pinned
        with (
            patch("modules.generation.generate.count_active_generation_jobs", return_value=0),
            patch("modules.generation.generate.resolve_model_snapshot", return_value={"runtime": "OLLAMA"}),
            patch("modules.generation.generate.create_generation_job", return_value=self.job_id) as create_job,
        ):
            accepted = self.client.post("/api/v1/generate/questions", json=self.generation_payload())
            create_job.side_effect = ActiveJobLimitError()
            limited = self.client.post("/api/v1/generate/questions", json=self.generation_payload())
        self.assertEqual(accepted.status_code, 202)
        kwargs = create_job.call_args_list[0].kwargs
        self.assertEqual(kwargs["index_snapshot"], pinned)
        self.assertTrue(kwargs["request_hash"])
        self.assertIsNotNone(kwargs["max_active"])
        self.assertEqual(limited.status_code, 429)

    def test_enqueue_rejects_user_over_active_job_quota(self):
        with (
            patch("modules.generation.generate.get_generation_job_by_idempotency", return_value=None),
            patch("modules.generation.generate.count_active_generation_jobs", return_value=10),
            patch("modules.generation.generate.create_generation_job") as create_job,
        ):
            response = self.client.post(
                "/api/v1/generate/questions",
                json=self.generation_payload(),
                headers={"Idempotency-Key": "request-over-quota"},
            )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "15")
        create_job.assert_not_called()

    def test_enqueue_freezes_resolved_model_snapshot(self):
        snapshot = {
            "model_code": "qwen-fast",
            "model_name": "qwen2.5:14b",
            "runtime": "OLLAMA",
            "source": "catalog",
        }
        with (
            patch("modules.generation.generate.count_active_generation_jobs", return_value=0),
            patch("modules.generation.generate.resolve_model_snapshot", return_value=snapshot),
            patch("modules.generation.generate.create_generation_job", return_value=self.job_id) as create_job,
        ):
            payload = self.generation_payload()
            payload["model_provider"] = "qwen-fast"
            response = self.client.post("/api/v1/generate/questions", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(create_job.call_args.kwargs["model_snapshot"], snapshot)
        self.assertEqual(create_job.call_args.kwargs["code_model_snapshot"], snapshot)
        self.assertIsNone(create_job.call_args.kwargs["fallback_model_snapshot"])

    def test_enqueue_snapshots_separate_code_model(self):
        snapshots = {
            "qwen": {"model_code": "qwen", "runtime": "OLLAMA"},
            "deepseek": {"model_code": "deepseek", "runtime": "OLLAMA"},
        }
        with (
            patch("modules.generation.generate.count_active_generation_jobs", return_value=0),
            patch(
                "modules.generation.generate.resolve_model_snapshot",
                side_effect=lambda provider, **_kwargs: snapshots[provider],
            ) as resolve,
            patch("modules.generation.generate.create_generation_job", return_value=self.job_id) as create_job,
        ):
            payload = self.generation_payload()
            payload.update({"model_provider": "qwen", "code_model_provider": "deepseek"})
            response = self.client.post("/api/v1/generate/questions", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual([call.args[0] for call in resolve.call_args_list], ["qwen", "deepseek"])
        self.assertEqual(create_job.call_args.kwargs["model_snapshot"], snapshots["qwen"])
        self.assertEqual(create_job.call_args.kwargs["code_model_snapshot"], snapshots["deepseek"])
        self.assertEqual(create_job.call_args.args[0]["code_model_provider"], "deepseek")

    def test_enqueue_falls_back_when_code_model_unavailable(self):
        general_snapshot = {"model_code": "qwen", "runtime": "OLLAMA"}

        def resolve(provider, **_kwargs):
            if provider != "qwen":
                raise ValueError("unknown model")
            return general_snapshot

        with (
            patch("modules.generation.generate.count_active_generation_jobs", return_value=0),
            patch("modules.generation.generate.resolve_model_snapshot", side_effect=resolve),
            patch("modules.generation.generate.create_generation_job", return_value=self.job_id) as create_job,
        ):
            payload = self.generation_payload()
            payload.update({"model_provider": "qwen", "code_model_provider": "missing"})
            response = self.client.post("/api/v1/generate/questions", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertEqual(create_job.call_args.kwargs["code_model_snapshot"], general_snapshot)
        self.assertEqual(create_job.call_args.args[0]["code_model_provider"], "qwen")

    def test_content_mode_detects_code_and_honors_override(self):
        auto = QuestionPlanItem(question_type="trac_nghiem", content_mode="auto")
        forced_general = QuestionPlanItem(question_type="trac_nghiem", content_mode="general")
        context = "```cpp\nint push(int value) { return value; }\n```"

        self.assertEqual(_content_mode(auto, context, None), "code")
        self.assertEqual(_content_mode(forced_general, context, None), "general")

    def test_prompt_contains_content_mode_and_valid_clo_catalog(self):
        prompt = PromptBuilder().build(
            context="Stack dùng nguyên tắc LIFO.",
            bloom_level="3_van_dung",
            question_type="trac_nghiem",
            num_questions=1,
            content_mode="code",
            learning_outcomes=[{"clo_code": "CLO2", "description": "Cài đặt cấu trúc dữ liệu"}],
        )

        self.assertIn("CONTENT MODE: CODE", prompt)
        self.assertIn("CLO2: Cài đặt cấu trúc dữ liệu", prompt)
        self.assertIn("Do not invent codes", prompt)

    def test_hybrid_score_rewards_keyword_overlap(self):
        query_tokens = _keyword_tokens("cây nhị phân tìm kiếm")
        matching = _hybrid_score("Duyệt cây nhị phân tìm kiếm", {}, query_tokens, vector_rank=1)
        unrelated = _hybrid_score("Ngăn xếp hoạt động theo LIFO", {}, query_tokens, vector_rank=1)

        self.assertGreater(matching, unrelated)

    def test_context_snapshot_combines_semantic_and_keyword_ranking(self):
        class CollectionStub:
            def count(self):
                return 2

            def query(self, **kwargs):
                self.query_kwargs = kwargs
                return {
                    "documents": [["Stack dùng LIFO.", "Cây nhị phân tìm kiếm hỗ trợ tra cứu."]],
                    "metadatas": [[
                        {"chunk_id": "stack", "chunk_set_id": "set-1", "information_density": 0},
                        {"chunk_id": "tree", "chunk_set_id": "set-1", "information_density": 0},
                    ]],
                }

        collection = CollectionStub()
        with (
            patch("modules.rag.search._active_vector_snapshot", return_value=("set-1", "vector-1")),
            patch("modules.rag.search.get_collection", return_value=collection),
        ):
            snapshot = get_context_snapshot(
                document_id=str(ObjectId()),
                collection_name="chunks",
                query_text="cây nhị phân tìm kiếm",
                limit=2,
            )

        self.assertEqual(collection.query_kwargs["query_texts"], ["cây nhị phân tìm kiếm"])
        self.assertEqual(snapshot["results"][0]["chunk_id"], "tree")

    def test_gemini_retrieval_does_not_wait_for_ollama_gpu(self):
        class CollectionStub:
            def __init__(self):
                self.query_called = False

            def count(self):
                return 2

            def query(self, **_kwargs):
                self.query_called = True
                raise AssertionError("Vector query must not wait behind Ollama")

            def get(self, **_kwargs):
                return {
                    "documents": [
                        "Câu lệnh if dùng để rẽ nhánh theo điều kiện.",
                        "Vòng lặp for dùng để lặp với số lần xác định.",
                    ],
                    "metadatas": [
                        {"chunk_id": "if", "chunk_set_id": "set-1"},
                        {"chunk_id": "for", "chunk_set_id": "set-1"},
                    ],
                }

        collection = CollectionStub()
        with (
            patch("modules.rag.search._active_vector_snapshot", return_value=("set-1", "vector-1")),
            patch("modules.rag.search.get_collection", return_value=collection),
            patch("modules.rag.search.current_gpu_operation_label", return_value="ollama"),
        ):
            snapshot = get_context_snapshot(
                document_id=str(ObjectId()),
                collection_name="chunks",
                query_text="if",
                avoid_ollama_gpu_wait=True,
                limit=1,
            )

        self.assertFalse(collection.query_called)
        self.assertEqual(snapshot["results"][0]["chunk_id"], "if")

    def test_context_snapshot_excludes_heading_only_chunks(self):
        class CollectionStub:
            def count(self):
                return 3

            def query(self, **kwargs):
                return {
                    "documents": [[
                        "# Chương 6: MẢNG",
                        "Mảng là cấu trúc dữ liệu lưu nhiều phần tử cùng kiểu.",
                        "Mảng có thể được truy cập bằng chỉ số của phần tử.",
                    ]],
                    "metadatas": [[
                        {"chunk_id": "heading", "chunk_set_id": "set-1", "heading": "Chương 6: MẢNG"},
                        {"chunk_id": "definition", "chunk_set_id": "set-1", "heading": "Chương 6: MẢNG"},
                        {"chunk_id": "index", "chunk_set_id": "set-1", "heading": "Chương 6: MẢNG"},
                    ]],
                }

        with (
            patch("modules.rag.search._active_vector_snapshot", return_value=("set-1", "vector-1")),
            patch("modules.rag.search.get_collection", return_value=CollectionStub()),
        ):
            snapshot = get_context_snapshot(
                document_id=str(ObjectId()),
                collection_name="chunks",
                target_heading="Chương 6: MẢNG",
                limit=2,
            )
        self.assertEqual([item["chunk_id"] for item in snapshot["results"]], ["definition", "index"])
        self.assertNotIn("Nội dung: # Chương 6: MẢNG", snapshot["context_text"])

    def test_context_snapshot_excludes_nested_markdown_heading(self):
        class CollectionStub:
            def count(self):
                return 2

            def query(self, **kwargs):
                return {
                    "documents": [[
                        "## IV. TRUY CẬP TẬP TIN NHỊ PHÂN",
                        "Tập tin nhị phân được đọc bằng các hàm phù hợp với kiểu dữ liệu đã lưu.",
                    ]],
                    "metadatas": [[
                        {
                            "chunk_id": "heading",
                            "chunk_set_id": "set-1",
                            "heading": "IV. TRUY CẬP TẬP TIN NHỊ PHÂN",
                            "heading_path_text": "Chương 10 > IV. TRUY CẬP TẬP TIN NHỊ PHÂN",
                        },
                        {"chunk_id": "content", "chunk_set_id": "set-1", "heading": "Chương 10"},
                    ]],
                }

        with (
            patch("modules.rag.search._active_vector_snapshot", return_value=("set-1", "vector-1")),
            patch("modules.rag.search.get_collection", return_value=CollectionStub()),
        ):
            snapshot = get_context_snapshot(
                document_id=str(ObjectId()),
                collection_name="chunks",
                query_text="truy cập tập tin",
                limit=2,
            )

        self.assertEqual([item["chunk_id"] for item in snapshot["results"]], ["content"])
        self.assertNotIn("Nội dung: ##", snapshot["context_text"])

    def test_context_snapshot_matches_short_chapter_heading(self):
        class CollectionStub:
            def count(self):
                return 2

            def query(self, **kwargs):
                return {
                    "documents": [[
                        "Mảng một chiều lưu các phần tử cùng kiểu dữ liệu.",
                        "Chuỗi ký tự là một dãy các ký tự liên tiếp.",
                    ]],
                    "metadatas": [[
                        {"chunk_id": "array", "chunk_set_id": "set-1", "heading": "Chương 6"},
                        {"chunk_id": "string", "chunk_set_id": "set-1", "heading": "Chương 8"},
                    ]],
                }

        with (
            patch("modules.rag.search._active_vector_snapshot", return_value=("set-1", "vector-1")),
            patch("modules.rag.search.get_collection", return_value=CollectionStub()),
        ):
            snapshot = get_context_snapshot(
                document_id=str(ObjectId()),
                collection_name="chunks",
                target_heading="Chương 6: MẢNG",
                limit=2,
            )

        self.assertEqual([item["chunk_id"] for item in snapshot["results"]], ["array"])

    def test_clo_resolution_prefers_model_code_from_catalog(self):
        outcomes = [
            {"id": str(ObjectId()), "clo_code": "CLO1", "description": "Giải thích cấu trúc dữ liệu"},
            {"id": str(ObjectId()), "clo_code": "CLO2", "description": "Cài đặt cấu trúc dữ liệu"},
        ]
        with patch("modules.generation.mongodb.get_document_learning_outcomes", return_value=outcomes):
            result = _resolve_clo_ids(
                str(ObjectId()),
                {"question": "Viết mã cài đặt stack", "clo_codes": ["clo2", "không-tồn-tại"]},
            )

        self.assertEqual(result, [outcomes[1]["id"]])

    def test_clo_resolution_assigns_targets_round_robin_when_model_code_invalid(self):
        targets = [
            {"id": "t1", "clo_code": "CLO1", "description": "Giải thích"},
            {"id": "t2", "clo_code": "CLO2", "description": "Cài đặt"},
        ]
        assigned = [
            _resolve_clo_ids("doc", {"question": "Câu hỏi", "clo_codes": ["CLO9"]}, targets, round_robin_slot=slot)
            for slot in range(3)
        ]
        self.assertEqual(assigned, [["t1"], ["t2"], ["t1"]])
        kept = _resolve_clo_ids("doc", {"question": "Câu", "clo_codes": ["clo2"]}, targets, round_robin_slot=0)
        self.assertEqual(kept, ["t2"])


class IncrementalGenerationTests(unittest.IsolatedAsyncioTestCase):
    def test_focused_context_snapshot_rotates_context_sections(self):
        snapshot = {
            "context_text": "Mục lục: [A]\nNội dung: A đủ dài.\n\n---\n\nMục lục: [B]\nNội dung: B đủ dài.",
            "results": [{"chunk_id": "a"}, {"chunk_id": "b"}],
            "chunk_set_id": "set-1",
            "vector_collection_id": "vec-1",
        }

        first, first_directive = _focused_context_snapshot(snapshot, 0)
        second, second_directive = _focused_context_snapshot(snapshot, 1)

        self.assertIn("Nội dung: A", first["context_text"])
        self.assertEqual(first["results"], [{"chunk_id": "a"}])
        self.assertIn("section 1 of 2", first_directive)
        self.assertIn("Nội dung: B", second["context_text"])
        self.assertEqual(second["results"], [{"chunk_id": "b"}])
        self.assertIn("section 2 of 2", second_directive)

    async def test_generation_saves_and_reports_each_question_independently(self):
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=3,
        )
        progress_updates = []

        async def generate_one(_req, plan_item, *, plan_index, **_kwargs):
            questions = [
                GeneratedQuestion(
                    question=f"Câu hỏi {question_number}",
                    options={"A": "Một", "B": "Hai", "C": "Ba", "D": "Bốn"},
                    correct_answer="A",
                    explanation="Giải thích",
                    question_type="trac_nghiem",
                    bloom_level="2_hieu",
                    source_context="Nội dung nguồn đủ dài để kiểm thử.",
                    question_id=str(ObjectId()),
                )
                for question_number in range(1, plan_item.num_questions + 1)
            ]
            summary = GenerationPlanSummary(
                plan_index=plan_index,
                question_type="trac_nghiem",
                bloom_level="2_hieu",
                requested_count=plan_item.num_questions,
                parsed_count=plan_item.num_questions,
                valid_count=plan_item.num_questions,
                saved_count=plan_item.num_questions,
            )
            return questions, summary

        async def report_progress(progress):
            progress_updates.append(progress)

        with (
            patch(
                "modules.generation.question.get_context_snapshot",
                return_value={
                    "context_text": "Nội dung: Kiến thức dùng để sinh câu hỏi.",
                    "results": [],
                    "chunk_set_id": None,
                    "vector_collection_id": None,
                },
            ),
            patch("modules.generation.question.get_document_learning_outcomes", return_value=[]),
            patch("modules.generation.question.get_existing_question_texts", return_value=[]),
            patch("modules.generation.question.document_subject_id", return_value=None),
            patch(
                "modules.generation.question.resolve_model_snapshot",
                return_value={"runtime": "OLLAMA"},
            ),
            patch("modules.generation.question.get_llm_service", return_value=object()),
            patch(
                "modules.generation.question._generate_questions_for_plan_item",
                new=AsyncMock(side_effect=generate_one),
            ) as generate_batch,
        ):
            result = await generate_questions_rag(request, progress_callback=report_progress)

        self.assertEqual(generate_batch.await_count, 3)
        self.assertTrue(
            all(call.args[1].num_questions == 1 for call in generate_batch.await_args_list)
        )
        self.assertEqual(len(result.data), 3)
        self.assertEqual(result.summary[0].requested_count, 3)
        self.assertEqual(result.summary[0].saved_count, 3)
        self.assertEqual([item["completed"] for item in progress_updates], [0, 1, 2, 3])
        self.assertEqual([len(item["data"]) for item in progress_updates], [0, 1, 2, 3])

    async def test_multi_document_job_rotates_batches_and_targets_selected_clos(self):
        primary, extra = str(ObjectId()), str(ObjectId())
        request = QuestionGenerateRequest(
            document_id=primary,
            document_ids=[extra, primary],
            target_clo_ids=["clo-2"],
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=2,
        )
        self.assertEqual(request.all_document_ids(), [primary, extra])
        retrieval_calls = []

        def snapshot(**kwargs):
            retrieval_calls.append(kwargs)
            return {
                "context_text": f"Nội dung của {kwargs['document_id']}", "results": [],
                "chunk_set_id": None, "vector_collection_id": None,
            }

        async def generate_one(_req, plan_item, *, plan_index, **_kwargs):
            return [], GenerationPlanSummary(
                plan_index=plan_index, question_type="trac_nghiem", bloom_level="2_hieu",
                requested_count=plan_item.num_questions,
            )

        outcomes = [
            {"id": "clo-1", "clo_code": "CLO1", "description": "Nhớ khái niệm"},
            {"id": "clo-2", "clo_code": "CLO2", "description": "Phân tích thuật toán sắp xếp"},
        ]
        with (
            patch("modules.generation.question.get_context_snapshot", side_effect=snapshot),
            patch("modules.generation.question.get_document_learning_outcomes", return_value=outcomes),
            patch("modules.generation.question.get_existing_question_texts", return_value=[]),
            patch("modules.generation.question.document_subject_id", return_value=None),
            patch("modules.generation.question.resolve_model_snapshot", return_value={"runtime": "OLLAMA"}),
            patch("modules.generation.question.get_llm_service", return_value=object()),
            patch(
                "modules.generation.question._generate_questions_for_plan_item",
                new=AsyncMock(side_effect=generate_one),
            ) as generate_batch,
        ):
            await generate_questions_rag(request)

        self.assertEqual([call["document_id"] for call in retrieval_calls], [primary, extra])
        self.assertTrue(all(call["query_text"] == "Phân tích thuật toán sắp xếp" for call in retrieval_calls))
        planned = generate_batch.await_args_list[:2]
        self.assertEqual([call.args[0].document_id for call in planned], [primary, extra])
        self.assertEqual(planned[0].kwargs["learning_outcomes"], [outcomes[1]])
        self.assertIn("learning outcomes", planned[0].kwargs["focus_directive"])

    def test_ratio_distribution_expands_into_a_reproducible_plan(self):
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="1_nho",
            distribution={
                "total_questions": 10,
                "question_types": ["trac_nghiem", "dung_sai"],
                "bloom_ratios": {"1_nho": 0.3, "2_hieu": 0.5, "4_phan_tich": 0.2},
                "difficulty_ratios": {"de": 1, "trung_binh": 1},
            },
        )
        plan = request.effective_plan()
        self.assertEqual(sum(item.num_questions for item in plan), 10)
        by_bloom = {}
        for item in plan:
            by_bloom[item.bloom_level.value] = by_bloom.get(item.bloom_level.value, 0) + item.num_questions
            self.assertTrue(is_bloom_allowed(item.question_type, item.bloom_level))
        self.assertEqual(by_bloom, {"1_nho": 3, "2_hieu": 5, "4_phan_tich": 2})
        self.assertTrue(all(item.question_type == QuestionType.TRAC_NGHIEM
                            for item in plan if item.bloom_level.value == "4_phan_tich"))

        # The persisted job snapshot carries both fields and must re-validate unchanged.
        reparsed = QuestionGenerateRequest(**request.model_dump(mode="json"))
        self.assertEqual(reparsed.effective_plan(), plan)

        with self.assertRaises(ValidationError):
            QuestionGenerateRequest(
                document_id=str(ObjectId()),
                bloom_level="1_nho",
                question_plan=[{"question_type": "trac_nghiem", "num_questions": 1}],
                distribution={"total_questions": 2, "question_types": ["trac_nghiem"], "bloom_ratios": {"1_nho": 1}},
            )
        with self.assertRaises(ValidationError):
            QuestionGenerateRequest(
                document_id=str(ObjectId()),
                bloom_level="1_nho",
                distribution={"total_questions": 2, "question_types": ["ghep_cot"], "bloom_ratios": {"5_danh_gia": 1}},
            )

    def test_generation_scope_requires_same_subject_and_owned_clos(self):
        with (
            patch("modules.generation.mongodb.document_subject_id", side_effect=["s1", "s2"]),
            self.assertRaisesRegex(ValueError, "cùng một học phần"),
        ):
            validate_generation_scope(["d1", "d2"], [])
        with (
            patch("modules.generation.mongodb.get_document_learning_outcomes", return_value=[{"id": "clo-1"}]),
            self.assertRaisesRegex(ValueError, "CLO"),
        ):
            validate_generation_scope(["d1"], ["clo-9"])
        with patch("modules.generation.mongodb.document_subject_id") as subject_lookup:
            validate_generation_scope(["d1"], [])
        subject_lookup.assert_not_called()

    async def test_prompt_avoid_list_only_contains_requesters_own_questions(self):
        requester = ObjectId()
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()), bloom_level="2_hieu", question_type="trac_nghiem", num_questions=1,
        )
        lookups = []

        def existing_texts(_document_id, *, viewer_user_id, include_shared=False, shared_subject_ids=(),
                           subject_id=None):
            lookups.append((viewer_user_id, include_shared, set(shared_subject_ids), subject_id))
            return ["Câu của tôi", "Câu được chia sẻ"] if include_shared else ["Câu của tôi"]

        async def generate_one(_req, plan_item, *, plan_index, **_kwargs):
            return [], GenerationPlanSummary(
                plan_index=plan_index, question_type="trac_nghiem", bloom_level="2_hieu",
                requested_count=plan_item.num_questions,
            )

        with (
            patch("modules.generation.question.get_context_snapshot", return_value={
                "context_text": "Nội dung: Kiến thức.", "results": [],
                "chunk_set_id": None, "vector_collection_id": None,
            }),
            patch("modules.generation.question.get_document_learning_outcomes", return_value=[]),
            patch("modules.generation.question.get_existing_question_texts", side_effect=existing_texts),
            patch("modules.generation.question.teaching_subject_ids_for", return_value={"s1"}),
            patch("modules.generation.question.document_subject_id", return_value="subject-1"),
            patch("modules.generation.question.resolve_model_snapshot", return_value={"runtime": "OLLAMA"}),
            patch("modules.generation.question.get_llm_service", return_value=object()),
            patch(
                "modules.generation.question._generate_questions_for_plan_item",
                new=AsyncMock(side_effect=generate_one),
            ) as generate_batch,
        ):
            await generate_questions_rag(request, requested_by_user_id=requester)

        # The prompt list stays per-document; local dedup spans every document of the subject.
        self.assertEqual(lookups, [(requester, False, set(), None), (requester, True, {"s1"}, "subject-1")])
        avoid = generate_batch.await_args_list[0].kwargs["avoid_questions"]
        self.assertIn("Câu của tôi", avoid)
        self.assertNotIn("Câu được chia sẻ", avoid)
        self.assertIn(
            question_fingerprint("Câu được chia sẻ"),
            generate_batch.await_args_list[0].kwargs["seen_question_fingerprints"],
        )

    def test_request_accepts_up_to_fifty_questions_across_formats_and_difficulties(self):
        plan = [
            {
                "question_type": question_type,
                "bloom_level": "3_van_dung" if question_type == QuestionType.TINH_HUONG else "2_hieu",
                "difficulty": difficulty,
                "num_questions": 1,
            }
            for question_type in QuestionType
            for difficulty in ("de", "trung_binh", "kho")
        ]
        plan[0]["num_questions"] = 50 - (len(plan) - 1)
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()), bloom_level="2_hieu", question_plan=plan
        )
        self.assertEqual(sum(item.num_questions for item in request.effective_plan()), 50)

        plan[0]["num_questions"] += 1
        with self.assertRaises(ValueError):
            QuestionGenerateRequest(document_id=str(ObjectId()), bloom_level="2_hieu", question_plan=plan)

    def test_bloom_matrix_limits_levels_per_question_type(self):
        expected = {
            QuestionType.TRAC_NGHIEM: (1, 5),
            QuestionType.NHIEU_LUA_CHON: (1, 5),
            QuestionType.DUNG_SAI: (1, 3),
            QuestionType.DIEN_KHUYET: (1, 3),
            QuestionType.GHEP_COT: (1, 2),
            QuestionType.SAP_XEP: (1, 3),
            QuestionType.TINH_HUONG: (3, 6),
        }
        self.assertEqual(set(QUESTION_TYPE_BLOOM_LEVELS), set(QuestionType))
        for question_type, (first, last) in expected.items():
            ranks = sorted(int(level.value.split("_", 1)[0]) for level in QUESTION_TYPE_BLOOM_LEVELS[question_type])
            self.assertEqual(ranks, list(range(first, last + 1)), question_type)

    def test_request_rejects_bloom_level_locked_for_question_type(self):
        document_id = str(ObjectId())
        with self.assertRaisesRegex(ValueError, "6_sang_tao"):
            QuestionGenerateRequest(
                document_id=document_id, bloom_level="6_sang_tao", question_type="trac_nghiem"
            )
        with self.assertRaisesRegex(ValueError, "tinh_huong"):
            QuestionGenerateRequest(
                document_id=document_id,
                bloom_level="2_hieu",
                question_plan=[{"question_type": "tinh_huong", "num_questions": 1}],
            )

        request = QuestionGenerateRequest(
            document_id=document_id,
            bloom_level="6_sang_tao",
            question_plan=[
                {"question_type": "tinh_huong", "num_questions": 1},
                {"question_type": "trac_nghiem", "bloom_level": "5_danh_gia", "num_questions": 1},
                {"question_type": "dung_sai", "bloom_level": "3_van_dung", "num_questions": 1},
            ],
        )
        self.assertEqual(len(request.effective_plan()), 3)

    def test_matching_output_schema_requires_flat_items_with_one_distractor(self):
        schema = PromptBuilder.output_schema("ghep_cot")
        item = schema["properties"]["questions"]["items"]["properties"]
        self.assertEqual(item["options"]["required"], ["1", "2", "3", "a", "b", "c", "d"])
        self.assertFalse(item["options"]["additionalProperties"])
        self.assertRegex("1-b, 2-a, 3-c", item["correct_answer"]["pattern"])

    def test_context_section_limit_scales_with_question_count(self):
        self.assertEqual(_context_section_limit(1), 5)
        self.assertEqual(_context_section_limit(7), 5)
        self.assertEqual(_context_section_limit(50), 25)
        self.assertEqual(_context_section_limit(500), 30)

    async def test_failed_batch_keeps_questions_from_other_batches(self):
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=3,
        )
        calls = 0

        async def generate_one(_req, plan_item, *, plan_index, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise GenerationOutputError("Mô hình không tạo được câu hỏi hợp lệ sau bước kiểm tra đầu ra.")
            question = GeneratedQuestion(
                question=f"Câu hỏi {calls}",
                options={"A": "Một", "B": "Hai", "C": "Ba", "D": "Bốn"},
                correct_answer="A",
                explanation="Giải thích",
                question_type="trac_nghiem",
                bloom_level="2_hieu",
                source_context="Nội dung nguồn đủ dài để kiểm thử.",
                question_id=str(ObjectId()),
            )
            return [question], GenerationPlanSummary(
                plan_index=plan_index, question_type="trac_nghiem", bloom_level="2_hieu",
                requested_count=1, parsed_count=1, valid_count=1, saved_count=1,
            )

        with (
            patch(
                "modules.generation.question.get_context_snapshot",
                return_value={
                    "context_text": "Nội dung: Kiến thức dùng để sinh câu hỏi.",
                    "results": [],
                    "chunk_set_id": None,
                    "vector_collection_id": None,
                },
            ),
            patch("modules.generation.question.get_document_learning_outcomes", return_value=[]),
            patch("modules.generation.question.get_existing_question_texts", return_value=[]),
            patch("modules.generation.question.document_subject_id", return_value=None),
            patch("modules.generation.question.resolve_model_snapshot", return_value={"runtime": "OLLAMA"}),
            patch("modules.generation.question.get_llm_service", return_value=object()),
            patch(
                "modules.generation.question._generate_questions_for_plan_item",
                new=AsyncMock(side_effect=generate_one),
            ) as generate_batch,
        ):
            result = await generate_questions_rag(request)

        self.assertEqual(generate_batch.await_count, 4)
        self.assertEqual([item.question for item in result.data], ["Câu hỏi 1", "Câu hỏi 3", "Câu hỏi 4"])
        self.assertEqual(result.summary[0].requested_count, 3)
        self.assertEqual(result.summary[0].saved_count, 3)
        self.assertIn("BATCH_NO_VALID_OUTPUT", [item.code for item in result.summary[0].rejection_reasons])

    async def test_invalid_json_fails_after_retrying_the_current_question(self):
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=1,
        )
        plan_item = request.effective_plan()[0]
        llm = MagicMock()
        llm.generate_chat = AsyncMock(
            side_effect=[
                '{"questions": [{"question": "bị cắt"}',
                '{"questions": [{"question": "vẫn bị cắt"}',
                '{"questions": [{"question": "tiếp tục bị cắt"}',
            ]
        )
        prompt_builder = MagicMock()
        prompt_builder.build_chat.return_value = ChatPromptPackage("system", "user", {"type": "object"})

        with (
            patch("modules.generation.question.create_generation_run", return_value=str(ObjectId())),
            patch("modules.generation.question.finish_generation_run") as finish_run,
            patch("modules.generation.question.reset_llm_execution_tracking"),
            patch("modules.generation.question.get_llm_execution_snapshot", return_value={}),
        ):
            with self.assertRaises(GenerationOutputError):
                await _generate_questions_for_plan_item(
                    request,
                    plan_item,
                    plan_index=1,
                    avoid_questions=[],
                    seen_question_fingerprints=set(),
                    context_snapshot={
                        "results": [],
                        "chunk_set_id": None,
                        "vector_collection_id": None,
                    },
                    context_text="Nội dung: Kiến thức dùng để sinh câu hỏi.",
                    prompt_builder=prompt_builder,
                    llm=llm,
                    model_snapshot={},
                    model_provider="qwen",
                    content_mode="general",
                    learning_outcomes=[],
                )

        self.assertEqual(finish_run.call_args.kwargs["status"], "FAILED")
        self.assertEqual(finish_run.call_args.kwargs["post_processing"]["retry_attempt_count"], 2)
        self.assertEqual(finish_run.call_args.kwargs["validation_errors"][0]["code"], "INVALID_JSON_RESPONSE")
        self.assertEqual(llm.generate_chat.call_count, 3)
        finish_run.assert_called_once()
        self.assertEqual(finish_run.call_args.kwargs["status"], "FAILED")

    async def test_invalid_json_is_repaired_before_rejecting_question(self):
        source_context = "Kiến thức dùng để sinh câu hỏi về ngăn xếp theo nguyên tắc LIFO."
        repaired_question = {
            "question": "Ngăn xếp hoạt động theo nguyên tắc nào?",
            "options": {"A": "FIFO", "B": "LIFO", "C": "Ngẫu nhiên", "D": "Theo khóa"},
            "correct_answer": "B",
            "explanation": "Ngăn xếp lấy phần tử được thêm sau cùng trước.",
            "question_type": "trac_nghiem",
            "bloom_level": "2_hieu",
            "source_context": source_context,
            "source_keywords": ["ngăn xếp", "LIFO"],
            "false_mutation": None,
        }
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=1,
        )
        plan_item = request.effective_plan()[0]
        llm = MagicMock()
        llm.generate_chat = AsyncMock(
            side_effect=[
                '{"questions": [{"question": "bị cắt"}',
                json.dumps({"questions": [repaired_question]}, ensure_ascii=False),
            ]
        )
        prompt_builder = MagicMock()
        prompt_builder.build_chat.return_value = ChatPromptPackage(
            "system", "user", {"type": "object"}
        )

        with (
            patch("modules.generation.question.create_generation_run", return_value=str(ObjectId())),
            patch("modules.generation.question.finish_generation_run") as finish_run,
            patch("modules.generation.question.reset_llm_execution_tracking"),
            patch("modules.generation.question.get_llm_execution_snapshot", return_value={}),
            patch(
                "modules.generation.question.save_generated_questions",
                return_value=[{**repaired_question, "question_id": str(ObjectId())}],
            ),
        ):
            questions, summary = await _generate_questions_for_plan_item(
                request,
                plan_item,
                plan_index=1,
                avoid_questions=[],
                seen_question_fingerprints=set(),
                context_snapshot={
                    "results": [],
                    "chunk_set_id": None,
                    "vector_collection_id": None,
                },
                context_text=f"Nội dung: {source_context}",
                prompt_builder=prompt_builder,
                llm=llm,
                model_snapshot={},
                model_provider="qwen",
                content_mode="general",
                learning_outcomes=[],
            )

        self.assertEqual(llm.generate_chat.call_count, 2)
        self.assertEqual(len(questions), 1)
        self.assertEqual(summary.saved_count, 1)
        self.assertEqual(summary.retry_attempt_count, 1)
        self.assertEqual(finish_run.call_args.kwargs["status"], "COMPLETED")

    async def test_duplicate_candidate_is_retried_before_fallback(self):
        source_context = "Kiến thức dùng để sinh câu hỏi về ngăn xếp LIFO."
        duplicate_question = {
            "question": "Ngăn xếp hoạt động theo nguyên tắc nào?",
            "options": {"A": "FIFO", "B": "LIFO", "C": "Random", "D": "Hash"},
            "correct_answer": "B",
            "explanation": "Ngăn xếp hoạt động theo LIFO.",
            "question_type": "trac_nghiem",
            "bloom_level": "2_hieu",
            "source_context": source_context,
            "source_keywords": ["ngăn xếp"],
            "false_mutation": None,
        }
        unique_question = {
            **duplicate_question,
            "question": "Vì sao thao tác pop của ngăn xếp lấy phần tử được thêm sau cùng?",
            "options": {
                "A": "Vì ngăn xếp dùng FIFO",
                "B": "Vì ngăn xếp dùng LIFO",
                "C": "Vì ngăn xếp sắp xếp theo khóa",
                "D": "Vì ngăn xếp truy cập ngẫu nhiên",
            },
        }
        request = QuestionGenerateRequest(
            document_id=str(ObjectId()),
            bloom_level="2_hieu",
            question_type="trac_nghiem",
            num_questions=1,
        )
        plan_item = request.effective_plan()[0]
        llm = MagicMock()
        llm.generate_chat = AsyncMock(side_effect=[
            json.dumps({"questions": [duplicate_question]}, ensure_ascii=False),
            json.dumps({"questions": [unique_question]}, ensure_ascii=False),
        ])
        prompt_builder = MagicMock()
        prompt_builder.build_chat.return_value = ChatPromptPackage("system", "user", {"type": "object"})

        with (
            patch("modules.generation.question.create_generation_run", return_value=str(ObjectId())),
            patch("modules.generation.question.finish_generation_run"),
            patch("modules.generation.question.reset_llm_execution_tracking"),
            patch("modules.generation.question.get_llm_execution_snapshot", return_value={}),
            patch(
                "modules.generation.question.save_generated_questions",
                return_value=[{**unique_question, "question_id": str(ObjectId())}],
            ) as save_questions,
        ):
            questions, summary = await _generate_questions_for_plan_item(
                request,
                plan_item,
                plan_index=1,
                avoid_questions=[duplicate_question["question"]],
                seen_question_fingerprints={question_fingerprint(duplicate_question["question"])},
                context_snapshot={
                    "results": [],
                    "chunk_set_id": None,
                    "vector_collection_id": None,
                },
                context_text=f"Nội dung: {source_context}",
                prompt_builder=prompt_builder,
                llm=llm,
                model_snapshot={},
                model_provider="qwen",
                content_mode="general",
                learning_outcomes=[],
            )

        self.assertEqual(llm.generate_chat.call_count, 2)
        self.assertEqual(questions[0].question, unique_question["question"])
        self.assertEqual(save_questions.call_args.args[1][0]["question"], unique_question["question"])
        self.assertEqual(summary.saved_count, 1)
        self.assertEqual(summary.retry_attempt_count, 1)


if __name__ == "__main__":
    unittest.main()
