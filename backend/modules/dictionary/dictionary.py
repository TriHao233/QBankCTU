import json
import logging

import httpx
from bson import ObjectId

from core.config import settings
from core.database import get_rag_db
from modules.documents.store import get_document_repository
from modules.dictionary.service import add_pending_keywords

logger = logging.getLogger(__name__)

KEYWORD_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["keywords"],
    "additionalProperties": False,
}


def _parse_keywords(raw_text: str) -> list[str] | None:
    cleaned = str(raw_text or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    try:
        parsed = json.loads(cleaned.strip())
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        parsed = parsed.get("keywords")
    if not isinstance(parsed, list):
        return None
    return [keyword.strip() for keyword in parsed if isinstance(keyword, str) and keyword.strip()]


def _document_page_query(document_id: str) -> dict:
    try:
        return {"document_id": ObjectId(document_id)}
    except Exception:
        return {"document_id": document_id}


async def run_dictionary_auto_learning(document_id: str, course_id: str):
    """
    Hàm phân tích tài liệu ngầm: Trích xuất các đoạn văn đặc sắc,
    gọi AI để học từ khóa mới và đưa vào vùng chờ duyệt.
    """
    logger.info(f"==> Bắt đầu tiến trình Auto-Learning cho Document: {document_id}")
    db = get_rag_db()

    # 1. Lấy tối đa 15 trang V2 để làm mẫu học từ khóa.
    pages_cursor = get_document_repository(db).list_pages(document_id, limit=15)

    sample_texts = []
    for page in pages_cursor:
        text = (page.get("cleaned_text") or page.get("raw_text") or "").strip()
        if len(text) > 200:  # Bỏ qua trang quá ngắn như trang bìa, mục lục trống
            sample_texts.append(text[:1500])  # Giới hạn ký tự mỗi trang để tránh tràn Context Window bừa bãi

    if not sample_texts:
        logger.warning("Không tìm thấy văn bản đủ điều kiện để học từ khóa.")
        return

    combined_text = "\n--- TRANG MẪU ---\n".join(sample_texts)

    # 2. Xây dựng cấu trúc Prompt chuyên sâu ép cấu trúc JSON đầu ra
    prompt = f"""
Bạn là chuyên gia xử lý ngôn ngữ tự nhiên (NLP) hỗ trợ xây dựng từ điển thuật ngữ cho một học phần.
Nhiệm vụ của bạn là đọc kỹ đoạn văn bản trích dẫn từ giáo trình dưới đây và trích xuất ra các "Thuật ngữ chuyên ngành cốt lõi" của chính lĩnh vực mà giáo trình trình bày.

Mục tiêu của các từ khóa này là giúp hệ thống RAG đo lường chính xác mật độ tri thức của văn bản.

Yêu cầu trích xuất:
1. Từ khóa phải là thuật ngữ chuyên môn xuất hiện trong văn bản, không suy diễn từ kiến thức bên ngoài.
2. Chấp nhận cả thuật ngữ tiếng Việt và thuật ngữ tiếng Anh chuẩn của lĩnh vực.
3. KHÔNG lấy các từ ngữ đời sống phổ thông, động từ chung chung hoặc từ nối (ví dụ: "sử dụng", "thực hiện", "quan trọng").
4. Kết quả TRẢ VỀ DUY NHẤT một JSON object có trường "keywords" là mảng chuỗi; không thêm markdown hay giải thích.

VĂN BẢN GIÁO TRÌNH:
{combined_text}

JSON KẾT QUẢ ĐÚNG FORMAT MẪU:
{{"keywords": ["từ khóa 1", "từ khóa 2", "từ khóa 3"]}}
"""


    try:
        # 3. Sử dụng factory chung của hệ thống để gọi LLM
        from modules.generation.llm.factory import get_llm_service
        llm = get_llm_service()

        raw_ai_text = await llm.generate_chat(
            system_prompt="Trích xuất thuật ngữ chuyên môn. Chỉ trả về JSON đúng schema được cung cấp.",
            user_prompt=prompt,
            output_schema=KEYWORD_OUTPUT_SCHEMA,
        )
        extracted_keywords = _parse_keywords(raw_ai_text)

        if extracted_keywords is not None:
            logger.info(f"AI đã học được {len(extracted_keywords)} từ khóa tiềm năng: {extracted_keywords}")

            # 4. Đẩy vào MongoDB vùng chờ duyệt (Pending)
            if extracted_keywords:
                add_pending_keywords(course_id=course_id, keywords=extracted_keywords)
                logger.info("Đã cập nhật bộ từ khóa vào trạng thái Chờ duyệt (Pending) thành công!")
        else:
            logger.warning("AI không trả về danh sách keywords hợp lệ; bỏ qua lần học từ khóa này.")
    except Exception as e:
        logger.exception(f"Tiến trình Auto-Learning thất bại do sự cố: {str(e)}")
