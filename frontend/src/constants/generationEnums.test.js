import assert from 'node:assert/strict';
import test from 'node:test';

import {
  allowedBloomLevels,
  isBloomAllowedForQuestionType,
  normalizeBloomForQuestionType,
} from './generationEnums.js';

test('Bloom matrix matches every question type used by the project', () => {
  assert.deepEqual(
    allowedBloomLevels('mcq').map((item) => item.id),
    ['remember', 'understand', 'apply', 'analyze', 'evaluate'],
  );
  assert.deepEqual(
    allowedBloomLevels('multi').map((item) => item.id),
    ['remember', 'understand', 'apply', 'analyze', 'evaluate'],
  );
  assert.deepEqual(
    allowedBloomLevels('tf').map((item) => item.id),
    ['remember', 'understand', 'apply'],
  );
  assert.deepEqual(
    allowedBloomLevels('fill').map((item) => item.id),
    ['remember', 'understand', 'apply'],
  );
  assert.deepEqual(
    allowedBloomLevels('match').map((item) => item.id),
    ['remember', 'understand'],
  );
  assert.deepEqual(
    allowedBloomLevels('order').map((item) => item.id),
    ['remember', 'understand', 'apply'],
  );
  assert.deepEqual(
    allowedBloomLevels('scenario').map((item) => item.id),
    ['apply', 'analyze', 'evaluate', 'create'],
  );
});

test('locked Bloom values are rejected and normalized after changing question type', () => {
  assert.equal(isBloomAllowedForQuestionType('mcq', 'create'), false);
  assert.equal(isBloomAllowedForQuestionType('mcq', 'evaluate'), true);
  assert.equal(isBloomAllowedForQuestionType('tf', 'analyze'), false);
  assert.equal(isBloomAllowedForQuestionType('scenario', 'create'), true);
  assert.equal(normalizeBloomForQuestionType('scenario', 'remember'), 'apply');
  assert.equal(normalizeBloomForQuestionType('mcq', 'analyze'), 'analyze');
  assert.equal(normalizeBloomForQuestionType('match', 'apply'), 'remember');
});
