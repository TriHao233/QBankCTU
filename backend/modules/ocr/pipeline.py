import os
import re
import logging
import json
import gzip
import mimetypes
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any

from core.config import settings
from modules.documents.ingest import ParseContext, build_default_registry
from modules.documents.ingest.quality import validate_parsed_document
from modules.ocr.pdf_text_extractor import extract_pdf_text_layer
from modules.ocr.text_cleaner import clean_ocr_pages

logger = logging.getLogger(__name__)


class DocumentQualityError(ValueError):
    def __init__(self, report: dict):
        self.report = report
        message = "; ".join(report.get("errors") or []) or "Document extraction quality failed"
        super().__init__(message)

def remove_headers_footers(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Tự động phát hiện và xóa Header/Footer (tiêu đề đầu/cuối trang lặp lại).
    Dùng thuật toán đếm tần suất (Counter): Nếu một chuỗi xuất hiện ở đầu/cuối
    trên 30% số trang, nó sẽ bị coi là header/footer và bị xóa bỏ.
    """
    if len(pages) < 3:
        return pages

    first_prefixes = []
    last_prefixes = []
    max_prefix_len = 50

    for page in pages:
        lines = page["text"].strip().split("\n")
        if len(lines) >= 2:
            for line in lines[:3]:
                stripped = line.strip()
                if stripped:
                    first_prefixes.append(stripped[:max_prefix_len].strip())
            for line in lines[-3:]:
                stripped = line.strip()
                if stripped:
                    last_prefixes.append(stripped[:max_prefix_len].strip())

    threshold = max(3, len(pages) * 0.3)
    common_headers = {prefix for prefix, count in Counter(first_prefixes).items() if count >= threshold}
    common_footers = {prefix for prefix, count in Counter(last_prefixes).items() if count >= threshold}

    page_number_pattern = re.compile(r"^\s*\d+\s*$")
    cleaned_pages: list[dict[str, Any]] = []

    for page in pages:
        lines = page["text"].strip().split("\n")
        filtered_lines = []

        for i, line in enumerate(lines):
            line_stripped = line.strip()

            # Xử lý 3 dòng đầu (Header)
            if i < 3:
                skip = False
                modified_line = None
                for header in common_headers:
                    if line_stripped == header:
                        skip = True
                        break
                    if line_stripped.startswith(header) and len(line_stripped) > len(header) + 3:
                        modified_line = line_stripped[len(header) :].strip()
                        break
                if skip: continue
                if modified_line is not None:
                    filtered_lines.append(modified_line)
                    continue

            # Xử lý 3 dòng cuối (Footer)
            if i >= len(lines) - 3:
                skip_footer = False
                for footer in common_footers:
                    if line_stripped == footer or line_stripped.startswith(footer):
                        skip_footer = True
                        break
                if skip_footer: continue

            # Bỏ số trang đứng đơn độc
            if page_number_pattern.match(line_stripped): continue

            # Bỏ dòng bản quyền
            if any(keyword in line_stripped.lower() for keyword in ["bản quyền", "copyright", "©", "all rights reserved"]):
                continue

            filtered_lines.append(line)

        cleaned_pages.append({"page_number": page["page_number"], "text": "\n".join(filtered_lines)})

    return cleaned_pages


def save_markdown(pages: list[dict[str, Any]], output_path: str, document_title: str) -> None:
    """Lưu kết quả cuối cùng ra file Markdown để dùng cho Chunking hoặc người dùng đọc"""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as f:
        f.write(f"# OCR Result - {document_title}\n\n")
        f.write(f"**Tổng số trang:** {len(pages)}\n\n---\n\n")

        for page in pages:
            f.write(f"## Page {page['page_number']}\n\n")
            if page["text"].strip():
                f.write(page["text"] + "\n\n")
            else:
                f.write("*(Trang trống hoặc không có nội dung văn bản)*\n\n")

            formula_blocks = page.get("formula_blocks", [])
            if formula_blocks:
                f.write("### Formula Blocks\n\n")
                f.write("\n\n".join(formula_blocks) + "\n\n")

            f.write("---\n\n")


def _save_parsed_markdown(pages: list[dict[str, Any]], output_path: str, document_title: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as destination:
        destination.write(f"# Extraction Result - {document_title}\n\n")
        for unit in pages:
            location = (
                f"Page {unit['page_number']}"
                if unit.get("page_number") is not None
                else f"Source Unit {unit.get('unit_number', 1)}"
            )
            destination.write(f"## {location}\n\n")
            for block in unit.get("content_blocks") or []:
                content = block.get("content") or ""
                if not content:
                    continue
                destination.write(f"<!-- BLOCK:{block.get('block_type', 'prose')}:{block.get('block_id')} -->\n")
                destination.write(content)
                destination.write("\n\n")
            destination.write("---\n\n")


def run_document_pipeline(
    source_path: str,
    output_path: str,
    document_title: str | None = None,
    *,
    document_id: str = "standalone",
    source_file_name: str | None = None,
    source_uri: str | None = None,
    mime_type: str | None = None,
    pdf_ocr_enabled: bool = True,
) -> dict[str, Any]:
    pipeline_started_at = perf_counter()
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {source_path}")
    title = document_title or path.stem.replace("_", " ")
    resolved_mime = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    document_type = path.suffix.lower().lstrip(".") or "unknown"
    context = ParseContext(
        document_id=document_id,
        source_file_name=source_file_name or path.name,
        source_uri=source_uri or str(path),
        mime_type=resolved_mime,
        document_type=document_type,
    )
    logger.info("Starting structured extraction: %s (%s)", title, document_type)
    registry = build_default_registry(pdf_ocr_enabled=pdf_ocr_enabled)
    parser = registry.resolve(path, resolved_mime)
    parsed = parser.parse(path, context)
    report = validate_parsed_document(parsed, min_text_unit_ratio=settings.document_min_text_unit_ratio)
    if not report.passed:
        raise DocumentQualityError(report.to_dict())

    pages = parsed.to_page_records()
    total_chars = sum(len(page.get("text") or "") for page in pages)
    write_started_at = perf_counter()
    _save_parsed_markdown(pages, output_path, title)
    markdown_ms = (perf_counter() - write_started_at) * 1000

    raw_write_started_at = perf_counter()
    raw_payload = json.dumps(parsed.model_dump(mode="json"), ensure_ascii=False, indent=2).encode("utf-8")
    if settings.raw_artifact_compression == "gzip":
        raw_path = Path(output_path).with_suffix(".raw.json.gz")
        with gzip.open(raw_path, "wb", compresslevel=6) as destination:
            destination.write(raw_payload)
        raw_mime_type = "application/gzip"
    elif settings.raw_artifact_compression in {"", "none"}:
        raw_path = Path(output_path).with_suffix(".raw.json")
        raw_path.write_bytes(raw_payload)
        raw_mime_type = "application/json"
    else:
        raise ValueError("RAW_ARTIFACT_COMPRESSION must be 'gzip' or 'none'")
    raw_write_ms = (perf_counter() - raw_write_started_at) * 1000
    raw_physical_bytes = raw_path.stat().st_size
    timings_ms = {
        "markdown_write": round(markdown_ms, 2),
        "raw_serialize_compress_write": round(raw_write_ms, 2),
        "pipeline_total": round((perf_counter() - pipeline_started_at) * 1000, 2),
    }
    stats_data = {
        **parsed.stats,
        "total_pages": sum(page.get("page_number") is not None for page in pages),
        "total_units": len(pages),
        "total_chars": total_chars,
        "avg_chars_per_page": round(total_chars / max(len(pages), 1)),
        "parser": type(parser).__name__,
        "storage": {
            "raw_uncompressed_bytes": len(raw_payload),
            "raw_physical_bytes": raw_physical_bytes,
            "raw_compression": settings.raw_artifact_compression or "none",
            "raw_compression_ratio": round(raw_physical_bytes / max(len(raw_payload), 1), 6),
        },
        "quality": report.to_dict(),
        "timings_ms": timings_ms,
    }
    return {
        "pages": pages,
        "assets": [asset.model_dump(mode="json") for asset in parsed.assets],
        "output_file": output_path,
        "raw_extraction_file": str(raw_path),
        "raw_extraction_mime_type": raw_mime_type,
        "stats": stats_data,
    }


def run_ocr_pipeline(
    pdf_path: str,
    output_path: str,
    document_title: str | None = None,
    languages: list[str] | None = None,
    gpu: bool | None = None,
    document_id: str = "standalone",
    source_file_name: str | None = None,
    source_uri: str | None = None,
    mime_type: str | None = None,
) -> dict[str, Any]:
    return run_document_pipeline(
        source_path=pdf_path,
        output_path=output_path,
        document_title=document_title,
        document_id=document_id,
        source_file_name=source_file_name,
        source_uri=source_uri,
        mime_type=mime_type,
    )
