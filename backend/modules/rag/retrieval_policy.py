"""Conservative retrieval-only filtering; original pages/blocks remain canonical.

No filename/page-number/subject vocabulary is used. Decisions are recorded by
source block and reason so filtering is inspectable without destroying extraction.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
import unicodedata


TOC_ENTRY = re.compile(r"\S.*?(?:\.{3,}|…{2,}|(?:\.\s){3,})\s*\d+\b.*$")
PAGE_NUMBER = re.compile(r"^(?:trang|page)\s*\d+\s*$", re.IGNORECASE)
PROTECTED = {"code", "formula", "table", "image", "diagram"}


def folded(text: str) -> str:
    value = "".join(c for c in unicodedata.normalize("NFD", text).casefold() if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value.replace("đ", "d")).strip(" #\t\n")


def _header_key(text: str) -> str:
    # Only for repeated-layout comparison, never for emitted Vietnamese text.
    return "".join(folded(text).split())


def _cover(blocks: list[dict]) -> bool:
    lines = [line.strip() for block in blocks for line in (block.get("content") or "").splitlines() if line.strip()]
    lines = [line for line in lines if not PAGE_NUMBER.fullmatch(line) and not line.isdigit()]
    if not lines or any(block.get("block_type") in PROTECTED for block in blocks):
        return False
    if any(len(line.split()) >= 20 or len(line) > 180 for line in lines):
        return False
    upper = sum(len([c for c in line if c.isalpha()]) >= 4 and line.isupper() for line in lines)
    institution = any(re.search(r"\b(?:truong|dai hoc|khoa|nha xuat ban|university|faculty)\b", folded(line)) for line in lines)
    return len(lines) >= 3 and upper / len(lines) >= 0.75 and (institution or upper >= 4)


def filter_retrieval_pages(pages: list[dict], metrics: dict | None = None) -> list[dict]:
    """Filter cover/navigation/running furniture, retaining source IDs and logs."""
    headers: Counter = Counter()
    for page in pages:
        blocks = page.get("content_blocks") or []
        if page.get("page_number") is not None and blocks:
            lines = (blocks[0].get("content") or "").splitlines()
            if lines and len(lines[0].strip()) <= 140:
                if not re.search(r"[.!?]$", lines[0].strip()):
                    headers[_header_key(lines[0])] += 1
    repeated = {line for line, count in headers.items() if line and count >= 3}
    decisions: list[dict] = []
    filtered_chars = 0
    filtered_pages = []
    body_started = False
    for page in pages:
        blocks = page.get("content_blocks") or []
        if (page.get("quality") or {}).get("retrieval_excluded"):
            if blocks:
                decisions.append({
                    "block_id": None, "page_number": page.get("page_number"),
                    "reasons": ["blank_page"], "removed_lines": 0,
                })
                filtered_chars += sum(len(block.get("content") or "") for block in blocks)
            filtered_pages.append({**page, "content_blocks": []})
            continue
        cover = not body_started and _cover(blocks)
        all_lines = [line.strip() for b in blocks for line in b.get("content", "").splitlines() if line.strip()]
        toc_lines = sum(bool(TOC_ENTRY.fullmatch(line)) for line in all_lines)
        explanatory = any(len(line.split()) >= 3 and re.search(r"[.!?]$", line) for line in all_lines if not TOC_ENTRY.fullmatch(line))
        navigation_page = toc_lines >= 3 and toc_lines >= len(all_lines) * 0.5 and not explanatory
        preface = (
            not body_started and any(folded(line) in {"loi noi dau", "loi tua", "preface", "acknowledgements"} for line in all_lines)
            and not any(re.match(r"^(?:chuong|chapter)\s+(?:\d+|[ivxlcdm]+)\b", folded(line)) for line in all_lines)
            and not any(block.get("block_type") in PROTECTED for block in blocks)
        )
        kept = []
        last_source_line = max((
            (b.get("provenance") or {}).get("source_location", {}).get("line_end", 0)
            for b in blocks
        ), default=0)
        for block_index, original in enumerate(blocks):
            block = deepcopy(original)
            content = block.get("content") or ""
            lines = content.splitlines()
            kept_lines = []
            reasons = []
            for line_index, line in enumerate(lines):
                clean = line.strip()
                reason = None
                if cover:
                    reason = "cover_metadata"
                elif preface:
                    reason = "preface_metadata"
                elif navigation_page and block.get("block_type") not in PROTECTED:
                    reason = "toc_page_navigation"
                elif block.get("block_type") not in {"code", "formula", "image", "diagram"}:
                    if TOC_ENTRY.fullmatch(clean):
                        reason = "toc_navigation"
                    elif folded(clean) in {"muc luc", "table of contents", "contents"}:
                        reason = "toc_title"
                    elif page.get("page_number") is not None:
                        if block_index == 0 and line_index == 0 and _header_key(clean) in repeated:
                            reason = "running_header"
                        elif block.get("block_type") != "table" and (
                            block_index == len(blocks) - 1
                            or (last_source_line and (block.get("provenance") or {}).get("source_location", {}).get("line_end") == last_source_line)
                        ) and line_index == len(lines) - 1 and (
                            PAGE_NUMBER.fullmatch(clean) or clean.isdigit()
                        ):
                            reason = "page_number"
                if reason:
                    reasons.append(reason)
                    filtered_chars += len(line)
                else:
                    kept_lines.append(line)
            if reasons:
                decisions.append({
                    "block_id": block.get("block_id"), "page_number": page.get("page_number"),
                    "reasons": sorted(set(reasons)), "removed_lines": len(reasons),
                })
                block["transformation_log"] = [*(block.get("transformation_log") or []), {
                    "operation": "retrieval_structure_filter", "reasons": sorted(set(reasons)),
                    "original_content": content,
                }]
                block["content"] = "\n".join(kept_lines).strip()
            if block.get("content", "").strip() or (not content and block.get("block_type") in {"image", "diagram"}):
                kept.append(block)
        if any(b.get("block_type") in {"prose", "list", "code", "formula"} and len(b.get("content", "").split()) >= 20 for b in kept):
            body_started = True
        filtered_pages.append({**page, "content_blocks": kept})
    if metrics is not None:
        metrics.update({
            "filtered_characters": filtered_chars, "affected_blocks": len(decisions),
            "reasons": dict(Counter(reason for item in decisions for reason in item["reasons"])),
            "decisions": decisions,
        })
    return filtered_pages
