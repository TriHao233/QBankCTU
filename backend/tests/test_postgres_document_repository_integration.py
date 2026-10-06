"""Run with RUN_POSTGRES_INTEGRATION=1 and POSTGRES_DSN on a test database."""

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from bson import ObjectId

from core.config import settings
from core import outbox
from core.postgres import postgres_connection
from db.copy_business_data import projected_rows, upsert
from modules.documents import postgres_repository
from modules.documents.postgres_repository import PostgresDocumentRepository
from modules.documents.store import get_document_repository
from modules.admin.overview_service import AdminOverviewService
from modules.admin.jobs_service import AdminJobService
from modules.admin.job_metrics import _postgres_document_queue_metrics
from modules.users.postgres_repository import PostgresUserRepository
from modules.users import postgres_repository as postgres_user_repository


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1" or not settings.postgres_dsn,
    reason="Requires an explicitly configured PostgreSQL test database",
)


def test_document_metadata_crud_is_postgres_backed(monkeypatch):
    monkeypatch.setattr(settings, "user_store", "postgres")
    monkeypatch.setattr(settings, "catalog_store", "postgres")
    monkeypatch.setattr(postgres_repository, "get_database",
                        lambda: SimpleNamespace(documents=object()))
    suffix = uuid4().hex[:12]
    owner = PostgresUserRepository().create({
        "firebase_uid": f"document-{suffix}",
        "email": f"document-{suffix}@example.test",
        "display_name": "Teacher", "role": "Teacher",
    })
    now = datetime.now(timezone.utc)
    primary, secondary = ObjectId(), ObjectId()
    for subject_id, code in ((primary, "A"), (secondary, "B")):
        subject = {
            "_id": subject_id, "subject_code": f"{code}-{suffix}",
            "subject_name": code, "is_active": True,
            "created_at": now, "updated_at": now,
        }
        with postgres_connection() as conn:
            for table, row in projected_rows("subjects", subject):
                upsert(conn, table, row)
    document_id = None
    try:
        repository = PostgresDocumentRepository()
        created = repository.create({
            "title": "Slide", "original_filename": "slide.pdf",
            "subject_id": str(primary), "subject_ids": [str(secondary), str(primary)],
            "original_uri": "C:/uploads/slide.pdf", "sha256": "digest", "size_bytes": 12,
        }, owner["_id"])
        document_id = created["_id"]
        fetched = repository.find_by_id(document_id)
        assert fetched["_id"] == document_id
        assert fetched["subject_ids"] == [primary, secondary]
        assert fetched["uploaded_by_user_id"] == owner["_id"]
        assert fetched["artifacts"][0]["storage"]["uri"] == "C:/uploads/slide.pdf"
        visible, total = repository.list(1, 10, None, "Slide", subject_id=secondary,
                                         visible_to_user_id=owner["_id"])
        assert total == 1 and visible[0]["_id"] == document_id
        hidden, total = repository.list(1, 10, None, None,
                                        visible_to_user_id=ObjectId())
        assert total == 0 and hidden == []
        monkeypatch.setattr(settings, "document_store", "postgres")

        class FakeQuestions:
            def count_documents(self, query):
                return 2 if "lifecycle_status" in query else 1

        monkeypatch.setattr(postgres_user_repository, "get_database",
                            lambda: SimpleNamespace(questions=FakeQuestions()))
        profile = PostgresUserRepository()
        assert profile.get_stats(owner["_id"]) == {
            "documents_count": 1, "questions_count": 2,
            "pending_questions_count": 1,
        }
        assert profile.get_calendar_documents(owner["_id"])[0]["_id"] == document_id
        overview = AdminOverviewService(SimpleNamespace())
        assert overview._document_count({"status": "UPLOADED"}) == 1
        assert overview._document_count({"status": {"$in": ["READY", "UPLOADED"]}}) == 1

        updated = repository.update(document_id, {"title": "Slide mới",
                                                  "shared_scope": "SUBJECT"})
        assert updated["title"] == "Slide mới"
        shared, total = repository.list(1, 10, None, "Slide mới",
                                         visible_to_user_id=ObjectId(),
                                         shared_subject_ids=[str(secondary)])
        assert total == 1 and shared[0]["_id"] == document_id
        _other_subject, total = repository.list(1, 10, None, "Slide mới",
                                                visible_to_user_id=ObjectId(),
                                                shared_subject_ids=[str(ObjectId())])
        assert total == 0
        with postgres_connection() as conn:
            row = conn.execute("SELECT title FROM documents WHERE id=%s",
                               (str(document_id),)).fetchone()
            assert row["title"] == "Slide mới"
            assert conn.execute("SELECT count(*) AS n FROM document_subjects WHERE document_id=%s",
                                (str(document_id),)).fetchone()["n"] == 2
            assert conn.execute("SELECT count(*) AS n FROM document_artifacts WHERE document_id=%s",
                                (str(document_id),)).fetchone()["n"] == 1
        job = repository.create_job(document_id, "OCR", config={"source_format": "pdf"})
        assert job["attempt_no"] == 1
        assert repository.find_job(job["_id"])["status"] == "QUEUED"
        assert repository.list_jobs(document_id)[0]["_id"] == job["_id"]
        assert _postgres_document_queue_metrics(datetime.now(timezone.utc))["queued"] >= 1
        admin_jobs = AdminJobService(SimpleNamespace()).list_jobs(
            page=1, page_size=10, job_kind="document", user_id=str(owner["_id"]),
        )
        assert admin_jobs["total"] == 1
        assert admin_jobs["items"][0]["id"] == str(job["_id"])
        assert repository.save_pages(str(document_id), str(job["_id"]), [{
            "unit_number": 1, "page_number": 1, "text": "Clean",
            "original_text": "Original", "source_location": {"page": 1},
        }]) == 1
        page = repository.list_pages(document_id, document_version=1)[0]
        assert page["raw_text"] == "Original"
        assert page["cleaned_text"] == "Clean"
        assert page["source_location"] == {"page": 1}
        assert isinstance(get_document_repository(), PostgresDocumentRepository)
        from modules.ocr.mongodb import get_document_status
        from modules.rag.mongodb import get_document_record, iter_document_pages
        assert get_document_status(str(job["_id"]))["document"]["id"] == str(document_id)
        assert get_document_record(str(document_id))["_id"] == document_id
        assert list(iter_document_pages(str(document_id), job["_id"]))[0]["text"] == "Clean"
        changed = repository.update_page(
            document_id, page["_id"], document_version=1, cleaned_text="Edited",
        )
        assert changed["cleaned_text"] == "Edited"
        assert repository.list_pages(document_id)[0]["cleaned_text"] == "Edited"
        completed = repository.update_job(job["_id"], "COMPLETED", stats={"pages": 1})
        assert completed["status"] == "COMPLETED" and completed["progress"] == 100
        assert repository.find_by_id(document_id)["current_processing"]["ocr_job_id"] == job["_id"]
        repository.attach_processing_artifact(
            document_id, job_id=job["_id"], uri="C:/ocr/result.md", size_bytes=8,
            sha256="ocr-digest", artifact_type="EXTRACTION_MARKDOWN",
            mime_type="text/markdown",
        )
        assert len(repository.find_by_id(document_id)["artifacts"]) == 2
        assert repository.find_by_id(document_id)["page_count"] == 1
        chunk_job = repository.create_job(document_id, "CHUNK")
        cancelled = repository.update_job(chunk_job["_id"], "CANCELLED",
                                          error_message="Cancelled by teacher")
        assert cancelled["status"] == "CANCELLED"
        with postgres_connection() as conn:
            event_row = conn.execute(
                "SELECT event_type, payload FROM outbox_events WHERE event_key=%s",
                (f"document.chunk_cancel:{chunk_job['_id']}",),
            ).fetchone()
        assert event_row["event_type"] == "document.chunk_cancel"
        assert event_row["payload"]["document_id"] == str(document_id)
        monkeypatch.setattr(outbox, "get_database",
                            lambda: (_ for _ in ()).throw(RuntimeError("private detail")))
        assert outbox.process_available_outbox_once("test-worker") is True
        with postgres_connection() as conn:
            failed = conn.execute(
                "SELECT status, last_error FROM outbox_events WHERE event_key=%s",
                (f"document.chunk_cancel:{chunk_job['_id']}",),
            ).fetchone()
            assert failed == {"status": "PENDING", "last_error": "RuntimeError"}
            conn.execute("UPDATE outbox_events SET next_attempt_at=now()-interval '1 second' "
                         "WHERE event_key=%s", (f"document.chunk_cancel:{chunk_job['_id']}",))

        class FakeChunkSets:
            def __init__(self):
                self.updated = None

            def find(self, *_args):
                return [{"_id": ObjectId()}]

            def update_many(self, query, update):
                self.updated = (query, update)

        class FakeEmbeddings:
            def __init__(self):
                self.updated = None

            def update_many(self, query, update):
                self.updated = (query, update)

        vector_db = SimpleNamespace(chunk_sets=FakeChunkSets(),
                                    chunk_embeddings=FakeEmbeddings())
        monkeypatch.setattr(outbox, "get_database", lambda: vector_db)
        assert outbox.process_available_outbox_once("test-worker") is True
        assert vector_db.chunk_sets.updated[1]["$set"]["status"] == "CANCELLED"
        assert vector_db.chunk_embeddings.updated[1]["$set"]["status"] == "CANCELLED"
        with postgres_connection() as conn:
            assert conn.execute("SELECT status FROM outbox_events WHERE event_key=%s",
                                (f"document.chunk_cancel:{chunk_job['_id']}",)).fetchone()["status"] == "DONE"
        stale = repository.create_job(document_id, "INDEX")
        cutoff = datetime.now(timezone.utc)
        with postgres_connection() as conn:
            conn.execute(
                "UPDATE document_jobs SET updated_at=%s WHERE id=%s",
                (cutoff.replace(year=cutoff.year - 1), str(stale["_id"])),
            )
        assert repository.recover_stale_jobs(cutoff, "worker restarted") == 1
        assert repository.find_job(stale["_id"])["status"] == "FAILED"
        fresh = repository.create_job(document_id, "INDEX")
        assert repository.recover_stale_jobs(cutoff, "worker restarted") == 0
        assert repository.find_job(fresh["_id"])["status"] == "QUEUED"
        assert repository.archive(document_id) is True
        assert repository.find_by_id(document_id) is None
        assert repository.archive(document_id) is False
    finally:
        with postgres_connection() as conn:
            if document_id:
                conn.execute("DELETE FROM outbox_events WHERE aggregate_id=%s",
                             (str(document_id),))
                conn.execute("DELETE FROM document_pages WHERE document_id=%s",
                             (str(document_id),))
                conn.execute("DELETE FROM document_jobs WHERE document_id=%s",
                             (str(document_id),))
                conn.execute("DELETE FROM document_artifacts WHERE document_id=%s",
                             (str(document_id),))
                conn.execute("DELETE FROM documents WHERE id=%s", (str(document_id),))
            conn.execute("DELETE FROM subjects WHERE id=ANY(%s)",
                         ([str(primary), str(secondary)],))
            conn.execute("DELETE FROM users WHERE id=%s", (str(owner["_id"]),))


