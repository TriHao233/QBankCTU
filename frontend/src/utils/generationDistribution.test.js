import assert from 'node:assert/strict';
import test from 'node:test';

import { expandDistribution, largestRemainder } from './generationDistribution.js';

test('largestRemainder keeps the total and favours the largest remainders', () => {
  assert.deepEqual(largestRemainder(10, { a: 0.3, b: 0.5, c: 0.2 }), { a: 3, b: 5, c: 2 });
  assert.deepEqual(largestRemainder(5, { a: 1, b: 1 }), { a: 3, b: 2 });
});

test('expandDistribution splits ratios into plan rows that respect the Bloom matrix', () => {
  const rows = expandDistribution({
    total: 10,
    questionTypeIds: ['mcq', 'tf'],
    bloomRatios: { remember: 0.3, understand: 0.5, analyze: 0.2 },
    difficultyRatios: { de: 1, trung_binh: 1 },
  });
  assert.equal(rows.reduce((sum, row) => sum + row.count, 0), 10);
  const analyze = rows.filter((row) => row.bloomId === 'analyze');
  assert.ok(analyze.every((row) => row.questionTypeId === 'mcq'));
  assert.equal(analyze.reduce((sum, row) => sum + row.count, 0), 2);
});

test('expandDistribution rejects a Bloom level no chosen type supports', () => {
  assert.throws(
    () => expandDistribution({ total: 2, questionTypeIds: ['tf'], bloomRatios: { evaluate: 1 } }),
    /Bloom/,
  );
});
