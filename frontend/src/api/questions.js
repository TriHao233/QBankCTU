import { auth } from '../firebase';
import { apiRequest, ApiError } from '../services/apiClient';

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || '/api/v1'
).replace(/\/$/, '');

function localDateBoundary(value, endOfDay = false) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return value;
  const time = endOfDay ? '23:59:59.999' : '00:00:00.000';
  const date = new Date(`${value}T${time}`);
  return Number.isNaN(date.getTime()) ? value : date.toISOString();
}

export function listQuestions({
  page = 1,
  pageSize = 20,
  reviewStatus,
  search,
  questionType,
  bloomLevel,
  documentId,
  subjectId,
  chapterId,
  cloId,
  difficulty,
  qualityColor,
  minScore,
  publicationStatus,
  evaluationStatus,
  assignmentStatus,
  assignedTo,
  creatorUserId,
  waitingHoursMin,
  overdueOnly = false,
  createdFrom,
  createdTo,
  submittedFrom,
  submittedTo,
  includeStatusCounts = false,
  sortBy,
  sourcePresence,
  secondaryStatus,
} = {}) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', pageSize);
  if (reviewStatus) params.set('review_status', reviewStatus);
  if (search) params.set('search', search);
  if (questionType) params.set('question_type', questionType);
  if (bloomLevel) params.set('bloom_level', bloomLevel);
  if (documentId) params.set('document_id', documentId);
  if (subjectId) params.set('subject_id', subjectId);
  if (chapterId) params.set('chapter_id', chapterId);
  if (cloId) params.set('clo_id', cloId);
  if (difficulty) params.set('difficulty', difficulty);
  if (qualityColor) params.set('quality_color', qualityColor);
  if (minScore !== undefined && minScore !== null && minScore !== '') params.set('min_score', minScore);
  if (publicationStatus) params.set('publication_status', publicationStatus);
  if (evaluationStatus) params.set('evaluation_status', evaluationStatus);
  if (assignmentStatus) params.set('assignment_status', assignmentStatus);
  if (assignedTo) params.set('assigned_to', assignedTo);
  if (creatorUserId) params.set('creator_user_id', creatorUserId);
  if (waitingHoursMin) params.set('waiting_hours_min', waitingHoursMin);
  if (overdueOnly) params.set('overdue_only', 'true');
  if (createdFrom) params.set('created_from', localDateBoundary(createdFrom));
  if (createdTo) params.set('created_to', localDateBoundary(createdTo, true));
  if (submittedFrom) params.set('submitted_from', localDateBoundary(submittedFrom));
  if (submittedTo) params.set('submitted_to', localDateBoundary(submittedTo, true));
  if (includeStatusCounts) params.set('include_status_counts', 'true');
  if (sortBy) params.set('sort_by', sortBy);
  if (sourcePresence) params.set('source_presence', sourcePresence);
  if (secondaryStatus) params.set('secondary_status', secondaryStatus);
  return apiRequest(`/questions?${params.toString()}`);
}

export function getQuestion(id) {
  return apiRequest(`/questions/${id}`);
}

export function getQuestionSources(id) {
  return apiRequest(`/questions/${id}/sources`);
}

function filenameFromDisposition(disposition) {
  const match = disposition?.match(/filename="?([^";]+)"?/i);
  return match?.[1] || 'source.pdf';
}

export async function fetchQuestionSourcePdf(id) {
  if (!auth) {
    throw new ApiError('Firebase web app chưa được cấu hình', 503, null);
  }
  await auth.authStateReady();
  const firebaseUser = auth.currentUser;
  if (!firebaseUser) {
    throw new ApiError('Bạn chưa đăng nhập', 401, null);
  }
  const response = await fetch(`${API_BASE_URL}/questions/${id}/source-pdf`, {
    headers: {
      Authorization: `Bearer ${await firebaseUser.getIdToken()}`,
    },
  });
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      // Keep the fallback message below if the PDF endpoint returns plain text.
    }
    throw new ApiError(payload?.detail || 'Không mở được PDF nguồn', response.status, payload);
  }
  const blob = await response.blob();
  return {
    url: window.URL.createObjectURL(blob),
    filename: filenameFromDisposition(response.headers.get('content-disposition')),
  };
}

export function createQuestion(payload) {
  return apiRequest('/questions', { method: 'POST', body: payload });
}

export function duplicateQuestion(id) {
  return apiRequest(`/questions/${id}/duplicate`, { method: 'POST' });
}

export function updateQuestion(id, payload) {
  return apiRequest(`/questions/${id}`, { method: 'PATCH', body: payload });
}

export function updateQuestionSharing(id, payload) {
  return apiRequest(`/questions/${id}/sharing`, { method: 'PATCH', body: payload });
}

