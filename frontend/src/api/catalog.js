import { apiRequest } from '../services/apiClient';

export function getCatalogOverview() {
  return apiRequest('/catalog/overview');
}

export function getRuntimeConfig() {
  return apiRequest('/catalog/runtime-config');
}

export function listSubjects() {
  return apiRequest('/catalog/subjects');
}

export function saveSubject(payload) {
  return apiRequest('/catalog/subjects', { method: 'POST', body: payload });
}

export function updateSubject(subjectId, payload) {
  return apiRequest(`/catalog/subjects/${subjectId}`, { method: 'PATCH', body: payload });
}

export function deactivateSubject(subjectId) {
  return apiRequest(`/catalog/subjects/${subjectId}`, { method: 'DELETE' });
}

export function approveSubject(subjectId) {
  return apiRequest(`/catalog/subjects/${subjectId}/approve`, { method: 'POST' });
}

export function rejectSubject(subjectId, reason) {
  return apiRequest(`/catalog/subjects/${subjectId}/reject`, { method: 'POST', body: { reason } });
}

export function resubmitSubject(subjectId) {
  return apiRequest(`/catalog/subjects/${subjectId}/resubmit`, { method: 'POST' });
}

export function addSubjectChapter(subjectId, payload) {
  return apiRequest(`/catalog/subjects/${subjectId}/chapters`, { method: 'POST', body: payload });
}

export function updateSubjectChapter(subjectId, chapterId, payload) {
  return apiRequest(`/catalog/subjects/${subjectId}/chapters/${chapterId}`, { method: 'PATCH', body: payload });
}

export function addSubjectLearningOutcome(subjectId, payload) {
  return apiRequest(`/catalog/subjects/${subjectId}/learning-outcomes`, { method: 'POST', body: payload });
}

export function updateSubjectLearningOutcome(subjectId, cloId, payload) {
  return apiRequest(`/catalog/subjects/${subjectId}/learning-outcomes/${cloId}`, { method: 'PATCH', body: payload });
}

export function listAiModels() {
  return apiRequest('/catalog/ai-models');
}

export function listAvailableAiModels(capability = 'QUESTION_GENERATION') {
  return apiRequest(`/catalog/ai-models/available?capability=${encodeURIComponent(capability)}`);
}

export function saveAiModel(payload) {
  return apiRequest('/catalog/ai-models', { method: 'POST', body: payload });
}

export function setAiModelActive(payload) {
  return apiRequest('/catalog/ai-models/active', { method: 'POST', body: payload });
}

export function checkAiModelHealth(payload) {
  return apiRequest('/catalog/ai-models/health-check', { method: 'POST', body: payload });
}

export function listPromptTemplates() {
  return apiRequest('/catalog/prompt-templates');
}

export function savePromptTemplate(payload) {
  return apiRequest('/catalog/prompt-templates', { method: 'POST', body: payload });
}

export function activatePromptTemplate(payload) {
  return apiRequest('/catalog/prompt-templates/active', { method: 'POST', body: payload });
}

export function testPromptTemplate(payload) {
  return apiRequest('/catalog/prompt-templates/test-build', { method: 'POST', body: payload });
}

export function listEvaluationPolicies() {
  return apiRequest('/catalog/evaluation-policies');
}

export function saveEvaluationPolicy(payload) {
  return apiRequest('/catalog/evaluation-policies', { method: 'POST', body: payload });
}

export function activateEvaluationPolicy(payload) {
  return apiRequest('/catalog/evaluation-policies/active', { method: 'POST', body: payload });
}
