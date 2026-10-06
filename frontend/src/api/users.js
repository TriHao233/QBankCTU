import { apiRequest } from '../services/apiClient';

export function getMe() {
  return apiRequest('/users/me');
}

export function updateMe(payload) {
  return apiRequest('/users/me', { method: 'PATCH', body: payload });
}

export function uploadMyAvatar(file) {
  const body = new FormData();
  body.append('file', file);
  return apiRequest('/users/me/avatar', { method: 'POST', body });
}

export function listUsers({ page = 1, pageSize = 20, role, search, approvalStatus } = {}) {
  const params = new URLSearchParams();
  params.set('page', page);
  params.set('page_size', pageSize);
  if (role) params.set('role', role);
  if (search) params.set('search', search);
  if (approvalStatus) params.set('approval_status', approvalStatus);
  return apiRequest(`/users?${params.toString()}`);
}

export function listTeacherOptions({ search } = {}) {
  const params = new URLSearchParams();
  if (search) params.set('search', search);
  const query = params.toString();
  return apiRequest(`/users/teachers${query ? `?${query}` : ''}`);
}

export function listReviewerOptions({ search } = {}) {
  const params = new URLSearchParams();
  if (search) params.set('search', search);
  const query = params.toString();
  return apiRequest(`/users/reviewers${query ? `?${query}` : ''}`);
}

export function getUser(id) {
  return apiRequest(`/users/${id}`);
}

export function createUser(payload) {
  return apiRequest('/users', { method: 'POST', body: payload });
}

export function inviteUser(payload) {
  return apiRequest('/users/invite', { method: 'POST', body: payload });
}

export function importUsers(payload) {
  return apiRequest('/users/import', { method: 'POST', body: payload });
}

export function updateUser(id, payload) {
  return apiRequest(`/users/${id}`, { method: 'PATCH', body: payload });
}

export function resetUserPassword(id) {
  return apiRequest(`/users/${id}/password-reset`, { method: 'POST' });
}

export function deleteUser(id) {
  return apiRequest(`/users/${id}`, { method: 'DELETE' });
}

export function listGenerationPresets() {
  return apiRequest('/users/me/generation-presets');
}

export function saveGenerationPreset(payload) {
  return apiRequest('/users/me/generation-presets', { method: 'POST', body: payload });
}

export function deleteGenerationPreset(id) {
  return apiRequest(`/users/me/generation-presets/${id}`, { method: 'DELETE' });
}

export function getMyCalendar({ from, to, status = 'all', priority = 'all' } = {}) {
  const params = new URLSearchParams();
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  params.set('status', status);
  params.set('priority', priority);
  return apiRequest(`/users/me/calendar?${params.toString()}`);
}

export function createCalendarTask(payload) {
  return apiRequest('/users/me/calendar', { method: 'POST', body: payload });
}

export function updateCalendarTask(id, payload) {
  return apiRequest(`/users/me/calendar/${id}`, { method: 'PATCH', body: payload });
}

export function deleteCalendarTask(id) {
  return apiRequest(`/users/me/calendar/${id}`, { method: 'DELETE' });
}
