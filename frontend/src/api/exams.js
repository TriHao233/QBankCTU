import { auth } from '../firebase';
import { apiRequest, ApiError } from '../services/apiClient';

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || '/api/v1'
).replace(/\/$/, '');

export function listExams({ page = 1, pageSize = 20 } = {}) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', pageSize);
  return apiRequest(`/exams?${params.toString()}`);
}

export function createExam(payload) {
  return apiRequest('/exams', { method: 'POST', body: payload });
}

export function duplicateExam(examId) {
  return apiRequest(`/exams/${examId}/duplicate`, { method: 'POST' });
}

export function getExam(examId) {
  return apiRequest(`/exams/${examId}`);
}

export function updateExam(examId, payload) {
  return apiRequest(`/exams/${examId}`, { method: 'PATCH', body: payload });
}

export function updateExamStatus(examId, status) {
  return apiRequest(`/exams/${examId}/status`, { method: 'POST', body: { status } });
}

export function reopenExam(examId, reason) {
  return apiRequest(`/exams/${examId}/reopen`, { method: 'POST', body: { reason } });
}

export function updateExamSharing(examId, sharedWithUserIds) {
  return apiRequest(`/exams/${examId}/sharing`, {
    method: 'PATCH',
    body: { shared_with_user_ids: sharedWithUserIds },
  });
}

export function transferExamOwner(examId, newOwnerUserId, reason) {
  return apiRequest(`/exams/${examId}/transfer-owner`, {
    method: 'POST',
    body: { new_owner_user_id: newOwnerUserId, reason },
  });
}

export function deleteExam(examId) {
  return apiRequest(`/exams/${examId}`, { method: 'DELETE' });
}

export function saveMatrix(examId, cells) {
  return apiRequest(`/exams/${examId}/matrix`, { method: 'PUT', body: { cells } });
}

export function getMatrixAvailability(examId) {
  return apiRequest(`/exams/${examId}/matrix/availability`);
}

export function autoGenerateQuestions(examId, seed) {
  const query = Number.isInteger(seed) ? `?seed=${seed}` : '';
  return apiRequest(`/exams/${examId}/questions/auto-generate${query}`, { method: 'POST' });
}

export function listExamQuestionPool(examId, {
  page = 1,
  pageSize = 20,
  search,
  questionType,
  bloomLevel,
  chapterId,
  difficulty,
} = {}) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', pageSize);
  if (search) params.set('search', search);
  if (questionType) params.set('question_type', questionType);
  if (bloomLevel) params.set('bloom_level', bloomLevel);
  if (chapterId) params.set('chapter_id', chapterId);
  if (difficulty) params.set('difficulty', difficulty);
  return apiRequest(`/exams/${examId}/question-pool?${params.toString()}`);
}

export function addQuestionsManual(examId, questionIds) {
  return apiRequest(`/exams/${examId}/questions`, {
    method: 'POST',
    body: { question_ids: questionIds },
  });
}

export function removeQuestion(examId, questionId) {
  return apiRequest(`/exams/${examId}/questions/${questionId}`, { method: 'DELETE' });
}

export function listVariants(examId) {
  return apiRequest(`/exams/${examId}/variants`);
}

export function createVariant(examId, examCode, shuffle = true) {
  return apiRequest(`/exams/${examId}/variants`, {
    method: 'POST',
    body: { exam_code: examCode, shuffle },
  });
}

export function deleteVariant(examId, variantId) {
  return apiRequest(`/exams/${examId}/variants/${variantId}`, { method: 'DELETE' });
}

export function getVariantPreview(examId, variantId) {
  return apiRequest(`/exams/${examId}/variants/${variantId}/preview`);
}

async function downloadVariantExport(examId, variantId, format, type, fallbackMessage) {
  if (!auth) {
    throw new ApiError('Firebase web app chưa được cấu hình', 503, null);
  }
  await auth.authStateReady();
  const firebaseUser = auth.currentUser;
  if (!firebaseUser) {
    throw new ApiError('Bạn chưa đăng nhập', 401, null);
  }
  const token = await firebaseUser.getIdToken();
  const params = new URLSearchParams({ type });
  const response = await fetch(
    `${API_BASE_URL}/exams/${examId}/variants/${variantId}/export/${format}?${params.toString()}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  if (!response.ok) {
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      // ignore parse errors, fall back to generic message below
    }
    throw new ApiError(payload?.detail || fallbackMessage, response.status, payload);
  }
  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${type}.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.URL.revokeObjectURL(url);
}

export async function downloadVariantPdf(examId, variantId, type = 'de') {
  return downloadVariantExport(examId, variantId, 'pdf', type, 'Xuất PDF thất bại');
}

export async function downloadVariantDocx(examId, variantId, type = 'de') {
  return downloadVariantExport(examId, variantId, 'docx', type, 'Xuất DOCX thất bại');
}