export function transferQuestionOwner(id, newOwnerUserId, reason) {
  return apiRequest(`/questions/${id}/transfer-owner`, {
    method: 'POST',
    body: { new_owner_user_id: newOwnerUserId, reason },
  });
}

export function listQuestionVersions(id) {
  return apiRequest(`/questions/${id}/versions`);
}

export function submitQuestionForReview(id, responseNote = '') {
  return apiRequest(`/questions/${id}/submit-review`, {
    method: 'POST',
    ...(responseNote ? { body: { response_note: responseNote } } : {}),
  });
}

export function deleteQuestion(id, reason = '') {
  const query = reason ? `?reason=${encodeURIComponent(reason)}` : '';
  return apiRequest(`/questions/${id}${query}`, { method: 'DELETE' });
}

export function listArchivedQuestions({ page = 1, pageSize = 20 } = {}) {
  return apiRequest(`/questions/archived?page=${page}&page_size=${pageSize}`);
}

export function restoreQuestion(id) {
  return apiRequest(`/questions/${id}/restore`, { method: 'POST' });
}

export function withdrawQuestionSubmission(id, expectedVersion) {
  const query = expectedVersion ? `?expected_version=${expectedVersion}` : '';
  return apiRequest(`/questions/${id}/withdraw${query}`, { method: 'POST' });
}

export function autoEvaluateQuestion(id, payload) {
  return apiRequest(`/questions/${id}/evaluations/auto`, { method: 'POST', body: payload });
}

export function listQuestionEvaluations(id) {
  return apiRequest(`/questions/${id}/evaluations`);
}

export function listReviewReasons() {
  return apiRequest('/questions/review-reasons');
}

export function reviewQuestion(id, payload) {
  return apiRequest(`/questions/${id}/reviews`, { method: 'POST', body: payload });
}

export function getQuestionReviewDraft(id) {
  return apiRequest(`/questions/${id}/review-draft`);
}

export function saveQuestionReviewDraft(id, payload) {
  return apiRequest(`/questions/${id}/review-draft`, { method: 'PUT', body: payload });
}

export function deleteQuestionReviewDraft(id) {
  return apiRequest(`/questions/${id}/review-draft`, { method: 'DELETE' });
}

export function claimQuestionReview(id, reason = '') {
  return apiRequest(`/questions/${id}/review-assignment/claim`, {
    method: 'POST',
    ...(reason ? { body: { reason } } : {}),
  });
}

export function releaseQuestionReview(id) {
  return apiRequest(`/questions/${id}/review-assignment/release`, { method: 'POST' });
}

export function assignQuestionReview(id, payload) {
  return apiRequest(`/questions/${id}/review-assignment`, { method: 'POST', body: payload });
}

export function listQuestionReviews(id) {
  return apiRequest(`/questions/${id}/reviews`);
}

export function listQuestionComments(id) {
  return apiRequest(`/questions/${id}/comments`);
}

export function addQuestionComment(id, payload) {
  return apiRequest(`/questions/${id}/comments`, { method: 'POST', body: payload });
}

export function updateQuestionComment(id, commentId, payload) {
  return apiRequest(`/questions/${id}/comments/${commentId}`, { method: 'PATCH', body: payload });
}

export function deleteQuestionComment(id, commentId) {
  return apiRequest(`/questions/${id}/comments/${commentId}`, { method: 'DELETE' });
}

export function setQuestionSecondaryReview(id, payload) {
  return apiRequest(`/questions/${id}/secondary-review`, { method: 'POST', body: payload });
}

export function getReviewDashboard() {
  return apiRequest('/questions/review-dashboard');
}

export function publishQuestionToMoodle(id, payload) {
  return apiRequest(`/questions/${id}/moodle-publications`, { method: 'POST', body: payload });
}

export function listQuestionMoodlePublications(id) {
  return apiRequest(`/questions/${id}/moodle-publications`);
}

export function exportQuestionMoodle(id, format = 'gift') {
  const params = new URLSearchParams({ format });
  return apiRequest(`/questions/${id}/moodle-export?${params.toString()}`);
}

export function renewQuestionReview(id) { return apiRequest(`/questions/${id}/review-assignment/renew`, { method: 'POST' }); }
export function getReviewPolicy() { return apiRequest('/questions/review-policy'); }
export function updateReviewPolicy(payload) { return apiRequest('/questions/review-policy', { method: 'PUT', body: payload }); }
export function autoAssignReviews(payload) { return apiRequest('/questions/review-assignments/auto', { method: 'POST', body: payload }); }
export function suggestReviewSubjects(reviewerUserId) { return apiRequest(`/questions/review-subject-suggestions?reviewer_user_id=${encodeURIComponent(reviewerUserId)}`); }
