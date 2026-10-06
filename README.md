# QBankCTU — Ứng dụng Web Quản lý Ngân hàng Câu hỏi tích hợp LLM

> **Đề tài NCKH cấp cơ sở, Trường CNTT&TT — Đại học Cần Thơ**
> Thời gian thực hiện: 03/2026 – 08/2026
> Chủ nhiệm đề tài: Trương Trí Hào (B2203553) — GVHD: TS. Phan Phương Lan, KS. Trương Phúc Vĩnh

**QBankCTU** là hệ thống Web hỗ trợ quản lý và tự động hóa quy trình soạn thảo ngân hàng câu hỏi trắc nghiệm từ tài liệu giảng dạy. Hệ thống tận dụng sức mạnh của các mô hình ngôn ngữ lớn (LLM) mã nguồn mở chạy local, kết hợp với kỹ thuật **RAG (Retrieval-Augmented Generation)** để đảm bảo câu hỏi sinh ra luôn bám sát tài liệu gốc, loại bỏ tình trạng ảo giác (hallucination).
Với cơ chế **Human-in-the-loop**, AI đóng vai trò trợ lý sinh câu hỏi nháp, còn giảng viên là người kiểm duyệt, chỉnh sửa và phê duyệt cuối cùng trước khi đồng bộ trực tiếp lên hệ thống Moodle của trường.

---

## Hướng dẫn cài đặt và chạy dự án (Getting Started)

### 1. Yêu cầu hệ thống

- **OS:** Windows, Linux, hoặc macOS.
- **Phần mềm:**
  - Docker Desktop (hoặc Docker Engine + Docker Compose).
  - Git.
  - **Ollama**: Phải cài đặt trực tiếp trên máy host để chạy LLM cục bộ (Local). Bạn cần kéo các model như `qwen3-8b` hoặc `deepseek-r1` về máy trước (`ollama run qwen3-8b`).
  - (Tùy chọn) Node.js và Python 3.10+ nếu muốn chạy code local không thông qua Docker.
- **Phần cứng:** Khuyến nghị có NVIDIA GPU và cài đặt NVIDIA Container Toolkit để tăng tốc model OCR Docling (có thể cấu hình chạy CPU nhưng tốc độ sẽ rất chậm).

### 2. Cài đặt biến môi trường và Firebase

1. Clone dự án về máy: `git clone https://github.com/TriHao233/QBankCTU.git`
2. Di chuyển vào thư mục dự án: `cd QBankCTU`
3. Cấu hình Backend:
   - Copy file `backend/.env.example` thành `backend/.env` và điền các thông số (như `POSTGRES_DSN`, URL DB).
   - Đặt file `firebase-service-account.json` vào trong thư mục `backend/` để backend có thể xác thực người dùng.
4. Cấu hình Frontend:
   - Copy file `frontend/.env.example` thành `frontend/.env`.
   - Cấu hình các biến Vite (như `VITE_DEMO_MODE=true` nếu muốn bật chế độ demo).

### 3. Khởi chạy bằng Docker (Khuyên dùng)

Đây là cách đơn giản nhất để chạy toàn bộ hệ thống (Frontend, Backend, PostgreSQL, MongoDB, Worker):

```bash
docker compose up -d --build
```

Sau khi các container đã khởi động thành công, bạn có thể truy cập:

