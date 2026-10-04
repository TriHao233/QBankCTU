function refId(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return value.id || value._id || '';
}

function percent(count, total) {
  if (!total) return 0;
  return Math.round((count / total) * 100);
}

function increment(map, key, approved) {
  if (!key) return;
  const current = map.get(key) || { count: 0, approved: 0 };
  current.count += 1;
  if (approved) current.approved += 1;
  map.set(key, current);
}

function questionBloomLevel(question) {
  const level = question?.classification?.bloom?.level;
  return level ? String(level) : '';
}

function questionChapterId(question) {
  return refId(question?.classification?.chapter?.id || question?.classification?.chapter);
}

function questionCloIds(question) {
  return (question?.clos || []).map((clo) => refId(clo.id || clo)).filter(Boolean);
}

function questionTypeKey(question) {
  return String(
    question?.classification?.assessment_type
    || question?.question_type
    || question?.type
    || '',
  ).trim().toLowerCase();
}

function isApproved(question) {
  return question?.review_status === 'APPROVED';
}

function coverageStatus({ count, total, targetRatio = 0 }) {
  if (!total || count === 0) return 'empty';
  if (targetRatio > 0 && count / total < targetRatio * 0.75) return 'low';
  return 'ok';
}

function catalogItems(items, fallbackCounts, labelForItem) {
  const activeItems = (items || []).filter((item) => item?.is_active !== false);
  if (activeItems.length > 0) {
    return activeItems.map((item) => ({
      id: refId(item),
      label: labelForItem(item),
      target_weight: Number(item.target_weight) || 0,
    })).filter((item) => item.id);
  }
  return Array.from(fallbackCounts.keys()).map((id) => ({ id, label: id, target_weight: 0 }));
}

function enrichRows(items, counts, total, totalWeight = 0) {
  return items.map((item) => {
    const value = counts.get(item.id) || { count: 0, approved: 0 };
    const targetRatio = totalWeight > 0 && item.target_weight > 0
      ? item.target_weight / totalWeight
      : 0;
    return {
      ...item,
      count: value.count,
      approved: value.approved,
      percent: percent(value.count, total),
      target_percent: percent(targetRatio, 1),
      status: coverageStatus({ count: value.count, total, targetRatio }),
    };
  });
}

export function buildQuestionCoverage({
  questions = [],
  subject = null,
  bloomLevels = [],
  questionTypes = [],
} = {}) {
  const total = questions.length;
  const bloomCounts = new Map();
  const chapterCounts = new Map();
  const cloCounts = new Map();
  const typeCounts = new Map();
  let approvedTotal = 0;

  questions.forEach((question) => {
    const approved = isApproved(question);
    if (approved) approvedTotal += 1;
    increment(bloomCounts, questionBloomLevel(question), approved);
    increment(chapterCounts, questionChapterId(question), approved);
    increment(typeCounts, questionTypeKey(question), approved);
    questionCloIds(question).forEach((cloId) => increment(cloCounts, cloId, approved));
  });

  const bloom = bloomLevels.map((level) => {
    const key = String(level.level);
    const value = bloomCounts.get(key) || { count: 0, approved: 0 };
    return {
      id: key,
      label: level.label || key,
      count: value.count,
      approved: value.approved,
      percent: percent(value.count, total),
      status: coverageStatus({ count: value.count, total }),
    };
  });

  const chapterItems = catalogItems(
    subject?.chapters,
    chapterCounts,
    (chapter) => chapter.chapter_code
      ? `${chapter.chapter_code} - ${chapter.chapter_name || chapter.chapter_code}`
      : chapter.chapter_name || chapter.name || refId(chapter),
  );
  const cloItems = catalogItems(
    subject?.learning_outcomes,
    cloCounts,
    (clo) => clo.clo_code || clo.code || refId(clo),
  );
  const cloWeight = cloItems.reduce((sum, item) => sum + Math.max(0, item.target_weight), 0);
  const chapters = enrichRows(chapterItems, chapterCounts, total);
  const clos = enrichRows(cloItems, cloCounts, total, cloWeight);

  const typeCatalog = (questionTypes || []).map((item) => ({
    id: item.backend || item.id,
    label: item.label || item.backend || item.id,
    target_weight: 0,
  })).filter((item) => item.id);
  const typeFallback = catalogItems([], typeCounts, (item) => item.id || item);
  const typeItems = typeCatalog.length
    ? typeCatalog
    : typeFallback;
  const types = enrichRows(typeItems, typeCounts, total);

  return {
    total,
    approvedTotal,
    bloom,
    chapters,
    clos,
    types,
    gaps: {
      bloom: bloom.filter((item) => item.count === 0).length,
      chapters: chapters.filter((item) => item.count === 0).length,
      clos: clos.filter((item) => item.count === 0).length,
      types: types.filter((item) => item.count === 0).length,
    },
  };
}
