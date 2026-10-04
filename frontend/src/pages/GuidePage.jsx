import React from 'react';
import { Link } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faChalkboardUser, faClipboardCheck, faShieldHalved, faArrowRight } from '@fortawesome/free-solid-svg-icons';
import { QUESTION_TYPES, allowedBloomLevels } from '../constants/generationEnums';
import '../css/GuidePage.css';
import { GuideConfigSandbox, GuideTroubleshooter } from '../components/GuideTools';

const roles = [
  { title: 'Giảng viên', icon: faChalkboardUser, target: '#guide-teacher', desc: 'Chuẩn bị học phần/CLO, sinh câu hỏi và tự kiểm duyệt human-in-the-loop.' },
  { title: 'Kiểm duyệt', icon: faClipboardCheck, target: '#guide-reviewer', desc: 'Giảng viên đối chiếu nguồn, Bloom/CLO rồi duyệt hoặc yêu cầu sửa.' },
  { title: 'Quản trị viên', icon: faShieldHalved, target: '#guide-admin', desc: 'Quản lý tài khoản, danh mục, giám sát tài liệu/sở hữu câu hỏi; không duyệt câu hỏi.' },
];
const steps = [
  { title: 'Chuẩn bị học phần', desc: 'Vào Quản lý học phần để tạo hoặc kiểm tra học phần, chương và chuẩn đầu ra (CLO) trước khi tải tài liệu.', link: '/quan-ly-hoc-phan', action: 'Mở Quản lý học phần' },
  { title: 'Chọn nguồn và xử lý tài liệu', desc: 'Trong Sinh câu hỏi, chọn Tải tài liệu mới, chọn file và Học phần, rồi bấm Xử lý tài liệu. Chờ xử lý hoàn tất. Nếu đã có nguồn, dùng Chọn tài liệu đã xử lý; học phần được lấy từ tài liệu đó.', link: '/sinh-cau-hoi', action: 'Mở Sinh câu hỏi' },
  { title: 'Thiết lập ma trận câu hỏi', desc: 'Mỗi dòng chọn Dạng câu hỏi, Mức nhận thức Bloom, Độ khó, Số câu và Nội dung. Dùng Thêm dòng để phối hợp nhiều dạng; tổng số câu từ 1 đến 7 mỗi lượt. Các mức Bloom không phù hợp sẽ bị khóa.' },
  { title: 'Chọn mô hình và phạm vi', desc: 'Chọn Gemini hoặc Qwen trong Mô hình ngôn ngữ theo danh sách được cấu hình. Có thể nhập Chương hoặc mục cần tập trung và Yêu cầu sinh câu hỏi. Chọn Có mã nguồn cho câu hỏi dựa trên đoạn mã; chọn Lý thuyết cho nội dung khái niệm hoặc Tự nhận diện để hệ thống xác định.' },
  { title: 'Sinh và kiểm tra bản nháp', desc: 'Bấm Sinh câu hỏi bằng AI rồi theo dõi trạng thái. Đọc nội dung, đáp án, giải thích và dẫn chứng. Dùng Sửa để hiệu chỉnh hoặc Bỏ câu để loại bản nháp chưa đạt. Câu cần mã nguồn phải có đoạn mã đủ dữ kiện ngay trong đề.' },
  { title: 'Gửi duyệt và theo dõi phản hồi', desc: 'Bấm Gửi duyệt cho câu đã rà soát. Các ứng viên AI ban đầu gắn với tác vụ sinh, chưa tự trở thành câu trong ngân hàng. Theo dõi tại Quản lý câu hỏi; với câu Cần sửa, đọc góp ý, sửa rồi gửi duyệt lại.', link: '/quan-ly', action: 'Mở Quản lý câu hỏi' },
];
const faqs = [
  { q: 'Tôi đăng nhập hoặc đăng ký bằng cách nào?', a: 'Dùng trang Đăng nhập với tài khoản của bạn. Nếu chưa có tài khoản, mở Đăng ký; giao diện hỗ trợ email/mật khẩu và Google. Quyền sử dụng chức năng phụ thuộc vai trò tài khoản; không phải mọi người dùng đều có quyền kiểm duyệt hoặc quản trị.' },
  { q: 'Tài liệu đầu vào hỗ trợ những định dạng nào?', a: 'PDF, DOC/DOCX, Markdown và TXT. PDF scan được xử lý bằng OCR. Phạm vi đề tài tập trung học phần Cấu trúc dữ liệu; kỹ thuật có thể gắn học phần khác nhưng CLO chuẩn được seed cho CTDL theo CTĐT.' },
  { q: 'Tôi không thấy học phần hoặc tài liệu đã xử lý?', a: 'Tạo hoặc kiểm tra học phần tại Quản lý học phần. Với tài liệu cũ, bấm Tải lại ở phần Chọn tài liệu đã xử lý; chỉ tài liệu đủ điều kiện mới xuất hiện. Nếu danh sách vẫn trống, tải tài liệu mới và chờ xử lý thành công.' },
  { q: 'Tại sao nút Sinh câu hỏi hoặc một số mức Bloom bị khóa?', a: 'Kiểm tra tài liệu đã xử lý và các điều kiện được báo trên trang. Tổng số câu phải từ 1 đến 7. Bloom phụ thuộc dạng câu hỏi; ví dụ MCQ chỉ hỗ trợ Nhớ, Hiểu, Vận dụng và Phân tích. Khi có tác vụ đang chạy, thao tác cấu hình có thể tạm khóa.' },
  { q: 'OCR hoặc xử lý tài liệu thất bại thì làm gì?', a: 'Đọc thông báo lỗi, kiểm tra định dạng và khả năng mở file. Với bản scan, ưu tiên trang rõ nét, đúng chiều; file có mật khẩu hoặc khó đọc nên được chuẩn bị lại. Thử xử lý lại sau khi khắc phục. Nếu lỗi lặp lại, ghi thông báo và tên tài liệu để quản trị viên kiểm tra.' },
  { q: 'Mô hình không tạo được câu hỏi hợp lệ thì xử lý thế nào?', a: 'Kiểm tra chất lượng tài liệu và phạm vi nội dung, thử ít câu hơn hoặc điều chỉnh ma trận phù hợp nguồn. Với câu hỏi về mã, chọn Có mã nguồn và dùng tài liệu có đoạn mã đầy đủ. Dùng nút thử lại khi trang hiển thị; nếu tiếp tục lỗi, ghi tên mô hình và thông báo lỗi để được hỗ trợ.' },
  { q: 'Câu hỏi thiếu đoạn mã hoặc dữ kiện thì có nên gửi duyệt?', a: 'Chưa nên gửi. Đối chiếu dẫn chứng, dùng Sửa để bổ sung đoạn mã và dữ kiện cần thiết; kiểm tra lại đáp án, giải thích. Nếu không xác minh được câu hỏi từ nguồn, dùng Bỏ câu và tạo lại.' },
  { q: 'Đóng trang có làm mất câu hỏi chưa gửi duyệt không?', a: 'Bản nháp AI chưa gửi duyệt không đồng nghĩa với câu đã lưu trong ngân hàng. Nên rà soát và gửi duyệt các câu cần giữ trước khi rời trang; không coi việc đóng trang hoặc đăng nhập lại là thao tác lưu bản nháp.' },
  { q: 'Xuất tệp Moodle và xuất bản Moodle khác nhau thế nào?', a: 'Câu đã duyệt có thể tải dưới dạng GIFT hoặc XML để nhập vào Moodle. Chức năng xuất bản hiện ghi mô phỏng trong hệ thống demo; ghi nhận thành công tại đây không có nghĩa câu hỏi đã được gửi lên máy chủ Moodle thật.' },
];

