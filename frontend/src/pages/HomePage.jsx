import React from 'react';
import { Link } from 'react-router-dom';
import KnowledgePipelineSection from '../components/home/KnowledgePipelineSection';
import '../css/HomePage.css';

/* ═══════════════════════════════════════════════════════════════
   Content — kept above the component so the copy is reviewable in
   one place and the JSX stays about layout.

   Every model name, question type, Bloom level and workflow status
   below is taken from what the system actually implements
   (backend/modules/generation/llm/*, constants/generationEnums.js,
   the DRAFT/PENDING/APPROVED/NEEDS_REVISION status map). No
   invented benchmarks, accuracy scores or adoption counts.
   ═══════════════════════════════════════════════════════════════ */

const WORKFLOW = [
  {
    stage: '1.0',
    name: 'Tải lên',
    heading: 'Tài liệu học phần vào hệ thống',
    body:
      'Giảng viên tải giáo trình, slide bài giảng hoặc đề cương chi tiết dạng PDF, DOCX, Markdown hoặc TXT. '
      + 'Tài liệu scan được đưa qua OCR để bóc tách văn bản trước khi xử lý.',
    aside: {
      kind: 'files',
      items: [
        { name: 'giao-trinh-ctdl.pdf', meta: 'PDF · 214 trang' },
        { name: 'de-cuong-hp.docx', meta: 'DOCX · 12 trang' },
      ],
    },
  },
  {
    stage: '2.0',
    name: 'Đọc hiểu',
    heading: 'Chunking, nhúng vector và truy xuất',
    body:
      'Văn bản được cắt thành các đoạn theo ngữ nghĩa, nhúng thành vector và lập chỉ mục. '
      + 'Khi sinh câu hỏi, hệ thống truy xuất đúng đoạn tài liệu liên quan để làm ngữ cảnh.',
    aside: {
      kind: 'chunks',
      items: [
        { label: 'Chương 3 · Ngăn xếp', hit: true },
        { label: 'Chương 3 · Hàng đợi', hit: true },
        { label: 'Chương 5 · Cây nhị phân', hit: false },
      ],
    },
  },
  {
    stage: '3.0',
    name: 'Sinh câu hỏi',
    heading: 'LLM viết từ ngữ cảnh, không viết từ trí nhớ',
    body:
      'Mô hình ngôn ngữ lớn sinh câu hỏi dựa trên ngữ cảnh vừa truy xuất. '
      + 'Ràng buộc ngữ cảnh là cách hệ thống hạn chế hiện tượng mô hình bịa nội dung.',
    aside: {
      kind: 'params',
      items: [
        { k: 'Loại câu hỏi', v: 'Trắc nghiệm' },
        { k: 'Cấp độ Bloom', v: 'Vận dụng' },
        { k: 'Số lượng', v: '7 câu / lượt' },
      ],
    },
  },
  {
    stage: '4.0',
    name: 'Kiểm duyệt',
    heading: 'Người quyết định, không phải mô hình',
    body:
      'Câu hỏi vào trạng thái Nháp rồi chuyển sang hàng đợi kiểm duyệt. '
      + 'Người duyệt đối chiếu với tài liệu nguồn và chọn: duyệt, trả lại để sửa, hoặc từ chối.',
    aside: {
      kind: 'statuses',
      items: [
        { label: 'Nháp', tone: 'draft' },
        { label: 'Chờ duyệt', tone: 'pending' },
        { label: 'Cần sửa', tone: 'revise' },
        { label: 'Đã duyệt', tone: 'approved' },
      ],
    },
  },
  {
    stage: '5.0',
    name: 'Xuất bản',
    heading: 'Ra ngân hàng câu hỏi Moodle',
    body:
      'Chỉ câu hỏi đã được phê duyệt mới đi tiếp. Hệ thống chuyển đổi sang định dạng '
      + 'GIFT/XML theo cấu trúc ngân hàng câu hỏi của Moodle để đưa vào kỳ thi.',
    aside: {
      kind: 'export',
      items: ['GIFT', 'Moodle XML'],
    },
  },
];

