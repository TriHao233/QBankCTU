import React, { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faCheck,
  faChevronLeft,
  faChevronRight,
  faCopy,
  faEnvelope,
  faFileImport,
  faFilter,
  faKey,
  faLock,
  faLockOpen,
  faPen,
  faPlus,
  faRotateRight,
  faSearch,
  faXmark,
} from '@fortawesome/free-solid-svg-icons';
import { createUser, deleteUser, importUsers, inviteUser, listUsers, resetUserPassword, updateUser } from '../api/users';
import { ROLE_DEFAULT_PERMISSIONS, TEACHER_ASSIGNABLE_PERMISSIONS, REVIEW_ASSIGNABLE_PERMISSIONS, PERMISSION_LABELS, assignablePermissions } from '../auth/permissions';
import { normalizeAvatarUrl } from '../utils/avatarUrl';
import { listSubjects } from '../api/catalog';
import { suggestReviewSubjects } from '../api/questions';
import '../css/AdminJobsPage.css';
import '../css/UsersAdminPage.css';
import { notify } from '../utils/notify';

const PAGE_SIZE = 20;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

const ROLE_LABEL = {
  Admin: 'Quản trị viên',
  Teacher: 'Giảng viên',
  Reviewer: 'Người duyệt',
};

const ROLE_COLOR = {
  Admin: '#DC2626',
  Teacher: '#0c78d4',
  Reviewer: '#087f5b',
};

function PermissionFields({ role, permissions, onToggle }) {
  if (role === 'Admin') return <p className="field-hint">Quản trị viên luôn có toàn bộ quyền.</p>;
  return [['Giảng viên', TEACHER_ASSIGNABLE_PERMISSIONS], ['Kiểm duyệt', REVIEW_ASSIGNABLE_PERMISSIONS]].map(([label, keys]) => (
    <fieldset key={label} className="field-group"><legend>{label}</legend><div className="permission-grid">
      {keys.map((key) => <label className="field-checkbox" key={key}><input type="checkbox" checked={permissions.includes(key)} onChange={() => onToggle(key)} />{PERMISSION_LABELS[key]}</label>)}
    </div></fieldset>
  ));
}

const emptyCreateForm = {
  email: '',
  password: '',
  display_name: '',
  role: 'Teacher',
  permissions: [...ROLE_DEFAULT_PERMISSIONS.Teacher],
};

function permissionsForRole(role) {
  return role === 'Admin' ? [] : assignablePermissions(ROLE_DEFAULT_PERMISSIONS[role]);
}

function togglePermission(list, permission) {
  const current = new Set(list || []);
  if (current.has(permission)) {
    current.delete(permission);
  } else {
    current.add(permission);
  }
  return Array.from(current);
}

function initials(name) {
  const trimmed = (name || '').trim();
  if (!trimmed) return '?';
  const parts = trimmed.split(/\s+/);
  const first = parts[0]?.[0] || '';
  const last = parts.length > 1 ? parts[parts.length - 1][0] : '';
  return (first + last).toUpperCase() || '?';
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' }).format(date);
}

function parseImportRows(text) {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [email = '', displayName, role = 'Teacher', permissions = ''] = line.split(',').map((part) => part.trim());
      return {
        email,
        display_name: displayName || email,
        role: ROLE_LABEL[role] ? role : 'Teacher',
        permissions: role === 'Admin' ? [] : permissions
          ? assignablePermissions(permissions.split('|').map((item) => item.trim()).filter(Boolean))
          : permissionsForRole(ROLE_LABEL[role] ? role : 'Teacher'),
      };
    });
}

