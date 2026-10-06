from __future__ import annotations

import re
from collections import Counter
import unicodedata
from dataclasses import dataclass, field

from modules.documents.ingest.models import ParsedDocument


VALID_ASSET_STATUSES = {"stored", "reference_only", "unsupported", "failed"}
PROTECTED_BLOCK_TYPES = {"table", "code", "formula"}
TYPED_BLOCK_TYPES = {"table", "code", "formula", "image", "diagram", "caption"}


@dataclass
class QualityReport:
    status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status != "quality_failed"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


def text_quality_metrics(value: str) -> dict[str, float | int]:
    text = unicodedata.normalize("NFC", value or "")
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    alpha_tokens = [token for token in tokens if token.isalpha()]
    single_alpha = sum(len(token) == 1 for token in alpha_tokens)
    controls = sum(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text)
    non_whitespace = len("".join(text.split()))
    code_symbols = sum(char in "{}[]();=<>+-*/" for char in text)
    return {
        "characters": len(text),
        "non_whitespace_characters": non_whitespace,
        "tokens": len(tokens),
        "single_alpha_ratio": round(single_alpha / max(len(alpha_tokens), 1), 6),
        "replacement_ratio": round(text.count("\ufffd") / max(len(text), 1), 6),
        "control_ratio": round(controls / max(len(text), 1), 6),
        "whitespace_ratio": round(1.0 - non_whitespace / max(len(text), 1), 6),
        "code_symbol_ratio": round(code_symbols / max(non_whitespace, 1), 6),
    }


def text_quality_score(value: str) -> float:
    metrics = text_quality_metrics(value)
    density = min(float(metrics["non_whitespace_characters"]) / 150.0, 1.0)
    score = density
    code_discount = max(0.15, 1.0 - min(float(metrics["code_symbol_ratio"]) * 8.0, 0.85))
    score -= min(float(metrics["single_alpha_ratio"]) * 1.5 * code_discount, 0.8)
    score -= min(float(metrics["replacement_ratio"]) * 20.0, 1.0)
    score -= min(float(metrics["control_ratio"]) * 20.0, 1.0)
    score -= max(float(metrics["whitespace_ratio"]) - 0.55, 0.0)
    return round(max(0.0, min(score, 1.0)), 6)