const RESEARCH = [
  {
    term: 'Mô hình ngôn ngữ lớn',
    en: 'Large Language Model',
    body:
      'Hệ thống khảo sát và tích hợp các mô hình mã nguồn mở cùng mô hình thương mại '
      + '(Qwen, Gemini, DeepSeek) để so sánh chất lượng sinh câu hỏi trên tài liệu chuyên ngành tiếng Việt.',
  },
  {
    term: 'Truy xuất tăng cường',
    en: 'Retrieval-Augmented Generation',
    body:
      'Thay vì để mô hình trả lời từ tham số đã học, hệ thống truy xuất đoạn tài liệu liên quan '
      + 'và bắt mô hình viết trong phạm vi ngữ cảnh đó — hướng tiếp cận chính để giảm ảo giác.',
  },
  {
    term: 'Thiết kế prompt',
    en: 'Prompt Engineering',
    body:
      'Prompt được tách theo loại câu hỏi và cấp độ Bloom, kèm ràng buộc định dạng đầu ra '
      + 'để kết quả có thể phân tích tự động và ánh xạ thẳng sang cấu trúc Moodle.',
  },
  {
    term: 'Con người trong vòng lặp',
    en: 'Human-in-the-loop',
    body:
      'Nguyên tắc vận hành của đề tài: AI khởi tạo, giảng viên thẩm định. '
      + 'Không có đường đi nào đưa câu hỏi từ mô hình thẳng vào ngân hàng chính thức.',
  },
  {
    term: 'Thang đo Bloom',
    en: 'Bloom’s Taxonomy',
    body:
      'Mỗi câu hỏi được gắn một trong sáu cấp độ tư duy — Nhớ, Hiểu, Vận dụng, Phân tích, '
      + 'Đánh giá, Sáng tạo — để đề thi phủ đủ các mức nhận thức thay vì dồn vào ghi nhớ.',
  },
  {
    term: 'Triển khai cục bộ',
    en: 'Local AI',
    body:
      'Đề tài hướng tới khả năng chạy mô hình mã nguồn mở tại chỗ, để tài liệu nội bộ '
      + 'của nhà trường không phải rời khỏi hạ tầng khi sinh câu hỏi.',
  },
];

/* ─── Small real-UI fragments used as asides in the workflow ──── */
function WorkflowAside({ aside }) {
  if (aside.kind === 'files') {
    return (
      <ul className="wf-files">
        {aside.items.map((f) => (
          <li key={f.name}>
            <span className="wf-file-name">{f.name}</span>
            <span className="wf-file-meta">{f.meta}</span>
          </li>
        ))}
      </ul>
    );
  }

  if (aside.kind === 'chunks') {
    return (
      <ul className="wf-chunks">
        {aside.items.map((c) => (
          <li key={c.label} className={c.hit ? 'is-hit' : ''}>
            <span className="wf-chunk-bar" aria-hidden="true" />
            <span className="wf-chunk-label">{c.label}</span>
            <span className="wf-chunk-tag">{c.hit ? 'Đã truy xuất' : 'Bỏ qua'}</span>
          </li>
        ))}
      </ul>
    );
  }

  if (aside.kind === 'params') {
    return (
      <dl className="wf-params">
        {aside.items.map((p) => (
          <div key={p.k}>
            <dt>{p.k}</dt>
            <dd>{p.v}</dd>
          </div>
        ))}
      </dl>
    );
  }

  if (aside.kind === 'statuses') {
    return (
      <ul className="wf-statuses">
        {aside.items.map((s) => (
          <li key={s.label} className={`wf-status wf-status--${s.tone}`}>{s.label}</li>
        ))}
      </ul>
    );
  }

  return (
    <ul className="wf-export">
      {aside.items.map((e) => (
        <li key={e}>{e}</li>
      ))}
    </ul>
  );
}

