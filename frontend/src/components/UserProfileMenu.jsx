import React, { useState, useRef, useEffect, useContext } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faUser,
  faCalendarCheck,
  faSignOutAlt,
  faChevronDown,
  faListCheck,
  faBook,
  faFileLines,
  faClockRotateLeft,
  faEnvelope,
} from '@fortawesome/free-solid-svg-icons';
import { AuthContext } from '../context/AuthContext';
import { canAccessPath } from '../auth/permissions';
import { buildFallbackAvatar, normalizeAvatarUrl } from '../utils/avatarUrl';
import './UserProfileMenu.css';

const UserProfileMenu = () => {
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef(null);
  const navigate = useNavigate();
  
  // Lấy dữ liệu user và hàm logout từ AuthContext
  const { user, logout } = useContext(AuthContext);

  // Xử lý đóng menu khi click ra ngoài
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (menuRef.current && !menuRef.current.contains(event.target)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  useEffect(() => {
    if (!isOpen) return undefined;
    const handleEscape = (event) => {
      if (event.key === 'Escape') {
        setIsOpen(false);
        menuRef.current?.querySelector('.user-menu-trigger')?.focus();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen]);

  const handleLogout = async () => {
    await logout();
    navigate('/dang-nhap'); // Chuyển hướng về đăng nhập
  };

  // Tránh lỗi render nếu trạng thái user chưa sẵn sàng
  if (!user) return null;

  // Trích xuất thông tin hiển thị (Dựa trên cấu trúc UserInfo trả về từ MongoDB)
  const roleLabel = {
    Admin: 'Quản trị viên',
    Teacher: 'Giảng viên',
    Reviewer: 'Người duyệt',
  };
  const displayName = user.display_name || 'Người dùng';
  const displayRole = roleLabel[user.role] || user.role || 'Người dùng';
  const canOpenSettings = user.role === 'Admin';
  
  // Tự động generate avatar dựa trên tên người dùng
  const avatarUrl = normalizeAvatarUrl(user.profile?.avatar) || buildFallbackAvatar(displayName);

  return (
    <div className="user-menu-container" ref={menuRef}>
      <button 
        type="button"
        className={`user-menu-trigger ${isOpen ? 'active' : ''}`}
        onClick={() => setIsOpen(!isOpen)}
        aria-label={`Tài khoản ${displayName}`}
        aria-expanded={isOpen}
        aria-controls={isOpen ? 'header-account-panel' : undefined}
      >
        <img src={avatarUrl} alt="" className="user-avatar" referrerPolicy="no-referrer" />
        <span className="user-identity">
          <span className="user-name">{displayName}</span>
          <span className="user-role">{displayRole}</span>
        </span>
        <FontAwesomeIcon icon={faChevronDown} className="user-chevron" />
      </button>

      {isOpen && (
        <div className="user-dropdown" id="header-account-panel">
          <div className="dropdown-header">
            <img src={avatarUrl} alt="" className="dropdown-avatar" referrerPolicy="no-referrer" />
            <div className="dropdown-identity">
              <p className="dropdown-name">{displayName}</p>
              <p className="dropdown-role">{displayRole}</p>
            </div>
          </div>
          
          <div className="dropdown-divider"></div>
          
          <Link to="/ho-so" className="dropdown-item" onClick={() => setIsOpen(false)}>
            <FontAwesomeIcon icon={faUser} className="dropdown-icon" />
            Hồ sơ cá nhân
          </Link>

          {canAccessPath(user, '/quan-ly-hoc-phan') && (
            <Link to="/quan-ly-hoc-phan" className="dropdown-item" onClick={() => setIsOpen(false)}>
              <FontAwesomeIcon icon={faBook} className="dropdown-icon" />
              Quản lý học phần
            </Link>
          )}
          {canAccessPath(user, '/quan-ly-tai-lieu') && (
            <Link to="/quan-ly-tai-lieu" className="dropdown-item dropdown-item--nested" onClick={() => setIsOpen(false)}>
              <FontAwesomeIcon icon={faFileLines} className="dropdown-icon" />
              Quản lý tài liệu
            </Link>
          )}
          {canAccessPath(user, '/quan-ly') && user.role !== 'Admin' && (
            <Link to="/quan-ly" className="dropdown-item" onClick={() => setIsOpen(false)}>
              <FontAwesomeIcon icon={faListCheck} className="dropdown-icon" />
              Quản lý câu hỏi
            </Link>
          )}

          {canAccessPath(user, '/lich-cong-viec') && (
            <Link to="/lich-cong-viec" className="dropdown-item" onClick={() => setIsOpen(false)}>
              <FontAwesomeIcon icon={faCalendarCheck} className="dropdown-icon" />
              Lịch công việc
            </Link>
          )}

          {canOpenSettings && (
            <>
              <Link to="/lien-he?tab=all" className="dropdown-item" onClick={() => setIsOpen(false)}>
                <FontAwesomeIcon icon={faEnvelope} className="dropdown-icon" />
                Xem liên hệ
              </Link>
              <Link to="/nhat-ky-he-thong" className="dropdown-item" onClick={() => setIsOpen(false)}>
                <FontAwesomeIcon icon={faClockRotateLeft} className="dropdown-icon" />
                Lịch sử dùng
              </Link>
              <Link to="/quan-ly-job" className="dropdown-item" onClick={() => setIsOpen(false)}>
                <FontAwesomeIcon icon={faListCheck} className="dropdown-icon" />
                Tác vụ hệ thống
              </Link>
            </>
          )}
          
          <div className="dropdown-divider"></div>
          
          <button className="dropdown-item text-danger" onClick={handleLogout}>
            <FontAwesomeIcon icon={faSignOutAlt} className="dropdown-icon" />
            Đăng xuất
          </button>
        </div>
      )}
    </div>
  );
};

export default UserProfileMenu;
