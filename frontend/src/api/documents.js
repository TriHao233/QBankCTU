import { apiRequest } from '../services/apiClient';

export function listDocuments({ page = 1, pageSize = 20, status, search, subjectId } = {}) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', pageSize);
  if (status) params.set('status', status);
  if (search) params.set('search', search);
  if (subjectId) params.set('subject_id', subjectId);
  return apiRequest(`/documents?${params.toString()}`);
}

export function getDocument(id) {
  return apiRequest(`/documents/${id}`);
}

export function listDocumentJobs(id, { limit = 20 } = {}) {
  const params = new URLSearchParams();
  params.set('limit', limit);
  return apiRequest(`/documents/${id}/jobs?${params.toString()}`);
}

export function listDocumentPages(id, { limit = 100 } = {}) {
  const params = new URLSearchParams();
  params.set('limit', limit);
  return apiRequest(`/documents/${id}/pages?${params.toString()}`);
}

export function updateDocumentPage(documentId, pageId, payload) {
  return apiRequest(`/documents/${documentId}/pages/${pageId}`, { method: 'PATCH', body: payload });
}

export function retryDocumentJob(documentId, jobId) {
  return apiRequest(`/documents/${documentId}/jobs/${jobId}/retry`, { method: 'POST' });
}

export function cancelDocumentJob(documentId, jobId) {
  return apiRequest(`/documents/${documentId}/jobs/${jobId}/cancel`, { method: 'POST' });
}

export function reindexDocument(documentId) {
  return apiRequest(`/documents/${documentId}/reindex`, { method: 'POST' });
}

export function updateDocument(id, payload) {
  return apiRequest(`/documents/${id}`, { method: 'PATCH', body: payload });
}

export function updateDocumentSharing(id, payload) {
  return apiRequest(`/documents/${id}/sharing`, { method: 'PATCH', body: payload });
}

export function deleteDocument(id) {
  return apiRequest(`/documents/${id}`, { method: 'DELETE' });
}

export function restoreDocument(id) {
  return apiRequest(`/documents/${id}/restore`, { method: 'POST' });
}
