import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import QuestionContent from '../components/QuestionContent';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faChevronDown, faLayerGroup, faUpload, faWandMagicSparkles, faMicrochip, faRobot, faRotateRight, faBookOpen, faArrowRight, faPlus, faPen, faPaperPlane, faTrashCan, faFloppyDisk, faXmark } from '@fortawesome/free-solid-svg-icons';
import { chunkDocument } from '../api/chunk';
import { listAvailableAiModels, listSubjects } from '../api/catalog';
import { getDocument, listDocuments } from '../api/documents';
import { enqueueGenerateQuestions, getGenerateStatus, streamGenerateStatus, cancelGenerateJob } from '../api/generate';
import { getOcrStatus, uploadSourceDocument } from '../api/ocr';
import { createQuestion, deleteQuestion, submitQuestionForReview, updateQuestion } from '../api/questions';
import { deleteGenerationPreset, listGenerationPresets, saveGenerationPreset } from '../api/users';
import {
  BLOOM_LEVELS,
  DIFFICULTIES,
  QUESTION_TYPES,
  isBloomAllowedForQuestionType,
  bloomLevelLabel,
  difficultyLabel,
  normalizeBloomForQuestionType,
  questionTypeLabel,
  toBackendBloomLevel,
  toBackendQuestionType,
} from '../constants/generationEnums';
import {
  isDocumentPipelineFailed,
  isDocumentPipelineReady,
} from '../constants/lifecycleStatus';
import { pollJob, watchJob } from '../hooks/useJobPoll';
import { buildGenerationRequest } from '../utils/generationRequest';
import { modelPresentation } from '../utils/modelPresentation';
import { formatChoices, mapGeneratedQuestions } from '../utils/mapGeneratedQuestion';
import {
  SINGLE_CHOICE_TYPES,
  MULTI_CHOICE_TYPES,
  correctAnswerValues,
  entriesToOptions,
  joinCorrectValues,
  optionEntriesForQuestion as optionEntriesForDraft,
  validateQuestionAnswer,
} from '../utils/questionOptions';
import '../css/GeneratePage.css';

const PHASE_LABELS = {
  idle: 'Sẵn sàng',
  uploading: 'Đang tải tài liệu lên...',
  ocr_queued: 'Tài liệu đã vào hàng đợi xử lý',
  ocr_processing: 'Đang OCR/trích xuất tài liệu...',
  chunking: 'Đang index / chunk tài liệu...',
  generate_queued: 'Sinh câu hỏi đã vào hàng đợi',
  generate_processing: 'Đang sinh câu hỏi bằng AI...',
  completed: 'Hoàn tất',
  failed: 'Thất bại',
};

const MAX_TOTAL_QUESTIONS = 7;
const DRAFTS_PER_PAGE = 3;
const SUPPORTED_SOURCE_EXTENSIONS = ['.pdf', '.doc', '.docx', '.md', '.markdown', '.txt'];
const PRESET_STORAGE_KEY = 'qbank_generation_presets';
const SUBMITTABLE_REVIEW_STATUSES = new Set(['DRAFT', 'NEEDS_REVISION']);

function isDeepSeekModel(model = {}) {
  return [model.code, model.name, model.version]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes('deepseek'));
}

const REVIEW_STATUS_LABEL = {
  DRAFT: 'Nháp',
  PENDING: 'Chờ duyệt',
  APPROVED: 'Đã duyệt',
  NEEDS_REVISION: 'Cần sửa',
  REJECTED: 'Từ chối',
};

function shortId(id) {
  if (!id) return '';
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

function shortCode(code) {
  if (!code) return 'Q';
  return code.length > 18 ? `${code.slice(0, 12)}...${code.slice(-4)}` : code;
}

function formatDateTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('vi-VN');
}

function nowMs() {
  return globalThis.performance?.now?.() ?? Date.now();
}

