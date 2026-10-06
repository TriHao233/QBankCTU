export const QUESTION_TYPES = [
  { id: 'mcq', label: 'Trắc nghiệm (MCQ)', backend: 'trac_nghiem' },
  { id: 'multi', label: 'Nhiều lựa chọn', backend: 'nhieu_lua_chon' },
  { id: 'tf', label: 'Đúng / Sai', backend: 'dung_sai' },
  { id: 'fill', label: 'Điền khuyết', backend: 'dien_khuyet' },
  { id: 'match', label: 'Ghép đôi', backend: 'ghep_cot' },
  { id: 'order', label: 'Sắp xếp', backend: 'sap_xep' },
  { id: 'scenario', label: 'Tình huống', backend: 'tinh_huong' },
];

export const BLOOM_LEVELS = [
  { id: 'remember', level: 1, label: '1. Nhớ', caption: 'Thuật ngữ, định nghĩa', backend: '1_nho' },
  { id: 'understand', level: 2, label: '2. Hiểu', caption: 'Diễn giải nguyên lý', backend: '2_hieu' },
  { id: 'apply', level: 3, label: '3. Vận dụng', caption: 'Áp dụng vào bài toán', backend: '3_van_dung' },
  { id: 'analyze', level: 4, label: '4. Phân tích', caption: 'Tách ý, so sánh', backend: '4_phan_tich' },
  { id: 'evaluate', level: 5, label: '5. Đánh giá', caption: 'Nhận xét, lựa chọn', backend: '5_danh_gia' },
  { id: 'create', level: 6, label: '6. Sáng tạo', caption: 'Thiết kế giải pháp', backend: '6_sang_tao' },
];

// Ma trận tương thích đã được chốt theo loại câu hỏi của dự án.
// Các giá trị là UI id để giao diện có thể khóa ngay trước khi tạo request.
// Phải đồng bộ với QUESTION_TYPE_BLOOM_LEVELS trong backend/modules/generation/schemas.py.
export const QUESTION_TYPE_BLOOM_MATRIX = Object.freeze({
  mcq: Object.freeze(['remember', 'understand', 'apply', 'analyze', 'evaluate']),
  multi: Object.freeze(['remember', 'understand', 'apply', 'analyze', 'evaluate']),
  tf: Object.freeze(['remember', 'understand', 'apply']),
  fill: Object.freeze(['remember', 'understand', 'apply']),
  match: Object.freeze(['remember', 'understand']),
  order: Object.freeze(['remember', 'understand', 'apply']),
  scenario: Object.freeze(['apply', 'analyze', 'evaluate', 'create']),
});

export function allowedBloomLevels(questionTypeId) {
  const allowedIds = new Set(QUESTION_TYPE_BLOOM_MATRIX[questionTypeId] || []);
  return BLOOM_LEVELS.filter((level) => allowedIds.has(level.id));
}

export function isBloomAllowedForQuestionType(questionTypeId, bloomId) {
  return QUESTION_TYPE_BLOOM_MATRIX[questionTypeId]?.includes(bloomId) === true;
}

export function normalizeBloomForQuestionType(questionTypeId, bloomId) {
  if (isBloomAllowedForQuestionType(questionTypeId, bloomId)) return bloomId;
  return allowedBloomLevels(questionTypeId)[0]?.id || BLOOM_LEVELS[0].id;
}

const questionTypeByUi = Object.fromEntries(QUESTION_TYPES.map((item) => [item.id, item]));
const bloomByUi = Object.fromEntries(BLOOM_LEVELS.map((item) => [item.id, item]));
const questionTypeByBackend = Object.fromEntries(QUESTION_TYPES.map((item) => [item.backend, item]));
const bloomByBackend = Object.fromEntries(BLOOM_LEVELS.map((item) => [item.backend, item]));

export function toBackendQuestionType(uiId) {
  return questionTypeByUi[uiId]?.backend;
}

export function toBackendBloomLevel(uiId) {
  return bloomByUi[uiId]?.backend;
}

export function questionTypeLabel(backendValue) {
  return questionTypeByBackend[backendValue]?.label || backendValue;
}

export function bloomLevelLabel(backendValue) {
  return bloomByBackend[backendValue]?.label || backendValue;
}

export const DIFFICULTIES = [
  { id: 'de', label: 'Dễ', backend: 'de' },
  { id: 'trung_binh', label: 'Trung bình', backend: 'trung_binh' },
  { id: 'kho', label: 'Khó', backend: 'kho' },
];

const difficultyByValue = Object.fromEntries(
  DIFFICULTIES.flatMap((item) => [[item.id, item], [item.backend, item]]),
);

export function toBackendDifficulty(uiId) {
  return difficultyByValue[uiId]?.backend;
}

export function difficultyLabel(value) {
  if (!value) return '';
  return difficultyByValue[String(value).trim()]?.label || '';
}
