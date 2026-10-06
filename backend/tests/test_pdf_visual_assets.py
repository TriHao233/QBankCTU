from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from modules.documents.ingest.models import ParseContext
from modules.documents.ingest.parsers.pdf import PdfParser
from modules.documents.ingest.quality import validate_parsed_document


def _parse_vector_pdf(tmp_path: Path, structured_blocks: list[dict], text: str = ""):
    source = tmp_path / "vector.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    drawing = DecodedStreamObject()
    drawing.set_data(b"10 20 100 80 re S\n150 200 80 90 re S\n")
    page[NameObject("/Contents")] = writer._add_object(drawing)
    writer.write(source)
    context = ParseContext(
        document_id="vector-document",
        document_type="pdf",
        source_file_name=source.name,
        source_uri=str(source),
        mime_type="application/pdf",
    )

    def extract(path, page_numbers, _context):
        assert path == source
        assert page_numbers == [1]
        return {1: {
            "text": text or "This page contains source drawings and accompanying explanatory text. " * 4,
            "structured_blocks": structured_blocks,
        }}

    return PdfParser(ocr_page_extractor=extract).parse(source, context)


@pytest.mark.parametrize("matched_text", [False, True])
def test_docling_visuals_without_xobjects_keep_distinct_pdf_region_assets(tmp_path, matched_text):
    content = "Source drawing with explanatory text." if matched_text else ""
    parsed = _parse_vector_pdf(tmp_path, [
        {"block_type": "image", "content": content,
         "bbox": {"l": 10, "b": 20, "r": 110, "t": 100, "coord_origin": "BOTTOMLEFT"}},
        {"block_type": "diagram", "content": "", "bbox": [150, 200, 230, 290]},
    ], text=content)

    report = validate_parsed_document(parsed)
    assert report.passed, report.errors
    visuals = [block for block in parsed.units[0].content_blocks if block.block_type in {"image", "diagram"}]
    assert len(visuals) == len(parsed.assets) == 2
    assert len(set(parsed.units[0].asset_ids)) == 2
    for block, asset in zip(visuals, parsed.assets):
        assert block.asset_ids == [asset.asset_id]
        assert asset.asset_type == block.block_type
        assert asset.status == "reference_only"
        assert asset.storage_uri == parsed.source_uri
        assert asset.provenance.document_id == parsed.document_id
        assert asset.provenance.page_number == 1
        assert asset.provenance.bbox == block.provenance.bbox
        assert asset.provenance.raw_ref.startswith(f"{parsed.source_uri}#page=1&bbox=")
        assert asset.validation_status == "needs_review"
        assert asset.content_sha256 is None  # No fabricated hash of image bytes.
    assert parsed.assets[0].provenance.source_location["coord_origin"] == "BOTTOMLEFT"
    records = parsed.to_page_records()
    assert {asset["asset_id"] for asset in records[0]["assets"]} == set(parsed.units[0].asset_ids)
    assert parsed.stats["asset_count"] == 2


@pytest.mark.parametrize("bbox", [
    None, [1, 2, 3], ["bad", 2, 3, 4], [1, 2, 1, 4], [1, 2, 3, 2],
    [3, 2, 1, 4], [1, 2, float("inf"), 4], [1, float("nan"), 3, 4],
])
def test_visual_without_xobject_or_valid_region_still_fails_quality_gate(tmp_path, bbox):
    parsed = _parse_vector_pdf(tmp_path, [{"block_type": "image", "content": "", "bbox": bbox}])

    report = validate_parsed_document(parsed)
    assert not report.passed
    assert any("visual block missing source asset" in error for error in report.errors)
    assert parsed.assets == []


def test_docling_top_left_region_preserves_coordinate_origin(tmp_path):
    parsed = _parse_vector_pdf(tmp_path, [{
        "block_type": "image", "content": "",
        "bbox": {"l": 10, "b": 100, "r": 110, "t": 20, "coord_origin": "TOPLEFT"},
    }])

    assert validate_parsed_document(parsed).passed
    assert parsed.assets[0].provenance.bbox == [10, 100, 110, 20]
    assert parsed.assets[0].provenance.source_location["coord_origin"] == "TOPLEFT"
