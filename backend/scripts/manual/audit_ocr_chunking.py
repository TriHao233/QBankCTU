"""Measure production extraction/splitting/embedding without writing Mongo lineage.

Use a NEW output directory. Main DB, uploads and Chroma are never modified.
The source is read-only; generated audit outputs may be deleted after review.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-docling", type=Path, help="Reuse Docling output from a same-SHA audit run; clearly labeled replay")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    replayed = {}
    if args.replay_docling:
        baseline = args.replay_docling.resolve(strict=True)
        baseline_summary = json.loads((baseline / "summary.json").read_text(encoding="utf-8"))
        if baseline_summary["source_sha256"] != source_sha256:
            raise ValueError("Replay source SHA-256 mismatch")
        with gzip.open(baseline / "extraction.raw.json.gz", "rt", encoding="utf-8") as handle:
            baseline_raw = json.load(handle)
        for unit in baseline_raw["units"]:
            cached = unit.get("raw_extraction", {}).get("docling")
            if cached:
                replayed[unit["page_number"]] = {
                    **cached, "raw_document": baseline_raw.get("raw_engine_outputs", {}).get("docling"),
                }
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    # Set before importing modules whose settings are constructed at import time.
    os.environ["CHROMADB_PATH"] = str(output / "chroma")
    os.environ["METADATA_DIR"] = str(output / "metadata")
    os.environ["CHUNK_OUTPUT_DIR"] = str(output / "chunks")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

    from core.config import settings
    from modules.ocr.pipeline import run_document_pipeline
    from modules.documents.ingest.quality import validate_chunks
    from modules.rag.chunking import _structured_chunks
    from modules.rag.chunking_export import export_chunks_to_file
    from modules.rag.chromadb_engine import get_chroma_client, store_chunks
    from modules.rag import chromadb_engine

    started = perf_counter()
    document_id = "audit-ocr-chunking"
    print(f"[OCR] Starting {source.name}", flush=True)
    with ExitStack() as stack:
        if args.replay_docling:
            def cached_docling(_source, page_numbers):
                if any(number not in replayed for number in page_numbers):
                    raise ValueError("Replay lacks a requested Docling page")
                return {number: replayed[number] for number in page_numbers}

            stack.enter_context(patch("modules.ocr.docling_engine.ocr_pdf_pages", side_effect=cached_docling))
            print("[REPLAY] Docling response reused; PDF extraction and embedding execute normally", flush=True)
        result = run_document_pipeline(
            str(source), str(output / "extraction.md"), source.stem,
            document_id=document_id, source_uri=str(source),
        )
    ocr_seconds = perf_counter() - started
    pages = result["pages"]
    print(f"[OCR] {len(pages)} pages in {ocr_seconds:.3f}s", flush=True)
    split_started = perf_counter()
    document = {
        "_id": document_id, "original_filename": source.name,
        "artifacts": [{"type": "ORIGINAL_PDF", "storage": {"uri": str(source)}}],
    }
    retrieval_metrics: dict = {}
    chunks = _structured_chunks(
        document, pages, document_id, settings.chunk_size_default,
        settings.chunk_overlap_default, settings.max_code_block_lines, [], settings.embedding_max_tokens,
        retrieval_metrics=retrieval_metrics,
    )
    quality = validate_chunks(chunks)
    if not quality.passed or not chunks:
        raise RuntimeError("Chunk quality gate failed; indexing aborted")
    split_seconds = perf_counter() - split_started
    print(f"[CHUNKING] {len(chunks)} chunks in {split_seconds:.3f}s; {quality.status}", flush=True)
    export_chunks_to_file(document_id, chunks)
    embedding_metrics: dict = {}
    observed_inputs: list[str] = []
    original_loader = chromadb_engine._get_embedding_model
    hooked = set()

    def observe_model():
        model = original_loader()
        if id(model) not in hooked:
            original_encode = model.encode

            def observe_encode(sentences, *positional, **kwargs):
                observed_inputs.extend(sentences)
                return original_encode(sentences, *positional, **kwargs)

            model.encode = observe_encode
            hooked.add(id(model))
        return model

    with patch.object(chromadb_engine, "_get_embedding_model", side_effect=observe_model):
        stored = store_chunks(
            [chunk["chunk_id"] for chunk in chunks], [chunk["content"] for chunk in chunks],
            [chunk["metadata"] for chunk in chunks], "audit-ocr-chunking", metrics=embedding_metrics,
        )
    (output / "embedding-inputs.json").write_text(
        json.dumps(observed_inputs, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    collection = get_chroma_client().get_collection("audit-ocr-chunking")
    indexed = collection.count()
    assert stored == indexed == len(chunks)
    indexed_documents = collection.get(include=["documents"])
    assert dict(zip(indexed_documents["ids"], indexed_documents["documents"])) == {
        chunk["chunk_id"]: chunk["content"] for chunk in chunks
    }
    assert set(observed_inputs) == {chunk["content"] for chunk in chunks}  # fresh isolated cache
    chunk_seconds = perf_counter() - split_started
    contents = Counter(chunk["content"] for chunk in chunks)
    summary = {
        "source_sha256": source_sha256,
        "docling_replayed": bool(args.replay_docling),
        "pages": len(pages), "chunks": len(chunks), "vectors": indexed,
        "chunk_characters": {
            "min": min(len(chunk["content"]) for chunk in chunks),
            "average": round(sum(len(chunk["content"]) for chunk in chunks) / len(chunks), 2),
            "max": max(len(chunk["content"]) for chunk in chunks),
        },
        "retrieval_filter": retrieval_metrics,
        "embedding_input_exact_match": True,
        "embedding_input_count": len(observed_inputs),
        "configuration": {
            "chunk_size": settings.chunk_size_default, "chunk_overlap": settings.chunk_overlap_default,
            "embedding_max_tokens": settings.embedding_max_tokens,
            "embedding_model": settings.embedding_model_name,
        },
        "ocr_seconds": round(ocr_seconds, 3),
        "ocr_seconds_per_page": round(ocr_seconds / len(pages), 3),
        "split_seconds": round(split_seconds, 3),
        "chunk_seconds": round(chunk_seconds, 3),
        "chunk_seconds_per_page": round(chunk_seconds / len(pages), 3),
        "total_seconds": round(perf_counter() - started, 3),
        "duplicates": sum(count - 1 for count in contents.values()),
        "long_whitespace_chunks": sum(bool(re.search(r"[ \t]{20,}", text)) for text in contents.elements()),
        "short_chunks_under_120": sum(len(chunk["content"]) < 120 for chunk in chunks),
        "over_chunk_size": sum(len(chunk["content"]) > settings.chunk_size_default for chunk in chunks),
        "empty_chunks": sum(not chunk["content"].strip() for chunk in chunks),
        "broken_fences": sum(chunk["content"].count("```") % 2 for chunk in chunks),
        "empty_pages": sum(not (page.get("text") or "").strip() for page in pages),
        "replacement_characters": sum((page.get("text") or "").count("\ufffd") for page in pages),
        "page_refs": sorted({p for c in chunks for p in c["metadata"]["page_marks"]}),
        "ocr_stats": result["stats"], "chunk_quality": quality.to_dict(),
        "embedding_metrics": embedding_metrics,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[VERIFY] " + json.dumps({k: v for k, v in summary.items() if k not in {
        "ocr_stats", "chunk_quality", "embedding_metrics", "retrieval_filter"
    }}), flush=True)
    print(f"[PIPELINE] Complete; summary: {output / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
