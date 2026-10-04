export function buildGenerationRequest({
  documentId,
  questionPlan,
  teacherInstruction,
  targetHeading,
  sourceMode,
  modelProvider,
  timings,
  pipelineStartedAt,
  now,
  cloIds,
}) {
  const firstPlanItem = questionPlan[0];
  const instruction = teacherInstruction.trim() || undefined;
  const selectedCloIds = [...new Set((cloIds || []).map((id) => String(id || '').trim()).filter(Boolean))];

  return {
    document_id: documentId,
    bloom_level: firstPlanItem.bloom_level,
    difficulty: firstPlanItem.difficulty,
    question_type: firstPlanItem.question_type,
    num_questions: firstPlanItem.num_questions,
    question_plan: questionPlan,
    instruction,
    ...(targetHeading?.trim() ? { target_heading: targetHeading.trim() } : {}),
    ...(modelProvider ? { model_provider: modelProvider, code_model_provider: modelProvider } : {}),
    ...(selectedCloIds.length ? { clo_ids: selectedCloIds } : {}),
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
