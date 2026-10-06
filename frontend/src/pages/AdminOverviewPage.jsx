import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faBookOpen,
  faClipboardCheck,
  faDatabase,
  faPlugCircleCheck,
  faRotateRight,
  faServer,
  faTriangleExclamation,
  faUsers,
} from '@fortawesome/free-solid-svg-icons';
import { getAdminOverview, listAdminDocuments } from '../api/adminOverview';
import '../css/AdminOverviewPage.css';

const JOB_TYPE_LABEL = {
  generation: 'Sinh câu hỏi',
  Generation: 'Sinh câu hỏi',
  evaluation: 'Đánh giá',
  Evaluation: 'Đánh giá',
  document: 'Tài liệu',
  DOCUMENT: 'Tài liệu',
};

const ACTION_LABEL = {
  'user.admin_update': 'Cập nhật người dùng',
  'user.deactivate': 'Khóa người dùng',
  'QUESTION_EVALUATED': 'Đánh giá câu hỏi',
  'QUESTION_APPROVED': 'Duyệt câu hỏi',
  'QUESTION_REJECTED': 'Từ chối câu hỏi',
  'QUESTION_NEEDS_REVISION': 'Yêu cầu sửa',
  'QUESTION_REVIEW_CLAIMED': 'Nhận kiểm duyệt',
  'QUESTION_REVIEW_RELEASED': 'Trả câu kiểm duyệt',
  'admin.job_retry': 'Chạy lại tác vụ',
  'admin.job_cancel': 'Hủy tác vụ',
  'admin.moodle_target_save': 'Lưu cấu hình Moodle',
  'admin.moodle_target_deactivate': 'Tắt cấu hình Moodle',
  'admin.moodle_target_check': 'Kiểm tra cấu hình Moodle',
  'auth.demo_login': 'Đăng nhập demo',
  'user.password_reset': 'Đặt lại mật khẩu',
};

const ENTITY_TYPE_LABEL = {
  'user': 'Người dùng',
  'QUESTION': 'Câu hỏi',
  'question': 'Câu hỏi',
  'generation': 'Sinh câu hỏi',
  'evaluation': 'Đánh giá',
  'document': 'Tài liệu',
  'moodle_target': 'Cấu hình Moodle',
  'subject': 'Môn học',
};

const PIPELINE_STATUS_LABEL = {
  NOT_STARTED: 'Chưa chạy',
  QUEUED: 'Chờ chạy',
  PROCESSING: 'Đang chạy',
  COMPLETED: 'Hoàn tất',
  FAILED: 'Lỗi',
  CANCELLED: 'Đã hủy',
};

