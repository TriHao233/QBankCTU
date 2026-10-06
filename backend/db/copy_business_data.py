"""Copy MongoDB business records into PostgreSQL for shadow verification.

This command never deletes or updates MongoDB. It does not switch the API's
source of truth. Run without --apply to inspect counts first; --apply performs
idempotent upserts into an already migrated PostgreSQL database.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

from bson import Binary, Decimal128, ObjectId
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pymongo import MongoClient

from core.config import settings

VECTOR_COLLECTIONS = {
    "chunk_sets", "document_chunks", "vector_collections",
    "chunk_embeddings",
}
SOURCE_ORDER = (
    "users", "subjects", "keywords", "dictionaries", "ai_models", "prompt_templates",
    "evaluation_policies", "documents", "document_jobs", "document_pages",
    "pipeline_lineage_events",
    "generation_jobs", "generation_runs", "questions", "question_versions",
    "evaluation_jobs", "question_evaluations", "question_reviews",
    "question_review_drafts", "question_comments", "exams", "exam_variants",
    "llm_slots", "notifications", "audit_logs", "review_settings", "moodle_targets",
    "moodle_publications",
)
JSON_COLUMNS = {
    "permissions", "permission_grants", "permission_revokes",
    "review_subject_ids", "teaching_subject_ids", "profile", "generation_presets", "task_calendar",
    "payload",
    "capabilities", "parameters", "weights", "thresholds", "assignment",
    "question_data", "classification", "clos", "sources", "request", "changes",
    "model_snapshot", "prompt_snapshot", "retrieval_snapshot", "result",
    "metrics", "policy_snapshot", "source_snapshot", "error", "draft",
    "snapshot", "from_snapshot", "to_snapshot", "validation",
    "before_state", "after_state", "metadata", "response_payload",
    "request_payload", "last_health_check", "source_location",
}


def normalized(value):
    """Convert BSON values to portable JSON without exposing credentials."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (Binary, bytes)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): normalized(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(item) for item in value]
    return value


def oid(value):
    return str(value) if value is not None else None


def timestamp(doc, *keys):
    for key in keys:
        value = doc.get(key)
        if isinstance(value, datetime):
            return value
    return datetime.now(timezone.utc)


