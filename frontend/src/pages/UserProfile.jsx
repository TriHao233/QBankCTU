import React, { useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faBriefcase,
  faArrowUpRightFromSquare,
  faCircleCheck,
  faEnvelope,
  faFileLines,
  faLocationDot,
  faPen,
  faRotateRight,
  faShieldHalved,
  faTrashCan,
  faUpload,
  faUser,
  faUserGroup,
} from '@fortawesome/free-solid-svg-icons';
import {
  EmailAuthProvider,
  reauthenticateWithCredential,
  updatePassword,
} from 'firebase/auth';
import { AuthContext } from '../context/AuthContext';
import { auth } from '../firebase';
import { getMe, updateMe, uploadMyAvatar } from '../api/users';
import { PERMISSION_LABELS, canAccessPath, permissionsForUser } from '../auth/permissions';
import { buildFallbackAvatar, normalizeAvatarUrl } from '../utils/avatarUrl';
import '../css/UserProfile.css';

const MAX_SCHOOL_LENGTH = 200;
const MAX_ADDRESS_LENGTH = 300;
const MAX_AVATAR_FILE_SIZE = 2 * 1024 * 1024;
const URL_PATTERN = /^(?:https?:\/\/[^\s]+|\/api\/[^\s]+)$/i;
const ROLE_LABELS = { Teacher: 'Giảng viên', Admin: 'Quản trị viên', Reviewer: 'Người duyệt' };

function toFormState(user) {
  return {
    displayName: user?.display_name || '',
    school: user?.profile?.school || '',
    address: user?.profile?.address || '',
    avatar: normalizeAvatarUrl(user?.profile?.avatar),
    notificationEmail: user?.profile?.notification_email || '',
    emailNotificationsEnabled: user?.profile?.email_notifications_enabled !== false,
  };
}

function validateForm(form) {
  const errors = {};
  if (!form.displayName.trim()) {
    errors.displayName = 'Họ và tên không được để trống.';
  }
  if (form.school.trim().length > MAX_SCHOOL_LENGTH) {
    errors.school = `Đơn vị công tác không vượt quá ${MAX_SCHOOL_LENGTH} ký tự.`;
  }
  if (form.address.trim().length > MAX_ADDRESS_LENGTH) {
    errors.address = `Địa chỉ không vượt quá ${MAX_ADDRESS_LENGTH} ký tự.`;
  }
  if (form.avatar.trim() && !URL_PATTERN.test(form.avatar.trim())) {
    errors.avatar = 'Ảnh đại diện phải là URL hợp lệ, đường dẫn /api/... hoặc để trống.';
  }
  const notif = (form.notificationEmail || '').trim();
  if (notif && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(notif)) {
    errors.notificationEmail = 'Email nhận thông báo không hợp lệ.';
  }
  return errors;
}

function mapFirebaseAuthError(error) {
  const code = error?.code || '';
  if (code === 'auth/wrong-password' || code === 'auth/invalid-credential') {
    return 'Mật khẩu hiện tại không đúng.';
  }
  if (code === 'auth/weak-password') {
    return 'Mật khẩu mới quá yếu, cần tối thiểu 6 ký tự.';
  }
  if (code === 'auth/too-many-requests') {
    return 'Bạn đã thử quá nhiều lần. Vui lòng thử lại sau.';
  }
  if (code === 'auth/requires-recent-login') {
    return 'Phiên đăng nhập đã cũ. Vui lòng đăng xuất rồi đăng nhập lại trước khi đổi mật khẩu.';
  }
  return error?.message || 'Đổi mật khẩu thất bại.';
}

