from __future__ import annotations

import re
import hashlib
import math
import unicodedata
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from modules.documents.ingest.base import DocumentParser
from modules.documents.ingest.models import (
    Asset,
    ContentBlock,
    DocumentUnit,
    ParsedDocument,
    ParseContext,
    SourceProvenance,
    stable_asset_id,
)
from modules.documents.ingest.parsers.common import make_block
from modules.documents.ingest.quality import text_quality_metrics, text_quality_score


SPACE_WIDTH_CANDIDATES = (200.0, 500.0, 800.0, 1200.0, 2000.0)
CODE_PREFIXES = (
    "#include",
    "#define",
    "typedef ",
    "struct ",
    "class ",
    "void ",
    "int ",
    "char ",
    "float ",
    "double ",
    "return ",
    "for ",
    "while ",
    "if ",
    "else",
    "//",
    "/*",
    "* ",
)
LIST_PATTERN = re.compile(r"^\s*(?:[-+•]|\d+[.)]|[a-zA-Z][.)])\s+")
HEADING_PATTERN = re.compile(r"^\s*(?:CHƯƠNG|CHUONG|PHẦN|PHAN|BÀI|BAI)\s+(?:[IVXLCDM]+|\d+)\b", re.IGNORECASE)


def _resolve(value: Any) -> Any:
    try:
        return value.get_object()
    except (AttributeError, TypeError):
        return value