function GuidePage() {
  return (
    <main className="guide-page">
      <section className="page-hero"><div className="container">
        <div className="page-hero-badge">Hướng dẫn sử dụng</div>
        <h1 className="page-hero-title">Từ tài liệu đến câu hỏi đã được duyệt</h1>
        <p className="page-hero-desc">Chọn hướng dẫn theo vai trò, làm theo tên nút trên giao diện và kiểm tra nội dung trước khi gửi duyệt.</p>
        <nav className="guide-jump-nav" aria-label="Mục lục hướng dẫn"><a href="#guide-try">Thử cấu hình</a><a href="#guide-help">Tra cứu lỗi</a><a href="#guide-teacher">Giảng viên</a><a href="#guide-reviewer">Kiểm duyệt</a><a href="#guide-admin">Quản trị viên</a><a href="#guide-faq">Giải đáp</a></nav>
      </div></section>
      <section className="guide-start" aria-label="Chọn hướng dẫn theo vai trò"><div className="container">
        <div className="roles-grid">{roles.map((role) => <a className="role-card" href={role.target} key={role.title}><span className="role-icon"><FontAwesomeIcon icon={role.icon} aria-hidden="true" /></span><h3>{role.title}</h3><p>{role.desc}</p><span className="guide-role-action">Xem hướng dẫn <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" /></span></a>)}</div>
      </div></section>
      <GuideConfigSandbox />
      <GuideTroubleshooter />
      <section className="steps" id="guide-teacher" aria-labelledby="guide-teacher-title"><div className="container">
        <div className="section-header"><h2 className="section-title" id="guide-teacher-title">Giảng viên · 6 bước tạo và duyệt câu hỏi</h2><p className="section-desc">AI tạo ứng viên; giảng viên rà soát và là người quyết định duyệt cuối cùng theo thuyết minh.</p></div>
        <ol className="steps-list">{steps.map((step, index) => <li className="step-row" key={step.title}><span className="step-num" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><div className="step-body"><h3>{step.title}</h3><p>{step.desc}</p>{step.link && <Link className="guide-inline-link" to={step.link}>{step.action} <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" /></Link>}</div></li>)}</ol>
        <div className="guide-example" aria-labelledby="guide-example-title"><div><span className="guide-example-label">Ví dụ cấu hình</span><h3 id="guide-example-title">Ôn tập cấu trúc rẽ nhánh</h3><p>Dùng tài liệu học phần có đoạn mã if/else rõ ràng. Chương cần tập trung: “Cấu trúc rẽ nhánh”. Yêu cầu: “Dùng đoạn mã ngắn, đủ dữ kiện; nêu rõ điều kiện và giá trị ban đầu”.</p></div><dl><div><dt>Dạng câu hỏi</dt><dd>Trắc nghiệm (MCQ)</dd></div><div><dt>Bloom / Độ khó</dt><dd>Hiểu / Trung bình</dd></div><div><dt>Số câu / Nội dung</dt><dd>3 câu / Có mã nguồn</dd></div></dl><p className="guide-example-note">Ví dụ tham khảo; kết quả cần được kiểm tra theo tài liệu thực tế.</p></div>
        <details className="guide-type-reference"><summary>7 dạng câu hỏi và mức Bloom tương ứng</summary><ul>{QUESTION_TYPES.map((type) => <li key={type.id}><strong>{type.label}</strong><span>{allowedBloomLevels(type.id).map((level) => level.label).join(' · ')}</span></li>)}</ul></details>
      </div></section>
      <section className="guide-workflows" aria-label="Hướng dẫn kiểm duyệt và quản trị"><div className="container guide-workflow-grid">
        <article id="guide-reviewer" className="guide-workflow-card"><span className="role-icon"><FontAwesomeIcon icon={faClipboardCheck} aria-hidden="true" /></span><h2>Kiểm duyệt (do giảng viên)</h2><ol><li>Mở Hàng kiểm duyệt, chọn câu của mình hoặc câu được phân công. Bấm <strong>Nhận câu</strong> khi được phép.</li><li>Đối chiếu đề, đáp án, giải thích, Bloom/CLO và dẫn chứng. Góp ý AI chỉ hỗ trợ.</li><li>Chọn duyệt, <strong>Cần sửa</strong> hoặc <strong>Từ chối</strong>; ghi rõ lý do.</li><li>Với câu đã duyệt, dùng <strong>Tải tệp GIFT</strong> hoặc <strong>Tải tệp XML</strong> khi cần nhập Moodle.</li></ol><Link to="/kiem-duyet" className="guide-inline-link">Mở Hàng kiểm duyệt <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" /></Link></article>
        <article id="guide-admin" className="guide-workflow-card"><span className="role-icon"><FontAwesomeIcon icon={faShieldHalved} aria-hidden="true" /></span><h2>Quản trị viên</h2><ol><li>Quản lý tài khoản <strong>Admin / Giảng viên</strong> (không tạo role Người duyệt mới).</li><li>Quản lý học phần, CLO theo CTĐT và cấu hình AI/policy (kèm lý do chuyển version).</li><li>Giám sát tài liệu upload và tổng quan sở hữu câu hỏi theo giảng viên; <strong>không duyệt câu hỏi</strong>.</li><li>Theo dõi tác vụ/nhật ký và cấu hình Moodle demo.</li></ol><Link to="/tong-quan" className="guide-inline-link">Mở Tổng quan quản trị <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" /></Link></article>
      </div></section>
      <section className="faq" id="guide-faq" aria-labelledby="guide-faq-title"><div className="container"><div className="section-header"><h2 className="section-title" id="guide-faq-title">Giải đáp và xử lý vướng mắc</h2><p className="section-desc">Mở câu hỏi để xem cách kiểm tra trước khi liên hệ hỗ trợ.</p></div><div className="faq-list">{faqs.map((faq) => <details className="faq-item" key={faq.q}><summary>{faq.q}</summary><p>{faq.a}</p></details>)}</div><p className="guide-access-note">Các liên kết chức năng vẫn tuân theo đăng nhập và phân quyền hiện tại. Nếu chưa đăng nhập, hệ thống sẽ đưa bạn đến trang Đăng nhập.</p></div></section>
    </main>
  );
}
export default GuidePage;
