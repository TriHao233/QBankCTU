import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faChevronLeft,
  faChevronRight,
  faFileCsv,
  faFileExcel,
  faFilter,
  faRotateRight,
  faSearch,
} from '@fortawesome/free-solid-svg-icons';
import { listAdminAuditLogs } from '../api/adminAudit';
import {
  downloadCsv,
  downloadXlsx,
  rowsToCsv,
  timestampedCsvFilename,
  timestampedXlsxFilename,
} from '../utils/csvExport';
import '../css/AdminJobsPage.css';
import { notify } from '../utils/notify';
import { userOptionText, useUserOptions } from '../hooks/useUserOptions';

const PAGE_SIZE = 25;
const EXPORT_PAGE_SIZE = 100;

const ACTION_OPTIONS = [
  { value: 'all', label: 'Tất cả hành động' },
  { value: 'user.admin_update', label: 'Cập nhật người dùng' },
  { value: 'user.deactivate', label: 'Khóa người dùng' },
  { value: 'QUESTION_EVALUATED', label: 'Đánh giá câu hỏi' },
  { value: 'QUESTION_APPROVED', label: 'Duyệt câu hỏi' },
  { value: 'QUESTION_REJECTED', label: 'Từ chối câu hỏi' },
  { value: 'QUESTION_NEEDS_REVISION', label: 'Yêu cầu sửa' },
  { value: 'QUESTION_REVIEW_CLAIMED', label: 'Nhận kiểm duyệt' },
  { value: 'QUESTION_REVIEW_RELEASED', label: 'Trả câu kiểm duyệt' },
  { value: 'admin.job_retry', label: 'Chạy lại tác vụ' },
  { value: 'admin.job_cancel', label: 'Hủy tác vụ' },
  { value: 'admin.moodle_target_save', label: 'Lưu cấu hình Moodle' },
  { value: 'admin.moodle_target_deactivate', label: 'Tắt cấu hình Moodle' },
  { value: 'admin.moodle_target_check', label: 'Kiểm tra cấu hình Moodle' },
  { value: 'auth.demo_login', label: 'Đăng nhập demo' },
  { value: 'user.password_reset', label: 'Đặt lại mật khẩu' },
];

const ENTITY_OPTIONS = [
  { value: 'all', label: 'Tất cả đối tượng' },
  { value: 'user', label: 'Người dùng' },
  { value: 'QUESTION', label: 'Câu hỏi' },
  { value: 'generation', label: 'Tác vụ sinh câu hỏi' },
  { value: 'evaluation', label: 'Tác vụ đánh giá' },
  { value: 'document', label: 'Tác vụ tài liệu' },
  { value: 'moodle_target', label: 'Cấu hình Moodle' },
];

function compactId(value) {
  if (!value) return 'Chưa có';
  const text = String(value);
  if (text.length <= 14) return text;
  return `${text.slice(0, 7)}...${text.slice(-5)}`;
}

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

function actionLabel(action) {
  return ACTION_OPTIONS.find((option) => option.value === action)?.label
    || String(action || 'UNKNOWN').replace(/[._]/g, ' ');
}

function jsonText(value) {
  if (!value || (Array.isArray(value) && value.length === 0)) return '{}';
  if (!Array.isArray(value) && typeof value === 'object' && Object.keys(value).length === 0) return '{}';
  return JSON.stringify(value, null, 2);
}

function entityText(log) {
  const entity = log.entity || {};
  return `${entity.type || 'entity'} ${compactId(entity.id)}`;
}

function actorText(log) {
  const actor = log.actor || {};
  if (actor.user_name) return actor.user_name;
  if (actor.user_id) return compactId(actor.user_id);
  return actor.service_name || actor.type || 'Hệ thống';
}

const AUDIT_EXPORT_COLUMNS = [
  { header: 'Mã nhật ký (Audit ID)', value: (log) => log.id },
  { header: 'Ngày tạo', value: (log) => log.created_at || '' },
  { header: 'Thao tác (Action)', value: (log) => log.action || '' },
  { header: 'Nhãn thao tác', value: (log) => actionLabel(log.action) },
  { header: 'Người dùng/Hệ thống', value: actorText },
  { header: 'Vai trò', value: (log) => log.actor?.role || '' },
  { header: 'Loại tác nhân', value: (log) => log.actor?.type || '' },
  { header: 'Đối tượng', value: entityText },
  { header: 'Loại đối tượng', value: (log) => log.entity?.type || '' },
  { header: 'Mã đối tượng', value: (log) => log.entity?.id || '' },
  { header: 'Mã phiên bản đối tượng', value: (log) => log.entity?.version_id || '' },
  { header: 'Dữ liệu trước (Before)', value: (log) => log.before || {} },
  { header: 'Dữ liệu sau (After)', value: (log) => log.after || {} },
  { header: 'Thay đổi (Changes)', value: (log) => log.changes || [] },
  { header: 'Thông tin thêm (Metadata)', value: (log) => log.metadata || {} },
];

