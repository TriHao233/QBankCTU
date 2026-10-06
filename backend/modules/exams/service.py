from __future__ import annotations

import random
from copy import deepcopy
from typing import Any

from bson import ObjectId

from core.bootstrap import SCHEMA_VERSION
from core.config import settings
from core.database import get_database
from core.dependencies import CurrentUser
from modules.catalog.postgres_subject_repository import subject_record
from modules.catalog.subject_approval import ensure_subject_approved
from modules.exams.repository import (
    ExamRepository,
    ExamVariantRepository,
    MongoExamRepository,
    MongoExamVariantRepository,
    json_safe,
    object_id,
    utc_now,
)
from modules.exams.schemas import (
    MAX_SEED,
    MAX_VARIANTS_PER_EXAM,
    COGNITIVE_LEVEL_TO_BLOOM,
    AddQuestionsManualRequest,
    ExamCreateRequest,
    ExamMatrixRequest,
    ExamOwnerTransferRequest,
    ExamSharingRequest,
    ExamStatus,
    ExamStatusUpdateRequest,
    ExamUpdateRequest,
    ExamVariantCreateRequest,
    MatrixCell,
)
from modules.questions.repository import MongoQuestionRepository, serialize_question
from modules.users.store import ensure_teaches_subjects, users_by_ids

EDITABLE_EXAM_STATUSES = {"DRAFT", "READY"}
LOCKED_EXAM_STATUSES = {"FINALIZED", "PUBLISHED", "ARCHIVED"}
STATUS_ALIASES = {
    "draft": "DRAFT",
    "ready": "READY",
    "finalized": "FINALIZED",
    "published": "PUBLISHED",
    "archived": "ARCHIVED",
}


CRITICAL_EXAM_STATUSES = {"FINALIZED", "PUBLISHED", "ARCHIVED"}

ITEM_FLAG_MESSAGES = {
    "archived_item": "đã bị lưu trữ",
    "version_changed": "không còn ở version đã duyệt đã chọn",
    "access_revoked": "không còn thuộc quyền sử dụng của chủ đề thi",
}


def _new_seed() -> int:
    return random.SystemRandom().randint(0, MAX_SEED)


def _exam_audit(action: str, exam: dict, current_user: CurrentUser | None, *,
                before: dict | None = None, after: dict | None = None,
                metadata: dict | None = None) -> dict:
    return {
        "action": action,
        "entity_type": "exam",
        "entity_id": exam["_id"],
        "actor_user_id": current_user.id if current_user else None,
        "actor_role": current_user.role if current_user else None,
        "before": before or {},
        "after": after or {},
        "metadata": {"name": exam.get("name"), "status": exam.get("status"), **(metadata or {})},
    }


def _exam_status(exam: dict) -> str:
    raw_status = str(exam.get("status") or "DRAFT")
    return STATUS_ALIASES.get(raw_status, raw_status.upper())


def _matrix_cell_dict(cell: MatrixCell) -> dict:
    return {
        "chapter_id": object_id(cell.chapter_id, "chapter_id") if cell.chapter_id else None,
        "cognitive_level": cell.cognitive_level.value,
        "difficulty": cell.difficulty.value,
        "count": cell.count,
    }


def _version_matches_cell(version: dict, cell: dict) -> bool:
    classification = version.get("classification") or {}
    bloom = COGNITIVE_LEVEL_TO_BLOOM.get(cell.get("cognitive_level"))
    if bloom is not None and (classification.get("bloom") or {}).get("level") != bloom:
        return False
    if cell.get("difficulty") and classification.get("difficulty") != cell["difficulty"]:
        return False
    chapter = cell.get("chapter_id")
    return not chapter or str((classification.get("chapter") or {}).get("id")) == str(chapter)


def _matrix_shortages(matrix: list[dict], versions: list[tuple[str, dict]]) -> list[str]:
    """Rows of the matrix the exam's questions do not fill; each question counts for one row.

    Rows bound to a chapter are filled before chapter-agnostic rows so the latter
    cannot consume questions a specific row needs.
    """
    used: set[str] = set()
    shortages = []
    for cell in sorted(matrix, key=lambda item: item.get("chapter_id") is None):
        wanted = int(cell.get("count") or 0)
        matched = [qid for qid, version in versions if qid not in used and _version_matches_cell(version, cell)]
        used.update(matched[:wanted])
        if len(matched) < wanted:
            shortages.append(
                f"{cell.get('cognitive_level')}/{cell.get('difficulty')}"
                + (f"/chương {cell['chapter_id']}" if cell.get("chapter_id") else "")
                + f": cần {wanted}, có {len(matched)}"
            )
    return shortages


def serialize_exam(exam: dict, variant_count: int = 0, *, item_flags: list[dict] | None = None,
                   can_edit: bool = True, can_export_official: bool | None = None) -> dict:
    reopen_history = [
        {
            "voided_at": snapshot.get("voided_at"),
            "voided_by_user_id": snapshot.get("voided_by_user_id"),
            "reason": snapshot.get("void_reason"),
            "finalized_at": snapshot.get("created_at"),
        }
        for snapshot in exam.get("voided_snapshots") or []
    ]
    return json_safe(
        {
            "id": exam["_id"],
            "name": exam["name"],
            "exam_title": exam["exam_title"],
            "subject_id": exam["subject_id"],
            "question_count": exam["question_count"],
            "header": exam["header"],
            "matrix": exam.get("matrix", []),
            "questions": exam.get("questions", []),
            "status": _exam_status(exam),
            "variant_count": variant_count,
            "delivery_mode": exam.get("delivery_mode", "paper"),
            "time_limit_seconds": exam.get("time_limit_seconds"),
            "scoring_config": exam.get("scoring_config"),
            "lms_export_status": exam.get("lms_export_status", "not_exported"),
            "created_by_user_id": exam.get("created_by_user_id"),
            "shared_with_user_ids": exam.get("shared_with_user_ids") or [],
            "can_edit": can_edit,
            "can_export_official": can_edit if can_export_official is None else can_export_official,
            "selection_seed": exam.get("selection_seed"),
            "item_flags": item_flags or [],
            "reopen_history": reopen_history,
            "published_at": exam.get("published_at"),
            "official_exported_at": exam.get("official_exported_at"),
            "created_at": exam["created_at"],
            "updated_at": exam["updated_at"],
        }
    )


