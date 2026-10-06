import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from bson import ObjectId

from modules.generation.question import heading_scope_warnings
from modules.questions.repository import REVIEW_IN_PROGRESS, review_fields_after_edit
from modules.questions.review_scope import ReviewerScope, reviewer_can_read
from modules.rag.search import _heading_matches_target, _normalize_heading_text


NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _pending(**fields):
    return {"review_status": "PENDING", "review_assignment": {"status": "UNASSIGNED"}, **fields}


class EditDuringReviewTests(unittest.TestCase):
    def test_active_claim_blocks_edit(self):
        question = _pending(review_assignment={
            "status": "IN_REVIEW", "reviewer_user_id": ObjectId(),
            "lock_expires_at": NOW + timedelta(hours=1),
        })
        with self.assertRaisesRegex(RuntimeError, REVIEW_IN_PROGRESS):
            review_fields_after_edit(question, NOW, None)

    def test_editing_pending_question_returns_it_to_draft(self):
        question = _pending(review_assignment={
            "status": "IN_REVIEW", "reviewer_user_id": ObjectId(),
            "lock_expires_at": NOW - timedelta(minutes=1),
        }, review_submission={"note": "cũ", "checks": {"flags": ["weak_grounding"]}})
        fields = review_fields_after_edit(question, NOW, None)
        self.assertEqual(fields["review_status"], "DRAFT")
        self.assertEqual(fields["review_assignment"]["status"], "UNASSIGNED")
        self.assertIsNone(fields["review_assignment"]["reviewer_user_id"])
        self.assertEqual(fields["review_submission"], {})

    def test_two_reviewer_requirement_survives_edit(self):
        question = _pending(secondary_review={
            "required": True, "status": "AWAITING_SECONDARY",
            "primary_review_id": ObjectId(), "primary_reviewer_user_id": ObjectId(),
        })
        fields = review_fields_after_edit(question, NOW, None)
        self.assertEqual(fields["secondary_review"]["status"], "REQUESTED")
        self.assertIsNone(fields["secondary_review"]["primary_reviewer_user_id"])

    def test_non_pending_question_returns_to_draft(self):
        fields = review_fields_after_edit({"review_status": "APPROVED"}, NOW, None)
        self.assertEqual(fields["review_status"], "DRAFT")
        self.assertEqual(fields["review_assignment"]["status"], "UNASSIGNED")


class SubjectChangeTests(unittest.TestCase):
    def test_edit_cannot_move_question_to_another_subject(self):
        from modules.questions.schemas import QuestionUpdateRequest
        from modules.questions.service import QuestionService

        subject_id = ObjectId()
        version = {"classification": {"subject": {"id": subject_id}, "chapter": {"id": None}}}

        class Repository:
            def find_pair(self, _question_id):
                return {"_id": ObjectId(), "review_status": "DRAFT"}, version

            def create_version(self, *_args, **_kwargs):
                raise AssertionError("must not write")

        service = QuestionService(repository=Repository(), references=object())
        with self.assertRaisesRegex(ValueError, "đổi học phần"):
            service.update(
                str(ObjectId()),
                QuestionUpdateRequest(expected_version=1, subject_id=str(ObjectId())),
                ObjectId(),
            )


class ArchiveDuringReviewTests(unittest.TestCase):
    def _service(self, question):
        from modules.questions.service import QuestionService

        version = {"_id": ObjectId(), "created_by_user_id": question["created_by_user_id"],
                   "classification": {"subject": {"id": None}}}

        class Repository:
            calls = []

            def find_pair(self, _question_id):
                return (question, version) if question.get("lifecycle_status") == "ACTIVE" else None

            def archive(self, _question_id, *, audit=None, expected_review_status=None, extra_fields=None):
                if question["review_status"] != expected_review_status:
                    return False
                self.calls.append((audit, extra_fields))
                question.update(extra_fields or {}, lifecycle_status="ARCHIVED")
                return True

        repository = Repository()
        return QuestionService(repository=repository, references=object()), repository

    def test_owner_must_withdraw_and_admin_force_archive_releases_claim(self):
        from tests.test_schema_v2 import _current_user

        owner = _current_user("Teacher")
        admin = _current_user("Admin")
        reviewer_id = ObjectId()
        question = {
            "_id": ObjectId(), "created_by_user_id": owner.id, "lifecycle_status": "ACTIVE",
            "review_status": "PENDING", "shared_scope": "PRIVATE",
            "review_assignment": {
                "status": "IN_REVIEW", "reviewer_user_id": reviewer_id, "renewal_count": 2,
                "lock_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
            },
        }
        service, repository = self._service(question)
        with self.assertRaisesRegex(ValueError, "rút lại"):
            service.archive(str(question["_id"]), owner)
        with self.assertRaisesRegex(ValueError, "lý do"):
            service.archive(str(question["_id"]), admin)
        displaced = []
        self.assertTrue(service.archive(
            str(question["_id"]), admin, reason="Câu trùng đề thi cũ",
            on_reviewer_displaced=lambda *args: displaced.append(args),
        ))
        audit, extra = repository.calls[0]
        self.assertEqual(extra["review_assignment"]["status"], "UNASSIGNED")
        self.assertEqual(extra["review_assignment"]["last_renewal_count"], 2)
        self.assertTrue(audit["metadata"]["forced"])
        self.assertEqual(displaced[0][2], reviewer_id)
        self.assertEqual(displaced[0][3], "Câu trùng đề thi cũ")


