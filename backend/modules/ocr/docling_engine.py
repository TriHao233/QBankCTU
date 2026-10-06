"""
Docling OCR Engine — gọi Docling container qua HTTP API.

Engine OCR duy nhất cho PDF, không có fallback. Output: Markdown có cấu trúc
(heading, table, code block, formula).
"""

import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any

import requests
from pypdf import PdfReader, PdfWriter

from core.config import settings

logger = logging.getLogger(__name__)

SUPPORTED_RAPIDOCR_BACKENDS = {"onnxruntime", "openvino", "paddle", "torch"}


def is_docling_available() -> bool:
    """Kiểm tra Docling service có hoạt động."""
    try:
        resp = requests.get(f"{settings.docling_url}/health", timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def _ocr_form_data() -> tuple[list[tuple[str, str]], str]:
    """Build a validated Docling OCR request without combining conflicting fields."""

    preset = settings.docling_ocr_preset or "auto"
    backend = settings.docling_ocr_backend or "onnxruntime"
    languages = list(settings.docling_ocr_languages)

    if preset != "rapidocr" or backend == "onnxruntime":
        fields = [("ocr_preset", preset)]
        fields.extend(("ocr_lang", language) for language in languages)
        return fields, backend

    if backend not in SUPPORTED_RAPIDOCR_BACKENDS:
        raise ValueError(f"Unsupported RapidOCR backend: {backend}")

    custom_config: dict[str, Any] = {"kind": "rapidocr", "backend": backend}
    if languages:
        custom_config["lang"] = languages
    return [("ocr_custom_config", json.dumps(custom_config, ensure_ascii=True))], backend


def ocr_pdf(file_path: str) -> dict[str, Any]:
    """
    Gửi PDF tới Docling qua API bất đồng bộ, chờ kết quả và nhận về Markdown có cấu trúc.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File không tồn tại: {file_path}")

    started_at = time.perf_counter()
    logger.info("Gửi tài liệu tới Docling Async: %s", path.name)

    ocr_fields, requested_ocr_backend = _ocr_form_data()
    form_data: list[tuple[str, str]] = [
        ("to_formats", "md"),
        ("to_formats", "json"),
        ("do_ocr", "true"),
        ("force_ocr", "false"),
        ("images_scale", str(settings.docling_images_scale)),
        ("table_mode", settings.docling_table_mode),
        ("do_table_structure", str(settings.docling_do_table_structure).lower()),
        ("include_images", str(settings.docling_include_images).lower()),
    ]
    form_data.extend(ocr_fields)

    submit_started_at = time.perf_counter()
    with open(file_path, "rb") as f:
        response = requests.post(
            f"{settings.docling_url}/v1/convert/file/async",
            files={"files": (path.name, f, "application/pdf")},
            data=form_data,
            timeout=settings.docling_timeout,
        )
    response.raise_for_status()
    submit_ms = (time.perf_counter() - submit_started_at) * 1000
    task_id = response.json().get("task_id")
    if not task_id:
        raise RuntimeError("Không nhận được task_id từ Docling")

    logger.info("Docling Task ID: %s. Đang chờ xử lý...", task_id)

    status_url = f"{settings.docling_url}/v1/status/poll/{task_id}"
    poll_count = 0
    wait_started_at = time.perf_counter()
    deadline = wait_started_at + settings.docling_timeout
    while True:
        if time.perf_counter() >= deadline:
            raise TimeoutError("Docling job exceeded the configured timeout")
        poll_resp = requests.get(status_url, timeout=10)
        poll_resp.raise_for_status()
        poll_count += 1
        status_data = poll_resp.json()
        status = status_data.get("task_status")

        if status in {"success", "partial_success"}:
            break
        if status in {"failure", "skipped"}:
            logger.error("Docling task %s failed with status %s", task_id, status)
            raise RuntimeError(f"Docling job failed with status: {status}")

        time.sleep(max(settings.docling_poll_seconds, 0.1))

    wait_ms = (time.perf_counter() - wait_started_at) * 1000

    logger.info("Docling Task %s hoàn tất. Đang tải kết quả...", task_id)
    result_started_at = time.perf_counter()
    result_resp = requests.get(f"{settings.docling_url}/v1/result/{task_id}", timeout=60)
    result_resp.raise_for_status()
    result = result_resp.json()
    result_ms = (time.perf_counter() - result_started_at) * 1000

    document = result.get("document", {})
    markdown = document.get("md_content") or document.get("text_content") or ""

    pages = result.get("pages") or document.get("pages") or _pages_from_docling_json(document)
    if not pages:
        pages = _split_markdown_to_pages(markdown)

    logger.info(
        "Docling hoàn tất: %d trang, %d ký tự",
        len(pages),
        len(markdown),
    )

    return {
        "markdown": markdown,
        "pages": pages,
        "metadata": result.get("metadata") or {},
        "timings_ms": {
            "docling_submit": round(submit_ms, 2),
            "docling_wait": round(wait_ms, 2),
            "docling_result_download": round(result_ms, 2),
            "docling_total": round((time.perf_counter() - started_at) * 1000, 2),
        },
        "diagnostics": {
            "poll_count": poll_count,
            "task_status": status,
            "server_processing_seconds": result.get("processing_time"),
            "server_timings": result.get("timings") or {},
            "ocr_preset": settings.docling_ocr_preset,
            "ocr_backend": requested_ocr_backend,
            "page_count": len(pages),
        },
        "raw_document": document,
        "structured_blocks": _blocks_from_docling_json(document),
    }


def _load_docling_json(document: dict[str, Any]) -> dict[str, Any] | None:
    raw = document.get("json_content") or document.get("json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _table_to_markdown(item: dict[str, Any]) -> str:
    data = item.get("data") or {}
    cells = data.get("table_cells") or []
    if not cells:
        return str(item.get("text") or "").strip()

    row_count = max(
        (int(cell.get("end_row_offset_idx", cell.get("start_row_offset_idx", 0))) for cell in cells),
        default=0,
    )
    col_count = max(
        (int(cell.get("end_col_offset_idx", cell.get("start_col_offset_idx", 0))) for cell in cells),
        default=0,
    )
    if row_count <= 0 or col_count <= 0:
        return str(item.get("text") or "").strip()

    grid = [["" for _ in range(col_count)] for _ in range(row_count)]
    for cell in cells:
        row = int(cell.get("start_row_offset_idx", 0))
        col = int(cell.get("start_col_offset_idx", 0))
        if 0 <= row < row_count and 0 <= col < col_count:
            grid[row][col] = str(cell.get("text") or "").replace("|", "\\|").strip()
    header = "| " + " | ".join(grid[0]) + " |"
    separator = "| " + " | ".join("---" for _ in range(col_count)) + " |"
    body = ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join([header, separator, *body])


def _render_docling_item(item: dict[str, Any]) -> str:
    label = str(item.get("label") or "").lower()
    text = str(item.get("text") or "").strip()
    if label == "table":
        return _table_to_markdown(item)
    if not text:
        return ""
    if label == "title":
        return f"# {text}"
    if label in {"section_header", "heading"}:
        level = min(max(int(item.get("level") or 2), 1), 3)
        return f"{'#' * level} {text}"
    if label == "list_item":
        return f"- {text}"
    if label == "code":
        return f"```\n{text}\n```"
    if label in {"formula", "equation"}:
        return f"$$\n{text}\n$$"
    return text


def _rendered_provenance_parts(item: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Split Docling text items that span pages using their source char spans."""

    provenance = [entry for entry in item.get("prov") or [] if isinstance(entry, dict)]
    if not provenance:
        return [({}, _render_docling_item(item))]
    source_text = str(item.get("text") or item.get("orig") or "")
    if len(provenance) == 1 or not source_text:
        return [(provenance[0], _render_docling_item(item))]

    parts: list[tuple[dict[str, Any], str]] = []
    for entry in provenance:
        span = entry.get("charspan")
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            continue
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError):
            continue
        if start < 0 or end <= start or end > len(source_text):
            continue
        sliced = dict(item)
        sliced["text"] = source_text[start:end]
        sliced["orig"] = source_text[start:end]
        rendered = _render_docling_item(sliced)
        if rendered:
            parts.append((entry, rendered))
    return parts or [(provenance[0], _render_docling_item(item))]