def _bbox_list(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return [float(item) for item in value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, dict):
        keys = ("l", "b", "r", "t")
        if all(key in value for key in keys):
            try:
                return [float(value[key]) for key in keys]
            except (TypeError, ValueError):
                return None
    return None


def _image_objects(resources: Any, prefix: str = "") -> list[tuple[str, Any]]:
    resolved = _resolve(resources)
    if not resolved or not hasattr(resolved, "get"):
        return []
    xobjects = _resolve(resolved.get("/XObject"))
    if not xobjects or not hasattr(xobjects, "items"):
        return []
    images: list[tuple[str, Any]] = []
    for name, raw_object in xobjects.items():
        obj = _resolve(raw_object)
        if not obj or not hasattr(obj, "get"):
            continue
        location = f"{prefix}/{name}" if prefix else str(name)
        subtype = str(obj.get("/Subtype") or "")
        if subtype == "/Image":
            images.append((location, obj))
        elif subtype == "/Form":
            images.extend(_image_objects(obj.get("/Resources"), location))
    return images


def _candidate_texts(page: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for space_width in SPACE_WIDTH_CANDIDATES:
        try:
            text = (page.extract_text(extraction_mode="plain", space_width=space_width) or "").strip()
        except Exception:
            continue
        if text in seen:
            continue
        seen.add(text)
        candidates.append(
            {
                "method": "pypdf_plain",
                "space_width": space_width,
                "text": text,
                "quality": text_quality_metrics(text),
                "score": text_quality_score(text),
            }
        )
    try:
        layout = (page.extract_text(extraction_mode="layout") or "").strip()
    except Exception:
        layout = ""
    if layout not in seen:
        candidates.append(
            {
                "method": "pypdf_layout",
                "space_width": None,
                "text": layout,
                "quality": text_quality_metrics(layout),
                "score": text_quality_score(layout),
            }
        )
    return candidates


def _looks_like_code(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(CODE_PREFIXES) or stripped in {"{", "}", "};"}:
        return True
    if stripped.endswith(";") and re.search(r"[A-Za-z_]\w*\s*(?:\(|=|\[)", stripped):
        return True
    if stripped.endswith(";") and re.match(
        r"^(?:unsigned\s+|const\s+|static\s+)?(?:int|float|double|char|long|short|void|Node|Position|ElementType|vertex)\b",
        stripped,
    ):
        return True
    return bool(re.match(r"^(?:do|switch|case)\b", stripped))


def _looks_like_formula(line: str) -> bool:
    stripped = line.strip()
    if any(marker in stripped for marker in (";", "->", "==", "!=", "++", "--", "{", "}")):
        return False
    if len(stripped) > 240 or not re.search(r"[=∑√≤≥±×÷^]", stripped):
        return False
    words = re.findall(r"[^\W_]+", stripped, flags=re.UNICODE)
    return len(words) <= 12


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 160:
        return False
    letters = [char for char in stripped if char.isalpha()]
    numbered = re.match(r"^(?:\d{1,3}(?:\.\d{1,3})+[.)]?|\d{1,3}[.)]|[a-zA-Z][.)]|[IVXLCDM]+\.)\s+\S", stripped)
    title_text = stripped[numbered.end() - 1:] if numbered else stripped
    short_title = len(stripped.split()) <= 18 and not re.search(r"[;!?]$|\.\s|[=<>]", title_text)
    return (
        bool(HEADING_PATTERN.match(stripped))
        or bool(letters and len(letters) >= 4 and all(char.isupper() for char in letters))
        or bool(numbered and short_title and not stripped.endswith("."))
    )


def _looks_like_table_line(line: str) -> bool:
    stripped = line.rstrip()
    return stripped.count("|") >= 2 or bool(re.search(r"\S(?:\s{3,})\S", stripped))


def _asset_type_from_source_caption(caption: str) -> str | None:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFD", caption).casefold()
        if not unicodedata.combining(character)
    ).replace("đ", "d")
    if any(marker in normalized for marker in ("so do", "do thi", "luoc do", "diagram", "flowchart")):
        return "diagram"
    return None


def _link_source_caption(asset: Asset, content: str) -> None:
    if not asset.source_caption:
        asset.source_caption = content
        asset.metadata["caption_source"] = "docling_source_caption"
    source_type = _asset_type_from_source_caption(content)
    if source_type and asset.asset_type != source_type:
        asset.asset_type = source_type
        asset.metadata["asset_type_source"] = "source_caption"


_CODE_LINE_PATTERN = re.compile(r"/\*|\*/|//|[;{}]\s*$")


def _column_starts(line: str) -> tuple[int, ...]:
    stripped = line.rstrip()
    if _CODE_LINE_PATTERN.search(stripped):
        return ()
    return tuple(match.end() for match in re.finditer(r"\S\s{3,}(?=\S)", stripped))


def _columns_align(previous: tuple[int, ...], current: tuple[int, ...], tolerance: int = 2) -> bool:
    return any(abs(left - right) <= tolerance for left in previous for right in current)


def _has_table_signature(text: str) -> bool:
    """Conservatively flag pages whose text layer has repeated data rows.

    Layout extraction pads prose and page headers with wide gaps, so a gap alone
    is not a table: whitespace-aligned rows must share a column position.
    """
    numeric_consecutive = 0
    piped_consecutive = 0
    aligned_consecutive = 0
    previous_starts: tuple[int, ...] = ()
    for line in text.splitlines():
        if not line.strip():
            continue
        values = re.findall(r"(?:^|\s)(?:-?\d+(?:[.,]\d+)?|∞|\{[^}\n]+\})(?=\s|$)", line)
        numeric_consecutive = numeric_consecutive + 1 if len(values) >= 3 else 0
        piped_consecutive = piped_consecutive + 1 if line.count("|") >= 2 else 0
        starts = _column_starts(line)
        if starts and previous_starts and _columns_align(previous_starts, starts):
            aligned_consecutive += 1
        else:
            aligned_consecutive = 1 if starts else 0
        previous_starts = starts
        if numeric_consecutive >= 2 or piped_consecutive >= 2 or aligned_consecutive >= 3:
            return True
    return False


def _split_table_rows(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in lines:
        if "|" in line:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        else:
            cells = [cell.strip() for cell in re.split(r"\s{3,}", line.strip())]
        rows.append(cells)
    return rows


def _normalize_table_structure(value: Any) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    rows = data.get("rows")
    cells = data.get("table_cells") or data.get("cells") or []
    if not rows and cells:
        row_count = max(
            (int(cell.get("end_row_offset_idx", cell.get("row", 0) + 1)) for cell in cells),
            default=0,
        )
        column_count = max(
            (int(cell.get("end_col_offset_idx", cell.get("column", 0) + 1)) for cell in cells),
            default=0,
        )
        rows = [["" for _ in range(column_count)] for _ in range(row_count)]
        for cell in cells:
            row = int(cell.get("start_row_offset_idx", cell.get("row", 0)))
            column = int(cell.get("start_col_offset_idx", cell.get("column", 0)))
            if row < row_count and column < column_count:
                rows[row][column] = str(cell.get("text") or "")
    rows = rows or []
    normalized_cells = []
    if cells:
        for cell in cells:
            row = int(cell.get("start_row_offset_idx", cell.get("row", 0)))
            column = int(cell.get("start_col_offset_idx", cell.get("column", 0)))
            row_end = int(cell.get("end_row_offset_idx", row + 1))
            column_end = int(cell.get("end_col_offset_idx", column + 1))
            normalized_cells.append(
                {
                    "row": row,
                    "column": column,
                    "text": str(cell.get("text") or ""),
                    "row_span": max(1, row_end - row),
                    "column_span": max(1, column_end - column),
                }
            )
    else:
        normalized_cells = [
            {"row": row_index, "column": column_index, "text": cell, "row_span": 1, "column_span": 1}
            for row_index, row in enumerate(rows)
            for column_index, cell in enumerate(row)
        ]
    return {
        **data,
        "rows": rows,
        "row_count": len(rows),
        "column_count": len(rows[0]) if rows else 0,
        "cells": normalized_cells,
    }


def _normalize_layout_blocks(units: list[DocumentUnit]) -> None:
    """Remove visual padding, retaining raw candidates and typed table cells.

    Code/formula whitespace is significant. Never apply prose cleanup to it.
    Block IDs and original source locations remain attached to the transformed text.
    """
    for unit in units:
        for block in unit.content_blocks:
            original = block.content
            normalized = original
            if block.block_type == "table" and isinstance(block.structured_content, dict):
                rows = block.structured_content.get("rows")
                if rows and all(isinstance(row, list) for row in rows):
                    # Keep cell boundaries, order and values (including inner spaces).
                    # No invented header row or merged-cell interpretation.
                    normalized = "\n".join(
                        " | ".join(str(cell).strip().replace("|", "\\|").replace("\n", "<br>") for cell in row)
                        for row in rows
                    )
            elif block.block_type in {"prose", "heading", "caption", "list"} and "`" not in original:
                normalized = "\n".join(
                    re.sub(r"[ \t]+", " ", line.strip()) if block.block_type != "list"
                    else line[:len(line) - len(line.lstrip())] + re.sub(r"[ \t]+", " ", line.lstrip()).rstrip()
                    for line in original.splitlines()
                )
            if normalized != original:
                block.content = normalized
                block.transformation_log.append({
                    "operation": "pdf_layout_whitespace_normalization",
                    "semantic_change": False,
                    "original_content": original,
                })


def _append_docling_structured_blocks(
    unit: DocumentUnit,
    result: dict[str, Any],
    context: ParseContext,
    assets: list[Asset],
    *,
    page_number: int,
    confidence: float,
) -> None:
    page_asset_ids = set(unit.asset_ids)
    page_assets = [
        asset for asset in assets
        if asset.asset_id in page_asset_ids
        and asset.metadata.get("original_or_crop") != "pdf_page_region_reference"
    ]
    for raw_block in result.get("structured_blocks") or []:
        block_type = raw_block.get("block_type") or "prose"
        content = str(raw_block.get("content") or "")
        if block_type not in {"table", "formula", "image", "diagram", "caption"}:
            continue
        bbox = _bbox_list(raw_block.get("bbox"))
        block_assets = page_assets
        if (
            block_type in {"image", "diagram"}
            and not page_assets
            and bbox
            and all(math.isfinite(value) for value in bbox)
            and bbox[0] < bbox[2]
            and bbox[1] != bbox[3]
        ):
            # Vector drawings and inline images need not have an /Image XObject.
            # Retain the original PDF region, without claiming an image was extracted.
            raw_bbox = raw_block.get("bbox")
            coord_origin = raw_bbox.get("coord_origin") if isinstance(raw_bbox, dict) else None
            location = f"pdf:{page_number}:bbox:{bbox}:origin:{coord_origin}"
            asset_id = stable_asset_id(context, location, 0)
            asset = next((item for item in assets if item.asset_id == asset_id), None)
            if asset is None:
                asset = Asset(
                    asset_id=asset_id,
                    asset_type=block_type,
                    status="reference_only",
                    storage_uri=context.source_uri,
                    provenance=SourceProvenance(
                        **context.model_dump(exclude={"mime_type"}),
                        page_number=page_number,
                        source_location={"page_number": page_number, "coord_origin": coord_origin},
                        bbox=bbox,
                        extractor="docling",
                        extraction_method="pdf_page_region_reference",
                        confidence=confidence,
                        raw_ref=f"{context.source_uri}#page={page_number}&bbox={','.join(map(str, bbox))}",
                    ),
                    validation_status="needs_review",
                    validation_notes=["PDF region retained as source; image bytes have not been extracted"],
                    metadata={"original_or_crop": "pdf_page_region_reference"},
                )
                assets.append(asset)
                unit.asset_ids.append(asset_id)
            block_assets = [asset]
        linked_asset_ids = [asset.asset_id for asset in block_assets]
        candidates = [
            block for block in unit.content_blocks
            if content and (not bbox or not block.provenance.bbox or block.provenance.bbox == bbox) and (
                block.content.strip() == content.strip()
                or (
                    block_type == block.block_type == "table"
                    and isinstance(block.structured_content, dict)
                    and block.structured_content.get("rows")
                    == _normalize_table_structure(raw_block.get("structured_content")).get("rows")
                )
            )
        ]
        # Repeated identical tables on one page can be distinct occurrences.
        # Do not replace another occurrence's bbox or guess among ambiguous matches.
        located = [block for block in candidates if bbox and block.provenance.bbox == bbox]
        candidates = located or candidates
        matching = candidates[0] if len(candidates) == 1 else None
        if matching is not None:
            if matching.block_type in {"prose", "heading"}:
                matching.block_type = block_type
            matching.structured_content = (
                _normalize_table_structure(raw_block.get("structured_content"))
                if block_type == "table"
                else raw_block.get("structured_content") or matching.structured_content
            )
            if bbox:
                matching.provenance.bbox = bbox
            if block_type in {"image", "diagram", "caption"}:
                matching.asset_ids = linked_asset_ids
            matching.validation_status = (
                "passed" if matching.provenance.bbox or block_type not in {"table", "formula"} else "needs_review"
            )
            matching.validation_notes = [] if matching.validation_status == "passed" else ["layout block has no source bbox"]
            matching.transformation_log.append(
                {"operation": "docling_layout_enrichment", "semantic_change": False}
            )
            if len(page_assets) == 1 and block_type == "caption":
                _link_source_caption(page_assets[0], content)
            continue
        review_notes: list[str] = []
        if block_type in {"table", "formula", "image", "diagram"} and not bbox:
            review_notes.append("layout block has no source bbox")
        if block_type in {"image", "diagram", "caption"} and len(linked_asset_ids) != 1:
            review_notes.append("visual-to-asset link is ambiguous")
        block = make_block(
            context,
            location_key=f"pdf:{page_number}:docling:{len(unit.content_blocks)}",
            index=len(unit.content_blocks),
            block_type=block_type,
            content=content,
            source_location={"page_number": page_number},
            extractor="docling",
            extraction_method="page_selective_layout",
            page_number=page_number,
            confidence=confidence,
            structured_content=(
                _normalize_table_structure(raw_block.get("structured_content"))
                if block_type == "table"
                else raw_block.get("structured_content")
            ),
            asset_ids=linked_asset_ids if block_type in {"image", "diagram", "caption"} else None,
            bbox=bbox,
            validation_status="needs_review" if review_notes else "passed",
            validation_notes=review_notes,
        )
        unit.content_blocks.append(block)
        if len(page_assets) == 1 and block_type in {"image", "diagram"} and bbox:
            asset = page_assets[0]
            asset.provenance.bbox = bbox
            asset.validation_status = "passed"
            asset.validation_notes = []
        if len(page_assets) == 1 and block_type == "caption" and content:
            _link_source_caption(page_assets[0], content)


def _blocks_from_text(
    text: str,
    context: ParseContext,
    *,
    page_number: int,
    extractor: str,
    extraction_method: str,
    confidence: float,
) -> list[ContentBlock]:
    lines = text.splitlines()
    blocks: list[ContentBlock] = []
    cursor = 0
    paragraph: list[str] = []
    paragraph_start = 0

    def emit(block_type: str, content_lines: list[str], start_line: int, structured=None) -> None:
        if not content_lines or not any(line.strip() for line in content_lines):
            return
        content = "\n".join(content_lines).rstrip()
        blocks.append(
            make_block(
                context,
                location_key=f"pdf:{page_number}:lines:{start_line + 1}-{start_line + len(content_lines)}",
                index=len(blocks),
                block_type=block_type,
                content=content,
                source_location={
                    "page_number": page_number,
                    "line_start": start_line + 1,
                    "line_end": start_line + len(content_lines),
                },
                extractor=extractor,
                extraction_method=extraction_method,
                page_number=page_number,
                confidence=confidence,
                structured_content=structured,
            )
        )

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            emit("prose", paragraph, paragraph_start)
            paragraph = []

    while cursor < len(lines):
        line = lines[cursor]
        if not line.strip():
            flush_paragraph()
            cursor += 1
            continue
        if _looks_like_code(line):
            flush_paragraph()
            start = cursor
            group = [line]
            cursor += 1
            while cursor < len(lines):
                candidate = lines[cursor]
                if re.fullmatch(r"\s*(?:trang|page)\s+\d+\s*", candidate, re.IGNORECASE) and not any(
                    remaining.strip() for remaining in lines[cursor + 1:]
                ):
                    # A terminal page label is furniture, not an indented code line.
                    break
                if not candidate.strip():
                    group.append(candidate)
                    cursor += 1
                    continue
                if not _looks_like_code(candidate) and not candidate.startswith((" ", "\t")):
                    break
                group.append(candidate)
                cursor += 1
            emit("code", group, start)
            continue
        if _looks_like_formula(line):
            flush_paragraph()
            emit("formula", [line], cursor, {"raw": line})
            cursor += 1
            continue
        if _looks_like_heading(line) and not _looks_like_table_line(line):
            flush_paragraph()
            emit("heading", [line], cursor)
            cursor += 1
            continue
        if LIST_PATTERN.match(line):
            flush_paragraph()
            start = cursor
            group = [line]
            cursor += 1
            while cursor < len(lines) and LIST_PATTERN.match(lines[cursor]):
                group.append(lines[cursor])
                cursor += 1
            emit("list", group, start)
            continue
        if _looks_like_table_line(line):
            start = cursor
            group = [line]
            cursor += 1
            while cursor < len(lines) and _looks_like_table_line(lines[cursor]):
                group.append(lines[cursor])
                cursor += 1
            rows = _split_table_rows(group)
            # PDF justification also contains wide spaces. A ragged collection
            # of visual gaps is not evidence of a table; retain it as text.
            if len(group) >= 2 and len({len(row) for row in rows}) == 1:
                flush_paragraph()
                emit("table", group, start, _normalize_table_structure({"rows": rows}))
                continue
            # The common text branch below advances once after preserving line.
            cursor = start
        if _looks_like_heading(line):
            flush_paragraph()
            emit("heading", [line], cursor)
            cursor += 1
            continue
        if not paragraph:
            paragraph_start = cursor
        paragraph.append(line)
        cursor += 1
    flush_paragraph()
    return blocks


OcrPageExtractor = Callable[[Path, list[int], ParseContext], dict[int, dict[str, Any]]]


def _text_is_usable(metrics: dict[str, Any]) -> bool:
    """Reject empty/corrupt extraction without mistaking a short valid page for bad OCR."""
    return (
        int(metrics.get("non_whitespace_characters") or 0) >= 20
        and float(metrics.get("replacement_ratio") or 0.0) <= 0.02
        and float(metrics.get("control_ratio") or 0.0) <= 0.01
    )


def _default_ocr_page_extractor(path: Path, page_numbers: list[int], context: ParseContext) -> dict[int, dict[str, Any]]:
    from modules.ocr.docling_engine import ocr_pdf_pages

    return ocr_pdf_pages(str(path), page_numbers)


class PdfParser(DocumentParser):
    extensions = frozenset({".pdf"})
    mime_types = frozenset({"application/pdf"})

    def __init__(self, ocr_page_extractor: OcrPageExtractor | None = _default_ocr_page_extractor):
        self.ocr_page_extractor = ocr_page_extractor

    def parse(self, path: Path, context: ParseContext) -> ParsedDocument:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF được mã hóa và không thể mở nếu thiếu mật khẩu")

        units: list[DocumentUnit] = []
        assets: list[Asset] = []
        raw_engine_outputs: dict[str, Any] = {}
        docling_required: list[int] = []
        hard_ocr_required: set[int] = set()
        for page_number, page in enumerate(reader.pages, start=1):
            candidates = _candidate_texts(page)
            # Equal-quality candidates often differ only in synthetic spacing at
            # font switches. Prefer the least padded plain extraction, not the longest.
            selected = max(candidates, key=lambda candidate: (
                candidate["score"], candidate["method"] == "pypdf_plain", -len(candidate["text"]),
            )) if candidates else {
                "method": "pypdf_plain",
                "space_width": None,
                "text": "",
                "quality": text_quality_metrics(""),
                "score": 0.0,
            }
            page_assets: list[str] = []
            for asset_index, (xobject_name, image_object) in enumerate(_image_objects(page.get("/Resources"))):
                asset_id = stable_asset_id(context, f"pdf:{page_number}:{xobject_name}", asset_index)
                provenance = SourceProvenance(
                    source_file_name=context.source_file_name,
                    source_uri=context.source_uri,
                    document_id=context.document_id,
                    document_type=context.document_type,
                    page_number=page_number,
                    source_location={"page_number": page_number, "xobject_name": xobject_name},
                    extractor="pypdf",
                    extraction_method="pdf_xobject_reference",
                    raw_ref=f"{context.source_uri}#page={page_number}&xobject={xobject_name}",
                )
                try:
                    image_bytes = image_object.get_data()
                except Exception:
                    image_bytes = None
                assets.append(
                    Asset(
                        asset_id=asset_id,
                        asset_type="image",
                        status="reference_only",
                        provenance=provenance,
                        storage_uri=context.source_uri,
                        content_sha256=hashlib.sha256(image_bytes).hexdigest() if image_bytes else None,
                        validation_status="needs_review",
                        validation_notes=["PDF XObject placement bbox/caption requires layout validation"],
                        metadata={
                            "xobject_name": xobject_name,
                            "width": int(image_object.get("/Width") or 0),
                            "height": int(image_object.get("/Height") or 0),
                            "filter": str(image_object.get("/Filter") or ""),
                            "original_or_crop": "pdf_xobject_reference",
                            "byte_length": len(image_bytes) if image_bytes else None,
                        },
                    )
                )
                page_assets.append(asset_id)

            score = float(selected["score"])
            quality = dict(selected["quality"])
            text_is_inadequate = not _text_is_usable(selected["quality"])
            table_layout = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate["method"] == "pypdf_layout" and _has_table_signature(candidate["text"])
                ),
                None,
            )
            layout_is_material = bool(page_assets) or bool(table_layout) or _has_table_signature(selected["text"])
            quality.update(
                {
                    "score": score,
                    "selected_method": selected["method"],
                    "space_width": selected["space_width"],
                    "requires_ocr": text_is_inadequate,
                    "requires_layout_extraction": layout_is_material,
                }
            )
            if text_is_inadequate:
                hard_ocr_required.add(page_number)
                if len(page_assets) == 1:
                    page_asset = next((asset for asset in assets if asset.asset_id == page_assets[0]), None)
                    if page_asset:
                        page_asset.metadata["is_page_raster"] = True
            if text_is_inadequate or layout_is_material:
                docling_required.append(page_number)
            block_source = selected
            block_text = selected["text"]
            if (
                table_layout and not text_is_inadequate
                and len(table_layout["text"]) <= 2 * max(len(selected["text"]), 1)
            ):
                block_source = table_layout
                block_text = "\n".join(line for line in table_layout["text"].splitlines() if line.strip())
            blocks = _blocks_from_text(
                block_text,
                context,
                page_number=page_number,
                extractor="pypdf",
                extraction_method=f"{block_source['method']}:space_width={block_source['space_width']}",
                confidence=score,
            )
            units.append(
                DocumentUnit(
                    unit_number=page_number,
                    page_number=page_number,
                    source_location={"page_number": page_number},
                    raw_text=selected["text"],
                    content_blocks=blocks,
                    asset_ids=page_assets,
                    raw_extraction={
                        "candidates": candidates,
                        "selected_candidate": selected["method"],
                        "block_candidate": block_source["method"],
                    },
                    quality=quality,
                )
            )

        if docling_required and self.ocr_page_extractor:
            docling_failed = False
            try:
                ocr_results = self.ocr_page_extractor(path, docling_required, context)
            except Exception as exc:
                docling_failed = True
                ocr_results = {}
                for page_number in docling_required:
                    status = "quality_failed" if page_number in hard_ocr_required else "passed_with_warning"
                    units[page_number - 1].quality.update(
                        {"status": status, "reason": f"Docling extraction failed: {type(exc).__name__}"}
                    )
            for page_number in docling_required:
                result = ocr_results.get(page_number)
                if not result or not (result.get("text") or "").strip():
                    if docling_failed:
                        continue
                    if page_number in hard_ocr_required:
                        # Blank page: kept for provenance, excluded from retrieval; the document-level
                        # text-unit ratio decides whether the document as a whole fails.
                        units[page_number - 1].quality.update(
                            {
                                "status": "passed_with_warning",
                                "reason": "OCR returned no text; page treated as blank",
                                "blank_page": True,
                                "retrieval_excluded": True,
                            }
                        )
                    else:
                        units[page_number - 1].quality.update(
                            {"status": "passed_with_warning", "reason": "Layout extraction returned no text"}
                        )
                    continue
                ocr_text = str(result["text"])
                score = text_quality_score(ocr_text)
                if result.get("raw_document") is not None:
                    raw_engine_outputs["docling"] = result["raw_document"]
                units[page_number - 1].raw_extraction["docling"] = {
                    key: value for key, value in result.items() if key != "raw_document"
                }
                if page_number in hard_ocr_required:
                    units[page_number - 1].raw_text = ocr_text
                    units[page_number - 1].content_blocks = _blocks_from_text(
                        ocr_text,
                        context,
                        page_number=page_number,
                        extractor="docling",
                        extraction_method="page_selective_ocr",
                        confidence=score,
                    )
                _append_docling_structured_blocks(
                    units[page_number - 1],
                    result,
                    context,
                    assets,
                    page_number=page_number,
                    confidence=score,
                )
                if page_number in hard_ocr_required:
                    ocr_metrics = text_quality_metrics(ocr_text)
                    units[page_number - 1].quality.update(
                        {
                            **ocr_metrics,
                            "score": score,
                            "selected_method": "docling_page_selective_ocr",
                            "layout_method": "docling_page_selective_layout",
                            "status": "passed" if _text_is_usable(ocr_metrics) else "quality_failed",
                        }
                    )
                else:
                    units[page_number - 1].quality.update(
                        {
                            "layout_method": "docling_page_selective_layout",
                            "layout_quality": {**text_quality_metrics(ocr_text), "score": score},
                            "status": "passed",
                        }
                    )

        _normalize_layout_blocks(units)
        return ParsedDocument(
            document_id=context.document_id,
            document_type=context.document_type,
            source_file_name=context.source_file_name,
            source_uri=context.source_uri,
            units=units,
            assets=assets,
            raw_engine_outputs=raw_engine_outputs,
            stats={
                "source_format": "pdf",
                "page_count": len(units),
                "asset_count": len(assets),
                "ocr_page_count": len(hard_ocr_required),
                "layout_page_count": len(docling_required) - len(hard_ocr_required),
                "docling_page_count": len(docling_required),
                "text_layer_page_count": len(units) - len(hard_ocr_required),
            },
        )