def fingerprint(value) -> str:
    encoded = json.dumps(normalized(value), sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def projected_rows(name: str, item: dict):
    """Yield a parent row followed by any embedded child rows."""
    data = normalized(item)
    row_id = oid(item["_id"])
    created = timestamp(item, "created_at", "queued_at", "reviewed_at")
    updated = timestamp(item, "updated_at", "finished_at", "started_at", "created_at", "queued_at")
    if name == "users":
        yield "users", dict(
            id=row_id, firebase_uid=item["firebase_uid"], email=item["email"].lower(),
            display_name=item.get("display_name") or item["email"],
            role=item.get("role") or "Teacher", permissions=data.get("permissions") or [],
            permission_grants=data.get("permission_grants") or [],
            permission_revokes=data.get("permission_revokes") or [],
            review_subject_ids=data.get("review_subject_ids") or [],
            teaching_subject_ids=data.get("teaching_subject_ids") or [],
            profile=data.get("profile") or {},
            generation_presets=data.get("generation_presets") or [],
            task_calendar=data.get("task_calendar") or [],
            is_active=item.get("is_active", True), created_at=created, updated_at=updated,
        )
        return
    if name == "subjects":
        yield "subjects", dict(
            id=row_id, subject_code=item["subject_code"],
            subject_name=item["subject_name"], owner_id=oid(item.get("owner_id")),
            is_active=item.get("is_active", True), payload=data,
            created_at=created, updated_at=updated,
        )
        for chapter in item.get("chapters") or []:
            child = normalized(chapter)
            child_id = oid(chapter.get("_id")) or f"{row_id}:chapter:{chapter['chapter_code']}"
            yield "subject_chapters", dict(
                id=child_id, subject_id=row_id, chapter_code=chapter["chapter_code"],
                chapter_name=chapter.get("chapter_name") or chapter["chapter_code"],
                sequence_no=chapter.get("sequence_no", 1),
                is_active=chapter.get("is_active", True), payload=child,
            )
        for clo in item.get("learning_outcomes") or []:
            child = normalized(clo)
            child_id = oid(clo.get("_id")) or f"{row_id}:clo:{clo['clo_code']}"
            yield "learning_outcomes", dict(
                id=child_id, subject_id=row_id, clo_code=clo["clo_code"],
                description=clo.get("description") or clo["clo_code"],
                target_weight=clo.get("target_weight", 1),
                is_active=clo.get("is_active", True), payload=child,
            )
        return
    if name == "keywords":
        yield "keywords", dict(id=row_id, subject_id=oid(item.get("subject_id")),
                               keyword=item.get("keyword") or "", status=item.get("status") or "ACTIVE",
                               payload=data)
        return
    if name == "dictionaries":
        yield "legacy_dictionaries", dict(
            id=row_id, course_id=item["course_id"], name=item.get("name"),
            category=item.get("category"), is_active=item.get("is_active", True),
            payload=data, created_at=created, updated_at=updated,
        )
        for field, status in (
            ("core_keywords", "CORE"),
            ("learned_keywords", "LEARNED"),
            ("pending_keywords", "PENDING"),
        ):
            for keyword in item.get(field) or []:
                clean = str(keyword).strip()
                if not clean:
                    continue
                key = hashlib.sha256(f"{row_id}:{status}:{clean.lower()}".encode()).hexdigest()[:24]
                yield "keywords", dict(
                    id=key, subject_id=None, keyword=clean, status=status,
                    payload={"course_id": item["course_id"], "dictionary_id": row_id},
                )
        return
    if name == "ai_models":
        version_id = f"{row_id}:v1"
        yield "ai_models", dict(
            id=row_id, model_code=item["model_code"],
            display_name=item.get("display_name") or item.get("model_name") or item["model_code"],
            runtime=item.get("runtime") or "OLLAMA", capabilities=data.get("capabilities") or [],
            priority=item.get("priority", 10), is_active=item.get("is_active", True),
            active_version_id=version_id, created_at=created, updated_at=updated,
            description=item.get("description") or "",
            kind=item.get("kind") or "CHAT",
            is_local=item.get("is_local", (item.get("runtime") or "OLLAMA") == "OLLAMA"),
            last_health_check=data.get("last_health_check"),
        )
        config = data.get("config") or {}
        yield "ai_model_versions", dict(
            id=version_id, model_id=row_id, version=1,
            model_name=item.get("model_name") or item["model_code"],
            revision=item.get("revision"), parameters=config,
            endpoint_alias=config.get("endpoint"), secret_ref=None,
            config_hash=fingerprint({"model_name": item.get("model_name"), "config": config}),
            created_by_user_id=None, created_at=created,
        )
        return
    if name == "prompt_templates":
        yield "prompt_templates", dict(
            id=row_id, template_key=item["template_key"], version=item["version"],
            kind=item.get("kind") or "GENERATION", name=item.get("name") or item["template_key"],
            prompt_body=item["prompt_body"],
            content_hash=item.get("content_hash") or fingerprint(item["prompt_body"]),
            is_active=item.get("is_active", False), created_by_user_id=None,
            created_at=created,
        )
        return
    if name == "evaluation_policies":
        weights = data.get("weights") or {}
        yield "evaluation_policies", dict(
            id=row_id, policy_name=item["policy_name"], version=item["version"],
            weights=weights, thresholds=data.get("thresholds") or {},
            weights_hash=item.get("weights_hash") or fingerprint(weights),
            is_active=item.get("is_active", False), created_by_user_id=None,
            created_at=created,
        )
        return
    if name == "documents":
        current = item.get("current_processing") or {}
        yield "documents", dict(
            id=row_id, title=item.get("title") or item.get("original_filename") or row_id,
            original_filename=item.get("original_filename") or row_id,
            subject_id=oid(item.get("subject_id")),
            uploaded_by_user_id=oid(item.get("uploaded_by_user_id")),
            status=item.get("status") or "UPLOADED",
            current_version=item.get("current_version", 1),
            active_chunk_set_id=oid(current.get("chunk_set_id")),
            payload=data, created_at=created, updated_at=updated,
        )
        subject_ids = list(dict.fromkeys(
            ([oid(item["subject_id"])] if item.get("subject_id") else [])
            + [oid(value) for value in item.get("subject_ids") or []]
        ))
        for position, subject_id in enumerate(subject_ids):
            yield "document_subjects", dict(
                document_id=row_id, subject_id=subject_id, position_no=position,
            )
        for index, artifact in enumerate(item.get("artifacts") or []):
            storage = artifact.get("storage") or {}
            yield "document_artifacts", dict(
                id=oid(artifact.get("_id")) or f"{row_id}:artifact:{index}",
                document_id=row_id, artifact_type=artifact.get("type") or "UNKNOWN",
                storage_provider=storage.get("provider") or "LOCAL",
                storage_key=storage.get("uri") or "",
                checksum_sha256=artifact.get("sha256"), mime_type=artifact.get("mime_type"),
                size_bytes=artifact.get("size_bytes"),
                version=artifact.get("document_version") or 1,
                payload=normalized(artifact), created_at=timestamp(artifact, "created_at"),
            )
        return
    if name == "document_jobs":
        yield "document_jobs", dict(
            id=row_id, document_id=oid(item["document_id"]),
            job_type=item.get("job_type") or "UNKNOWN", status=item["status"],
            attempt_no=item.get("attempt_no", 0), lease_owner=item.get("lease_owner"),
            lease_expires_at=item.get("lease_expires_at"), payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "document_pages":
        yield "document_pages", dict(
            id=row_id, document_id=oid(item["document_id"]),
            ocr_job_id=oid(item.get("ocr_job_id")), page_number=item.get("page_number"),
            unit_number=item.get("unit_number") or item.get("page_number"),
            source_location=data.get("source_location") or {},
            raw_text=item.get("raw_text"), clean_text=item.get("cleaned_text") or item.get("clean_text"),
            version=item.get("document_version", 1), payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "pipeline_lineage_events":
        yield "document_lineage_events", dict(
            operation_id=item.get("operation_id") or row_id,
            document_id=oid(item["document_id"]),
            event_type=item.get("event_type") or "UNKNOWN",
            actor=str(item.get("actor") or "SYSTEM"),
            reason=str(item.get("reason") or ""),
            from_snapshot=data.get("from_snapshot") or {},
            to_snapshot=data.get("to_snapshot") or {},
            validation=data.get("validation") or {},
            rollback_available=bool(item.get("rollback_available", False)),
            promotion_operation_id=item.get("promotion_operation_id"),
            status=item.get("status"), payload=data, created_at=created,
        )
        return
    if name == "generation_jobs":
        yield "generation_jobs", dict(
            id=row_id, requested_by_user_id=oid(item.get("requested_by_user_id")),
            idempotency_key=item.get("idempotency_key"), status=item["status"],
            request=data.get("request") or {}, model_snapshot=data.get("model_snapshot") or {},
            result=data.get("result"), metrics=data.get("metrics"),
            attempt_no=item.get("attempt_count", 0),
            lease_owner=item.get("locked_by") or item.get("worker_id"),
            lease_expires_at=item.get("lease_expires_at"),
            next_attempt_at=item.get("next_attempt_at"),
            error_message=item.get("error_message"), payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "generation_runs":
        yield "generation_runs", dict(
            id=row_id, generation_job_id=oid(item.get("generation_job_id")),
            document_id=oid(item.get("document_id")),
            requested_by_user_id=oid(item.get("requested_by_user_id")),
            model_snapshot=data.get("model") or {},
            prompt_snapshot=data.get("prompts") or {},
            retrieval_snapshot=data.get("retrieval") or {}, result=data,
            metrics=data.get("execution") or {}, status=item.get("status") or "COMPLETED",
            created_at=created, updated_at=updated,
        )
        return
    if name == "questions":
        yield "questions", dict(
            id=row_id, question_code=item["question_code"],
            subject_id=oid(item.get("subject_id")),
            created_by_user_id=oid(item.get("created_by_user_id")),
            current_version=item["current_version"],
            current_version_id=oid(item.get("current_version_id")),
            approved_version_id=oid(item.get("approved_version_id")),
            lifecycle_status=item["lifecycle_status"], review_status=item["review_status"],
            evaluation_status=item["evaluation_status"],
            publication_status=item["publication_status"],
            assignment=data.get("review_assignment") or {}, payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "question_versions":
        yield "question_versions", dict(
            id=row_id, question_id=oid(item["question_id"]), version=item["version"],
            origin=item.get("origin") or "MANUAL", content=item["content"],
            question_data=data.get("question_data") or {},
            classification=data.get("classification") or {},
            clos=data.get("clos") or [], sources=data.get("sources") or [],
            content_hash=item["content_hash"],
            created_by_user_id=oid(item.get("created_by_user_id")),
            generation_run_id=oid(item.get("generation_run_id")),
            payload=data, created_at=created,
        )
        return
    if name == "evaluation_jobs":
        yield "evaluation_jobs", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            requested_by_user_id=oid(item.get("requested_by_user_id")),
            status=item["status"], evaluator_model_code=item["evaluator_model_code"],
            model_snapshot=data.get("model_snapshot") or {},
            policy_snapshot=data.get("policy_snapshot") or {},
            source_snapshot=data.get("source_snapshot") or [], result=data.get("result"),
            attempt_no=item.get("attempt_no", 0), lease_owner=item.get("worker_id"),
            lease_expires_at=item.get("lease_expires_at"),
            next_attempt_at=item.get("next_attempt_at"), error=data.get("error"),
            payload=data, created_at=created, updated_at=updated,
        )
        return
    if name == "question_evaluations":
        yield "question_evaluations", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            evaluation_job_id=oid(item.get("evaluation_job_id")), result=data,
            model_snapshot=data.get("evaluator_model") or {},
            policy_snapshot=data.get("policy") or {}, created_at=created,
        )
        return
    if name == "question_reviews":
        yield "question_reviews", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            reviewer_user_id=oid(item.get("reviewer_user_id")),
            decision=item["decision"], payload=data,
            reviewed_at=timestamp(item, "reviewed_at", "created_at"),
        )
        return
    if name == "question_review_drafts":
        yield "question_review_drafts", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            reviewer_user_id=oid(item["reviewer_user_id"]),
            draft=data.get("draft") or {}, payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "question_comments":
        yield "question_comments", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            author_user_id=oid(item["author_user_id"]), body=item["body"],
            payload=data, created_at=created, updated_at=updated,
            deleted_at=item.get("deleted_at"),
        )
        return
    if name == "exams":
        yield "exams", dict(
            id=row_id, created_by_user_id=oid(item.get("created_by_user_id")),
            subject_id=oid(item.get("subject_id")), status=item["status"],
            payload=data, created_at=created, updated_at=updated,
        )
        for position, ref in enumerate(item.get("questions") or [], start=1):
            yield "exam_questions", dict(
                exam_id=row_id, question_id=oid(ref["question_id"]),
                question_version_id=oid(ref["version_id"]), position=position,
                snapshot=normalized(ref.get("content_snapshot") or ref),
            )
        return
    if name == "exam_variants":
        yield "exam_variants", dict(
            id=row_id, exam_id=oid(item["exam_id"]), payload=data, created_at=created,
        )
        return
    if name == "llm_slots":
        yield "llm_slots", dict(
            provider=item["provider"], slot_index=item["slot_index"],
            holder_id=item.get("holder_id"), lease_expires_at=item.get("lease_expires_at"),
            updated_at=updated,
        )
        return
    if name == "notifications":
        yield "notifications", dict(
            id=row_id, recipient_user_id=oid(item["recipient_user_id"]),
            kind=item.get("type") or item.get("kind") or "UNKNOWN", payload=data,
            read_at=item.get("read_at"), created_at=created,
        )
        return
    if name == "audit_logs":
        actor = item.get("actor") or {}
        entity = item.get("entity") or {}
        actor_user_id = item.get("actor_user_id") or actor.get("user_id")
        yield "audit_logs", dict(
            id=row_id, actor_user_id=oid(actor_user_id),
            actor_type=actor.get("type") or ("USER" if actor_user_id else "SYSTEM"),
            actor_role=item.get("actor_role") or actor.get("role"),
            action=item.get("action") or "UNKNOWN",
            entity_type=item.get("entity_type") or entity.get("type") or "unknown",
            entity_id=oid(item.get("entity_id") or entity.get("id")),
            before_state=data.get("before") or {}, after_state=data.get("after") or {},
            changes=data.get("changes") or [],
            before_hash=item.get("before_hash"), after_hash=item.get("after_hash"),
            metadata=data.get("metadata") or {}, payload=data, created_at=created,
        )
        return
    if name == "review_settings":
        yield "review_settings", dict(
            id=row_id, payload=data,
            updated_by_user_id=oid(item.get("updated_by_user_id")),
            updated_at=updated,
        )
        return
    if name == "moodle_targets":
        yield "moodle_targets", dict(
            id=row_id, site_key=item["site_key"], site_name=item["site_name"],
            mode=item["mode"], secret_ref=item.get("token_env_var"),
            is_active=item.get("is_active", True), payload=data,
            created_at=created, updated_at=updated,
        )
        return
    if name == "moodle_publications":
        target = item.get("target") or {}
        yield "moodle_publications", dict(
            id=row_id, question_id=oid(item["question_id"]),
            question_version_id=oid(item["question_version_id"]),
            target_id=oid(item.get("target_id") or target.get("target_id")
                          or target.get("_id")),
            publisher_user_id=oid(item.get("publisher_user_id")),
            idempotency_key=item.get("idempotency_key") or row_id,
            status=item["status"], request_payload=data.get("request_payload") or {},
            response_payload=data.get("response_payload"),
            external_ref_id=item.get("moodle_question_ref_id"),
            payload=data, created_at=created, updated_at=updated,
        )
        return
    raise ValueError(f"No PostgreSQL projection for MongoDB collection {name}")


# Rows that still point at user accounts deleted from MongoDB. Anything not
# listed here must reference an existing user, or the copy fails on the
# foreign key instead of guessing an owner.
DELETED_USER_POLICIES = {
    # Keep audit history; only the FK column is cleared, payload keeps the id.
    "audit_logs": ("actor_user_id", "actor_detached"),
    # Nobody can read an inbox item of a deleted account.
    "notifications": ("recipient_user_id", "skipped"),
}


def known_user_ids(mongo_db) -> set[str]:
    return {str(item["_id"]) for item in mongo_db["users"].find({}, {"_id": 1})}


def reconcile_deleted_users(table: str, row: dict,
                            user_ids: set[str]) -> tuple[dict | None, str | None]:
    """Apply DELETED_USER_POLICIES; return the row to write (or None) and the action."""
    policy = DELETED_USER_POLICIES.get(table)
    if not policy:
        return row, None
    column, action = policy
    if row.get(column) is None or row[column] in user_ids:
        return row, None
    if action == "skipped":
        return None, action
    return {**row, column: None}, action


def upsert(connection, table: str, row: dict) -> None:
    columns = tuple(row)
    key_columns = (
        ("provider", "slot_index") if table == "llm_slots" else
        ("exam_id", "position") if table == "exam_questions" else
        ("document_id", "subject_id") if table == "document_subjects" else
        ("operation_id",) if table == "document_lineage_events" else ("id",)
    )
    assignments = [column for column in columns if column not in key_columns]
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, columns)),
        sql.SQL(", ").join(sql.Placeholder() for _ in columns),
        sql.SQL(", ").join(map(sql.Identifier, key_columns)),
        sql.SQL(", ").join(
            sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
            for column in assignments
        ),
    )
    values = [Jsonb(row[column]) if column in JSON_COLUMNS and row[column] is not None else row[column]
              for column in columns]
    connection.execute(statement, values)


