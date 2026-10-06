import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faBan,
  faChevronLeft,
  faChevronRight,
  faFileCsv,
  faFileExcel,
  faFilter,
  faPlay,
  faRotateRight,
  faSearch,
} from '@fortawesome/free-solid-svg-icons';
import { cancelAdminJob, listAdminJobs, retryAdminJob } from '../api/adminJobs';
import {
  downloadCsv,
  downloadXlsx,
  rowsToCsv,
  timestampedCsvFilename,
  timestampedXlsxFilename,
} from '../utils/csvExport';
import { useLocation } from 'react-router-dom';
import { STATUS_OPTIONS, parseJobFilters } from '../utils/adminJobFilters';
import '../css/AdminJobsPage.css';

const PAGE_SIZE = 25;
const EXPORT_PAGE_SIZE = 100;

const KIND_LABEL = {
  generation: 'Sinh câu hỏi',
  evaluation: 'Đánh giá',
  document: 'Tài liệu',
};

const JOB_TYPE_LABEL = {
  'generation': 'Sinh câu hỏi',
  'Generation': 'Sinh câu hỏi',
  'evaluation': 'Đánh giá câu hỏi',
  'Evaluation': 'Đánh giá câu hỏi',
  'document': 'Tài liệu',
  'DOCUMENT': 'Tài liệu',
  'EXTRACT_TEXT': 'Trích xuất văn bản',
  'CHUNK_AND_EMBED': 'Phân mảnh & Nhúng',
};

const STATUS_LABEL = {
  queued: 'Đang chờ',
  processing: 'Đang xử lý',
  failed: 'Thất bại',
  completed: 'Hoàn tất',
  error: 'Lỗi',
  stale: 'Cần chạy lại',
  cancelled: 'Đã hủy',
  QUEUED: 'Đang chờ',
  PROCESSING: 'Đang xử lý',
  COMPLETED: 'Hoàn tất',
  FAILED: 'Thất bại',
  ERROR: 'Lỗi',
  STALE: 'Cần chạy lại',
  BLOCKED: 'Bị chặn do thiếu bằng chứng',
  CANCELLED: 'Đã hủy',
};


function jobKey(job) {
  return `${job.kind}:${job.id}`;
}

