import React from 'react';
import { Link } from 'react-router-dom';
import '../css/AboutPage.css';

/* ─── Icons (feather-style, stroke 2) ───────────────────────────── */
const Icon = {
  warning: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  scale: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M16 3h5v5M8 3H3v5M3 16v5h5M16 21h5v-5" /><path d="M3 3l7 7M21 3l-7 7M3 21l7-7M21 21l-7-7" />
    </svg>
  ),
  brain: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><path d="M8 14s1.5 2 4 2 4-2 4-2" /><line x1="9" y1="9" x2="9.01" y2="9" /><line x1="15" y1="9" x2="15.01" y2="9" />
    </svg>
  ),
  shield: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2l8 4v6c0 5-3.5 8-8 10-4.5-2-8-5-8-10V6l8-4z" />
    </svg>
  ),
  monitor: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  ),
  server: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="20" height="8" rx="2" /><rect x="2" y="14" width="20" height="8" rx="2" /><line x1="6" y1="6" x2="6.01" y2="6" /><line x1="6" y1="18" x2="6.01" y2="18" />
    </svg>
  ),
  cpu: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2" /><rect x="9" y="9" width="6" height="6" /><line x1="9" y1="1" x2="9" y2="4" /><line x1="15" y1="1" x2="15" y2="4" /><line x1="9" y1="20" x2="9" y2="23" /><line x1="15" y1="20" x2="15" y2="23" /><line x1="20" y1="9" x2="23" y2="9" /><line x1="20" y1="15" x2="23" y2="15" /><line x1="1" y1="9" x2="4" y2="9" /><line x1="1" y1="15" x2="4" y2="15" />
    </svg>
  ),
  database: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  ),
  scan: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2" /><line x1="3" y1="12" x2="21" y2="12" />
    </svg>
  ),
  link: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" /><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </svg>
  ),
  arrowRight: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <line x1="5" y1="12" x2="19" y2="12" /><polyline points="12 5 19 12 12 19" />
    </svg>
  ),
  arrowDown: (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <line x1="12" y1="5" x2="12" y2="19" /><polyline points="19 12 12 19 5 12" />
    </svg>
  ),
  target: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" /><circle cx="12" cy="12" r="6" /><circle cx="12" cy="12" r="2" />
    </svg>
  ),
  eye: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z" /><circle cx="12" cy="12" r="3" />
    </svg>
  ),
  list: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
    </svg>
  ),
  atom: (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
      <ellipse cx="12" cy="12" rx="10" ry="4.2" />
      <ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(60 12 12)" />
      <ellipse cx="12" cy="12" rx="10" ry="4.2" transform="rotate(120 12 12)" />
    </svg>
  ),
  code: (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" />
    </svg>
  ),
  nodes: (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="5" cy="6" r="2.2" /><circle cx="19" cy="6" r="2.2" /><circle cx="12" cy="13" r="2.2" /><circle cx="5" cy="20" r="2.2" /><circle cx="19" cy="20" r="2.2" />
      <line x1="6.6" y1="7.5" x2="10.4" y2="11.5" /><line x1="17.4" y1="7.5" x2="13.6" y2="11.5" /><line x1="10.4" y1="14.5" x2="6.6" y2="18.5" /><line x1="13.6" y1="14.5" x2="17.4" y2="18.5" />
    </svg>
  ),
  key: (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="7.5" cy="15.5" r="4.5" /><path d="M10.6 12.4 20 3M17 6l3 3M14 9l2.5 2.5" />
    </svg>
  ),
  cap: (
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M2 9.5 12 5l10 4.5-10 4.5-10-4.5Z" /><path d="M6 11.6v4.4c0 1.4 2.7 3 6 3s6-1.6 6-3v-4.4" /><path d="M22 9.5v6" />
    </svg>
  ),
};

const problems = [
  {
    icon: Icon.warning,
    title: 'Tốn kém nguồn lực',
    desc: 'Biên soạn câu hỏi thủ công khiến giảng viên tốn nhiều thời gian để tránh trùng lặp nội dung.',
  },
  {
    icon: Icon.scale,
    title: 'Chất lượng không đồng đều',
    desc: 'Câu hỏi phụ thuộc vào chủ quan người soạn, độ phủ kiến thức lệch, thiếu câu hỏi tư duy cao.',
  },
  {
    icon: Icon.brain,
    title: 'Rủi ro "ảo giác" của AI',
    desc: 'AI tạo sinh giúp tự động hoá nhưng có thể sinh nội dung sai lệch mà vẫn nghe thuyết phục.',
  },
];

