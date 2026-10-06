export function buildGenerationRequest({
  documentId,
  extraDocumentIds = [],
  targetCloIds = [],
  questionPlan,
  teacherInstruction,
  targetHeading,
  sourceMode,
  modelProvider,
  timings,
  pipelineStartedAt,
  now,
}) {
  const firstPlanItem = questionPlan[0];
  const instruction = teacherInstruction.trim() || undefined;

  return {
    document_id: documentId,
    ...(extraDocumentIds.length ? { document_ids: extraDocumentIds.filter((id) => id && id !== documentId) } : {}),
    ...(targetCloIds.length ? { target_clo_ids: targetCloIds } : {}),
    bloom_level: firstPlanItem.bloom_level,
    difficulty: firstPlanItem.difficulty,
    question_type: firstPlanItem.question_type,
    num_questions: firstPlanItem.num_questions,
    question_plan: questionPlan,
    instruction,
    ...(targetHeading?.trim() ? { target_heading: targetHeading.trim() } : {}),
    ...(modelProvider ? { model_provider: modelProvider } : {}),
    client_telemetry: {
      source_mode: sourceMode,
      document_reused: timings.documentMs === 'reused',
      upload_ms: timings.uploadMs,
      ocr_ms: timings.ocrMs,
      chunk_ms: timings.chunkMs,
      elapsed_before_generate_ms: Math.round(now() - pipelineStartedAt),
    },
  };
}