function formatDuration(value) {
  if (!Number.isFinite(value) || value < 0) return '';
  const totalSeconds = Math.max(1, Math.round(value / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes === 0) return `${seconds}s`;
  return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

function normalizeCount(value) {
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return 1;
  return Math.min(MAX_TOTAL_QUESTIONS, Math.max(1, Math.trunc(parsed)));
}

function createPlanItem(overrides = {}) {
  const fallbackId = `plan-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const item = {
    id: globalThis.crypto?.randomUUID?.() || fallbackId,
    questionTypeId: 'mcq',
    bloomId: 'remember',
    difficulty: 'trung_binh',
    count: 1,
    contentMode: 'auto',
    ...overrides,
  };
  return {
    ...item,
    bloomId: normalizeBloomForQuestionType(item.questionTypeId, item.bloomId),
  };
}

function createInitialPlan() {
  return [createPlanItem({ count: 5 })];
}

function loadStoredPresets() {
  try {
    const rawValue = globalThis.localStorage?.getItem(PRESET_STORAGE_KEY);
    const parsed = rawValue ? JSON.parse(rawValue) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function storePresets(presets) {
  globalThis.localStorage?.setItem(PRESET_STORAGE_KEY, JSON.stringify(presets));
}

function presetInstructionValue(preset) {
  return String(preset.instruction || '').trim();
}

function presetApiPayload(preset) {
  return {
    name: String(preset.name || '').trim(),
    planItems: (preset.planItems || []).map(({ questionTypeId, bloomId, difficulty, count, contentMode }) => ({
      questionTypeId,
      bloomId,
      difficulty: difficulty || 'trung_binh',
      count: normalizeCount(count),
      contentMode: contentMode || 'auto',
    })),
    instruction: String(preset.instruction || '').trim(),
    targetHeading: preset.targetHeading || null,
  };
}

function documentPipeline(document) {
  return document?.pipeline_summary || {};
}

function normalizeStatus(status) {
  return String(status || '').toUpperCase();
}

function isUnavailableDocument(document) {
  const pipeline = documentPipeline(document);
  const blockedStatuses = new Set(['FAILED', 'CANCELLED', 'ARCHIVED']);
  return (
    blockedStatuses.has(normalizeStatus(document?.status))
    || blockedStatuses.has(normalizeStatus(pipeline.ocr_status))
    || blockedStatuses.has(normalizeStatus(pipeline.chunk_status))
    || blockedStatuses.has(normalizeStatus(pipeline.index_status))
  );
}

function isDocumentOcrReady(document) {
  if (isUnavailableDocument(document)) return false;
  const pipeline = documentPipeline(document);
  return (
    normalizeStatus(document?.status) === 'READY' ||
    normalizeStatus(pipeline.ocr_status) === 'COMPLETED' ||
    Boolean(document?.current_processing?.ocr_job_id && Number(document?.page_count) > 0)
  );
}

function isDocumentIndexed(document) {
  if (isUnavailableDocument(document)) return false;
  const pipeline = documentPipeline(document);
  return (
    normalizeStatus(document?.status) === 'READY' ||
    (normalizeStatus(pipeline.chunk_status) === 'COMPLETED' && normalizeStatus(pipeline.index_status) === 'COMPLETED')
  );
}

function reusableDocumentLabel(document) {
  const pages = document.page_count ? `${document.page_count} trang` : 'chưa rõ số trang';
  const pipeline = documentPipeline(document);
  const state = isDocumentIndexed(document)
    ? 'đã index'
    : normalizeStatus(pipeline.ocr_status) === 'COMPLETED'
      ? 'đã xử lý'
      : document.status;
  return `${document.title} (${pages}, ${state})`;
}

function validateDraftBeforeSave(draft) {
  return validateQuestionAnswer({
    questionType: draft.questionType,
    rawOptions: draft.rawOptions,
    correctAnswer: draft.correctAnswer,
  });
}

function isSupportedSourceFile(fileValue) {
  const name = fileValue?.name || '';
  return SUPPORTED_SOURCE_EXTENSIONS.some((extension) => name.toLowerCase().endsWith(extension));
}

function mergeUpdatedDraft(draft, updatedQuestion) {
  const questionData = updatedQuestion.question_data || {};
  const rawOptions = questionData.options ?? draft.rawOptions;
  const correctAnswer = questionData.correct_answer ?? draft.correctAnswer;

  return {
    ...draft,
    persistedId: updatedQuestion.id || draft.persistedId,
    questionCode: updatedQuestion.question_code || draft.questionCode,
    currentVersion: updatedQuestion.current_version || draft.currentVersion,
    currentVersionId: updatedQuestion.current_version_id || draft.currentVersionId,
    reviewStatus: updatedQuestion.review_status || draft.reviewStatus,
    text: updatedQuestion.content || draft.text,
    rawOptions,
    correctAnswer,
    choices: formatChoices(rawOptions, correctAnswer),
    explanation: questionData.explanation ?? draft.explanation,
    sourceContext: questionData.model_source_context ?? draft.sourceContext,
    validationWarnings: questionData.validation_warnings
      || questionData.post_processing?.warnings
      || draft.validationWarnings
      || [],
  };
}

function canSubmitDraft(draft) {
  return SUBMITTABLE_REVIEW_STATUSES.has(String(draft.reviewStatus || 'DRAFT').toUpperCase());
}

function generatedDraftPayload(draft, fallbackDocumentId) {
  return {
    content: draft.text.trim(),
    question_type: draft.questionType,
    bloom_level: Number.parseInt(String(draft.bloomLevel || ''), 10) || null,
    difficulty: draft.difficulty || null,
    question_data: {
      options: draft.rawOptions ?? null,
      correct_answer: draft.correctAnswer,
      explanation: draft.explanation,
      model_source_context: draft.sourceContext,
      source_keywords: draft.sourceKeywords || [],
      false_mutation: draft.falseMutation || null,
      validation_warnings: draft.validationWarnings || [],
    },
    document_id: draft.documentId || fallbackDocumentId || null,
    source_chunk_ids: draft.sourceChunkIds || [],
    clo_ids: draft.cloIds || [],
  };
}

function GeneratePage() {
  const abortRef = useRef(null);
  const fileInputRef = useRef(null);
  const timingRef = useRef({});
  const pipelineBusyRef = useRef(false);
  const [phase, setPhase] = useState('idle');
  const [error, setError] = useState('');
  const [statusDetail, setStatusDetail] = useState('');
  const [sourceMode, setSourceMode] = useState('upload');
  const [file, setFile] = useState(null);
  const [fileName, setFileName] = useState('');
  const [documents, setDocuments] = useState([]);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [documentsError, setDocumentsError] = useState('');
  const [selectedDocumentId, setSelectedDocumentId] = useState('');
  const [subjects, setSubjects] = useState([]);
  const [subjectsLoading, setSubjectsLoading] = useState(false);
  const [subjectsError, setSubjectsError] = useState('');
  const [selectedSubjectId, setSelectedSubjectId] = useState('');
  const [selectedCloIds, setSelectedCloIds] = useState([]);
  const [planItems, setPlanItems] = useState(createInitialPlan);
  const [presets, setPresets] = useState(loadStoredPresets);
  const [selectedPresetId, setSelectedPresetId] = useState('');
  const [presetDialogOpen, setPresetDialogOpen] = useState(false);
  const [presetName, setPresetName] = useState('');
  const [presetError, setPresetError] = useState('');
  const [teacherInstruction, setTeacherInstruction] = useState('');
  const [targetHeading, setTargetHeading] = useState('');
  const [availableModels, setAvailableModels] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const [selectedModelCode, setSelectedModelCode] = useState('');
  const [documentId, setDocumentId] = useState(null);
  const [activeJobId, setActiveJobId] = useState('');
  const generationJobRef = useRef('');
  const [generationInfo, setGenerationInfo] = useState(null);
  const [drafts, setDrafts] = useState([]);
  const [generationSummary, setGenerationSummary] = useState([]);
  const [timings, setTimings] = useState({});
  const [chunkReady, setChunkReady] = useState(false);
  const [presetSectionOpen, setPresetSectionOpen] = useState(false);
  const [draftPage, setDraftPage] = useState(0);
  const [editingDraftId, setEditingDraftId] = useState(null);
  const [draftEditSnapshot, setDraftEditSnapshot] = useState(null);
  const [savingDraftId, setSavingDraftId] = useState(null);
  const [removingDraftId, setRemovingDraftId] = useState(null);
  const [submittingDraftId, setSubmittingDraftId] = useState(null);
  const [bulkSubmittingDrafts, setBulkSubmittingDrafts] = useState(false);

  const isBusy = !['idle', 'completed', 'failed'].includes(phase);
  const questionPlan = planItems
    .map((item) => ({
      question_type: toBackendQuestionType(item.questionTypeId),
      bloom_level: toBackendBloomLevel(item.bloomId),
      difficulty: item.difficulty || 'trung_binh',
      num_questions: normalizeCount(item.count),
      content_mode: item.contentMode || 'auto',
    }))
    .filter((item) => item.question_type && item.bloom_level && item.num_questions > 0);
  const totalQuestions = questionPlan.reduce((total, item) => total + item.num_questions, 0);
  const reusableDocuments = documents.filter(isDocumentOcrReady);
  const selectedDocument = reusableDocuments.find((item) => item.id === selectedDocumentId);
  const activeSubjects = subjects.filter((item) => item.is_active !== false);
  const selectedSubject = activeSubjects.find(
    (item) => String(item.id || item._id) === String(selectedSubjectId),
  ) || null;
  const availableClos = (selectedSubject?.learning_outcomes || []).filter(
    (clo) => clo.is_active !== false,
  );
  const selectedDocumentSubjectName = selectedDocument
    ? subjects.find((item) => (item.id || item._id) === selectedDocument.subject_id)?.subject_name
    : null;
  const selectedModel = availableModels.find((model) => model.code === selectedModelCode);
  const modelDisplay = modelPresentation(selectedModel);
  const modelIcon = modelDisplay.family === 'gemini' ? faWandMagicSparkles
    : modelDisplay.family === 'qwen' ? faMicrochip : faRobot;
  const draftPageCount = Math.max(1, Math.ceil(drafts.length / DRAFTS_PER_PAGE));
  const safeDraftPage = Math.min(draftPage, draftPageCount - 1);
  const visibleDrafts = drafts.slice(
    safeDraftPage * DRAFTS_PER_PAGE,
    safeDraftPage * DRAFTS_PER_PAGE + DRAFTS_PER_PAGE,
  );
  const generationShortfalls = generationSummary.filter((item) => (
    item.skipped_count > 0 || item.duplicate_count > 0 || (item.warnings || []).length > 0
  ));
  const generationAcceptedNothing = (
    phase === 'completed'
    &&
    generationSummary.length > 0
    && drafts.length === 0
    && generationSummary.every((item) => (item.saved_count || 0) === 0)
  );
  const generationStatusClass = phase === 'completed'
    ? (generationAcceptedNothing ? 'warning' : 'done')
    : 'running';
  const submittableDraftCount = drafts.filter(canSubmitDraft).length;
  const bankLinkQuestionId = drafts.find((item) => item.persistedId)?.persistedId;
  const serverMetrics = generationInfo?.metrics?.server || {};
  const hasServerMetrics = Number.isFinite(serverMetrics.processing_ms);
  const hasTimings = Object.values(timings).some((value) => (
    value === 'reused' || Number.isFinite(value)
  )) || hasServerMetrics;
  const timingItems = [
    timings.documentMs === 'reused'
      ? { label: 'Tài liệu', value: 'Đã xử lý/index sẵn' }
      : null,
    Number.isFinite(timings.uploadMs)
      ? { label: 'Upload', value: formatDuration(timings.uploadMs) }
      : null,
    Number.isFinite(timings.ocrMs)
      ? { label: 'Xử lý tài liệu', value: formatDuration(timings.ocrMs) }
      : null,
    Number.isFinite(timings.chunkMs)
      ? { label: 'Chunk/Index', value: formatDuration(timings.chunkMs) }
      : null,
    Number.isFinite(timings.generateMs)
      ? { label: 'Sinh câu hỏi', value: formatDuration(timings.generateMs) }
      : null,
    Number.isFinite(serverMetrics.processing_ms)
      ? { label: 'Backend job', value: formatDuration(serverMetrics.processing_ms) }
      : null,
    Number.isFinite(timings.totalMs)
      ? { label: 'Tổng', value: formatDuration(timings.totalMs) }
      : null,
  ].filter(Boolean);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    let saved;
    try {
      saved = JSON.parse(sessionStorage.getItem('active-generation-job') || 'null');
    } catch {
      return;
    }
    if (!saved?.jobId) return;
    const controller = new AbortController();
    generationJobRef.current = saved.jobId;
    setActiveJobId(saved.jobId);
    setDocumentId(saved.documentId || null);
    setChunkReady(true);
    setPhase('generate_processing');
    watchJob(getGenerateStatus, saved.jobId, {
      streamStatus: streamGenerateStatus,
      signal: controller.signal,
      timeoutMs: 20 * 60 * 1000,
    }).then(async (terminal) => {
      const result = terminal.status === 'completed'
        ? await getGenerateStatus(saved.jobId, { signal: controller.signal })
        : terminal;
      if (controller.signal.aborted) return;
      sessionStorage.removeItem('active-generation-job');
      generationJobRef.current = '';
      if (result.status !== 'completed' || !Array.isArray(result.data) || result.data.length === 0) {
        setPhase('failed');
        setError(result.error_message || `Job ${saved.jobId} không trả về câu hỏi`);
        return;
      }
      setDrafts(mapGeneratedQuestions(result.data));
      setGenerationSummary(result.summary || []);
      setGenerationInfo({
        jobId: saved.jobId,
        documentId: saved.documentId,
        requestedCount: saved.requestedCount,
        generatedCount: result.data.length,
        createdAt: result.created_at,
        updatedAt: result.updated_at,
        metrics: result.metrics,
        model: result.model,
      });
      setStatusDetail(`Đã sinh ${result.data.length}/${saved.requestedCount || result.data.length} câu hỏi`);
      setPhase('completed');
    }).catch((err) => {
      if (controller.signal.aborted) return;
      setPhase('failed');
      setError(err.message || 'Không tải được kết quả sinh câu hỏi');
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    setDraftPage((current) => Math.min(current, draftPageCount - 1));
  }, [draftPageCount]);

  const resetPoll = () => {
    abortRef.current?.abort();
    abortRef.current = new AbortController();
    return abortRef.current;
  };

  const setTimingValues = (patch) => {
    timingRef.current = { ...timingRef.current, ...patch };
    setTimings(timingRef.current);
    return timingRef.current;
  };

  const resetTimings = (nextTimings = {}) => {
    timingRef.current = nextTimings;
    setTimings(nextTimings);
  };

  const markTiming = (key, startedAt) => {
    const duration = Math.round(nowMs() - startedAt);
    setTimingValues({ [key]: duration });
    return duration;
  };

  const fetchReusableDocuments = async () => {
    setDocumentsLoading(true);
    setDocumentsError('');
    try {
      const result = await listDocuments({ page: 1, pageSize: 100 });
      setDocuments(result.items || []);
    } catch (err) {
      setDocumentsError(err.message || 'Không tải được danh sách tài liệu đã xử lý');
    } finally {
      setDocumentsLoading(false);
    }
  };

  const fetchSubjects = async () => {
    setSubjectsLoading(true);
    setSubjectsError('');
    try {
      const result = await listSubjects();
      setSubjects(result || []);
    } catch (err) {
      setSubjectsError(err.message || 'Không tải được danh sách học phần');
    } finally {
      setSubjectsLoading(false);
    }
  };

  const fetchAvailableModels = async () => {
    setModelsLoading(true);
    setModelsError('');
    try {
      const result = await listAvailableAiModels('QUESTION_GENERATION');
      const items = (result.items || []).filter((model) => !isDeepSeekModel(model));
      const defaultModelCode = items.some((model) => model.code === result.default_model_code)
        ? result.default_model_code
        : (items[0]?.code || '');
      setAvailableModels(items);
      setSelectedModelCode((current) => (
        items.some((model) => model.code === current)
          ? current
          : defaultModelCode
      ));
    } catch {
      setAvailableModels([]);
      setSelectedModelCode('');
      setModelsError('Đang dùng mô hình mặc định của hệ thống.');
    } finally {
      setModelsLoading(false);
    }
  };

  const syncGenerationPresets = async () => {
    try {
      const result = await listGenerationPresets();
      const localPresets = loadStoredPresets();
      const serverPresets = Array.isArray(result.items) ? result.items : [];
      if (serverPresets.length === 0 && localPresets.length > 0) {
        const migratedPresets = [];
        for (const preset of localPresets.slice(0, 12)) {
          const payload = presetApiPayload(preset);
          if (!payload.name || payload.planItems.length === 0) continue;
          migratedPresets.push(await saveGenerationPreset(payload));
        }
        if (migratedPresets.length > 0) {
          setPresets(migratedPresets);
          storePresets(migratedPresets);
          return;
        }
      }
      setPresets(serverPresets);
      storePresets(serverPresets);
    } catch {
      setPresets(loadStoredPresets());
    }
  };

  useEffect(() => {
    fetchReusableDocuments();
    fetchSubjects();
    fetchAvailableModels();
    syncGenerationPresets();
  }, []);

  const validateForm = (processDocumentOnly = false) => {
    if (sourceMode === 'upload') {
      if (!file) return 'Vui lòng chọn file PDF, DOC/DOCX, Markdown hoặc TXT';
      if (!isSupportedSourceFile(file)) return 'Chỉ hỗ trợ file PDF, DOC/DOCX, Markdown hoặc TXT';
      if (!selectedSubjectId) return 'Vui lòng chọn học phần';
    } else {
      if (!selectedDocumentId) return 'Vui lòng chọn tài liệu đã xử lý';
      if (!selectedDocument) return 'Không tìm thấy tài liệu đã chọn';
      if (!isDocumentOcrReady(selectedDocument)) return 'Tài liệu này chưa xử lý xong';
    }

    if (!processDocumentOnly) {
      if (questionPlan.length === 0) return 'Vui lòng thêm ít nhất một dòng cấu hình câu hỏi';
      if (totalQuestions < 1 || totalQuestions > MAX_TOTAL_QUESTIONS) {
        return `Tổng số câu hỏi phải từ 1 đến ${MAX_TOTAL_QUESTIONS}`;
      }
    }
    return null;
  };

  const updatePlanItem = (itemId, patch) => {
    setPlanItems((current) => current.map((item) => (
      item.id === itemId ? { ...item, ...patch } : item
    )));
  };

  const updatePlanQuestionType = (itemId, questionTypeId) => {
    setPlanItems((current) => current.map((item) => (
      item.id === itemId
        ? {
          ...item,
          questionTypeId,
          bloomId: normalizeBloomForQuestionType(questionTypeId, item.bloomId),
        }
        : item
    )));
  };

  const addPlanItem = () => {
    setPlanItems((current) => [...current, createPlanItem()]);
  };

  const removePlanItem = (itemId) => {
    setPlanItems((current) => {
      if (current.length === 1) return current;
      return current.filter((item) => item.id !== itemId);
    });
  };

  const applyPreset = (presetId) => {
    const preset = presets.find((item) => item.id === presetId);
    if (!preset) return;
    setSelectedPresetId(presetId);
    setPlanItems((preset.planItems || []).map((item) => createPlanItem(item)));
    setTeacherInstruction(presetInstructionValue(preset));
    setTargetHeading(String(preset.targetHeading || ''));
    setError('');
    setStatusDetail(`Đã áp dụng mẫu "${preset.name}"`);
  };

  const openPresetDialog = () => {
    setPresetName('');
    setPresetError('');
    setPresetDialogOpen(true);
  };

  const closePresetDialog = () => {
    setPresetDialogOpen(false);
    setPresetName('');
    setPresetError('');
  };

  const savePreset = async (event) => {
    event?.preventDefault();
    const name = presetName.trim();
    if (!name) {
      setPresetError('Vui lòng nhập tên mẫu.');
      return;
    }
    const presetPayload = {
      name,
      planItems: planItems.map(({ questionTypeId, bloomId, difficulty, count, contentMode }) => ({
        questionTypeId,
        bloomId,
        difficulty: difficulty || 'trung_binh',
        count: normalizeCount(count),
        contentMode: contentMode || 'auto',
      })),
      instruction: teacherInstruction.trim(),
      targetHeading: targetHeading.trim() || null,
    };
    try {
      const nextPreset = await saveGenerationPreset(presetPayload);
      const nextPresets = [nextPreset, ...presets.filter((preset) => preset.id !== nextPreset.id)].slice(0, 12);
      storePresets(nextPresets);
      setPresets(nextPresets);
      setSelectedPresetId(nextPreset.id);
      closePresetDialog();
      setStatusDetail(`Đã lưu mẫu "${nextPreset.name}"`);
    } catch (err) {
      const fallbackPreset = {
        id: globalThis.crypto?.randomUUID?.() || `preset-${Date.now()}`,
        ...presetPayload,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
      };
      const nextPresets = [fallbackPreset, ...presets].slice(0, 12);
      try {
        storePresets(nextPresets);
        setPresets(nextPresets);
        setSelectedPresetId(fallbackPreset.id);
        closePresetDialog();
        setStatusDetail(`Đã lưu mẫu "${fallbackPreset.name}" trên trình duyệt; chưa đồng bộ server.`);
      } catch (storageError) {
        setPresetError(`Lưu mẫu thất bại: ${storageError.message || err.message}`);
      }
    }
  };

  const deleteSelectedPreset = async () => {
    const preset = presets.find((item) => item.id === selectedPresetId);
    if (!preset) return;
    if (!window.confirm(`Xóa mẫu cấu hình "${preset.name}"?`)) {
      return;
    }
    const nextPresets = presets.filter((item) => item.id !== selectedPresetId);
    try {
      await deleteGenerationPreset(preset.id);
      storePresets(nextPresets);
      setPresets(nextPresets);
      setSelectedPresetId('');
      setStatusDetail(`Đã xóa mẫu "${preset.name}"`);
    } catch (err) {
      try {
        storePresets(nextPresets);
        setPresets(nextPresets);
        setSelectedPresetId('');
        setStatusDetail(`Đã xóa mẫu "${preset.name}" trên trình duyệt; server chưa xác nhận.`);
      } catch (storageError) {
        alert(`Xóa mẫu thất bại: ${storageError.message || err.message}`);
      }
    }
  };

  const updateDraft = (draftId, patch) => {
    setDrafts((current) => current.map((draft) => {
      if (draft.id !== draftId) return draft;
      const nextDraft = { ...draft, ...patch };
      if ('correctAnswer' in patch || 'rawOptions' in patch) {
        nextDraft.choices = formatChoices(nextDraft.rawOptions, nextDraft.correctAnswer);
      }
      return nextDraft;
    }));
  };

  const startEditDraft = (draft) => {
    setError('');
    setDraftEditSnapshot(draft);
    setEditingDraftId(draft.id);
  };

  const cancelEditDraft = () => {
    if (savingDraftId || removingDraftId) return;
    if (draftEditSnapshot) {
      setDrafts((current) => current.map((draft) => (
        draft.id === draftEditSnapshot.id ? draftEditSnapshot : draft
      )));
    }
    setDraftEditSnapshot(null);
    setEditingDraftId(null);
  };

  const handleSaveDraft = async (draft) => {
    if (!draft.text.trim()) {
      alert('Nội dung câu hỏi không được để trống.');
      return;
    }
    const draftValidationError = validateDraftBeforeSave(draft);
    if (draftValidationError) {
      alert(draftValidationError);
      return;
    }

    setSavingDraftId(draft.id);
    try {
      if (!draft.persistedId) {
        updateDraft(draft.id, {
          text: draft.text.trim(),
          choices: formatChoices(draft.rawOptions, draft.correctAnswer),
        });
        setDraftEditSnapshot(null);
        setEditingDraftId(null);
        setStatusDetail('Đã lưu chỉnh sửa trong danh sách nháp');
        return;
      }
      const updatedQuestion = await updateQuestion(draft.persistedId, {
        expected_version: draft.currentVersion || 1,
        content: draft.text.trim(),
        question_data: {
          options: draft.rawOptions ?? null,
          correct_answer: draft.correctAnswer,
          explanation: draft.explanation,
          model_source_context: draft.sourceContext,
        },
        change_note: 'Teacher edited generated draft',
      });
      setDrafts((current) => current.map((item) => (
        item.id === draft.id ? mergeUpdatedDraft(item, updatedQuestion) : item
      )));
      setDraftEditSnapshot(null);
      setEditingDraftId(null);
      setStatusDetail(`Đã lưu ${draft.questionCode || 'câu hỏi nháp'}`);
    } catch (err) {
      alert(`Lưu câu hỏi thất bại: ${err.message}`);
    } finally {
      setSavingDraftId(null);
    }
  };

  const handleRemoveDraft = async (draft) => {
    const label = draft.questionCode || draft.id;
    if (!window.confirm(`Bỏ câu hỏi "${label}" khỏi danh sách nháp?`)) {
      return;
    }

    setRemovingDraftId(draft.id);
    try {
      if (draft.persistedId) {
        await deleteQuestion(draft.persistedId);
      }
      setDrafts((current) => current.filter((item) => item.id !== draft.id));
      if (editingDraftId === draft.id) {
        setDraftEditSnapshot(null);
        setEditingDraftId(null);
      }
      setStatusDetail(`Đã bỏ ${label}`);
    } catch (err) {
      alert(`Bỏ câu hỏi thất bại: ${err.message}`);
    } finally {
      setRemovingDraftId(null);
    }
  };

  const handleSubmitDraftForReview = async (draft) => {
    if (!canSubmitDraft(draft)) {
      alert('Câu hỏi này không còn ở trạng thái có thể gửi duyệt.');
      return;
    }
    setSubmittingDraftId(draft.id);
    let createdQuestion = null;
    try {
      const questionId = draft.persistedId;
      if (!questionId) {
        const validationError = validateDraftBeforeSave(draft);
        if (!draft.text.trim() || validationError) {
          throw new Error(validationError || 'Nội dung câu hỏi không được để trống.');
        }
        createdQuestion = await createQuestion(generatedDraftPayload(draft, generationInfo?.documentId || documentId));
      }
      const updatedQuestion = await submitQuestionForReview(questionId || createdQuestion.id);
      setDrafts((current) => current.map((item) => (
        item.id === draft.id ? mergeUpdatedDraft(item, updatedQuestion) : item
      )));
      setStatusDetail(`Đã gửi ${draft.questionCode || 'câu hỏi nháp'} sang hàng đợi duyệt`);
    } catch (err) {
      if (createdQuestion?.id) {
        try {
          await deleteQuestion(createdQuestion.id);
        } catch {
          // Best-effort cleanup: preserve the original submission error.
        }
      }
      alert(`Gửi duyệt thất bại: ${err.message}`);
    } finally {
      setSubmittingDraftId(null);
    }
  };

  const handleSubmitAllDraftsForReview = async () => {
    const targets = drafts.filter(canSubmitDraft);
    if (targets.length === 0) return;
    if (!window.confirm(`Gửi ${targets.length} câu hỏi nháp sang hàng đợi duyệt?`)) {
      return;
    }
    setBulkSubmittingDrafts(true);
    let successCount = 0;
    const failures = [];
    try {
      // Chạy tuần tự để giữ thứ tự và cho backend rate-limit tự nhiên,
      // nhưng KHÔNG dừng ở lỗi đầu — thu lỗi từng câu để báo cáo cuối.
      for (const draft of targets) {
        setSubmittingDraftId(draft.id);
        let createdQuestion = null;
        try {
          if (!draft.persistedId) {
            createdQuestion = await createQuestion(generatedDraftPayload(draft, generationInfo?.documentId || documentId));
          }
          const updatedQuestion = await submitQuestionForReview(draft.persistedId || createdQuestion.id);
          setDrafts((current) => current.map((item) => (
            item.id === draft.id ? mergeUpdatedDraft(item, updatedQuestion) : item
          )));
          successCount += 1;
        } catch (error) {
          if (createdQuestion?.id) {
            try {
              await deleteQuestion(createdQuestion.id);
            } catch {
              // Best-effort cleanup: giữ lỗi gốc ở dưới.
            }
          }
          failures.push({
            label: draft.questionCode || draft.id,
            message: error?.message || 'Lỗi không rõ',
          });
        }
      }
      if (failures.length === 0) {
        setStatusDetail(`Đã gửi ${successCount}/${targets.length} câu hỏi sang hàng đợi duyệt`);
      } else {
        setStatusDetail(`Đã gửi ${successCount}/${targets.length} câu; ${failures.length} câu thất bại.`);
        const preview = failures.slice(0, 3).map((item) => `• ${item.label}: ${item.message}`).join('\n');
        const suffix = failures.length > 3 ? `\n... và ${failures.length - 3} câu khác` : '';
        alert(`Có ${failures.length}/${targets.length} câu gửi duyệt thất bại:\n${preview}${suffix}`);
      }
    } finally {
      setSubmittingDraftId(null);
      setBulkSubmittingDrafts(false);
    }
  };

  const updateDraftOption = (draft, optionKey, optionValue) => {
    const entries = optionEntriesForDraft(draft);
    const nextEntries = entries.map((entry) => (
      entry.key === optionKey ? { ...entry, value: optionValue } : entry
    ));
    updateDraft(draft.id, { rawOptions: entriesToOptions(nextEntries) });
  };

  const toggleDraftCorrectAnswer = (draft, optionKey) => {
    const entries = optionEntriesForDraft(draft);
    const currentValues = correctAnswerValues(draft.correctAnswer);
    const hasValue = currentValues.includes(optionKey);
    const nextValues = hasValue
      ? currentValues.filter((value) => value !== optionKey)
      : [...currentValues, optionKey];
    updateDraft(draft.id, { correctAnswer: joinCorrectValues(nextValues, entries) });
  };

  const renderDraftAnswerEditor = (question, disabled) => {
    const entries = optionEntriesForDraft(question);

    if (SINGLE_CHOICE_TYPES.has(question.questionType)) {
      return (
        <div className="draft-option-editor">
          {entries.map((entry) => (
            <label className="draft-option-row" key={entry.key}>
              <input
                type="radio"
                name={`answer-${question.id}`}
                checked={question.correctAnswer === entry.key}
                disabled={disabled}
                onChange={() => updateDraft(question.id, { correctAnswer: entry.key })}
              />
              <span className="draft-option-key">{entry.key}</span>
              <input
                className="field-input"
                value={entry.value}
                disabled={disabled}
                onChange={(e) => updateDraftOption(question, entry.key, e.target.value)}
              />
            </label>
          ))}
        </div>
      );
    }

    if (MULTI_CHOICE_TYPES.has(question.questionType)) {
      const selectedAnswers = correctAnswerValues(question.correctAnswer);
      return (
        <div className="draft-option-editor">
          {entries.map((entry) => (
            <label className="draft-option-row" key={entry.key}>
              <input
                type="checkbox"
                checked={selectedAnswers.includes(entry.key)}
                disabled={disabled}
                onChange={() => toggleDraftCorrectAnswer(question, entry.key)}
              />
              <span className="draft-option-key">{entry.key}</span>
              <input
                className="field-input"
                value={entry.value}
                disabled={disabled}
                onChange={(e) => updateDraftOption(question, entry.key, e.target.value)}
              />
            </label>
          ))}
        </div>
      );
    }

    if (entries.length > 0) {
      return (
        <div className="draft-option-editor">
          {entries.map((entry) => (
            <label className="draft-option-row draft-option-row--plain" key={entry.key}>
              <span className="draft-option-key">{entry.key}</span>
              <input
                className="field-input"
                value={entry.value}
                disabled={disabled}
                onChange={(e) => updateDraftOption(question, entry.key, e.target.value)}
              />
            </label>
          ))}
          <label className="draft-edit-field">
            <span>Đáp án đúng</span>
            <input
              className="field-input"
              value={question.correctAnswer || ''}
              disabled={disabled}
              onChange={(e) => updateDraft(question.id, { correctAnswer: e.target.value })}
            />
          </label>
        </div>
      );
    }

    return (
      <label className="draft-edit-field">
        <span>Đáp án đúng</span>
        <input
          className="field-input"
          value={question.correctAnswer || ''}
          disabled={disabled}
          onChange={(e) => updateDraft(question.id, { correctAnswer: e.target.value })}
        />
      </label>
    );
  };

  const selectSourceMode = (mode) => {
    if (isBusy) return;
    setSourceMode(mode);
    setError('');
    setStatusDetail('');
    setDrafts([]);
    setGenerationSummary([]);
    setEditingDraftId(null);
    setDraftEditSnapshot(null);
    setSavingDraftId(null);
    setRemovingDraftId(null);
    setSubmittingDraftId(null);
    setBulkSubmittingDrafts(false);
    setActiveJobId('');
    setGenerationInfo(null);
    resetTimings();
    if (mode === 'upload') {
      setSelectedDocumentId('');
      setDocumentId(null);
      setChunkReady(false);
    } else {
      setFile(null);
      setFileName('');
    }
  };

  const handleSelectExistingDocument = (docId) => {
    const nextDocument = documents.find((item) => item.id === docId);
    setSelectedDocumentId(docId);
    setDocumentId(docId || null);
    setChunkReady(Boolean(nextDocument && isDocumentIndexed(nextDocument)));
    setDrafts([]);
    setGenerationSummary([]);
    setEditingDraftId(null);
    setDraftEditSnapshot(null);
    setSavingDraftId(null);
    setRemovingDraftId(null);
    setSubmittingDraftId(null);
    setBulkSubmittingDrafts(false);
    setActiveJobId('');
    setGenerationInfo(null);
    resetTimings();
  };

  const runOcrPipeline = async (sourceFile, signal) => {
    setPhase('uploading');
    setStatusDetail('Đang upload tài liệu...');
    const uploadStartedAt = nowMs();
    const uploadResult = await uploadSourceDocument(sourceFile, { subjectId: selectedSubjectId || undefined });
    markTiming('uploadMs', uploadStartedAt);
    const ocrJobId = uploadResult.job_id;
    const docId = uploadResult.document_id;
    setActiveJobId(ocrJobId);
    setDocumentId(docId);

    const ocrStartedAt = nowMs();
    const ocrResult = await pollJob(getOcrStatus, ocrJobId, {
      signal,
      timeoutMs: 45 * 60 * 1000,
      onUpdate: (status) => {
        if (status.status === 'queued') setPhase('ocr_queued');
        if (status.status === 'processing') setPhase('ocr_processing');
        setStatusDetail(`Xử lý tài liệu: ${status.status}`);
      },
    });

    if (ocrResult.status === 'failed') {
      throw new Error(ocrResult.error_message || 'Xử lý tài liệu thất bại');
    }
    markTiming('ocrMs', ocrStartedAt);

    return docId;
  };

  const sleepWithSignal = (ms, signal) => new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, ms);
    if (!signal) return;
    if (signal.aborted) {
      clearTimeout(timer);
      reject(new DOMException('Theo dõi pipeline đã bị hủy', 'AbortError'));
      return;
    }
    signal.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Theo dõi pipeline đã bị hủy', 'AbortError'));
    }, { once: true });
  });

  const isTransientNetworkError = (err) => {
    const message = String(err?.message || err || '');
    return err?.name === 'TypeError'
      || err?.status === 502
      || err?.status === 503
      || err?.status === 504
      || /Failed to fetch|NetworkError|ECONNREFUSED|timeout|Không thể kết nối máy chủ/i.test(message);
  };

  const getDocumentResilient = async (docId, signal) => {
    let lastError;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      try {
        return await getDocument(docId);
      } catch (err) {
        lastError = err;
        if (!isTransientNetworkError(err) || signal?.aborted) throw err;
        setStatusDetail(`Máy chủ đang bận xử lý tài liệu, đang thử lại (${attempt + 1}/8)...`);
        await sleepWithSignal(2500 * (attempt + 1), signal);
      }
    }
    throw lastError;
  };

  const waitForDocumentReady = async (docId, signal, { allowManualChunk = false } = {}) => {
    setPhase('chunking');
    setStatusDetail('Đang chunk và lưu vector...');
    const chunkStartedAt = nowMs();
    let manualChunkStarted = false;
    const startedAt = Date.now();
    const timeoutMs = 45 * 60 * 1000;
    const manualChunkGraceMs = 12000;

    while (true) {
      if (signal?.aborted) {
        throw new DOMException('Theo dõi pipeline đã bị hủy', 'AbortError');
      }
      if (Date.now() - startedAt > timeoutMs) {
        throw new Error('Hết thời gian chờ xử lý tài liệu');
      }

      const document = await getDocumentResilient(docId, signal);
      if (isDocumentPipelineReady(document)) {
        markTiming('chunkMs', chunkStartedAt);
        setChunkReady(true);
        return;
      }
      if (isDocumentPipelineFailed(document)) {
        throw new Error('Xử lý tài liệu (chunk/index) thất bại');
      }

      const chunkStatus = String(document?.pipeline_summary?.chunk_status || 'NOT_STARTED').toUpperCase();
      const indexStatus = String(document?.pipeline_summary?.index_status || 'NOT_STARTED').toUpperCase();
      const ocrStatus = String(document?.pipeline_summary?.ocr_status || '').toUpperCase();
      const waitedLongEnough = Date.now() - startedAt >= manualChunkGraceMs;

      // Upload path: chờ auto-chain; nếu quá lâu vẫn NOT_STARTED thì kích hoạt chunk thủ công.
      if (
        (allowManualChunk || waitedLongEnough)
        && !manualChunkStarted
        && chunkStatus === 'NOT_STARTED'
        && (ocrStatus === 'COMPLETED' || ocrStatus === '')
      ) {
        setStatusDetail('Đang bắt đầu chunk và index...');
        try {
          await chunkDocument(docId);
          manualChunkStarted = true;
          continue;
        } catch (err) {
          if (!isTransientNetworkError(err)) throw err;
          setStatusDetail('Chưa gọi được chunk (máy chủ bận), sẽ thử lại...');
        }
      }

      setStatusDetail(`Đang xử lý pipeline: chunk=${chunkStatus}, index=${indexStatus}`);
      await sleepWithSignal(2000, signal);
    }
  };

  const runGenerate = async (docId, signal, pipelineStartedAt) => {
    setPhase('generate_queued');
    setStatusDetail('Đang đưa yêu cầu sinh câu hỏi vào hàng đợi...');
    const generateStartedAt = nowMs();

    const payload = buildGenerationRequest({
      documentId: docId,
      questionPlan,
      teacherInstruction,
      targetHeading,
      sourceMode,
      modelProvider: selectedModelCode,
      timings: timingRef.current,
      pipelineStartedAt,
      now: nowMs,
      cloIds: selectedCloIds,
    });

    const idempotencyKey = globalThis.crypto?.randomUUID?.()
      || `generation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const enqueueResult = await enqueueGenerateQuestions(payload, idempotencyKey);
    const genJobId = enqueueResult.job_id;
    generationJobRef.current = genJobId;
    sessionStorage.setItem('active-generation-job', JSON.stringify({
      jobId: genJobId, documentId: docId, requestedCount: totalQuestions,
    }));
    setActiveJobId(genJobId);

    const streamedResult = await watchJob(getGenerateStatus, genJobId, {
      streamStatus: streamGenerateStatus,
      signal,
      timeoutMs: 20 * 60 * 1000,
      onStreamFallback: () => {
        setStatusDetail('Mất kết nối tiến độ trực tiếp, đang chuyển sang polling...');
      },
      onUpdate: (status) => {
        if (status.status === 'queued') setPhase('generate_queued');
        if (status.status === 'processing') setPhase('generate_processing');
        const progress = status.progress;
        if (Array.isArray(progress?.data)) {
          setDrafts(mapGeneratedQuestions(progress.data));
          setDraftPage(0);
        }
        if (Array.isArray(progress?.summary)) {
          setGenerationSummary(progress.summary);
        }
        const progressLabel = progress?.total
          ? ` (${progress.completed || 0}/${progress.total})`
          : '';
        const acceptedLabel = Number.isFinite(progress?.generated_questions)
          ? ` · đã có ${progress.generated_questions} câu hợp lệ`
          : '';
        setStatusDetail(
          `Đang sinh câu hỏi${progressLabel}${acceptedLabel}`,
        );
      },
    });

    // Read the persisted result once more after the terminal event. This also
    // catches a truncated or stale stream payload before clearing the preview.
    const genResult = streamedResult.status === 'completed'
      ? await getGenerateStatus(genJobId, { signal })
      : streamedResult;

    if (genResult.status === 'failed' || genResult.status === 'cancelled') {
      throw new Error(genResult.error_message || 'Sinh câu hỏi thất bại');
    }
    if (genResult.status !== 'completed' || !Array.isArray(genResult.data) || genResult.data.length === 0) {
      throw new Error(`Job ${genJobId} báo hoàn tất nhưng không trả về câu hỏi. Vui lòng kiểm tra trạng thái job trên server.`);
    }
    markTiming('generateMs', generateStartedAt);

    setEditingDraftId(null);
    setDraftEditSnapshot(null);
    setGenerationSummary(genResult.summary || []);
    setDrafts(mapGeneratedQuestions(genResult.data || []));
    setDraftPage(0);
    setGenerationInfo({
      jobId: genJobId,
      documentId: docId,
      requestedCount: totalQuestions,
      generatedCount: (genResult.data || []).length,
      createdAt: genResult.created_at,
      updatedAt: genResult.updated_at,
      metrics: genResult.metrics,
      model: genResult.model || selectedModel || null,
    });
    setPhase('completed');
    generationJobRef.current = '';
    sessionStorage.removeItem('active-generation-job');
    const generatedCount = (genResult.data || []).length;
    setStatusDetail(
      generatedCount > 0
        ? `Đã sinh ${generatedCount}/${totalQuestions} câu hỏi`
        : `Không có câu hỏi hợp lệ (${generatedCount}/${totalQuestions})`,
    );
  };

  const runPipeline = async ({ fromGenerateOnly = false, processDocumentOnly = false } = {}) => {
    // Chặn double-submit: nếu pipeline đang khởi tạo (giữa click và cập nhật phase),
    // bỏ qua lời gọi trùng để không enqueue hai job sinh câu hỏi cùng payload.
    if (pipelineBusyRef.current) return;
    const validationError = validateForm(processDocumentOnly);
    if (validationError) {
      setError(validationError);
      return;
    }
    pipelineBusyRef.current = true;

    setError('');
    const pipelineStartedAt = nowMs();
    if (!fromGenerateOnly) {
      setDrafts([]);
      setGenerationSummary([]);
      setGenerationInfo(null);
      resetTimings();
      setEditingDraftId(null);
      setDraftEditSnapshot(null);
    } else {
      setTimingValues({
        generateMs: undefined,
        totalMs: undefined,
      });
    }

    const signal = resetPoll().signal;

    try {
      let docId = documentId;

      if (fromGenerateOnly) {
        if (!docId || !chunkReady) {
          throw new Error('Tài liệu chưa sẵn sàng để sinh câu hỏi lại');
        }
      } else if (sourceMode === 'upload') {
        docId = await runOcrPipeline(file, signal);
        // OCR hoàn tất đã kích hoạt auto chunk/index trên BE — FE chỉ chờ READY.
        await waitForDocumentReady(docId, signal, { allowManualChunk: false });
        await fetchReusableDocuments();
      } else {
        if (!selectedDocument) {
          throw new Error('Vui lòng chọn tài liệu đã xử lý');
        }
        docId = selectedDocument.id;
        setDocumentId(docId);
        if (isDocumentIndexed(selectedDocument)) {
          setChunkReady(true);
          setTimingValues({ documentMs: 'reused' });
          setStatusDetail('Sử dụng tài liệu đã xử lý và index trước đó');
        } else {
          await waitForDocumentReady(docId, signal, { allowManualChunk: true });
          await fetchReusableDocuments();
        }
      }

      if (processDocumentOnly) {
        setPhase('idle');
        setStatusDetail('Tài liệu đã được xử lý xong. Hãy cấu hình và bấm Sinh câu hỏi bằng AI.');
        return;
      }

      await runGenerate(docId, signal, pipelineStartedAt);
      markTiming('totalMs', pipelineStartedAt);
    } catch (err) {
      if (err.name === 'AbortError') return;
      setPhase('failed');
      const message = String(err.message || '');
      setError(
        /Failed to fetch|NetworkError|ECONNREFUSED/i.test(message)
          ? 'Mất kết nối máy chủ trong lúc xử lý tài liệu (OCR/chunk đang chạy nặng). Đợi backend sẵn sàng rồi bấm Thử lại.'
          : (message || 'Đã xảy ra lỗi'),
      );
      setStatusDetail('');
    } finally {
      pipelineBusyRef.current = false;
    }
  };

  const handleProcessDocument = async (e) => {
    e.preventDefault();
    await runPipeline({ processDocumentOnly: true });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    await runPipeline({ fromGenerateOnly: true });
  };

  const handleRetry = async () => {
    if (chunkReady && documentId) {
      await runPipeline({ fromGenerateOnly: true });
      return;
    }
    await runPipeline({ fromGenerateOnly: false });
  };

  const handleReset = async () => {
    const jobId = generationJobRef.current;
    if (jobId) {
      try {
        const cancelled = await cancelGenerateJob(jobId);
        if (!['cancelled', 'completed', 'failed'].includes(cancelled.status)) {
          throw new Error(`Server chưa xác nhận hủy job (trạng thái: ${cancelled.status})`);
        }
      } catch (err) {
        const reason = err.message === 'Not Found'
          ? 'Backend đang chạy phiên bản cũ, chưa có API hủy job. Cần build và khởi động lại backend cùng worker.'
          : err.message;
        setError(`Không hủy được job ${jobId}: ${reason}`);
        return;
      }
    }
    generationJobRef.current = '';
    sessionStorage.removeItem('active-generation-job');
    abortRef.current?.abort();
    setPhase('idle');
    setError('');
    setStatusDetail('');
    setSourceMode('upload');
    setFile(null);
    setFileName('');
    if (fileInputRef.current) fileInputRef.current.value = '';
    setSelectedDocumentId('');
    setSelectedSubjectId('');
    setPlanItems(createInitialPlan());
    setSelectedPresetId('');
    setPresetDialogOpen(false);
    setPresetName('');
    setPresetError('');
    setTeacherInstruction('');
    setTargetHeading('');
    setSelectedModelCode(availableModels.find((model) => model.is_default)?.code || availableModels[0]?.code || '');
    setDocumentId(null);
    setActiveJobId('');
    setGenerationInfo(null);
    resetTimings();
    setDrafts([]);
    setGenerationSummary([]);
    setEditingDraftId(null);
    setDraftEditSnapshot(null);
    setSavingDraftId(null);
    setRemovingDraftId(null);
    setSubmittingDraftId(null);
    setBulkSubmittingDrafts(false);
    setChunkReady(false);
    setDraftPage(0);
  };

  const step1Active = ['uploading', 'ocr_queued', 'ocr_processing', 'chunking'].includes(phase);
  const step2Active = ['generate_queued', 'generate_processing'].includes(phase);
  const step3Active = phase === 'completed';
  const step1Done = ['generate_queued', 'generate_processing', 'completed'].includes(phase) || chunkReady;
  const step2Done = phase === 'completed';

  return (
    <main className="generate-page">
      <section className="page-hero">
        <div className="container">
          <div className="page-hero-badge">RAG question studio</div>
          <h1 className="page-hero-title">Trình sinh câu hỏi bằng AI</h1>
          <p className="page-hero-desc">
            Chọn tài liệu, đặt ma trận Bloom và độ khó, rồi rà từng câu nháp kèm đáp án và dẫn chứng nguồn.
          </p>
          <Link to="/huong-dan" className="gen-help-link">
            <FontAwesomeIcon icon={faBookOpen} aria-hidden="true" /> Hướng dẫn sinh câu hỏi
          </Link>
        </div>
      </section>

      <section className="gen-steps">
        <div className="container gen-steps-row">
          <div className={`gen-step ${step1Active || step1Done ? 'gen-step--active' : ''}`}>
            <span className="gen-step-num">1</span>
            <span className="gen-step-label">Tải tài liệu</span>
          </div>
          <div className="gen-step-line" />
          <div className={`gen-step ${step2Active || step2Done ? 'gen-step--active' : ''}`}>
            <span className="gen-step-num">2</span>
            <span className="gen-step-label">Cấu hình sinh câu hỏi</span>
          </div>
          <div className="gen-step-line" />
          <div className={`gen-step ${step3Active ? 'gen-step--active' : ''}`}>
            <span className="gen-step-num">3</span>
            <span className="gen-step-label">Xem trước &amp; duyệt</span>
          </div>
        </div>
      </section>

      <section className="gen-body">
        <div className="container gen-grid">
          <form className="gen-form-card" onSubmit={handleSubmit}>
            <div className="gen-panel-head">
              <div>
                <span className="gen-panel-kicker">Thiết lập</span>
                <h2 className="gen-card-title">Nguồn và ma trận</h2>
              </div>
              <span className={`plan-total ${totalQuestions > MAX_TOTAL_QUESTIONS ? 'plan-total--error' : ''}`}>
                {totalQuestions}/{MAX_TOTAL_QUESTIONS}
              </span>
            </div>

            {phase !== 'idle' && phase !== 'failed' && (
              <div className={`gen-status gen-status--${generationStatusClass}`}>
                {phase !== 'completed' && <span className="gen-status-spinner" aria-hidden="true" />}
                <div className="gen-status-text">
                  <strong>{PHASE_LABELS[phase] || phase}</strong>
                  {statusDetail && <span>{statusDetail}</span>}
                  {activeJobId && (
                    <span className="job-badge">Job: {shortId(activeJobId)}</span>
                  )}
                  {phase === 'completed' && generationInfo && (
                    <span className="job-badge">
                      {generationInfo.generatedCount}/{generationInfo.requestedCount} câu
                      {generationInfo.updatedAt ? ` · ${formatDateTime(generationInfo.updatedAt)}` : ''}
                    </span>
                  )}
                  {phase === 'completed' && hasTimings && (
                    <div className="gen-timing-grid">
                      {timingItems.map((item) => (
                        <span className="gen-timing-chip" key={item.label}>
                          <b>{item.label}</b>
                          {item.value}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {error && (
              <div className="gen-error">
                <p>{error}</p>
                <div className="gen-error-actions">
                  <button type="button" className="btn btn--secondary" onClick={handleRetry}>
                    Thử lại
                  </button>
                  <button type="button" className="btn btn--ghost" onClick={handleReset}>
                    Bắt đầu lại
                  </button>
                </div>
              </div>
            )}

            <div className="field-group">
              <label className="field-label">Tài liệu nguồn</label>
              <div className="source-mode-switch">
                <button
                  type="button"
                  className={`source-mode-btn ${sourceMode === 'upload' ? 'source-mode-btn--active' : ''}`}
                  disabled={isBusy}
                  onClick={() => selectSourceMode('upload')}
                >
                  Tải tài liệu mới
                </button>
                <button
                  type="button"
                  className={`source-mode-btn ${sourceMode === 'existing' ? 'source-mode-btn--active' : ''}`}
                  disabled={isBusy}
                  onClick={() => selectSourceMode('existing')}
                >
                  Chọn tài liệu đã xử lý
                </button>
              </div>

              {sourceMode === 'upload' ? (
                <label className={`upload-drop ${isBusy ? 'upload-drop--disabled' : ''}`}>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".pdf,.doc,.docx,.md,.markdown,.txt"
                    disabled={isBusy}
                    onChange={(e) => {
                      const nextFile = e.target.files?.[0] || null;
                      setFile(nextFile);
                      setFileName(nextFile?.name || '');
                      setSelectedDocumentId('');
                      setDocumentId(null);
                      setDrafts([]);
                      setGenerationSummary([]);
                      setEditingDraftId(null);
                      setDraftEditSnapshot(null);
                      setSubmittingDraftId(null);
                      setBulkSubmittingDrafts(false);
                      setGenerationInfo(null);
                      setChunkReady(false);
                    }}
                    hidden
                  />
                  <FontAwesomeIcon icon={faUpload} className="upload-dropzone-icon" />
                  <span>{fileName || 'Kéo thả hoặc chọn PDF, DOC/DOCX, Markdown, TXT'}</span>
                  <span className="upload-hint">PDF được định tuyến theo từng trang · văn bản giữ cấu trúc nguồn</span>
                </label>
              ) : (
                <div className="existing-doc-panel">
                  <div className="existing-doc-row">
                    <select
                      className="field-select existing-doc-select"
                      value={selectedDocumentId}
                      disabled={isBusy || documentsLoading}
                      onChange={(e) => handleSelectExistingDocument(e.target.value)}
                    >
                      <option value="">
                        {documentsLoading ? 'Đang tải tài liệu...' : 'Chọn tài liệu đã xử lý'}
                      </option>
                      {reusableDocuments.map((doc) => (
                        <option
                          key={doc.id}
                          value={doc.id}
                        >
                          {reusableDocumentLabel(doc)}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="btn btn--ghost existing-doc-refresh"
                      disabled={isBusy || documentsLoading}
                      onClick={fetchReusableDocuments}
                    >
                      Tải lại
                    </button>
                  </div>
                  {documentsError && <p className="source-note source-note--error">{documentsError}</p>}
                  {!documentsLoading && reusableDocuments.length === 0 && !documentsError && (
                    <p className="source-note">Chưa có tài liệu sẵn sàng. Hãy tải tài liệu mới trước.</p>
                  )}
                  {selectedDocument && (
                    <p className="source-note">
                      {isDocumentIndexed(selectedDocument)
                        ? 'Tài liệu đã index, có thể sinh câu hỏi ngay.'
                        : 'Tài liệu đã xử lý; hệ thống sẽ chunk/index trước khi sinh câu hỏi.'}
                    </p>
                  )}
                </div>
              )}
            </div>

            <div className="field-group">
              <label className="field-label">Học phần</label>
              {sourceMode === 'upload' ? (
                <>
                  <select
                    className="field-select"
                    value={selectedSubjectId}
                    disabled={isBusy || subjectsLoading}
                    onChange={(e) => {
                      setSelectedSubjectId(e.target.value);
                      setSelectedCloIds([]);
                    }}
                  >
                    <option value="">
                      {subjectsLoading ? 'Đang tải học phần...' : 'Chọn học phần'}
                    </option>
                    {activeSubjects.map((subject) => (
                      <option key={subject.id || subject._id} value={subject.id || subject._id}>
                        {subject.subject_code} - {subject.subject_name}
                      </option>
                    ))}
                  </select>
                  {subjectsError && <p className="source-note source-note--error">{subjectsError}</p>}
                  {!subjectsLoading && !subjectsError && activeSubjects.length === 0 && (
                    <p className="source-note">
                      Chưa có học phần nào. Vào "Quản lý học phần" để tạo trước khi sinh câu hỏi.
                    </p>
                  )}
                </>
              ) : (
                <p className="source-note">
                  {selectedDocument
                    ? (selectedDocumentSubjectName || 'Tài liệu này chưa được gán học phần.')
                    : 'Chọn một tài liệu đã xử lý để xem học phần tương ứng.'}
                </p>
              )}
            </div>

            {sourceMode === 'upload' && selectedSubjectId && (
              <div className="field-group">
                <label className="field-label">Chuẩn đầu ra CLO (do GV chọn)</label>
                <p className="source-note">
                  Chọn CLO mục tiêu để sinh câu hỏi. Bỏ trống để dùng toàn bộ CLO đang hoạt động của học phần.
                </p>
                {availableClos.length === 0 ? (
                  <p className="source-note">Học phần này chưa có CLO. Hãy bổ sung trong Quản lý học phần.</p>
                ) : (
                  <div className="clo-option-list">
                    {availableClos.map((clo) => {
                      const cloId = String(clo.id || clo._id || '');
                      const typeLabel = {
                        kien_thuc: 'Kiến thức',
                        ky_nang: 'Kỹ năng',
                        thai_do: 'Thái độ',
                      }[clo.outcome_type] || 'Kiến thức';
                      return (
                        <label className="clo-option" key={cloId}>
                          <input
                            type="checkbox"
                            disabled={isBusy}
                            checked={selectedCloIds.includes(cloId)}
                            onChange={() => {
                              setSelectedCloIds((current) => (
                                current.includes(cloId)
                                  ? current.filter((id) => id !== cloId)
                                  : [...current, cloId]
                              ));
                            }}
                          />
                          <span>
                            <b>{clo.clo_code || clo.code}</b>
                            {' '}
                            <small>({typeLabel})</small>
                            {clo.description ? ` — ${clo.description}` : ''}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            <div className="field-group document-action-row">
              <button
                className="btn btn--secondary gen-submit"
                type="button"
                disabled={isBusy || chunkReady}
                onClick={handleProcessDocument}
              >
                {isBusy && !chunkReady ? (
                  <>
                    <span className="gen-status-spinner gen-status-spinner--inline" aria-hidden="true" />
                    Đang xử lý...
                  </>
                ) : (
                  'Xử lý tài liệu'
                )}
              </button>
            </div>

            <div className="field-group preset-section">
              <button
                type="button"
                className="preset-toggle"
                aria-expanded={presetSectionOpen}
                onClick={() => setPresetSectionOpen((current) => !current)}
              >
                <span className="preset-toggle-label">
                  Mẫu cấu hình sinh câu hỏi
                  <small>Lưu ma trận dạng câu hỏi, mức nhận thức, độ khó và số lượng</small>
                </span>
                {presets.length > 0 && <span className="preset-toggle-count">{presets.length}</span>}
                <FontAwesomeIcon
                  icon={faChevronDown}
                  className={`preset-toggle-chevron ${presetSectionOpen ? 'preset-toggle-chevron--open' : ''}`}
                />
              </button>

              {presetSectionOpen && (
                <div className="preset-section-body">
                  <div className="field-label-row">
                    <label className="field-label">Mẫu đã lưu</label>
                    <button
                      type="button"
                      className="preset-save-btn"
                      disabled={isBusy}
                      onClick={openPresetDialog}
                    >
                      Lưu mẫu hiện tại
                    </button>
                  </div>
                  <div className="preset-control-row">
                    <select
                      className="field-select"
                      value={selectedPresetId}
                      disabled={isBusy || presets.length === 0}
                      onChange={(e) => {
                        if (!e.target.value) {
                          setSelectedPresetId('');
                          return;
                        }
                        applyPreset(e.target.value);
                      }}
                    >
                      <option value="">
                        {presets.length ? 'Chọn mẫu cấu hình đã lưu' : 'Chưa có mẫu đã lưu'}
                      </option>
                      {presets.map((preset) => (
                        <option key={preset.id} value={preset.id}>{preset.name}</option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="preset-delete-btn"
                      disabled={isBusy || !selectedPresetId}
                      onClick={deleteSelectedPreset}
                    >
                      Xóa mẫu
                    </button>
                  </div>
                </div>
              )}
            </div>



            <div className="field-group">
              <div className="field-label-row">
                <label className="field-label">Ma trận sinh câu hỏi</label>
              </div>
              <div className="plan-builder-list">
                {planItems.map((item, index) => {
                  const count = normalizeCount(item.count);
                  return (
                    <div className="plan-builder-row" key={item.id}>
                      <div className="plan-row-header">
                        <span className="plan-row-index">Dòng {index + 1}</span>
                        <button
                          type="button"
                          className="plan-remove-btn"
                          title={planItems.length === 1 ? 'Cần giữ ít nhất một dòng' : 'Xóa dòng này'}
                          aria-label={`Xóa dòng ${index + 1}`}
                          disabled={isBusy || planItems.length === 1}
                          onClick={() => removePlanItem(item.id)}
                        >
                          <FontAwesomeIcon icon={faTrashCan} aria-hidden="true" />
                          <span>Xóa dòng</span>
                        </button>
                      </div>
                      <div className="plan-builder-fields">
                        <label className="plan-field">
                          <span>Dạng câu hỏi</span>
                          <select
                            className="field-select plan-select"
                            value={item.questionTypeId}
                            disabled={isBusy}
                            onChange={(e) => updatePlanQuestionType(item.id, e.target.value)}
                          >
                            {QUESTION_TYPES.map((type) => (
                              <option key={type.id} value={type.id}>{type.label}</option>
                            ))}
                          </select>
                        </label>
                        <label className="plan-field plan-field--wide">
                          <span>Mức nhận thức Bloom</span>
                          <select
                            className="field-select plan-select"
                            value={item.bloomId}
                            disabled={isBusy}
                            onChange={(e) => updatePlanItem(item.id, { bloomId: e.target.value })}
                          >
                            {BLOOM_LEVELS.map((bloom) => (
                              <option
                                key={bloom.id}
                                value={bloom.id}
                                disabled={!isBloomAllowedForQuestionType(item.questionTypeId, bloom.id)}
                                className={isBloomAllowedForQuestionType(item.questionTypeId, bloom.id)
                                  ? 'bloom-option--allowed'
                                  : 'bloom-option--locked'}
                              >
                                {isBloomAllowedForQuestionType(item.questionTypeId, bloom.id)
                                  ? bloom.label
                                  : `\u{1F512}\uFE0E ${bloom.label}`}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="plan-field">
                          <span>Độ khó</span>
                          <select
                            className="field-select plan-select"
                            value={item.difficulty || 'trung_binh'}
                            disabled={isBusy}
                            onChange={(e) => updatePlanItem(item.id, { difficulty: e.target.value })}
                          >
                            {DIFFICULTIES.map((difficulty) => (
                              <option key={difficulty.id} value={difficulty.backend}>
                                {difficulty.label}
                              </option>
                            ))}
                          </select>
                          {/* <small>Ước lượng độc lập với Bloom</small> */}
                        </label>
                        <label className="plan-field plan-field--count">
                          <span>Số câu</span>
                          <input
                            className="field-input plan-count-input"
                            type="number"
                            min="1"
                            max={MAX_TOTAL_QUESTIONS}
                            value={count}
                            disabled={isBusy}
                            onFocus={(e) => e.target.select()}
                            onChange={(e) => updatePlanItem(item.id, { count: normalizeCount(e.target.value) })}
                          />
                        </label>
                        <label className="plan-field">
                          <span>Nội dung</span>
                          <select
                            className="field-select plan-select"
                            value={item.contentMode || 'auto'}
                            disabled={isBusy}
                            onChange={(e) => updatePlanItem(item.id, { contentMode: e.target.value })}
                          >
                            <option value="auto">Tự nhận diện</option>
                            <option value="code">Có mã nguồn</option>
                            <option value="general">Lý thuyết</option>
                          </select>
                        </label>
                      </div>
                    </div>
                  );
                })}
              </div>
              <button
                type="button"
                className="btn btn--secondary plan-add-btn"
                disabled={isBusy || totalQuestions >= MAX_TOTAL_QUESTIONS}
                onClick={addPlanItem}
              >
                <FontAwesomeIcon icon={faPlus} aria-hidden="true" /> Thêm dòng
              </button>
            </div>

            <div className="field-group">
              <div className="gen-model-heading">
                <label className="field-label" htmlFor="generation-model">Mô hình ngôn ngữ</label>
                <button type="button" className="gen-model-refresh" onClick={fetchAvailableModels} disabled={isBusy || modelsLoading}>
                  <FontAwesomeIcon icon={faRotateRight} aria-hidden="true" /> {modelsLoading ? 'Đang tải...' : 'Tải lại'}
                </button>
              </div>
              <div className={`gen-model-summary gen-model-summary--${modelDisplay.family}`}>
                <span className="gen-model-icon"><FontAwesomeIcon icon={modelIcon} aria-hidden="true" /></span>
                <div>
                  <strong>{modelsLoading ? 'Đang tải mô hình...' : selectedModel ? modelDisplay.label : 'Mô hình mặc định'}</strong>
                  {selectedModel && modelDisplay.detail && <small>{modelDisplay.detail}</small>}
                </div>
                {selectedModel?.is_default && <span className="gen-model-default">Mặc định</span>}
              </div>
              <select
                id="generation-model"
                className="field-select"
                value={selectedModelCode}
                disabled={isBusy || modelsLoading || availableModels.length === 0}
                onChange={(event) => setSelectedModelCode(event.target.value)}
              >
                {availableModels.length === 0 && (
                  <option value="">{modelsLoading ? 'Đang tải...' : 'Mặc định hệ thống'}</option>
                )}
                {availableModels.map((model) => (
                  <option key={model.code} value={model.code}>
                    {modelPresentation(model).label}{model.is_default ? ' · Mặc định' : ''}
                  </option>
                ))}
              </select>
              <p className="source-note">
                {modelsError || selectedModel?.description || 'Giữ lựa chọn mặc định nếu bạn không chắc.'}
              </p>
            </div>

            <div className="field-group">
              <label className="field-label">Chương hoặc mục cần tập trung</label>
              <input
                className="field-input"
                value={targetHeading}
                disabled={isBusy}
                maxLength="300"
                placeholder="Ví dụ: Cây nhị phân tìm kiếm"
                onChange={(e) => setTargetHeading(e.target.value)}
              />
            </div>

            <div className="field-group">
              <label className="field-label">Yêu cầu sinh câu hỏi</label>
              <textarea
                className="field-textarea"
                rows="4"
                maxLength="1200"
                value={teacherInstruction}
                disabled={isBusy}
                placeholder="Ví dụ: Tập trung vào cây nhị phân tìm kiếm, tạo câu hỏi vận dụng, tránh câu hỏi định nghĩa."
                onChange={(e) => setTeacherInstruction(e.target.value)}
              />
            </div>

            <button
              className="btn btn--primary gen-submit"
              type="submit"
              disabled={isBusy || !chunkReady || totalQuestions > MAX_TOTAL_QUESTIONS || totalQuestions < 1}
              title={totalQuestions > MAX_TOTAL_QUESTIONS
                ? `Tổng số câu (${totalQuestions}) vượt quá giới hạn ${MAX_TOTAL_QUESTIONS}. Giảm bớt số câu ở các dòng.`
                : totalQuestions < 1
                  ? 'Cần cấu hình ít nhất 1 câu hỏi.'
                  : ''}
            >
              {isBusy && chunkReady ? (
                <>
                  <span className="gen-status-spinner gen-status-spinner--inline" aria-hidden="true" />
                  Đang sinh câu hỏi...
                </>
              ) : (
                <>
                  <FontAwesomeIcon icon={faLayerGroup} />
                  Sinh câu hỏi bằng AI
                </>
              )}
            </button>
            {isBusy && generationJobRef.current && (
              <button className="btn btn--ghost" type="button" onClick={handleReset}>
                Dừng sinh câu hỏi
              </button>
            )}
            <p className="gen-form-note">
              Toàn bộ câu hỏi sinh ra sẽ ở trạng thái nháp để giảng viên rà soát trước khi gửi kiểm duyệt.
            </p>
          </form>

          <div className="gen-preview-card">
            <div className="gen-card-title-row">
              <div>
                <span className="gen-panel-kicker">Kết quả</span>
                <h2 className="gen-card-title">Bảng nháp câu hỏi</h2>
              </div>
              <div className="gen-preview-actions">
                <Link
                  to={bankLinkQuestionId ? `/quan-ly?questionId=${bankLinkQuestionId}` : '/quan-ly'}
                  className="gen-bank-link"
                >
                  Quản lý câu hỏi <FontAwesomeIcon icon={faArrowRight} aria-hidden="true" />
                </Link>
                <span className="gen-preview-count">{drafts.length} câu hỏi</span>
                {submittableDraftCount > 0 && (
                  <button
                    type="button"
                    className="mini-submit-btn"
                    disabled={isBusy || bulkSubmittingDrafts || Boolean(editingDraftId)}
                    onClick={handleSubmitAllDraftsForReview}
                  >
                    {bulkSubmittingDrafts ? 'Đang gửi...' : `Gửi ${submittableDraftCount} câu`}
                  </button>
                )}
                {phase === 'completed' && generationInfo && submittableDraftCount === 0
                  && !editingDraftId && !savingDraftId && !removingDraftId
                  && !submittingDraftId && !bulkSubmittingDrafts && (
                    <button type="button" className="icon-btn" onClick={handleReset}>
                      Làm mới
                    </button>
                  )}
              </div>
            </div>

            {generationSummary.length > 0 && (
              <div className={`gen-summary-list ${generationShortfalls.length ? 'gen-summary-list--warning' : ''}`}>
                {generationSummary.map((item) => (
                  <div className="gen-summary-item" key={`${item.plan_index}-${item.question_type}-${item.bloom_level}-${item.difficulty || 'auto'}`}>
                    <strong>Dòng {item.plan_index}</strong>
                    <span>
                      {questionTypeLabel(item.question_type)} · {bloomLevelLabel(item.bloom_level)}
                      {item.difficulty ? ` · ${difficultyLabel(item.difficulty)}` : ''}
                      {` · ${item.saved_count}/${item.requested_count}`}
                    </span>
                    {(
                      item.format_rejected_count > 0
                      || item.grounding_rejected_count > 0
                      || item.clarity_rejected_count > 0
                      || item.exact_duplicate_count > 0
                      || item.near_duplicate_count > 0
                    ) && (
                      <small>
                        {[
                          item.format_rejected_count > 0 && `Format: ${item.format_rejected_count}`,
                          item.grounding_rejected_count > 0 && `Nguồn/keyword: ${item.grounding_rejected_count}`,
                          item.clarity_rejected_count > 0 && `Diễn đạt: ${item.clarity_rejected_count}`,
                          item.exact_duplicate_count > 0 && `Trùng: ${item.exact_duplicate_count}`,
                          item.near_duplicate_count > 0 && `Gần trùng: ${item.near_duplicate_count}`,
                        ].filter(Boolean).join(' · ')}
                      </small>
                    )}
                    {(item.warnings || []).slice(0, 4).map((warning) => (
                      <small key={warning}>{warning}</small>
                    ))}
                    {(item.rejection_reasons || []).slice(0, 3).map((reason, index) => (
                      <small key={`${reason.code || 'reason'}-${index}`}>
                        {reason.candidate_index ? `Câu ${reason.candidate_index}: ` : ''}
                        {reason.message}
                      </small>
                    ))}
                  </div>
                ))}
              </div>
            )}

            {drafts.length === 0 ? (
              <div className={`gen-preview-empty ${generationAcceptedNothing ? 'gen-preview-empty--warning' : ''}`}>
                <p>{generationAcceptedNothing ? 'Không có câu hỏi hợp lệ.' : 'Chưa có câu hỏi nháp.'}</p>
                <span>
                  {generationAcceptedNothing
                    ? 'Các ứng viên AI sinh ra đã bị loại ở bước sàng lọc sau sinh. Xem lý do ở phần cảnh báo phía trên.'
                    : isBusy
                      ? 'Đang chờ câu hỏi hợp lệ đầu tiên...'
                    : 'Tải tài liệu, cấu hình và bấm sinh câu hỏi để xem kết quả tại đây.'}
                </span>
              </div>
            ) : (
              <div className="draft-list">
                {visibleDrafts.map((question) => {
                  const isEditing = editingDraftId === question.id;
                  const isSaving = savingDraftId === question.id;
                  const isRemoving = removingDraftId === question.id;
                  const isSubmitting = submittingDraftId === question.id;
                  const actionBusy = Boolean(
                    isBusy
                    || savingDraftId
                    || removingDraftId
                    || editingDraftId
                    || submittingDraftId
                    || bulkSubmittingDrafts,
                  );
                  const reviewStatus = String(question.reviewStatus || 'DRAFT').toUpperCase();
                  const reviewStatusLabel = REVIEW_STATUS_LABEL[reviewStatus] || reviewStatus;

                  return (
                    <article className={`draft-item ${isEditing ? 'draft-item--editing' : ''}`} key={question.id}>
                      <div className="draft-item-meta">
                        <span className="q-tag">{question.type}</span>
                        <span className="bloom-tag">{question.bloom}</span>
                        {question.difficultyLabel ? (
                          <span className={`difficulty-tag difficulty-tag--${question.difficulty}`}>
                            {question.difficultyLabel}
                          </span>
                        ) : null}
                        <span className="draft-status" title={question.questionCode}>
                          {shortCode(question.questionCode)} · Phiên bản {question.currentVersion || 1} · {reviewStatusLabel}
                        </span>
                        {question.validationWarnings?.length ? (
                          <span className="draft-warning-pill">Cần xem lại</span>
                        ) : null}
                      </div>

                      {isEditing ? (
                        <div className="draft-edit-form">
                          <label className="draft-edit-field">
                            <span>Nội dung câu hỏi</span>
                            <textarea
                              className="field-textarea draft-edit-textarea"
                              rows="4"
                              value={question.text}
                              disabled={isSaving || isRemoving}
                              onChange={(e) => updateDraft(question.id, { text: e.target.value })}
                            />
                          </label>
                          <div className="draft-edit-field">
                            <span>Đáp án</span>
                            {renderDraftAnswerEditor(question, isSaving || isRemoving)}
                          </div>
                          <label className="draft-edit-field">
                            <span>Giải thích</span>
                            <textarea
                              className="field-textarea draft-edit-textarea"
                              rows="3"
                              value={question.explanation || ''}
                              disabled={isSaving || isRemoving}
                              onChange={(e) => updateDraft(question.id, { explanation: e.target.value })}
                            />
                          </label>
                        </div>
                      ) : (
                        <div className="draft-item-body">
                          <section className="draft-section">
                            <h4 className="draft-section-label">Nội dung</h4>
                            <QuestionContent text={question.text} />
                          </section>

                          {question.validationWarnings?.length ? (
                            <section className="draft-section draft-section--warning">
                              <h4 className="draft-section-label">Cảnh báo</h4>
                              <ul className="draft-warning-list">
                                {question.validationWarnings.map((warning) => (
                                  <li key={warning}>{warning}</li>
                                ))}
                              </ul>
                            </section>
                          ) : null}

                          <section className="draft-section">
                            <h4 className="draft-section-label">Đáp án</h4>
                            <div className="draft-item-choices">
                              {question.choices.map((choice) => (
                                <span
                                  key={choice.text}
                                  className={`choice ${choice.isCorrect ? 'choice--correct' : ''}`}
                                >
                                  {choice.text}
                                </span>
                              ))}
                            </div>
                          </section>

                          {question.explanation && (
                            <section className="draft-section">
                              <h4 className="draft-section-label">Giải thích</h4>
                              <p className="draft-item-explanation">{question.explanation}</p>
                            </section>
                          )}

                          {question.sourceContext && (
                            <section className="draft-section draft-section--evidence">
                              <h4 className="draft-section-label">Dẫn chứng</h4>
                              <blockquote className="draft-item-evidence">
                                {question.sourceContext}
                              </blockquote>
                            </section>
                          )}
                        </div>
                      )}

                      <div className="draft-item-actions">
                        {isEditing ? (
                          <>
                            <button
                              type="button"
                              className="icon-btn icon-btn--approve"
                              disabled={isSaving || isRemoving}
                              onClick={() => handleSaveDraft(question)}
                            >
                              <FontAwesomeIcon icon={faFloppyDisk} aria-hidden="true" /> {isSaving ? 'Đang lưu...' : 'Lưu chỉnh sửa'}
                            </button>
                            <button
                              type="button"
                              className="icon-btn"
                              disabled={isSaving || isRemoving}
                              onClick={cancelEditDraft}
                            >
                              <FontAwesomeIcon icon={faXmark} aria-hidden="true" /> Hủy
                            </button>
                          </>
                        ) : (
                          <>
                            {canSubmitDraft(question) && (
                              <button
                                type="button"
                                className="icon-btn icon-btn--approve"
                                disabled={actionBusy}
                                onClick={() => handleSubmitDraftForReview(question)}
                              >
                                <FontAwesomeIcon icon={faPaperPlane} aria-hidden="true" /> {isSubmitting ? 'Đang gửi...' : 'Gửi duyệt'}
                              </button>
                            )}
                            <button
                              type="button"
                              className="icon-btn"
                              disabled={actionBusy}
                              onClick={() => startEditDraft(question)}
                            >
                              <FontAwesomeIcon icon={faPen} aria-hidden="true" /> Sửa
                            </button>
                            <button
                              type="button"
                              className="icon-btn icon-btn--reject"
                              disabled={actionBusy}
                              onClick={() => handleRemoveDraft(question)}
                            >
                              <FontAwesomeIcon icon={faTrashCan} aria-hidden="true" /> {isRemoving ? 'Đang bỏ...' : 'Bỏ câu'}
                            </button>
                          </>
                        )}
                      </div>
                    </article>
                  );
                })}
              </div>
            )}

            {draftPageCount > 1 && (
              <div className="draft-pagination">
                <button
                  type="button"
                  className="draft-pagination-btn"
                  disabled={safeDraftPage === 0}
                  onClick={() => setDraftPage((current) => Math.max(0, current - 1))}
                >
                  ‹ Trước
                </button>
                <div className="draft-pagination-pages">
                  {Array.from({ length: draftPageCount }, (_, index) => (
                    <button
                      type="button"
                      key={index}
                      className={`draft-pagination-page ${index === safeDraftPage ? 'draft-pagination-page--active' : ''}`}
                      onClick={() => setDraftPage(index)}
                    >
                      {index + 1}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="draft-pagination-btn"
                  disabled={safeDraftPage >= draftPageCount - 1}
                  onClick={() => setDraftPage((current) => Math.min(draftPageCount - 1, current + 1))}
                >
                  Sau ›
                </button>
              </div>
            )}
          </div>
        </div>
      </section>

      {presetDialogOpen && (
        <div className="preset-dialog-backdrop" onClick={closePresetDialog}>
          <form className="preset-dialog" onSubmit={savePreset} onClick={(e) => e.stopPropagation()}>
            <div>
              <h3>Lưu mẫu cấu hình sinh câu hỏi</h3>
              <p>Mẫu sẽ lưu ma trận câu hỏi và yêu cầu sinh câu hỏi hiện tại.</p>
            </div>

            <label className="draft-edit-field">
              <span>Tên mẫu</span>
              <input
                className="field-input"
                value={presetName}
                autoFocus
                maxLength="80"
                placeholder="Ví dụ: Ôn tập cây nhị phân - 7 câu"
                onChange={(e) => {
                  setPresetName(e.target.value);
                  setPresetError('');
                }}
              />
            </label>

            <div className="preset-dialog-summary">
              <div>
                <strong>{totalQuestions}</strong>
                <span>Tổng câu hỏi</span>
              </div>
              <div>
                <strong>{planItems.length}</strong>
                <span>Dòng cấu hình</span>
              </div>
            </div>

            <div className="preset-plan-preview">
              {planItems.map((item, index) => {
                const type = QUESTION_TYPES.find((entry) => entry.id === item.questionTypeId);
                const bloom = BLOOM_LEVELS.find((entry) => entry.id === item.bloomId);
                const difficulty = difficultyLabel(item.difficulty || 'trung_binh');
                return (
                  <div key={item.id}>
                    <b>Dòng {index + 1}</b>
                    <span>
                      {type?.label || item.questionTypeId} · {bloom?.label || item.bloomId} · {difficulty} · {normalizeCount(item.count)} câu
                    </span>
                  </div>
                );
              })}
            </div>

            <div className="preset-dialog-context">
              <span><b>Chương/mục:</b> {targetHeading.trim() || 'Toàn bộ tài liệu'}</span>
              <span><b>Yêu cầu sinh câu hỏi:</b> {teacherInstruction.trim() || 'Chưa nhập'}</span>
            </div>

            {presetError && <p className="preset-dialog-error">{presetError}</p>}

            <div className="preset-dialog-actions">
              <button type="button" className="btn btn--ghost" onClick={closePresetDialog}>
                Hủy
              </button>
              <button type="submit" className="btn btn--primary">
                Lưu mẫu
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}

export default GeneratePage;