const solutions = [
  {
    title: 'Nền tảng quản lý tập trung',
    desc: 'Số hoá, lưu trữ và tổ chức dữ liệu câu hỏi – đề thi có hệ thống, dễ truy xuất và tái sử dụng.',
  },
  {
    title: 'Tự động hoá bằng RAG',
    desc: 'Kết hợp LLM với kỹ thuật RAG để trích xuất đúng ngữ cảnh từ tài liệu, sinh câu hỏi nhanh và khách quan.',
  },
  {
    title: 'Con người trong vòng lặp',
    desc: 'AI chỉ khởi tạo câu hỏi nháp; giảng viên và người duyệt luôn giữ quyền chỉnh sửa, phê duyệt cuối cùng.',
  },
  {
    title: 'Liên kết hệ thống LMS',
    desc: 'Chuẩn hoá và chuyển đổi câu hỏi đã duyệt sang định dạng Ngân hàng câu hỏi của Moodle.',
  },
];

const capabilities = [
  {
    icon: Icon.list,
    title: '7 loại câu hỏi',
    desc: 'Trắc nghiệm, Đúng/Sai, Điền khuyết, Ghép cột, Sắp xếp, Tình huống và Nhiều lựa chọn.',
  },
  {
    icon: Icon.target,
    title: 'Phân loại theo thang Bloom',
    desc: 'Gắn cấp độ tư duy cho câu hỏi: Nhớ, Hiểu, Vận dụng, Phân tích, Đánh giá, Sáng tạo.',
  },
  {
    icon: Icon.scan,
    title: 'OCR tài liệu bản scan',
    desc: 'Tự động nhận dạng và trích xuất văn bản từ tài liệu PDF dạng hình ảnh.',
  },
  {
    icon: Icon.eye,
    title: 'Con người trong vòng lặp',
    desc: 'Giảng viên và người duyệt luôn kiểm soát nội dung trước khi xuất bản.',
  },
  {
    icon: Icon.link,
    title: 'Xuất bản chuẩn Moodle',
    desc: 'Chuyển đổi và export câu hỏi đã duyệt sang định dạng GIFT/XML.',
  },
];

const techStack = [
  { icon: Icon.atom, name: 'ReactJS', caption: 'Frontend' },
  { icon: Icon.code, name: 'FastAPI', caption: 'Backend' },
  { icon: Icon.database, name: 'MongoDB', caption: 'Cơ sở dữ liệu' },
  { icon: Icon.nodes, name: 'ChromaDB', caption: 'Vector Database' },
  { icon: Icon.key, name: 'Firebase', caption: 'Xác thực' },
  { icon: Icon.scan, name: 'Docling', caption: 'Xử lý tài liệu' },
  { icon: Icon.cpu, name: 'LLM', caption: 'Mô hình AI' },
  { icon: Icon.cap, name: 'Moodle', caption: 'Tích hợp LMS' },
];

const architectureLayers = [
  {
    icon: Icon.monitor,
    title: 'Client Layer',
    desc: 'Admin Dashboard (React)',
    items: ['Giảng viên', 'Người duyệt', 'Quản trị viên'],
  },
  {
    icon: Icon.server,
    title: 'API / Backend Layer',
    desc: 'FastAPI · REST API',
    items: ['Auth & Roles', 'Quản lý câu hỏi (CRUD)', 'Điều phối tác vụ'],
  },
  {
    icon: Icon.cpu,
    title: 'AI Core — RAG Pipeline',
    desc: 'Xử lý & sinh câu hỏi',
    items: ['OCR → Chunking → Embedding', 'Truy xuất ngữ cảnh (Retrieval)', 'LLM Generation'],
  },
  {
    icon: Icon.database,
    title: 'Data Layer',
    desc: 'Lưu trữ hệ thống',
    items: ['MongoDB — dữ liệu nghiệp vụ', 'ChromaDB — vector index', 'Firebase — tài khoản'],
  },
];

