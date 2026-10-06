import test from 'node:test';
import assert from 'node:assert/strict';
import { submissionCheckFlags, submissionCheckWarnings, unacknowledgedFlags } from './submissionChecks.js';

test('flags must each be acknowledged before approving', () => {
  const question = { review_submission: { checks: { flags: ['near_duplicate', 'custom', 'near_duplicate'] } } };
  assert.deepEqual(submissionCheckFlags(question), [
    { flag: 'near_duplicate', label: 'Gần trùng với câu hỏi khác của tài liệu' },
    { flag: 'custom', label: 'custom' },
  ]);
  assert.deepEqual(unacknowledgedFlags(question, ['custom']).map((item) => item.flag), ['near_duplicate']);
  assert.deepEqual(unacknowledgedFlags(question, ['custom', 'near_duplicate']), []);
  assert.deepEqual(unacknowledgedFlags({}, []), []);
});

test('submissionCheckWarnings labels flags and keeps extra warnings without duplicates', () => {
  const question = {
    review_submission: {
      checks: {
        flags: ['answer_not_in_source', 'weak_grounding', 'unknown_flag'],
        warnings: [
          'Đáp án đúng không tìm thấy căn cứ trong nguồn trích dẫn',
          '1 nguồn trích dẫn không còn hợp lệ và không được tính',
        ],
      },
    },
  };
  assert.deepEqual(submissionCheckWarnings(question), [
    'Đáp án đúng không tìm thấy căn cứ trong nguồn trích dẫn',
    'Đoạn minh chứng không khớp nguồn trích dẫn',
    'unknown_flag',
    '1 nguồn trích dẫn không còn hợp lệ và không được tính',
  ]);
});

test('submissionCheckWarnings is empty without checks', () => {
  assert.deepEqual(submissionCheckWarnings({}), []);
  assert.deepEqual(submissionCheckWarnings(null), []);
});
