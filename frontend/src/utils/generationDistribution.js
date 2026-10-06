import { BLOOM_LEVELS, DIFFICULTIES, isBloomAllowedForQuestionType } from '../constants/generationEnums.js';

// Mirrors expand_distribution in backend/modules/generation/schemas.py.
export function largestRemainder(total, ratios) {
  const entries = Object.entries(ratios).filter(([, ratio]) => Number(ratio) > 0);
  const weight = entries.reduce((sum, [, ratio]) => sum + Number(ratio), 0);
  if (!weight) return {};
  const exact = entries.map(([key, ratio], index) => {
    const value = (total * Number(ratio)) / weight;
    return { key, index, value, count: Math.floor(value) };
  });
  let remaining = total - exact.reduce((sum, item) => sum + item.count, 0);
  [...exact]
    .sort((a, b) => (b.value - b.count) - (a.value - a.count) || a.index - b.index)
    .forEach((item) => {
      if (remaining > 0) {
        item.count += 1;
        remaining -= 1;
      }
    });
  return Object.fromEntries(exact.filter((item) => item.count > 0).map((item) => [item.key, item.count]));
}

export function expandDistribution({ total, questionTypeIds, bloomRatios, difficultyRatios = {} }) {
  if (!questionTypeIds?.length) throw new Error('Chọn ít nhất một dạng câu hỏi.');
  const orderedBlooms = Object.fromEntries(
    BLOOM_LEVELS.filter((level) => Number(bloomRatios[level.id]) > 0).map((level) => [level.id, bloomRatios[level.id]]),
  );
  const orderedDifficulties = Object.fromEntries(
    DIFFICULTIES.filter((item) => Number(difficultyRatios[item.id]) > 0).map((item) => [item.id, difficultyRatios[item.id]]),
  );
  const bloomCounts = largestRemainder(total, orderedBlooms);
  if (!Object.keys(bloomCounts).length) throw new Error('Nhập tỷ lệ cho ít nhất một mức Bloom.');
  const counts = new Map();
  let rotation = 0;
  Object.entries(bloomCounts).forEach(([bloomId, bloomCount]) => {
    const allowed = questionTypeIds.filter((typeId) => isBloomAllowedForQuestionType(typeId, bloomId));
    if (!allowed.length) {
      const label = BLOOM_LEVELS.find((level) => level.id === bloomId)?.label || bloomId;
      throw new Error(`Không có dạng câu hỏi đã chọn nào phù hợp với mức Bloom ${label}.`);
    }
    const cells = Object.keys(orderedDifficulties).length
      ? largestRemainder(bloomCount, orderedDifficulties)
      : { trung_binh: bloomCount };
    Object.entries(cells).forEach(([difficulty, cellCount]) => {
      for (let index = 0; index < cellCount; index += 1) {
        const typeId = allowed[rotation % allowed.length];
        const key = `${typeId}|${bloomId}|${difficulty}`;
        const current = counts.get(key) || { questionTypeId: typeId, bloomId, difficulty, count: 0 };
        current.count += 1;
        counts.set(key, current);
        rotation += 1;
      }
    });
  });
  return [...counts.values()];
}