const flowSteps = [
  { num: '01', title: 'Tải tài liệu học phần', desc: 'Giảng viên tải giáo trình, bài giảng dạng PDF/DOC lên hệ thống.' },
  { num: '02', title: 'Tiền xử lý & Embedding', desc: 'OCR (nếu là bản scan), Chunking văn bản và nhúng (embedding) vào ChromaDB.' },
  { num: '03', title: 'RAG truy xuất ngữ cảnh', desc: 'Hệ thống truy xuất đúng đoạn nội dung liên quan nhất từ tài liệu nguồn.' },
  { num: '04', title: 'LLM sinh câu hỏi nháp', desc: 'Mô hình ngôn ngữ lớn sinh câu hỏi theo loại (Trắc nghiệm, Đúng/Sai, Điền khuyết, Ghép cột, Sắp xếp, Tình huống, Nhiều lựa chọn).' },
  { num: '05', title: 'Chỉnh sửa & gửi kiểm duyệt', desc: 'Giảng viên rà soát, chỉnh sửa nội dung rồi gửi sang hàng đợi kiểm duyệt.' },
  { num: '06', title: 'Duyệt & xuất bản Moodle', desc: 'Người duyệt phê duyệt hoặc yêu cầu sửa; câu đã duyệt được export GIFT/XML và đồng bộ Moodle.' },
];

const team = [
  {
    name: 'Trương Trí Hào',
    role: 'Project Lead & Backend Engineer',
    lead: true,
    badge: 'Chủ nhiệm đề tài',
    tags: ['Project Management', 'LLM Research', 'OCR Integration', 'Backend Development'],
    tasks: 'Quản lý chung tiến độ dự án, thu thập và xử lý tài liệu nghiên cứu. Nghiên cứu mô hình LLM, tích hợp pipeline OCR và phát triển hệ thống Backend.',
  },
  {
    name: 'Trần Hải Thiên',
    role: 'RAG Engineer & QA Engineer',
    tags: ['RAG Pipeline', 'Document Chunking', 'Technical Writing', 'Testing', 'UI Optimization'],
    tasks: 'Tích hợp kỹ thuật RAG, xây dựng quy trình Chunking và xử lý tài liệu. Soạn thảo tài liệu kỹ thuật, kiểm thử dự án và tối ưu hoá giao diện.',
  },
  {
    name: 'Tiêu Lê Gia Linh',
    role: 'System Architect & Prompt Engineer',
    tags: ['System Architecture', 'UI/UX Design', 'Prompt Engineering', 'Auth & Roles Module', 'QA & Documentation'],
    tasks: 'Thiết kế kiến trúc hệ thống và giao diện UI/UX. Xây dựng, tối ưu hoá prompt cho LLM. Phát triển module xác thực và phân quyền. Kiểm thử và viết tài liệu.',
  },
  {
    name: 'Vương Phan Quốc Cường',
    role: 'Database Engineer & QA Systems',
    tags: ['Database Design', 'Question Quality Control', 'Admin Authorization'],
    tasks: 'Thiết kế cơ sở dữ liệu hệ thống. Xây dựng hệ thống kiểm định chất lượng câu hỏi và cơ chế phân quyền quản trị hệ thống.',
  },
  {
    name: 'Lê Trọng Thiện',
    role: 'Backend Engineer & API Gateway',
    tags: ['API Gateway', 'Auth & Security Module', 'CRUD API Development'],
    tasks: 'Xây dựng API Gateway, phát triển module bảo mật xác thực và phân quyền. Lập trình các API quản lý (CRUD) cho hệ thống Backend.',
  },
];

const advisors = [
  {
    name: 'TS. Phan Phương Lan',
    unit: 'Trường Công nghệ Thông tin và Truyền thông – Đại học Cần Thơ',
    field: 'Công nghệ Phần mềm',
    task: 'Định hướng phát triển hệ thống, hướng dẫn xây dựng tài liệu, báo cáo khoa học, kiểm thử và tối ưu hệ thống.',
  },
  {
    name: 'KS. Trương Phúc Vĩnh',
    unit: 'Trường Công nghệ Thông tin và Truyền thông – Đại học Cần Thơ',
    field: 'Công nghệ Phần mềm',
    task: 'Hướng dẫn tích hợp các kỹ thuật thiết kế hệ thống, tối ưu prompt, câu hỏi và mô hình ngôn ngữ lớn (LLM).',
  },
];