function AdminAuditPage() {
  const [logs, setLogs] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [actionFilter, setActionFilter] = useState('all');
  const [entityTypeFilter, setEntityTypeFilter] = useState('all');
  const [actorUserId, setActorUserId] = useState('');
  const userOptions = useUserOptions();
  const [entityId, setEntityId] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [exportKey, setExportKey] = useState('');
  const [selectedId, setSelectedId] = useState('');

  useEffect(() => {
    const handle = setTimeout(() => {
      setPage(1);
      setSearchTerm(searchInput.trim());
    }, 350);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const buildAuditQuery = useCallback((nextPage = page, pageSize = PAGE_SIZE) => ({
    page: nextPage,
    pageSize,
    search: searchTerm,
    action: actionFilter,
    entityType: entityTypeFilter,
    actorUserId: actorUserId.trim(),
    entityId: entityId.trim(),
    dateFrom,
    dateTo,
  }), [actionFilter, actorUserId, dateFrom, dateTo, entityId, entityTypeFilter, page, searchTerm]);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await listAdminAuditLogs(buildAuditQuery());
      setLogs(result.items || []);
      setTotal(result.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được nhật ký');
      setLogs([]);
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [buildAuditQuery]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  useEffect(() => {
    if (!logs.length) {
      setSelectedId('');
      return;
    }
    if (!selectedId || !logs.some((log) => log.id === selectedId)) {
      setSelectedId(logs[0].id);
    }
  }, [logs, selectedId]);

  const selectedLog = useMemo(
    () => logs.find((log) => log.id === selectedId) || logs[0] || null,
    [logs, selectedId],
  );
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const updateActionFilter = (value) => {
    setActionFilter(value);
    setPage(1);
  };

  const updateEntityTypeFilter = (value) => {
    setEntityTypeFilter(value);
    setPage(1);
  };

  const updateActorUserId = (value) => {
    setActorUserId(value);
    setPage(1);
  };

  const updateEntityId = (value) => {
    setEntityId(value);
    setPage(1);
  };

  const fetchAuditForExport = async () => {
    const firstResult = await listAdminAuditLogs(buildAuditQuery(1, EXPORT_PAGE_SIZE));
    const exportRows = [...(firstResult.items || [])];
    const expectedTotal = firstResult.total || exportRows.length;

    for (let nextPage = 2; exportRows.length < expectedTotal; nextPage += 1) {
      const result = await listAdminAuditLogs(buildAuditQuery(nextPage, EXPORT_PAGE_SIZE));
      const pageRows = result.items || [];
      if (!pageRows.length) break;
      exportRows.push(...pageRows);
    }

    return exportRows;
  };

  const exportAuditCsv = async () => {
    setExportKey('audit-csv');
    try {
      const exportRows = await fetchAuditForExport();
      const csv = rowsToCsv(AUDIT_EXPORT_COLUMNS, exportRows);
      downloadCsv(timestampedCsvFilename('admin-audit'), csv);
    } catch (err) {
      notify(err.message || 'Xuất CSV thất bại');
    } finally {
      setExportKey('');
    }
  };

  const exportAuditXlsx = async () => {
    setExportKey('audit-xlsx');
    try {
      const exportRows = await fetchAuditForExport();
      downloadXlsx(timestampedXlsxFilename('admin-audit'), AUDIT_EXPORT_COLUMNS, exportRows, 'Admin audit');
    } catch (err) {
      notify(err.message || 'Xuất XLSX thất bại');
    } finally {
      setExportKey('');
    }
  };

  const exportDisabled = loading || Boolean(exportKey) || total === 0;

  return (
    <main className="admin-jobs-page">
      <section className="jobs-header">
        <div>
          <span>Quản trị hệ thống</span>
          <h1>Nhật ký hệ thống</h1>
          <p>Theo dõi hành động nhạy cảm, thay đổi quyền và luồng xử lý trên toàn hệ thống.</p>
        </div>
        <div className="jobs-header-actions">
          <button
            type="button"
            className="jobs-secondary-button"
            onClick={exportAuditCsv}
            disabled={exportDisabled}
          >
            <FontAwesomeIcon icon={faFileCsv} />
            <span>{exportKey === 'audit-csv' ? 'Đang xuất' : 'Xuất CSV'}</span>
          </button>
          <button
            type="button"
            className="jobs-secondary-button"
            onClick={exportAuditXlsx}
            disabled={exportDisabled}
          >
            <FontAwesomeIcon icon={faFileExcel} />
            <span>{exportKey === 'audit-xlsx' ? 'Đang xuất' : 'Xuất XLSX'}</span>
          </button>
          <button type="button" className="jobs-primary-button" onClick={fetchLogs} disabled={loading}>
            <FontAwesomeIcon icon={faRotateRight} />
            <span>{loading ? 'Đang tải' : 'Làm mới'}</span>
          </button>
        </div>
      </section>

      <section className="jobs-toolbar jobs-toolbar--audit" aria-label="Bộ lọc nhật ký">
        <div className="toolbar-field toolbar-field--search">
          <label htmlFor="audit-search">
            <FontAwesomeIcon icon={faSearch} />
            Tìm kiếm
          </label>
          <input
            id="audit-search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Hành động, người thực hiện, đối tượng..."
          />
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-action">
            <FontAwesomeIcon icon={faFilter} />
            Hành động
          </label>
          <select id="audit-action" value={actionFilter} onChange={(event) => updateActionFilter(event.target.value)}>
            {ACTION_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-entity-type">Đối tượng</label>
          <select id="audit-entity-type" value={entityTypeFilter} onChange={(event) => updateEntityTypeFilter(event.target.value)}>
            {ENTITY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-actor">Người thực hiện</label>
          <select id="audit-actor" value={actorUserId} onChange={(event) => updateActorUserId(event.target.value)}>
            <option value="">Tất cả người dùng</option>
            {userOptions.map((option) => (
              <option key={option.id} value={option.id}>{userOptionText(option)}</option>
            ))}
          </select>
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-entity">Mã đối tượng</label>
          <input id="audit-entity" value={entityId} onChange={(event) => updateEntityId(event.target.value)} placeholder="Mã đối tượng (tùy chọn)" />
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-from">Từ ngày</label>
          <input id="audit-from" type="datetime-local" value={dateFrom} onChange={(event) => { setDateFrom(event.target.value); setPage(1); }} />
        </div>
        <div className="toolbar-field">
          <label htmlFor="audit-to">Đến ngày</label>
          <input id="audit-to" type="datetime-local" value={dateTo} onChange={(event) => { setDateTo(event.target.value); setPage(1); }} />
        </div>
      </section>

      {error && <p className="jobs-error">{error}</p>}

      <section className="jobs-layout">
        <div className="jobs-table-panel">
          <div className="jobs-table-header">
            <div>
              <h2>Nhật ký</h2>
              <span>{total} kết quả</span>
            </div>
          </div>
          <div className="jobs-table-wrap">
            <table className="jobs-table">
              <thead>
                <tr>
                  <th>Thời gian</th>
                  <th>Hành động</th>
                  <th>Người thực hiện</th>
                  <th>Đối tượng</th>
                </tr>
              </thead>
              <tbody>
                {logs.map((log) => (
                  <tr
                    key={log.id}
                    className={selectedId === log.id ? 'is-selected' : ''}
                    onClick={() => setSelectedId(log.id)}
                  >
                    <td>
                      <span>{formatDateTime(log.created_at)}</span>
                    </td>
                    <td>
                      <strong>{actionLabel(log.action)}</strong>
                    </td>
                    <td>
                      <span>{actorText(log)}</span>
                      <small>{log.actor?.role || log.actor?.type || 'actor'}</small>
                    </td>
                    <td>
                      <span className="entity-text">{entityText(log)}</span>
                      <small>{compactId(log.entity?.version_id)}</small>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && logs.length === 0 && (
              <p className="jobs-empty">Không có nhật ký phù hợp với bộ lọc hiện tại.</p>
            )}
            {loading && (
              <p className="jobs-empty">Đang tải nhật ký...</p>
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

        <aside className="job-detail-panel" aria-label="Chi tiết nhật ký">
          {selectedLog ? (
            <>
              <div className="job-detail-header">
                <span>{selectedLog.action}</span>
                <h2>{actionLabel(selectedLog.action)}</h2>
              </div>
              <dl className="job-detail-list">
                <div>
                  <dt>Thời gian</dt>
                  <dd>{formatDateTime(selectedLog.created_at)}</dd>
                </div>
                <div>
                  <dt>Người thực hiện</dt>
                  <dd>{actorText(selectedLog)}</dd>
                </div>
                <div>
                  <dt>Đối tượng</dt>
                  <dd>{entityText(selectedLog)}</dd>
                </div>
              </dl>
              <div className="audit-json-grid">
                <div className="job-detail-json">
                  <span>Trước</span>
                  <pre>{jsonText(selectedLog.before)}</pre>
                </div>
                <div className="job-detail-json">
                  <span>Sau</span>
                  <pre>{jsonText(selectedLog.after)}</pre>
                </div>
                <div className="job-detail-json">
                  <span>Thay đổi</span>
                  <pre>{jsonText(selectedLog.changes)}</pre>
                </div>
                <div className="job-detail-json">
                  <span>Dữ liệu bổ sung</span>
                  <pre>{jsonText(selectedLog.metadata)}</pre>
                </div>
              </div>
            </>
          ) : (
            <p className="job-detail-empty">Chọn một nhật ký để xem chi tiết.</p>
          )}
        </aside>
      </section>
    </main>
  );
}

export default AdminAuditPage;
