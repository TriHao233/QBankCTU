import React, { useEffect, useMemo, useState } from 'react';
import {
  activateEvaluationPolicy,
  activatePromptTemplate,
  addSubjectChapter,
  addSubjectLearningOutcome,
  checkAiModelHealth,
  getCatalogOverview,
  saveAiModel,
  saveEvaluationPolicy,
  savePromptTemplate,
  saveSubject,
  setAiModelActive,
  testPromptTemplate,
  updateSubject,
  updateSubjectChapter,
  updateSubjectLearningOutcome,
} from '../api/catalog';
import '../css/CatalogAdminPage.css';

const DEFAULT_WEIGHTS = {
  faithfulness: 0.35,
  contextual_relevancy: 0.20,
  answer_relevancy: 0.15,
  bloom_alignment: 0.15,
  clo_alignment: 0.15,
};

const DEFAULT_THRESHOLDS = { yellow_min: 0.5, green_min: 0.75, pass_min: 0.65 };
const EMPTY_SUBJECT_FORM = { id: '', subject_code: '', subject_name: '', description: '', is_active: true };
const EMPTY_CHAPTER_FORM = { id: '', chapter_code: '', chapter_name: '', sequence_no: 1, is_active: true };
const EMPTY_CLO_FORM = { id: '', clo_code: '', description: '', target_weight: 1, is_active: true };

function compactJson(value) {
  return JSON.stringify(value, null, 2);
}

