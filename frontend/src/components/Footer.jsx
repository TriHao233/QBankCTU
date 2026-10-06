import React, { useContext } from 'react';
import { Link } from 'react-router-dom';
import { canAccessPath } from '../auth/permissions';
import { AuthContext } from '../context/AuthContext';
import './Footer.css';

const Footer = () => {
  const currentYear = new Date().getFullYear();
  const { user } = useContext(AuthContext);
  // Danh sách các liên kết điều hướng nội bộ
  const footerLinks = [
    { path: '/trang-chu', label: 'Trang chủ' },
    { path: '/gioi-thieu', label: 'Giới thiệu' },
    { path: '/tong-quan', label: 'Tổng quan quản trị' },
    { path: '/sinh-cau-hoi', label: 'Sinh câu hỏi' },
    { path: '/quan-ly', label: 'Quản lý câu hỏi' },
    { path: '/kiem-duyet', label: 'Hàng kiểm duyệt' },
    { path: '/huong-dan', label: 'Hướng dẫn' },
    { path: '/lien-he', label: 'Liên hệ' },
  ].filter((link) => canAccessPath(user || null, link.path));

  return (
    <footer
      className="footer"
      style={{ '--footer-background': `url("${import.meta.env.BASE_URL}images/footer-background.png")` }}
    >
      <div className="container">
        <div className="footer-grid">
          {/* Brand Column */}
          <div className="footer-brand">
            <h4 className="footer-col-title">Giới thiệu dự án</h4>
            <div className="footer-logo-row">
              <img 
                src={`${import.meta.env.BASE_URL}images/qbankctu-logo.png`}
                alt="Logo QBankCTU"
                className="footer-logo" 
                width="152"
                height="152"
                loading="lazy"
                decoding="async"
              />
              <div className="footer-brand-copy">
                <div className="footer-brand-title">Đại học Cần Thơ</div>
                <div className="footer-brand-subtitle">Ngân hàng câu hỏi ứng dụng mô hình ngôn ngữ lớn (LLMs)</div>
              </div>
            </div>
            <p className="footer-tagline">
              Hỗ trợ giảng viên tạo câu hỏi từ tài liệu học tập, phân loại theo thang Bloom, rà soát và quản lý nội dung phục vụ nghiên cứu, đào tạo và giảng dạy.
            </p>
          </div>

          {/* Contact Column */}
          <div className="footer-col footer-support">
            <h4 className="footer-col-title">Đơn vị hỗ trợ dự án</h4>
            <div className="footer-partner-logos">
              <a
                href="https://www.ctu.edu.vn/"
                className="footer-partner-link"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Website Đại học Cần Thơ (mở trong tab mới)"
              >
                <img
                  src={`${import.meta.env.BASE_URL}images/ctu-logo.png`}
                  alt="Logo Đại học Cần Thơ"
                  width="72"
                  height="72"
                  loading="lazy"
                  decoding="async"
                />
              </a>
              <a
                href="https://www.cit.ctu.edu.vn/"
                className="footer-partner-link"
                target="_blank"
                rel="noopener noreferrer"
                aria-label="Website Trường Công nghệ Thông tin & Truyền thông (mở trong tab mới)"
              >
                <img
                  src={`${import.meta.env.BASE_URL}images/cict-logo.png`}
                  alt="Logo CICT"
                  width="72"
                  height="72"
                  loading="lazy"
                  decoding="async"
                />
              </a>
            </div>
            <p className="footer-col-text">
              Khoa Công Nghệ Phần Mềm - Trường Công Nghệ Thông Tin &amp; Truyền Thông, Đại học Cần Thơ.
            </p>
            <p className="footer-col-text">
              <strong>Địa chỉ:</strong> Khu II - Đại học Cần Thơ, Đường 3/2, phường Xuân Khánh, Quận Ninh Kiều, TP Cần Thơ.
            </p>
          </div>

          {/* Navigation Column */}
          <div className="footer-col">
            <h4 className="footer-col-title">Điều hướng</h4>
            <ul className="footer-links">
              {footerLinks.map((link) => (
                <li key={link.path}>
                  <Link to={link.path} className="footer-link">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>

          {/* Research Group Column */}
          <div className="footer-col">
            <h4 className="footer-col-title">Nhóm nghiên cứu</h4>
            <p className="footer-col-text">
              Đề tài Nghiên Cứu Khoa Học phát triển bởi nhóm sinh viên ngành Kỹ Thuật Phần Mềm (Chương trình Chất lượng cao) - Khóa 48
            </p>
          </div>
        </div>

        {/* Bottom Bar */}
        <div className="footer-bottom">
          <span>&copy; {currentYear} QBankCTU - Trường Công Nghệ Thông Tin &amp; Truyền Thông - Đại học Cần Thơ.</span>
        </div>
      </div>
    </footer>
  );
};

export default Footer;
