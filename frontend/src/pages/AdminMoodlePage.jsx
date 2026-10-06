import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faBan,
  faCheckCircle,
  faCircleExclamation,
  faFloppyDisk,
  faPlug,
  faRotateRight,
  faSearch,
} from '@fortawesome/free-solid-svg-icons';
import {
  checkMoodleTarget,
  deactivateMoodleTarget,
  listMoodlePublications,
  listMoodleTargets,
  retryMoodlePublication,
  saveMoodleTarget,
} from '../api/adminMoodle';
import '../css/AdminMoodlePage.css';

const emptyForm = {
  site_key: '',
  site_name: '',
  mode: 'MOCK',
  base_url: '',
  token_env_var: '',
  default_course_id: '',
  default_category_id: '',
  allowed_roles: ['Admin', 'Reviewer'],
  is_active: true,
};

const PUBLISH_ROLES = [
  { value: 'Admin', label: 'Quản trị viên' },
  { value: 'Reviewer', label: 'Người duyệt' },
];

const MODE_LABEL = {
  MOCK: 'Mô phỏng',
  REST_API: 'REST API thật',
};

const ROLE_LABEL = Object.fromEntries(PUBLISH_ROLES.map((role) => [role.value, role.label]));

const STATUS_LABEL = {
  all: 'Tất cả trạng thái',
  PUBLISHED: 'Đã ghi nhận',
  FAILED: 'Lỗi',
  QUEUED: 'Đang chờ',
  PROCESSING: 'Đang xử lý',
};

function formatDateTime(value) {
  if (!value) return 'Chưa có';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Chưa có';
  return new Intl.DateTimeFormat('vi-VN', {
    hour: '2-digit',
    minute: '2-digit',
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  }).format(date);
}

function checkText(target) {
  const check = target?.last_check;
  if (!check) return 'Chưa kiểm tra';
  return check.ok ? 'Kết nối ổn' : 'Cần xử lý';
}

function publicationStatusClass(status) {
  if (status === 'PUBLISHED') return 'success';
  if (status === 'FAILED') return 'danger';
  if (['QUEUED', 'PROCESSING'].includes(status)) return 'active';
  return 'muted';
}

function formFromTarget(target = {}) {
  return {
    ...emptyForm,
    ...target,
    allowed_roles: target.allowed_roles?.length ? target.allowed_roles : emptyForm.allowed_roles,
  };
}

