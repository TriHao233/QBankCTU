import { apiRawRequest, apiRequest } from '../services/apiClient';
import { TERMINAL_GENERATION_JOB_STATUSES } from '../constants/lifecycleStatus';
import { parseGenerateStatusSseFrame } from '../utils/generateSse';

export function enqueueGenerateQuestions(payload, idempotencyKey) {
  return apiRequest('/generate/questions', {
    method: 'POST',
    headers: idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {},
    body: payload,
  });
}

export function getGenerateStatus(jobId, options = {}) {
  return apiRequest(`/generate/status/${jobId}`, { signal: options.signal });
}

export function cancelGenerateJob(jobId) {
  return apiRequest(`/generate/status/${jobId}/cancel`, { method: 'POST' });
}

export async function streamGenerateStatus(jobId, options = {}) {
  const {
    signal,
    timeoutMs = 20 * 60 * 1000,
    terminal = TERMINAL_GENERATION_JOB_STATUSES,
    onUpdate,
  } = options;
  const controller = new AbortController();
  const abortFromCaller = () => controller.abort(signal?.reason);
  signal?.addEventListener('abort', abortFromCaller, { once: true });
  const timeout = setTimeout(() => controller.abort('timeout'), timeoutMs);

  try {
    const response = await apiRawRequest(`/generate/status/${jobId}/events`, {
      accept: 'text/event-stream',
      signal: controller.signal,
    });
    if (!response.body) throw new Error('Trình duyệt không hỗ trợ đọc luồng tiến độ');

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let latest = null;

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, '\n');
      const frames = buffer.split('\n\n');
      buffer = frames.pop() || '';

      for (const frame of frames) {
        latest = parseGenerateStatusSseFrame(frame);
        if (!latest) continue;
        onUpdate?.(latest);
        if (terminal.includes(latest.status)) return latest;
      }
      if (done) break;
    }
    throw new Error('Luồng tiến độ kết thúc trước khi job hoàn tất');
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Theo dõi job đã bị hủy', 'AbortError');
    if (controller.signal.aborted && controller.signal.reason === 'timeout') {
      const timeoutError = new Error('Hết thời gian chờ xử lý');
      timeoutError.name = 'TimeoutError';
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', abortFromCaller);
  }
}
