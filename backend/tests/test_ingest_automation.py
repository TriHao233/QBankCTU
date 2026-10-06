from bson import ObjectId

from modules.documents.ingest.models import ContentBlock, DocumentUnit, ParsedDocument, SourceProvenance
from modules.documents.ingest.quality import validate_parsed_document
from modules.rag import chunking as chunking_module
from modules.rag import lineage as lineage_module
from modules.rag.schemas import ChunkingStats, DocumentChunkResponse


def _unit(number: int, text: str) -> DocumentUnit:
    blocks = []
    if text:
        blocks.append(ContentBlock(
            block_id=f"b{number}",
            block_type="prose",
            content=text,
            provenance=SourceProvenance(
                source_file_name="a.pdf",
                source_uri="upload://a.pdf",
                document_id="doc",
                document_type="pdf",
                page_number=number,
                extractor="test",
                extraction_method="native",
            ),
        ))
    return DocumentUnit(unit_number=number, page_number=number, content_blocks=blocks)


def _document(texts: list[str]) -> ParsedDocument:
    return ParsedDocument(
        document_id="doc",
        document_type="pdf",
        source_file_name="a.pdf",
        source_uri="upload://a.pdf",
        units=[_unit(index, text) for index, text in enumerate(texts, start=1)],
    )


def test_document_level_text_coverage_gate_fails_with_clear_reason():
    document = _document(["Nội dung trang một", "", "", "Trang bốn", "Trang năm"])

    report = validate_parsed_document(document, min_text_unit_ratio=0.8)

    assert not report.passed
    assert report.errors[0].startswith("Chỉ 3/5 trang/đơn vị (60%)")
    assert report.metrics["text_unit_ratio"] == 0.6


def test_document_level_text_coverage_gate_passes_at_threshold_and_can_be_disabled():
    document = _document(["Một", "Hai", "Ba", "Bốn", ""])

    assert validate_parsed_document(document, min_text_unit_ratio=0.8).passed
    assert validate_parsed_document(_document(["Một", "", ""]), min_text_unit_ratio=0).passed


def test_blank_pages_are_warned_and_excluded_without_failing_the_document():
    document = _document(["Một", "Hai", "Ba", "Bốn", "Năm", ""])
    document.units[5].quality.update({"status": "passed_with_warning", "retrieval_excluded": True})

    report = validate_parsed_document(document, min_text_unit_ratio=0.8)

    assert report.passed
    assert report.metrics["retrieval_excluded_units"] == [6]
    assert any("trang trống" in warning for warning in report.warnings)


def test_retrieval_filter_drops_blank_page_content():
    from modules.rag.retrieval_policy import filter_retrieval_pages

    pages = [
        {"page_number": 1, "content_blocks": [{"block_id": "a", "block_type": "prose", "content": "Nội dung"}]},
        {"page_number": 2, "quality": {"retrieval_excluded": True},
         "content_blocks": [{"block_id": "b", "block_type": "prose", "content": "~~ rác ~~"}]},
    ]
    metrics: dict = {}

    filtered = filter_retrieval_pages(pages, metrics)

    assert filtered[1]["content_blocks"] == []
    assert pages[1]["content_blocks"][0]["content"] == "~~ rác ~~"
    assert metrics["reasons"]["blank_page"] == 1


class _Repository:
    def __init__(self, document):
        self.document = document
        self.updates = []

    def find_by_id(self, _document_id):
        return self.document

    def update(self, _document_id, fields):
        self.updates.append(fields)
        self.document = {**self.document, **fields}
        return self.document


def test_auto_promotion_skips_when_nothing_is_pending(monkeypatch):
    repository = _Repository({"_id": ObjectId(), "current_processing": {}, "pending_processing": {}})
    monkeypatch.setattr(lineage_module, "get_document_repository", lambda _db=None: repository)

    outcome = lineage_module.auto_promote_pending_lineage("doc", actor="t@x", reason="r", db=object())

    assert outcome["status"] == "SKIPPED"
    assert repository.updates == []