def copy_business_data(mongo_db, connection, *, apply: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    if apply:
        connection.execute("SET CONSTRAINTS ALL DEFERRED")
    user_ids = known_user_ids(mongo_db)
    for name in SOURCE_ORDER:
        count = 0
        for item in mongo_db[name].find():
            for table, row in projected_rows(name, item):
                row, action = reconcile_deleted_users(table, row, user_ids)
                if action:
                    key = f"{table} ({action}: user deleted)"
                    counts[key] = counts.get(key, 0) + 1
                if row is None:
                    continue
                counts[table] = counts.get(table, 0) + 1
                if apply:
                    upsert(connection, table, row)
            count += 1
        if count == 0 and name not in counts:
            counts[name] = 0
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write shadow copy into PostgreSQL")
    parser.add_argument("--resume", action="store_true", help="Explicitly allow an upsert over an existing shadow copy")
    parser.add_argument("--mongo-uri", default=settings.mongo_uri)
    parser.add_argument("--mongo-db", default=settings.rag_db_name)
    args = parser.parse_args()
    if args.resume and not args.apply:
        parser.error("--resume requires --apply")
    if args.apply and not settings.postgres_dsn:
        parser.error("POSTGRES_DSN is required with --apply")
    with MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10000) as client:
        database = client[args.mongo_db]
        if args.apply:
            import psycopg
            from db.migrate import apply_migrations
            with psycopg.connect(settings.postgres_dsn, row_factory=dict_row) as connection:
                apply_migrations(connection, check=True)
                existing = connection.execute("""
                    SELECT
                      (SELECT count(*) FROM users) +
                      (SELECT count(*) FROM questions) +
                      (SELECT count(*) FROM documents) +
                      (SELECT count(*) FROM ai_models) AS total
                """).fetchone()["total"]
                if existing and not args.resume:
                    parser.error("PostgreSQL already contains business rows; use --resume only for an unmodified shadow copy")
                counts = copy_business_data(database, connection, apply=True)
        else:
            counts = copy_business_data(database, None, apply=False)
    mode = "shadow-copied" if args.apply else "source records"
    for name, count in sorted(counts.items()):
        # Deleted-user entries are a report, not a copied table.
        print(f"{name}: {count}" if "user deleted" in name else f"{name}: {count} {mode}")
    print("MongoDB vector collections were not copied:", ", ".join(sorted(VECTOR_COLLECTIONS)))


if __name__ == "__main__":
    main()