function usageText(counts = {}) {
  const parts = [
    counts.documents ? `${counts.documents} tài liệu` : '',
    counts.questions ? `${counts.questions} câu hỏi` : '',
    counts.exams ? `${counts.exams} đề` : '',
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : 'Chưa được dùng';
}

function childId(item) {
  return item?.id || item?._id || '';
}

function factoryText(model) {
  const status = model?.factory_status;
  if (!status) return 'Chưa kiểm tra kết nối';
  if (!status.supported) return status.error || 'Chưa hỗ trợ mô hình này';
  const runtime = status.runtime || {};
  return `${runtime.provider_class || 'Provider'}${runtime.model_name ? ` · ${runtime.model_name}` : ''}`;
}

function healthText(model) {
  const health = model?.last_health_check;
  if (!health) return 'Chưa kiểm tra kết nối';
  return health.status === 'OK'
    ? `OK · ${health.latency_ms || 0}ms`
    : `${health.status || 'FAILED'}${health.error ? ` · ${health.error}` : ''}`;
}

function subjectToForm(subject) {
  if (!subject) return EMPTY_SUBJECT_FORM;
  return {
    id: subject.id,
    subject_code: subject.subject_code || '',
    subject_name: subject.subject_name || '',
    description: subject.description || '',
    is_active: subject.is_active !== false,
  };
}

function chapterToForm(chapter) {
  return {
    id: childId(chapter),
    chapter_code: chapter.chapter_code || '',
    chapter_name: chapter.chapter_name || '',
    sequence_no: chapter.sequence_no || 1,
    is_active: chapter.is_active !== false,
  };
}

function cloToForm(clo) {
  return {
    id: childId(clo),
    clo_code: clo.clo_code || '',
    description: clo.description || '',
    target_weight: clo.target_weight ?? 1,
    is_active: clo.is_active !== false,
  };
}

function CatalogAdminPage() {
  const [catalog, setCatalog] = useState({
    subjects: [],
    ai_models: [],
    prompt_templates: [],
    evaluation_policies: [],
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [activeSubjectId, setActiveSubjectId] = useState('');
  const [subjectForm, setSubjectForm] = useState(EMPTY_SUBJECT_FORM);
  const [chapterForm, setChapterForm] = useState(EMPTY_CHAPTER_FORM);
  const [cloForm, setCloForm] = useState(EMPTY_CLO_FORM);
  const [modelForm, setModelForm] = useState({
    model_code: 'qwen3-8b',
    model_name: 'qwen3:8b',
    display_name: 'Qwen3 (8B)',
    description: 'Nhanh và phù hợp để sinh câu hỏi.',
    runtime: 'OLLAMA',
    kind: 'CHAT',
    revision: 'local',
    capabilities: 'QUESTION_GENERATION,QUESTION_EVALUATION',
    priority: 10,
    is_active: true,
    config: {
      endpoint: '',
      timeout_seconds: 300,
      temperature: 0,
      num_predict: 900,
      max_output_tokens: 2048,
    },
  });
  const [selectedPromptKey, setSelectedPromptKey] = useState('');
  const [promptForm, setPromptForm] = useState({ template_key: '', kind: 'QUESTION_TYPE', name: '', prompt_body: '' });
  const [policyForm, setPolicyForm] = useState({
    policy_name: 'Default question quality policy',
    weights: compactJson(DEFAULT_WEIGHTS),
    thresholds: compactJson(DEFAULT_THRESHOLDS),
  });
  const [healthCheckingCode, setHealthCheckingCode] = useState('');
  const [modelHealth, setModelHealth] = useState(null);
  const [promptTesting, setPromptTesting] = useState(false);
  const [promptPreview, setPromptPreview] = useState(null);
  const [saving, setSaving] = useState(false);

  const activeSubject = useMemo(
    () => catalog.subjects.find((subject) => subject.id === activeSubjectId) || catalog.subjects[0],
    [catalog.subjects, activeSubjectId],
  );
  const runtimeConfig = catalog.runtime_config || {};

  const promptOptions = useMemo(() => {
    const latestByKey = new Map();
    catalog.prompt_templates.forEach((template) => {
      const current = latestByKey.get(template.template_key);
      if (!current || template.version > current.version) latestByKey.set(template.template_key, template);
    });
    return Array.from(latestByKey.values());
  }, [catalog.prompt_templates]);

  const loadCatalog = async (preferredSubjectId = activeSubjectId) => {
    setLoading(true);
    setError('');
    try {
      const result = await getCatalogOverview();
      setCatalog(result);
      const nextActive = result.subjects?.find((subject) => subject.id === preferredSubjectId) || result.subjects?.[0];
      if (nextActive) {
        setActiveSubjectId(nextActive.id);
        setSubjectForm((current) => (
          current.id || (!current.subject_code && !current.subject_name)
            ? subjectToForm(nextActive)
            : current
        ));
      }
    } catch (err) {
      setError(err.message || 'Không tải được dữ liệu nền');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadCatalog();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const runSave = async (action) => {
    setSaving(true);
    setError('');
    try {
      const result = await action();
      await loadCatalog(result?.activeSubjectId);
    } catch (err) {
      setError(err.message || 'Lưu dữ liệu nền thất bại');
    } finally {
      setSaving(false);
    }
  };

  const handleSelectSubject = (subject) => {
    setActiveSubjectId(subject.id);
    setSubjectForm(subjectToForm(subject));
    setChapterForm(EMPTY_CHAPTER_FORM);
    setCloForm(EMPTY_CLO_FORM);
  };

  const handleNewSubject = () => {
    setSubjectForm(EMPTY_SUBJECT_FORM);
    setChapterForm(EMPTY_CHAPTER_FORM);
    setCloForm(EMPTY_CLO_FORM);
  };

  const handleSaveSubject = (event) => {
    event.preventDefault();
    runSave(async () => {
      const { id, ...payload } = subjectForm;
      const saved = id
        ? await updateSubject(id, payload)
        : await saveSubject(payload);
      setActiveSubjectId(saved.id);
      setSubjectForm(subjectToForm(saved));
      return { activeSubjectId: saved.id };
    });
  };

  const handleSaveChapter = (event) => {
    event.preventDefault();
    if (!activeSubject) return;
    runSave(async () => {
      const { id, ...payload } = chapterForm;
      const normalized = {
        ...payload,
        sequence_no: Number(chapterForm.sequence_no) || 1,
      };
      if (id) {
        await updateSubjectChapter(activeSubject.id, id, normalized);
      } else {
        await addSubjectChapter(activeSubject.id, normalized);
      }
      setChapterForm(EMPTY_CHAPTER_FORM);
    });
  };

  const handleSaveClo = (event) => {
    event.preventDefault();
    if (!activeSubject) return;
    runSave(async () => {
      const { id, ...payload } = cloForm;
      const normalized = {
        ...payload,
        target_weight: Number(cloForm.target_weight) || 0,
      };
      if (id) {
        await updateSubjectLearningOutcome(activeSubject.id, id, normalized);
      } else {
        await addSubjectLearningOutcome(activeSubject.id, normalized);
      }
      setCloForm(EMPTY_CLO_FORM);
    });
  };

  const toggleSubjectActive = () => {
    if (!activeSubject) return;
    runSave(async () => {
      const updated = await updateSubject(activeSubject.id, { is_active: !activeSubject.is_active });
      setSubjectForm(subjectToForm(updated));
    });
  };

  const toggleChapterActive = (chapter) => {
    if (!activeSubject) return;
    runSave(async () => updateSubjectChapter(
      activeSubject.id,
      childId(chapter),
      { is_active: chapter.is_active === false },
    ));
  };

  const toggleCloActive = (clo) => {
    if (!activeSubject) return;
    runSave(async () => updateSubjectLearningOutcome(
      activeSubject.id,
      childId(clo),
      { is_active: clo.is_active === false },
    ));
  };

  const handleSaveModel = (event) => {
    event.preventDefault();
    const config = {
      timeout_seconds: Number(modelForm.config.timeout_seconds) || 300,
      temperature: Number(modelForm.config.temperature) || 0,
      ...(modelForm.runtime === 'GEMINI'
        ? { max_output_tokens: Number(modelForm.config.max_output_tokens) || 2048 }
        : { num_predict: Number(modelForm.config.num_predict) || 900 }),
      ...(modelForm.runtime === 'OLLAMA' && modelForm.config.endpoint.trim()
        ? { endpoint: modelForm.config.endpoint.trim() }
        : {}),
    };
    runSave(async () => saveAiModel({
      ...modelForm,
      capabilities: modelForm.capabilities.split(',').map((item) => item.trim()).filter(Boolean),
      priority: Number(modelForm.priority) || 0,
      config,
      is_local: modelForm.runtime === 'OLLAMA',
      is_active: modelForm.is_active,
    }));
  };

  const handleSelectModel = (model) => {
    setModelForm({
      model_code: model.model_code || '',
      model_name: model.model_name || '',
      display_name: model.display_name || model.model_name || '',
      description: model.description || '',
      runtime: model.runtime || 'OLLAMA',
      kind: model.kind || 'CHAT',
      revision: model.revision || 'local',
      capabilities: (model.capabilities || []).join(','),
      priority: model.priority ?? 10,
      is_active: model.is_active !== false,
      config: {
        endpoint: model.config?.endpoint || '',
        timeout_seconds: model.config?.timeout_seconds ?? 300,
        temperature: model.config?.temperature ?? 0,
        num_predict: model.config?.num_predict ?? 900,
        max_output_tokens: model.config?.max_output_tokens ?? 2048,
      },
    });
  };

  const toggleModelCapability = (capability) => {
    const values = modelForm.capabilities.split(',').map((item) => item.trim()).filter(Boolean);
    const next = values.includes(capability)
      ? values.filter((item) => item !== capability)
      : [...values, capability];
    setModelForm({ ...modelForm, capabilities: next.join(',') });
  };

  const toggleModelActive = (model) => {
    runSave(async () => setAiModelActive({
      model_code: model.model_code,
      is_active: model.is_active === false,
    }));
  };

  const handleCheckModelHealth = async (model) => {
    setHealthCheckingCode(model.model_code);
    setError('');
    try {
      const result = await checkAiModelHealth({
        model_code: model.model_code,
        timeout_seconds: 10,
      });
      setModelHealth(result);
      await loadCatalog();
    } catch (err) {
      setError(err.message || 'Kiểm tra kết nối mô hình thất bại');
    } finally {
      setHealthCheckingCode('');
    }
  };

  const handleSelectPrompt = (templateKey) => {
    setSelectedPromptKey(templateKey);
    const selected = promptOptions.find((template) => template.template_key === templateKey);
    if (selected) {
      setPromptForm({
        template_key: selected.template_key,
        kind: selected.kind,
        name: selected.name,
        prompt_body: selected.prompt_body,
      });
    }
  };

  const handleEditPromptVersion = (template) => {
    setSelectedPromptKey(template.template_key);
    setPromptForm({
      template_key: template.template_key,
      kind: template.kind,
      name: template.name,
      prompt_body: template.prompt_body,
    });
  };

  const handleSavePrompt = (event) => {
    event.preventDefault();
    runSave(async () => savePromptTemplate({ ...promptForm, create_new_version: true, is_active: true }));
  };

  const handleActivatePrompt = (template, isActive = true) => {
    runSave(async () => activatePromptTemplate({
      template_key: template.template_key,
      version: template.version,
      is_active: isActive,
    }));
  };

  const handleTestPrompt = async () => {
    setPromptTesting(true);
    setError('');
    try {
      const result = await testPromptTemplate({});
      setPromptPreview(result);
    } catch (err) {
      setError(err.message || 'Không build được prompt mẫu');
    } finally {
      setPromptTesting(false);
    }
  };

  const handleSavePolicy = (event) => {
    event.preventDefault();
    runSave(async () => saveEvaluationPolicy({
      policy_name: policyForm.policy_name,
      weights: JSON.parse(policyForm.weights),
      thresholds: JSON.parse(policyForm.thresholds),
      create_new_version: true,
      is_active: true,
    }));
  };

  const handleEditPolicy = (policy) => {
    setPolicyForm({
      policy_name: policy.policy_name,
      weights: compactJson(policy.weights || DEFAULT_WEIGHTS),
      thresholds: compactJson(policy.thresholds || DEFAULT_THRESHOLDS),
    });
  };

  const handleActivatePolicy = (policy, isActive = true) => {
    runSave(async () => activateEvaluationPolicy({
      policy_name: policy.policy_name,
      version: policy.version,
      is_active: isActive,
    }));
  };

  return (
    <main className="catalog-page">
      <section className="catalog-header">
        <div>
          <span>Khu vực quản trị</span>
          <h1>Cấu hình dữ liệu nền</h1>
        </div>
        <button type="button" onClick={() => loadCatalog()} disabled={loading || saving}>Làm mới</button>
      </section>
      {error && <p className="catalog-error">{error}</p>}
      {loading ? (
        <p className="catalog-empty">Đang tải dữ liệu nền...</p>
      ) : (
        <section className="catalog-grid">
          <div className="catalog-card catalog-card--wide catalog-runtime-panel">
            <div className="catalog-card-title-row">
              <h2>Cấu hình AI đang chạy</h2>
              <span className={`catalog-status ${runtimeConfig.prompt_source === 'db' ? 'ok' : 'warn'}`}>
                Nguồn prompt: {runtimeConfig.prompt_source === 'db' ? 'cơ sở dữ liệu' : 'tệp mặc định'}
              </span>
            </div>
            <div className="runtime-grid">
              <div>
                <small>Sinh câu hỏi</small>
                <b>{runtimeConfig.generation_model_provider || '--'}</b>
                <span>{runtimeConfig.generation_factory?.runtime?.provider_class || factoryText(runtimeConfig.generation_catalog_model)}</span>
              </div>
              <div>
                <small>Đánh giá AI</small>
                <b>{runtimeConfig.evaluation_model_provider || '--'}</b>
                <span>{runtimeConfig.evaluation_factory?.runtime?.provider_class || factoryText(runtimeConfig.evaluation_catalog_model)}</span>
              </div>
              <div>
                <small>Prompt đang dùng trong CSDL</small>
                <b>{runtimeConfig.active_prompt_count ?? 0}</b>
                <span>{runtimeConfig.prompt_source === 'db' ? 'Đang được áp dụng' : 'Chưa áp dụng (đang dùng tệp mặc định)'}</span>
              </div>
              <div>
                <small>Bộ tiêu chí đang áp dụng</small>
                <b>{runtimeConfig.active_evaluation_policy?.policy_name || '--'}</b>
                <span>Phiên bản {runtimeConfig.active_evaluation_policy?.version || 1}</span>
              </div>
            </div>
            {(runtimeConfig.warnings || []).length > 0 && (
              <div className="catalog-warning-list">
                {runtimeConfig.warnings.map((warning) => (
                  <span key={warning}>{warning}</span>
                ))}
              </div>
            )}
          </div>

          <div className="catalog-card catalog-card--wide">
            <div className="catalog-card-title-row">
              <h2>Môn học / Chương / CLO</h2>
              <button type="button" className="catalog-ghost-button" onClick={handleNewSubject} disabled={saving}>
                Môn mới
              </button>
            </div>
            <form className="catalog-form catalog-form--inline" onSubmit={handleSaveSubject}>
              <label className="catalog-model-field">
                <span>Mã môn</span>
                <input placeholder="Ví dụ: CT177" value={subjectForm.subject_code} onChange={(e) => setSubjectForm({ ...subjectForm, subject_code: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Tên môn</span>
                <input placeholder="Ví dụ: Cấu trúc dữ liệu" value={subjectForm.subject_name} onChange={(e) => setSubjectForm({ ...subjectForm, subject_name: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Mô tả</span>
                <input placeholder="Nội dung chính của học phần" value={subjectForm.description} onChange={(e) => setSubjectForm({ ...subjectForm, description: e.target.value })} />
              </label>
              <label className="catalog-check">
                <input type="checkbox" checked={subjectForm.is_active} onChange={(e) => setSubjectForm({ ...subjectForm, is_active: e.target.checked })} />
                Đang dùng
              </label>
              <button type="submit" disabled={saving}>{subjectForm.id ? 'Lưu môn' : 'Tạo môn'}</button>
            </form>
            <div className="subject-layout">
              <div className="subject-list">
                {catalog.subjects.map((subject) => (
                  <button
                    type="button"
                    className={`${activeSubject?.id === subject.id ? 'active' : ''} ${subject.is_active === false ? 'inactive' : ''}`}
                    key={subject.id}
                    onClick={() => handleSelectSubject(subject)}
                  >
                    <b>{subject.subject_code}</b>
                    <span>{subject.subject_name}</span>
                    <small>{usageText(subject.usage_counts)}</small>
                  </button>
                ))}
              </div>
              <div className="subject-detail">
                <div className="catalog-detail-head">
                  <div>
                    <h3>{activeSubject?.subject_name || 'Chưa có môn'}</h3>
                    {activeSubject && <p>{usageText(activeSubject.usage_counts)}</p>}
                  </div>
                  {activeSubject && (
                    <button type="button" className="catalog-ghost-button" onClick={toggleSubjectActive} disabled={saving}>
                      {activeSubject.is_active === false ? 'Kích hoạt' : 'Tạm khóa'}
                    </button>
                  )}
                </div>
                {activeSubject ? (
                  <>
                    <div className="catalog-columns">
                      <form className="catalog-form" onSubmit={handleSaveChapter}>
                        <h4>{chapterForm.id ? 'Sửa chương' : 'Thêm chương'}</h4>
                        <label className="catalog-model-field">
                          <span>Mã chương</span>
                          <input placeholder="Ví dụ: C1" value={chapterForm.chapter_code} onChange={(e) => setChapterForm({ ...chapterForm, chapter_code: e.target.value })} />
                        </label>
                        <label className="catalog-model-field">
                          <span>Tên chương</span>
                          <input placeholder="Ví dụ: Danh sách liên kết" value={chapterForm.chapter_name} onChange={(e) => setChapterForm({ ...chapterForm, chapter_name: e.target.value })} />
                        </label>
                        <label className="catalog-model-field">
                          <span>Thứ tự chương</span>
                          <input type="number" min="1" value={chapterForm.sequence_no} onChange={(e) => setChapterForm({ ...chapterForm, sequence_no: e.target.value })} />
                        </label>
                        <label className="catalog-check">
                          <input type="checkbox" checked={chapterForm.is_active} onChange={(e) => setChapterForm({ ...chapterForm, is_active: e.target.checked })} />
                          Đang dùng
                        </label>
                        <div className="catalog-form-actions">
                          {chapterForm.id && (
                            <button type="button" className="catalog-ghost-button" onClick={() => setChapterForm(EMPTY_CHAPTER_FORM)} disabled={saving}>
                              Hủy
                            </button>
                          )}
                          <button type="submit" disabled={!activeSubject || saving}>{chapterForm.id ? 'Lưu chương' : 'Thêm chương'}</button>
                        </div>
                      </form>
                      <form className="catalog-form" onSubmit={handleSaveClo}>
                        <h4>{cloForm.id ? 'Sửa CLO' : 'Thêm CLO'}</h4>
                        <label className="catalog-model-field">
                          <span>Mã CLO</span>
                          <input placeholder="Ví dụ: CLO1" value={cloForm.clo_code} onChange={(e) => setCloForm({ ...cloForm, clo_code: e.target.value })} />
                        </label>
                        <label className="catalog-model-field">
                          <span>Mô tả chuẩn đầu ra</span>
                          <input placeholder="Sinh viên có khả năng..." value={cloForm.description} onChange={(e) => setCloForm({ ...cloForm, description: e.target.value })} />
                        </label>
                        <label className="catalog-model-field">
                          <span>Trọng số (0–1)</span>
                          <input type="number" min="0" max="1" step="0.05" value={cloForm.target_weight} onChange={(e) => setCloForm({ ...cloForm, target_weight: e.target.value })} />
                        </label>
                        <label className="catalog-check">
                          <input type="checkbox" checked={cloForm.is_active} onChange={(e) => setCloForm({ ...cloForm, is_active: e.target.checked })} />
                          Đang dùng
                        </label>
                        <div className="catalog-form-actions">
                          {cloForm.id && (
                            <button type="button" className="catalog-ghost-button" onClick={() => setCloForm(EMPTY_CLO_FORM)} disabled={saving}>
                              Hủy
                            </button>
                          )}
                          <button type="submit" disabled={!activeSubject || saving}>{cloForm.id ? 'Lưu CLO' : 'Thêm CLO'}</button>
                        </div>
                      </form>
                    </div>
                    <div className="catalog-list">
                      {(activeSubject?.chapters || []).map((chapter) => (
                        <article className={`catalog-list-item ${chapter.is_active === false ? 'inactive' : ''}`} key={childId(chapter)}>
                          <div>
                            <b>{chapter.chapter_code} - {chapter.chapter_name}</b>
                            <span>{usageText(chapter.usage_counts)}</span>
                          </div>
                          <div className="catalog-item-actions">
                            <button type="button" className="catalog-ghost-button" onClick={() => setChapterForm(chapterToForm(chapter))} disabled={saving}>
                              Sửa
                            </button>
                            <button type="button" className="catalog-ghost-button" onClick={() => toggleChapterActive(chapter)} disabled={saving}>
                              {chapter.is_active === false ? 'Kích hoạt' : 'Tạm khóa'}
                            </button>
                          </div>
                        </article>
                      ))}
                      {(activeSubject?.learning_outcomes || []).map((clo) => (
                        <article className={`catalog-list-item ${clo.is_active === false ? 'inactive' : ''}`} key={childId(clo)}>
                          <div>
                            <b>{clo.clo_code}: {clo.description}</b>
                            <span>{usageText(clo.usage_counts)}</span>
                          </div>
                          <div className="catalog-item-actions">
                            <button type="button" className="catalog-ghost-button" onClick={() => setCloForm(cloToForm(clo))} disabled={saving}>
                              Sửa
                            </button>
                            <button type="button" className="catalog-ghost-button" onClick={() => toggleCloActive(clo)} disabled={saving}>
                              {clo.is_active === false ? 'Kích hoạt' : 'Tạm khóa'}
                            </button>
                          </div>
                        </article>
                      ))}
                    </div>
                  </>
                ) : (
                  <p className="catalog-empty">Tạo hoặc chọn một môn ở bên trái để thêm chương và CLO.</p>
                )}
              </div>
            </div>
          </div>

          <div className="catalog-card catalog-card--wide catalog-card--split">
            <h2>Mô hình AI</h2>
            <p className="catalog-card-note">Thêm phiên bản mới hoặc đổi model đang dùng mà không cần sửa code.</p>
            <form className="catalog-form" onSubmit={handleSaveModel}>
              <label className="catalog-model-field">
                <span>Tên hiển thị</span>
                <input placeholder="Ví dụ: Qwen3 (8B)" value={modelForm.display_name} onChange={(e) => setModelForm({ ...modelForm, display_name: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Mã cấu hình</span>
                <input placeholder="Ví dụ: qwen3-8b" value={modelForm.model_code} onChange={(e) => setModelForm({ ...modelForm, model_code: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Tên model và phiên bản</span>
                <input placeholder="Ví dụ: qwen3:8b" value={modelForm.model_name} onChange={(e) => setModelForm({ ...modelForm, model_name: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Nền tảng</span>
                <select value={modelForm.runtime} onChange={(e) => setModelForm({ ...modelForm, runtime: e.target.value })}>
                  <option value="OLLAMA">Ollama</option>
                  <option value="GEMINI">Gemini</option>
                </select>
              </label>
              <label className="catalog-model-field">
                <span>Mô tả ngắn</span>
                <input placeholder="Người dùng nên chọn model này khi nào?" value={modelForm.description} onChange={(e) => setModelForm({ ...modelForm, description: e.target.value })} />
              </label>
              <div className="catalog-model-capabilities">
                <span>Dùng cho</span>
                <label className="catalog-check">
                  <input type="checkbox" checked={modelForm.capabilities.includes('QUESTION_GENERATION')} onChange={() => toggleModelCapability('QUESTION_GENERATION')} />
                  Sinh câu hỏi
                </label>
                <label className="catalog-check">
                  <input type="checkbox" checked={modelForm.capabilities.includes('QUESTION_EVALUATION')} onChange={() => toggleModelCapability('QUESTION_EVALUATION')} />
                  Đánh giá câu hỏi
                </label>
              </div>
              <details className="catalog-model-advanced">
                <summary>Cài đặt nâng cao</summary>
                <label className="catalog-model-field">
                  <span>Thời gian chờ (giây)</span>
                  <input type="number" min="1" max="1800" value={modelForm.config.timeout_seconds} onChange={(e) => setModelForm({ ...modelForm, config: { ...modelForm.config, timeout_seconds: e.target.value } })} />
                </label>
                <label className="catalog-model-field">
                  <span>Độ sáng tạo</span>
                  <input type="number" min="0" max="2" step="0.1" value={modelForm.config.temperature} onChange={(e) => setModelForm({ ...modelForm, config: { ...modelForm.config, temperature: e.target.value } })} />
                </label>
                {modelForm.runtime === 'OLLAMA' ? (
                  <>
                    <label className="catalog-model-field">
                      <span>Số token tối đa</span>
                      <input type="number" min="1" max="32768" value={modelForm.config.num_predict} onChange={(e) => setModelForm({ ...modelForm, config: { ...modelForm.config, num_predict: e.target.value } })} />
                    </label>
                    <label className="catalog-model-field">
                      <span>Địa chỉ Ollama (để trống để dùng mặc định)</span>
                      <input placeholder="http://.../api/generate" value={modelForm.config.endpoint} onChange={(e) => setModelForm({ ...modelForm, config: { ...modelForm.config, endpoint: e.target.value } })} />
                    </label>
                  </>
                ) : (
                  <label className="catalog-model-field">
                    <span>Số token tối đa</span>
                    <input type="number" min="1" max="65536" value={modelForm.config.max_output_tokens} onChange={(e) => setModelForm({ ...modelForm, config: { ...modelForm.config, max_output_tokens: e.target.value } })} />
                  </label>
                )}
              </details>
              <label className="catalog-model-field">
                <span>Thứ tự hiển thị</span>
                <input type="number" min="0" value={modelForm.priority} onChange={(e) => setModelForm({ ...modelForm, priority: e.target.value })} />
              </label>
              <label className="catalog-check">
                <input type="checkbox" checked={modelForm.is_active} onChange={(e) => setModelForm({ ...modelForm, is_active: e.target.checked })} />
                Đang dùng
              </label>
              <button type="submit" disabled={saving}>Lưu mô hình</button>
            </form>
            <div className="catalog-list">
              {catalog.ai_models.length === 0 && <p className="catalog-list-empty">Chưa có mô hình AI nào. Điền form bên trái để thêm mô hình đầu tiên.</p>}
              {catalog.ai_models.map((model) => (
                <article className={`catalog-list-item ${model.is_active === false ? 'inactive' : ''}`} key={model._id || model.model_code}>
                  <div>
                    <b>{model.display_name || model.model_name}</b>
                    <span>{model.model_name} · {model.runtime}</span>
                    <span>{factoryText(model)}</span>
                    <span>{healthText(model)}</span>
                  </div>
                  <div className="catalog-item-actions">
                    <button type="button" className="catalog-ghost-button" onClick={() => handleSelectModel(model)} disabled={saving}>
                      Sửa
                    </button>
                    <button type="button" className="catalog-ghost-button" onClick={() => toggleModelActive(model)} disabled={saving}>
                      {model.is_active === false ? 'Kích hoạt' : 'Tạm khóa'}
                    </button>
                    <button type="button" onClick={() => handleCheckModelHealth(model)} disabled={saving || healthCheckingCode === model.model_code}>
                      {healthCheckingCode === model.model_code ? 'Đang kiểm tra...' : 'Kiểm tra'}
                    </button>
                  </div>
                </article>
              ))}
            </div>
            {modelHealth && (
              <p className={`catalog-result ${modelHealth.status === 'OK' ? 'ok' : 'warn'}`}>
                {modelHealth.status === 'OK' ? 'Mô hình hoạt động tốt' : 'Mô hình chưa sẵn sàng'}
                {' · '}{modelHealth.latency_ms || 0} ms
              </p>
            )}
          </div>

          <div className="catalog-card catalog-card--wide">
            <div className="catalog-card-title-row">
              <h2>Mẫu prompt</h2>
              <button type="button" className="catalog-ghost-button" onClick={handleTestPrompt} disabled={promptTesting}>
                {promptTesting ? 'Đang dựng prompt...' : 'Xem trước prompt'}
              </button>
            </div>
            <div className="prompt-layout">
              <label className="catalog-model-field prompt-picker">
                <span>Prompt cần sửa</span>
                <select value={selectedPromptKey} onChange={(e) => handleSelectPrompt(e.target.value)}>
                  <option value="">Tạo prompt mới</option>
                {promptOptions.map((template) => (
                  <option key={template.template_key} value={template.template_key}>
                      {template.name || template.template_key} - phiên bản {template.version}
                    </option>
                  ))}
                </select>
              </label>
              <form className="catalog-form" onSubmit={handleSavePrompt}>
                <label className="catalog-model-field">
                  <span>Mã prompt</span>
                  <input placeholder="Ví dụ: trac_nghiem" value={promptForm.template_key} onChange={(e) => setPromptForm({ ...promptForm, template_key: e.target.value })} />
                </label>
                <label className="catalog-model-field">
                  <span>Nhóm prompt</span>
                  <input placeholder="QUESTION_TYPE" value={promptForm.kind} onChange={(e) => setPromptForm({ ...promptForm, kind: e.target.value })} />
                </label>
                <label className="catalog-model-field">
                  <span>Tên hiển thị</span>
                  <input placeholder="Ví dụ: Trắc nghiệm 4 lựa chọn" value={promptForm.name} onChange={(e) => setPromptForm({ ...promptForm, name: e.target.value })} />
                </label>
                <label className="catalog-model-field">
                  <span>Nội dung prompt</span>
                  <textarea rows={9} value={promptForm.prompt_body} onChange={(e) => setPromptForm({ ...promptForm, prompt_body: e.target.value })} />
                </label>
                <button type="submit" disabled={saving}>Lưu phiên bản mới</button>
              </form>
            </div>
            {promptPreview && (
              <div className="prompt-preview">
                <div className="catalog-card-title-row">
                  <b>Xem trước prompt · {promptPreview.length} ký tự</b>
                  <span className={`catalog-status ${promptPreview.prompt_source === 'db' ? 'ok' : 'warn'}`}>
                    {promptPreview.prompt_source === 'db' ? 'Nguồn: CSDL' : 'Nguồn: tệp mặc định'}
                  </span>
                </div>
                <pre>{promptPreview.rendered_prompt}</pre>
              </div>
            )}
            <div className="catalog-list">
              {catalog.prompt_templates.map((template) => (
                <article className={`catalog-list-item ${template.is_active ? '' : 'inactive'}`} key={`${template.template_key}-${template.version}`}>
                  <div>
                    <b>{template.name || template.template_key}</b>
                    <span>{template.template_key} · phiên bản {template.version} {template.is_active ? '· đang dùng' : ''}</span>
                  </div>
                  <div className="catalog-item-actions">
                    <button type="button" className="catalog-ghost-button" onClick={() => handleEditPromptVersion(template)} disabled={saving}>
                      Sửa
                    </button>
                    <button type="button" onClick={() => handleActivatePrompt(template, !template.is_active)} disabled={saving}>
                      {template.is_active ? 'Ngừng dùng' : 'Dùng phiên bản này'}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>

          <div className="catalog-card catalog-card--wide catalog-card--split">
            <h2>Bộ tiêu chí đánh giá</h2>
            <p className="catalog-card-note">Mỗi lần lưu tạo một phiên bản mới và áp dụng ngay phiên bản đó khi AI thẩm định câu hỏi.</p>
            <form className="catalog-form" onSubmit={handleSavePolicy}>
              <label className="catalog-model-field">
                <span>Tên bộ tiêu chí</span>
                <input value={policyForm.policy_name} onChange={(e) => setPolicyForm({ ...policyForm, policy_name: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Trọng số (JSON)</span>
                <textarea rows={7} value={policyForm.weights} onChange={(e) => setPolicyForm({ ...policyForm, weights: e.target.value })} />
              </label>
              <label className="catalog-model-field">
                <span>Ngưỡng điểm (JSON)</span>
                <textarea rows={5} value={policyForm.thresholds} onChange={(e) => setPolicyForm({ ...policyForm, thresholds: e.target.value })} />
              </label>
              <button type="submit" disabled={saving}>Lưu phiên bản tiêu chí</button>
            </form>
            <div className="catalog-list">
              {catalog.evaluation_policies.length === 0 && <p className="catalog-list-empty">Chưa có phiên bản tiêu chí nào được lưu.</p>}
              {catalog.evaluation_policies.map((policy) => (
                <article className={`catalog-list-item ${policy.is_active ? '' : 'inactive'}`} key={policy._id || `${policy.policy_name}-${policy.version}`}>
                  <div>
                    <b>{policy.policy_name}</b>
                    <span>Phiên bản {policy.version} {policy.is_active ? '· đang dùng' : ''}</span>
                  </div>
                  <div className="catalog-item-actions">
                    <button type="button" className="catalog-ghost-button" onClick={() => handleEditPolicy(policy)} disabled={saving}>
                      Sửa
                    </button>
                    <button type="button" onClick={() => handleActivatePolicy(policy, !policy.is_active)} disabled={saving}>
                      {policy.is_active ? 'Ngừng dùng' : 'Dùng phiên bản này'}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>
      )}
    </main>
  );
}

export default CatalogAdminPage;
