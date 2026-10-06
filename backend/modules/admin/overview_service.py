from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any

from pymongo.database import Database

from core.config import settings
from modules.admin.audit_service import AdminAuditService
from modules.admin.jobs_service import ACTIVE_STATUSES, RETRYABLE_STATUSES, AdminJobService, json_safe
from modules.admin.moodle_service import MoodleTargetService
from modules.catalog.postgres_subject_repository import subject_records
from modules.users.store import users_by_ids
from modules.documents.store import get_document_repository


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


MODEL_WINDOW_DAYS = 30
MODEL_SUCCESS_STATUSES = {"COMPLETED", "PASSED", "SUCCESS", "SUCCEEDED"}
MODEL_FAILED_STATUSES = {"FAILED", "ERROR", "STALE", "FAILED_VALIDATION", "BLOCKED"}
MODEL_ACTIVE_STATUSES = {"QUEUED", "PROCESSING", "RUNNING", "GENERATING"}
PROMPT_TOKEN_PATHS = [
    "usage.prompt_tokens",
    "usage.input_tokens",
    "token_usage.prompt_tokens",
    "token_usage.input_tokens",
    "execution.prompt_tokens",
    "execution.input_tokens",
    "execution.usage.prompt_tokens",
    "execution.usage.input_tokens",
    "metrics.prompt_tokens",
    "metrics.input_tokens",
]
COMPLETION_TOKEN_PATHS = [
    "usage.completion_tokens",
    "usage.output_tokens",
    "token_usage.completion_tokens",
    "token_usage.output_tokens",
    "execution.completion_tokens",
    "execution.output_tokens",
    "execution.usage.completion_tokens",
    "execution.usage.output_tokens",
    "metrics.completion_tokens",
    "metrics.output_tokens",
]
TOTAL_TOKEN_PATHS = [
    "usage.total_tokens",
    "token_usage.total_tokens",
    "execution.total_tokens",
    "execution.usage.total_tokens",
    "metrics.total_tokens",
    "total_tokens",
]
COST_USD_PATHS = [
    "cost_usd",
    "usage.cost_usd",
    "token_usage.cost_usd",
    "execution.cost_usd",
    "execution.usage.cost_usd",
    "billing.cost_usd",
    "metrics.cost_usd",
]
INPUT_PRICE_PER_1K_PATHS = [
    "model.config.input_cost_per_1k",
    "model.config.input_price_per_1k",
    "model.config.prompt_cost_per_1k",
    "evaluator_model.config.input_cost_per_1k",
    "evaluator_model.config.input_price_per_1k",
    "evaluator_model.config.prompt_cost_per_1k",
]
OUTPUT_PRICE_PER_1K_PATHS = [
    "model.config.output_cost_per_1k",
    "model.config.output_price_per_1k",
    "model.config.completion_cost_per_1k",
    "evaluator_model.config.output_cost_per_1k",
    "evaluator_model.config.output_price_per_1k",
    "evaluator_model.config.completion_cost_per_1k",
]
TOTAL_PRICE_PER_1K_PATHS = [
    "model.config.cost_per_1k_tokens",
    "model.config.price_per_1k_tokens",
    "evaluator_model.config.cost_per_1k_tokens",
    "evaluator_model.config.price_per_1k_tokens",
]


def _as_aware_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _path_value(record: dict, path: str):
    current = record
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return None
        return parsed if isfinite(parsed) else None
    return None


def _first_number(record: dict, paths: list[str]) -> float | None:
    for path in paths:
        value = _number(_path_value(record, path))
        if value is not None:
            return value
    return None


def _token_usage(record: dict) -> dict:
    prompt_tokens = int(max(0, _first_number(record, PROMPT_TOKEN_PATHS) or 0))
    completion_tokens = int(max(0, _first_number(record, COMPLETION_TOKEN_PATHS) or 0))
    total_tokens = int(max(0, _first_number(record, TOTAL_TOKEN_PATHS) or 0))
    if not total_tokens:
        total_tokens = prompt_tokens + completion_tokens
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _cost_usd(record: dict, usage: dict) -> float | None:
    direct_cost = _first_number(record, COST_USD_PATHS)
    if direct_cost is not None:
        return max(0.0, direct_cost)
    input_price = _first_number(record, INPUT_PRICE_PER_1K_PATHS)
    output_price = _first_number(record, OUTPUT_PRICE_PER_1K_PATHS)
    total_price = _first_number(record, TOTAL_PRICE_PER_1K_PATHS)
    if input_price is not None or output_price is not None:
        return (
            usage["prompt_tokens"] * max(0.0, input_price or 0.0)
            + usage["completion_tokens"] * max(0.0, output_price or 0.0)
        ) / 1000
    if total_price is not None:
        return usage["total_tokens"] * max(0.0, total_price) / 1000
    return None


