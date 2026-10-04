/** Shared question/document/job lifecycle labels — keep in sync with backend enums. */

export const REVIEW_STATUS_LABEL = Object.freeze({
  DRAFT: 'Nháp',
  PENDING: 'Chờ duyệt',
  APPROVED: 'Đã duyệt',
  NEEDS_REVISION: 'Cần sửa',
  REJECTED: 'Từ chối',
});

export const REVIEW_FILTER_LABEL = Object.freeze({
  all: 'Tất cả',
  ...REVIEW_STATUS_LABEL,
  PROCESSED: 'Đã xử lý',
});

export const REVIEW_STATUS_CLASS = Object.freeze({
  DRAFT: 'status--draft',
  PENDING: 'status--pending',
  APPROVED: 'status--approved',
  NEEDS_REVISION: 'status--revise',
  REJECTED: 'status--revise',
});

export const EVALUATION_STATUS_LABEL = Object.freeze({
  NOT_STARTED: 'Chưa đánh giá',
  QUEUED: 'Chờ AI đánh giá',
  PROCESSING: 'Đang đánh giá',
  RUNNING: 'Đang đánh giá',
  PASSED: 'AI đề xuất đạt',
  FAILED: 'AI đề xuất xem lại',
  ERROR: 'AI lỗi',
  STALE: 'Cần đánh giá lại',
  CANCELLED: 'Đã hủy đánh giá',
  BLOCKED: 'Thiếu bằng chứng',
  INSUFFICIENT_EVIDENCE: 'Không đủ bằng chứng',
  EVIDENCE_VALIDATION_FAILED: 'Minh chứng AI không hợp lệ',
});

export const PUBLICATION_STATUS_LABEL = Object.freeze({
  NOT_PUBLISHED: 'Chưa đưa lên Moodle',
  PENDING: 'Đang đưa lên Moodle',
  PUBLISHED: 'Đã đưa lên Moodle',
  STALE: 'Cần đưa lên Moodle lại',
  FAILED: 'Đưa lên Moodle lỗi',
});

export const DOC_STATUS_LABEL = Object.freeze({
  UPLOADED: 'Đã tải lên',
  PROCESSING: 'Đang xử lý',
  READY: 'Đã xử lý',
  FAILED: 'Thất bại',
  ARCHIVED: 'Đã lưu trữ',
});

/** Document/evaluation jobs use UPPERCASE; generation jobs use lowercase. */
export const JOB_STATUS_LABEL = Object.freeze({
  NOT_STARTED: 'Chưa chạy',
  QUEUED: 'Chờ chạy',
  PROCESSING: 'Đang chạy',
  COMPLETED: 'Hoàn tất',
  FAILED: 'Lỗi',
  CANCELLED: 'Đã hủy',
  ERROR: 'Lỗi',
  STALE: 'Cần chạy lại',
  BLOCKED: 'Bị chặn',
  queued: 'Chờ chạy',
  processing: 'Đang chạy',
  completed: 'Hoàn tất',
  failed: 'Lỗi',
  cancelled: 'Đã hủy',
});

export const PIPELINE_STEP_LABEL = Object.freeze({
  ocr_status: 'OCR',
  chunk_status: 'Chunk',
  index_status: 'Index',
});

export const ASSIGNMENT_STATUS_LABEL = Object.freeze({
  UNASSIGNED: 'Chưa nhận',
  ASSIGNED: 'Đã gán',
  IN_REVIEW: 'Đang xử lý',
});

export const ASSIGNMENT_FILTER_LABEL = Object.freeze({
  all: 'Mọi phân công',
  mine: 'Của tôi',
  ...ASSIGNMENT_STATUS_LABEL,
});

export const SECONDARY_REVIEW_STATUS_LABEL = Object.freeze({
  NOT_REQUIRED: 'Không yêu cầu',
  AWAITING_SECONDARY: 'Đang chờ duyệt lần hai',
  COMPLETED: 'Đã duyệt lần hai',
});

export const QUALITY_COLOR_LABEL = Object.freeze({
  GREEN: 'Đạt tốt',
  YELLOW: 'Cần xem lại',
  RED: 'Rủi ro cao',
});

export const SUBMITTABLE_REVIEW_STATUSES = Object.freeze(new Set(['DRAFT', 'NEEDS_REVISION']));
export const BUSY_EVALUATION_STATUSES = Object.freeze(new Set(['QUEUED', 'PROCESSING', 'RUNNING']));
export const ACTIVE_DOCUMENT_JOB_STATUSES = Object.freeze(new Set(['QUEUED', 'PROCESSING']));
export const RETRYABLE_DOCUMENT_JOB_STATUSES = Object.freeze(new Set(['FAILED', 'ERROR', 'STALE']));
export const TERMINAL_GENERATION_JOB_STATUSES = Object.freeze(['completed', 'failed', 'cancelled']);

export function normalizeJobStatus(status) {
  return String(status || '').trim().toLowerCase();
}

export function isBusyEvaluationStatus(status) {
  return BUSY_EVALUATION_STATUSES.has(String(status || '').toUpperCase());
}

export function isDocumentPipelineReady(document) {
  return String(document?.status || '').toUpperCase() === 'READY';
}

export function isDocumentPipelineFailed(document) {
  const status = String(document?.status || '').toUpperCase();
  if (status === 'FAILED') return true;
  const summary = document?.pipeline_summary || {};
  return ['chunk_status', 'index_status', 'ocr_status'].some((key) => (
    ['FAILED', 'ERROR', 'CANCELLED'].includes(String(summary[key] || '').toUpperCase())
  ));
}
