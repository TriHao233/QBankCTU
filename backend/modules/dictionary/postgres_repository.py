"""PostgreSQL keyword dictionary used during document chunking."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from bson import ObjectId
from psycopg.types.json import Jsonb

from core.postgres import postgres_connection
from modules.dictionary.constants import DICTIONARY_CATEGORY, DICTIONARY_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _keyword_id(dictionary_id: str, status: str, keyword: str) -> str:
    return hashlib.sha256(
        f"{dictionary_id}:{status}:{keyword.lower()}".encode()
    ).hexdigest()[:24]


class PostgresDictionaryRepository:
    def init_default_dictionary(self, course_id: str) -> None:
        with postgres_connection() as conn:
            if conn.execute("SELECT 1 FROM legacy_dictionaries WHERE course_id=%s", (course_id,)).fetchone():
                return
        now = _now()
        dictionary_id = str(ObjectId())
        payload = {
            "_id": dictionary_id,
            "course_id": course_id,
            "name": DICTIONARY_NAME,
            "category": DICTIONARY_CATEGORY,
            "is_active": True,
            "core_keywords": [],
            "learned_keywords": [],
            "pending_keywords": [],
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        with postgres_connection() as conn:
            conn.execute("""
                INSERT INTO legacy_dictionaries
                    (id, course_id, name, category, is_active, payload, created_at, updated_at)
                VALUES (%s,%s,%s,%s,true,%s,%s,%s)
                ON CONFLICT (course_id) DO NOTHING
            """, (dictionary_id, course_id, payload["name"], payload["category"],
                  Jsonb(payload), now, now))

    def get_active_keywords(self, course_id: str) -> list[str]:
        self.init_default_dictionary(course_id)
        with postgres_connection() as conn:
            dictionary = conn.execute("""
                SELECT is_active FROM legacy_dictionaries WHERE course_id=%s
            """, (course_id,)).fetchone()
            if not dictionary or not dictionary["is_active"]:
                return []
            rows = conn.execute("""
                SELECT keyword FROM keywords
                WHERE payload->>'course_id'=%s AND status IN ('CORE','LEARNED')
                ORDER BY lower(keyword), id
            """, (course_id,)).fetchall()
        return list(dict.fromkeys(row["keyword"].lower().strip() for row in rows if row["keyword"].strip()))

    def add_pending_keywords(self, course_id: str, keywords: list[str]) -> None:
        if not keywords:
            return
        with postgres_connection() as conn:
            with conn.transaction():
                dictionary = conn.execute("""
                    SELECT id, payload FROM legacy_dictionaries
                    WHERE course_id=%s FOR UPDATE
                """, (course_id,)).fetchone()
                if not dictionary:
                    return
                existing = {
                    row["keyword"].strip().lower()
                    for row in conn.execute("""
                        SELECT keyword FROM keywords WHERE payload->>'course_id'=%s
                    """, (course_id,))
                }
                new_keywords = []
                for keyword in keywords:
                    clean = keyword.strip()
                    key = clean.lower()
                    if not key or key in existing:
                        continue
                    existing.add(key)
                    new_keywords.append(clean)
                    conn.execute("""
                        INSERT INTO keywords (id, subject_id, keyword, status, payload)
                        VALUES (%s,NULL,%s,'PENDING',%s)
                    """, (_keyword_id(dictionary["id"], "PENDING", clean), clean,
                          Jsonb({"course_id": course_id, "dictionary_id": dictionary["id"]})))
                if new_keywords:
                    payload = dict(dictionary["payload"] or {})
                    payload["pending_keywords"] = list(payload.get("pending_keywords") or []) + new_keywords
                    now = _now()
                    payload["updated_at"] = now.isoformat()
                    conn.execute("""
                        UPDATE legacy_dictionaries SET payload=%s, updated_at=%s WHERE id=%s
                    """, (Jsonb(payload), now, dictionary["id"]))