def _blocks_from_docling_json(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return lossless-enough typed block descriptors from Docling JSON.

    The original JSON remains authoritative in ``raw_document``. These records
    only provide a stable adapter boundary for downstream parsing.
    """
    payload = _load_docling_json(document)
    if not payload:
        return []
    blocks: list[dict[str, Any]] = []
    seen_refs: set[str] = set()

    def resolve_ref(reference: str) -> Any:
        current: Any = payload
        for token in reference.removeprefix("#/").split("/"):
            if not token:
                continue
            token = token.replace("~1", "/").replace("~0", "~")
            try:
                current = current[int(token)] if isinstance(current, list) else current[token]
            except (KeyError, IndexError, TypeError, ValueError):
                return None
        return current

    def visit(raw_item: Any) -> None:
        if not isinstance(raw_item, dict):
            return
        reference = raw_item.get("$ref")
        if isinstance(reference, str):
            if reference in seen_refs:
                return
            seen_refs.add(reference)
            visit(resolve_ref(reference))
            return
        children = raw_item.get("children") or []
        if children:
            for child in children:
                visit(child)
            return
        label = str(raw_item.get("label") or "text").lower()
        for provenance, rendered in _rendered_provenance_parts(raw_item):
            if not rendered and label not in {"picture", "image"}:
                continue
            blocks.append(
                {
                    "block_type": {
                        "title": "heading",
                        "section_header": "heading",
                        "heading": "heading",
                        "list_item": "list",
                        "table": "table",
                        "code": "code",
                        "formula": "formula",
                        "equation": "formula",
                        "picture": "image",
                        "image": "image",
                        "caption": "caption",
                    }.get(label, "prose"),
                    "content": rendered,
                    "page_number": int(provenance.get("page_no") or 1),
                    "bbox": provenance.get("bbox"),
                    "label": label,
                    "structured_content": (
                        raw_item.get("data")
                        if label == "table"
                        else {"raw": rendered, "source_format": label}
                        if label in {"formula", "equation"}
                        else None
                    ),
                }
            )

    visit(payload.get("body") or {})
    return blocks


def ocr_pdf_pages(file_path: str, page_numbers: list[int]) -> dict[int, dict[str, Any]]:
    """Run Docling only for selected source pages and remap provenance.

    This prevents a single scanned/image page from forcing OCR over every good
    text-layer page in a mixed PDF.
    """
    if not page_numbers:
        return {}
    reader = PdfReader(file_path, strict=False)
    selected = sorted(set(page_numbers))
    if selected[0] < 1 or selected[-1] > len(reader.pages):
        raise ValueError("Selected OCR page is outside the PDF page range")
    writer = PdfWriter()
    for page_number in selected:
        writer.add_page(reader.pages[page_number - 1])
    handle = tempfile.NamedTemporaryFile(prefix="qbank-ocr-pages-", suffix=".pdf", delete=False)
    temp_path = Path(handle.name)
    try:
        with handle:
            writer.write(handle)
        result = ocr_pdf(str(temp_path))
    finally:
        try:
            os.remove(temp_path)
        except FileNotFoundError:
            pass

    relative_pages = {
        int(page.get("page_number", index)): page
        for index, page in enumerate(result.get("pages") or [], start=1)
        if isinstance(page, dict)
    }
    relative_blocks: dict[int, list[dict]] = {}
    for block in result.get("structured_blocks") or []:
        relative_blocks.setdefault(int(block.get("page_number") or 1), []).append(block)

    mapped: dict[int, dict[str, Any]] = {}
    for relative_number, original_number in enumerate(selected, start=1):
        page = relative_pages.get(relative_number) or {}
        blocks = []
        for block in relative_blocks.get(relative_number, []):
            mapped_block = dict(block)
            mapped_block["page_number"] = original_number
            blocks.append(mapped_block)
        mapped[original_number] = {
            "text": page.get("text") or page.get("content") or "",
            "original_text": page.get("original_text") or page.get("text") or "",
            "formula_blocks": page.get("formula_blocks") or [],
            "structured_blocks": blocks,
            "diagnostics": result.get("diagnostics") or {},
        }
    if selected and result.get("raw_document"):
        mapped[selected[0]]["raw_document"] = result["raw_document"]
    return mapped


def _pages_from_docling_json(document: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _load_docling_json(document)
    if not payload:
        return []

    page_parts: dict[int, list[str]] = {}
    seen_refs: set[str] = set()

    def resolve_ref(reference: str) -> Any:
        current: Any = payload
        for token in reference.removeprefix("#/").split("/"):
            if not token:
                continue
            token = token.replace("~1", "/").replace("~0", "~")
            try:
                current = current[int(token)] if isinstance(current, list) else current[token]
            except (KeyError, IndexError, TypeError, ValueError):
                return None
        return current

    def visit(raw_item: Any) -> None:
        if not isinstance(raw_item, dict):
            return
        reference = raw_item.get("$ref")
        if isinstance(reference, str):
            if reference in seen_refs:
                return
            seen_refs.add(reference)
            visit(resolve_ref(reference))
            return

        children = raw_item.get("children") or []
        if children:
            for child in children:
                visit(child)
            return

        for provenance, rendered in _rendered_provenance_parts(raw_item):
            if not rendered:
                continue
            page_number = int(provenance.get("page_no") or 1)
            page_parts.setdefault(max(page_number, 1), []).append(rendered)

    visit(payload.get("body") or {})
    if not page_parts:
        fallback_items: list[dict[str, Any]] = []
        for collection_name in ("texts", "tables", "key_value_items"):
            values = payload.get(collection_name) or []
            if isinstance(values, list):
                fallback_items.extend(value for value in values if isinstance(value, dict))
        fallback_items.sort(
            key=lambda item: (
                int(((item.get("prov") or [{}])[0]).get("page_no") or 1),
                -float((((item.get("prov") or [{}])[0]).get("bbox") or {}).get("t") or 0),
            )
        )
        for item in fallback_items:
            visit(item)

    return [
        {
            "page_number": page_number,
            "text": "\n\n".join(parts).strip(),
            "original_text": "\n\n".join(parts).strip(),
            "formula_blocks": [],
        }
        for page_number, parts in sorted(page_parts.items())
    ]


def _split_markdown_to_pages(markdown: str) -> list[dict[str, Any]]:
    """
    Nếu Docling trả markdown nguyên khối (không chia trang),
    tạo 1 page duy nhất chứa toàn bộ nội dung.
    """
    if not markdown.strip():
        return []
    return [
        {
            "page_number": 1,
            "text": markdown.strip(),
            "original_text": markdown.strip(),
            "formula_blocks": [],
        }
    ]
