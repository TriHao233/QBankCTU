/** @deprecated Prefer `services/apiClient`. Kept as a thin compatibility shim. */
export {
  API_BASE_URL,
  ApiError,
  apiRequest as apiFetch,
  getAuthHeaders as authHeaders,
} from '../services/apiClient';

export async function parseError(response) {
  let detail = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    detail = body.detail || body.message || detail;
  } catch {
    /* ignore */
  }
  throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
}