def _duration_ms(record: dict, *, duration_path: str, start_key: str = "started_at", end_key: str = "finished_at") -> int | None:
    duration = _path_value(record, duration_path)
    if isinstance(duration, (int, float)):
        return max(0, int(duration))
    start = _as_aware_utc(record.get(start_key))
    end = _as_aware_utc(record.get(end_key))
    if start and end and end >= start:
        return int((end - start).total_seconds() * 1000)
    return None


class AdminOverviewService:
    def __init__(self, database: Database):
        self.db = database

    def list_documents(self, page: int, page_size: int, status: str | None, search: str | None) -> dict:
        records, total = get_document_repository(self.db).list(page, page_size, status, search)
        owner_ids = list({row["uploaded_by_user_id"] for row in records if row.get("uploaded_by_user_id")})
        owners = {str(row["_id"]): row for row in users_by_ids(self.db, owner_ids)}
        subject_ids = list({subject_id for row in records for subject_id in
                            (row.get("subject_ids") or ([row["subject_id"]] if row.get("subject_id") else []))})
        subjects = {str(row["_id"]): row for row in subject_records(self.db, ids=subject_ids)} if subject_ids else {}
        items = []
        for row in records:
            owner_id = row.get("uploaded_by_user_id")
            owner = owners.get(str(owner_id), {})
            item_subjects = row.get("subject_ids") or ([row["subject_id"]] if row.get("subject_id") else [])
            error = row.get("latest_error") or {}
            items.append({
                "id": row["_id"], "title": row.get("title"),
                "original_filename": row.get("original_filename"), "status": row.get("status"),
                "page_count": row.get("page_count"), "current_version": row.get("current_version"),
                "owner": {"id": owner_id, "display_name": owner.get("display_name"), "email": owner.get("email")},
                "subjects": [{"id": item, "name": subjects.get(str(item), {}).get("subject_name"),
                              "code": subjects.get(str(item), {}).get("subject_code")} for item in item_subjects],
                "pipeline_summary": row.get("pipeline_summary") or {},
                "error_message": error.get("message") or error.get("detail"),
                "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
            })
        return json_safe({"items": items, "total": total, "page": page, "page_size": page_size})

    def overview(self) -> dict:
        job_service = AdminJobService(self.db)
        job_page = job_service.list_jobs(page=1, page_size=500)
        retryable_jobs = job_service.list_jobs(page=1, page_size=5, status="retryable")
        moodle_summary = MoodleTargetService(self.db)._publication_summary(None)
        audit_page = AdminAuditService(self.db).list(page=1, page_size=5)
        question_summary = self._question_summary()
        pending_review = question_summary["pending"]
        failed_documents = self._document_count({"status": "FAILED"})
        retryable_job_count = job_page["summary"].get("failed", 0)
        long_running_job_count = job_page["summary"].get("long_running", 0)
        model_report = self._model_usage_report()

        attention = [
            {
                "key": "pending_review",
                "label": "Câu hỏi chờ duyệt",
                "count": pending_review,
                "severity": "warning" if pending_review else "neutral",
                "path": "/kiem-duyet?status=PENDING",
            },
            {
                "key": "retryable_jobs",
                "label": "Tác vụ cần xử lý",
                "count": retryable_job_count,
                "severity": "danger" if retryable_job_count else "neutral",
                "path": "/quan-ly-job?status=retryable",
            },
            {
                "key": "long_running_jobs",
                "label": "Tác vụ quá ngưỡng",
                "count": long_running_job_count,
                "severity": "warning" if long_running_job_count else "neutral",
                "path": "/quan-ly-job?stale_only=true",
            },
            {
                "key": "failed_documents",
                "label": "Tài liệu lỗi",
                "count": failed_documents,
                "severity": "danger" if failed_documents else "neutral",
                "path": "/quan-ly-job?kind=document&status=retryable",
            },
        ]


        subjects_map = {
            str(s['_id']): s.get('subject_name') or s.get('subject_code')
            for s in subject_records(self.db)
        }
        from core.config import settings
        if settings.ai_config_store == "postgres":
            from modules.catalog.postgres_ai_repository import PostgresAiRepository
            model_records = PostgresAiRepository().models()
        else:
            model_records = self.db.ai_models.find({}, {'model_name': 1, 'model_code': 1})
        models_map = {
            str(m.get('model_code')): m.get('model_name') or m.get('model_code')
            for m in model_records
        }

        user_entity_ids = []
        for audit in audit_page['items']:
            entity = audit.get('entity')
            if isinstance(entity, dict) and str(entity.get('type')).lower() == 'user' and entity.get('id'):
                try:
                    from bson import ObjectId
                    user_entity_ids.append(ObjectId(entity['id']))
                except:
                    pass
        
        users_map = {}
        if user_entity_ids:
            users_map = {
                str(u['_id']): u.get('display_name') or str(u['_id'])
                for u in users_by_ids(self.db, user_entity_ids)
            }

        for job in retryable_jobs['items']:
            entity = job.get('entity')
            if isinstance(entity, dict) and entity.get('type') == 'subject' and entity.get('id'):
                subj_id = str(entity['id'])
                if subj_id in subjects_map:
                    entity['label'] = subjects_map[subj_id]

        for audit in audit_page['items']:
            entity = audit.get('entity')
            if isinstance(entity, dict) and entity.get('id'):
                etype = str(entity.get('type')).lower()
                eid = str(entity['id'])
                if etype == 'subject' and eid in subjects_map:
                    entity['label'] = subjects_map[eid]
                elif etype == 'user' and eid in users_map:
                    entity['label'] = users_map[eid]

        for row in model_report['rows']:
            mc = str(row.get('model_code', ''))
            if mc in models_map:
                row['model_name'] = models_map[mc]

        return json_safe(
            {
                "generated_at": utc_now(),
                "users": self._user_summary(),
                "questions": question_summary,
                "documents": {
                    "total": self._document_count({}),
                    "uploaded": self._document_count({"status": "UPLOADED"}),
                    "processing": self._document_count({"status": "PROCESSING"}),
                    "ready": self._document_count({"status": {"$in": ["READY", "INDEXED", "COMPLETED"]}}),
                    "failed": failed_documents,
                },
                "jobs": {
                    **job_page["summary"],
                    "breakdown": self._job_breakdown(job_page["items"]),
                },
                "moodle": {
                    "targets": self._moodle_target_count(),
                    "active_targets": self._moodle_target_count(active_only=True),
                    "publications": moodle_summary,
                },
                "model_performance": model_report["rows"],
                "model_usage_summary": model_report["summary"],
                "attention": attention,
                "recent_jobs": retryable_jobs["items"],
                "recent_audit": audit_page["items"],
            }
        )

    def _count(self, collection_name: str, query: dict) -> int:
        return getattr(self.db, collection_name).count_documents(query)

    def _moodle_target_count(self, *, active_only: bool = False) -> int:
        if settings.moodle_target_store == "postgres":
            from modules.admin.postgres_moodle_target_repository import PostgresMoodleTargetRepository
            return PostgresMoodleTargetRepository().count(active_only=active_only)
        return self._count("moodle_targets", {"is_active": True} if active_only else {})

    def _user_summary(self) -> dict:
        if settings.user_store == "postgres":
            from modules.users.postgres_repository import PostgresUserRepository
            return PostgresUserRepository().role_summary()
        return {
            "total": self._count("users", {}),
            "active": self._count("users", {"is_active": True}),
            "admins": self._count("users", {"role": "Admin", "is_active": True}),
            "teachers": self._count("users", {"role": "Teacher", "is_active": True}),
            "reviewers": self._count("users", {"role": "Reviewer", "is_active": True}),
            "pending": self._count("users", {"approval_status": "PENDING"}),
        }

    def _question_summary(self) -> dict:
        if settings.question_store == "postgres":
            from modules.questions.postgres_repository import PostgresQuestionRepository
            row = PostgresQuestionRepository.status_summary()
            quality = {key: row.pop(key) for key in ("green", "yellow", "red", "not_evaluated")}
            return {**row, "quality": quality}
        return {
            "total": self._question_count({}),
            "draft": self._question_count({"review_status": "DRAFT"}),
            "pending": self._question_count({"review_status": "PENDING"}),
            "approved": self._question_count({"review_status": "APPROVED"}),
            "needs_revision": self._question_count({"review_status": "NEEDS_REVISION"}),
            "rejected": self._question_count({"review_status": "REJECTED"}),
            "published": self._question_count({"publication_status": "PUBLISHED"}),
            "quality": self._quality_summary(),
        }

    def _question_count(self, query: dict) -> int:
        return self._count(
            "questions",
            {"lifecycle_status": "ACTIVE", **query},
        )

    def _document_count(self, query: dict) -> int:
        if settings.document_store == "postgres":
            from modules.documents.postgres_repository import PostgresDocumentRepository
            status = query.get("status")
            statuses = (status.get("$in") if isinstance(status, dict)
                        else [status] if status else None)
            return PostgresDocumentRepository().count_by_status(statuses)
        return self._count(
            "documents",
            {"archived_at": None, **query},
        )

    def _quality_summary(self) -> dict:
        return {
            "green": self._question_count({"quality_summary.color": "GREEN"}),
            "yellow": self._question_count({"quality_summary.color": "YELLOW"}),
            "red": self._question_count({"quality_summary.color": "RED"}),
            "not_evaluated": self._question_count(
                {
                    "$or": [
                        {"quality_summary.color": {"$exists": False}},
                        {"quality_summary.color": None},
                    ]
                }
            ),
        }

    @staticmethod
    def _job_group(job: dict) -> tuple[str, str]:
        if job.get("kind") == "generation":
            return "generation", "Sinh câu hỏi"
        if job.get("kind") == "evaluation":
            return "evaluation", "Đánh giá AI"
        if job.get("kind") == "document":
            job_type = str(job.get("type") or "").upper()
            if "OCR" in job_type:
                return "ocr", "OCR"
            if "CHUNK" in job_type or "INDEX" in job_type:
                return "chunk", "Chunk/Index"
            return "document", "Tài liệu"
        return "other", "Khác"

    def _job_breakdown(self, jobs: list[dict]) -> list[dict]:
        order = ["generation", "evaluation", "ocr", "chunk", "document", "other"]
        groups = {
            key: {
                "key": key,
                "label": label,
                "total": 0,
                "active": 0,
                "failed": 0,
                "long_running": 0,
            }
            for key, label in [
                ("generation", "Sinh câu hỏi"),
                ("evaluation", "Đánh giá AI"),
                ("ocr", "OCR"),
                ("chunk", "Chunk/Index"),
                ("document", "Tài liệu"),
                ("other", "Khác"),
            ]
        }
        for job in jobs:
            key, label = self._job_group(job)
            group = groups.setdefault(
                key,
                {"key": key, "label": label, "total": 0, "active": 0, "failed": 0, "long_running": 0},
            )
            status = job.get("status")
            group["total"] += 1
            if status in ACTIVE_STATUSES:
                group["active"] += 1
            if status in RETRYABLE_STATUSES:
                group["failed"] += 1
            if job.get("is_long_running"):
                group["long_running"] += 1
        return [groups[key] for key in order if groups[key]["total"] > 0]

    def _model_performance(self) -> list[dict]:
        return self._model_usage_report()["rows"]

    def _model_usage_report(self) -> dict:
        since = utc_now() - timedelta(days=MODEL_WINDOW_DAYS)
        groups: dict[str, dict] = {}
        self._collect_evaluation_model_performance(groups, since)
        self._collect_generation_model_performance(groups, since)

        rows = []
        summary_latencies = []
        summary = {
            "window_days": MODEL_WINDOW_DAYS,
            "total_requests": 0,
            "completed": 0,
            "failed": 0,
            "active": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "avg_latency_ms": None,
            "error_rate": None,
        }
        for group in groups.values():
            latencies = group.pop("_latencies")
            summary_latencies.extend(latencies)
            total = group["total"]
            failed = group["failed"]
            group["error_rate"] = round(failed / total, 3) if total else None
            group["avg_latency_ms"] = round(sum(latencies) / len(latencies)) if latencies else None
            group["cost_usd"] = round(group["cost_usd"], 6)
            for key in ["total", "completed", "failed", "active", "prompt_tokens", "completion_tokens", "total_tokens"]:
                summary_key = "total_requests" if key == "total" else key
                summary[summary_key] += group[key]
            summary["cost_usd"] += group["cost_usd"]
            rows.append(group)
        summary["cost_usd"] = round(summary["cost_usd"], 6)
        if summary["total_requests"]:
            summary["error_rate"] = round(summary["failed"] / summary["total_requests"], 3)
        if summary_latencies:
            summary["avg_latency_ms"] = round(sum(summary_latencies) / len(summary_latencies))
        rows.sort(key=lambda item: (item["failed"], item["total"], item["model_code"]), reverse=True)
        return {"summary": summary, "rows": rows[:8]}

    def _model_group(self, groups: dict[str, dict], *, kind: str, kind_label: str, model_code: str) -> dict:
        key = f"{kind}:{model_code}"
        return groups.setdefault(
            key,
            {
                "key": key,
                "kind": kind,
                "kind_label": kind_label,
                "model_code": model_code,
                "total": 0,
                "completed": 0,
                "failed": 0,
                "active": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "_latencies": [],
            },
        )

    def _collect_evaluation_model_performance(self, groups: dict[str, dict], since: datetime) -> None:
        if settings.question_store == "postgres":
            from modules.questions.postgres_evaluation_jobs import PostgresEvaluationJobs
            jobs = PostgresEvaluationJobs().recent_jobs(since, 1000)
        else:
            collection = getattr(self.db, "evaluation_jobs", None)
            if collection is None:
                return
            query = {
                "$or": [
                    {"updated_at": {"$gte": since}},
                    {"finished_at": {"$gte": since}},
                    {"queued_at": {"$gte": since}},
                ]
            }
            jobs = collection.find(query).sort("updated_at", -1).limit(1000)
        for job in jobs:
            model_code = job.get("evaluator_model_code") or "unknown"
            group = self._model_group(
                groups,
                kind="evaluation",
                kind_label="Đánh giá",
                model_code=str(model_code),
            )
            self._add_model_job(group, job, _duration_ms(job, duration_path="duration_ms"))

    def _collect_generation_model_performance(self, groups: dict[str, dict], since: datetime) -> None:
        if settings.generation_store == "postgres":
            from modules.generation.postgres_store import PostgresGenerationStore
            runs = PostgresGenerationStore().recent_runs(since, 1000)
        else:
            collection = getattr(self.db, "generation_runs", None)
            if collection is None:
                return
            query = {
                "$or": [
                    {"updated_at": {"$gte": since}},
                    {"finished_at": {"$gte": since}},
                    {"created_at": {"$gte": since}},
                ]
            }
            runs = collection.find(query).sort("finished_at", -1).limit(1000)
        for run in runs:
            model = run.get("model") or {}
            model_code = (
                model.get("model_code")
                or model.get("provider")
                or model.get("model_name")
                or "unknown"
            )
            group = self._model_group(
                groups,
                kind="generation",
                kind_label="Sinh câu hỏi",
                model_code=str(model_code),
            )
            self._add_model_job(group, run, _duration_ms(run, duration_path="execution.latency_ms"))

    @staticmethod
    def _add_model_job(group: dict, record: dict, duration_ms: int | None) -> None:
        status = str(record.get("status") or "").upper()
        usage = _token_usage(record)
        cost_usd = _cost_usd(record, usage)
        group["total"] += 1
        if status in MODEL_SUCCESS_STATUSES:
            group["completed"] += 1
        elif status in MODEL_FAILED_STATUSES:
            group["failed"] += 1
        elif status in MODEL_ACTIVE_STATUSES:
            group["active"] += 1
        group["prompt_tokens"] += usage["prompt_tokens"]
        group["completion_tokens"] += usage["completion_tokens"]
        group["total_tokens"] += usage["total_tokens"]
        if cost_usd is not None:
            group["cost_usd"] += cost_usd
        if duration_ms is not None:
            group["_latencies"].append(duration_ms)