class SharedBankAccessTests(unittest.TestCase):
    def _fixture(self, *, approved: bool):
        from modules.questions.service import QuestionService
        from tests.test_schema_v2 import _current_user

        owner = _current_user("Teacher")
        current_id, approved_id = ObjectId(), ObjectId()
        question = {
            "_id": ObjectId(), "created_by_user_id": owner.id, "lifecycle_status": "ACTIVE",
            "review_status": "DRAFT", "shared_scope": "PRIVATE", "shared_with_user_ids": [],
            "current_version_id": current_id, "approved_version_id": approved_id if approved else None,
            "grounding_required": True,
        }
        current = {"_id": current_id, "created_by_user_id": owner.id, "content": "Bản nháp sau duyệt",
                   "classification": {"subject": {"id": None}}, "document_id": None, "sources": []}
        approved_version = {**current, "_id": approved_id, "content": "Bản đã duyệt"}
        created = []

        class Repository:
            def find_pair(self, _question_id):
                return question, current

            def list_versions(self, _question_id):
                return [current, approved_version]

            def create(self, aggregate, version):
                created.append((aggregate, version))
                return aggregate, version

        return QuestionService(repository=Repository(), references=object()), question, owner, created

    def test_clone_by_shared_user_copies_approved_version_and_grounding(self):
        from tests.test_schema_v2 import _current_user

        service, question, _owner, created = self._fixture(approved=True)
        viewer = _current_user("Teacher")
        question["shared_with_user_ids"] = [viewer.id]
        service.duplicate(str(question["_id"]), viewer)
        aggregate, version = created[0]
        self.assertEqual(version["content"], "Bản đã duyệt")
        self.assertTrue(aggregate["grounding_required"])
        self.assertEqual(aggregate["created_by_user_id"], viewer.id)

    def test_clone_requires_approved_version_for_shared_user(self):
        from tests.test_schema_v2 import _current_user

        service, question, _owner, created = self._fixture(approved=False)
        viewer = _current_user("Teacher")
        question["shared_with_user_ids"] = [viewer.id]
        with self.assertRaisesRegex(ValueError, "được duyệt"):
            service.duplicate(str(question["_id"]), viewer)
        self.assertEqual(created, [])

    def test_owner_clone_copies_current_version(self):
        service, question, owner, created = self._fixture(approved=False)
        service.duplicate(str(question["_id"]), owner)
        self.assertEqual(created[0][1]["content"], "Bản nháp sau duyệt")

    def test_reviewer_scope_read_does_not_allow_clone(self):
        from tests.test_schema_v2 import _current_user

        service, question, _owner, created = self._fixture(approved=True)
        reviewer = _current_user("Reviewer")
        with unittest.mock.patch.object(service, "_ensure_read_access", side_effect=lambda pair, _user: pair):
            with self.assertRaises(PermissionError):
                service.duplicate(str(question["_id"]), reviewer)
        self.assertEqual(created, [])

    def test_shared_question_user_cannot_download_private_source_document(self):
        from modules.questions.service import QuestionService
        from tests.test_schema_v2 import _current_user

        owner, viewer = _current_user("Teacher"), _current_user("Teacher")
        document_id = ObjectId()
        question = {"_id": ObjectId(), "created_by_user_id": owner.id, "shared_with_user_ids": [viewer.id],
                    "shared_scope": "PRIVATE", "lifecycle_status": "ACTIVE"}
        version = {"_id": ObjectId(), "created_by_user_id": owner.id, "document_id": document_id,
                   "classification": {"subject": {"id": None}}}
        document = {"_id": document_id, "uploaded_by_user_id": owner.id, "shared_with_user_ids": [],
                    "shared_scope": "PRIVATE", "original_filename": "a.pdf", "current_version": 1, "artifacts": []}

        class Repository:
            def find_pair(self, _question_id):
                return question, version

        class References:
            def find_document(self, _document_id):
                return document

        service = QuestionService(repository=Repository(), references=References())
        with self.assertRaises(PermissionError):
            service.source_pdf_artifact(str(question["_id"]), viewer)
        document["shared_with_user_ids"] = [viewer.id]
        self.assertIsNone(service.source_pdf_artifact(str(question["_id"]), viewer))

    def test_subject_wide_share_hidden_until_approved_and_needs_shared_bank_permission(self):
        from modules.questions.service import QuestionService
        from tests.test_schema_v2 import _current_user

        subject_id = ObjectId()
        teacher = _current_user("Teacher")
        question = {"shared_scope": "SUBJECT", "subject_id": subject_id, "approved_version_id": None,
                    "shared_with_user_ids": []}
        service = QuestionService(repository=object(), references=object())
        with unittest.mock.patch.object(QuestionService, "_teaching_subject_ids", return_value={str(subject_id)}):
            self.assertFalse(service._is_shared_question(question, teacher))
            question["approved_version_id"] = ObjectId()
            self.assertTrue(service._is_shared_question(question, teacher))
            no_bank = _current_user("Teacher", permissions=[
                item for item in teacher.permissions if item != "questions.use_shared_bank"
            ])
            self.assertFalse(service._is_shared_question(question, no_bank))

    def test_question_list_drops_shared_clauses_without_shared_bank_permission(self):
        from modules.questions.service import QuestionService
        from tests.test_schema_v2 import _current_user

        teacher = _current_user("Teacher", permissions=[
            item for item in _current_user("Teacher").permissions if item != "questions.use_shared_bank"
        ])
        captured = {}

        class Repository:
            def list(self, *_args, **kwargs):
                captured.update(kwargs)
                return [], 0

        service = QuestionService(repository=Repository(), references=object())
        with unittest.mock.patch.object(QuestionService, "_teaching_subject_ids", return_value={"s"}):
            service.list(1, 20, None, None, current_user=teacher)
        self.assertFalse(captured["include_shared"])
        self.assertEqual(captured["shared_subject_ids"], [])


