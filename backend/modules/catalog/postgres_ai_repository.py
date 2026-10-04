"""Versioned PostgreSQL store for model, prompt and evaluation configuration."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from bson import ObjectId
from psycopg.types.json import Jsonb

from core.config import settings
from core.postgres import postgres_connection
from core.postgres_audit import write_postgres_audit_event


def _now():
    return datetime.now(timezone.utc)


def _hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _api(row: dict | None) -> dict | None:
    if row is None:
        return None
    record = dict(row)
    record["_id"] = record.pop("id")
    return record


def _audit(conn, actor_id, action: str, entity_type: str, entity_id: str,
           before: dict, after: dict) -> None:
    if actor_id is None:
        return
    write_postgres_audit_event(
        conn, action=action, entity_type=entity_type, entity_id=entity_id,
        actor_user_id=actor_id, before=before, after=after,
    )


MODEL_SELECT = """
SELECT m.id, m.model_code, m.display_name, m.description, m.kind, m.runtime,
       m.capabilities, m.priority, m.is_local, m.is_active,
       m.last_health_check, m.created_at, m.updated_at,
       v.id AS active_version_id, v.version, v.model_name, v.revision,
       v.parameters AS config, v.config_hash
FROM ai_models m
JOIN ai_model_versions v ON v.id = m.active_version_id
"""


class PostgresAiRepository:
    def model(self, code: str) -> dict | None:
        with postgres_connection() as conn:
            return _api(conn.execute(
                MODEL_SELECT + " WHERE m.model_code = %s", (code,)
            ).fetchone())

    def models(self, *, active_only: bool = False) -> list[dict]:
        query = MODEL_SELECT + (" WHERE m.is_active" if active_only else "")
        query += " ORDER BY m.priority, m.model_code"
        with postgres_connection() as conn:
            return [_api(row) for row in conn.execute(query).fetchall()]

    def model_versions(self, code: str) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute("""
                SELECT v.id, v.version, v.model_name, v.revision,
                       v.parameters AS config, v.config_hash, v.created_by_user_id,
                       v.created_at, (m.active_version_id = v.id) AS is_active
                FROM ai_models m JOIN ai_model_versions v ON v.model_id=m.id
                WHERE m.model_code=%s ORDER BY v.version DESC
            """, (code,)).fetchall()
        return [_api(row) for row in rows]

    def activate_model_version(self, code: str, version: int, *, actor_id=None) -> dict:
        with postgres_connection() as conn:
            with conn.transaction():
                model = conn.execute("""
                    SELECT id, active_version_id FROM ai_models WHERE model_code=%s FOR UPDATE
                """, (code,)).fetchone()
                if not model:
                    raise LookupError("Không tìm thấy model")
                target = conn.execute("""
                    SELECT id FROM ai_model_versions WHERE model_id=%s AND version=%s
                """, (model["id"], version)).fetchone()
                if not target:
                    raise LookupError("Không tìm thấy phiên bản model")
                conn.execute("""
                    UPDATE ai_models SET active_version_id=%s, updated_at=%s WHERE id=%s
                """, (target["id"], _now(), model["id"]))
                _audit(conn, actor_id, "ai_model.version_activate", "ai_model", model["id"],
                       {"active_version_id": model["active_version_id"]},
                       {"active_version_id": target["id"], "version": version})
        return self.model(code)

    def save_model(self, payload, *, actor_id=None, create: bool | None = None) -> dict:
        values = payload.model_dump()
        code = values["model_code"]
        config = values["config"]
        # A model config may name a runtime endpoint but never carry credentials.
        # Match whole secret-ish key names, not substrings (e.g. max_output_tokens).
        secret_keys = {"key", "api_key", "apikey", "token", "access_token", "password", "secret", "secret_key"}
        if any(key.lower() in secret_keys or key.lower().endswith(("_key", "_token", "_password", "_secret")) for key in config):
            raise ValueError("Cấu hình model không được chứa secret; hãy dùng biến môi trường")
        if config.get("endpoint") and config["endpoint"] != settings.ollama_generate_url:
            raise ValueError("Endpoint model phải dùng OLLAMA_GENERATE_URL của môi trường")
        digest = _hash({"model_name": values["model_name"], "revision": values["revision"], "config": config})
        now = _now()
        with postgres_connection() as conn:
            with conn.transaction():
                # Locks the logical code across competing admin requests, including insertion.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (code,))
                existing = conn.execute(
                        "SELECT id, active_version_id FROM ai_models WHERE model_code = %s FOR UPDATE", (code,)
                ).fetchone()
                if create is True and existing:
                    raise FileExistsError("Mã mô hình đã tồn tại")
                if create is False and not existing:
                    raise LookupError("Không tìm thấy mô hình")
                model_id = existing["id"] if existing else str(ObjectId())
                if not existing:
                    conn.execute("""
                        INSERT INTO ai_models
                        (id, model_code, display_name, description, kind, runtime, capabilities,
                         priority, is_local, is_active, created_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        model_id, code, values["display_name"], values["description"],
                        values["kind"], values["runtime"], Jsonb(values["capabilities"]), values["priority"],
                        values["is_local"], values["is_active"], now, now,
                    ))
                current = conn.execute("""
                    SELECT id, config_hash FROM ai_model_versions
                    WHERE model_id = %s ORDER BY version DESC LIMIT 1
                """, (model_id,)).fetchone()
                if current and current["config_hash"] == digest:
                    version_id = current["id"]
                else:
                    next_version = conn.execute(
                        "SELECT coalesce(max(version), 0) + 1 AS value FROM ai_model_versions WHERE model_id = %s",
                        (model_id,),
                    ).fetchone()["value"]
                    version_id = str(ObjectId())
                    conn.execute("""
                        INSERT INTO ai_model_versions
                        (id, model_id, version, model_name, revision, parameters,
                        endpoint_alias, config_hash, created_by_user_id, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (
                        version_id, model_id, next_version, values["model_name"],
                        values["revision"], Jsonb(config), config.get("endpoint"), digest,
                        str(actor_id) if actor_id else None, now,
                    ))
                conn.execute("""
                    UPDATE ai_models SET display_name=%s, description=%s, kind=%s, runtime=%s,
                        capabilities=%s, priority=%s, is_local=%s, is_active=%s,
                        active_version_id=%s, updated_at=%s WHERE id=%s
                """, (
                    values["display_name"], values["description"], values["kind"], values["runtime"],
                    Jsonb(values["capabilities"]), values["priority"], values["is_local"],
                    values["is_active"], version_id, now, model_id,
                ))
                _audit(conn, actor_id, "ai_model.save", "ai_model", model_id,
                       {"active_version_id": existing["active_version_id"] if existing else None},
                       {"active_version_id": version_id, "config_hash": digest})
        return self.model(code)

    def activate_model(self, code: str, active: bool, *, actor_id=None) -> dict:
        with postgres_connection() as conn:
            with conn.transaction():
                row = conn.execute("""
                    UPDATE ai_models SET is_active=%s, updated_at=%s
                    WHERE model_code=%s RETURNING id, is_active
                """, (active, _now(), code)).fetchone()
                if row:
                    _audit(conn, actor_id, "ai_model.activate", "ai_model", row["id"],
                           {}, {"is_active": active})
        if not row:
            raise LookupError("Không tìm thấy model")
        return self.model(code)

    def update_health(self, code: str, snapshot: dict) -> None:
        with postgres_connection() as conn:
            conn.execute("""
                UPDATE ai_models SET last_health_check=%s, updated_at=%s
                WHERE model_code=%s
            """, (Jsonb(snapshot), _now(), code))

    def prompts(self) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM prompt_templates ORDER BY template_key, version DESC
            """).fetchall()
        return [_api(row) for row in rows]

    def prompt(self, key: str, *, active_only: bool = False) -> dict | None:
        condition = " AND is_active" if active_only else ""
        with postgres_connection() as conn:
            row = conn.execute("""
                SELECT * FROM prompt_templates WHERE template_key=%s
            """ + condition + " ORDER BY version DESC LIMIT 1", (key,)).fetchone()
        return _api(row)

    def active_prompt_count(self) -> int:
        with postgres_connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM prompt_templates WHERE is_active"
            ).fetchone()["n"]

    def save_prompt(self, payload, *, actor_id=None) -> dict:
        now = _now()
        key = payload.template_key
        digest = hashlib.sha256(payload.prompt_body.encode("utf-8")).hexdigest()
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("prompt:" + key,))
                latest = conn.execute("""
                    SELECT * FROM prompt_templates WHERE template_key=%s
                    ORDER BY version DESC LIMIT 1 FOR UPDATE
                """, (key,)).fetchone()
                if latest and not payload.create_new_version:
                    if (latest["content_hash"] != digest or latest["kind"] != payload.kind
                            or latest["name"] != payload.name):
                        raise ValueError("Phiên bản prompt đã lưu là bất biến; hãy tạo phiên bản mới")
                    target_id = latest["id"]
                else:
                    target_id = str(ObjectId())
                    conn.execute("""
                        INSERT INTO prompt_templates
                        (id, template_key, version, kind, name, prompt_body, content_hash,
                         is_active, created_by_user_id, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,false,%s,%s)
                    """, (
                        target_id, key, (latest["version"] + 1 if latest else 1),
                        payload.kind, payload.name, payload.prompt_body, digest,
                        str(actor_id) if actor_id else None, now,
                    ))
                if payload.is_active:
                    conn.execute("UPDATE prompt_templates SET is_active=false WHERE template_key=%s", (key,))
                    conn.execute("UPDATE prompt_templates SET is_active=true WHERE id=%s", (target_id,))
                row = conn.execute("SELECT * FROM prompt_templates WHERE id=%s", (target_id,)).fetchone()
                _audit(conn, actor_id, "prompt.save", "prompt_template", target_id,
                       {}, {"template_key": key, "version": row["version"],
                            "content_hash": digest, "is_active": row["is_active"]})
        return _api(row)

    def activate_prompt(self, key: str, version: int, active: bool, *, actor_id=None) -> dict:
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("prompt:" + key,))
                row = conn.execute("""
                    SELECT * FROM prompt_templates WHERE template_key=%s AND version=%s FOR UPDATE
                """, (key, version)).fetchone()
                if not row:
                    raise LookupError("Không tìm thấy prompt version")
                if active:
                    conn.execute("UPDATE prompt_templates SET is_active=false WHERE template_key=%s", (key,))
                row = conn.execute("""
                    UPDATE prompt_templates SET is_active=%s WHERE id=%s RETURNING *
                """, (active, row["id"])).fetchone()
                _audit(conn, actor_id, "prompt.activate", "prompt_template", row["id"],
                       {}, {"template_key": key, "version": version, "is_active": active})
        return _api(row)

    def policies(self) -> list[dict]:
        with postgres_connection() as conn:
            rows = conn.execute("""
                SELECT * FROM evaluation_policies ORDER BY policy_name, version DESC
            """).fetchall()
        return [_api(row) for row in rows]

    def policy(self, *, active_only: bool = False) -> dict | None:
        condition = " WHERE is_active" if active_only else ""
        with postgres_connection() as conn:
            row = conn.execute("SELECT * FROM evaluation_policies" + condition +
                               " ORDER BY version DESC LIMIT 1").fetchone()
        return _api(row)

    def save_policy(self, payload, *, actor_id=None) -> dict:
        now = _now()
        name = payload.policy_name
        digest = _hash({"weights": payload.weights, "thresholds": payload.thresholds})
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('policy:global', 0))")
                latest = conn.execute("""
                    SELECT * FROM evaluation_policies WHERE policy_name=%s
                    ORDER BY version DESC LIMIT 1 FOR UPDATE
                """, (name,)).fetchone()
                if latest and not payload.create_new_version:
                    if latest["weights_hash"] != digest:
                        raise ValueError("Phiên bản policy đã lưu là bất biến; hãy tạo phiên bản mới")
                    target_id = latest["id"]
                else:
                    target_id = str(ObjectId())
                    change_reason = getattr(payload, "change_reason", "") or ""
                    conn.execute("""
                        INSERT INTO evaluation_policies
                        (id, policy_name, version, weights, thresholds, weights_hash,
                         change_reason, is_active, created_by_user_id, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,false,%s,%s)
                    """, (
                        target_id, name, (latest["version"] + 1 if latest else 1),
                        Jsonb(payload.weights), Jsonb(payload.thresholds), digest,
                        change_reason, str(actor_id) if actor_id else None, now,
                    ))
                if payload.is_active:
                    conn.execute("UPDATE evaluation_policies SET is_active=false WHERE is_active")
                    conn.execute("UPDATE evaluation_policies SET is_active=true WHERE id=%s", (target_id,))
                row = conn.execute("SELECT * FROM evaluation_policies WHERE id=%s", (target_id,)).fetchone()
                _audit(conn, actor_id, "evaluation_policy.save", "evaluation_policy", target_id,
                       {}, {"policy_name": name, "version": row["version"],
                            "weights_hash": digest, "change_reason": row.get("change_reason") or "",
                            "is_active": row["is_active"]})
        return _api(row)

    def activate_policy(self, name: str, version: int, active: bool, *, actor_id=None,
                        change_reason: str = "") -> dict:
        with postgres_connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('policy:global', 0))")
                row = conn.execute("""
                    SELECT * FROM evaluation_policies
                    WHERE policy_name=%s AND version=%s FOR UPDATE
                """, (name, version)).fetchone()
                if not row:
                    raise LookupError("Không tìm thấy policy version")
                if active:
                    conn.execute("UPDATE evaluation_policies SET is_active=false WHERE is_active")
                if change_reason:
                    row = conn.execute("""
                        UPDATE evaluation_policies
                        SET is_active=%s, change_reason=%s WHERE id=%s RETURNING *
                    """, (active, change_reason, row["id"])).fetchone()
                else:
                    row = conn.execute("""
                        UPDATE evaluation_policies SET is_active=%s WHERE id=%s RETURNING *
                    """, (active, row["id"])).fetchone()
                _audit(conn, actor_id, "evaluation_policy.activate", "evaluation_policy", row["id"],
                       {}, {"policy_name": name, "version": version, "is_active": active,
                            "change_reason": change_reason or row.get("change_reason") or ""})
        return _api(row)