def test_legacy_artifact_without_id_stays_readable(monkeypatch):
    """Artifact chép từ MongoDB không có _id nhận id tổng hợp, không phải ObjectId."""
    monkeypatch.setattr(settings, "user_store", "postgres")
    monkeypatch.setattr(settings, "catalog_store", "postgres")
    monkeypatch.setattr(settings, "document_store", "postgres")
    suffix = uuid4().hex[:12]
    owner = PostgresUserRepository().create({
        "firebase_uid": f"legacy-artifact-{suffix}",
        "email": f"legacy-artifact-{suffix}@example.test",
        "display_name": "Teacher", "role": "Teacher",
    })
    now = datetime.now(timezone.utc)
    document_id = ObjectId()
    title = f"Legacy {suffix}"
    legacy_document = {
        "_id": document_id, "title": title, "original_filename": "legacy.pdf",
        "uploaded_by_user_id": owner["_id"], "status": "READY", "current_version": 1,
        "artifacts": [{
            "type": "ORIGINAL_PDF", "storage": {"provider": "LOCAL", "uri": "C:/uploads/legacy.pdf"},
            "document_version": 1, "created_at": now,
        }],
        "created_at": now, "updated_at": now,
    }
    try:
        with postgres_connection() as conn:
            for table, row in projected_rows("documents", legacy_document):
                upsert(conn, table, row)
        repository = PostgresDocumentRepository()
        synthetic_id = f"{document_id}:artifact:0"
        assert repository.find_by_id(document_id)["artifacts"][0]["_id"] == synthetic_id
        listed, total = repository.list(1, 10, None, title)
        assert total == 1 and listed[0]["artifacts"][0]["_id"] == synthetic_id
        monitor = AdminOverviewService(SimpleNamespace()).list_documents(1, 10, None, title)
        assert monitor["total"] == 1
        assert monitor["items"][0]["owner"]["email"] == owner["email"]
    finally:
        with postgres_connection() as conn:
            conn.execute("DELETE FROM document_artifacts WHERE document_id=%s", (str(document_id),))
            conn.execute("DELETE FROM documents WHERE id=%s", (str(document_id),))
            conn.execute("DELETE FROM users WHERE id=%s", (str(owner["_id"]),))


