# Dataset đánh giá model thẩm định câu hỏi

Dataset dùng để đánh giá pipeline/model đánh giá chất lượng câu hỏi (không phải OCR corpus).

## Nguồn và phạm vi

- File nhãn: `evaluation_benchmark_cases.json`
- Runner: `backend/scripts/manual/evaluation_benchmark.py`
- Lĩnh vực: học phần **Cấu trúc dữ liệu** (queue, stack, BST, binary search, linked list, hash table, graph)
- Nhãn: `PASS` (nên duyệt) / `BLOCK` (nên chặn)

## Thiết kế khoa học tối thiểu

1. **Cặp đối chứng**: mỗi nhóm kiến thức có case đúng và case lỗi (đáp án sai, hallucination, giải thích mâu thuẫn nguồn).
2. **Phủ loại câu hỏi**: MCQ, đúng/sai, điền khuyết, nhiều lựa chọn, ghép cột, tình huống, sắp xếp.
3. **Phủ Bloom**: nhớ / hiểu / vận dụng.
4. **Bám nguồn**: mỗi case gắn `source` trích đoạn; model đánh giá chỉ được dùng ngữ cảnh được inject.
5. **CLO**: một số case gắn mô tả CLO để kiểm tiêu chí `clo_alignment`.

## Cách dùng để chuyển version policy

1. Chạy benchmark trên policy/model hiện tại.
2. Ghi false-approve / false-block.
3. Nếu điều chỉnh weights/thresholds làm giảm false-approve mà không tăng false-block vượt ngưỡng chấp nhận, tạo `evaluation_policies` version mới kèm `change_reason` (xem `docs/EVALUATION_POLICY_VERSIONS.md`).

## Hạn chế hiện tại

- Nhãn do nhóm đề tài gán (chưa có đa giám định / inter-rater).
- Quy mô còn nhỏ; phù hợp kiểm chứng kỹ thuật và biện minh chuyển version nội bộ đề tài, chưa phải corpus xuất bản.