function InfoTab({ user, onProfileUpdated }) {
  const avatarInputRef = useRef(null);
  const [initialForm, setInitialForm] = useState(() => toFormState(user));
  const [form, setForm] = useState(() => toFormState(user));
  const [fieldErrors, setFieldErrors] = useState({});
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [isUploadingAvatar, setIsUploadingAvatar] = useState(false);
  const [banner, setBanner] = useState(null); // { type: 'success' | 'error', message }

  useEffect(() => {
    let active = true;
    setIsLoading(true);
    getMe()
      .then((freshUser) => {
        if (!active) return;
        const next = toFormState(freshUser);
        setInitialForm(next);
        setForm(next);
        onProfileUpdated(freshUser);
      })
      .catch(() => {
        // Nếu không tải được bản mới nhất, vẫn dùng dữ liệu sẵn có từ AuthContext.
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const isDirty = useMemo(
    () =>
      form.displayName !== initialForm.displayName ||
      form.school !== initialForm.school ||
      form.address !== initialForm.address ||
      form.avatar !== initialForm.avatar ||
      form.notificationEmail !== initialForm.notificationEmail ||
      form.emailNotificationsEnabled !== initialForm.emailNotificationsEnabled,
    [form, initialForm],
  );

  const handleChange = (field) => (e) => {
    setForm((prev) => ({ ...prev, [field]: e.target.value }));
  };

  const handleUseDefaultAvatar = () => {
    setForm((prev) => ({ ...prev, avatar: '' }));
    setFieldErrors((prev) => ({ ...prev, avatar: undefined }));
  };

  const handleAvatarFileChange = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;

    if (!file.type.startsWith('image/')) {
      setFieldErrors((prev) => ({ ...prev, avatar: 'Vui lòng chọn file ảnh hợp lệ.' }));
      return;
    }
    if (file.size > MAX_AVATAR_FILE_SIZE) {
      setFieldErrors((prev) => ({ ...prev, avatar: 'Ảnh đại diện không vượt quá 2MB.' }));
      return;
    }

    setIsUploadingAvatar(true);
    setBanner(null);
    setFieldErrors((prev) => ({ ...prev, avatar: undefined }));
    try {
      const result = await uploadMyAvatar(file);
      setForm((prev) => ({ ...prev, avatar: normalizeAvatarUrl(result.avatar_url) }));
      setBanner({ type: 'success', message: 'Đã tải ảnh lên. Bấm Lưu thay đổi để cập nhật hồ sơ.' });
    } catch (error) {
      setFieldErrors((prev) => ({
        ...prev,
        avatar: error.message || 'Tải ảnh đại diện thất bại.',
      }));
    } finally {
      setIsUploadingAvatar(false);
    }
  };

  const handleReset = () => {
    setForm(initialForm);
    setFieldErrors({});
    setBanner(null);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errors = validateForm(form);
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) {
      return;
    }

    setIsSaving(true);
    setBanner(null);
    try {
      const updated = await updateMe({
        display_name: form.displayName.trim(),
        profile: {
          school: form.school.trim(),
          address: form.address.trim(),
          avatar: normalizeAvatarUrl(form.avatar),
          notification_email: (form.notificationEmail || '').trim(),
          email_notifications_enabled: !!form.emailNotificationsEnabled,
        },
      });
      onProfileUpdated(updated);
      const next = toFormState(updated);
      setInitialForm(next);
      setForm(next);
      setBanner({ type: 'success', message: 'Đã lưu thay đổi hồ sơ.' });
    } catch (error) {
      setBanner({ type: 'error', message: error.message || 'Cập nhật hồ sơ thất bại.' });
    } finally {
      setIsSaving(false);
    }
  };

  const avatarPreview = normalizeAvatarUrl(form.avatar) || buildFallbackAvatar(form.displayName);

  return (
    <form className="card profile-card" onSubmit={handleSubmit}>
      <div className="profile-card-heading">
        <span className="profile-section-icon"><FontAwesomeIcon icon={faUser} /></span>
        <div>
          <h3 className="profile-card-title">Thông tin cá nhân</h3>
          <p>Cập nhật thông tin để hoàn thiện hồ sơ của bạn.</p>
        </div>
      </div>

      {banner && (
        <div className={`profile-banner profile-banner--${banner.type}`}>{banner.message}</div>
      )}

      <div className="profile-form-section profile-form-section--avatar">
        <h4><FontAwesomeIcon icon={faUser} /> Ảnh đại diện</h4>
        <div className="field-group">
          <div className="avatar-edit-row">
            <img src={avatarPreview} alt="Xem trước ảnh đại diện" className="avatar-preview" referrerPolicy="no-referrer" />
            <div className="avatar-edit-controls">
              <details className="avatar-url-options">
                <summary>Dùng ảnh từ liên kết</summary>
                <label className="field-label" htmlFor="profile-avatar-url">Liên kết ảnh</label>
                <input
                id="profile-avatar-url"
                className="field-input"
                placeholder="Dán URL ảnh (https://...)"
                value={form.avatar}
                onChange={handleChange('avatar')}
                disabled={isLoading || isUploadingAvatar}
                />
              </details>
              <div className="avatar-button-row">
                <input
                  ref={avatarInputRef}
                  className="avatar-file-input"
                  type="file"
                  accept="image/png,image/jpeg,image/webp,image/gif"
                  onChange={handleAvatarFileChange}
                  disabled={isLoading || isUploadingAvatar}
                />
                <button
                  type="button"
                  className="btn btn--primary btn--small"
                  onClick={() => avatarInputRef.current?.click()}
                  disabled={isLoading || isUploadingAvatar}
                >
                  <FontAwesomeIcon icon={faUpload} />
                  {isUploadingAvatar ? 'Đang tải...' : 'Tải ảnh từ thiết bị'}
                </button>
                <button
                  type="button"
                  className="btn btn--outline btn--small"
                  onClick={handleUseDefaultAvatar}
                  disabled={isLoading || isUploadingAvatar}
                >
                  <FontAwesomeIcon icon={faTrashCan} />
                  Dùng ảnh mặc định
                </button>
              </div>
            </div>
          </div>
          {fieldErrors.avatar && <span className="field-error">{fieldErrors.avatar}</span>}
        </div>
      </div>

      <div className="profile-form-section">
        <h4><FontAwesomeIcon icon={faUser} /> Thông tin cơ bản</h4>
        <div className="field-group">
          <label className="field-label" htmlFor="profile-display-name">Họ và tên</label>
          <input
            id="profile-display-name"
            className="field-input"
            value={form.displayName}
            onChange={handleChange('displayName')}
            disabled={isLoading}
          />
          {fieldErrors.displayName && <span className="field-error">{fieldErrors.displayName}</span>}
        </div>

        <div className="field-row-2">
          <div className="field-group">
            <label className="field-label">Email</label>
            <input className="field-input" value={user?.email || ''} disabled />
            <span className="field-hint">Không thể thay đổi email tại đây.</span>
          </div>
          <div className="field-group">
            <label className="field-label">Vai trò</label>
            <input className="field-input" value={ROLE_LABELS[user?.role] || user?.role || ''} disabled />
            <span className="field-hint">Chỉ quản trị viên mới thay đổi được vai trò.</span>
          </div>
        </div>
      </div>

      <div className="profile-form-section">
        <h4><FontAwesomeIcon icon={faBriefcase} /> Thông tin công tác</h4>
        <div className="field-group">
          <label className="field-label">Đơn vị công tác</label>
          <input
            className="field-input"
            value={form.school}
            onChange={handleChange('school')}
            maxLength={MAX_SCHOOL_LENGTH}
            disabled={isLoading}
          />
          {fieldErrors.school && <span className="field-error">{fieldErrors.school}</span>}
        </div>

        <div className="field-group">
          <label className="field-label">Địa chỉ</label>
          <input
            className="field-input"
            placeholder="Chưa cập nhật"
            value={form.address}
            onChange={handleChange('address')}
            maxLength={MAX_ADDRESS_LENGTH}
            disabled={isLoading}
          />
          {fieldErrors.address && <span className="field-error">{fieldErrors.address}</span>}
        </div>
      </div>

      <div className="profile-form-section">
        <h4><FontAwesomeIcon icon={faFileLines} /> Thông báo qua email</h4>
        <div className="field-group">
          <label className="field-label">Email nhận thông báo</label>
          <input
            className="field-input"
            type="email"
            placeholder={`Để trống để dùng ${user?.email || 'email đăng nhập'}`}
            value={form.notificationEmail}
            onChange={handleChange('notificationEmail')}
            maxLength={320}
            disabled={isLoading}
          />
          <span className="field-hint">Nếu bỏ trống, hệ thống dùng email đăng nhập của bạn.</span>
          {fieldErrors.notificationEmail && <span className="field-error">{fieldErrors.notificationEmail}</span>}
        </div>
        <div className="field-group">
          <label className="field-label" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <input
              type="checkbox"
              checked={!!form.emailNotificationsEnabled}
              onChange={(e) => setForm((prev) => ({ ...prev, emailNotificationsEnabled: e.target.checked }))}
              disabled={isLoading}
            />
            Nhận email thông báo từ QBankCTU (liên hệ, xuất bản, ...)
          </label>
        </div>
      </div>

      <div className="profile-actions">
        <button className="btn btn--primary" type="submit" disabled={isSaving || isLoading || !isDirty}>
          <FontAwesomeIcon icon={faUpload} />
          {isSaving ? 'Đang lưu...' : 'Lưu thay đổi'}
        </button>
        <button
          className="btn btn--outline"
          type="button"
          onClick={handleReset}
          disabled={isSaving || isLoading || !isDirty}
        >
          <FontAwesomeIcon icon={faRotateRight} />
          Khôi phục
        </button>
      </div>
    </form>
  );
}

function SecurityTab() {
  const isPasswordAccount = (auth?.currentUser?.providerData || []).some(
    (provider) => provider.providerId === 'password',
  );

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [fieldErrors, setFieldErrors] = useState({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [banner, setBanner] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errors = {};
    if (!currentPassword) errors.currentPassword = 'Vui lòng nhập mật khẩu hiện tại.';
    if (!newPassword || newPassword.length < 6) errors.newPassword = 'Mật khẩu mới cần tối thiểu 6 ký tự.';
    if (newPassword !== confirmPassword) errors.confirmPassword = 'Xác nhận mật khẩu không khớp.';
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    setIsSubmitting(true);
    setBanner(null);
    try {
      if (!auth?.currentUser) {
        throw new Error('Firebase web app chưa được cấu hình hoặc phiên đăng nhập đã hết hạn.');
      }
      const credential = EmailAuthProvider.credential(auth.currentUser.email, currentPassword);
      await reauthenticateWithCredential(auth.currentUser, credential);
      await updatePassword(auth.currentUser, newPassword);
      setBanner({ type: 'success', message: 'Đổi mật khẩu thành công.' });
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
    } catch (error) {
      setBanner({ type: 'error', message: mapFirebaseAuthError(error) });
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="card profile-card">
      <h3 className="profile-card-title">Bảo mật</h3>

      <div className="field-group">
        <label className="field-label">Phương thức đăng nhập hiện tại</label>
        <input className="field-input" value={isPasswordAccount ? 'Email/Mật khẩu' : 'Google'} disabled />
      </div>

      {!isPasswordAccount ? (
        <p className="field-hint">
          Tài khoản này đăng nhập bằng Google nên mật khẩu được quản lý bởi Google.
          Vui lòng đổi mật khẩu tại{' '}
          <a href="https://myaccount.google.com/security" target="_blank" rel="noreferrer">
            myaccount.google.com/security
          </a>.
        </p>
      ) : (
        <form onSubmit={handleSubmit}>
          {banner && (
            <div className={`profile-banner profile-banner--${banner.type}`}>{banner.message}</div>
          )}

          <div className="field-group">
            <label className="field-label">Mật khẩu hiện tại</label>
            <input
              className="field-input"
              type="password"
              placeholder="••••••••"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
            />
            {fieldErrors.currentPassword && <span className="field-error">{fieldErrors.currentPassword}</span>}
          </div>

          <div className="field-row-2">
            <div className="field-group">
              <label className="field-label">Mật khẩu mới</label>
              <input
                className="field-input"
                type="password"
                placeholder="••••••••"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
              />
              {fieldErrors.newPassword && <span className="field-error">{fieldErrors.newPassword}</span>}
            </div>
            <div className="field-group">
              <label className="field-label">Xác nhận mật khẩu mới</label>
              <input
                className="field-input"
                type="password"
                placeholder="••••••••"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
              />
              {fieldErrors.confirmPassword && <span className="field-error">{fieldErrors.confirmPassword}</span>}
            </div>
          </div>

          <button className="btn btn--outline" type="submit" disabled={isSubmitting}>
            {isSubmitting ? 'Đang xử lý...' : 'Đổi mật khẩu'}
          </button>
        </form>
      )}
    </div>
  );
}

function ProfileSidebar({ user }) {
  const quickLinks = [
    { path: '/tong-quan', label: 'Tổng quan quản trị' },
    { path: '/sinh-cau-hoi', label: 'Sinh câu hỏi' },
    { path: '/quan-ly', label: 'Quản lý câu hỏi' },
    { path: '/kiem-duyet', label: 'Hàng kiểm duyệt' },
    { path: '/lich-cong-viec', label: 'Lịch công việc' },
    { path: '/huong-dan', label: 'Hướng dẫn sử dụng' },
    { path: '/lien-he', label: 'Liên hệ hỗ trợ' },
  ].filter((link) => canAccessPath(user, link.path));
  const permissionLabels = user?.role === 'Admin'
    ? ['Toàn quyền quản trị hệ thống']
    : permissionsForUser(user).map((key) => PERMISSION_LABELS[key]).filter(Boolean);
  const reviewSubjectCount = Array.isArray(user?.review_subject_ids) ? user.review_subject_ids.length : 0;

  const displayStatus = user?.is_active ? 'Đang hoạt động' : 'Ngừng hoạt động';
  const joinedAt = user?.created_at ? new Date(user.created_at).toLocaleDateString('vi-VN') : '—';

  return (
    <aside className="profile-side">
      <div className="card side-card">
        <h3><FontAwesomeIcon icon={faCircleCheck} /> Trạng thái tài khoản</h3>
        <div className="status-row">
          <span className={`status-dot ${user?.is_active ? '' : 'status-dot--inactive'}`} />
          {displayStatus}
        </div>
        <ul className="info-list">
          <li>
            <span className="info-list-label"><FontAwesomeIcon icon={faUserGroup} /> Vai trò</span>
            <span>{ROLE_LABELS[user?.role] || user?.role || '—'}</span>
          </li>
          <li>
            <span className="info-list-label"><FontAwesomeIcon icon={faFileLines} /> Ngày tham gia</span>
            <span>{joinedAt}</span>
          </li>
          {user?.role === 'Reviewer' && (
            <li>
              <span className="info-list-label"><FontAwesomeIcon icon={faBriefcase} /> Học phần phụ trách</span>
              <span>{reviewSubjectCount > 0 ? `${reviewSubjectCount} học phần` : 'Tất cả học phần'}</span>
            </li>
          )}
        </ul>
      </div>

      <div className="card side-card">
        <h3><FontAwesomeIcon icon={faShieldHalved} /> Quyền đang có</h3>
        {permissionLabels.length > 0 ? (
          <ul className="profile-permission-list">
            {permissionLabels.map((label) => <li key={label}>{label}</li>)}
          </ul>
        ) : (
          <p className="profile-permission-empty">Chưa được cấp quyền nào. Liên hệ quản trị viên.</p>
        )}
      </div>

      <div className="card side-card">
        <h3><FontAwesomeIcon icon={faArrowUpRightFromSquare} /> Truy cập nhanh</h3>
        <nav className="profile-quick-links" aria-label="Truy cập nhanh từ hồ sơ">
          {quickLinks.map((link) => (
            <Link key={link.path} to={link.path}>
              <span>{link.label}</span>
              <FontAwesomeIcon icon={faArrowUpRightFromSquare} aria-hidden="true" />
            </Link>
          ))}
        </nav>
      </div>
    </aside>
  );
}

function UserProfile() {
  const { user, updateUser } = useContext(AuthContext);
  const [activeTab, setActiveTab] = useState('info');

  const displayName = user?.display_name || 'Giảng viên';
  const displayEmail = user?.email || '';
  const displayRole = ROLE_LABELS[user?.role] || user?.role || 'Giảng viên';
  const avatarUrl = normalizeAvatarUrl(user?.profile?.avatar) || buildFallbackAvatar(displayName);
  const handleEditProfile = () => {
    setActiveTab('info');
    window.requestAnimationFrame(() => {
      const field = document.getElementById('profile-display-name');
      field?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      field?.focus({ preventScroll: true });
    });
  };

  return (
    <main className="profile-page">
      <section className="page-hero">
        <div className="container">
        <div className="profile-hero-row">
          <div className="profile-avatar-wrap">
            <img src={avatarUrl} alt="Ảnh đại diện" className="profile-avatar" referrerPolicy="no-referrer" />
          </div>
          <div className="profile-hero-text">
            <span className="profile-role-badge">{displayRole}</span>
            <h1 className="page-hero-title">{displayName}</h1>
            <p className="page-hero-desc"><FontAwesomeIcon icon={faEnvelope} /> {displayEmail}</p>
            {user?.profile?.address && (
              <p className="profile-hero-meta"><FontAwesomeIcon icon={faLocationDot} /> {user.profile.address}</p>
            )}
          </div>
          <button
            type="button"
            className="btn profile-edit-button"
            onClick={handleEditProfile}
          >
            <FontAwesomeIcon icon={faPen} />
            Chỉnh sửa hồ sơ
          </button>
        </div>
        </div>
      </section>

      <section className="profile-body">
        <div className="container profile-grid">
          <div className="profile-main">
            <div className="profile-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === 'info'}
                className={`profile-tab ${activeTab === 'info' ? 'profile-tab--active' : ''}`}
                onClick={() => setActiveTab('info')}
              >
                <FontAwesomeIcon icon={faUser} />
                Thông tin cá nhân
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={activeTab === 'security'}
                className={`profile-tab ${activeTab === 'security' ? 'profile-tab--active' : ''}`}
                onClick={() => setActiveTab('security')}
              >
                <FontAwesomeIcon icon={faShieldHalved} />
                Bảo mật
              </button>
            </div>

            {activeTab === 'info' ? (
              <InfoTab user={user} onProfileUpdated={updateUser} />
            ) : (
              <SecurityTab />
            )}
          </div>

          <ProfileSidebar user={user} />
        </div>
      </section>
    </main>
  );
}

export default UserProfile;