def test_auto_promotion_keeps_pending_lineage_when_validation_fails(monkeypatch):
    pending = {"ocr_job_id": ObjectId(), "chunk_set_id": ObjectId(), "vector_collection_id": ObjectId()}
    repository = _Repository({"_id": ObjectId(), "pending_processing": pending, "pipeline_summary": {}})
    promoted = []

    class FakeService:
        def __init__(self, _db=None):
            self.db = None

        def dry_run(self, _candidate, *, smoke_queries):
            return {"validation": {"status": "failed", "errors": ["chunk set is not COMPLETED"]}}

        def promote(self, *args, **kwargs):
            promoted.append(kwargs)

    monkeypatch.setattr(lineage_module, "get_document_repository", lambda _db=None: repository)
    monkeypatch.setattr(lineage_module, "LineagePromotionService", FakeService)
    monkeypatch.setattr(lineage_module, "_auto_smoke_queries", lambda _db, _id: ["truy vấn"])

    outcome = lineage_module.auto_promote_pending_lineage(str(ObjectId()), actor="t@x", reason="r")

    assert outcome["status"] == "FAILED"
    assert "chunk set is not COMPLETED" in outcome["reason"]
    assert promoted == []
    assert repository.document["pipeline_summary"]["auto_promotion"]["status"] == "FAILED"


def test_auto_promotion_promotes_validated_lineage(monkeypatch):
    pending = {"ocr_job_id": ObjectId(), "chunk_set_id": ObjectId(), "vector_collection_id": ObjectId()}
    repository = _Repository({"_id": ObjectId(), "pending_processing": pending, "pipeline_summary": {}})
    calls = []

    class FakeService:
        def __init__(self, _db=None):
            self.db = None

        def dry_run(self, _candidate, *, smoke_queries):
            return {"validation": {"status": "passed", "errors": []}, "confirmation_token": "TOKEN"}

        def promote(self, _candidate, **kwargs):
            calls.append(kwargs)
            return {"operation_id": "op-1"}

    monkeypatch.setattr(lineage_module, "get_document_repository", lambda _db=None: repository)
    monkeypatch.setattr(lineage_module, "LineagePromotionService", FakeService)
    monkeypatch.setattr(lineage_module, "_auto_smoke_queries", lambda _db, _id: ["truy vấn"])

    outcome = lineage_module.auto_promote_pending_lineage(str(ObjectId()), actor="t@x", reason="r")

    assert outcome == {**outcome, "status": "PROMOTED", "operation_id": "op-1"}
    assert calls[0]["confirmation"] == "TOKEN"
    assert calls[0]["actor"] == "t@x"


def _chunk_response(total: int) -> DocumentChunkResponse:
    return DocumentChunkResponse(
        document_id="doc",
        chunk_job_id="job",
        chunk_set_id="set",
        vector_collection_id=None,
        collection_name="chunks",
        total_chunks=total,
        stored_chunks=total,
        stats=ChunkingStats(
            original_length=0, total_chunks=total, junk_removed=0, avg_chunk_size=0,
            min_chunk_size=0, max_chunk_size=0, content_type_distribution={},
        ),
    )


def test_auto_chunk_promotes_only_for_ui_reprocessing(monkeypatch):
    promoted = []
    monkeypatch.setattr(chunking_module, "chunk_document_and_store", lambda **_kwargs: _chunk_response(3))
    monkeypatch.setattr(
        chunking_module,
        "auto_promote_pending_lineage",
        lambda document_id, **kwargs: promoted.append((document_id, kwargs)) or {"status": "PROMOTED"},
    )
    monkeypatch.setattr(chunking_module.settings, "auto_promote_reprocessed_lineage", True)

    chunking_module.auto_chunk_extracted_document("doc")
    assert promoted == []

    chunking_module.auto_chunk_extracted_document("doc", promote_actor="t@x")
    assert promoted[0][0] == "doc"
    assert promoted[0][1]["actor"] == "t@x"

    monkeypatch.setattr(chunking_module.settings, "auto_promote_reprocessed_lineage", False)
    chunking_module.auto_chunk_extracted_document("doc", promote_actor="t@x")
    assert len(promoted) == 1


def test_auto_chunk_failure_marks_chunking_failed(monkeypatch):
    statuses = []

    def failing(**_kwargs):
        raise ValueError("quality_failed: no chunks generated")

    monkeypatch.setattr(chunking_module, "chunk_document_and_store", failing)
    monkeypatch.setattr(
        chunking_module,
        "update_chunking_status",
        lambda document_id, **kwargs: statuses.append((document_id, kwargs)),
    )

    assert chunking_module.auto_chunk_extracted_document("doc") is None
    assert statuses == [("doc", {"status": "failed", "error_message": "quality_failed: no chunks generated"})]