class DocumentLifecycleTests(unittest.TestCase):
    def _repository(self, document, *, active_jobs=0):
        from unittest.mock import MagicMock

        from modules.documents.repository import MongoDocumentRepository

        db = MagicMock()
        db.documents.find_one.return_value = document
        db.document_jobs.find_one.return_value = None
        db.document_jobs.count_documents.return_value = active_jobs
        db.documents.update_one.return_value.matched_count = 1
        return MongoDocumentRepository(db), db

    def test_reindex_job_keeps_ready_document_ready(self):
        document = {"_id": ObjectId(), "status": "READY", "current_version": 1,
                    "current_processing": {"chunk_set_id": ObjectId()},
                    "pipeline_summary": {"index_status": "COMPLETED"}}
        repository, db = self._repository(document)
        job = repository.create_job(document["_id"], "INDEX")
        self.assertTrue(job["preserves_active_pipeline"])
        fields = db.documents.update_one.call_args.args[1]["$set"]
        self.assertNotIn("status", fields)

    def test_mongo_recovery_restores_ready_for_preserving_job(self):
        from unittest.mock import MagicMock

        from core.job_recovery import _recover_document_jobs

        job = {"_id": ObjectId(), "document_id": ObjectId(), "job_type": "INDEX",
               "preserves_active_pipeline": True,
               "previous_document_state": {"status": "READY", "pipeline_status": "COMPLETED"}}
        db = MagicMock()
        db.document_jobs.find.return_value = [job]
        now = datetime.now(timezone.utc)
        _recover_document_jobs(db, now, now, "stale")
        fields = db.documents.update_one.call_args.args[1]["$set"]
        self.assertEqual(fields["status"], "READY")
        self.assertEqual(fields["pipeline_summary.index_status"], "COMPLETED")

    def test_archive_blocked_while_processing_and_clears_pending(self):
        from contextlib import nullcontext

        from modules.documents.repository import DocumentBusyError

        document = {"_id": ObjectId(), "status": "READY"}
        with unittest.mock.patch("modules.documents.repository.mongo_transaction", return_value=nullcontext()):
            repository, _db = self._repository(document, active_jobs=1)
            with self.assertRaises(DocumentBusyError):
                repository.archive(document["_id"])
            repository, db = self._repository(document)
            self.assertTrue(repository.archive(document["_id"]))
        self.assertEqual(db.documents.update_one.call_args.args[1]["$set"]["pending_processing"], {})

    def test_restore_returns_ready_only_with_active_index(self):
        from modules.documents.service import DocumentService
        from tests.test_schema_v2 import _current_user

        owner, stranger = _current_user("Teacher"), _current_user("Teacher")
        archived = {"_id": ObjectId(), "uploaded_by_user_id": owner.id, "title": "T",
                    "current_processing": {"chunk_set_id": ObjectId()}}
        restored_with = []

        class Repository:
            def find_archived(self, _document_id):
                return archived

            def restore(self, _document_id, *, status, audit=None):
                restored_with.append((status, audit))
                return None

        service = DocumentService(Repository())
        with self.assertRaises(LookupError):
            service.restore(str(archived["_id"]), stranger)
        service.restore(str(archived["_id"]), owner)
        archived["current_processing"] = {}
        service.restore(str(archived["_id"]), owner)
        self.assertEqual([item[0] for item in restored_with], ["READY", "FAILED"])
        self.assertEqual(restored_with[0][1]["action"], "document.restored")

    def test_lineage_delete_refused_when_chunks_are_cited(self):
        from unittest.mock import MagicMock

        from modules.rag.lineage import LineagePromotionService

        db = MagicMock()
        db.document_chunks.distinct.return_value = [ObjectId()]
        service = LineagePromotionService(db=db, validator=object())
        with unittest.mock.patch("modules.rag.lineage.count_versions_citing_chunks", return_value=2):
            with self.assertRaisesRegex(ValueError, "cited by 2"):
                service._ensure_not_cited(ObjectId())
        with unittest.mock.patch("modules.rag.lineage.count_versions_citing_chunks", return_value=0):
            service._ensure_not_cited(ObjectId())


