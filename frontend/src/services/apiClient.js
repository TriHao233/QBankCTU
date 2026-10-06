import { auth } from "../firebase";
import { demoAuthHeaders, handleDemoSessionResponse, SESSION_EXPIRED_EVENT } from "../auth/demoSession";

const API_BASE_URL = (
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
    const demoHeaders = demoAuthHeaders();
    if (demoHeaders) {
      Object.assign(requestHeaders, demoHeaders);
      demoToken = demoHeaders.Authorization;
      authRequired = false;
    }
  }

  if (authRequired) {
    if (!auth) {
      throw new ApiError("Firebase web app chưa được cấu hình", 503, null);
    }
    await auth.authStateReady();
    firebaseUser = auth.currentUser;
    if (!firebaseUser) {
      throw new ApiError("Bạn chưa đăng nhập", 401, null);
    }
    requestHeaders.Authorization = `Bearer ${await firebaseUser.getIdToken()}`;
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
    // Chỉ xoá phiên khi token bị từ chối vẫn là phiên hiện tại (không đụng lần đăng nhập mới hơn).
    handleDemoSessionResponse(response.status, demoToken);
  } else if (response.status === 401 && firebaseUser && auth.currentUser?.uid === firebaseUser.uid) {
    globalThis.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
  }

  const isJson = response.headers
    .get("content-type")
    ?.includes("application/json");
  const rawText = response.status === 204 ? "" : await response.text();
  let payload = rawText;
  if (isJson && rawText) {
    payload = JSON.parse(rawText);
  } else if (isJson) {
    payload = null;
  }
  if (!response.ok) {
    const fallbackMessage = [502, 503, 504].includes(response.status)
      ? "Không thể kết nối máy chủ. Vui lòng thử lại."
      : "Yêu cầu API thất bại";
    const detail = payload?.detail;
    const detailMessage = detail && typeof detail === "object" && !Array.isArray(detail)
      ? detail.message
      : detail;
    throw new ApiError(
      detailMessage || payload?.message || fallbackMessage,
      response.status,
      payload,
    );
  }
  return payload;
}