function UsersAdminPage() {
  const [users, setUsers] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [stats, setStats] = useState(null);

  const [searchParams, setSearchParams] = useSearchParams();
  const approvalFilter = searchParams.get('approval') === 'PENDING' ? 'PENDING' : '';
  const [roleFilter, setRoleFilter] = useState('all');
  const [searchInput, setSearchInput] = useState('');
  const [searchTerm, setSearchTerm] = useState('');

  const [showCreate, setShowCreate] = useState(false);
  const [createMode, setCreateMode] = useState('direct');
  const [createForm, setCreateForm] = useState(emptyCreateForm);
  const [creating, setCreating] = useState(false);
  const [inviteResult, setInviteResult] = useState(null);
  const [showImport, setShowImport] = useState(false);
  const [importText, setImportText] = useState('');
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);

  const [editing, setEditing] = useState(null);
  const [editDisplayName, setEditDisplayName] = useState('');
  const [editRole, setEditRole] = useState('Teacher');
  const [editActive, setEditActive] = useState(true);
  const [editPermissions, setEditPermissions] = useState([]);
  const [subjects, setSubjects] = useState(null);
  const [editReviewSubjects, setEditReviewSubjects] = useState([]);
  const [subjectSuggestions, setSubjectSuggestions] = useState(null);
  useEffect(() => { listSubjects().then(setSubjects).catch(() => setSubjects(null)); }, []);
  const showReviewSubjects = editRole === 'Reviewer' || editPermissions.includes('reviews.manage');
  useEffect(() => {
    let active = true;
    setSubjectSuggestions(null);
    if (editing && showReviewSubjects) {
      suggestReviewSubjects(editing.id)
        .then((result) => { if (active) setSubjectSuggestions(result); })
        .catch(() => {});
    }
    return () => { active = false; };
  }, [editing, showReviewSubjects]);
  const [saving, setSaving] = useState(false);

  const [togglingId, setTogglingId] = useState(null);
  const [resettingId, setResettingId] = useState(null);
  const [resetResult, setResetResult] = useState(null);
  const [copiedKey, setCopiedKey] = useState('');

  useEffect(() => {
    const handle = setTimeout(() => {
      setPage(1);
      setSearchTerm(searchInput.trim());
    }, 400);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const fetchUsers = async (pageArg = page) => {
    setLoading(true);
    setError('');
    try {
      const result = await listUsers({
        page: pageArg,
        pageSize: PAGE_SIZE,
        role: roleFilter === 'all' ? undefined : roleFilter,
        search: searchTerm || undefined,
        approvalStatus: approvalFilter || undefined,
      });
      setUsers(result.items || []);
      setTotal(result.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách người dùng');
      setUsers([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  };

  const fetchStats = async () => {
    try {
      const [allRes, adminRes, teacherRes, reviewerRes, pendingRes] = await Promise.all([
        listUsers({ page: 1, pageSize: 1 }),
        listUsers({ page: 1, pageSize: 1, role: 'Admin' }),
        listUsers({ page: 1, pageSize: 1, role: 'Teacher' }),
        listUsers({ page: 1, pageSize: 1, role: 'Reviewer' }),
        listUsers({ page: 1, pageSize: 1, approvalStatus: 'PENDING' }),
      ]);
      setStats({
        all: allRes.total || 0,
        Admin: adminRes.total || 0,
        Teacher: teacherRes.total || 0,
        Reviewer: reviewerRes.total || 0,
        PENDING: pendingRes.total || 0,
      });
    } catch {
      // stat tiles are non-critical; keep the previously known values on failure
    }
  };

  useEffect(() => {
    fetchUsers(page);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, searchTerm, roleFilter, approvalFilter]);

  useEffect(() => {
    fetchStats();
  }, []);

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const selectRoleFilter = (role) => {
    setRoleFilter(role);
    setPage(1);
    if (approvalFilter) setSearchParams({}, { replace: true });
  };

  const selectPendingFilter = () => {
    setRoleFilter('all');
    setPage(1);
    setSearchParams({ approval: 'PENDING' }, { replace: true });
  };

  const refreshAll = async (pageArg = page) => {
    await Promise.all([fetchUsers(pageArg), fetchStats()]);
  };

  const importRows = useMemo(() => parseImportRows(importText), [importText]);
  const importValidCount = importRows.filter((row) => EMAIL_RE.test(row.email)).length;
  const importInvalidCount = importRows.length - importValidCount;

  const copyToClipboard = async (text, key) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(key);
      setTimeout(() => setCopiedKey(''), 1500);
    } catch {
      // clipboard API may be unavailable (e.g. insecure context); user can still select the text manually
    }
  };

  const openCreate = (mode = 'direct') => {
    setCreateMode(mode);
    setInviteResult(null);
    setCreateForm({
      ...emptyCreateForm,
      password: '',
      permissions: permissionsForRole('Teacher'),
    });
    setShowCreate(true);
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    const email = createForm.email.trim();
    const displayName = createForm.display_name.trim();
    if (!email || !displayName) {
      notify('Vui lòng nhập đầy đủ email và họ tên.');
      return;
    }
    if (!EMAIL_RE.test(email)) {
      notify('Email không hợp lệ.');
      return;
    }
    if (createMode === 'direct' && createForm.password.length < 6) {
      notify('Mật khẩu phải có ít nhất 6 ký tự.');
      return;
    }
    setCreating(true);
    try {
      const payload = {
        ...createForm,
        email,
        display_name: displayName,
        permissions: createForm.role === 'Admin' ? [] : assignablePermissions(createForm.permissions),
      };
      if (createMode === 'invite') {
        const result = await inviteUser({
          email: payload.email,
          display_name: payload.display_name,
          role: payload.role,
          permissions: payload.permissions,
        });
        setInviteResult(result);
      } else {
        await createUser(payload);
        setShowCreate(false);
      }
      await refreshAll(1);
      setPage(1);
    } catch (err) {
      notify('Tạo tài khoản thất bại: ' + err.message);
    } finally {
      setCreating(false);
    }
  };

  const openEdit = (user) => {
    setEditing(user);
    setEditDisplayName(user.display_name || '');
    setEditRole(user.role);
    setEditActive(user.is_active);
    setEditPermissions(assignablePermissions(user.permissions || permissionsForRole(user.role)));
    setEditReviewSubjects(user.review_subject_ids || []);
  };

  const closeEdit = () => {
    if (saving) return;
    setEditing(null);
  };

  const handleSaveEdit = async (e) => {
    e.preventDefault();
    if (!editing) return;
    setSaving(true);
    try {
      await updateUser(editing.id, {
        display_name: editDisplayName,
        role: editRole,
        is_active: editActive,
        permissions: editRole === 'Admin' ? [] : assignablePermissions(editPermissions),
        // Không còn kiểm duyệt thì xoá học phần phụ trách cũ để không giữ dữ liệu thừa.
        ...(showReviewSubjects
          ? { review_subject_ids: editReviewSubjects }
          : (editing.review_subject_ids?.length ? { review_subject_ids: [] } : {})),
      });
      setEditing(null);
      await refreshAll();
    } catch (err) {
      notify('Cập nhật tài khoản thất bại: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleResetPassword = async (user) => {
    setResettingId(user.id);
    try {
      const result = await resetUserPassword(user.id);
      setResetResult(result);
    } catch (err) {
      notify('Tạo link reset mật khẩu thất bại: ' + err.message);
    } finally {
      setResettingId(null);
    }
  };

  const handleImportUsers = async (event) => {
    event.preventDefault();
    if (importRows.length === 0) {
      notify('Vui lòng nhập ít nhất một dòng CSV.');
      return;
    }
    setImporting(true);
    setImportResult(null);
    try {
      const result = await importUsers({ users: importRows });
      setImportResult(result);
      await refreshAll(1);
      setPage(1);
    } catch (err) {
      notify('Import tài khoản thất bại: ' + err.message);
    } finally {
      setImporting(false);
    }
  };

  const handleToggleActive = async (user) => {
    const activate = !user.is_active;
    const pending = user.approval_status === 'PENDING';
    const confirmMessage = pending
      ? `Duyệt và kích hoạt tài khoản "${user.display_name}" (${user.email}) với vai trò ${ROLE_LABEL[user.role] || user.role}?`
      : activate
        ? `Kích hoạt lại tài khoản "${user.display_name}" (${user.email})?`
        : `Vô hiệu hoá tài khoản "${user.display_name}" (${user.email})?`;
    if (!window.confirm(confirmMessage)) return;
    setTogglingId(user.id);
    try {
      if (activate) {
        await updateUser(user.id, { is_active: true });
      } else {
        await deleteUser(user.id);
      }
      await refreshAll();
    } catch (err) {
      notify((activate ? 'Kích hoạt' : 'Vô hiệu hoá') + ' tài khoản thất bại: ' + err.message);
    } finally {
      setTogglingId(null);
    }
  };

  return (
    <main className="admin-jobs-page users-page">
      <section className="jobs-header">
        <div>
          <span>Khu vực quản trị</span>
          <h1>Quản lý người dùng</h1>
          <p>Quản lý tài khoản giảng viên, người duyệt và quản trị viên trong hệ thống ngân hàng câu hỏi.</p>
        </div>
        <div className="jobs-header-actions">
          <button type="button" className="jobs-secondary-button" onClick={() => refreshAll()} disabled={loading}>
            <FontAwesomeIcon icon={faRotateRight} />
            <span>{loading ? 'Đang tải' : 'Làm mới'}</span>
          </button>
          <button type="button" className="jobs-secondary-button" onClick={() => setShowImport(true)}>
            <FontAwesomeIcon icon={faFileImport} />
            <span>Nhập từ CSV</span>
          </button>
          <button type="button" className="jobs-secondary-button" onClick={() => openCreate('invite')}>
            <FontAwesomeIcon icon={faEnvelope} />
            <span>Mời qua email</span>
          </button>
          <button type="button" className="jobs-primary-button" onClick={() => openCreate('direct')}>
            <FontAwesomeIcon icon={faPlus} />
            <span>Tạo tài khoản</span>
          </button>
        </div>
      </section>

      <section className="jobs-summary users-summary">
        <button type="button" className={`summary-tile ${roleFilter === 'all' && !approvalFilter ? 'summary-tile--active' : ''}`} onClick={() => selectRoleFilter('all')}>
          <b>{stats ? stats.all : '—'}</b>
          <span>Tổng tài khoản</span>
        </button>
        <button type="button" className={`summary-tile ${roleFilter === 'Admin' && !approvalFilter ? 'summary-tile--active' : ''}`} onClick={() => selectRoleFilter('Admin')}>
          <b>{stats ? stats.Admin : '—'}</b>
          <span>Quản trị viên</span>
        </button>
        <button type="button" className={`summary-tile ${roleFilter === 'Teacher' && !approvalFilter ? 'summary-tile--active' : ''}`} onClick={() => selectRoleFilter('Teacher')}>
          <b>{stats ? stats.Teacher : '—'}</b>
          <span>Giảng viên</span>
        </button>
        <button type="button" className={`summary-tile ${roleFilter === 'Reviewer' && !approvalFilter ? 'summary-tile--active' : ''}`} onClick={() => selectRoleFilter('Reviewer')}>
          <b>{stats ? stats.Reviewer : '—'}</b>
          <span>Người duyệt</span>
        </button>
        <button type="button" className={`summary-tile ${approvalFilter ? 'summary-tile--active' : ''} ${stats?.PENDING ? 'summary-tile--attention' : ''}`} onClick={selectPendingFilter}>
          <b>{stats ? stats.PENDING : '—'}</b>
          <span>Chờ kích hoạt</span>
        </button>
      </section>

      <section className="jobs-toolbar jobs-toolbar--users" aria-label="Bộ lọc người dùng">
        <div className="toolbar-field toolbar-field--search">
          <label htmlFor="users-search">
            <FontAwesomeIcon icon={faSearch} />
            Tìm kiếm
          </label>
          <div className="search-input-wrap">
            <input
              id="users-search"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Tìm theo tên hoặc email..."
            />
            {searchInput && (
              <button type="button" className="search-clear-btn" title="Xoá tìm kiếm" onClick={() => setSearchInput('')}>
                <FontAwesomeIcon icon={faXmark} />
              </button>
            )}
          </div>
        </div>
        <div className="toolbar-field">
          <label htmlFor="users-role">
            <FontAwesomeIcon icon={faFilter} />
            Vai trò
          </label>
          <select id="users-role" value={roleFilter} onChange={(e) => selectRoleFilter(e.target.value)}>
            <option value="all">Tất cả vai trò</option>
            <option value="Admin">Quản trị viên</option>
            <option value="Teacher">Giảng viên</option>
            <option value="Reviewer">Người duyệt</option>
          </select>
        </div>
      </section>

      {error && <p className="jobs-error">{error}</p>}

      <section className="jobs-layout jobs-layout--single">
        <div className="jobs-table-panel">
          <div className="jobs-table-header">
            <div>
              <h2>Danh sách người dùng</h2>
              <span>{total} tài khoản</span>
            </div>
          </div>
          <div className={`jobs-table-wrap ${loading && users.length > 0 ? 'is-loading' : ''}`}>
            <table className="jobs-table users-table">
              <thead>
                <tr>
                  <th>Người dùng</th>
                  <th>Vai trò</th>
                  <th>Quyền</th>
                  <th>Trạng thái</th>
                  <th>Ngày tạo</th>
                  <th>Thao tác</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>
                      <div className="user-cell">
                        {normalizeAvatarUrl(u.profile?.avatar) ? (
                          <img className="user-avatar-img" src={normalizeAvatarUrl(u.profile?.avatar)} alt="" referrerPolicy="no-referrer" />
                        ) : (
                          <span className="user-avatar-initials" style={{ background: ROLE_COLOR[u.role] || '#5c6f89' }}>
                            {initials(u.display_name)}
                          </span>
                        )}
                        <div>
                          <strong>{u.display_name}</strong>
                          <small>{u.email}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <span className={`role-pill role-pill--${(u.role || '').toLowerCase()}`}>{ROLE_LABEL[u.role] || u.role}</span>{u.role === 'Reviewer' && <small>{u.review_subject_ids?.length ? `${u.review_subject_ids.length} học phần` : 'Mọi học phần'}</small>}
                    </td>
                    <td>{(u.permissions || []).length} quyền</td>
                    <td>
                      {u.approval_status === 'PENDING' ? (
                        <span className="status-pill status-pill--warning">Chờ kích hoạt</span>
                      ) : u.is_active ? (
                        <span className="status-pill status-pill--success">Hoạt động</span>
                      ) : (
                        <span className="status-pill status-pill--danger">Đã khoá</span>
                      )}
                    </td>
                    <td>{formatDate(u.created_at)}</td>
                    <td>
                      <div className="row-actions">
                        <button
                          type="button"
                          title="Tạo link reset mật khẩu"
                          disabled={resettingId === u.id}
                          onClick={() => handleResetPassword(u)}
                        >
                          <FontAwesomeIcon icon={faKey} />
                        </button>
                        <button type="button" title="Chỉnh sửa" onClick={() => openEdit(u)}>
                          <FontAwesomeIcon icon={faPen} />
                        </button>
                        <button
                          type="button"
                          className={u.is_active ? 'danger-action' : u.approval_status === 'PENDING' ? 'approve-action' : ''}
                          title={u.approval_status === 'PENDING' ? 'Duyệt và kích hoạt tài khoản' : u.is_active ? 'Vô hiệu hoá tài khoản' : 'Kích hoạt lại tài khoản'}
                          disabled={togglingId === u.id}
                          onClick={() => handleToggleActive(u)}
                        >
                          <FontAwesomeIcon icon={u.approval_status === 'PENDING' ? faCheck : u.is_active ? faLock : faLockOpen} />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && users.length === 0 && (
              <p className="jobs-empty">
                {approvalFilter
                  ? 'Không có tài khoản nào đang chờ kích hoạt.'
                  : searchTerm || roleFilter !== 'all'
                  ? 'Không có người dùng nào khớp với bộ lọc hiện tại.'
                  : 'Chưa có người dùng nào trong hệ thống.'}
              </p>
            )}
            {loading && users.length === 0 && (
              <p className="jobs-empty">Đang tải danh sách người dùng...</p>
            )}
          </div>
          <div className="jobs-pagination">
            <button type="button" disabled={page <= 1 || loading} onClick={() => setPage((current) => Math.max(1, current - 1))}>
              <FontAwesomeIcon icon={faChevronLeft} />
            </button>
            <span>Trang {page} / {pageCount}</span>
            <button type="button" disabled={page >= pageCount || loading} onClick={() => setPage((current) => Math.min(pageCount, current + 1))}>
              <FontAwesomeIcon icon={faChevronRight} />
            </button>
          </div>
        </div>
      </section>

      {showCreate && (
        <div className="modal-overlay" onClick={() => !creating && setShowCreate(false)}>
          <form className="modal-card" onClick={(e) => e.stopPropagation()} onSubmit={handleCreate}>
            <h3 className="modal-title">{createMode === 'invite' ? 'Mời tài khoản mới' : 'Tạo tài khoản bằng mật khẩu'}</h3>

            <div className="field-group">
              <label className="field-label">Email</label>
              <input
                className="field-input"
                type="email"
                value={createForm.email}
                onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })}
              />
            </div>

            {createMode === 'direct' && (
              <div className="field-group">
                <label className="field-label">Mật khẩu</label>
                <input
                  className="field-input"
                  type="password"
                  value={createForm.password}
                  onChange={(e) => setCreateForm({ ...createForm, password: e.target.value })}
                />
                <p className="field-hint">Tối thiểu 6 ký tự.</p>
              </div>
            )}

            <div className="field-group">
              <label className="field-label">Họ và tên</label>
              <input
                className="field-input"
                value={createForm.display_name}
                onChange={(e) => setCreateForm({ ...createForm, display_name: e.target.value })}
              />
            </div>

            <div className="field-group">
              <label className="field-label">Vai trò</label>
              <select
                className="field-select"
                value={createForm.role}
                onChange={(e) => setCreateForm({
                  ...createForm,
                  role: e.target.value,
                  permissions: permissionsForRole(e.target.value),
                })}
              >
                <option value="Teacher">Giảng viên</option>
                <option value="Reviewer">Người duyệt</option>
                <option value="Admin">Quản trị viên</option>
              </select>
            </div>

            <PermissionFields role={createForm.role} permissions={createForm.permissions || []} onToggle={(key) => setCreateForm({ ...createForm, permissions: togglePermission(createForm.permissions, key) })} />

            {inviteResult?.reset_link && (
              <div className="users-result-box">
                <div className="users-result-box-header">
                  <b>Link đặt mật khẩu</b>
                  <button type="button" className="copy-btn" onClick={() => copyToClipboard(inviteResult.reset_link, 'invite')}>
                    <FontAwesomeIcon icon={faCopy} />
                    {copiedKey === 'invite' ? 'Đã sao chép' : 'Sao chép'}
                  </button>
                </div>
                <textarea className="field-input" readOnly value={inviteResult.reset_link} rows={3} />
              </div>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={() => setShowCreate(false)} disabled={creating}>
                Huỷ
              </button>
              <button type="submit" className="btn btn--primary" disabled={creating}>
                {creating ? 'Đang lưu...' : (createMode === 'invite' ? 'Tạo link mời' : 'Tạo tài khoản')}
              </button>
            </div>
          </form>
        </div>
      )}

      {showImport && (
        <div className="modal-overlay" onClick={() => !importing && setShowImport(false)}>
          <form className="modal-card" onClick={(e) => e.stopPropagation()} onSubmit={handleImportUsers}>
            <h3 className="modal-title">Import người dùng</h3>
            <div className="field-group">
              <label className="field-label">CSV</label>
              <textarea
                className="field-input"
                rows={8}
                value={importText}
                onChange={(e) => setImportText(e.target.value)}
                placeholder="email@ctu.edu.vn,Nguyễn Văn A,Teacher,questions.generate|questions.manage_own"
              />
              <p className="field-hint">Mỗi dòng: email, họ tên, vai trò (Teacher/Reviewer/Admin), quyền cách nhau bởi dấu "|" (tuỳ chọn).</p>
              {importRows.length > 0 && (
                <p className={`field-hint ${importInvalidCount > 0 ? 'field-hint--warn' : ''}`}>
                  {importRows.length} dòng ({importValidCount} hợp lệ{importInvalidCount > 0 ? `, ${importInvalidCount} thiếu email hợp lệ` : ''})
                </p>
              )}
            </div>
            {importText.split(/\r?\n/).some((line) => (line.split(',')[3] || '').split('|').some((key) => key.trim() && !assignablePermissions([key.trim()]).length)) && <p className="field-hint field-hint--warn">Quyền không hợp lệ hoặc chỉ dành cho Admin sẽ bị bỏ khỏi CSV trước khi gửi.</p>}
            {importResult && (
              <div className="users-result-box">
                <b>{importResult.created} tạo thành công, {importResult.failed} lỗi</b>
                {(importResult.items || []).slice(0, 8).map((item) => (
                  <p key={item.email}>{item.email}: {item.ok ? 'OK' : item.error}</p>
                ))}
              </div>
            )}
            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={() => setShowImport(false)} disabled={importing}>
                Đóng
              </button>
              <button type="submit" className="btn btn--primary" disabled={importing || importRows.length === 0}>
                {importing ? 'Đang import...' : 'Import'}
              </button>
            </div>
          </form>
        </div>
      )}

      {resetResult && (
        <div className="modal-overlay" onClick={() => setResetResult(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <h3 className="modal-title">Link reset mật khẩu</h3>
            <p className="user-email">{resetResult.email}</p>
            <div className="users-result-box-header">
              <b>Link đặt lại mật khẩu</b>
              <button type="button" className="copy-btn" onClick={() => copyToClipboard(resetResult.reset_link, 'reset')}>
                <FontAwesomeIcon icon={faCopy} />
                {copiedKey === 'reset' ? 'Đã sao chép' : 'Sao chép'}
              </button>
            </div>
            <textarea className="field-input" readOnly rows={4} value={resetResult.reset_link} />
            <div className="modal-actions">
              <button type="button" className="btn btn--primary" onClick={() => setResetResult(null)}>
                Đóng
              </button>
            </div>
          </div>
        </div>
      )}

      {editing && (
        <div className="modal-overlay" onClick={closeEdit}>
          <form className="modal-card" onClick={(e) => e.stopPropagation()} onSubmit={handleSaveEdit}>
            <h3 className="modal-title">Chỉnh sửa tài khoản</h3>

            <div className="field-group">
              <label className="field-label">Email</label>
              <input className="field-input" value={editing.email} disabled />
            </div>

            <div className="field-group">
              <label className="field-label">Họ và tên</label>
              <input
                className="field-input"
                value={editDisplayName}
                onChange={(e) => setEditDisplayName(e.target.value)}
              />
            </div>

            <div className="field-group">
              <label className="field-label">Vai trò</label>
              <select
                className="field-select"
                value={editRole}
                onChange={(e) => {
                  setEditRole(e.target.value);
                  setEditPermissions(permissionsForRole(e.target.value));
                }}
              >
                <option value="Teacher">Giảng viên</option>
                <option value="Reviewer">Người duyệt</option>
                <option value="Admin">Quản trị viên</option>
              </select>
            </div>

            <PermissionFields role={editRole} permissions={editPermissions} onToggle={(key) => setEditPermissions((current) => togglePermission(current, key))} />
            <p className="field-hint">Thay đổi quyền có hiệu lực trên giao diện sau khi người dùng đăng nhập lại.</p>
            {subjects && showReviewSubjects && (
              <fieldset className="field-group">
                <legend>Học phần phụ trách</legend>
                <p className="field-hint">Để trống = có thể nhận câu mọi học phần.</p>
                <div className="permission-grid">
                  {subjects
                    .map((item) => ({ item, id: String(item.id || item._id) }))
                    // Học phần đã ngừng vẫn hiện nếu đang được gán, để có thể bỏ tick.
                    .filter(({ item, id }) => item.is_active !== false || editReviewSubjects.includes(id))
                    .map(({ item, id }) => (
                      <label className="field-checkbox" key={id}>
                        <input
                          type="checkbox"
                          checked={editReviewSubjects.includes(id)}
                          onChange={() => setEditReviewSubjects((current) => togglePermission(current, id))}
                        />
                        {item.subject_code} — {item.subject_name}{item.is_active === false ? ' (đã ngừng)' : ''}
                      </label>
                    ))}
                </div>
                {Boolean(subjectSuggestions?.items?.length) && (
                  <div className="users-subject-suggestions">
                    <span>Gợi ý theo lịch sử duyệt:</span>
                    {subjectSuggestions.items.map((item) => (
                      <button
                        type="button"
                        key={item.subject_id}
                        disabled={editReviewSubjects.includes(String(item.subject_id))}
                        onClick={() => setEditReviewSubjects((current) => [...new Set([...current, String(item.subject_id)])])}
                      >
                        {item.subject_code} — {item.reviews} lượt duyệt / {subjectSuggestions.window_days} ngày
                      </button>
                    ))}
                  </div>
                )}
              </fieldset>
            )}

            <div className="field-group field-group--checkbox">
              <label className="field-checkbox">
                <input
                  type="checkbox"
                  checked={editActive}
                  onChange={(e) => setEditActive(e.target.checked)}
                />
                Tài khoản đang hoạt động
              </label>
            </div>

            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={closeEdit} disabled={saving}>
                Huỷ
              </button>
              <button type="submit" className="btn btn--primary" disabled={saving}>
                {saving ? 'Đang lưu...' : 'Lưu thay đổi'}
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}

export default UsersAdminPage;