class MarkdownRuleContextTests(unittest.TestCase):
    SECTIONS = [
        "Mục lục: [A]\nNội dung: Phần đầu\n\n---\n\nPhần sau của cùng chunk A",
        "Mục lục: [B]\nNội dung: Nội dung chunk B",
    ]

    def test_focused_snapshot_keeps_chunk_with_markdown_rule_aligned(self):
        from modules.generation.question import _focused_context_snapshot

        snapshot = {"context_text": "\n\n---\n\n".join(self.SECTIONS), "context_sections": list(self.SECTIONS),
                    "results": [{"chunk_id": "a"}, {"chunk_id": "b"}]}
        focused, _ = _focused_context_snapshot(snapshot, 1)
        self.assertEqual(focused["context_text"], self.SECTIONS[1])
        self.assertEqual(focused["results"], [{"chunk_id": "b"}])
        # Snapshots persisted before context_sections existed still split only at section headers.
        legacy, _ = _focused_context_snapshot({**snapshot, "context_sections": None}, 1)
        self.assertEqual(legacy["results"], [{"chunk_id": "b"}])

    def test_evidence_sections_include_text_after_markdown_rule(self):
        from modules.generation.postprocessing import _context_evidence_sections

        sections = _context_evidence_sections("\n\n---\n\n".join(self.SECTIONS))
        self.assertEqual(len(sections), 2)
        self.assertIn("Phần sau của cùng chunk A", sections[0])