def test_chunk_completion_promotes_postgres_document_after_vector_write(monkeypatch):
    from contextlib import contextmanager
    from modules.rag import mongodb as rag_store

    monkeypatch.setattr(settings, "user_store", "postgres")
    monkeypatch.setattr(settings, "catalog_store", "postgres")
    monkeypatch.setattr(settings, "document_store", "postgres")
    monkeypatch.setattr(postgres_repository, "get_database",
                        lambda: SimpleNamespace(documents=object()))
    suffix = uuid4().hex[:12]
    user = PostgresUserRepository().create({
        "firebase_uid": f"chunk-{suffix}", "email": f"chunk-{suffix}@example.test",
        "display_name": "Teacher", "role": "Teacher",
    })
    subject_id = ObjectId()
    now = datetime.now(timezone.utc)
    subject = {"_id": subject_id, "subject_code": f"C-{suffix}",
               "subject_name": "Chunk", "created_at": now, "updated_at": now}
    with postgres_connection() as conn:
        for table, row in projected_rows("subjects", subject):
            upsert(conn, table, row)
    document_id = None
    try:
        repository = PostgresDocumentRepository()
        document = repository.create({
            "title": "Chunk source", "original_filename": "chunk.txt",
            "subject_id": str(subject_id),
        }, user["_id"])
        document_id = document["_id"]
        ocr_job = repository.create_job(document_id, "OCR")
        repository.update_job(ocr_job["_id"], "COMPLETED")
        chunk_job = repository.create_job(document_id, "CHUNK")
        repository.update_job(chunk_job["_id"], "PROCESSING")
        chunk_set_id, vector_id = ObjectId(), ObjectId()

        class FakeSets:
            available = False
            updated = None

            def find_one(self, *_args, **_kwargs):
                return {"source_ocr_job_id": ocr_job["_id"]} if self.available else None

            def update_one(self, query, update, **_kwargs):
                self.updated = (query, update)

        class FakeEmbeddings:
            updated = None

            def update_many(self, query, update, **_kwargs):
                self.updated = (query, update)

        vector_db = SimpleNamespace(chunk_sets=FakeSets(), chunk_embeddings=FakeEmbeddings())
        monkeypatch.setattr(rag_store, "get_database", lambda: vector_db)

        @contextmanager
        def no_mongo_transaction():
            yield None

        monkeypatch.setattr(rag_store, "mongo_transaction", no_mongo_transaction)
        arguments = dict(total_chunks=2, total_characters=30,
                         stats={"total_chunks": 2}, dry_run=False)
        with pytest.raises(ValueError, match="chunk set"):
            rag_store.complete_chunk_set(
                str(document_id), str(chunk_job["_id"]), str(chunk_set_id),
                str(vector_id), **arguments,
            )
        assert repository.find_by_id(document_id)["current_processing"]["chunk_set_id"] is None
        vector_db.chunk_sets.available = True
        rag_store.complete_chunk_set(
            str(document_id), str(chunk_job["_id"]), str(chunk_set_id),
            str(vector_id), **arguments,
        )
        ready = repository.find_by_id(document_id)
        assert ready["status"] == "READY"
        assert ready["current_processing"]["chunk_set_id"] == chunk_set_id
        assert ready["current_processing"]["vector_collection_id"] == vector_id
        assert repository.find_job(chunk_job["_id"])["status"] == "COMPLETED"
        assert vector_db.chunk_sets.updated[1]["$set"]["status"] == "COMPLETED"
        assert vector_db.chunk_embeddings.updated is not None
        rag_store.complete_chunk_set(
            str(document_id), str(chunk_job["_id"]), str(chunk_set_id),
            str(vector_id), **arguments,
        )
        assert repository.find_by_id(document_id)["pending_processing"] == {}
    finally:
        with postgres_connection() as conn:
            if document_id:
                conn.execute("DELETE FROM document_jobs WHERE document_id=%s",
                             (str(document_id),))
                conn.execute("DELETE FROM documents WHERE id=%s", (str(document_id),))
            conn.execute("DELETE FROM subjects WHERE id=%s", (str(subject_id),))
            conn.execute("DELETE FROM users WHERE id=%s", (str(user["_id"]),))