- **Giao diện Web (Frontend):** [http://localhost:5177](http://localhost:5177)
- **API Docs (Backend):** [http://localhost:8000/docs](http://localhost:8000/docs)

*(Để dừng hệ thống, chạy `docker compose down`. Nếu chỉ muốn khởi động lại mà không build, dùng `docker compose up -d`)*

### 4. Khởi chạy Development (Chạy local)

Dành cho mục đích code và debug:

**Khởi chạy Backend (FastAPI):**

```bash
cd backend
# Tạo file .env và cấu hình như hướng dẫn ở trên
python scripts/database/bootstrap_v2.py
python -m db.migrate --apply  # Chạy DB migration cho PostgreSQL
uvicorn main:app --reload
```

**Khởi chạy Frontend (React/Vite):**

```bash
cd frontend
npm install
npm run dev
```

---

## 1. Bài toán & lý do làm

Việc soạn ngân hàng câu hỏi tại các cơ sở giáo dục hiện chủ yếu làm **thủ công**: tốn thời gian, chất lượng không đồng đều, thiếu câu hỏi ở mức tư duy cao. Các mô hình ngôn ngữ lớn (LLMs) có thể tự động sinh câu hỏi từ tài liệu, nhưng rào cản lớn nhất là **hallucination** — AI bịa thông tin nghe có vẻ đúng.

**Giải pháp:** xây dựng một hệ thống Web kết hợp:

- **RAG (Retrieval-Augmented Generation)** để LLM sinh câu hỏi bám sát nội dung tài liệu thật, giảm ảo giác.
- **Human-in-the-loop**: AI chỉ tạo câu hỏi *nháp*, giảng viên luôn là người kiểm duyệt/chỉnh sửa/phê duyệt cuối cùng trước khi xuất bản.

## 2. Mục tiêu

**Mục tiêu tổng quát:** Xây dựng ứng dụng Web quản lý ngân hàng câu hỏi, dùng LLM hỗ trợ sinh câu hỏi tự động từ tài liệu văn bản, với quy trình kiểm duyệt chặt chẽ trước khi đưa vào sử dụng chính thức.

**Mục tiêu cụ thể:**

1. Nền tảng quản lý ngân hàng câu hỏi tập trung, số hóa và có tổ chức.
2. Tự động hóa sinh đề bằng LLM + RAG, nhanh hơn nhiều lần so với thủ công.
3. Đảm bảo chất lượng nội dung qua cơ chế Human-in-the-loop (AI khởi tạo → giảng viên thẩm định).
4. Kiểm chứng tính khả thi của LLM **chạy local** (chi phí thấp, bảo mật cao) cho bài toán giáo dục chuyên ngành.
5. Đồng bộ dữ liệu câu hỏi sang **Moodle** (LMS của trường) thông qua Plugin.

## 3. Phạm vi

| Hạng mục              | Phạm vi                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------- |
| Ngôn ngữ tài liệu   | Tiếng Việt                                                                                      |
| Lĩnh vực nội dung    | Học phần "Cấu trúc dữ liệu" (Data Structures)                                               |
| Định dạng đầu vào | PDF, DOC (kể cả PDF dạng scan → cần OCR)                                                     |
| Loại câu hỏi         | Trắc nghiệm 4 lựa chọn (MCQ), Đúng/Sai, Điền khuyết, Ghép đôi, Câu hỏi tình huống |
| Mô hình AI            | LLM mã nguồn mở, chạy **local** (ứng viên: Qwen, Llama...)                           |
| Kỹ thuật lõi         | RAG (truy xuất ngữ cảnh từ tài liệu để giảm ảo giác)                                   |
| Đầu ra                | Lưu trực tiếp vào Question Bank của Moodle qua Plugin                                        |

## 4. Kiến trúc hệ thống & tech stack hiện tại

```
QBankCTU/
├── frontend/                  # React + Vite — Admin Dashboard
│   └── src/
│       ├── pages/             # HomePage, LoginPage, RegisterPage, GeneratePage,
│       │                      # ManagePage, GuidePage, AboutPage, ContactPage, UserProfile
│       ├── components/        # Header, Footer, Layout, UserProfileMenu
│       └── context/           # AuthContext (Firebase Auth)
│
├── backend/                   # FastAPI — 1 app duy nhất (auth + AI core)
│   ├── main.py                 # Entrypoint duy nhất: FastAPI(), CORS, router mount
│   ├── core/                   # config, database (Mongo/Firebase), security, logging — dùng chung
│   ├── common/                 # exceptions, responses, utils dùng chung
│   ├── modules/                # Feature-based, mỗi module theo router → service → repository
│   │   ├── auth/                # register / login / profile (Firebase Auth)
│   │   ├── ocr/                  # Docling OCR/layout chọn lọc cho trang PDF scan, bảng, hình
│   │   ├── rag/                   # chunking, chromadb vector store, search
│   │   ├── generation/            # sinh câu hỏi, prompt builder, LLM factory (gemini/qwen3/deepseek)
│   │   └── dictionary/             # auto-learning từ khóa
│   ├── prompts/                # system.txt, bloom/, question_type/, examples/
│   └── data/                   # uploads, ocr_outputs, chunk_outputs, metadata, chroma_data
```

**Stack:**

- **Frontend:** React 18 + Vite, React Router, Firebase SDK (auth), FontAwesome.
- **Backend:** 1 FastAPI app duy nhất (`uvicorn main:app --reload`), Firebase Admin (xác thực người dùng).
  - OCR: Docling Serve (container riêng, Tesseract tiếng Việt `vie`), chỉ xử lý các trang PDF scan hoặc có bảng/hình;
  - Vector DB: `chromadb` + `sentence-transformers` (embedding & retrieval cho RAG).
  - LLM: `google-genai` hiện dùng để thử nghiệm (mục tiêu cuối là LLM local qua PyTorch CUDA — `torch`/`torchvision`/`torchaudio` đã có trong requirements).
  - Lưu trữ dữ liệu: PostgreSQL (`psycopg`) cho dữ liệu nghiệp vụ, MongoDB (`pymongo`) cho chunk/vector RAG — đang trong giai đoạn chuyển đổi
  - File (tài liệu gốc, artifact OCR, avatar): thư mục local hoặc object storage tương thích S3 (`boto3`).
  - Prompt được tổ chức theo **thang đo BLOOM** và theo **loại câu hỏi** (`prompts/bloom/`, `prompts/question_type/`).

## 5. Flow hoạt động dự kiến (end-to-end)

```
1. Giảng viên đăng nhập (Firebase Auth) → vào Admin Dashboard
2. Upload tài liệu (PDF/DOC) — môn Cấu trúc dữ liệu
        │
        ▼
3. Tiền xử lý tài liệu
   - Nếu là PDF scan hoặc trang có bảng/hình → Docling trích xuất văn bản và bố cục; trang có text layer tốt giữ nguyên văn bản gốc
   - Làm sạch, chuẩn hóa văn bản
        │
        ▼
4. Chunking — chia văn bản thành đoạn nhỏ, sinh embedding
   → lưu vào Vector DB (ChromaDB) — đây là "kho tri thức" cho RAG
        │
        ▼
5. Sinh câu hỏi (Generation)
   - Truy xuất (retrieve) đoạn liên quan từ ChromaDB theo chủ đề
   - Ghép prompt (system + BLOOM level + loại câu hỏi + ngữ cảnh truy xuất)
   - Gọi LLM → sinh câu hỏi nháp (MCQ / True-False / Fill-in-blank / Matching / Scenario)
        │
        ▼
6. Human-in-the-loop — Question Editor (giảng viên)
   - Xem, chỉnh sửa nội dung, đáp án, độ khó (BLOOM)
   - Duyệt (approve) hoặc từ chối
        │
        ▼
7. Câu hỏi đã duyệt → lưu vào Ngân hàng câu hỏi (Question Bank nội bộ)
        │
        ▼
8. Xuất bản / đồng bộ sang Moodle
   - Chuyển đổi dữ liệu sang định dạng chuẩn Moodle (nội dung, đáp án, xáo trộn)
   - Plugin Moodle (PHP + Moodle Database API) nhận và lưu vào Question Bank của Moodle
```

**Vòng đời một câu hỏi:** `Soạn thảo (AI draft) → Phản biện → Duyệt → Xuất bản (Moodle)`

## 6. Chức năng chính (theo nhóm người dùng)

**Admin:**

- Quản lý người dùng, phân quyền (Admin / Giảng viên).
- Quản lý học phần, cấu hình tham số AI (model, prompt, RAG settings).

**Giảng viên:**

- Upload tài liệu nguồn (`GeneratePage`) → kích hoạt pipeline sinh câu hỏi.
- Chỉnh sửa câu hỏi bằng Question Editor.
- Quản lý ngân hàng câu hỏi cá nhân/môn học (`ManagePage`).
- Duyệt & xuất bản câu hỏi sang Moodle.
- Quản lý hồ sơ cá nhân (`UserProfile`).

Các trang frontend hiện có khớp với các chức năng trên: `HomePage`, `LoginPage`/`RegisterPage` (auth), `GeneratePage` (sinh câu hỏi từ tài liệu), `ManagePage` (quản lý ngân hàng câu hỏi), `GuidePage`, `AboutPage`, `ContactPage`.

## 7. Tiêu chí chất lượng câu hỏi

- Phân loại theo **thang đo BLOOM**: Nhận biết – Thông hiểu – Vận dụng – ...
- Đúng kiến thức nguồn (nhờ RAG bám tài liệu), văn phong tự nhiên tiếng Việt.
- Đúng định dạng nhập liệu Moodle (đáp án, thiết lập xáo trộn, hiển thị tiếng Việt ổn định).

## 8. Timeline nghiên cứu

| Giai đoạn | Thời gian | Nội dung |
| ------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------ |
| 1. Thu thập dữ liệu & nghiên cứu công nghệ | 03/2026 | Tìm hiểu LLM/RAG, thu thập giáo trình Cấu trúc dữ liệu, tiền xử lý (clean/OCR) |
| 2. Phân tích & thiết kế | 03–04/2026 | Đặc tả yêu cầu, thiết kế DB & Moodle mapping, thiết kế UI Dashboard |
| 3. Module AI (Core) | 04–05/2026 | Pipeline Đọc tài liệu → Chunking → Embedding → LLM Generation; tối ưu prompt |
| 4. Backend & tích hợp Moodle | 05–06/2026 | API CRUD, Plugin Moodle |
| 5. Frontend (Admin Dashboard) | 06–07/2026 | Giao diện quản lý ngân hàng câu hỏi, Question Editor |
| 6. Kiểm thử & hoàn thiện | 07–08/2026 | Kiểm thử chức năng + kiểm thử chất lượng AI, báo cáo, video demo |

## 9. Sản phẩm bàn giao

- Hệ thống Web quản lý & sinh câu hỏi trắc nghiệm (Admin Dashboard).
- Plugin tích hợp Moodle (đồng bộ câu hỏi tự động, đúng chuẩn Moodle).
- Mã nguồn hoàn chỉnh Backend + Moodle Plugin, tài liệu hướng dẫn cài đặt/tích hợp.
- Báo cáo tổng kết + video demo quy trình (≤ 2 phút).

## 10. Định hướng kỹ thuật cần lưu ý khi code tiếp

- **RAG là xương sống chống hallucination** — mọi câu hỏi sinh ra phải truy xuất ngữ cảnh từ ChromaDB trước khi gọi LLM, không sinh "chay" từ kiến thức nội tại của model.
- **LLM mục tiêu là chạy local/mã nguồn mở** (Qwen, Llama...) — `google-genai` trong requirements hiện tại là phương án thử nghiệm/tạm thời, không phải đích cuối.
- **OCR chỉ cần khi tài liệu là PDF scan** (ảnh) — tài liệu PDF text thuần không cần qua OCR.
- **Không được auto-publish câu hỏi AI sinh ra** — luôn phải qua bước duyệt của giảng viên (human-in-the-loop) trước khi vào Moodle.
- **Chuẩn hóa output theo Moodle**: đáp án, thiết lập xáo trộn, mã hóa tiếng Việt phải đúng ngay từ bước Generation để Plugin xuất bản không cần xử lý lại.
- Backend đã hợp nhất thành 1 FastAPI app duy nhất (`backend/main.py`), tổ chức theo Feature-based Modular Architecture (`core/`, `common/`, `modules/{auth,ocr,rag,generation,dictionary}/`), mỗi module theo pattern router → service → repository.

## 11. CRUD V2, Firebase và API Gateway

Nhánh `fixed_CRUD` giữ cấu trúc feature-based của `full-dev` và bổ sung:

- `modules/users`: CRUD hồ sơ ứng dụng; Firebase quản lý identity/password.
- `modules/documents`: CRUD document aggregate và processing jobs.
- `modules/questions`: CRUD câu hỏi theo `question_versions`, optimistic locking và transaction.
- `core/bootstrap.py`: tự tạo collections, validators, indexes và seed khi FastAPI startup.
- Frontend dùng `src/services/apiClient.js`; Firebase ID token được gửi qua
  `Authorization: Bearer <token>`.

Sao chép `backend/.env.example` thành `backend/.env`, cấu hình `MONGO_URI`,
`AUTH_DB_NAME=NCKH`, `RAG_DB_NAME=rag_database` và đường dẫn Firebase service
account. `NCKH.User` chỉ lưu `uid` và Firebase token; hồ sơ/role đầy đủ để CRUD
nằm ở `rag_database.users`. Token do Firebase phát hành và xác minh, backend
không phát JWT riêng. MongoDB phải là replica set.
Trong workspace cũ, backend cũng tự fallback sang `rag-ocr-pipeline/.env` để
tái sử dụng URI MongoDB hiện có mà không sao chép secret vào Git.

```powershell
cd backend
python scripts/database/bootstrap_v2.py
python scripts/database/migrate_v2.py
# Sau khi kiểm tra dry-run:
python scripts/database/migrate_v2.py --apply
uvicorn main:app --reload
```

Frontend local gọi `/api/v1` qua Vite proxy:

```powershell
cd frontend
npm install
npm run dev
```

Khi deploy, đặt `VITE_API_BASE_URL` thành URL backend hoặc cấu hình gateway
reverse-proxy `/api`; đồng thời đặt `CORS_ORIGINS` bằng origin frontend và
`ALLOWED_HOSTS` bằng hostname backend.

Các CRUD endpoint:

- `/api/v1/users`
- `/api/v1/documents`
- `/api/v1/questions`
- `/api/v1/questions/{question_id}/versions`

Thiết kế dữ liệu chi tiết xem tại [`docs/DATABASE_DESIGN_V2.md`](docs/DATABASE_DESIGN_V2.md).

## 12. Refactor cơ sở dữ liệu: PostgreSQL + MongoDB

> **Tóm tắt nhanh:** Dữ liệu nghiệp vụ giờ **mặc định lưu ở PostgreSQL**, không cần bật cờ nào. MongoDB chỉ còn giữ chunk/vector cho RAG. **Sau khi pull code này, mỗi người phải làm một lần bước [Chuẩn bị PostgreSQL trên máy mình](#chuẩn-bị-postgresql-trên-máy-mình)** (tạo bảng + chép dữ liệu cũ từ MongoDB), nếu không backend sẽ không khởi động. Hãy đọc phần [Quy tắc khi viết code](#quy-tắc-khi-viết-code-mới) trước khi thêm dữ liệu hoặc collection mới.

### Vì sao đổi?

- **Nhiều thao tác phải thành công cùng lúc.** Ví dụ khi Reviewer duyệt câu hỏi: lưu quyết định, đổi trạng thái câu hỏi, dừng job AI, ghi audit và gửi thông báo. PostgreSQL gói tất cả vào một transaction: hoặc xong hết, hoặc không có gì thay đổi.
- **Ràng buộc dữ liệu chặt hơn.** Khóa ngoại chặn việc xóa môn học/tài liệu đang được câu hỏi dùng, hoặc câu hỏi trỏ tới người dùng không tồn tại.
- **Lọc và thống kê dễ hơn.** Hàng đợi Reviewer, dashboard, trang Admin là các truy vấn có điều kiện và đếm — thế mạnh của SQL.
- **Chuẩn bị lên cloud.** Chạy nhiều backend/worker cùng lúc; file lưu trên object storage thay vì ổ đĩa một máy.

### Dữ liệu nằm ở đâu sau khi đổi

| Loại dữ liệu | Nơi lưu |
| -------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Người dùng, quyền, học phần/chương/CLO, từ điển, cấu hình AI | PostgreSQL |
| Tài liệu, trang OCR, job OCR/chunk/index | PostgreSQL |
| Câu hỏi và các phiên bản, đánh giá AI, duyệt, bình luận, draft | PostgreSQL |
| Job sinh câu hỏi, job đánh giá, đề thi và mã đề | PostgreSQL |
| Thông báo, audit, cấu hình và lịch sử xuất Moodle | PostgreSQL |
| Chunk, chunk set, metadata embedding (dùng cho RAG) | **Vẫn ở MongoDB** |
| Vector tìm kiếm | **Vẫn ở ChromaDB** |
| File gốc, artifact OCR, avatar | Thư mục local, hoặc S3/MinIO/R2/GCS khi bật`STORAGE_PROVIDER=s3` |

ID vẫn là chuỗi ObjectId 24 ký tự như cũ, nên API và frontend **không đổi**.

### Công tắc bật/tắt (cờ trong `backend/.env`)

Mỗi nhóm dữ liệu có một cờ riêng, giá trị `postgres` (**mặc định**) hoặc `mongo`. Giá trị `mongo` chỉ dùng để **quay lui** khi có sự cố, không dùng cho công việc thường ngày. Đổi cờ xong phải khởi động lại **cả backend lẫn worker**.

| Cờ | Nhóm dữ liệu | Cần bật thêm |
| ----------------------- | ---------------------------------------------------- | ------------------------------------- |
| `USER_STORE` | Người dùng, phiên đăng nhập | — |
| `CATALOG_STORE` | Học phần, chương, CLO | `USER_STORE` |
| `DICTIONARY_STORE` | Từ điển từ khóa | — |
| `AI_CONFIG_STORE` | Model, prompt, policy đánh giá | — |
| `DOCUMENT_STORE` | Tài liệu, OCR, job tài liệu | `USER_STORE`, `CATALOG_STORE` |
| `QUESTION_STORE` | Câu hỏi, duyệt, đánh giá, publication Moodle | `USER_STORE`, `CATALOG_STORE` |
| `GENERATION_STORE` | Job/lịch sử sinh câu hỏi | `USER_STORE`, `DOCUMENT_STORE` |
| `EXAM_STORE` | Đề thi, mã đề | `QUESTION_STORE`, `CATALOG_STORE` |
| `NOTIFICATION_STORE` | Thông báo | `USER_STORE` |
| `AUDIT_STORE` | Nhật ký audit | — |
| `REVIEW_POLICY_STORE` | Chính sách duyệt 2 vòng | `USER_STORE` |
| `MOODLE_TARGET_STORE` | Cấu hình Moodle target | `USER_STORE` |
| `LLM_SLOT_STORE` | Giới hạn số lời gọi model đồng thời | — |
| `STORAGE_PROVIDER` | Nơi lưu**file mới**: `local` hoặc `s3` | `S3_BUCKET`, v.v. |

Thiếu cờ phụ thuộc thì backend báo lỗi ghi rõ cờ còn thiếu ngay khi tính năng đó được dùng tới. ⚠️ **Trên môi trường thật, bật tất cả cờ cùng một lúc theo runbook**, không bật lẻ từng cờ: các nhóm đọc dữ liệu của nhau, bật lệch sẽ thấy thiếu dữ liệu.

### Chuẩn bị PostgreSQL trên máy mình

Làm **một lần** sau khi pull. `docker compose up -d` đã có sẵn PostgreSQL 17 (`nckh-postgres`, cổng `127.0.0.1:5432`). Thêm dòng `POSTGRES_DSN` như trong `backend/.env.example` vào `backend/.env` (thiếu dòng này backend báo `POSTGRES_DSN is required`), rồi từ thư mục `backend`:

```powershell
# 1. Tạo bảng (chạy lại được, chỉ áp phần còn thiếu)
python -m db.migrate --apply
# 2. Xem sẽ chép bao nhiêu bản ghi từ MongoDB (không ghi gì)
python -m db.copy_business_data
# 3. Chép thật vào PostgreSQL, rồi đối soát hai bên
python -m db.copy_business_data --apply
python -m db.verify_business_data
```

Không cần đặt cờ nào. Nếu `backend/.env` cũ của bạn còn dòng `*_STORE=mongo` thì xóa đi. Khởi động lại backend và worker. Muốn quay lui thì đặt cả 13 cờ về `mongo`. Lưu ý: dữ liệu tạo trong lúc chạy PostgreSQL sẽ không có bên MongoDB.

Nếu máy bạn đã có database `nckh` từ lần thử trước (bảng cũ, thiếu migration), cách sạch nhất là tạo lại rồi làm lại 3 bước trên:

```powershell
docker exec nckh-postgres psql -U nckh -d postgres -c "DROP DATABASE nckh" -c "CREATE DATABASE nckh OWNER nckh"
```

### Chạy test

Test PostgreSQL chỉ chạy khi được bật rõ ràng, và phải trỏ tới **một database riêng để test** (test tự tạo rồi xóa dữ liệu của nó):

```powershell
cd backend
$env:PYTHONUTF8 = "1"   # Windows: tránh lỗi mã hóa khi đọc .env
$env:RUN_POSTGRES_INTEGRATION = "1"
$env:POSTGRES_DSN = "postgresql://nckh:nckh_local_dev_only@127.0.0.1:5432/nckh_test"
python -m db.migrate --apply
python -m pytest tests/test_postgres_*.py
python -m pytest tests/test_schema_v2.py tests/test_s3_artifact_storage.py
```

Test S3 dùng thư viện `moto` để giả lập, không cần tài khoản cloud.

Unit test thường vẫn chạy nhánh MongoDB với dữ liệu giả trong bộ nhớ: `backend/tests/conftest.py` đặt mọi cờ về `mongo` cho từng test; test PostgreSQL tự bật cờ nó cần.

### Lưu ý về Docker Container

Toàn bộ cấu hình dịch vụ nằm trong file `docker-compose.yml`. Backend và worker mặc định sẽ sử dụng GPU NVIDIA, do đó máy host cần có NVIDIA GPU và NVIDIA Container Toolkit. Docling cũng dùng GPU (chiếm khoảng 1,7 GB VRAM, có thể chia sẻ chung với Ollama). Nếu cần nhường VRAM cho LLM sinh câu hỏi, bạn có thể thiết lập `DOCLING_DEVICE=cpu` trong cấu hình backend. MongoDB được publish tại cổng `27017` trên máy host (có thể đổi cổng bằng biến `MONGO_HOST_PORT`).
