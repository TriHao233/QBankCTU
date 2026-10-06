import test from 'node:test';
import assert from 'node:assert/strict';
import { overrideRequired, isAiRunning, breakGlassReasonRequired } from './reviewDecisionRules.js';
for (const status of ['FAILED', 'PASSED', 'ERROR', 'NOT_STARTED', 'STALE', undefined]) {
  test(`override only on FAILED approval: ${status}`, () => {
    assert.equal(overrideRequired({ evaluation_status: status }, 'APPROVED'), status === 'FAILED');
    assert.equal(overrideRequired({ evaluation_status: status }, 'NEEDS_REVISION'), false);
    assert.equal(overrideRequired({ evaluation_status: status }, 'REJECTED'), false);
  });
}
test('running AI and admin self review', () => {
  for (const status of ['QUEUED', 'PROCESSING', 'RUNNING']) assert.equal(isAiRunning({ evaluation_status: status }), true);
  assert.equal(isAiRunning({ evaluation_status: 'FAILED' }), false);
  assert.equal(isAiRunning(null), false);
  const question = { author_user_ids: ['1'] };
  assert.equal(breakGlassReasonRequired(question, { role: 'Admin', id: 1 }), true);
  assert.equal(breakGlassReasonRequired(question, { role: 'Admin', id: '2' }), false);
  assert.equal(breakGlassReasonRequired(question, { role: 'Reviewer', id: '1' }), false);
  assert.equal(breakGlassReasonRequired(null, { role: 'Admin', id: '1' }), false);
});
