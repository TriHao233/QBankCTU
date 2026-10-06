import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId

from modules.dictionary import mongodb
from modules.dictionary.dictionary import _parse_keywords, run_dictionary_auto_learning
from modules.dictionary.service import dictionary_key_for_document, get_active_keywords


class KeywordOutputTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_accepts_schema_and_legacy_array(self):
        self.assertEqual(_parse_keywords('{"keywords": [" con trỏ ", "", 7]}'), ["con trỏ"])
        self.assertEqual(_parse_keywords('["mảng động"]'), ["mảng động"])
        self.assertIsNone(_parse_keywords('{"explanation": "không có danh sách"}'))

    async def test_auto_learning_saves_only_valid_keyword_list(self):
        database = MagicMock()
        database.document_pages.find.return_value.sort.return_value.limit.return_value = [
            {"cleaned_text": "Giải thuật và cấu trúc dữ liệu. " * 12}
        ]
        provider = MagicMock()
        provider.generate_chat = AsyncMock(return_value='{"keywords": ["con trỏ", "mảng động"]}')
        with (
            patch("modules.dictionary.dictionary.get_rag_db", return_value=database),
            patch("modules.generation.llm.factory.get_llm_service", return_value=provider),
            patch("modules.dictionary.dictionary.add_pending_keywords") as save,
        ):
            await run_dictionary_auto_learning("507f1f77bcf86cd799439011", "subject-1")
            save.assert_called_once_with(
                course_id="subject-1", keywords=["con trỏ", "mảng động"]
            )
            self.assertNotIn("Công nghệ thông tin", provider.generate_chat.call_args.kwargs["user_prompt"])
            self.assertEqual(provider.generate_chat.call_args.kwargs["output_schema"]["required"], ["keywords"])

            provider.generate_chat.return_value = '{"explanation": "không có danh sách"}'
            save.reset_mock()
            await run_dictionary_auto_learning("507f1f77bcf86cd799439011", "subject-1")
            save.assert_not_called()


class SubjectDictionaryTests(unittest.TestCase):
    def test_dictionary_is_keyed_by_document_subject(self):
        self.assertEqual(dictionary_key_for_document({"subject_id": ObjectId("507f1f77bcf86cd799439011")}),
                         "507f1f77bcf86cd799439011")
        self.assertIsNone(dictionary_key_for_document({"subject_id": None}))
        self.assertIsNone(dictionary_key_for_document(None))

    def test_document_without_subject_uses_no_keywords(self):
        with patch("modules.dictionary.service._store") as store:
            self.assertEqual(get_active_keywords(None), [])
            store.assert_not_called()

    def test_new_subject_dictionary_starts_without_seeded_vocabulary(self):
        collection = MagicMock()
        collection.find_one.return_value = None
        with patch("modules.dictionary.mongodb._dictionaries_collection", return_value=collection):
            mongodb.init_default_dictionary("subject-1")
        inserted = collection.insert_one.call_args.args[0]
        self.assertEqual(inserted["course_id"], "subject-1")
        self.assertEqual(inserted["core_keywords"], [])