function AdminMoodlePage() {
  const [targets, setTargets] = useState([]);
  const [publications, setPublications] = useState([]);
  const [publicationSummary, setPublicationSummary] = useState(null);
  const [publicationTotal, setPublicationTotal] = useState(0);
  const [form, setForm] = useState(emptyForm);
  const [selectedKey, setSelectedKey] = useState('');
  const [isCreatingTarget, setIsCreatingTarget] = useState(false);
  const [publicationStatus, setPublicationStatus] = useState('all');
  const [siteFilter, setSiteFilter] = useState('all');
  const [searchInput, setSearchInput] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [loading, setLoading] = useState(true);
  const [publicationsLoading, setPublicationsLoading] = useState(true);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [checkingKey, setCheckingKey] = useState('');
  const [retryingId, setRetryingId] = useState('');

  useEffect(() => {
    const handle = setTimeout(() => setSearchTerm(searchInput.trim()), 350);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const loadTargets = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await listMoodleTargets();
      const items = result.items || [];
      setTargets(items);
      if (!selectedKey && !isCreatingTarget && items[0]) {
        setSelectedKey(items[0].site_key);
        setForm(formFromTarget(items[0]));
      }
    } catch (err) {
      setError(err.message || 'Không tải được danh sách kết nối Moodle');
    } finally {
      setLoading(false);
    }
  }, [isCreatingTarget, selectedKey]);

  const loadPublications = useCallback(async () => {
    setPublicationsLoading(true);
    try {
      const result = await listMoodlePublications({
        page: 1,
        pageSize: 50,
        status: publicationStatus,
        siteKey: siteFilter,
        search: searchTerm,
      });
      setPublications(result.items || []);
      setPublicationSummary(result.summary || { total: 0, published: 0, simulated: 0, failed: 0, pending: 0 });
      setPublicationTotal(result.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được lịch sử đưa câu hỏi lên Moodle');
      setPublications([]);
      setPublicationSummary(null);
      setPublicationTotal(0);
    } finally {
      setPublicationsLoading(false);
    }
  }, [publicationStatus, searchTerm, siteFilter]);

  useEffect(() => {
    loadTargets();
  }, [loadTargets]);

  useEffect(() => {
    loadPublications();
  }, [loadPublications]);

  const selectedTarget = useMemo(
    () => (isCreatingTarget ? null : targets.find((target) => target.site_key === selectedKey) || null),
    [isCreatingTarget, selectedKey, targets],
  );

  const pickTarget = (target) => {
    setIsCreatingTarget(false);
    setSelectedKey(target.site_key);
    setForm(formFromTarget(target));
  };

  const newTarget = () => {
    setIsCreatingTarget(true);
    setSelectedKey('');
    setForm(formFromTarget());
  };

  const updateForm = (field, value) => {
    setForm((current) => ({ ...current, [field]: value }));
  };

  const toggleAllowedRole = (role) => {
    setForm((current) => {
      const currentRoles = current.allowed_roles?.length ? current.allowed_roles : emptyForm.allowed_roles;
      const nextSet = new Set(currentRoles);
      if (nextSet.has(role) && nextSet.size > 1) {
        nextSet.delete(role);
      } else {
        nextSet.add(role);
      }
      return {
        ...current,
        allowed_roles: PUBLISH_ROLES
          .map((item) => item.value)
          .filter((item) => nextSet.has(item)),
      };
    });
  };

  const handleSave = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    try {
      const saved = await saveMoodleTarget({
        ...form,
        allowed_roles: form.allowed_roles?.length ? form.allowed_roles : ['Admin'],
      });
      setIsCreatingTarget(false);
      setSelectedKey(saved.site_key);
      setForm(formFromTarget(saved));
      await loadTargets();
    } catch (err) {
      setError(err.message || 'Lưu kết nối Moodle thất bại');
    } finally {
      setSaving(false);
    }
  };

  const handleCheck = async (target) => {
    setCheckingKey(target.site_key);
    setError('');
    try {
      const result = await checkMoodleTarget(target.site_key);
      setForm((current) => (
        current.site_key === target.site_key ? { ...current, last_check: result.check } : current
      ));
      await loadTargets();
    } catch (err) {
      setError(err.message || 'Kiểm tra kết nối Moodle thất bại');
    } finally {
      setCheckingKey('');
    }
  };

  const handleDeactivate = async (target) => {
    if (!window.confirm(`Khóa kết nối Moodle "${target.site_name}"? Câu hỏi sẽ không được đưa lên site này nữa.`)) return;
    setSaving(true);
    setError('');
    try {
      await deactivateMoodleTarget(target.site_key);
      await loadTargets();
    } catch (err) {
      setError(err.message || 'Khóa kết nối Moodle thất bại');
    } finally {
      setSaving(false);
    }
  };

  const handleRetryPublication = async (item) => {
    if (!window.confirm(`Gửi lại câu hỏi "${item.question_code || item.question_id}" lên Moodle?`)) return;
    setRetryingId(item.id);
    setError('');
    try {
      await retryMoodlePublication(item.id);
      await loadPublications();
    } catch (err) {
      setError(err.message || 'Gửi lại câu hỏi lên Moodle thất bại');
    } finally {
      setRetryingId('');
    }
  };

  return (
    <main className="admin-moodle-page">
      <section className="moodle-header">
        <div>
          <span>Quản trị hệ thống</span>
          <h1>Kết nối Moodle</h1>
          <p>Cấu hình site Moodle (khóa học, danh mục câu hỏi) và theo dõi các lượt đưa câu hỏi lên Moodle, ở chế độ mô phỏng hoặc REST API thật.</p>
        </div>
        <button type="button" className="moodle-primary-button" onClick={() => { loadTargets(); loadPublications(); }} disabled={loading || publicationsLoading}>
          <FontAwesomeIcon icon={faRotateRight} />
          <span>Làm mới</span>
        </button>
      </section>

      <section className="moodle-summary">
        <button type="button" onClick={() => setPublicationStatus('all')}>
          <b>{publicationSummary?.total ?? '—'}</b>
          <span>Tổng lượt đưa lên</span>
        </button>
        <button type="button" onClick={() => setPublicationStatus('PUBLISHED')}>
          <b>{publicationSummary?.published ?? '—'}</b>
          <span>Đã ghi nhận ({publicationSummary?.simulated || 0} mô phỏng)</span>
        </button>
        <button type="button" className="summary-danger" onClick={() => setPublicationStatus('FAILED')}>
          <b>{publicationSummary?.failed ?? '—'}</b>
          <span>Lỗi cần gửi lại</span>
        </button>
        <button type="button" onClick={() => setPublicationStatus('PROCESSING')}>
          <b>{publicationSummary?.pending ?? '—'}</b>
          <span>Đang xử lý</span>
        </button>
      </section>

      {error && <p className="moodle-error">{error}</p>}

      <section className="moodle-layout">
        <div className="moodle-target-panel">
          <div className="panel-heading">
            <div>
              <h2>Site Moodle</h2>
              <span>{targets.length} kết nối</span>
            </div>
            <button type="button" onClick={newTarget}>Thêm kết nối</button>
          </div>

          {loading ? (
            <p className="moodle-empty">Đang tải kết nối...</p>
          ) : (
            <div className="target-list">
              {targets.map((target) => (
                <article
                  key={target.site_key}
                  className={`target-row ${selectedKey === target.site_key ? 'target-row--active' : ''}`}
                  onClick={() => pickTarget(target)}
                >
                  <div className="target-main">
                    <div>
                      <strong>{target.site_name}</strong>
                      <small>{target.site_key} · {target.default_course_id}/{target.default_category_id}</small>
                    </div>
                    <span className={`target-check ${target.last_check?.ok ? 'target-check--ok' : ''}`}>
                      {checkText(target)}
                    </span>
                  </div>
                  <div className="target-meta">
                    <span>{MODE_LABEL[target.mode] || target.mode}</span>
                    <span>{target.is_active ? 'Đang dùng' : 'Đã khóa'}</span>
                    <span>{(target.allowed_roles?.length ? target.allowed_roles : emptyForm.allowed_roles).map((role) => ROLE_LABEL[role] || role).join(', ')}</span>
                    <span>{formatDateTime(target.last_check?.checked_at)}</span>
                  </div>
                  <div className="target-actions">
                    <button
                      type="button"
                      title="Kiểm tra kết nối"
                      disabled={checkingKey === target.site_key}
                      onClick={(event) => {
                        event.stopPropagation();
                        handleCheck(target);
                      }}
                    >
                      <FontAwesomeIcon icon={faPlug} />
                    </button>
                    <button
                      type="button"
                      title="Khóa kết nối"
                      disabled={!target.is_active || saving}
                      onClick={(event) => {
                        event.stopPropagation();
                        handleDeactivate(target);
                      }}
                    >
                      <FontAwesomeIcon icon={faBan} />
                    </button>
                  </div>
                </article>
              ))}
              {targets.length === 0 && <p className="moodle-empty">Chưa có kết nối Moodle nào.</p>}
            </div>
          )}
        </div>

        <form className="moodle-form-panel" onSubmit={handleSave}>
          <div className="panel-heading">
            <div>
              <h2>{selectedTarget ? 'Cập nhật kết nối' : 'Thêm kết nối'}</h2>
              <span>{selectedTarget?.last_check?.message || 'Token Moodle thật chỉ được đọc từ biến môi trường của máy chủ'}</span>
            </div>
          </div>
          <div className="form-grid">
            <label>
              Mã site
              <input value={form.site_key} onChange={(event) => updateForm('site_key', event.target.value)} />
            </label>
            <label>
              Tên site
              <input value={form.site_name} onChange={(event) => updateForm('site_name', event.target.value)} />
            </label>
            <label>
              Chế độ
              <select value={form.mode} onChange={(event) => updateForm('mode', event.target.value)}>
                <option value="MOCK">{MODE_LABEL.MOCK}</option>
                <option value="REST_API">{MODE_LABEL.REST_API}</option>
              </select>
            </label>
            <label>
              Trạng thái
              <select value={form.is_active ? 'true' : 'false'} onChange={(event) => updateForm('is_active', event.target.value === 'true')}>
                <option value="true">Đang dùng</option>
                <option value="false">Đã khóa</option>
              </select>
            </label>
            <label className="form-span">
              Địa chỉ site (URL)
              <input value={form.base_url || ''} onChange={(event) => updateForm('base_url', event.target.value)} placeholder="https://moodle.example.edu" />
            </label>
            <label>
              Biến môi trường chứa token
              <input value={form.token_env_var || ''} onChange={(event) => updateForm('token_env_var', event.target.value)} placeholder="MOODLE_API_TOKEN" />
            </label>
            <label>
              Mã khóa học (course ID)
              <input value={form.default_course_id} onChange={(event) => updateForm('default_course_id', event.target.value)} />
            </label>
            <label>
              Mã danh mục câu hỏi (category ID)
              <input value={form.default_category_id} onChange={(event) => updateForm('default_category_id', event.target.value)} />
            </label>
            <div className="form-span role-toggle-group">
              <span>Vai trò được đưa câu hỏi lên</span>
              <div>
                {PUBLISH_ROLES.map((role) => (
                  <label key={role.value}>
                    <input
                      type="checkbox"
                      checked={(form.allowed_roles || emptyForm.allowed_roles).includes(role.value)}
                      onChange={() => toggleAllowedRole(role.value)}
                    />
                    {role.label}
                  </label>
                ))}
              </div>
            </div>
          </div>
          <div className="form-actions">
            {selectedTarget?.last_check && (
              <span className={selectedTarget.last_check.ok ? 'check-ok' : 'check-fail'}>
                <FontAwesomeIcon icon={selectedTarget.last_check.ok ? faCheckCircle : faCircleExclamation} />
                {selectedTarget.last_check.message}
              </span>
            )}
            <button type="submit" disabled={saving}>
              <FontAwesomeIcon icon={faFloppyDisk} />
              <span>{saving ? 'Đang lưu' : 'Lưu kết nối'}</span>
            </button>
          </div>
        </form>
      </section>

      <section className="publication-panel">
        <div className="panel-heading">
          <div>
            <h2>Lịch sử đưa câu hỏi lên Moodle</h2>
            <span>{publicationTotal} kết quả</span>
          </div>
          <div className="publication-filters">
            <select value={siteFilter} onChange={(event) => setSiteFilter(event.target.value)}>
              <option value="all">Tất cả site</option>
              {targets.map((target) => (
                <option key={target.site_key} value={target.site_key}>{target.site_name}</option>
              ))}
            </select>
            <select value={publicationStatus} onChange={(event) => setPublicationStatus(event.target.value)}>
              {Object.entries(STATUS_LABEL).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <label>
              <FontAwesomeIcon icon={faSearch} />
              <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Mã câu hỏi, ref, ghi chú..." />
            </label>
          </div>
        </div>

        <div className="publication-table-wrap">
          <table className="publication-table">
            <thead>
              <tr>
                <th>Câu hỏi</th>
                <th>Site</th>
                <th>Trạng thái</th>
                <th>Mã trên Moodle</th>
                <th>Định dạng</th>
                <th>Thời gian</th>
                <th>Ghi chú</th>
                <th>Gửi lại</th>
              </tr>
            </thead>
            <tbody>
              {publications.map((item) => (
                <tr key={item.id}>
                  <td>
                    <strong>{item.question_code || item.question_id}</strong>
                    <small>Phiên bản {item.question_version}</small>
                  </td>
                  <td>
                    <span>{item.target?.site_name || item.target?.moodle_site_id || 'Site'}</span>
                    <small>{MODE_LABEL[item.publication_mode || item.target?.mode || 'MOCK']} · {item.target?.course_id}/{item.target?.category_id}</small>
                  </td>
                  <td>
                    <span className={`publication-status publication-status--${publicationStatusClass(item.status)}`}>
                      {item.status_label || STATUS_LABEL[item.status] || item.status || 'Chưa rõ'}
                    </span>
                  </td>
                  <td>
                    <span>{item.moodle_question_ref_id || 'Chưa có'}</span>
                    {item.publication_mode === 'MOCK' && <small>Mã mô phỏng, không phải ID thật trên Moodle</small>}
                  </td>
                  <td>{item.export_formats?.length ? item.export_formats.join(', ') : item.export_format}</td>
                  <td>{formatDateTime(item.created_at)}</td>
                  <td className="publication-error">{item.error_message || item.message || (item.external_sync === false ? 'Ghi nhận cục bộ' : 'Không có')}</td>
                  <td>
                    {item.status === 'FAILED' ? (
                      <button
                        type="button"
                        className="publication-retry-button"
                        title="Gửi lại lượt bị lỗi"
                        disabled={retryingId === item.id}
                        onClick={() => handleRetryPublication(item)}
                      >
                        <FontAwesomeIcon icon={faRotateRight} />
                      </button>
                    ) : (
                      <span className="publication-no-action">-</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {publicationsLoading && <p className="moodle-empty">Đang tải lịch sử...</p>}
          {!publicationsLoading && publications.length === 0 && <p className="moodle-empty">Không có lượt đưa lên nào phù hợp.</p>}
        </div>
      </section>
    </main>
  );
}

export default AdminMoodlePage;