class ExamMatrixFinalizeTests(unittest.TestCase):
    @staticmethod
    def _version(bloom, difficulty, chapter=None):
        return {"classification": {"bloom": {"level": bloom}, "difficulty": difficulty, "chapter": {"id": chapter}}}

    def test_each_matrix_row_must_be_filled(self):
        from modules.exams.service import _matrix_shortages

        chapter = ObjectId()
        matrix = [
            {"chapter_id": None, "cognitive_level": "nhan_biet", "difficulty": "de", "count": 1},
            {"chapter_id": chapter, "cognitive_level": "nhan_biet", "difficulty": "de", "count": 1},
        ]
        in_chapter = ("q1", self._version(1, "de", str(chapter)))
        elsewhere = ("q2", self._version(1, "de", str(ObjectId())))
        self.assertEqual(_matrix_shortages(matrix, [elsewhere, in_chapter]), [])
        wrong_level = ("q3", self._version(2, "de", str(chapter)))
        shortages = _matrix_shortages(matrix, [in_chapter, wrong_level])
        self.assertEqual(len(shortages), 1)
        self.assertIn("cần 1, có 0", shortages[0])

    def test_content_edit_fails_when_status_changed_meanwhile(self):
        from modules.exams.service import ExamService

        class Repository:
            def update(self, _exam_id, _updates, *, expected_status=None, **_kwargs):
                self.expected = expected_status
                return None

        service = ExamService.__new__(ExamService)
        service.repository = Repository()
        with self.assertRaisesRegex(ValueError, "vừa thay đổi"):
            service._update_unchanged("e1", {"status": "READY"}, {"name": "x"})
        self.assertEqual(service.repository.expected, "READY")


class ReviewerScopeTests(unittest.TestCase):
    def setUp(self):
        self.reviewer = ObjectId()
        self.subject = ObjectId()
        self.scope = ReviewerScope(self.reviewer, [str(self.subject)])

    def test_pending_question_in_reviewed_subject_is_readable(self):
        self.assertTrue(reviewer_can_read(_pending(subject_id=self.subject), {}, self.scope, lambda: False))

    def test_pending_question_in_other_subject_is_hidden(self):
        self.assertFalse(reviewer_can_read(_pending(subject_id=ObjectId()), {}, self.scope, lambda: False))

    def test_assignment_and_past_decision_grant_read(self):
        assigned = _pending(subject_id=ObjectId(), review_assignment={"reviewer_user_id": self.reviewer})
        self.assertTrue(reviewer_can_read(assigned, {}, self.scope, lambda: False))
        approved = {"review_status": "APPROVED", "subject_id": ObjectId()}
        self.assertFalse(reviewer_can_read(approved, {}, self.scope, lambda: False))
        self.assertTrue(reviewer_can_read(approved, {}, self.scope, lambda: True))

    def test_reviewer_without_subjects_covers_no_subject(self):
        scope = ReviewerScope(self.reviewer, [])
        self.assertFalse(reviewer_can_read(_pending(subject_id=ObjectId()), {}, scope, lambda: False))


class HeadingScopeTests(unittest.TestCase):
    def _matches(self, target, **meta):
        return _heading_matches_target(meta, _normalize_heading_text(target))

    def test_sub_heading_chunks_belong_to_selected_section(self):
        self.assertTrue(self._matches(
            "Chương 2: Ngăn xếp",
            heading="2.1.3 Thao tác push",
            heading_path=["Chương 2 - Ngăn xếp", "2.1 Cài đặt", "2.1.3 Thao tác push"],
        ))

    def test_section_number_covers_its_sub_sections(self):
        self.assertTrue(self._matches("2.1 Cài đặt mảng", heading_path=["Chương 2", "2.1. Cài đặt", "2.1.3 Push"]))
        self.assertFalse(self._matches("2.1 Cài đặt mảng", heading_path=["Chương 2", "2.2 Hàng đợi"]))

    def test_unrelated_section_does_not_match(self):
        self.assertFalse(self._matches("Hàng đợi", heading_path=["Chương 2", "Ngăn xếp"]))

    def test_warnings_for_narrow_and_missing_sections(self):
        narrow = heading_scope_warnings({"target": "Ngăn xếp", "status": "NARROW", "matched_chunks": 3}, 20)
        self.assertEqual(len(narrow), 1)
        self.assertIn("khoảng 6 câu", narrow[0])
        self.assertEqual(heading_scope_warnings({"status": "MATCHED", "matched_chunks": 10}, 20), [])
        missing = heading_scope_warnings({"target": "Ngăn xếp", "status": "NO_MATCH", "matched_chunks": 0}, 5)
        self.assertIn("toàn bộ tài liệu", missing[0])
        self.assertEqual(heading_scope_warnings({"status": "NOT_REQUESTED"}, 5), [])
        self.assertEqual(heading_scope_warnings(None, 5), [])


if __name__ == "__main__":
    unittest.main()
