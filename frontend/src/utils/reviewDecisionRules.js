export const AI_BUSY_STATUSES = Object.freeze(['QUEUED', 'PROCESSING', 'RUNNING']);
export const BREAK_GLASS_REASON_MIN = 10;
export function overrideRequired(question, decision) {
  return decision === 'APPROVED' && question?.evaluation_status === 'FAILED';
}
export function isAiRunning(question) {
  return AI_BUSY_STATUSES.includes(question?.evaluation_status);
}
// Backend only accepts an Admin's self-review as break-glass (feature flag on, no other eligible reviewer).
export function breakGlassReasonRequired(question, user) {
  return user?.role === 'Admin' && Boolean(user.id)
    && (question?.author_user_ids || []).map(String).includes(String(user.id));
}
