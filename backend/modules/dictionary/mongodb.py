from datetime import datetime

from core.database import get_rag_db
from modules.dictionary.constants import DICTIONARY_CATEGORY, DICTIONARY_NAME


def _dictionaries_collection():
    return get_rag_db()["dictionaries"]


def init_default_dictionary(course_id: str):
    """Khởi tạo từ điển rỗng của học phần nếu chưa tồn tại"""
    collection = _dictionaries_collection()
    existing = collection.find_one({"course_id": course_id})
    if not existing:
        default_dict = {
            "course_id": course_id,
            "name": DICTIONARY_NAME,
            "category": DICTIONARY_CATEGORY,
            "is_active": True,
            "core_keywords": [],
            "learned_keywords": [],
            "pending_keywords": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        collection.insert_one(default_dict)


def get_active_keywords(course_id: str) -> list[str]:
    """Lấy toàn bộ từ khóa active (Core + Learned) để phục vụ tính toán Density"""
    collection = _dictionaries_collection()
    # Khởi tạo nếu trống
    init_default_dictionary(course_id)

    dict_doc = collection.find_one({"course_id": course_id, "is_active": True})
    if not dict_doc:
        return []

    core = dict_doc.get("core_keywords", [])
    learned = dict_doc.get("learned_keywords", [])

    # Gộp lại và chuẩn hóa viết thường
    all_kws = list(set(core + learned))
    return [str(kw).lower().strip() for kw in all_kws if kw]


def add_pending_keywords(course_id: str, keywords: list[str]):
    """Đẩy các từ khóa mới do AI tự học vào vùng chờ duyệt (Pending)"""
    collection = _dictionaries_collection()
    # Lấy từ điển hiện tại để tránh trùng với từ đã có
    dict_doc = collection.find_one({"course_id": course_id})
    if not dict_doc:
        return

    existing_core = set([k.lower() for k in dict_doc.get("core_keywords", [])])
    existing_learned = set([k.lower() for k in dict_doc.get("learned_keywords", [])])
    existing_pending = set([k.lower() for k in dict_doc.get("pending_keywords", [])])

    new_filtered = []
    for kw in keywords:
        kw_clean = kw.strip().lower()
        if (kw_clean and
            kw_clean not in existing_core and
            kw_clean not in existing_learned and
            kw_clean not in existing_pending):
            new_filtered.append(kw.strip())  # Giữ nguyên format chữ để hiển thị

    if new_filtered:
        collection.update_one(
            {"course_id": course_id},
            {
                "$push": {"pending_keywords": {"$each": new_filtered}},
                "$set": {"updated_at": datetime.utcnow()}
            }
        )
