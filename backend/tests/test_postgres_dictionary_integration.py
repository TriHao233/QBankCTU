"""Run with RUN_POSTGRES_INTEGRATION=1 and POSTGRES_DSN against a test database."""

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from bson import ObjectId

from core.config import settings
from core.postgres import postgres_connection
from db.copy_business_data import projected_rows, upsert
from modules.dictionary import mongodb
from modules.dictionary.service import add_pending_keywords, get_active_keywords


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRES_INTEGRATION") != "1" or not settings.postgres_dsn,
    reason="Requires an explicitly configured PostgreSQL test database",
)


def test_dictionary_is_postgres_backed_and_pending_keywords_are_unique(monkeypatch):
    monkeypatch.setattr(settings, "dictionary_store", "postgres")
    monkeypatch.setattr(mongodb, "_dictionaries_collection", lambda: pytest.fail("MongoDB was queried"))
    course_id = f"test-{uuid4().hex[:12]}"
    try:
        assert get_active_keywords(course_id) == []
        assert get_active_keywords(course_id) == []
        add_pending_keywords(course_id, ["  Học sâu  ", "học sâu", "", "Giải thuật"])
        assert "học sâu" not in get_active_keywords(course_id)
        with postgres_connection() as conn:
            rows = conn.execute("""
                SELECT keyword, status FROM keywords WHERE payload->>'course_id'=%s
                    AND lower(keyword)=lower(%s)
            """, (course_id, "học sâu")).fetchall()
            assert len(rows) == 1 and rows[0]["status"] == "PENDING"
            dictionary = conn.execute("""
                SELECT id, payload FROM legacy_dictionaries WHERE course_id=%s
            """, (course_id,)).fetchone()
            assert dictionary["payload"]["pending_keywords"] == ["Học sâu", "Giải thuật"]
            conn.execute("UPDATE keywords SET status='LEARNED' WHERE id=%s", (
                conn.execute("SELECT id FROM keywords WHERE payload->>'course_id'=%s AND keyword=%s",
                             (course_id, "Học sâu")).fetchone()["id"],
            ))
        assert "học sâu" in get_active_keywords(course_id)
    finally:
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM keywords WHERE payload->>'course_id'=%s", (course_id,))
                conn.execute("DELETE FROM legacy_dictionaries WHERE course_id=%s", (course_id,))


def test_shadow_copied_dictionary_is_read_from_normalized_keywords(monkeypatch):
    monkeypatch.setattr(settings, "dictionary_store", "postgres")
    course_id = f"copied-{uuid4().hex[:12]}"
    dictionary_id = ObjectId()
    now = datetime.now(timezone.utc)
    legacy = {
        "_id": dictionary_id, "course_id": course_id,
        "name": "Legacy", "category": "tech_keywords", "is_active": True,
        "core_keywords": [" Cây "], "learned_keywords": ["Đồ thị"],
        "pending_keywords": ["Hàng đợi"], "created_at": now, "updated_at": now,
    }
    try:
        with postgres_connection() as conn:
            with conn.transaction():
                for table, row in projected_rows("dictionaries", legacy):
                    upsert(conn, table, row)
        assert get_active_keywords(course_id) == ["cây", "đồ thị"]
        add_pending_keywords(course_id, ["cây", "HÀNG ĐỢI", "Ngăn xếp"])
        with postgres_connection() as conn:
            pending = conn.execute("""
                SELECT keyword FROM keywords
                WHERE payload->>'course_id'=%s AND status='PENDING' ORDER BY keyword
            """, (course_id,)).fetchall()
        assert [row["keyword"] for row in pending] == ["Hàng đợi", "Ngăn xếp"]
    finally:
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM keywords WHERE payload->>'course_id'=%s", (course_id,))
                conn.execute("DELETE FROM legacy_dictionaries WHERE course_id=%s", (course_id,))
