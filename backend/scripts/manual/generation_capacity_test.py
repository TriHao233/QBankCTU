"""Queue one mixed-format generation job and report throughput.

Run inside the backend container while the worker is up, e.g.:
    python scripts/manual/generation_capacity_test.py --document-id <id> --user-id <id> --total 50

The job goes through the normal worker path (lease, retries, generation runs).
Generated questions stay attached to the job result owned by ``--user-id``;
nothing enters the question bank until a teacher submits them.
"""

import argparse
import itertools
import json
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

from core.config import settings  # noqa: E402
from modules.generation.llm.model_registry import GENERATION_CAPABILITY, resolve_model_snapshot  # noqa: E402
from modules.generation.mongodb import create_generation_job, get_generation_job  # noqa: E402
from modules.generation.schemas import (  # noqa: E402
    QUESTION_TYPE_BLOOM_LEVELS,
    BloomLevel,
    QuestionGenerateRequest,
    QuestionType,
)

BLOOMS_BY_DIFFICULTY = {
    "de": (BloomLevel.NHO, BloomLevel.HIEU),
    "trung_binh": (BloomLevel.VAN_DUNG, BloomLevel.PHAN_TICH),
    "kho": (BloomLevel.DANH_GIA, BloomLevel.SANG_TAO),
}


def _bloom_rank(level: BloomLevel) -> int:
    return int(level.value.split("_", 1)[0])


def allowed_bloom(question_type: QuestionType, preferred: BloomLevel) -> BloomLevel:
    """Closest Bloom level the question type accepts."""
    allowed = QUESTION_TYPE_BLOOM_LEVELS[question_type]
    return min(allowed, key=lambda level: (abs(_bloom_rank(level) - _bloom_rank(preferred)), _bloom_rank(level)))


def mixed_plan(total: int, types: list[QuestionType] | None = None) -> list[dict]:
    """Spread ``total`` questions over every format x difficulty, alternating Bloom levels."""
    combos = list(itertools.product(types or list(QuestionType), BLOOMS_BY_DIFFICULTY))
    base, extra = divmod(total, len(combos))
    plan = []
    for index, (question_type, difficulty) in enumerate(combos):
        count = base + (1 if index < extra else 0)
        if not count:
            continue
        bloom = allowed_bloom(question_type, BLOOMS_BY_DIFFICULTY[difficulty][index % 2])
        plan.append({
            "question_type": question_type.value,
            "bloom_level": bloom.value,
            "difficulty": difficulty,
            "num_questions": count,
        })
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--total", type=int, default=7)
    parser.add_argument("--model", default=settings.model_provider)
    parser.add_argument("--poll-seconds", type=float, default=30)
    parser.add_argument("--types", help="Comma-separated question types; defaults to every format.")
    args = parser.parse_args()
    types = [QuestionType(value.strip()) for value in args.types.split(",")] if args.types else None

    request = QuestionGenerateRequest(
        document_id=args.document_id,
        bloom_level=BloomLevel.HIEU,
        model_provider=args.model,
        code_model_provider=args.model,
        question_plan=mixed_plan(args.total, types),
    )
    snapshot = resolve_model_snapshot(args.model, capability=GENERATION_CAPABILITY)
    job_id = create_generation_job(
        request.model_dump(mode="json"),
        args.user_id,
        None,
        model_snapshot=snapshot,
        code_model_snapshot=snapshot,
        fallback_model_snapshot=None,
    )
    print(f"job={job_id} total={args.total} model={args.model}", flush=True)
    for item in request.question_plan:
        print(f"  {item.question_type.value:15s} {item.bloom_level.value:12s} {item.difficulty:10s} x{item.num_questions}")

    started = time.perf_counter()
    last = None
    while True:
        job = get_generation_job(job_id) or {}
        status = str(job.get("status") or "").lower()
        progress = job.get("progress") or {}
        line = f"{status} {progress.get('completed', 0)}/{progress.get('total', args.total)} " \
               f"generated={progress.get('generated_questions', 0)}"
        if line != last:
            print(f"[{(time.perf_counter() - started) / 60:6.1f} min] {line}", flush=True)
            last = line
        if status in {"completed", "failed", "dead_letter", "cancelled"}:
            break
        time.sleep(args.poll_seconds)

    elapsed = time.perf_counter() - started
    result = job.get("result") or {}
    data = result.get("data") or []
    print(f"\nstatus={status} elapsed={elapsed / 60:.1f} min generated={len(data)}/{args.total}")
    if data:
        print(f"seconds per accepted question: {elapsed / len(data):.0f}")
    print("by type:", dict(Counter(item.get("question_type") for item in data)))
    print("by difficulty:", dict(Counter(item.get("difficulty") for item in data)))
    print("by bloom:", dict(Counter(item.get("bloom_level") for item in data)))
    for summary in result.get("summary") or []:
        print(json.dumps({key: summary.get(key) for key in (
            "question_type", "bloom_level", "difficulty", "requested_count", "valid_count", "saved_count",
            "duplicate_count", "format_rejected_count", "grounding_rejected_count", "clarity_rejected_count",
            "retry_attempt_count", "warnings",
        )}, ensure_ascii=False))
    if job.get("error_message"):
        print("error:", job["error_message"])
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
