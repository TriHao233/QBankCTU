import hashlib
from datetime import datetime, timezone

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import CollectionInvalid

from core.database import get_auth_db, get_rag_db
from core.config import resolve_path, settings

SCHEMA_VERSION = 2

AUTH_COLLECTIONS = ("User",)

RAG_COLLECTIONS = (
    "users",
    "subjects",
    "documents",
    "document_jobs",
    "document_pages",
    "chunk_sets",
    "document_chunks",
    "vector_collections",
    "chunk_embeddings",
    "pipeline_lineage_events",
    "keywords",
    "ai_models",
    "prompt_templates",
    "evaluation_policies",
    "generation_jobs",
    "generation_runs",
    "questions",
    "question_versions",
    "evaluation_jobs",
    "llm_slots",
    "question_evaluations",
    "question_reviews",
    "question_review_drafts",
    "question_comments",
    "audit_logs",
    "notifications",
    "moodle_targets",
    "moodle_publications",
    "exams",
    "exam_variants",
    "schema_meta",
    "migration_id_map",
)

COLLECTIONS = AUTH_COLLECTIONS + RAG_COLLECTIONS

# Business collections move to PostgreSQL when their store flag is "postgres".
# Bootstrap then neither creates them nor their indexes, so MongoDB keeps only
# vector/RAG data (chunk_sets, document_chunks, vector_collections,
# chunk_embeddings, pipeline_lineage_events) plus schema_meta and
# migration_id_map.
POSTGRES_OWNERS = {
    "User": "user_store",
    "users": "user_store",
    "subjects": "catalog_store",
    "keywords": "dictionary_store",
    "documents": "document_store",
    "document_jobs": "document_store",
    "document_pages": "document_store",
    "ai_models": "ai_config_store",
    "prompt_templates": "ai_config_store",
    "evaluation_policies": "ai_config_store",
    "generation_jobs": "generation_store",
    "generation_runs": "generation_store",
    "questions": "question_store",
    "question_versions": "question_store",
    "evaluation_jobs": "question_store",
    "question_evaluations": "question_store",
    "question_reviews": "question_store",
    "question_review_drafts": "question_store",
    "question_comments": "question_store",
    "moodle_publications": "question_store",
    "llm_slots": "llm_slot_store",
    "audit_logs": "audit_store",
    "notifications": "notification_store",
    "moodle_targets": "moodle_target_store",
    "exams": "exam_store",
    "exam_variants": "exam_store",
}


def mongo_owns(collection: str) -> bool:
    """True while MongoDB is still the store of record for ``collection``."""
    store = POSTGRES_OWNERS.get(collection)
    return store is None or getattr(settings, store) != "postgres"