function pipelineStatusLabel(value) {
  return PIPELINE_STATUS_LABEL[value] || value || '—';
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '—';
  return new Intl.NumberFormat('vi-VN').format(value);
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

function formatPercent(value) {
  return typeof value === 'number' ? `${Math.round(value * 100)}%` : '--';
}

function formatLatency(value) {
  if (typeof value !== 'number') return '--';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}

function formatCurrency(value) {
  if (typeof value !== 'number') return '--';
  return new Intl.NumberFormat('vi-VN', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

function compactId(value) {
  if (!value) return 'Chưa có';
  const text = String(value);
  if (text.length <= 12) return text;
  return `${text.slice(0, 6)}...${text.slice(-4)}`;
}

function AdminOverviewPage() {
  const [overview, setOverview] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [documentFilters, setDocumentFilters] = useState({ search: '', status: '' });
  const [documentFilterDraft, setDocumentFilterDraft] = useState({ search: '', status: '' });
  const [documentPage, setDocumentPage] = useState(1);
  const [documentRefresh, setDocumentRefresh] = useState(0);
  const [documentData, setDocumentData] = useState(null);
  const [documentsLoading, setDocumentsLoading] = useState(true);
  const [documentsError, setDocumentsError] = useState('');

  const loadOverview = useCallback(async () => {
    setDocumentRefresh((value) => value + 1);
    setLoading(true);
    setError('');
    try {
      setOverview(await getAdminOverview());
    } catch (err) {
      setError(err.message || 'Không tải được tổng quan hệ thống');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadOverview();
  }, [loadOverview]);

  useEffect(() => {
    let active = true;
    setDocumentsLoading(true);
    setDocumentsError('');
    listAdminDocuments({ page: documentPage, ...documentFilters }).then((result) => {
      if (active) setDocumentData(result);
    }).catch((err) => {
      if (active) { setDocumentData(null); setDocumentsError(err.message || 'Không tải được bảng tài liệu.'); }
    }).finally(() => { if (active) setDocumentsLoading(false); });
    return () => { active = false; };
  }, [documentPage, documentFilters, documentRefresh]);

  const stats = useMemo(() => {
    const users = overview?.users || {};
    const questions = overview?.questions || {};
    const documents = overview?.documents || {};
    const jobs = overview?.jobs || {};
    const moodle = overview?.moodle || {};
    return [
      {
        key: 'users',
        label: 'Người dùng hoạt động',
        value: users.active,
        detail: `${formatNumber(users.teachers)} giảng viên · ${formatNumber(users.reviewers)} người duyệt${users.pending ? ` · ${formatNumber(users.pending)} chờ kích hoạt` : ''}`,
        icon: faUsers,
      },
      {
        key: 'questions',
        label: 'Câu hỏi đang dùng',
        value: questions.total,
        detail: `${formatNumber(questions.pending)} chờ duyệt · ${formatNumber(questions.approved)} đã duyệt`,
        icon: faBookOpen,
      },
      {
        key: 'documents',
        label: 'Tài liệu',
        value: documents.total,
        detail: `${formatNumber(documents.processing)} đang xử lý · ${formatNumber(documents.failed)} lỗi`,
        icon: faDatabase,
      },
      {
        key: 'jobs',
        label: 'Hàng đợi cần xử lý',
        value: jobs.failed,
        detail: `${formatNumber(jobs.active)} đang chạy · ${formatNumber(jobs.long_running)} quá ngưỡng`,
        icon: faServer,
      },
      {
        key: 'moodle',
        label: 'Ghi mô phỏng Moodle',
        value: moodle.publications?.total,
        detail: `${formatNumber(moodle.publications?.simulated)} lượt mô phỏng · ${formatNumber(moodle.active_targets)} cấu hình hoạt động`,
        icon: faPlugCircleCheck,
      },
    ];
  }, [overview]);

  const attention = overview?.attention || [];
  const recentJobs = overview?.recent_jobs || [];
  const recentAudit = overview?.recent_audit || [];
  const quality = overview?.questions?.quality || {};
  const publications = overview?.moodle?.publications || {};
  const jobBreakdown = overview?.jobs?.breakdown || [];
  const modelPerformance = overview?.model_performance || [];
  const modelUsage = overview?.model_usage_summary || {};

  return (
    <main className="admin-overview-page">
      <section className="overview-header">
        <div>
          <span>Quản trị hệ thống</span>
          <h1>Tổng quan vận hành</h1>
          <p>Theo dõi sức khỏe hệ thống, hàng đợi, kiểm duyệt và mô phỏng Moodle trong một màn hình.</p>
        </div>
        <button type="button" className="overview-primary-button" onClick={loadOverview} disabled={loading}>
          <FontAwesomeIcon icon={faRotateRight} />
          <span>{loading ? 'Đang tải' : 'Làm mới'}</span>
        </button>
      </section>

      {error && <p className="overview-error">{error}</p>}

      <section className="overview-stats" aria-label="Chỉ số vận hành">
        {stats.map((item) => (
          <article className={`overview-stat overview-stat--${item.key}`} key={item.key}>
            <FontAwesomeIcon icon={item.icon} />
            <div>
              <span>{item.label}</span>
              <b>{formatNumber(item.value)}</b>
              <small>{item.detail}</small>
            </div>
          </article>
        ))}
      </section>

      <section className="overview-grid">
        <section className="overview-panel overview-panel--wide" aria-label="Giám sát tài liệu">
          <div className="overview-panel-heading">
            <div><span>Tài liệu toàn hệ thống · Chỉ đọc</span><h2>Giám sát tài liệu</h2></div>
            <Link to="/quan-ly-job?kind=document">Xem tác vụ tài liệu</Link>
          </div>
          <form className="overview-document-filters" onSubmit={(event) => {
            event.preventDefault(); setDocumentPage(1);
            setDocumentFilters({ ...documentFilterDraft, search: documentFilterDraft.search.trim() });
          }}>
            <label>Tìm tài liệu<input type="search" maxLength={200} placeholder="Tên tài liệu hoặc tên file" value={documentFilterDraft.search} onChange={(event) => setDocumentFilterDraft({ ...documentFilterDraft, search: event.target.value })} /></label>
            <label>Trạng thái<select value={documentFilterDraft.status} onChange={(event) => setDocumentFilterDraft({ ...documentFilterDraft, status: event.target.value })}>
              <option value="">Tất cả</option><option value="UPLOADED">Đã tải lên</option><option value="PROCESSING">Đang xử lý</option><option value="READY">Sẵn sàng</option><option value="FAILED">Lỗi</option>
            </select></label>
            <button type="submit" className="overview-primary-button" disabled={documentsLoading}>Lọc</button>
          </form>
          {documentsError && <p className="overview-error" role="alert">{documentsError}</p>}
          <div className="overview-table-wrap" aria-busy={documentsLoading}>
            <table className="overview-table"><thead><tr><th>Tài liệu</th><th>Người tải</th><th>Học phần</th><th>Trạng thái</th><th>Số trang</th><th>Xử lý / lỗi</th><th>Cập nhật</th></tr></thead><tbody>
              {(documentData?.items || []).map((item) => <tr key={item.id}>
                <td><strong>{item.title || item.original_filename}</strong><small>{item.original_filename}</small></td>
                <td>{item.owner?.display_name || item.owner?.email || 'Chưa có'}<small>{item.owner?.display_name ? item.owner?.email : ''}</small></td>
                <td>{item.subjects?.map((subject) => subject.name || subject.code || subject.id).join(', ') || 'Chưa gắn học phần'}</td>
                <td>{({ UPLOADED: 'Đã tải lên', PROCESSING: 'Đang xử lý', READY: 'Sẵn sàng', FAILED: 'Lỗi' })[item.status] || item.status}</td>
                <td>{item.page_count ?? '—'}</td>
                <td className="overview-error-cell">{item.error_message || <><span>OCR: {pipelineStatusLabel(item.pipeline_summary?.ocr_status)}</span><small>Tách đoạn: {pipelineStatusLabel(item.pipeline_summary?.chunk_status)}</small></>}</td>
                <td>{formatDateTime(item.updated_at)}</td>
              </tr>)}
            </tbody></table>
            {documentsLoading && <p className="overview-empty">Đang tải tài liệu...</p>}
            {!documentsLoading && !documentsError && !documentData?.items?.length && <p className="overview-empty">Không có tài liệu phù hợp.</p>}
          </div>
          {documentData && <div className="overview-document-pagination"><span>{formatNumber(documentData.total)} tài liệu · Trang {documentPage}</span>
            <button type="button" disabled={documentsLoading || documentPage <= 1} onClick={() => setDocumentPage((value) => value - 1)}>Trước</button>
            <button type="button" disabled={documentsLoading || documentPage * documentData.page_size >= documentData.total} onClick={() => setDocumentPage((value) => value + 1)}>Sau</button>
          </div>}
        </section>
        <section className="overview-panel overview-panel--attention">
          <div className="overview-panel-heading">
            <div>
              <span>Cần chú ý</span>
              <h2>Hàng đợi vận hành</h2>
            </div>
            <FontAwesomeIcon icon={faTriangleExclamation} />
          </div>
          <div className="attention-list">
            {attention.map((item) => (
              <Link className={`attention-row attention-row--${item.severity}`} to={item.path} key={item.key}>
                <span>{item.label}</span>
                <b>{formatNumber(item.count)}</b>
              </Link>
            ))}
          </div>
        </section>

        <section className="overview-panel">
          <div className="overview-panel-heading">
            <div>
              <span>Ngân hàng câu hỏi</span>
              <h2>Trạng thái kiểm duyệt</h2>
            </div>
            <FontAwesomeIcon icon={faClipboardCheck} />
          </div>
          <dl className="overview-breakdown overview-breakdown--three">
            <div>
              <dt>Nháp</dt>
              <dd>{formatNumber(overview?.questions?.draft)}</dd>
            </div>
            <div>
              <dt>Chờ duyệt</dt>
              <dd>{formatNumber(overview?.questions?.pending)}</dd>
            </div>
            <div>
              <dt>Đã duyệt</dt>
              <dd>{formatNumber(overview?.questions?.approved)}</dd>
            </div>
            <div>
              <dt>Cần sửa</dt>
              <dd>{formatNumber(overview?.questions?.needs_revision)}</dd>
            </div>
            <div>
              <dt>Từ chối</dt>
              <dd>{formatNumber(overview?.questions?.rejected)}</dd>
            </div>
            <div>
              <dt>Đã ghi Moodle</dt>
              <dd>{formatNumber(overview?.questions?.published)}</dd>
            </div>
          </dl>
        </section>

        <section className="overview-panel">
          <div className="overview-panel-heading">
            <div>
              <span>Chất lượng AI</span>
              <h2>Màu đánh giá</h2>
            </div>
            <FontAwesomeIcon icon={faClipboardCheck} />
          </div>
          <dl className="overview-breakdown">
            <div>
              <dt>Xanh lá</dt>
              <dd>{formatNumber(quality.green)}</dd>
            </div>
            <div>
              <dt>Vàng</dt>
              <dd>{formatNumber(quality.yellow)}</dd>
            </div>
            <div>
              <dt>Đỏ</dt>
              <dd>{formatNumber(quality.red)}</dd>
            </div>
            <div>
              <dt>Chưa chấm</dt>
              <dd>{formatNumber(quality.not_evaluated)}</dd>
            </div>
          </dl>
        </section>

        <section className="overview-panel">
          <div className="overview-panel-heading">
            <div>
              <span>Mô phỏng Moodle</span>
              <h2>Trạng thái ghi nhận</h2>
            </div>
            <FontAwesomeIcon icon={faPlugCircleCheck} />
          </div>
          <dl className="overview-breakdown">
            <div>
              <dt>Đã ghi</dt>
              <dd>{formatNumber(publications.published)}</dd>
            </div>
            <div>
              <dt>Lỗi</dt>
              <dd>{formatNumber(publications.failed)}</dd>
            </div>
            <div>
              <dt>Đang chờ</dt>
              <dd>{formatNumber(publications.pending)}</dd>
            </div>
            <div>
              <dt>Mô phỏng</dt>
              <dd>{formatNumber(publications.simulated)}</dd>
            </div>
          </dl>
        </section>

        <section className="overview-panel overview-panel--wide">
          <div className="overview-panel-heading">
            <div>
              <span>Hàng đợi</span>
              <h2>Tác vụ theo loại</h2>
            </div>
            <Link to="/quan-ly-job">Mở hàng đợi</Link>
          </div>
          <div className="overview-table-wrap">
            <table className="overview-table overview-table--compact">
              <thead>
                <tr>
                  <th>Loại</th>
                  <th>Tổng</th>
                  <th>Đang chạy</th>
                  <th>Lỗi</th>
                  <th>Quá ngưỡng</th>
                </tr>
              </thead>
              <tbody>
                {jobBreakdown.map((item) => (
                  <tr key={item.key}>
                    <td><strong>{item.label}</strong></td>
                    <td>{formatNumber(item.total)}</td>
                    <td>{formatNumber(item.active)}</td>
                    <td>{formatNumber(item.failed)}</td>
                    <td>{formatNumber(item.long_running)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && jobBreakdown.length === 0 && <p className="overview-empty">Chưa có tác vụ vận hành.</p>}
          </div>
        </section>

        <section className="overview-panel overview-panel--wide">
          <div className="overview-panel-heading">
            <div>
              <span>30 ngày gần nhất</span>
              <h2>Sử dụng mô hình, token và chi phí</h2>
            </div>
            <FontAwesomeIcon icon={faServer} />
          </div>
          <div className="model-usage-summary">
            <div>
              <span>Lượt gọi</span>
              <b>{formatNumber(modelUsage.total_requests)}</b>
            </div>
            <div>
              <span>Tokens</span>
              <b>{formatNumber(modelUsage.total_tokens)}</b>
              <small>{formatNumber(modelUsage.prompt_tokens)} in · {formatNumber(modelUsage.completion_tokens)} out</small>
            </div>
            <div>
              <span>Chi phí</span>
              <b>{formatCurrency(modelUsage.cost_usd)}</b>
            </div>
            <div>
              <span>Độ trễ TB</span>
              <b>{formatLatency(modelUsage.avg_latency_ms)}</b>
            </div>
          </div>
          <div className="overview-table-wrap">
            <table className="overview-table overview-table--compact">
              <thead>
                <tr>
                  <th>Mô hình</th>
                  <th>Luồng</th>
                  <th>Tổng</th>
                  <th>Hoàn tất</th>
                  <th>Lỗi</th>
                  <th>Tỉ lệ lỗi</th>
                  <th>Độ trễ TB</th>
                  <th>Token</th>
                  <th>Chi phí</th>
                </tr>
              </thead>
              <tbody>
                {modelPerformance.map((item) => (
                  <tr key={item.key}>
                    <td><strong>{item.model_name || item.model_code}</strong></td>
                    <td>{item.kind_label}</td>
                    <td>{formatNumber(item.total)}</td>
                    <td>{formatNumber(item.completed)}</td>
                    <td>{formatNumber(item.failed)}</td>
                    <td>{formatPercent(item.error_rate)}</td>
                    <td>{formatLatency(item.avg_latency_ms)}</td>
                    <td>
                      {formatNumber(item.total_tokens)}
                      <small>{formatNumber(item.prompt_tokens)} in · {formatNumber(item.completion_tokens)} out</small>
                    </td>
                    <td>{formatCurrency(item.cost_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && modelPerformance.length === 0 && <p className="overview-empty">Chưa có dữ liệu model trong 30 ngày.</p>}
          </div>
        </section>

        <section className="overview-panel overview-panel--wide">
          <div className="overview-panel-heading">
            <div>
              <span>Tác vụ lỗi gần đây</span>
              <h2>Hàng đợi cần chạy lại</h2>
            </div>
            <Link to="/quan-ly-job?status=retryable">Mở hàng đợi</Link>
          </div>
          <div className="overview-table-wrap">
            <table className="overview-table">
              <thead>
                <tr>
                  <th>Loại</th>
                  <th>Đối tượng</th>
                  <th>Cập nhật</th>
                  <th>Lỗi</th>
                </tr>
              </thead>
              <tbody>
                {recentJobs.map((job) => (
                  <tr key={`${job.kind}:${job.id}`}>
                    <td>
                      <strong>{JOB_TYPE_LABEL[job.type] || JOB_TYPE_LABEL[job.kind] || job.type || job.kind}</strong>
                    </td>
                    <td>{job.entity?.label || compactId(job.entity?.id)}</td>
                    <td>{formatDateTime(job.updated_at || job.finished_at || job.started_at || job.queued_at)}</td>
                    <td className="overview-error-cell">{job.error_message || 'Không có'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!loading && recentJobs.length === 0 && <p className="overview-empty">Không có tác vụ cần chạy lại.</p>}
          </div>
        </section>

        <section className="overview-panel overview-panel--wide">
          <div className="overview-panel-heading">
            <div>
              <span>Nhật ký gần đây</span>
              <h2>Thay đổi hệ thống</h2>
            </div>
            <Link to="/nhat-ky-he-thong">Mở nhật ký</Link>
          </div>
          <div className="audit-list-compact">
            {recentAudit.map((item) => (
              <article key={item.id}>
                <strong>{ACTION_LABEL[item.action] || item.action}</strong>
                <span>{ENTITY_TYPE_LABEL[item.entity?.type] || item.entity?.type || 'đối tượng'} · {item.entity?.label || compactId(item.entity?.id)}</span>
                <small>{formatDateTime(item.created_at)}</small>
              </article>
            ))}
            {!loading && recentAudit.length === 0 && <p className="overview-empty">Chưa có nhật ký.</p>}
          </div>
        </section>
      </section>
    </main>
  );
}

export default AdminOverviewPage;
