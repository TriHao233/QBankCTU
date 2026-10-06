import assert from 'node:assert/strict';
import test from 'node:test';

import { buildGenerationRequest } from './generationRequest.js';

test('buildGenerationRequest uses backend defaults and keeps instruction separate from heading', () => {
  const questionPlan = [{
    bloom_level: '2_hieu',
    difficulty: 'kho',
    question_type: 'dung_sai',
    num_questions: 3,
  }];
  const payload = buildGenerationRequest({
    documentId: 'document-1',
    questionPlan,
    teacherInstruction: '  Tập trung vào định nghĩa  ',
    targetHeading: '  Chương 3 - Hàng đợi  ',
    sourceMode: 'existing',
    modelProvider: 'qwen-fast',
    timings: { documentMs: 'reused', uploadMs: 10, ocrMs: 20, chunkMs: 30 },
    pipelineStartedAt: 100,
    now: () => 225.4,
  });

  assert.equal(payload.instruction, 'Tập trung vào định nghĩa');
  assert.equal(payload.model_provider, 'qwen-fast');
  // Code questions use the backend's separate code model.
  assert.equal(payload.code_model_provider, undefined);
  assert.equal(payload.difficulty, 'kho');
  assert.equal(payload.collection_name, undefined);
  assert.equal(payload.target_heading, 'Chương 3 - Hàng đợi');
  assert.equal(payload.client_telemetry.document_reused, true);
  assert.equal(payload.client_telemetry.elapsed_before_generate_ms, 125);
  assert.deepEqual(payload.question_plan, questionPlan);
});

test('buildGenerationRequest sends extra documents and target CLOs only when chosen', () => {
  const base = {
    documentId: 'doc-1',
    questionPlan: [{ bloom_level: '1_nho', difficulty: 'de', question_type: 'trac_nghiem', num_questions: 1 }],
    teacherInstruction: '',
    sourceMode: 'existing',
    timings: {},
    pipelineStartedAt: 0,
    now: () => 0,
  };
  const plain = buildGenerationRequest(base);
  assert.equal(plain.document_ids, undefined);
  assert.equal(plain.target_clo_ids, undefined);

  const scoped = buildGenerationRequest({ ...base, extraDocumentIds: ['doc-1', 'doc-2'], targetCloIds: ['clo-1'] });
  assert.deepEqual(scoped.document_ids, ['doc-2']);
  assert.deepEqual(scoped.target_clo_ids, ['clo-1']);
});