VALIDATORS = {
    "User": {
        "$jsonSchema": {
            "bsonType": "object",
            "additionalProperties": False,
            "required": ["uid", "token"],
            "properties": {
                "_id": {"bsonType": "objectId"},
                "uid": {"bsonType": "string", "minLength": 1},
                "token": {"bsonType": ["string", "null"]},
            },
        }
    },
    "users": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "firebase_uid",
                "email",
                "display_name",
                "role",
                "is_active",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "firebase_uid": {"bsonType": "string", "minLength": 1},
                "email": {"bsonType": "string", "minLength": 3},
                "display_name": {"bsonType": "string", "minLength": 1},
                "role": {"enum": ["Admin", "Teacher", "Reviewer"]},
                "generation_presets": {"bsonType": "array"},
                "is_active": {"bsonType": "bool"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "generation_jobs": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "request",
                "status",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "request": {"bsonType": "object"},
                "requested_by_user_id": {"bsonType": ["objectId", "null"]},
                "status": {
                    "enum": ["queued", "processing", "completed", "failed"]
                },
                "result": {"bsonType": ["object", "null"]},
                "metrics": {"bsonType": ["object", "null"]},
                "error_message": {"bsonType": ["string", "null"]},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "llm_slots": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": ["provider", "slot_index", "updated_at"],
            "properties": {
                "provider": {"bsonType": "string", "minLength": 1},
                "slot_index": {"bsonType": "int", "minimum": 0},
                "holder_id": {"bsonType": ["string", "null"]},
                "lease_expires_at": {"bsonType": ["date", "null"]},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "documents": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "title",
                "original_filename",
                "status",
                "current_version",
                "artifacts",
                "current_processing",
                "pipeline_summary",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "title": {"bsonType": "string", "minLength": 1},
                "original_filename": {"bsonType": "string", "minLength": 1},
                "subject_id": {"bsonType": ["objectId", "null"]},
                "subject_ids": {"bsonType": "array", "items": {"bsonType": "objectId"}},
                "status": {
                    "enum": [
                        "UPLOADED",
                        "PROCESSING",
                        "READY",
                        "FAILED",
                        "ARCHIVED",
                    ]
                },
                "current_version": {"bsonType": "int", "minimum": 1},
                "artifacts": {"bsonType": "array"},
                "current_processing": {"bsonType": "object"},
                "pending_processing": {"bsonType": "object"},
                "pipeline_summary": {"bsonType": "object"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "questions": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "question_code",
                "current_version",
                "current_version_id",
                "lifecycle_status",
                "evaluation_status",
                "review_status",
                "publication_status",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "question_code": {"bsonType": "string", "minLength": 1},
                "created_by_user_id": {"bsonType": ["objectId", "null"]},
                "subject_id": {"bsonType": ["objectId", "null"]},
                "review_submission": {"bsonType": "object"},
                "current_version": {"bsonType": "int", "minimum": 1},
                "current_version_id": {"bsonType": "objectId"},
                "lifecycle_status": {"enum": ["ACTIVE", "ARCHIVED"]},
                "evaluation_status": {
                    "enum": [
                        "NOT_STARTED",
                        "QUEUED",
                        "PROCESSING",
                        "PASSED",
                        "FAILED",
                        "ERROR",
                        "STALE",
                        "INSUFFICIENT_EVIDENCE",
                        "EVIDENCE_VALIDATION_FAILED",
                    ]
                },
                "review_status": {
                    "enum": [
                        "DRAFT",
                        "PENDING",
                        "APPROVED",
                        "REJECTED",
                        "NEEDS_REVISION",
                    ]
                },
                "publication_status": {
                    "enum": ["NOT_PUBLISHED", "PUBLISHED", "STALE", "FAILED"]
                },
                "review_assignment": {"bsonType": "object"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "question_review_drafts": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "question_id",
                "question_version_id",
                "question_version",
                "reviewer_user_id",
                "draft",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "question_id": {"bsonType": "objectId"},
                "question_version_id": {"bsonType": "objectId"},
                "question_version": {"bsonType": "int", "minimum": 1},
                "reviewer_user_id": {"bsonType": "objectId"},
                "decision": {"bsonType": ["string", "null"]},
                "draft": {"bsonType": "object"},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "question_comments": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "question_id",
                "question_version_id",
                "question_version",
                "author_user_id",
                "author_role",
                "body",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "question_id": {"bsonType": "objectId"},
                "question_version_id": {"bsonType": "objectId"},
                "question_version": {"bsonType": "int", "minimum": 1},
                "author_user_id": {"bsonType": "objectId"},
                "author_role": {"enum": ["Admin", "Teacher", "Reviewer"]},
                "body": {"bsonType": "string"},
                "mention_user_ids": {"bsonType": "array"},
                "edited_at": {"bsonType": ["date", "null"]},
                "deleted_at": {"bsonType": ["date", "null"]},
                "deleted_by_user_id": {"bsonType": ["objectId", "null"]},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "question_versions": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "question_id",
                "version",
                "origin",
                "classification",
                "content",
                "question_data",
                "sources",
                "content_hash",
                "created_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "question_id": {"bsonType": "objectId"},
                "version": {"bsonType": "int", "minimum": 1},
                "origin": {"enum": ["AI", "MANUAL", "IMPORT"]},
                "classification": {"bsonType": "object"},
                "content": {"bsonType": "string", "minLength": 1},
                "question_data": {"bsonType": "object"},
                "sources": {"bsonType": "array"},
                "content_hash": {"bsonType": "string", "minLength": 1},
                "created_at": {"bsonType": "date"},
            },
        }
    },
    "evaluation_jobs": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "question_id",
                "question_version_id",
                "question_version",
                "status",
                "evaluator_model_code",
                "trigger",
                "attempt_no",
                "queued_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "question_id": {"bsonType": "objectId"},
                "question_version_id": {"bsonType": "objectId"},
                "question_version": {"bsonType": "int", "minimum": 1},
                "question_snapshot_hash": {"bsonType": ["string", "null"]},
                "dedupe_key": {"bsonType": "string", "minLength": 1},
                "status": {
                    "enum": [
                        "QUEUED",
                        "PROCESSING",
                        "COMPLETED",
                        "ERROR",
                        "STALE",
                        "CANCELLED",
                        "BLOCKED",
                    ]
                },
                "trigger": {"bsonType": "string", "minLength": 1},
                "requested_by_user_id": {"bsonType": ["objectId", "null"]},
                "evaluator_model_code": {"bsonType": "string", "minLength": 1},
                "policy_snapshot": {"bsonType": "object"},
                "prompt_snapshot": {"bsonType": "object"},
                "source_snapshot": {"bsonType": "array"},
                "retrieval_snapshot": {"bsonType": "object"},
                "attempt_no": {"bsonType": "int", "minimum": 1},
                "max_attempts": {"bsonType": "int", "minimum": 1},
                "result": {"bsonType": ["object", "null"]},
                "error": {"bsonType": ["object", "null"]},
                "queued_at": {"bsonType": "date"},
                "started_at": {"bsonType": ["date", "null"]},
                "finished_at": {"bsonType": ["date", "null"]},
                "duration_ms": {"bsonType": ["int", "null"]},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
    "moodle_targets": {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "schema_version",
                "site_key",
                "site_name",
                "mode",
                "default_course_id",
                "default_category_id",
                "allowed_roles",
                "is_active",
                "created_at",
                "updated_at",
            ],
            "properties": {
                "schema_version": {"bsonType": "int", "minimum": 2},
                "site_key": {"bsonType": "string", "minLength": 1},
                "site_name": {"bsonType": "string", "minLength": 1},
                "mode": {"enum": ["MOCK", "REST_API"]},
                "base_url": {"bsonType": "string"},
                "token_env_var": {"bsonType": "string"},
                "default_course_id": {"bsonType": "string", "minLength": 1},
                "default_category_id": {"bsonType": "string", "minLength": 1},
                "allowed_roles": {"bsonType": "array"},
                "is_active": {"bsonType": "bool"},
                "last_check": {"bsonType": ["object", "null"]},
                "created_by_user_id": {"bsonType": ["objectId", "null"]},
                "updated_by_user_id": {"bsonType": ["objectId", "null"]},
                "created_at": {"bsonType": "date"},
                "updated_at": {"bsonType": "date"},
            },
        }
    },
}