function HomePage() {
  return (
    <main className="home-page">
      {/* ═══════════════════════════════════════════
          1 · HERO
      ═══════════════════════════════════════════ */}
      <section className="hero">
        <div className="container hero-grid">
          <div className="hero-text">
            <p className="hero-eyebrow">
              Đề tài nghiên cứu khoa học cấp cơ sở
              <span className="hero-eyebrow-sep" aria-hidden="true">·</span>
              Trường CNTT &amp; Truyền thông, ĐH Cần Thơ
            </p>

            <h1 className="hero-heading">
              Ngân hàng câu hỏi sinh từ tài liệu,
              <span className="hero-heading-accent"> duyệt bởi giảng viên</span>
            </h1>

            <p className="hero-lede">
              QBankCTU đọc giáo trình của học phần, truy xuất đúng ngữ cảnh bằng kỹ thuật RAG
              và để mô hình ngôn ngữ lớn soạn câu hỏi nháp. Mọi câu hỏi đều dừng lại ở khâu
              kiểm duyệt của con người trước khi vào ngân hàng đề thi.
            </p>

            <div className="hero-cta">
              <Link to="/sinh-cau-hoi" className="h-btn h-btn--primary">
                Bắt đầu sinh câu hỏi
              </Link>
              <Link to="/gioi-thieu" className="h-btn h-btn--quiet">
                Tìm hiểu hệ thống
              </Link>
            </div>
          </div>

          {/* Real product surface — a question record as the app stores it.
              Deliberately not wrapped in fake browser or window chrome. */}
          <figure className="hero-panel">
            <figcaption className="hero-panel-cap">
              Bản ghi câu hỏi trong ngân hàng · học phần Cấu trúc dữ liệu
            </figcaption>

            <div className="hero-panel-body">
              <article className="q-record">
                <div className="q-record-meta">
                  <span className="q-chip q-chip--type">Trắc nghiệm</span>
                  <span className="q-chip q-chip--bloom">Nhớ</span>
                  <span className="q-chip q-chip--state q-chip--approved">Đã duyệt</span>
                </div>
                <p className="q-record-stem">
                  Cấu trúc dữ liệu nào hoạt động theo nguyên tắc LIFO (vào sau, ra trước)?
                </p>
                <ol className="q-record-options">
                  <li>Hàng đợi (Queue)</li>
                  <li className="is-correct">Ngăn xếp (Stack)</li>
                  <li>Danh sách liên kết</li>
                  <li>Đồ thị (Graph)</li>
                </ol>
                <p className="q-record-source">
                  Nguồn: giao-trinh-ctdl.pdf · Chương 3
                </p>
              </article>

              <article className="q-record q-record--muted">
                <div className="q-record-meta">
                  <span className="q-chip q-chip--type">Đúng / Sai</span>
                  <span className="q-chip q-chip--bloom">Phân tích</span>
                  <span className="q-chip q-chip--state q-chip--pending">Chờ duyệt</span>
                </div>
                <p className="q-record-stem">
                  Trong cây nhị phân tìm kiếm, nút con trái luôn lớn hơn nút cha.
                </p>
              </article>
            </div>
          </figure>
        </div>
      </section>

      {/* ═══════════════════════════════════════════
          2 · 3D KNOWLEDGE PIPELINE
      ═══════════════════════════════════════════ */}
      <KnowledgePipelineSection />

      {/* ═══════════════════════════════════════════
          3 · HOW IT WORKS — narrative stages
      ═══════════════════════════════════════════ */}
      <section className="workflow" aria-labelledby="workflow-heading">
        <div className="container">
          <h2 className="workflow-heading" id="workflow-heading">
            Với người dùng, gói gọn trong năm bước
          </h2>

          <div className="workflow-stages">
            {WORKFLOW.map((step) => (
              <article className="wf-stage" key={step.stage}>
                {/* Stage marker stacks directly above its heading — never
                    in a column beside it (that hanging-header shape is a
                    templated-editorial tell). */}
                <div className="wf-content">
                  <p className="wf-marker">
                    <span className="wf-num">{step.stage}</span>
                    <span className="wf-name">{step.name}</span>
                  </p>
                  <h3 className="wf-title">{step.heading}</h3>
                  <p className="wf-body">{step.body}</p>
                </div>
                <div className="wf-aside">
                  <WorkflowAside aside={step.aside} />
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════
          4 · QUESTION EDITOR SHOWCASE
      ═══════════════════════════════════════════ */}
      <section className="editor" aria-labelledby="editor-heading">
        <div className="container editor-grid">
          <div className="editor-copy">
            <h2 className="editor-heading" id="editor-heading">
              Khâu kiểm duyệt là nơi câu hỏi thành đề thi
            </h2>
            <p className="editor-lede">
              Trình biên tập được xây quanh bốn thao tác của người duyệt. Mỗi câu hỏi luôn
              hiển thị kèm tài liệu nguồn, để việc đối chiếu không phải mở thêm cửa sổ nào.
            </p>

            <dl className="editor-actions">
              <div>
                <dt>Sửa</dt>
                <dd>Chỉnh câu dẫn, phương án, đáp án đúng, cấp độ Bloom và chuẩn đầu ra.</dd>
              </div>
              <div>
                <dt>Kiểm tra</dt>
                <dd>Đối chiếu nội dung với đoạn tài liệu mà hệ thống đã truy xuất.</dd>
              </div>
              <div>
                <dt>Phản hồi</dt>
                <dd>Ghi lỗi cần sửa và trả câu hỏi về cho giảng viên biên soạn.</dd>
              </div>
              <div>
                <dt>Phê duyệt</dt>
                <dd>Chuyển câu hỏi sang trạng thái đã duyệt, sẵn sàng cho bước xuất bản.</dd>
              </div>
            </dl>
          </div>

          <figure className="editor-panel">
            <figcaption className="editor-panel-cap">Hàng đợi kiểm duyệt</figcaption>

            <div className="editor-panel-body">
              <div className="ed-question">
                <div className="q-record-meta">
                  <span className="q-chip q-chip--type">Điền khuyết</span>
                  <span className="q-chip q-chip--bloom">Hiểu</span>
                  <span className="q-chip q-chip--state q-chip--revise">Cần sửa</span>
                </div>
                <p className="ed-stem">
                  Thao tác thêm một phần tử vào đỉnh ngăn xếp được gọi là
                  <span className="ed-blank"> ______ </span>.
                </p>
                <p className="ed-answer">
                  <span className="ed-answer-key">Đáp án</span> push
                </p>
              </div>

              <div className="ed-source">
                <span className="ed-source-label">Đoạn tài liệu đã truy xuất</span>
                <blockquote>
                  Ngăn xếp hỗ trợ hai thao tác cơ bản: push để thêm phần tử vào đỉnh
                  và pop để lấy phần tử ở đỉnh ra khỏi ngăn xếp.
                </blockquote>
                <span className="ed-source-ref">giao-trinh-ctdl.pdf · Chương 3</span>
              </div>

              <div className="ed-note">
                <span className="ed-note-label">Phản hồi của người duyệt</span>
                <p>Bổ sung ngữ cảnh “trong cấu trúc ngăn xếp” vào câu dẫn cho rõ nghĩa.</p>
              </div>

              <div className="ed-bar" role="group" aria-label="Thao tác kiểm duyệt (minh hoạ)">
                <span className="ed-act">Sửa</span>
                <span className="ed-act">Kiểm tra</span>
                <span className="ed-act">Phản hồi</span>
                <span className="ed-act ed-act--primary">Phê duyệt</span>
              </div>
            </div>
          </figure>
        </div>
      </section>

      {/* ═══════════════════════════════════════════
          5 · RESEARCH BEHIND THE SYSTEM
      ═══════════════════════════════════════════ */}
      <section className="research" aria-labelledby="research-heading">
        <div className="container">
          <div className="research-head">
            <h2 className="research-heading" id="research-heading">
              Nền tảng nghiên cứu
            </h2>
            <p className="research-lede">
              Sáu hướng kỹ thuật được khảo sát trong đề tài và đưa vào hệ thống.
            </p>
          </div>

          <dl className="research-list">
            {RESEARCH.map((item, i) => (
              <div className="research-item" key={item.term}>
                <dt>
                  <span className="research-idx">{String(i + 1).padStart(2, '0')}</span>
                  <span className="research-term">{item.term}</span>
                  <span className="research-en">{item.en}</span>
                </dt>
                <dd>{item.body}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      {/* ═══════════════════════════════════════════
          6 · MOODLE INTEGRATION
      ═══════════════════════════════════════════ */}
      <section className="moodle" aria-labelledby="moodle-heading">
        <div className="container">
          <div className="moodle-head">
            <h2 className="moodle-heading" id="moodle-heading">
              Từ ngân hàng nội bộ sang Moodle
            </h2>
            <p className="moodle-lede">
              Câu hỏi đã duyệt được chuyển đổi sang cấu trúc ngân hàng câu hỏi của Moodle —
              câu dẫn, phương án, đáp án đúng và thiết lập xáo trộn.
            </p>
          </div>

          <div className="moodle-flow">
            <div className="moodle-end">
              <span className="moodle-end-label">QBankCTU</span>
              <span className="moodle-end-sub">Ngân hàng câu hỏi nội bộ</span>
              <ul className="moodle-end-list">
                <li>Câu hỏi đã duyệt</li>
                <li>Cấp độ Bloom</li>
                <li>Chuẩn đầu ra (CLO)</li>
              </ul>
            </div>

            <div className="moodle-wire" aria-hidden="true">
              <span className="moodle-wire-format">GIFT / XML</span>
              <span className="moodle-wire-track">
                <span className="moodle-wire-flow" />
              </span>
            </div>

            <div className="moodle-end moodle-end--target">
              <span className="moodle-end-label">Moodle</span>
              <span className="moodle-end-sub">Question Bank của học phần</span>
              <ul className="moodle-end-list">
                <li>Sẵn sàng đưa vào đề thi</li>
                <li>Giữ nguyên tiếng Việt</li>
                <li>Thiết lập xáo trộn</li>
              </ul>
            </div>
          </div>
        </div>
      </section>

      {/* ═══════════════════════════════════════════
          7 · FINAL CTA
      ═══════════════════════════════════════════ */}
      <section className="closing" aria-labelledby="closing-heading">
        <div className="container closing-inner">
          <h2 className="closing-heading" id="closing-heading">
            Thử với một chương giáo trình
          </h2>
          <p className="closing-body">
            Tải lên một tài liệu học phần và xem hệ thống đề xuất bộ câu hỏi đầu tiên.
          </p>
          <Link to="/sinh-cau-hoi" className="h-btn h-btn--primary">
            Bắt đầu sinh câu hỏi
          </Link>
        </div>
      </section>
    </main>
  );
}

export default HomePage;
