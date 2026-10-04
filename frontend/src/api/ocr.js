import { apiRequest } from '../services/apiClient';

export function uploadOcrPdf(file, { subjectId, chapterId } = {}) {
  const form = new FormData();
  form.append('file', file);
  if (subjectId) form.append('subject_id', subjectId);
  if (chapterId) form.append('chapter_id', chapterId);
  return apiRequest('/documents/upload', { method: 'POST', body: form });
}

export const uploadSourceDocument = uploadOcrPdf;

export function getOcrStatus(jobId, options = {}) {
  return apiRequest(`/ocr/status/${jobId}`, { signal: options.signal });
}