def _ensure_collections(db, collection_names: tuple[str, ...]) -> None:
    existing = set(db.list_collection_names())
    for name in collection_names:
        if name in existing:
            continue
        options = {}
        if name in VALIDATORS:
            options = {
                "validator": VALIDATORS[name],
                "validationLevel": "strict",
                "validationAction": "error",
            }
        try:
            db.create_collection(name, **options)
        except CollectionInvalid:
            pass

    for name in collection_names:
        validator = VALIDATORS.get(name)
        if validator is None:
            continue
        db.command(
            {
                "collMod": name,
                "validator": validator,
                "validationLevel": "strict",
                "validationAction": "error",
            }
        )


def _ensure_indexes() -> None:
    rag_db = get_rag_db()
    if mongo_owns("User"):
        get_auth_db()["User"].create_indexes(
            [
                IndexModel([("uid", ASCENDING)], unique=True, name="uq_user_uid"),
            ]
        )
    if mongo_owns("users"):
        rag_db.users.create_indexes(
            [
                IndexModel([("firebase_uid", ASCENDING)], unique=True, name="uq_users_firebase_uid"),
                IndexModel([("email", ASCENDING)], unique=True, name="uq_users_email"),
                IndexModel([("role", ASCENDING), ("is_active", ASCENDING)], name="ix_users_role_active"),
            ]
        )
    if mongo_owns("subjects"):
        rag_db.subjects.create_index([("subject_code", ASCENDING)], unique=True, name="uq_subject_code")
    if mongo_owns("documents"):
        rag_db.documents.create_indexes(
            [
                IndexModel(
                    [
                        ("subject_id", ASCENDING),
                        ("chapter_id", ASCENDING),
                        ("status", ASCENDING),
                        ("created_at", DESCENDING),
                    ],
                    name="ix_documents_catalog",
                ),
                IndexModel([("uploaded_by_user_id", ASCENDING), ("created_at", DESCENDING)], name="ix_documents_uploader"),
                IndexModel([("subject_ids", ASCENDING), ("status", ASCENDING)], name="ix_documents_subjects_status"),
                IndexModel([("artifacts.sha256", ASCENDING)], name="ix_documents_artifact_hash"),
            ]
        )
        rag_db.document_jobs.create_indexes(
            [
                IndexModel(
                    [
                        ("document_id", ASCENDING),
                        ("document_version", ASCENDING),
                        ("job_type", ASCENDING),
                        ("attempt_no", ASCENDING),
                    ],
                    unique=True,
                    name="uq_document_job_attempt",
                ),
                IndexModel([("status", ASCENDING), ("queued_at", ASCENDING)], name="ix_document_jobs_queue"),
            ]
        )
        rag_db.document_pages.create_indexes(
            [
                IndexModel([("ocr_job_id", ASCENDING), ("page_number", ASCENDING)], unique=True, name="uq_ocr_job_page"),
                IndexModel([("ocr_job_id", ASCENDING), ("unit_number", ASCENDING)], name="ix_ocr_job_unit"),
                IndexModel(
                    [("document_id", ASCENDING), ("document_version", ASCENDING), ("page_number", ASCENDING)],
                    name="ix_document_pages_version",
                ),
            ]
        )
    rag_db.chunk_sets.create_indexes(
        [
            IndexModel([("chunk_job_id", ASCENDING)], unique=True, name="uq_chunk_set_job"),
            IndexModel([("document_id", ASCENDING), ("document_version", ASCENDING)], name="ix_chunk_sets_document"),
        ]
    )
    rag_db.document_chunks.create_indexes(
        [
            IndexModel([("chunk_set_id", ASCENDING), ("chunk_no", ASCENDING)], unique=True, name="uq_chunk_set_number"),
            IndexModel([("document_id", ASCENDING), ("chunk_set_id", ASCENDING)], name="ix_chunks_document_set"),
        ]
    )
    rag_db.vector_collections.create_index(
        [("provider", ASCENDING), ("collection_name", ASCENDING)],
        unique=True,
        name="uq_vector_collection",
    )
    rag_db.chunk_embeddings.create_indexes(
        [
            IndexModel(
                [("chunk_id", ASCENDING), ("vector_collection_id", ASCENDING)],
                unique=True,
                name="uq_chunk_embedding",
            ),
            IndexModel(
                [("vector_collection_id", ASCENDING), ("external_vector_id", ASCENDING)],
                unique=True,
                name="uq_external_vector",
            ),
        ]
    )
    rag_db.pipeline_lineage_events.create_indexes(
        [
            IndexModel([("document_id", ASCENDING), ("created_at", DESCENDING)], name="ix_lineage_document"),
            IndexModel([("event_type", ASCENDING), ("created_at", DESCENDING)], name="ix_lineage_event_type"),
            IndexModel([("operation_id", ASCENDING)], unique=True, name="uq_lineage_operation"),
        ]
    )
    if mongo_owns("generation_jobs"):
        rag_db.generation_jobs.create_indexes(
            [
                IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="ix_generation_jobs_queue"),
                IndexModel([("requested_by_user_id", ASCENDING), ("created_at", DESCENDING)], name="ix_generation_jobs_requester"),
                IndexModel(
                    [("requested_by_user_id", ASCENDING), ("idempotency_key", ASCENDING)],
                    unique=True,
                    partialFilterExpression={"idempotency_key": {"$type": "string"}},
                    name="uq_generation_jobs_idempotency",
                ),
                IndexModel([("status", ASCENDING), ("lease_expires_at", ASCENDING)], name="ix_generation_jobs_lease"),
                IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_generation_jobs"),
            ]
        )
    if mongo_owns("llm_slots"):
        rag_db.llm_slots.create_indexes(
            [
                IndexModel(
                    [("provider", ASCENDING), ("slot_index", ASCENDING)],
                    unique=True,
                    name="uq_llm_slots_provider_index",
                ),
                IndexModel(
                    [("provider", ASCENDING), ("lease_expires_at", ASCENDING)],
                    name="ix_llm_slots_lease",
                ),
            ]
        )
    if mongo_owns("generation_runs"):
        rag_db.generation_runs.create_indexes(
            [
                IndexModel([("document_id", ASCENDING), ("created_at", DESCENDING)], name="ix_generation_document"),
                IndexModel([("requested_by_user_id", ASCENDING), ("created_at", DESCENDING)], name="ix_generation_requester"),
                IndexModel([("generation_job_id", ASCENDING), ("status", ASCENDING)], name="ix_generation_job_status"),
            ]
        )
    if mongo_owns("questions"):
        rag_db.questions.create_indexes(
            [
                IndexModel([("question_code", ASCENDING)], unique=True, name="uq_question_code"),
                IndexModel([("created_by_user_id", ASCENDING), ("updated_at", DESCENDING)], name="ix_questions_owner"),
                IndexModel(
                    [("lifecycle_status", ASCENDING), ("created_at", DESCENDING)],
                    name="ix_questions_active_created",
                ),
                IndexModel(
                    [
                        ("lifecycle_status", ASCENDING),
                        ("subject_id", ASCENDING),
                        ("created_at", DESCENDING),
                    ],
                    name="ix_questions_active_subject_created",
                ),
                IndexModel(
                    [
                        ("lifecycle_status", ASCENDING),
                        ("review_status", ASCENDING),
                        ("review_submission.submitted_at", DESCENDING),
                    ],
                    name="ix_questions_active_review_submitted",
                ),
                IndexModel(
                    [
                        ("review_status", ASCENDING),
                        ("evaluation_status", ASCENDING),
                        ("updated_at", DESCENDING),
                    ],
                    name="ix_questions_workflow",
                ),
                IndexModel(
                    [
                        ("review_status", ASCENDING),
                        ("review_assignment.status", ASCENDING),
                        ("review_assignment.reviewer_user_id", ASCENDING),
                        ("review_assignment.lock_expires_at", ASCENDING),
                    ],
                    name="ix_questions_review_assignment",
                ),
            ]
        )
        rag_db.question_versions.create_indexes(
            [
                IndexModel([("question_id", ASCENDING), ("version", ASCENDING)], unique=True, name="uq_question_version"),
                IndexModel([("sources.chunk_id", ASCENDING)], name="ix_question_sources"),
            ]
        )
        rag_db.evaluation_jobs.create_indexes(
            [
                IndexModel([("status", ASCENDING), ("queued_at", ASCENDING)], name="ix_evaluation_jobs_queue"),
                IndexModel([("status", ASCENDING), ("lease_expires_at", ASCENDING)], name="ix_evaluation_jobs_lease"),
                IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0, name="ttl_evaluation_jobs"),
                IndexModel([("question_version_id", ASCENDING), ("created_at", DESCENDING)], name="ix_evaluation_jobs_version"),
                IndexModel(
                    [("dedupe_key", ASCENDING)],
                    unique=True,
                    name="uq_active_evaluation_job",
                    partialFilterExpression={
                        "$or": [{"status": "QUEUED"}, {"status": "PROCESSING"}],
                    },
                ),
            ]
        )
        rag_db.question_evaluations.create_index(
            [("question_version_id", ASCENDING), ("created_at", DESCENDING)],
            name="ix_evaluations_version",
        )
        rag_db.question_reviews.create_index(
            [("question_version_id", ASCENDING), ("reviewed_at", DESCENDING)],
            name="ix_reviews_version",
        )
        rag_db.question_review_drafts.create_indexes(
            [
                IndexModel(
                    [("question_id", ASCENDING), ("reviewer_user_id", ASCENDING)],
                    unique=True,
                    name="uq_review_draft_question_reviewer",
                ),
                IndexModel(
                    [("reviewer_user_id", ASCENDING), ("updated_at", DESCENDING)],
                    name="ix_review_drafts_reviewer_updated",
                ),
            ]
        )
        rag_db.question_comments.create_index(
            [("question_id", ASCENDING), ("deleted_at", ASCENDING), ("created_at", ASCENDING)],
            name="ix_question_comments_thread",
        )
    if mongo_owns("audit_logs"):
        rag_db.audit_logs.create_index(
            [("entity.type", ASCENDING), ("entity.id", ASCENDING), ("created_at", DESCENDING)],
            name="ix_audit_entity",
        )
        rag_db.audit_logs.create_index(
            [("entity_type", ASCENDING), ("entity_id", ASCENDING), ("created_at", DESCENDING)],
            name="ix_audit_entity_flat",
        )
        rag_db.audit_logs.create_index(
            [("actor_user_id", ASCENDING), ("created_at", DESCENDING)],
            name="ix_audit_actor_flat",
        )
        rag_db.audit_logs.create_index(
            [("actor.user_id", ASCENDING), ("created_at", DESCENDING)],
            name="ix_audit_actor",
        )
        rag_db.audit_logs.create_index(
            [("action", ASCENDING), ("created_at", DESCENDING)],
            name="ix_audit_action",
        )
    if mongo_owns("notifications"):
        rag_db.notifications.create_indexes(
            [
                IndexModel(
                    [("recipient_user_id", ASCENDING), ("is_read", ASCENDING), ("created_at", DESCENDING)],
                    name="ix_notifications_recipient_read",
                ),
                IndexModel(
                    [("recipient_user_id", ASCENDING), ("created_at", DESCENDING)],
                    name="ix_notifications_recipient_created",
                ),
            ]
        )
    if mongo_owns("moodle_targets"):
        rag_db.moodle_targets.create_indexes(
            [
                IndexModel([("site_key", ASCENDING)], unique=True, name="uq_moodle_target_site_key"),
                IndexModel([("is_active", ASCENDING), ("mode", ASCENDING)], name="ix_moodle_targets_active_mode"),
            ]
        )
    if mongo_owns("moodle_publications"):
        rag_db.moodle_publications.create_indexes(
            [
                IndexModel([("idempotency_key", ASCENDING)], unique=True, name="uq_publication_idempotency"),
                IndexModel([("target.moodle_site_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)], name="ix_publications_target_status"),
                IndexModel([("question_id", ASCENDING), ("created_at", DESCENDING)], name="ix_publications_question"),
            ]
        )
    rag_db.migration_id_map.create_index(
        [("source_collection", ASCENDING), ("source_id", ASCENDING)],
        unique=True,
        name="uq_migration_source",
    )


def _seed_reference_data() -> None:
    db = get_rag_db()
    now = datetime.now(timezone.utc)
    weights = {
        "faithfulness": 0.35,
        "contextual_relevancy": 0.20,
        "answer_relevancy": 0.15,
        "bloom_alignment": 0.15,
        "clo_alignment": 0.15,
    }
    if mongo_owns("evaluation_policies"):
        has_active_policy = db.evaluation_policies.find_one(
            {"is_active": True}, {"_id": 1}
        ) is not None
        db.evaluation_policies.update_one(
            {"policy_name": "Default question quality policy", "version": 2},
            {
                "$setOnInsert": {
                    "schema_version": SCHEMA_VERSION,
                    "weights": weights,
                    "weights_hash": hashlib.sha256(str(sorted(weights.items())).encode()).hexdigest(),
                    "thresholds": {"yellow_min": 0.50, "green_min": 0.75, "pass_min": 0.65},
                    "is_active": not has_active_policy,
                    "created_at": now,
                }
            },
            upsert=True,
        )
    if mongo_owns("moodle_targets"):
        db.moodle_targets.update_one(
            {"site_key": "demo-moodle"},
            {
                "$setOnInsert": {
                    "schema_version": SCHEMA_VERSION,
                    "site_name": "Demo Moodle",
                    "mode": "MOCK",
                    "base_url": "",
                    "token_env_var": "",
                    "default_course_id": "ctdl-demo",
                    "default_category_id": "qbank-demo",
                    "allowed_roles": ["Admin", "Reviewer"],
                    "is_active": True,
                    "last_check": None,
                    "created_by_user_id": None,
                    "updated_by_user_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
    if not mongo_owns("ai_models"):
        return
    for model in (
        {
            "model_code": "qwen3-8b",
            "model_name": "qwen3:8b",
            "display_name": "Qwen3 (8B)",
            "description": "Sinh và đánh giá câu hỏi trên Ollama.",
            "runtime": "OLLAMA",
            "kind": "CHAT",
            "capabilities": ["QUESTION_GENERATION", "QUESTION_EVALUATION"],
            "priority": 10,
            "config": {"think": False},
        },
        {
            "model_code": "deepseek",
            "model_name": settings.deepseek_model_name,
            "display_name": "DeepSeek R1",
            "description": "Phù hợp với câu hỏi cần suy luận.",
            "runtime": "OLLAMA",
            "kind": "REASONING",
            "capabilities": ["QUESTION_EVALUATION", "QUESTION_GENERATION"],
            "priority": 20,
        },
        {
            "model_code": "deepseek-r1",
            "model_name": settings.deepseek_model_name,
            "display_name": "DeepSeek R1 - Đánh giá",
            "description": "Dùng để đánh giá chất lượng câu hỏi.",
            "runtime": "OLLAMA",
            "kind": "REASONING",
            "capabilities": ["QUESTION_EVALUATION"],
            "priority": 15,
        },
        {
            "model_code": "gemini",
            "model_name": settings.gemini_model_name,
            "display_name": "Gemini 3.6 Flash",
            "description": "Sinh câu hỏi qua Google Gemini API.",
            "runtime": "GEMINI",
            "kind": "CHAT",
            "capabilities": ["QUESTION_GENERATION", "QUESTION_EVALUATION"],
            "priority": 30,
            "is_local": False,
        },
    ):
        is_local = model.get("is_local", True)
        db.ai_models.update_one(
            {"model_code": model["model_code"]},
            {
                "$setOnInsert": {
                    "schema_version": SCHEMA_VERSION,
                    **model,
                    "revision": "remote" if not is_local else "local",
                    "config": model.get("config", {}),
                    "is_local": is_local,
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        db.ai_models.update_one(
            {"model_code": model["model_code"], "display_name": {"$exists": False}},
            {
                "$set": {
                    "display_name": model["display_name"],
                    "description": model["description"],
                    "updated_at": now,
                }
            },
        )
    db.ai_models.update_many(
        {
            "model_code": {"$in": ["qwen3-8b", "deepseek", "deepseek-r1", "gemini"]},
            "config.endpoint": "http://localhost:11434/api/generate",
        },
        {"$unset": {"config.endpoint": ""}},
    )
    db.ai_models.update_one(
        {
            "model_code": "gemini",
            "$or": [
                {"model_name": {"$in": ["", "gemini-2.0-flash"]}},
                {"display_name": {"$in": ["", "Gemini"]}},
            ],
        },
        {
            "$set": {
                "model_name": settings.gemini_model_name,
                "display_name": "Gemini 3.6 Flash",
                "revision": "remote",
                "is_local": False,
                "updated_at": now,
            }
        },
    )
    _seed_prompt_templates(db, now)


def _seed_prompt_templates(db, now: datetime) -> None:
    prompt_root = resolve_path(settings.prompts_dir)
    specs = [
        ("system", "SYSTEM", "System prompt", prompt_root / "system.txt"),
        ("question_rule", "QUESTION_RULE", "Forbidden question rules", prompt_root / "question_rule.txt"),
        ("quy_dinh_do_kho", "DIFFICULTY_RULE", "Quy định độ khó", prompt_root / "quy_dinh_do_kho.txt"),
        ("output_format", "OUTPUT_FORMAT", "Output format", prompt_root / "output_format.txt"),
    ]
    for folder, kind in (
        ("bloom", "BLOOM"),
        ("question_type", "QUESTION_TYPE"),
        ("question_structure", "QUESTION_STRUCTURE"),
        ("evaluation", "EVALUATION"),
    ):
        folder_path = prompt_root / folder
        if folder_path.exists():
            for path in sorted(folder_path.rglob("*.txt")):
                relative_key = path.relative_to(folder_path).with_suffix("").as_posix()
                template_key = relative_key.replace("/", ":")
                specs.append((f"{folder}:{template_key}", kind, relative_key, path))
    for template_key, kind, name, path in specs:
        if not path.exists():
            continue
        prompt_body = path.read_text(encoding="utf-8")
        db.prompt_templates.update_one(
            {"template_key": template_key, "version": 1},
            {
                "$setOnInsert": {
                    "schema_version": SCHEMA_VERSION,
                    "template_key": template_key,
                    "version": 1,
                    "kind": kind,
                    "name": name,
                    "prompt_body": prompt_body,
                    "content_hash": hashlib.sha256(prompt_body.encode("utf-8")).hexdigest(),
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )


def bootstrap_database() -> None:
    """Create or align V2 collections without deleting existing data."""
    if settings.auth_db_name == settings.rag_db_name:
        raise ValueError("AUTH_DB_NAME và RAG_DB_NAME phải là hai database khác nhau")
    if mongo_owns("User"):
        _ensure_collections(get_auth_db(), AUTH_COLLECTIONS)
    _ensure_collections(
        get_rag_db(),
        tuple(name for name in RAG_COLLECTIONS if mongo_owns(name)),
    )
    _ensure_indexes()
    _seed_reference_data()
    now = datetime.now(timezone.utc)
    get_rag_db().schema_meta.update_one(
        {"_id": "database_schema"},
        {
            "$set": {"current_version": SCHEMA_VERSION, "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
