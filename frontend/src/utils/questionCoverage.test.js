import test from 'node:test';
import assert from 'node:assert/strict';
import { buildQuestionCoverage } from './questionCoverage.js';

const bloomLevels = [
  { level: 1, label: 'Remember' },
  { level: 2, label: 'Understand' },
  { level: 3, label: 'Apply' },
];

test('question coverage counts bloom levels and approved questions', () => {
  const coverage = buildQuestionCoverage({
    bloomLevels,
    questions: [
      {
        review_status: 'APPROVED',
        classification: { bloom: { level: 1 }, chapter: { id: 'ch1' } },
        clos: [{ id: 'clo1' }],
      },
      {
        review_status: 'PENDING',
        classification: { bloom: { level: 1 }, chapter: { id: 'ch2' } },
        clos: [{ id: 'clo2' }],
      },
      {
        review_status: 'APPROVED',
        classification: { bloom: { level: 3 }, chapter: { id: 'ch2' } },
        clos: [{ id: 'clo2' }],
      },
    ],
  });

  assert.equal(coverage.total, 3);
  assert.equal(coverage.approvedTotal, 2);
  assert.equal(coverage.bloom[0].count, 2);
  assert.equal(coverage.bloom[0].approved, 1);
  assert.equal(coverage.bloom[1].status, 'empty');
  assert.equal(coverage.bloom[2].percent, 33);
});

test('question coverage counts question types', () => {
  const coverage = buildQuestionCoverage({
    bloomLevels,
    questionTypes: [
      { backend: 'trac_nghiem', label: 'MCQ' },
      { backend: 'dung_sai', label: 'True/False' },
    ],
    questions: [
      { review_status: 'APPROVED', classification: { assessment_type: 'trac_nghiem', bloom: { level: 1 } }, clos: [] },
      { review_status: 'PENDING', classification: { assessment_type: 'trac_nghiem', bloom: { level: 1 } }, clos: [] },
      { review_status: 'APPROVED', classification: { assessment_type: 'dung_sai', bloom: { level: 2 } }, clos: [] },
    ],
  });

  assert.equal(coverage.types[0].count, 2);
  assert.equal(coverage.types[0].approved, 1);
  assert.equal(coverage.types[1].count, 1);
  assert.equal(coverage.gaps.types, 0);
});

test('question coverage keeps empty catalog chapters and CLO targets visible', () => {
  const coverage = buildQuestionCoverage({
    bloomLevels,
    subject: {
      chapters: [
        { id: 'ch1', chapter_code: 'C1', chapter_name: 'Intro' },
        { id: 'ch2', chapter_code: 'C2', chapter_name: 'Trees' },
      ],
      learning_outcomes: [
        { id: 'clo1', clo_code: 'CLO1', target_weight: 3 },
        { id: 'clo2', clo_code: 'CLO2', target_weight: 1 },
      ],
    },
    questions: [
      {
        review_status: 'APPROVED',
        classification: { bloom: { level: 1 }, chapter: { id: 'ch1' } },
        clos: [{ id: 'clo2' }],
      },
      {
        review_status: 'APPROVED',
        classification: { bloom: { level: 2 }, chapter: { id: 'ch1' } },
        clos: [{ id: 'clo2' }],
      },
    ],
  });

  assert.equal(coverage.chapters.length, 2);
  assert.equal(coverage.chapters[1].count, 0);
  assert.equal(coverage.gaps.chapters, 1);
  assert.equal(coverage.clos[0].target_percent, 75);
  assert.equal(coverage.clos[0].status, 'empty');
  assert.equal(coverage.clos[1].count, 2);
});

test('question coverage derives chapter and CLO rows without a selected subject', () => {
  const coverage = buildQuestionCoverage({
    bloomLevels,
    questions: [
      {
        review_status: 'APPROVED',
        classification: { bloom: { level: 1 }, chapter: { id: 'ch1' } },
        clos: [{ id: 'clo1' }],
      },
    ],
  });

  assert.deepEqual(coverage.chapters.map((item) => item.id), ['ch1']);
  assert.deepEqual(coverage.clos.map((item) => item.id), ['clo1']);
});