function compactId(value) {
  if (!value) return 'Chưa có';
  const text = String(value);
  if (text.length <= 12) return text;
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
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

function formatAge(seconds) {
  if (seconds === null || seconds === undefined) return 'Chưa có';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} giờ`;
  return `${Math.floor(seconds / 86400)} ngày`;
}

const OUTCOME_LABEL = {
  SUCCEEDED: 'Hoàn tất',
  PARTIAL: 'Hoàn tất một phần',
  CANCELLED: 'Đã hủy',
  FAILED: 'Thất bại',
  DEAD_LETTER: 'Hết lượt thử lại',
};

function statusClass(status) {
  const normalized = String(status || 'unknown').toLowerCase();
  if (['failed', 'error', 'cancelled'].includes(normalized)) return 'danger';
  if (normalized === 'stale') return 'warning';
  if (['queued', 'processing'].includes(normalized)) return 'active';
  if (normalized === 'completed') return 'success';
  return 'muted';
}

function entityText(job) {
  const entity = job.entity || {};
  return entity.label || entity.id || 'Chưa gắn đối tượng';
}

const JOB_EXPORT_COLUMNS = [
  { header: 'Mã tác vụ (Job ID)', value: (job) => job.id },
  { header: 'Loại (Kind)', value: (job) => KIND_LABEL[job.kind] || job.kind || '' },
  { header: 'Trạng thái', value: (job) => STATUS_LABEL[job.status] || job.status || '' },
  { header: 'Tên đối tượng', value: entityText },
  { header: 'Loại đối tượng', value: (job) => job.entity?.type || '' },
  { header: 'Mã đối tượng', value: (job) => job.entity?.id || '' },
  { header: 'Tên người tạo', value: (job) => job.actor_user_name || job.actor_user_id || '' },
  { header: 'Ngày vào hàng đợi', value: (job) => job.queued_at || '' },
  { header: 'Ngày bắt đầu', value: (job) => job.started_at || '' },
  { header: 'Ngày hoàn tất', value: (job) => job.finished_at || '' },
  { header: 'Ngày cập nhật', value: (job) => job.updated_at || '' },
  { header: 'Tiến độ', value: (job) => job.progress ?? '' },
  { header: 'Thời gian chạy (giây)', value: (job) => job.age_seconds ?? '' },
  { header: 'Quá thời gian (Long running)', value: (job) => (job.is_long_running ? 'có' : 'không') },
  { header: 'Có thể chạy lại', value: (job) => (job.can_retry ? 'có' : 'không') },
  { header: 'Có thể hủy', value: (job) => (job.can_cancel ? 'có' : 'không') },
  { header: 'Kết quả', value: (job) => OUTCOME_LABEL[job.outcome] || '' },
  { header: 'Lỗi', value: (job) => job.error_message || '' },
  { header: 'Dữ liệu trạng thái', value: (job) => job.snapshot || {} },
];

function AdminJobsPage() {
  const location = useLocation();
  const initialFilters = parseJobFilters(location.search);
  const [jobs, setJobs] = useState([]);
  const [summary, setSummary] = useState({ total: 0, active: 0, failed: 0, long_running: 0 });
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [kindFilter, setKindFilter] = useState(initialFilters.kind);
  const [statusFilter, setStatusFilter] = useState(initialFilters.status);
  const [staleOnly, setStaleOnly] = useState(initialFilters.staleOnly);
  const [userIdFilter, setUserIdFilter] = useState('');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [searchInput, setSearchInput] = useState(initialFilters.search);
  const [searchTerm, setSearchTerm] = useState(initialFilters.search);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionKey, setActionKey] = useState('');
  const [exportKey, setExportKey] = useState('');
  const [selectedKey, setSelectedKey] = useState('');

  useEffect(() => {
    const filters = parseJobFilters(location.search);
    setKindFilter(filters.kind); setStatusFilter(filters.status); setStaleOnly(filters.staleOnly);
    setSearchInput(filters.search); setSearchTerm(filters.search); setPage(1);
  }, [location.search]);

  useEffect(() => {
    const handle = setTimeout(() => {
      setPage(1);
      setSearchTerm(searchInput.trim());
    }, 350);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const buildJobQuery = useCallback((nextPage = page, pageSize = PAGE_SIZE) => ({
    page: nextPage,
    pageSize,
    kind: kindFilter,
    status: statusFilter,
    staleOnly,
    search: searchTerm,
    userId: userIdFilter.trim(),
    dateFrom,
    dateTo,
  }), [dateFrom, dateTo, kindFilter, page, searchTerm, staleOnly, statusFilter, userIdFilter]);

  const fetchJobs = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await listAdminJobs(buildJobQuery());
      setJobs(result.items || []);
      setSummary(result.summary || { total: 0, active: 0, failed: 0, long_running: 0 });
      setTotal(result.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách tác vụ');
      setJobs([]);
      setSummary({ total: 0, active: 0, failed: 0, long_running: 0 });
      setTotal(0);
    } finally {
      setLoading(false);
    }
  }, [buildJobQuery]);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    if (!jobs.length) {
      setSelectedKey('');
      return;
    }
    if (!selectedKey || !jobs.some((job) => jobKey(job) === selectedKey)) {
      setSelectedKey(jobKey(jobs[0]));
    }
  }, [jobs, selectedKey]);

  const selectedJob = useMemo(
    () => jobs.find((job) => jobKey(job) === selectedKey) || jobs[0] || null,
    [jobs, selectedKey],
  );

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const updateKindFilter = (value) => {
    setKindFilter(value);
    setPage(1);
  };

  const updateStatusFilter = (value) => {
    setStatusFilter(value);
    setPage(1);
  };

  const toggleStaleOnly = () => {
    setStaleOnly((current) => !current);
    setPage(1);
  };

  const updateUserIdFilter = (value) => {
    setUserIdFilter(value);
    setPage(1);
  };

  const updateDateFrom = (value) => {
    setDateFrom(value);
    setPage(1);
  };

  const updateDateTo = (value) => {
    setDateTo(value);
    setPage(1);
  };

  const showAllJobs = () => {
    setStatusFilter('all');
    setStaleOnly(false);
    setPage(1);
  };

  const showActiveJobs = () => {
    setStaleOnly(false);
    updateStatusFilter('active');
  };

  const showRetryableJobs = () => {
    setStaleOnly(false);
    updateStatusFilter('retryable');
  };

  const showLongRunningJobs = () => {
    setStatusFilter('all');
    setStaleOnly(true);
    setPage(1);
  };

  const handleRetry = async (job) => {
    const key = `retry:${jobKey(job)}`;
    setActionKey(key);
    try {
      await retryAdminJob(job.kind, job.id);
      await fetchJobs();
    } catch (err) {
      window.alert(err.message || 'Chạy lại tác vụ thất bại');
    } finally {
      setActionKey('');
    }
  };

  const handleCancel = async (job) => {
    if (!window.confirm(`Hủy tác vụ ${compactId(job.id)}?`)) return;
    const key = `cancel:${jobKey(job)}`;
    setActionKey(key);
    try {
      await cancelAdminJob(job.kind, job.id);
      await fetchJobs();
    } catch (err) {
      window.alert(err.message || 'Hủy tác vụ thất bại');
    } finally {
      setActionKey('');
    }
  };

  const fetchJobsForExport = async () => {
    const firstResult = await listAdminJobs(buildJobQuery(1, EXPORT_PAGE_SIZE));
    const exportRows = [...(firstResult.items || [])];
    const expectedTotal = firstResult.total || exportRows.length;

    for (let nextPage = 2; exportRows.length < expectedTotal; nextPage += 1) {
      const result = await listAdminJobs(buildJobQuery(nextPage, EXPORT_PAGE_SIZE));
      const pageRows = result.items || [];
      if (!pageRows.length) break;
      exportRows.push(...pageRows);
    }

    return exportRows;
  };

  const exportJobsCsv = async () => {
    setExportKey('jobs-csv');
    try {
      const exportRows = await fetchJobsForExport();
      const csv = rowsToCsv(JOB_EXPORT_COLUMNS, exportRows);
      downloadCsv(timestampedCsvFilename('admin-jobs'), csv);
    } catch (err) {
      window.alert(err.message || 'Xuất CSV thất bại');
    } finally {
      setExportKey('');
    }
  };

  const exportJobsXlsx = async () => {
    setExportKey('jobs-xlsx');
    try {
      const exportRows = await fetchJobsForExport();
      downloadXlsx(timestampedXlsxFilename('admin-jobs'), JOB_EXPORT_COLUMNS, exportRows, 'Admin jobs');
    } catch (err) {
      window.alert(err.message || 'Xuất XLSX thất bại');
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
          <h1>Hàng đợi hệ thống</h1>
          <p>Theo dõi hàng đợi sinh câu hỏi, đánh giá chất lượng và xử lý tài liệu.</p>
        </div>
        <div className="jobs-header-actions">
          <button
            type="button"
            className="jobs-secondary-button"
            onClick={exportJobsCsv}
            disabled={exportDisabled}
          >
            <FontAwesomeIcon icon={faFileCsv} />
            <span>{exportKey === 'jobs-csv' ? 'Đang xuất' : 'Xuất CSV'}</span>
          </button>
          <button
            type="button"
            className="jobs-secondary-button"
            onClick={exportJobsXlsx}
            disabled={exportDisabled}
          >
            <FontAwesomeIcon icon={faFileExcel} />
            <span>{exportKey === 'jobs-xlsx' ? 'Đang xuất' : 'Xuất XLSX'}</span>
          </button>
          <button type="button" className="jobs-primary-button" onClick={fetchJobs} disabled={loading}>
            <FontAwesomeIcon icon={faRotateRight} />
            <span>{loading ? 'Đang tải' : 'Làm mới'}</span>
          </button>
        </div>
      </section>

      <section className="jobs-summary" aria-label="Tổng quan hàng đợi">
        <button type="button" className="summary-tile" onClick={showAllJobs}>
          <b>{summary.total}</b>
          <span>Tổng tác vụ</span>
        </button>
        <button type="button" className="summary-tile summary-tile--active" onClick={showActiveJobs}>
          <b>{summary.active}</b>
          <span>Đang chạy</span>
        </button>
        <button type="button" className="summary-tile summary-tile--danger" onClick={showRetryableJobs}>
          <b>{summary.failed}</b>
          <span>Cần xử lý</span>
        </button>
        <button type="button" className="summary-tile summary-tile--warning" onClick={showLongRunningJobs}>
          <b>{summary.long_running}</b>
          <span>Quá lâu</span>
        </button>
      </section>

      <section className="jobs-toolbar" aria-label="Bộ lọc hàng đợi">
        <div className="toolbar-field toolbar-field--search">
          <label htmlFor="job-search">
            <FontAwesomeIcon icon={faSearch} />
            Tìm kiếm
          </label>
          <input
            id="job-search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="Mã tác vụ, đối tượng, lỗi..."
          />
        </div>
        <div className="toolbar-field">
          <label htmlFor="job-kind">
            <FontAwesomeIcon icon={faFilter} />
            Loại
          </label>
          <select id="job-kind" value={kindFilter} onChange={(event) => updateKindFilter(event.target.value)}>
            <option value="all">Tất cả loại tác vụ</option>
            <option value="generation">Sinh câu hỏi</option>
            <option value="evaluation">Đánh giá</option>
            <option value="document">Tài liệu</option>
          </select>
        </div>
        <div className="toolbar-field">
          <label htmlFor="job-status">Trạng thái</label>
          <select id="job-status" value={statusFilter} onChange={(event) => updateStatusFilter(event.target.value)}>
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="toolbar-field">
          <label htmlFor="job-user">Người tạo</label>
          <input
            id="job-user"
            value={userIdFilter}
            onChange={(event) => updateUserIdFilter(event.target.value)}
            placeholder="User ID"
          />
        </div>
        <div className="toolbar-field">
          <label htmlFor="job-date-from">Từ ngày</label>
          <input
            id="job-date-from"
            type="date"
            value={dateFrom}
            max={dateTo || undefined}
            onChange={(event) => updateDateFrom(event.target.value)}
          />
        </div>
        <div className="toolbar-field">
          <label htmlFor="job-date-to">Đến ngày</label>
          <input
            id="job-date-to"
            type="date"
            value={dateTo}
            min={dateFrom || undefined}
            onChange={(event) => updateDateTo(event.target.value)}
          />
        </div>
        <label className="jobs-toggle">
          <input type="checkbox" checked={staleOnly} onChange={toggleStaleOnly} />
          Chỉ tác vụ quá lâu
        </label>
      </section>

      {error && <p className="jobs-error">{error}</p>}

      <section className="jobs-layout">
        <div className="jobs-table-panel">
          <div className="jobs-table-header">
            <div>
              <h2>Danh sách tác vụ</h2>
              <span>{total} kết quả</span>
            </div>
          </div>
          <div className="jobs-table-wrap">
            <table className="jobs-table">
              <thead>
                <tr>
                  <th>Loại</th>
                  <th>Trạng thái</th>
                  <th>Đối tượng</th>
                  <th>Người tạo</th>
                  <th>Cập nhật</th>
                  <th>Lỗi</th>
                  <th>Thao tác</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((job) => {
                  const key = jobKey(job);
                  return (
                    <tr
                      key={key}
                      className={selectedKey === key ? 'is-selected' : ''}
                      onClick={() => setSelectedKey(key)}
                    >
                      <td>
                        <strong>{KIND_LABEL[job.kind] || job.kind}</strong>
                      </td>
                      <td>
                        <span className={`status-pill status-pill--${statusClass(job.status)}`}>
                          {STATUS_LABEL[job.status] || job.status || 'Chưa rõ'}
                        </span>
                        {OUTCOME_LABEL[job.outcome] && job.outcome !== 'SUCCEEDED' && (
                          <small className="long-running">{OUTCOME_LABEL[job.outcome]}</small>
                        )}
                        {job.is_long_running && <small className="long-running">Quá ngưỡng</small>}
                      </td>
                      <td>
                        <span className="entity-text">{entityText(job)}</span>
                      </td>
                      <td>{job.actor_user_name || compactId(job.actor_user_id)}</td>
                      <td>
                        <span>{formatDateTime(job.updated_at || job.finished_at || job.started_at || job.queued_at)}</span>
                        <small>{formatAge(job.age_seconds)}</small>
                      </td>
                      <td>
                        <span className="error-cell">{job.error_message || 'Không có'}</span>
                      </td>
                      <td>
                        <div className="row-actions">
                          <button
                            type="button"
                            title="Chạy lại tác vụ"
                            disabled={!job.can_retry || Boolean(actionKey)}
                            onClick={(event) => {
                              event.stopPropagation();
                              handleRetry(job);
                            }}
                          >
                            <FontAwesomeIcon icon={faPlay} />
                          </button>
                          <button
                            type="button"
                            title="Hủy tác vụ"
                            disabled={!job.can_cancel || Boolean(actionKey)}
                            onClick={(event) => {
                              event.stopPropagation();
                              handleCancel(job);
                            }}
                          >
                            <FontAwesomeIcon icon={faBan} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!loading && jobs.length === 0 && (
              <p className="jobs-empty">Không có tác vụ phù hợp với bộ lọc hiện tại.</p>
            )}
            {loading && (
              <p className="jobs-empty">Đang tải danh sách tác vụ...</p>
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

        <aside className="job-detail-panel" aria-label="Chi tiết tác vụ">
          {selectedJob ? (
            <>
              <div className="job-detail-header">
                <span>{KIND_LABEL[selectedJob.kind] || selectedJob.kind}</span>
                <h2>{JOB_TYPE_LABEL[selectedJob.type] || selectedJob.type || compactId(selectedJob.id)}</h2>
              </div>
              <dl className="job-detail-list">
                <div>
                  <dt>Trạng thái</dt>
                  <dd>{STATUS_LABEL[selectedJob.status] || selectedJob.status || 'Chưa rõ'}</dd>
                </div>
                <div>
                  <dt>Đối tượng</dt>
                  <dd>{entityText(selectedJob)}</dd>
                </div>
                <div>
                  <dt>Đưa vào hàng đợi</dt>
                  <dd>{formatDateTime(selectedJob.queued_at)}</dd>
                </div>
                <div>
                  <dt>Bắt đầu</dt>
                  <dd>{formatDateTime(selectedJob.started_at)}</dd>
                </div>
                <div>
                  <dt>Hoàn tất</dt>
                  <dd>{formatDateTime(selectedJob.finished_at)}</dd>
                </div>
                <div>
                  <dt>Tiến độ</dt>
                  <dd>{selectedJob.progress === null || selectedJob.progress === undefined ? 'Chưa có' : `${selectedJob.progress}%`}</dd>
                </div>
              </dl>
              {selectedJob.error_message && (
                <div className="job-detail-error">
                  <span>Lỗi gần nhất</span>
                  <p>{selectedJob.error_message}</p>
                </div>
              )}
              <div className="job-detail-json">
                <span>Dữ liệu trạng thái</span>
                <pre>{JSON.stringify(selectedJob.snapshot || {}, null, 2)}</pre>
              </div>
            </>
          ) : (
            <p className="job-detail-empty">Chọn một tác vụ để xem chi tiết.</p>
          )}
        </aside>
      </section>
    </main>
  );
}

export default AdminJobsPage;
