const FLAG_LABELS = {
  duplicate_suspect: 'Nghi trùng với câu hỏi khác của tài liệu',
  near_duplicate: 'Gần trùng với câu hỏi khác của tài liệu',
  weak_grounding: 'Đoạn minh chứng không khớp nguồn trích dẫn',
  ungrounded: 'Câu hỏi không có nguồn trích dẫn',
  answer_not_in_source: 'Đáp án đúng không tìm thấy căn cứ trong nguồn trích dẫn',
  distractor_equals_answer: 'Có phương án nhiễu trùng nội dung với đáp án đúng',
  duplicate_options: 'Có các lựa chọn trùng nội dung nhau',
  distractor_in_evidence: 'Đoạn minh chứng chứa một phương án nhiễu nhưng không chứa đáp án đúng',
};

export function submissionCheckFlags(question) {
  const flags = question?.review_submission?.checks?.flags || [];
  return [...new Set(flags.map(String))].map((flag) => ({ flag, label: FLAG_LABELS[flag] || flag }));
}

export function unacknowledgedFlags(question, acknowledged) {
  const seen = new Set(acknowledged || []);
  return submissionCheckFlags(question).filter((item) => !seen.has(item.flag));
}

export function submissionCheckWarnings(question) {
  const checks = question?.review_submission?.checks;
  if (!checks) return [];
  const labels = (checks.flags || []).map((flag) => FLAG_LABELS[flag] || flag);
  const extra = (checks.warnings || []).filter((warning) => !labels.includes(warning));
  return [...new Set([...labels, ...extra])];
}