ANSWER_FIELD_PREFIXES = (
    "correct", "answer", "explanation", "solution", "rubric", "sample_answer", "expected", "grading",
    "structured_answer",
)


def _without_answer_fields(value: dict) -> dict:
    return {key: item for key, item in value.items() if not str(key).lower().startswith(ANSWER_FIELD_PREFIXES)}


def _snapshot_without_answers(snapshot: dict) -> dict:
    stripped = _without_answer_fields(snapshot or {})
    if isinstance(stripped.get("question_data"), dict):
        stripped["question_data"] = _without_answer_fields(stripped["question_data"])
    return stripped


def _questions_without_answers(questions: list[dict]) -> list[dict]:
    return [
        {**entry, "content_snapshot": _snapshot_without_answers(entry.get("content_snapshot") or {})}
        for entry in questions or []
    ]


def serialize_variant(variant: dict) -> dict:
    return json_safe(
        {
            "id": variant["_id"],
            "exam_id": variant["exam_id"],
            "exam_code": variant["exam_code"],
            "questions": variant["questions"],
            "answer_key": variant["answer_key"],
            "shuffle_seed": variant.get("shuffle_seed"),
            "created_at": variant["created_at"],
        }
    )


class ExamService:
    def __init__(self, repository: ExamRepository, question_repository: MongoQuestionRepository):
        self.repository = repository
        self.question_repository = question_repository

    def _get_or_404(self, exam_id: str) -> dict:
        exam = self.repository.find(exam_id)
        if not exam:
            raise LookupError("Không tìm thấy đề thi")
        return exam

    @staticmethod
    def _can_edit(exam: dict, current_user: CurrentUser | None) -> bool:
        if current_user is None:
            return False
        return current_user.role == "Admin" or str(exam.get("created_by_user_id")) == str(current_user.id)

    @staticmethod
    def _assert_can_access(exam: dict, current_user: CurrentUser | None, *, write: bool = True) -> None:
        if current_user is None:
            raise PermissionError("Bạn chưa đăng nhập")
        if ExamService._can_edit(exam, current_user):
            return
        if str(current_user.id) in {str(item) for item in exam.get("shared_with_user_ids") or []}:
            if not write:
                return
            raise PermissionError("Đề thi chỉ được chia sẻ quyền xem")
        # Other users' exams look missing rather than forbidden.
        raise LookupError("Không tìm thấy đề thi")

    def _get_for_user_or_404(self, exam_id: str, current_user: CurrentUser | None, *, write: bool = True) -> dict:
        exam = self._get_or_404(exam_id)
        self._assert_can_access(exam, current_user, write=write)
        return exam

    @staticmethod
    def _exam_owner_is_admin(exam: dict, actor: CurrentUser | None) -> bool:
        owner_id = exam.get("created_by_user_id")
        if actor is not None and str(actor.id) == str(owner_id):
            return actor.role == "Admin"
        if not owner_id or not ObjectId.is_valid(str(owner_id)):
            return False
        users = users_by_ids(get_database(), [ObjectId(str(owner_id))], active_only=False)
        return bool(users) and users[0].get("role") == "Admin"

    def _owner_can_use_question(self, exam: dict, question: dict, version: dict,
                                actor: CurrentUser | None) -> bool:
        """Whether the exam owner (not whoever is acting) may put this question into the exam."""
        owner_id = str(exam.get("created_by_user_id"))
        question_owner = question.get("created_by_user_id") or version.get("created_by_user_id")
        if owner_id == str(question_owner):
            return True
        if owner_id in {str(item) for item in question.get("shared_with_user_ids") or []}:
            return True
        if question.get("shared_scope") == "SUBJECT" and str(question.get("subject_id")) == str(exam.get("subject_id")):
            return True
        return self._exam_owner_is_admin(exam, actor)

    def _exam_question_visibility(self, exam: dict, actor: CurrentUser | None) -> dict:
        """Question-list filter for the bank the exam owner may draw from."""
        if self._exam_owner_is_admin(exam, actor):
            return {}
        return {
            "visible_to_user_id": ObjectId(str(exam["created_by_user_id"])),
            "shared_subject_ids": [str(exam["subject_id"])],
        }

    def _item_flags(self, exam: dict, actor: CurrentUser | None) -> list[dict]:
        """Problems that block finalizing a draft exam; locked exams keep their snapshot."""
        if _exam_status(exam) not in EDITABLE_EXAM_STATUSES:
            return []
        flags = []
        for ref in exam.get("questions", []):
            question_id = str(ref["question_id"])
            codes: list[str] = []
            pair = self.question_repository.find_approved_pair(question_id)
            if not pair:
                find_archived = getattr(self.question_repository, "find_archived_pair", None)
                codes.append("archived_item" if find_archived and find_archived(question_id) else "version_changed")
            else:
                question, version = pair
                if str(ref.get("version_id")) != str(version["_id"]):
                    codes.append("version_changed")
                if not self._owner_can_use_question(exam, question, version, actor):
                    codes.append("access_revoked")
            if codes:
                snapshot = ref.get("content_snapshot") or {}
                flags.append({
                    "question_id": question_id,
                    "question_code": snapshot.get("question_code"),
                    "flags": codes,
                })
        return flags

    def _serialize(self, exam: dict, current_user: CurrentUser | None) -> dict:
        exam_id = exam["_id"]
        can_edit = self._can_edit(exam, current_user)
        if not can_edit:
            exam = {**exam, "questions": _questions_without_answers(exam.get("questions") or [])}
        return serialize_exam(
            exam, self.repository.count_variants(exam_id),
            item_flags=self._item_flags(exam, current_user),
            can_edit=can_edit,
            can_export_official=(
                current_user is not None and str(exam.get("created_by_user_id")) == str(current_user.id)
            ),
        )

    @staticmethod
    def _assert_mutable(exam: dict) -> None:
        if _exam_status(exam) in LOCKED_EXAM_STATUSES:
            raise ValueError("Đề thi đã chốt hoặc lưu trữ, không thể chỉnh sửa nội dung")

    @staticmethod
    def _question_subject_id(version: dict) -> str | None:
        subject = (version.get("classification") or {}).get("subject") or {}
        subject_id = subject.get("id") if isinstance(subject, dict) else subject
        return str(subject_id) if subject_id else None

    def _assert_question_can_join_exam(self, exam: dict, question: dict, version: dict) -> None:
        # Exams pin the approved version; a newer draft or a pending re-review of the
        # question does not remove the already-approved version from exams.
        if not question.get("approved_version_id"):
            raise ValueError(f"Câu hỏi {question.get('question_code')} chưa được duyệt")
        if str(question.get("approved_version_id")) != str(version["_id"]):
            raise ValueError(f"Câu hỏi {question.get('question_code')} không còn ở version đã duyệt")
        question_subject_id = self._question_subject_id(version)
        if not question_subject_id or question_subject_id != str(exam["subject_id"]):
            raise ValueError(f"Câu hỏi {question.get('question_code')} không thuộc môn của đề thi")

    def _is_question_usable_for_exam(self, exam: dict, question: dict, version: dict) -> bool:
        try:
            self._assert_question_can_join_exam(exam, question, version)
        except ValueError:
            return False
        return True

    def _validate_ready_payload(self, exam: dict, actor: CurrentUser | None = None) -> None:
        header = exam.get("header") or {}
        for field, label in (
            ("school_name", "tên trường"),
            ("exam_name", "tên kỳ thi"),
            ("subject_name", "tên môn học"),
        ):
            if not str(header.get(field) or "").strip():
                raise ValueError(f"Cần nhập {label} trước khi chốt đề giấy")
        questions = exam.get("questions", [])
        if len(questions) != int(exam["question_count"]):
            raise ValueError(
                f"Đề thi cần đúng {exam['question_count']} câu trước khi chốt"
            )
        matrix_total = sum(int(cell.get("count", 0)) for cell in exam.get("matrix", []))
        if exam.get("matrix") and matrix_total != int(exam["question_count"]):
            raise ValueError("Tổng số câu trong ma trận phải bằng số câu của đề thi")
        flags = self._item_flags(exam, actor)
        if flags:
            details = "; ".join(
                f"{item.get('question_code') or item['question_id']} "
                + ", ".join(ITEM_FLAG_MESSAGES[code] for code in item["flags"])
                for item in flags
            )
            raise ValueError(f"Cần xử lý các câu hỏi bị gắn cờ trước khi chốt đề: {details}")
        versions = []
        for ref in questions:
            question, version = self.question_repository.find_approved_pair(str(ref["question_id"]))
            self._assert_question_can_join_exam(exam, question, version)
            versions.append((str(question["_id"]), version))
        shortages = _matrix_shortages(exam.get("matrix") or [], versions)
        if shortages:
            raise ValueError("Câu hỏi trong đề chưa khớp ma trận: " + "; ".join(shortages))

    @staticmethod
    def _finalized_snapshot(exam: dict) -> dict:
        return {
            "subject_id": exam["subject_id"],
            "question_count": exam["question_count"],
            "header": exam["header"],
            "matrix": exam.get("matrix", []),
            "questions": exam.get("questions", []),
            "selection_seed": exam.get("selection_seed"),
            "created_at": utc_now(),
        }

    @staticmethod
    def _assert_subject_approved(subject_id: ObjectId) -> None:
        subject = subject_record(get_database(), subject_id)
        if not subject:
            raise ValueError("Học phần không tồn tại hoặc đã ngừng hoạt động")
        ensure_subject_approved(subject, "tạo đề thi")

    @staticmethod
    def _assert_teaches_subject(subject_id, current_user: CurrentUser | None) -> None:
        ensure_teaches_subjects(get_database(), current_user, [subject_id], "làm đề thi cho học phần này")

    def create_exam(
        self, payload: ExamCreateRequest, created_by_user_id: ObjectId, *, actor: CurrentUser | None = None,
    ) -> dict:
        self._assert_subject_approved(object_id(payload.subject_id, "subject_id"))
        self._assert_teaches_subject(payload.subject_id, actor)
        now = utc_now()
        exam = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "name": payload.name,
            "exam_title": payload.exam_title,
            "subject_id": object_id(payload.subject_id, "subject_id"),
            "question_count": payload.question_count,
            "header": payload.header.model_dump(),
            "matrix": [],
            "questions": [],
            "status": ExamStatus.DRAFT.value,
            "delivery_mode": "paper",
            "time_limit_seconds": None,
            "scoring_config": None,
            "lms_export_status": "not_exported",
            "created_by_user_id": created_by_user_id,
            "created_at": now,
            "updated_at": now,
        }
        self.repository.create(exam)
        return serialize_exam(exam, 0)

    def get_exam(self, exam_id: str, current_user: CurrentUser) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user, write=False)
        return self._serialize(exam, current_user)

    def duplicate_exam(self, exam_id: str, current_user: CurrentUser) -> dict:
        # A view-only share must not yield a copy carrying the answer key.
        source = self._get_for_user_or_404(exam_id, current_user, write=True)
        snapshot = source.get("finalized_snapshot") or {}
        self._assert_teaches_subject(snapshot.get("subject_id") or source["subject_id"], current_user)
        now = utc_now()
        clone = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "name": f"{source['name']} (bản sao)",
            "exam_title": source["exam_title"],
            "subject_id": snapshot.get("subject_id") or source["subject_id"],
            "question_count": snapshot.get("question_count") or source["question_count"],
            "header": deepcopy(snapshot.get("header") or source["header"]),
            "matrix": deepcopy(snapshot.get("matrix") or source.get("matrix", [])),
            "questions": deepcopy(snapshot.get("questions") or source.get("questions", [])),
            "status": ExamStatus.DRAFT.value,
            "delivery_mode": source.get("delivery_mode", "paper"),
            "time_limit_seconds": source.get("time_limit_seconds"),
            "scoring_config": deepcopy(source.get("scoring_config")),
            "lms_export_status": "not_exported",
            "created_by_user_id": current_user.id,
            "created_at": now,
            "updated_at": now,
        }
        self.repository.create(clone)
        return serialize_exam(clone, 0)

    def list_exams(self, page: int, page_size: int, current_user: CurrentUser | None) -> dict:
        visible_to_user_id = (
            current_user.id
            if current_user and current_user.role != "Admin"
            else None
        )
        exams, total = self.repository.list(page, page_size, None, visible_to_user_id=visible_to_user_id)
        items = []
        for exam in exams:
            can_edit = self._can_edit(exam, current_user)
            if not can_edit:
                exam = {**exam, "questions": _questions_without_answers(exam.get("questions") or [])}
            items.append(serialize_exam(
                exam, self.repository.count_variants(exam["_id"]), can_edit=can_edit,
                can_export_official=(
                    current_user is not None and str(exam.get("created_by_user_id")) == str(current_user.id)
                ),
            ))
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def question_pool(
        self,
        exam_id: str,
        current_user: CurrentUser,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
        question_type: str | None = None,
        bloom_level: int | None = None,
        chapter_id: str | None = None,
        difficulty: str | None = None,
    ) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        pairs, total = self.question_repository.list(
            page,
            page_size,
            None,
            search,
            question_type=question_type,
            bloom_level=bloom_level,
            subject_id=str(exam["subject_id"]),
            chapter_id=chapter_id,
            difficulty=difficulty,
            approved_version_join=True,
            **self._exam_question_visibility(exam, current_user),
        )
        selected_ids = {str(ref["question_id"]) for ref in exam.get("questions", [])}
        return {
            "items": [
                {
                    **serialize_question(question, version),
                    "in_exam": str(question["_id"]) in selected_ids,
                }
                for question, version in pairs
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def update_status(
        self,
        exam_id: str,
        payload: ExamStatusUpdateRequest,
        current_user: CurrentUser,
    ) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        current_status = _exam_status(exam)
        target_status = payload.status.value
        if current_status == "ARCHIVED":
            raise ValueError("Đề thi đã lưu trữ")
        if target_status == current_status:
            return self._serialize(exam, current_user)
        if target_status in EDITABLE_EXAM_STATUSES and current_status in LOCKED_EXAM_STATUSES:
            if current_status == "PUBLISHED":
                raise ValueError("Đề thi đã phát hành, không thể quay lại nháp")
            raise ValueError("Đề thi đã chốt; dùng chức năng mở lại đề (cần nêu lý do) để quay về nháp")
        if target_status == "DRAFT":
            updates = {"status": ExamStatus.DRAFT.value}
        elif target_status == "READY":
            self._validate_ready_payload(exam, current_user)
            updates = {"status": ExamStatus.READY.value}
        elif target_status == "FINALIZED":
            if current_status != "READY":
                raise ValueError("Cần chuyển đề thi sang READY trước khi chốt")
            self._validate_ready_payload(exam, current_user)
            updates = {
                "status": ExamStatus.FINALIZED.value,
                "finalized_snapshot": self._finalized_snapshot(exam),
            }
        elif target_status == "PUBLISHED":
            if current_status != "FINALIZED":
                raise ValueError("Chỉ phát hành đề thi đã chốt")
            if self.repository.count_variants(exam_id) < 1:
                raise ValueError("Cần tạo ít nhất một mã đề trước khi phát hành đề thi")
            updates = {
                "status": ExamStatus.PUBLISHED.value,
                "published_at": utc_now(),
                "published_by_user_id": object_id(current_user.id, "user_id"),
            }
        elif target_status == "ARCHIVED":
            if current_status not in {"FINALIZED", "PUBLISHED"}:
                raise ValueError("Chỉ lưu trữ đề thi đã chốt hoặc đã phát hành")
            updates = {"status": ExamStatus.ARCHIVED.value}
        else:
            raise ValueError("Trạng thái đề thi không hợp lệ")
        audit = None
        if target_status in CRITICAL_EXAM_STATUSES:
            audit = _exam_audit(f"exam.{target_status.lower()}", exam, current_user,
                                before={"status": current_status}, after={"status": target_status})
        updated = self.repository.update(exam_id, updates, expected_status=current_status,
                                         **({"audit": audit} if audit else {}))
        if not updated:
            raise ValueError("Trạng thái đề thi vừa thay đổi, hãy tải lại trang")
        return self._serialize(updated, current_user)

    def reopen_exam(self, exam_id: str, reason: str, current_user: CurrentUser) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        if str(exam.get("created_by_user_id")) != str(current_user.id):
            raise PermissionError("Chỉ người tạo đề thi được mở lại đề đã chốt")
        current_status = _exam_status(exam)
        if current_status != "FINALIZED":
            raise ValueError("Chỉ mở lại đề thi đang ở trạng thái đã chốt")
        if exam.get("official_exported_at"):
            raise ValueError("Đề thi đã xuất bản chính thức, không thể mở lại; hãy nhân bản đề thi để chỉnh sửa")
        if self.repository.count_variants(exam_id) > 0:
            raise ValueError("Đề thi đã có mã đề, không thể mở lại; hãy nhân bản đề thi để chỉnh sửa")
        reason = reason.strip()
        if len(reason) < 5:
            raise ValueError("Cần nêu lý do mở lại đề thi")
        now = utc_now()
        voided = {
            **(exam.get("finalized_snapshot") or self._finalized_snapshot(exam)),
            "voided_at": now,
            "voided_by_user_id": object_id(current_user.id, "user_id"),
            "void_reason": reason,
        }
        updates = {
            "status": ExamStatus.DRAFT.value,
            "finalized_snapshot": None,
            "voided_snapshots": [*(exam.get("voided_snapshots") or []), voided],
        }
        audit = _exam_audit("exam.reopened", exam, current_user,
                            before={"status": current_status}, after={"status": "DRAFT"},
                            metadata={"reason": reason})
        try:
            updated = self.repository.update(exam_id, updates, audit=audit,
                                             expected_status="FINALIZED", require_no_variants=True)
        except ValueError as exc:
            raise ValueError("Đề thi đã có mã đề, không thể mở lại; hãy nhân bản đề thi để chỉnh sửa") from exc
        if not updated:
            raise ValueError("Trạng thái đề thi vừa thay đổi, hãy tải lại trang")
        return self._serialize(updated, current_user)

    def update_sharing(self, exam_id: str, payload: ExamSharingRequest, current_user: CurrentUser) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        owner_id = str(exam.get("created_by_user_id"))
        wanted = list(dict.fromkeys(
            str(object_id(item, "shared_with_user_id")) for item in payload.shared_with_user_ids
        ))
        wanted = [item for item in wanted if item != owner_id]
        targets = [ObjectId(item) for item in wanted]
        found = {str(user["_id"]) for user in users_by_ids(get_database(), targets, active_only=True)}
        missing = [item for item in wanted if item not in found]
        if missing:
            raise ValueError("Người được chia sẻ không tồn tại hoặc đã bị vô hiệu hóa")
        before = [str(item) for item in exam.get("shared_with_user_ids") or []]
        audit = _exam_audit("exam.sharing_update", exam, current_user,
                            before={"shared_with_user_ids": before},
                            after={"shared_with_user_ids": wanted})
        updated = self.repository.update(exam_id, {"shared_with_user_ids": targets}, audit=audit)
        if not updated:
            raise LookupError("Không tìm thấy đề thi")
        return self._serialize(updated, current_user)

    def transfer_owner(self, exam_id: str, payload: ExamOwnerTransferRequest, current_user: CurrentUser) -> dict:
        if current_user.role != "Admin":
            raise PermissionError("Chỉ Admin được chuyển chủ sở hữu đề thi")
        exam = self._get_or_404(exam_id)
        new_owner = object_id(payload.new_owner_user_id, "new_owner_user_id")
        users = users_by_ids(get_database(), [new_owner], active_only=True)
        if not users:
            raise ValueError("Người nhận không tồn tại hoặc đã bị vô hiệu hóa")
        if users[0].get("role") not in {"Teacher", "Admin"}:
            raise ValueError("Chỉ chuyển đề thi cho giảng viên hoặc Admin")
        previous = exam.get("created_by_user_id")
        if str(previous) == str(new_owner):
            return self._serialize(exam, current_user)
        shared = [item for item in exam.get("shared_with_user_ids") or [] if str(item) != str(new_owner)]
        audit = _exam_audit("exam.owner_transferred", exam, current_user,
                            before={"created_by_user_id": previous},
                            after={"created_by_user_id": new_owner},
                            metadata={"reason": payload.reason.strip()})
        updated = self.repository.update(
            exam_id, {"created_by_user_id": new_owner, "shared_with_user_ids": shared}, audit=audit,
        )
        if not updated:
            raise LookupError("Không tìm thấy đề thi")
        return self._serialize(updated, current_user)

    def update_exam(self, exam_id: str, payload: ExamUpdateRequest, current_user: CurrentUser) -> dict:
        current_exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(current_exam)
        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = payload.name
        if payload.exam_title is not None:
            updates["exam_title"] = payload.exam_title
        if payload.question_count is not None:
            updates["question_count"] = payload.question_count
        if payload.header is not None:
            updates["header"] = payload.header.model_dump()
        if payload.question_count is not None and _exam_status(current_exam) == "READY":
            updates["status"] = ExamStatus.DRAFT.value
        exam = self._update_unchanged(exam_id, current_exam, updates) if updates else self._get_or_404(exam_id)
        return self._serialize(exam, current_user)

    def _update_unchanged(self, exam_id: str, exam: dict, updates: dict) -> dict:
        """Write content edits only while the exam is still in the status they were checked against."""
        updated = self.repository.update(exam_id, updates, expected_status=_exam_status(exam))
        if not updated:
            raise ValueError("Trạng thái đề thi vừa thay đổi, hãy tải lại trang")
        return updated

    def delete_exam(self, exam_id: str, current_user: CurrentUser) -> None:
        exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(exam)
        if self.repository.count_variants(exam_id) > 0:
            raise ValueError("Không thể xoá đề thi đã có mã đề, hãy xoá mã đề trước")
        audit = _exam_audit("exam.deleted", exam, current_user)
        if not self.repository.delete(
            exam_id, deleted_by_user_id=object_id(current_user.id, "user_id"), audit=audit,
        ):
            raise LookupError("Không tìm thấy đề thi")

    def save_matrix(self, exam_id: str, payload: ExamMatrixRequest, current_user: CurrentUser) -> dict:
        current_exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(current_exam)
        cells = [_matrix_cell_dict(cell) for cell in payload.cells]
        total_count = sum(cell["count"] for cell in cells)
        if total_count > int(current_exam["question_count"]):
            raise ValueError("Tổng số câu trong ma trận vượt quá số câu của đề thi")
        updates: dict[str, Any] = {"matrix": cells}
        if _exam_status(current_exam) == "READY":
            updates["status"] = ExamStatus.DRAFT.value
        exam = self._update_unchanged(exam_id, current_exam, updates)
        return self._serialize(exam, current_user)

    def _find_approved_for_cell(
        self,
        exam: dict,
        cell: dict,
        current_user: CurrentUser | None = None,
    ) -> list[tuple[dict, dict]]:
        bloom_level = COGNITIVE_LEVEL_TO_BLOOM.get(cell["cognitive_level"])
        chapter_id = str(cell["chapter_id"]) if cell.get("chapter_id") else None
        pairs, _total = self.question_repository.list(
            1,
            1000,
            None,
            None,
            bloom_level=bloom_level,
            subject_id=str(exam["subject_id"]),
            chapter_id=chapter_id,
            difficulty=cell["difficulty"],
            approved_version_join=True,
            **self._exam_question_visibility(exam, current_user),
        )
        return [
            pair
            for pair in pairs
            if self._is_question_usable_for_exam(exam, pair[0], pair[1])
        ]

    def matrix_availability(self, exam_id: str, current_user: CurrentUser) -> list[dict]:
        exam = self._get_for_user_or_404(exam_id, current_user, write=False)
        results = []
        for cell in exam.get("matrix", []):
            pairs = self._find_approved_for_cell(exam, cell, current_user)
            available = len(pairs)
            results.append(
                {
                    "chapter_id": json_safe(cell.get("chapter_id")),
                    "cognitive_level": cell["cognitive_level"],
                    "difficulty": cell["difficulty"],
                    "requested": cell["count"],
                    "available": available,
                    "sufficient": available >= cell["count"],
                }
            )
        return results

    def auto_generate_pool(self, exam_id: str, current_user: CurrentUser, seed: int | None = None) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(exam)
        self._assert_teaches_subject(exam["subject_id"], current_user)
        matrix = exam.get("matrix", [])
        if not matrix:
            raise ValueError("Chưa cấu hình ma trận đề thi")
        selection_seed = _new_seed() if seed is None else seed
        rng = random.Random(selection_seed)
        selected_ids: set[str] = set()
        selected_refs: list[dict] = []
        shortages: list[dict] = []
        for cell in matrix:
            pairs = self._find_approved_for_cell(exam, cell, current_user)
            # A stable pool order makes the stored seed reproduce the same selection.
            pool = sorted(
                (pair for pair in pairs if str(pair[0]["_id"]) not in selected_ids),
                key=lambda pair: str(pair[0]["_id"]),
            )
            if len(pool) < cell["count"]:
                shortages.append(
                    {
                        "chapter_id": json_safe(cell.get("chapter_id")),
                        "cognitive_level": cell["cognitive_level"],
                        "difficulty": cell["difficulty"],
                        "requested": cell["count"],
                        "available": len(pool),
                    }
                )
                continue
            chosen = rng.sample(pool, cell["count"])
            for question, version in chosen:
                selected_ids.add(str(question["_id"]))
                selected_refs.append(
                    {
                        "question_id": question["_id"],
                        "version_id": version["_id"],
                        "content_snapshot": serialize_question(question, version),
                    }
                )
        if shortages:
            raise ValueError(
                "Không đủ câu hỏi đã duyệt cho một số nhóm trong ma trận: "
                + str(shortages)
            )
        if len(selected_refs) > exam["question_count"]:
            selected_refs = selected_refs[: exam["question_count"]]
        updates: dict[str, Any] = {"questions": selected_refs, "selection_seed": selection_seed}
        if _exam_status(exam) == "READY":
            updates["status"] = ExamStatus.DRAFT.value
        updated = self._update_unchanged(exam_id, exam, updates)
        return self._serialize(updated, current_user)

    def add_questions_manual(self, exam_id: str, payload: AddQuestionsManualRequest, current_user: CurrentUser) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(exam)
        self._assert_teaches_subject(exam["subject_id"], current_user)
        existing_refs = list(exam.get("questions", []))
        existing_ids = {str(ref["question_id"]) for ref in existing_refs}
        new_refs = []
        for question_id in payload.question_ids:
            if question_id in existing_ids:
                continue
            pair = self.question_repository.find_approved_pair(question_id)
            if not pair:
                raise ValueError(f"Câu hỏi không tồn tại hoặc chưa được duyệt: {question_id}")
            question, version = pair
            if not self._owner_can_use_question(exam, question, version, current_user):
                raise ValueError(f"Câu hỏi không tồn tại hoặc chưa được duyệt: {question_id}")
            self._assert_question_can_join_exam(exam, question, version)
            existing_ids.add(question_id)
            new_refs.append(
                {
                    "question_id": question["_id"],
                    "version_id": version["_id"],
                    "content_snapshot": serialize_question(question, version),
                }
            )
        total = existing_refs + new_refs
        if len(total) > exam["question_count"]:
            raise ValueError(
                f"Tổng số câu hỏi ({len(total)}) vượt quá số câu đã khai báo ({exam['question_count']})"
            )
        updates: dict[str, Any] = {"questions": total}
        if _exam_status(exam) == "READY":
            updates["status"] = ExamStatus.DRAFT.value
        updated = self._update_unchanged(exam_id, exam, updates)
        return self._serialize(updated, current_user)

    def remove_question(self, exam_id: str, question_id: str, current_user: CurrentUser) -> dict:
        exam = self._get_for_user_or_404(exam_id, current_user)
        self._assert_mutable(exam)
        remaining = [
            ref
            for ref in exam.get("questions", [])
            if str(ref["question_id"]) != question_id
        ]
        updates: dict[str, Any] = {"questions": remaining}
        if _exam_status(exam) == "READY":
            updates["status"] = ExamStatus.DRAFT.value
        updated = self._update_unchanged(exam_id, exam, updates)
        return self._serialize(updated, current_user)


class ExamVariantService:
    def __init__(self, exams: ExamRepository, variants: ExamVariantRepository):
        self.exams = exams
        self.variants = variants

    def _get_exam_or_404(self, exam_id: str) -> dict:
        exam = self.exams.find(exam_id)
        if not exam:
            raise LookupError("Không tìm thấy đề thi")
        return exam

    def _get_exam_for_user_or_404(self, exam_id: str, current_user: CurrentUser | None, *,
                                  write: bool = True) -> dict:
        exam = self._get_exam_or_404(exam_id)
        ExamService._assert_can_access(exam, current_user, write=write)
        return exam

    def get_exam_variant_pair(
        self,
        exam_id: str,
        variant_id: str,
        current_user: CurrentUser | None,
        *,
        write: bool = False,
    ) -> tuple[dict, dict]:
        exam = self._get_exam_for_user_or_404(exam_id, current_user, write=write)
        variant = self.variants.find(variant_id)
        if not variant or str(variant["exam_id"]) != exam_id:
            raise LookupError("Không tìm thấy mã đề")
        return exam, variant

    def create_variant(self, exam_id: str, payload: ExamVariantCreateRequest, current_user: CurrentUser) -> dict:
        exam = self._get_exam_for_user_or_404(exam_id, current_user)
        if _exam_status(exam) == "PUBLISHED":
            raise ValueError("Đề thi đã phát hành, không thể thêm mã đề")
        if _exam_status(exam) != "FINALIZED":
            raise ValueError("Chỉ có thể tạo mã đề sau khi chốt đề thi")
        questions = (exam.get("finalized_snapshot") or {}).get("questions") or exam.get("questions", [])
        if not questions:
            raise ValueError("Đề thi chưa có câu hỏi, không thể tạo mã đề")
        if len(questions) != int(exam["question_count"]):
            raise ValueError("Đề thi chưa đủ số câu, không thể tạo mã đề")
        if self.exams.count_variants(exam_id) >= MAX_VARIANTS_PER_EXAM:
            raise ValueError(f"Đã đạt tối đa {MAX_VARIANTS_PER_EXAM} mã đề cho kỳ thi này")

        shuffle_seed = (_new_seed() if payload.seed is None else payload.seed) if payload.shuffle else None
        rng = random.Random(shuffle_seed)
        ordered = list(questions)
        if payload.shuffle:
            rng.shuffle(ordered)

        variant_questions = []
        answer_key: dict[str, Any] = {}
        for index, ref in enumerate(ordered, start=1):
            snapshot = ref["content_snapshot"]
            question_data = snapshot.get("question_data") or {}
            options = question_data.get("options")
            option_order = None
            correct_answer = question_data.get("correct_answer")
            if payload.shuffle and isinstance(options, dict) and set(options.keys()) <= {"A", "B", "C", "D", "E"}:
                keys = list(options.keys())
                shuffled_keys = keys[:]
                rng.shuffle(shuffled_keys)
                key_map = dict(zip(keys, shuffled_keys))
                shuffled_options = {key_map[k]: v for k, v in options.items()}
                option_order = [ord(key_map[k]) - ord("A") for k in keys]
                if isinstance(correct_answer, str):
                    correct_keys = [c.strip() for c in correct_answer.split(",") if c.strip()]
                    correct_answer = ",".join(key_map.get(k, k) for k in correct_keys)
                question_data = {**question_data, "options": shuffled_options, "correct_answer": correct_answer}
                snapshot = {**snapshot, "question_data": question_data}
            variant_questions.append(
                {
                    "order": index,
                    "question_id": ref["question_id"],
                    "content_snapshot": snapshot,
                    "option_order": option_order,
                }
            )
            answer_key[str(ref["question_id"])] = correct_answer

        variant = {
            "_id": ObjectId(),
            "schema_version": SCHEMA_VERSION,
            "exam_id": object_id(exam_id, "exam_id"),
            "exam_code": payload.exam_code,
            "questions": variant_questions,
            "answer_key": answer_key,
            "shuffle_seed": shuffle_seed,
            "created_at": utc_now(),
        }
        self.variants.create(variant)
        return serialize_variant(variant)

    @staticmethod
    def _variant_for(exam: dict, variant: dict, current_user: CurrentUser | None) -> dict:
        if ExamService._can_edit(exam, current_user):
            return serialize_variant(variant)
        return serialize_variant(
            {**variant, "questions": _questions_without_answers(variant.get("questions") or []), "answer_key": {}}
        )

    def list_variants(self, exam_id: str, current_user: CurrentUser) -> list[dict]:
        exam = self._get_exam_for_user_or_404(exam_id, current_user, write=False)
        return [self._variant_for(exam, v, current_user) for v in self.variants.list_by_exam(exam_id)]

    def get_variant(self, exam_id: str, variant_id: str, current_user: CurrentUser) -> dict:
        exam, variant = self.get_exam_variant_pair(exam_id, variant_id, current_user)
        return self._variant_for(exam, variant, current_user)

    def delete_variant(self, exam_id: str, variant_id: str, current_user: CurrentUser) -> None:
        exam, variant = self.get_exam_variant_pair(exam_id, variant_id, current_user, write=True)
        if _exam_status(exam) in {"PUBLISHED", "ARCHIVED"}:
            raise ValueError("Đề thi đã phát hành hoặc lưu trữ, không thể xoá mã đề")
        if exam.get("official_exported_at"):
            raise ValueError("Đề thi đã xuất bản chính thức, không thể xoá mã đề")
        audit = _exam_audit("exam.variant_deleted", exam, current_user,
                            before={"variant_id": str(variant["_id"]), "exam_code": variant.get("exam_code")})
        if not self.variants.delete(variant_id, audit=audit):
            raise ValueError("Đề thi đã xuất bản chính thức hoặc mã đề không còn tồn tại")

    def export_access(
        self, exam_id: str, variant_id: str, export_type: str, current_user: CurrentUser,
    ) -> tuple[dict, dict, bool]:
        """Return (exam, variant, official). Only the exam owner gets an official export with answers;
        everyone else with read access gets a watermarked question-only preview."""
        exam, variant = self.get_exam_variant_pair(exam_id, variant_id, current_user)
        if str(exam.get("created_by_user_id")) == str(current_user.id):
            return exam, variant, True
        if export_type != "de":
            raise PermissionError("Chỉ người tạo đề thi được xuất đáp án")
        return exam, {**variant, "questions": _questions_without_answers(variant.get("questions") or [])}, False

    def record_official_export(
        self, exam: dict, variant: dict, *, export_type: str, file_format: str, current_user: CurrentUser,
    ) -> None:
        now = utc_now()
        updates = {"last_official_exported_at": now}
        if not exam.get("official_exported_at"):
            updates["official_exported_at"] = now
            updates["official_exported_by_user_id"] = object_id(current_user.id, "user_id")
        audit = _exam_audit(
            "exam.official_exported", exam, current_user,
            after={"variant_id": str(variant["_id"]), "exam_code": variant.get("exam_code")},
            metadata={"export_type": export_type, "format": file_format},
        )
        if not self.exams.update(str(exam["_id"]), updates, audit=audit):
            raise LookupError("Không tìm thấy đề thi")

    def build_preview(self, exam_id: str, variant_id: str, current_user: CurrentUser) -> dict:
        exam, variant = self.get_exam_variant_pair(exam_id, variant_id, current_user)
        questions = []
        for entry in sorted(variant["questions"], key=lambda item: item["order"]):
            snapshot = entry["content_snapshot"]
            question_data = snapshot.get("question_data") or {}
            options = question_data.get("options") or {}
            questions.append(
                {
                    "number": entry["order"],
                    "content": snapshot.get("content", ""),
                    "question_type": (snapshot.get("classification") or {}).get(
                        "assessment_type", ""
                    ),
                    "options": [
                        {"label": key, "text": value} for key, value in options.items()
                    ],
                }
            )
        return json_safe(
            {
                "header": exam["header"],
                "exam_code": variant["exam_code"],
                "questions": questions,
            }
        )


def _question_repository(database):
    if settings.question_store == "postgres":
        from modules.questions.postgres_repository import PostgresQuestionRepository
        return PostgresQuestionRepository()
    return MongoQuestionRepository(database)


def _exam_repositories(database) -> tuple[ExamRepository, ExamVariantRepository]:
    if settings.exam_store == "postgres":
        from modules.exams.postgres_repository import (
            PostgresExamRepository,
            PostgresExamVariantRepository,
        )
        return PostgresExamRepository(), PostgresExamVariantRepository()
    return MongoExamRepository(database), MongoExamVariantRepository(database)


def get_exam_service() -> ExamService:
    database = get_database()
    exams, _variants = _exam_repositories(database)
    return ExamService(exams, _question_repository(database))


def get_exam_variant_service() -> ExamVariantService:
    return ExamVariantService(*_exam_repositories(get_database()))
