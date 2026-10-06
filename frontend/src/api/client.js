import { auth } from '../firebase';
import { demoAuthHeaders, handleDemoSessionResponse } from '../auth/demoSession';

export const API_BASE_URL =
  (import.meta.env.VITE_API_BASE_URL || '/api/v1').replace(/\/$/, '');

export async function parseError(response) {
  let detail = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    detail = body.detail || body.message || detail;
  } catch {
    /* ignore */
  }
  if (typeof detail === 'string') throw new Error(detail);
  const error = new Error(detail?.message || JSON.stringify(detail));
  error.status = response.status;
  error.code = detail?.code;
  error.documentId = detail?.document_id;
  throw error;
}

export async function authHeaders() {
  const demoHeaders = demoAuthHeaders();
  if (demoHeaders) return demoHeaders;

  if (!auth) {
    throw new Error('Firebase web app chưa được cấu hình');
  }
  await auth.authStateReady();
  const firebaseUser = auth.currentUser;
  if (!firebaseUser) {
    throw new Error('Bạn chưa đăng nhập');
  }
  return { Authorization: `Bearer ${await firebaseUser.getIdToken()}` };
}

export async function apiFetch(path, options = {}) {
  const headers = { ...(options.headers || {}), ...(await authHeaders()) };
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  if (!response.ok) {
    handleDemoSessionResponse(response.status, headers.Authorization);
    await parseError(response);
  }
  return response.json();
}