def validate_parsed_document(document: ParsedDocument, *, min_text_unit_ratio: float = 0.0) -> QualityReport:
    errors: list[str] = []
    warnings: list[str] = []
    block_count = 0
    text_units = 0
    blank_units: list[int] = []
    for unit in document.units:
        if unit.quality.get("retrieval_excluded"):
            blank_units.append(unit.unit_number)
        elif any(
            block.content.strip() and block.block_type not in {"image", "diagram", "page_break"}
            for block in unit.content_blocks
        ):
            text_units += 1
        if unit.quality.get("status") == "quality_failed":
            errors.append(
                f"unit {unit.unit_number}: {unit.quality.get('reason') or 'extraction quality failed'}"
            )
        for block in unit.content_blocks:
            block_count += 1
            provenance = block.provenance
            if not provenance.source_file_name:
                errors.append(f"{block.block_id}: missing source_file_name")
            if not provenance.source_uri or not provenance.document_id:
                errors.append(f"{block.block_id}: missing source identity")
            if provenance.page_number is None and not provenance.source_location:
                errors.append(f"{block.block_id}: missing source location")
            if block.block_type == "code" and block.content.count("```") % 2:
                errors.append(f"{block.block_id}: unbalanced code fence")
            if block.part_count and block.block_type in PROTECTED_BLOCK_TYPES and not block.continuation_of:
                errors.append(f"{block.block_id}: protected continuation missing parent")
            if block.validation_status == "failed":
                errors.append(f"{block.block_id}: block validation failed")
            elif block.validation_status == "needs_review":
                warnings.append(f"{block.block_id}: block needs review")
            if block.block_type == "table":
                rows = block.structured_content.get("rows") if isinstance(block.structured_content, dict) else None
                if not isinstance(rows, list) or not rows or not all(isinstance(row, list) for row in rows):
                    errors.append(f"{block.block_id}: table cell grid missing")
                else:
                    widths = {len(row) for row in rows}
                    if len(widths) != 1:
                        errors.append(f"{block.block_id}: table cell grid is ragged")
                if block.provenance.page_number is not None and block.provenance.bbox is None:
                    warnings.append(f"{block.block_id}: table bbox missing")
            if block.block_type == "formula":
                formula = block.structured_content if isinstance(block.structured_content, dict) else {}
                if not any(formula.get(key) for key in ("raw", "latex", "mathml", "omml")):
                    errors.append(f"{block.block_id}: formula raw representation missing")
                if block.provenance.bbox is None and block.provenance.page_number is not None:
                    warnings.append(f"{block.block_id}: formula bbox missing")
            if block.block_type in {"image", "diagram"} and not block.asset_ids:
                errors.append(f"{block.block_id}: visual block missing source asset")
            if block.block_type == "caption" and not block.content.strip():
                errors.append(f"{block.block_id}: empty caption")
    for asset in document.assets:
        if asset.status not in VALID_ASSET_STATUSES:
            errors.append(f"{asset.asset_id}: invalid asset status")
        if asset.status in {"unsupported", "failed"} and not asset.reason:
            errors.append(f"{asset.asset_id}: asset failure reason missing")
        if asset.validation_status == "failed":
            errors.append(f"{asset.asset_id}: asset validation failed")
        elif asset.validation_status == "needs_review":
            warnings.append(f"{asset.asset_id}: asset needs review")
        if asset.status in {"stored", "reference_only"} and not asset.storage_uri:
            errors.append(f"{asset.asset_id}: asset storage/reference URI missing")
        if asset.status in {"stored", "reference_only"} and not asset.content_sha256:
            warnings.append(f"{asset.asset_id}: asset content hash missing")
        if asset.provenance.page_number is not None and asset.provenance.bbox is None:
            warnings.append(f"{asset.asset_id}: asset bbox missing")
        if asset.derived_description and not asset.derived_description.model_version:
            errors.append(f"{asset.asset_id}: derived description model version missing")
        if asset.derived_description and asset.derived_description.text == asset.source_content:
            warnings.append(f"{asset.asset_id}: derived description duplicates source content")
    if not document.units:
        errors.append("document has no units")
    if not block_count:
        errors.append("document has no content blocks")
    if blank_units:
        warnings.append(
            f"{len(blank_units)} trang trống/không trích xuất được văn bản bị loại khỏi truy xuất: "
            + ", ".join(str(number) for number in blank_units[:20])
            + (" …" if len(blank_units) > 20 else "")
        )
    text_unit_ratio = round(text_units / len(document.units), 4) if document.units else 0.0
    if document.units and min_text_unit_ratio > 0 and text_unit_ratio < min_text_unit_ratio:
        errors.insert(
            0,
            f"Chỉ {text_units}/{len(document.units)} trang/đơn vị ({text_unit_ratio:.0%}) trích xuất được văn bản; "
            f"yêu cầu tối thiểu {min_text_unit_ratio:.0%}"
        )
    metrics = {
        "units": len(document.units),
        "blocks": block_count,
        "assets": len(document.assets),
        "text_units": text_units,
        "text_unit_ratio": text_unit_ratio,
        "retrieval_excluded_units": blank_units,
    }
    status = "quality_failed" if errors else "needs_review" if warnings else "passed"
    return QualityReport(status, errors, warnings, metrics)


def validate_chunks(chunks: list[dict]) -> QualityReport:
    errors: list[str] = []
    warnings: list[str] = []
    content_counts = Counter(chunk.get("content") or "" for chunk in chunks)
    duplicate_groups = sum(count > 1 for content, count in content_counts.items() if content.strip())
    if duplicate_groups:
        warnings.append(f"{duplicate_groups} exact duplicate content groups; source occurrences retained")
    long_whitespace = 0
    if not chunks:
        errors.append("no chunks generated")
    for chunk in chunks:
        metadata = chunk.get("metadata") or {}
        chunk_id = chunk.get("chunk_id") or "unknown"
        if not metadata.get("source_file_name"):
            errors.append(f"{chunk_id}: missing source_file_name")
        if not metadata.get("document_id") or not metadata.get("source_uri"):
            errors.append(f"{chunk_id}: missing source identity")
        if not metadata.get("page_marks") and not metadata.get("source_locations"):
            errors.append(f"{chunk_id}: missing source location")
        content = chunk.get("content") or ""
        if not content.strip():
            errors.append(f"{chunk_id}: empty chunk")
        if re.search(r"[ \t]{20,}", content):
            long_whitespace += 1
            warnings.append(f"{chunk_id}: long layout whitespace; verify protected content")
        if metadata.get("content_type") == "code" and content.count("```") % 2:
            errors.append(f"{chunk_id}: unbalanced code fence")
        if metadata.get("part_count") and metadata.get("content_type") in PROTECTED_BLOCK_TYPES:
            if not metadata.get("continuation_of"):
                errors.append(f"{chunk_id}: protected continuation missing parent")
        if "failed" in (metadata.get("validation_statuses") or []):
            errors.append(f"{chunk_id}: contains failed source block")
        elif metadata.get("requires_review"):
            warnings.append(f"{chunk_id}: contains source block needing review")
    return QualityReport(
        "quality_failed" if errors else "needs_review" if warnings else "passed",
        errors,
        warnings,
        {
            "chunks": len(chunks),
            "exact_duplicate_groups": duplicate_groups,
            "duplicate_chunks": sum(count - 1 for content, count in content_counts.items() if content.strip()),
            "long_whitespace_chunks": long_whitespace,
        },
    )
