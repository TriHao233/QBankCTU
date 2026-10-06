"""Read-only lookup of question versions that cite given chunks as evidence."""

from bson import ObjectId

from core.config import settings
from core.postgres import postgres_connection

__all__ = ["count_versions_citing_chunks"]


def count_versions_citing_chunks(database, chunk_ids) -> int:
    keys = list(dict.fromkeys(str(item) for item in chunk_ids or () if item))
    if not keys:
        return 0
    if settings.question_store == "postgres":
        with postgres_connection() as conn:
            row = conn.execute(
                """SELECT count(*) AS total FROM question_versions v
                   WHERE EXISTS (SELECT 1 FROM jsonb_array_elements(v.sources) s
                                 WHERE s->>'chunk_id' = ANY(%s))""",
                (keys,),
            ).fetchone()
        return int(row["total"] or 0)
    oids = [ObjectId(item) for item in keys if ObjectId.is_valid(item)]
    return database.question_versions.count_documents({"sources.chunk_id": {"$in": [*oids, *keys]}})
