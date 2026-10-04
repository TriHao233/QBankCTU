"""Chuẩn đầu ra CLO tham chiếu cho học phần Cấu trúc dữ liệu (CTĐT).

Bộ CLO bám khung kiến thức–kỹ năng–thái độ thường dùng trong đề cương CTDL
cấp đại học CNTT. Admin/GV có thể chỉnh sửa sau khi seed.
"""

from __future__ import annotations

CTDL_SUBJECT_CODES = ("CTDL", "CTDL1", "CT101")
CTDL_SUBJECT_NAME_HINTS = ("cấu trúc dữ liệu", "cau truc du lieu", "data structures")

# outcome_type: kien_thuc | ky_nang | thai_do (theo phân loại CLO trong CTĐT)
CTDL_LEARNING_OUTCOMES: tuple[dict, ...] = (
    {
        "clo_code": "CLO1",
        "description": "Trình bày các khái niệm, đặc trưng và thao tác cơ bản của các cấu trúc dữ liệu tuyến tính (mảng, danh sách liên kết, ngăn xếp, hàng đợi).",
        "outcome_type": "kien_thuc",
        "target_weight": 0.15,
    },
    {
        "clo_code": "CLO2",
        "description": "Giải thích nguyên lý và độ phức tạp của các cấu trúc dữ liệu phi tuyến (cây, cây nhị phân tìm kiếm, bảng băm, đồ thị).",
        "outcome_type": "kien_thuc",
        "target_weight": 0.15,
    },
    {
        "clo_code": "CLO3",
        "description": "So sánh ưu nhược điểm của các cấu trúc dữ liệu và lựa chọn cấu trúc phù hợp cho bài toán cụ thể.",
        "outcome_type": "kien_thuc",
        "target_weight": 0.10,
    },
    {
        "clo_code": "CLO4",
        "description": "Cài đặt và thao tác đúng trên các cấu trúc dữ liệu tuyến tính bằng ngôn ngữ lập trình.",
        "outcome_type": "ky_nang",
        "target_weight": 0.15,
    },
    {
        "clo_code": "CLO5",
        "description": "Thiết kế và cài đặt thuật toán trên cây, bảng băm và đồ thị; phân tích độ phức tạp thời gian/không gian.",
        "outcome_type": "ky_nang",
        "target_weight": 0.15,
    },
    {
        "clo_code": "CLO6",
        "description": "Áp dụng cấu trúc dữ liệu để giải quyết bài toán thực tế (tìm kiếm, sắp xếp, duyệt, tối ưu bộ nhớ).",
        "outcome_type": "ky_nang",
        "target_weight": 0.15,
    },
    {
        "clo_code": "CLO7",
        "description": "Đánh giá tính đúng đắn, hiệu quả và khả năng bảo trì của giải pháp dùng cấu trúc dữ liệu.",
        "outcome_type": "thai_do",
        "target_weight": 0.10,
    },
    {
        "clo_code": "CLO8",
        "description": "Tuân thủ quy ước lập trình, tài liệu hóa và làm việc nhóm khi xây dựng module cấu trúc dữ liệu.",
        "outcome_type": "thai_do",
        "target_weight": 0.05,
    },
)


def is_ctdl_subject(subject: dict) -> bool:
    code = str(subject.get("subject_code") or "").strip().upper()
    if code in {item.upper() for item in CTDL_SUBJECT_CODES}:
        return True
    name = str(subject.get("subject_name") or "").strip().lower()
    return any(hint in name for hint in CTDL_SUBJECT_NAME_HINTS)
