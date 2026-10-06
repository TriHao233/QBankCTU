"""Contract tests; no live OCR, Docker image download or database writes."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

from modules.documents.ingest.models import ParseContext
from modules.documents.ingest.parsers import pdf
from modules.ocr import docling_engine


class DoclingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.pdf'
        self.context = ParseContext(document_id='test-only', source_file_name='fixture.pdf', source_uri=str(self.path), mime_type='application/pdf', document_type='pdf')
        self.prose = 'Tai lieu hoc phan giai thich cau truc du lieu va thuat toan. ' * 12

    def fixture(self, kinds):
        writer = PdfWriter()
        for kind in kinds:
            page = writer.add_blank_page(width=595, height=842)
            page[NameObject('/TestKind')] = TextStringObject(kind)
        with self.path.open('wb') as stream:
            writer.write(stream)

    def parse(self, kinds, failure=False, blank_ocr=False):
        self.fixture(kinds)
        selected = []

        def candidates(page):
            kind = str(page['/TestKind'])
            text = '' if kind == 'scan' else self.prose + (' TABLE' if kind == 'table' else '')
            quality = pdf.text_quality_metrics(text)
            quality['test_usable'] = kind != 'scan'
            return [{'method': 'pypdf_plain', 'space_width': None, 'text': text, 'quality': quality, 'score': 1 if text else 0}]

        def extract(path, pages, context):
            selected.extend(pages)
            if failure:
                raise ConnectionError('simulated unavailable Docling')
            text = '' if blank_ocr else self.prose
            return {number: {'text': text, 'structured_blocks': [], 'formula_blocks': []} for number in pages}

        with patch.object(pdf, '_candidate_texts', candidates), patch.object(pdf, '_text_is_usable', lambda quality: quality.get('test_usable', bool(quality.get('word_count')))), patch.object(pdf, '_has_table_signature', lambda text: 'TABLE' in text), patch.object(pdf, '_image_objects', lambda resources: []):
            parsed = pdf.PdfParser(ocr_page_extractor=extract).parse(self.path, self.context)
        self.assertEqual([unit.page_number for unit in parsed.units], list(range(1, len(kinds) + 1)))
        return selected, parsed

    def test_native_text_does_not_call_docling(self):
        selected, _ = self.parse(['plain', 'plain'])
        self.assertEqual(selected, [])

    def test_docling_processes_scan_and_layout_only(self):
        selected, parsed = self.parse(['plain', 'scan', 'table'])
        self.assertEqual(selected, [2, 3])
        self.assertEqual(parsed.stats['docling_page_count'], 2)
        self.assertEqual(parsed.stats['layout_page_count'], 1)
        self.assertIn('docling', parsed.units[1].raw_extraction)

    def test_docling_failure_retains_native_text_but_rejects_scan(self):
        _, parsed = self.parse(['plain', 'table', 'scan'], failure=True)
        self.assertEqual(parsed.units[1].quality['status'], 'passed_with_warning')
        self.assertEqual(parsed.units[2].quality['status'], 'quality_failed')
        self.assertEqual(parsed.units[1].raw_text, self.prose + ' TABLE')

    def test_scan_page_with_no_ocr_text_is_blank_and_retrieval_excluded(self):
        _, parsed = self.parse(['plain', 'scan'], blank_ocr=True)
        quality = parsed.units[1].quality
        self.assertEqual(quality['status'], 'passed_with_warning')
        self.assertTrue(quality['retrieval_excluded'])
        self.assertTrue(quality['blank_page'])

    def test_default_adapter_does_not_fallback_on_docling_failure(self):
        with patch.object(docling_engine, 'ocr_pdf_pages', side_effect=ConnectionError('offline')):
            with self.assertRaises(ConnectionError):
                pdf._default_ocr_page_extractor(self.path, [1], self.context)

    def test_docling_subset_maps_source_pages_and_blocks(self):
        self.fixture(['plain', 'plain', 'plain'])
        response = {'pages': [{'page_number': 1, 'text': 'first'}, {'page_number': 2, 'text': 'third'}], 'structured_blocks': [{'page_number': 2, 'block_type': 'code', 'content': 'x = 1;'}]}
        with patch.object(docling_engine, 'ocr_pdf', return_value=response):
            result = docling_engine.ocr_pdf_pages(str(self.path), [3, 1, 3])
        self.assertEqual(sorted(result), [1, 3])
        self.assertEqual(result[3]['text'], 'third')
        self.assertEqual(result[3]['structured_blocks'][0]['page_number'], 3)

    def test_tesseract_request_uses_vietnamese_language_code(self):
        settings = SimpleNamespace(docling_ocr_preset='tesseract', docling_ocr_backend='onnxruntime', docling_ocr_languages=['vie'])
        with patch.object(docling_engine, 'settings', settings):
            fields, _ = docling_engine._ocr_form_data()
        self.assertIn(('ocr_preset', 'tesseract'), fields)
        self.assertIn(('ocr_lang', 'vie'), fields)


if __name__ == '__main__':
    unittest.main()