function AboutPage() {
  return (
    <main className="about-page">
      <section className="page-hero">
        <div className="container">
          <div className="page-hero-badge">Đề tài nghiên cứu khoa học cấp cơ sở · CICT – ĐHCT</div>
          <h1 className="page-hero-title">Giới thiệu về QBankCTU</h1>
          <p className="page-hero-desc">
            Nghiên cứu và xây dựng ứng dụng Web quản lý ngân hàng câu hỏi phục vụ giảng dạy và kiểm tra – đánh giá,
            khai thác mô hình ngôn ngữ lớn (LLMs) kết hợp kỹ thuật RAG để hỗ trợ sinh câu hỏi tự động từ tài liệu,
            đồng thời đảm bảo quy trình quản lý và kiểm duyệt chặt chẽ trước khi đưa câu hỏi vào sử dụng chính thức.
          </p>
        </div>
      </section>

      <section className="problem-solution">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Vấn đề &amp; Giải pháp</h2>
            <p className="section-desc">Nhận diện đúng vấn đề để xây dựng giải pháp phù hợp cho công tác biên soạn ngân hàng câu hỏi.</p>
          </div>
          <div className="ps-grid">
            <div className="ps-col ps-issue">
              <h3 className="ps-col-title">Vấn đề đặt ra</h3>
              <div className="issue-list">
                {problems.map((p) => (
                  <div className="issue-item" key={p.title}>
                    <div className="issue-icon">{p.icon}</div>
                    <div>
                      <h4>{p.title}</h4>
                      <p>{p.desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="ps-connector" aria-hidden="true">{Icon.arrowRight}</div>

            <div className="ps-col ps-solution">
              <span className="ps-solution-badge">Giải pháp QBankCTU</span>
              <h3 className="ps-col-title">Giải pháp thực hiện</h3>
              <p className="ps-solution-lead">
                QBankCTU khai thác tốc độ của AI để gợi ý câu hỏi, đồng thời trao cho giảng viên toàn quyền kiểm
                duyệt, hiệu chỉnh và chuẩn hoá nội dung trước khi công bố.
              </p>
              <div className="solution-grid">
                {solutions.map((s, i) => (
                  <div className="solution-card" key={s.title}>
                    <span className="solution-num">{String(i + 1).padStart(2, '0')}</span>
                    <h4>{s.title}</h4>
                    <p>{s.desc}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="capabilities">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Năng lực hệ thống</h2>
            <p className="section-desc">Những gì QBankCTU có thể làm được cho công tác biên soạn và kiểm duyệt câu hỏi.</p>
          </div>
          <div className="capabilities-grid">
            {capabilities.map((c) => (
              <div className="capability-card" key={c.title}>
                <div className="capability-icon">{c.icon}</div>
                <h3>{c.title}</h3>
                <p>{c.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="tech">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Công nghệ sử dụng</h2>
            <p className="section-desc">Nền tảng mã nguồn mở, ưu tiên triển khai cục bộ và bảo mật dữ liệu.</p>
          </div>
          <div className="tech-tiles">
            {techStack.map((t) => (
              <div className="tech-tile" key={t.name}>
                <div className="tech-tile-icon">{t.icon}</div>
                <span className="tech-tile-name">{t.name}</span>
                <span className="tech-tile-caption">{t.caption}</span>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="architecture">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Kiến trúc hệ thống</h2>
            <p className="section-desc">Kiến trúc phân lớp tách biệt giao diện, xử lý nghiệp vụ, lõi AI và dữ liệu — dễ mở rộng và bảo trì.</p>
          </div>
          <div className="arch-flow">
            {architectureLayers.map((layer, i) => (
              <React.Fragment key={layer.title}>
                <div className="arch-box">
                  <div className="arch-box-head">
                    <div className="arch-icon">{layer.icon}</div>
                    <div>
                      <h3>{layer.title}</h3>
                      <span className="arch-box-desc">{layer.desc}</span>
                    </div>
                  </div>
                  <ul>
                    {layer.items.map((it) => (
                      <li key={it}>{it}</li>
                    ))}
                  </ul>
                </div>
                {i < architectureLayers.length - 1 && (
                  <div className="arch-arrow" aria-hidden="true">{Icon.arrowDown}</div>
                )}
              </React.Fragment>
            ))}
          </div>

          <div className="arch-integration">
            <span className="arch-integration-chip">Data Layer</span>
            <span className="arch-integration-link">{Icon.arrowRight}</span>
            <span className="arch-integration-chip arch-integration-chip--accent">
              {Icon.shield} Moodle LMS — Plugin &amp; Export GIFT/XML
            </span>
          </div>
        </div>
      </section>

      <section className="flow">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Quy trình hệ thống</h2>
            <p className="section-desc">Từ tài liệu thô đến câu hỏi sẵn sàng đưa vào Moodle — theo nguyên tắc "Con người trong vòng lặp".</p>
          </div>
          <div className="flow-steps">
            {flowSteps.map((s) => (
              <div className="flow-step" key={s.num}>
                <span className="flow-num">{s.num}</span>
                <div className="flow-body">
                  <h3>{s.title}</h3>
                  <p>{s.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="advisors">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Cán bộ hướng dẫn</h2>
            <p className="section-desc">Đồng hành cùng nhóm nghiên cứu trong suốt quá trình thực hiện đề tài</p>
          </div>
          <div className="advisors-grid">
            {advisors.map((a) => (
              <div className="advisor-card" key={a.name}>
                <div className="advisor-avatar">{a.name.split(' ').slice(-1)[0][0]}</div>
                <h3>{a.name}</h3>
                {a.mscb && <span className="advisor-mscb">{a.mscb}</span>}
                <dl className="advisor-fields">
                  <div className="advisor-field">
                    <dt>Đơn vị công tác</dt>
                    <dd>{a.unit}</dd>
                  </div>
                  <div className="advisor-field">
                    <dt>Lĩnh vực chuyên môn</dt>
                    <dd>{a.field}</dd>
                  </div>
                  <div className="advisor-field">
                    <dt>Nhiệm vụ</dt>
                    <dd>{a.task}</dd>
                  </div>
                </dl>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section className="team">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">Đội ngũ phát triển</h2>
            <p className="section-desc">Sinh viên Trường Công nghệ Thông tin &amp; Truyền thông · Khoá 48 · Ngành Kỹ thuật phần mềm (CLC)</p>
          </div>
          <div className="team-pyramid">
            <div className="team-row team-row-top">
              {team.slice(0, 2).map((m) => (
                <div className="team-card" key={m.name}>
                  {m.badge && <span className="team-lead-badge">{m.badge}</span>}
                  <div className="team-avatar">{m.name.split(' ').slice(-1)[0][0]}</div>
                  <h3>{m.name}</h3>
                  <span className="team-role">{m.role}</span>
                  <div className="team-tags">
                    {m.tags.map((tag) => (
                      <span className="team-tag" key={tag}>{tag}</span>
                    ))}
                  </div>
                  <p>{m.tasks}</p>
                </div>
              ))}
            </div>
            <div className="team-row team-row-bottom">
              {team.slice(2).map((m) => (
                <div className="team-card" key={m.name}>
                  {m.badge && <span className="team-lead-badge">{m.badge}</span>}
                  <div className="team-avatar">{m.name.split(' ').slice(-1)[0][0]}</div>
                  <h3>{m.name}</h3>
                  <span className="team-role">{m.role}</span>
                  <div className="team-tags">
                    {m.tags.map((tag) => (
                      <span className="team-tag" key={tag}>{tag}</span>
                    ))}
                  </div>
                  <p>{m.tasks}</p>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section className="about-cta">
        <div className="container about-cta-inner">
          <div>
            <h2>Tìm hiểu quy trình hoạt động của hệ thống</h2>
            <p>Xem hướng dẫn chi tiết hoặc dùng thử tính năng sinh câu hỏi bằng AI.</p>
          </div>
          <div className="about-cta-actions">
            <Link to="/huong-dan" className="btn btn--outline">Xem hướng dẫn</Link>
            <Link to="/sinh-cau-hoi" className="btn btn--primary">Trải nghiệm sinh câu hỏi</Link>
          </div>
        </div>
      </section>
    </main>
  );
}

export default AboutPage;
