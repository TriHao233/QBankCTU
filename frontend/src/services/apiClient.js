import { auth } from "../firebase";
import { demoAuthHeaders, handleDemoSessionResponse, SESSION_EXPIRED_EVENT } from "../auth/demoSession";

export const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL || "/api/v1"
).replace(/\/$/, "");

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

async function resolveAuthHeaders({ forceRefresh = false } = {}) {
  const demoHeaders = demoAuthHeaders();
  if (demoHeaders) {
    return { headers: demoHeaders, demoToken: demoHeaders.Authorization, firebaseUser: null };
  }

  if (!auth) {
    throw new ApiError("Firebase web app chưa được cấu hình", 503, null);
  }
  await auth.authStateReady();
  const firebaseUser = auth.currentUser;
  if (!firebaseUser) {
    throw new ApiError("Bạn chưa đăng nhập", 401, null);
  }
  return {
    headers: { Authorization: `Bearer ${await firebaseUser.getIdToken(forceRefresh)}` },
    demoToken: null,
    firebaseUser,
  };
}

export async function getAuthHeaders({ forceRefresh = false } = {}) {
  const { headers } = await resolveAuthHeaders({ forceRefresh });
  return headers;
}

function buildApiError(payload, status, fallbackMessage) {
  const message = payload?.detail || payload?.message || fallbackMessage;
  return new ApiError(
    typeof message === "string" ? message : JSON.stringify(message),
    status,
    payload,
  );
}

async function parseErrorPayload(response) {
  const isJson = response.headers.get("content-type")?.includes("application/json");
  const rawText = response.status === 204 ? "" : await response.text();
  if (!isJson) return rawText || null;
  if (!rawText) return null;
  try {
    return JSON.parse(rawText);
  } catch {
    return rawText;
  }
}

export async function apiRequest(
  path,
  { method = "GET", body, headers = {}, authRequired = true, signal } = {},
) {
  const requestHeaders = { Accept: "application/json", ...headers };
  let demoToken = null;
  let firebaseUser = null;
  if (body !== undefined && !(body instanceof FormData)) {
    requestHeaders["Content-Type"] = "application/json";
  }

  if (authRequired) {
    const authState = await resolveAuthHeaders();
    Object.assign(requestHeaders, authState.headers);
    demoToken = authState.demoToken;
    firebaseUser = authState.firebaseUser;
  }

  const requestOptions = {
    method,
    headers: requestHeaders,
    body:
      body === undefined || body instanceof FormData
        ? body
        : JSON.stringify(body),
    signal,
  };
  let response = await fetch(`${API_BASE_URL}${path}`, requestOptions);
  if (response.status === 401 && firebaseUser && auth.currentUser?.uid === firebaseUser.uid) {
    try {
      requestHeaders.Authorization = `Bearer ${await firebaseUser.getIdToken(true)}`;
    } catch (error) {
      if (auth.currentUser?.uid === firebaseUser.uid) {
        globalThis.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
      }
      throw error;
    }
    response = await fetch(`${API_BASE_URL}${path}`, requestOptions);
  }
  if (response.status === 401 && demoToken) {
    handleDemoSessionResponse(response.status, demoToken);
  } else if (response.status === 401 && firebaseUser && auth.currentUser?.uid === firebaseUser.uid) {
    globalThis.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }

  const payload = await parseErrorPayload(response);
  if (!response.ok) {
    const fallbackMessage = [502, 503, 504].includes(response.status)
      ? "Không thể kết nối máy chủ. Vui lòng thử lại."
      : "Yêu cầu API thất bại";
    throw buildApiError(payload, response.status, fallbackMessage);
  }
  return payload;
}

/** Authenticated fetch for binary/SSE responses. Returns the raw Response when ok. */
export async function apiRawRequest(
  path,
  { method = "GET", body, headers = {}, signal, accept } = {},
) {
  const authState = await resolveAuthHeaders();
  const requestHeaders = {
    ...(accept ? { Accept: accept } : {}),
    ...headers,
    ...authState.headers,
  };
  const requestOptions = { method, headers: requestHeaders, body, signal };
  let response = await fetch(`${API_BASE_URL}${path}`, requestOptions);

  if (
    response.status === 401
    && authState.firebaseUser
    && auth.currentUser?.uid === authState.firebaseUser.uid
  ) {
    try {
      requestHeaders.Authorization = `Bearer ${await authState.firebaseUser.getIdToken(true)}`;
      response = await fetch(`${API_BASE_URL}${path}`, requestOptions);
    } catch (error) {
      if (auth.currentUser?.uid === authState.firebaseUser.uid) {
        globalThis.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
      }
      throw error;
    }
  }

  if (response.status === 401 && authState.demoToken) {
    handleDemoSessionResponse(response.status, authState.demoToken);
  } else if (
    response.status === 401
    && authState.firebaseUser
    && auth.currentUser?.uid === authState.firebaseUser.uid
  ) {
    globalThis.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }

  if (!response.ok) {
    const payload = await parseErrorPayload(response);
    throw buildApiError(payload, response.status, "Yêu cầu API thất bại");
  }
  return response;
}
