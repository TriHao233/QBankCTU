import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  addQuestionsManual,
  autoGenerateQuestions,
  createVariant,
  deleteVariant,
  downloadVariantDocx,
  downloadVariantPdf,
  getExam,
  getMatrixAvailability,
  getVariantPreview,
  listExamQuestionPool,
  listVariants,
  removeQuestion,
  saveMatrix,
  updateExamStatus,
  updateExam,
} from '../api/exams';
import { listSubjects } from '../api/catalog';
import { difficultyLabel, questionTypeLabel } from '../constants/generationEnums';
import '../css/ExamBuilderPage.css';
import { notify } from '../utils/notify';

function refId(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return value.id || value._id || '';
}

const COGNITIVE_LEVELS = [
  { value: 'nhan_biet', label: 'Nhận biết' },
  { value: 'thong_hieu', label: 'Thông hiểu' },
  { value: 'van_dung', label: 'Vận dụng' },
  { value: 'van_dung_cao', label: 'Vận dụng cao' },
];

const DIFFICULTIES = [
  { value: 'de', label: 'Dễ' },
  { value: 'trung_binh', label: 'Trung bình' },
  { value: 'kho', label: 'Khó' },
];

const COGNITIVE_TO_BLOOM = {
  nhan_biet: 1,
  thong_hieu: 2,
  van_dung: 3,
  van_dung_cao: 4,
};

const EXAM_STATUS_LABEL = {
  DRAFT: 'Nháp',
  READY: 'Sẵn sàng',
  FINALIZED: 'Đã chốt',
  ARCHIVED: 'Lưu trữ',
};

const QUESTION_POOL_PAGE_SIZE = 20;

const EXPORT_VARIANT_TYPES = [
  { value: 'de', label: 'Đề thi' },
  { value: 'dapan', label: 'Đáp án' },
  { value: 'de_dapan', label: 'Đề + Đáp án' },
];

const EXPORT_FORMATS = [
  { value: 'pdf', label: 'PDF' },
  { value: 'docx', label: 'DOCX' },
];

function examStatus(exam) {
  return String(exam?.status || 'DRAFT').toUpperCase();
}

function isExamLocked(exam) {
  return ['FINALIZED', 'ARCHIVED'].includes(examStatus(exam));
}

const STEPS = [
  { id: 'info', label: '1. Thông tin đề thi' },
  { id: 'header', label: '2. Đầu trang' },
  { id: 'matrix', label: '3. Ma trận đề' },
  { id: 'questions', label: '4. Chọn câu hỏi' },
  { id: 'variants', label: '5. Mã đề' },
  { id: 'preview', label: '6. Xem trước' },
  { id: 'export', label: '7. Xuất đề' },
];

function emptyCell() {
  return { chapter_id: '', cognitive_level: 'nhan_biet', difficulty: 'de', count: 1 };
}

function ExamBuilderPage() {
  const { examId } = useParams();
  const navigate = useNavigate();
  const [exam, setExam] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [step, setStep] = useState('info');
  const [subjects, setSubjects] = useState([]);

  const fetchExam = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getExam(examId);
      setExam(data);
    } catch (err) {
      setError(err.message || 'Không tải được đề thi');
    } finally {
      setLoading(false);
    }
  }, [examId]);

  useEffect(() => {
    fetchExam();
    listSubjects().then(setSubjects).catch(() => {});
  }, [fetchExam]);

  const subject = useMemo(
    () => subjects.find((s) => refId(s) === exam?.subject_id),
    [subjects, exam],
  );
  const chapters = subject?.chapters || [];
  const statusValue = examStatus(exam);
  const locked = isExamLocked(exam);

  const handleStatusChange = async (targetStatus) => {
    if (targetStatus === 'FINALIZED' && !window.confirm('Chốt đề thi và khóa ma trận/danh sách câu hỏi?')) {
      return;
    }
    if (targetStatus === 'ARCHIVED' && !window.confirm('Lưu trữ đề thi này?')) {
      return;
    }
    try {
      const updated = await updateExamStatus(exam.id, targetStatus);
      setExam(updated);
    } catch (err) {
      notify('Cập nhật trạng thái đề thi thất bại: ' + err.message);
    }
  };

  if (loading) return <main className="exam-builder-page"><p className="empty-note">Đang tải đề thi...</p></main>;
  if (error) return <main className="exam-builder-page"><p className="exam-error">{error}</p></main>;
  if (!exam) return null;

  return (
    <main className="exam-builder-page">
      <section className="builder-hero">
        <div className="container">
          <button type="button" className="btn btn--outline" onClick={() => navigate('/lam-de-thi')}>
            ← Danh sách đề thi
          </button>
          <h1 className="page-hero-title">{exam.name}</h1>
          <p className="page-hero-desc">{[exam.exam_title, subject ? `${subject.subject_code} · ${subject.subject_name}` : ''].filter(Boolean).join(' — ')}</p>
        </div>
      </section>

      <section className="builder-body">
        <div className="container builder-grid">
          <nav className="builder-steps">
            {STEPS.map((s) => (
              <button
                key={s.id}
                type="button"
                className={`builder-step ${step === s.id ? 'builder-step--active' : ''}`}
                onClick={() => setStep(s.id)}
              >
                {s.label}
              </button>
            ))}
          </nav>

          <div className="builder-content card">
            <LifecycleBar
              exam={exam}
              status={statusValue}
              onStatusChange={handleStatusChange}
            />
            {locked && (
              <p className="locked-note">
                Đề thi đã {statusValue === 'ARCHIVED' ? 'lưu trữ' : 'chốt'}; thông tin, ma trận và danh sách câu hỏi đang được khóa.
              </p>
            )}
            {step === 'info' && <InfoStep exam={exam} subject={subject} onSaved={setExam} readOnly={locked} />}
            {step === 'header' && <HeaderStep exam={exam} onSaved={setExam} readOnly={locked} />}
            {step === 'matrix' && <MatrixStep exam={exam} chapters={chapters} onSaved={setExam} readOnly={locked} />}
            {step === 'questions' && <QuestionsStep exam={exam} chapters={chapters} onSaved={setExam} readOnly={locked} />}
            {step === 'variants' && <VariantsStep exam={exam} />}
            {step === 'preview' && <PreviewStep exam={exam} />}
            {step === 'export' && <ExportStep exam={exam} />}
          </div>
        </div>
      </section>
    </main>
  );
}

function LifecycleBar({ exam, status, onStatusChange }) {
  const selectedCount = exam.questions?.length || 0;
  const target = Number(exam.question_count) || 0;
  const hasExactQuestionCount = selectedCount === target;
  const canReady = status === 'DRAFT' && hasExactQuestionCount;
  const canFinalize = status === 'READY' && hasExactQuestionCount;
  const canArchive = status === 'FINALIZED';
  const diffMessage = !hasExactQuestionCount
    ? (selectedCount < target
      ? `Còn thiếu ${target - selectedCount} câu để chuyển sẵn sàng hoặc chốt đề.`
      : `Đang dư ${selectedCount - target} câu, cần bỏ bớt để chuyển sẵn sàng hoặc chốt đề.`)
    : '';

  return (
    <section className="lifecycle-bar">
      <div>
        <span className={`exam-status-pill exam-status-pill--${status.toLowerCase()}`}>
          {EXAM_STATUS_LABEL[status] || status}
        </span>
        <b>{selectedCount}/{target} câu</b>
        {diffMessage && (
          <small>{diffMessage}</small>
        )}
      </div>
      <div className="lifecycle-actions">
        {status === 'READY' && (
          <button type="button" className="btn btn--outline" onClick={() => onStatusChange('DRAFT')}>
            Mở chỉnh
          </button>
        )}
        {status === 'DRAFT' && (
          <button type="button" className="btn btn--outline" disabled={!canReady} onClick={() => onStatusChange('READY')}>
            Đánh dấu sẵn sàng
          </button>
        )}
        {status === 'READY' && (
          <button type="button" className="btn btn--primary" disabled={!canFinalize} onClick={() => onStatusChange('FINALIZED')}>
            Chốt đề
          </button>
        )}
        {canArchive && (
          <button type="button" className="btn btn--outline" onClick={() => onStatusChange('ARCHIVED')}>
            Lưu trữ
          </button>
        )}
      </div>
    </section>
  );
}

function InfoStep({ exam, subject, onSaved, readOnly }) {
  const [name, setName] = useState(exam.name);
  const [examTitle, setExamTitle] = useState(exam.exam_title);
  const [questionCount, setQuestionCount] = useState(exam.question_count);
  const [saving, setSaving] = useState(false);

  const handleSave = async (e) => {
    e.preventDefault();
    if (readOnly) return;
    setSaving(true);
    try {
      const updated = await updateExam(exam.id, {
        name: name.trim(),
        exam_title: examTitle.trim(),
        question_count: Number(questionCount),
      });
      onSaved(updated);
    } catch (err) {
      notify('Lưu thất bại: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSave}>
      <h3 className="step-title">Thông tin đề thi</h3>
      <div className="field-group">
        <label className="field-label">Học phần</label>
        <input className="field-input" value={subject ? `${subject.subject_code} · ${subject.subject_name}` : 'Đang tải học phần...'} disabled />
        <small className="field-hint">Học phần được chọn khi tạo đề và không đổi được, vì câu hỏi trong đề lấy từ học phần này.</small>
      </div>
      <div className="field-group">
        <label className="field-label">Tên đề thi</label>
        <input className="field-input" value={name} onChange={(e) => setName(e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Tên kỳ thi</label>
        <input className="field-input" value={examTitle} onChange={(e) => setExamTitle(e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Số lượng câu hỏi</label>
        <input type="number" min={1} max={200} className="field-input" value={questionCount} onChange={(e) => setQuestionCount(e.target.value)} disabled={readOnly} />
      </div>
      <button type="submit" className="btn btn--primary" disabled={saving || readOnly}>{saving ? 'Đang lưu...' : 'Lưu'}</button>
    </form>
  );
}

function HeaderStep({ exam, onSaved, readOnly }) {
  const [header, setHeader] = useState({ ...exam.header });
  const [saving, setSaving] = useState(false);

  const setField = (key, value) => setHeader((h) => ({ ...h, [key]: value }));

  const handleSave = async (e) => {
    e.preventDefault();
    if (readOnly) return;
    setSaving(true);
    try {
      const updated = await updateExam(exam.id, { header });
      onSaved(updated);
    } catch (err) {
      notify('Lưu thất bại: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={handleSave}>
      <h3 className="step-title">Cấu hình đầu trang đề thi</h3>
      <div className="field-group">
        <label className="field-label">Tên Trường/Đại học</label>
        <input className="field-input" value={header.school_name || ''} onChange={(e) => setField('school_name', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Tên Khoa/Bộ môn</label>
        <input className="field-input" value={header.faculty_name || ''} onChange={(e) => setField('faculty_name', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Tên kỳ thi (hiển thị trên đầu trang)</label>
        <input className="field-input" value={header.exam_name || ''} onChange={(e) => setField('exam_name', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Tên môn học/học phần</label>
        <input className="field-input" value={header.subject_name || ''} onChange={(e) => setField('subject_name', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Thời gian làm bài (phút)</label>
        <input type="number" min={1} className="field-input" value={header.duration_minutes || 60} onChange={(e) => setField('duration_minutes', Number(e.target.value))} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Lớp (tuỳ chọn)</label>
        <input className="field-input" value={header.class_name || ''} onChange={(e) => setField('class_name', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Phòng thi (tuỳ chọn)</label>
        <input className="field-input" value={header.room || ''} onChange={(e) => setField('room', e.target.value)} disabled={readOnly} />
      </div>
      <div className="field-group">
        <label className="field-label">Ngày thi (tuỳ chọn)</label>
        <input className="field-input" value={header.exam_date || ''} onChange={(e) => setField('exam_date', e.target.value)} placeholder="dd/mm/yyyy" disabled={readOnly} />
      </div>
      <button type="submit" className="btn btn--primary" disabled={saving || readOnly}>{saving ? 'Đang lưu...' : 'Lưu đầu trang'}</button>
    </form>
  );
}

function MatrixStep({ exam, chapters, onSaved, readOnly }) {
  const [cells, setCells] = useState(exam.matrix?.length ? exam.matrix : [emptyCell()]);
  const [saving, setSaving] = useState(false);
  const [availability, setAvailability] = useState(null);
  const [checking, setChecking] = useState(false);

  const updateCell = (index, patch) => {
    setCells((current) => current.map((cell, i) => (i === index ? { ...cell, ...patch } : cell)));
  };
  const addRow = () => setCells((current) => [...current, emptyCell()]);
  const removeRow = (index) => setCells((current) => (
    current.length <= 1 ? current : current.filter((_, i) => i !== index)
  ));

  const totalCount = cells.reduce((sum, cell) => sum + Number(cell.count || 0), 0);
  const targetCount = Number(exam.question_count) || 0;
  const matrixOver = targetCount > 0 && totalCount > targetCount;
  const matrixUnder = targetCount > 0 && totalCount < targetCount;

  const handleSave = async () => {
    if (readOnly) return;
    const normalizedCells = cells.map((cell) => ({
      ...cell,
      chapter_id: cell.chapter_id || null,
      count: Number(cell.count) || 0,
    }));
    const invalidCell = normalizedCells.findIndex((cell) => cell.count <= 0);
    if (invalidCell >= 0) {
      notify(`Dòng ${invalidCell + 1} có số câu không hợp lệ (phải ≥ 1).`);
      return;
    }
    setSaving(true);
    try {
      const updated = await saveMatrix(exam.id, normalizedCells);
      onSaved(updated);
      setAvailability(null);
    } catch (err) {
      notify('Lưu ma trận thất bại: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleCheck = async () => {
    setChecking(true);
    try {
      const result = await getMatrixAvailability(exam.id);
      setAvailability(result);
    } catch (err) {
      notify('Kiểm tra thất bại: ' + err.message);
    } finally {
      setChecking(false);
    }
  };

  return (
    <div>
      <h3 className="step-title">Ma trận đề thi</h3>
      <p className="step-desc">
        Tổng số câu trong ma trận: <b>{totalCount}</b> / Số câu đề thi yêu cầu: <b>{targetCount}</b>
        {matrixOver && (
          <span className="matrix-warning"> — Vượt quá số câu đã khai báo!</span>
        )}
        {matrixUnder && (
          <span className="matrix-warning"> — Còn thiếu {targetCount - totalCount} câu so với yêu cầu.</span>
        )}
      </p>
      <div className="matrix-table-wrap" role="region" aria-label="Ma trận đề thi" tabIndex={0}>
        <table className="matrix-table">
          <thead>
            <tr>
              <th>Chương</th>
              <th>Mức nhận thức</th>
              <th>Độ khó ước lượng</th>
              <th>Số câu</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {cells.map((cell, index) => (
              <tr key={index}>
                <td>
                  <select className="field-select" value={cell.chapter_id || ''} onChange={(e) => updateCell(index, { chapter_id: e.target.value })} disabled={readOnly}>
                    <option value="">Tất cả chương</option>
                    {chapters.map((chapter) => (
                      <option key={chapter._id || chapter.id} value={chapter._id || chapter.id}>
                        {chapter.chapter_name}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <select className="field-select" value={cell.cognitive_level} onChange={(e) => updateCell(index, { cognitive_level: e.target.value })} disabled={readOnly}>
                    {COGNITIVE_LEVELS.map((lvl) => <option key={lvl.value} value={lvl.value}>{lvl.label}</option>)}
                  </select>
                </td>
                <td>
                  <select className="field-select" value={cell.difficulty} onChange={(e) => updateCell(index, { difficulty: e.target.value })} disabled={readOnly}>
                    {DIFFICULTIES.map((d) => <option key={d.value} value={d.value}>{d.label}</option>)}
                  </select>
                </td>
                <td>
                  <input type="number" min={1} className="field-input matrix-count" value={cell.count} onChange={(e) => updateCell(index, { count: e.target.value })} disabled={readOnly} />
                </td>
                <td>
                  <button
                    type="button"
                    className="icon-btn icon-btn--danger"
                    onClick={() => removeRow(index)}
                    disabled={readOnly || cells.length <= 1}
                    title={cells.length <= 1 ? 'Cần giữ ít nhất một dòng' : 'Xoá dòng'}
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="step-actions">
        <button type="button" className="btn btn--outline" onClick={addRow} disabled={readOnly}>+ Thêm nhóm</button>
        <button type="button" className="btn btn--outline" onClick={handleCheck} disabled={checking}>
          {checking ? 'Đang kiểm tra...' : 'Kiểm tra đủ câu hỏi'}
        </button>
        <button type="button" className="btn btn--primary" onClick={handleSave} disabled={saving || readOnly}>
          {saving ? 'Đang lưu...' : 'Lưu ma trận'}
        </button>
      </div>
      {availability && (
        <div className="availability-list">
          {availability.map((item, index) => (
            <div key={index} className={`availability-item ${item.sufficient ? 'availability-ok' : 'availability-warn'}`}>
              {COGNITIVE_LEVELS.find((l) => l.value === item.cognitive_level)?.label} · {DIFFICULTIES.find((d) => d.value === item.difficulty)?.label}:
              {' '}{item.available}/{item.requested} câu {item.sufficient ? '(đủ)' : '(thiếu)'}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function QuestionsStep({ exam, chapters, onSaved, readOnly }) {
  const [mode, setMode] = useState('auto');
  const [busy, setBusy] = useState(false);
  const [poolItems, setPoolItems] = useState([]);
  const [poolTotal, setPoolTotal] = useState(0);
  const [selectedIds, setSelectedIds] = useState([]);
  const [loadingApproved, setLoadingApproved] = useState(false);
  const [poolPage, setPoolPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [searchTerm, setSearchTerm] = useState('');
  const [chapterFilter, setChapterFilter] = useState('');
  const [cognitiveFilter, setCognitiveFilter] = useState('');
  const [difficultyFilter, setDifficultyFilter] = useState('');

  const poolIds = new Set((exam.questions || []).map((q) => q.question_id));
  const poolPages = Math.max(1, Math.ceil(poolTotal / QUESTION_POOL_PAGE_SIZE));
  const visiblePoolItems = poolItems.filter((q) => !poolIds.has(q.id) && !q.in_exam);
  const hiddenInPage = poolItems.length - visiblePoolItems.length;

  useEffect(() => {
    const handle = setTimeout(() => {
      setSearchTerm(searchInput.trim());
      setPoolPage(1);
    }, 350);
    return () => clearTimeout(handle);
  }, [searchInput]);

  const loadApproved = useCallback(async () => {
    if (mode !== 'manual') return;
    setLoadingApproved(true);
    try {
      const result = await listExamQuestionPool(exam.id, {
        page: poolPage,
        pageSize: QUESTION_POOL_PAGE_SIZE,
        search: searchTerm || undefined,
        chapterId: chapterFilter || undefined,
        bloomLevel: cognitiveFilter ? COGNITIVE_TO_BLOOM[cognitiveFilter] : undefined,
        difficulty: difficultyFilter || undefined,
      });
      setPoolItems(result.items || []);
      setPoolTotal(result.total || 0);
    } catch (err) {
      notify('Tải câu hỏi thất bại: ' + err.message);
      setPoolItems([]);
      setPoolTotal(0);
    } finally {
      setLoadingApproved(false);
    }
  }, [chapterFilter, cognitiveFilter, difficultyFilter, exam.id, mode, poolPage, searchTerm]);

  useEffect(() => {
    loadApproved();
  }, [loadApproved]);

  useEffect(() => {
    setPoolPage(1);
    setSelectedIds([]);
  }, [chapterFilter, cognitiveFilter, difficultyFilter]);

  const handleAutoGenerate = async () => {
    if (readOnly) return;
    setBusy(true);
    try {
      const updated = await autoGenerateQuestions(exam.id);
      onSaved(updated);
      notify('Đã tự sinh đề theo ma trận.', 'success');
    } catch (err) {
      notify('Tự sinh đề thất bại: ' + err.message);
    } finally {
      setBusy(false);
    }
  };

  const toggleSelect = (id) => {
    setSelectedIds((current) => (current.includes(id) ? current.filter((v) => v !== id) : [...current, id]));
  };

  const handleAddManual = async () => {
    if (readOnly || selectedIds.length === 0) return;
    setBusy(true);
    try {
      const updated = await addQuestionsManual(exam.id, selectedIds);
      onSaved(updated);
      setSelectedIds([]);
      await loadApproved();
    } catch (err) {
      notify('Thêm câu hỏi thất bại: ' + err.message);
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (questionId) => {
    if (readOnly) return;
    setBusy(true);
    try {
      const updated = await removeQuestion(exam.id, questionId);
      onSaved(updated);
    } catch (err) {
      notify('Xoá câu hỏi thất bại: ' + err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="step-title">Chọn câu hỏi từ ngân hàng đã duyệt</h3>
      <div className="step-tabs">
        <button type="button" className={`step-tab ${mode === 'auto' ? 'step-tab--active' : ''}`} onClick={() => setMode('auto')}>Tự sinh theo ma trận</button>
        <button type="button" className={`step-tab ${mode === 'manual' ? 'step-tab--active' : ''}`} onClick={() => setMode('manual')}>Chọn thủ công</button>
      </div>

      {mode === 'auto' && (
        <div className="step-actions">
          <button type="button" className="btn btn--primary" onClick={handleAutoGenerate} disabled={busy || readOnly}>
            {busy ? 'Đang sinh đề...' : 'Tự sinh câu hỏi theo ma trận'}
          </button>
        </div>
      )}

      {mode === 'manual' && (
        <div>
          <div className="pool-toolbar">
            <input
              className="field-input"
              placeholder="Tìm câu hỏi..."
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
            />
            <select className="field-select" value={chapterFilter} onChange={(event) => setChapterFilter(event.target.value)}>
              <option value="">Tất cả chương</option>
              {chapters.map((chapter) => (
                <option key={chapter._id || chapter.id} value={chapter._id || chapter.id}>
                  {chapter.chapter_name}
                </option>
              ))}
            </select>
            <select className="field-select" value={cognitiveFilter} onChange={(event) => setCognitiveFilter(event.target.value)}>
              <option value="">Tất cả mức</option>
              {COGNITIVE_LEVELS.map((level) => (
                <option key={level.value} value={level.value}>{level.label}</option>
              ))}
            </select>
            <select className="field-select" value={difficultyFilter} onChange={(event) => setDifficultyFilter(event.target.value)}>
              <option value="">Tất cả độ khó ước lượng</option>
              {DIFFICULTIES.map((difficulty) => (
                <option key={difficulty.value} value={difficulty.value}>{difficulty.label}</option>
              ))}
            </select>
          </div>
          {loadingApproved ? (
            <p className="empty-note">Đang tải câu hỏi đã duyệt...</p>
          ) : (
            <div className="question-pick-list">
              {visiblePoolItems.map((q) => (
                <label className="question-pick-item" key={q.id}>
                  <input type="checkbox" checked={selectedIds.includes(q.id)} onChange={() => toggleSelect(q.id)} disabled={readOnly} />
                  <span>
                    <b>{questionTypeLabel((q.classification?.assessment_type || '').toLowerCase())}</b>
                    {q.classification?.difficulty ? ` · ${difficultyLabel(q.classification.difficulty)}` : ''}
                    {' — '}{q.content}
                  </span>
                </label>
              ))}
              {visiblePoolItems.length === 0 && <p className="empty-note">Không có câu hỏi đã duyệt phù hợp.</p>}
            </div>
          )}
          <div className="pool-pagination">
            <span>
              Hiển thị {visiblePoolItems.length} câu · Trang {poolPage}/{poolPages} (tổng {poolTotal} khớp bộ lọc
              {hiddenInPage > 0 ? `, ${hiddenInPage} câu đã có trong đề` : ''})
            </span>
            <div>
              <button type="button" className="btn btn--outline" disabled={poolPage <= 1 || loadingApproved} onClick={() => setPoolPage((page) => Math.max(1, page - 1))}>
                Trước
              </button>
              <button type="button" className="btn btn--outline" disabled={poolPage >= poolPages || loadingApproved} onClick={() => setPoolPage((page) => Math.min(poolPages, page + 1))}>
                Sau
              </button>
            </div>
          </div>
          <div className="step-actions">
            <button type="button" className="btn btn--primary" onClick={handleAddManual} disabled={busy || readOnly || selectedIds.length === 0}>
              Thêm {selectedIds.length > 0 ? `(${selectedIds.length})` : ''} vào đề thi
            </button>
          </div>
        </div>
      )}

      <h4 className="pool-title">Câu hỏi trong đề ({(exam.questions || []).length}/{exam.question_count})</h4>
      <div className="question-pool-list">
        {(exam.questions || []).map((ref, index) => (
          <div className="question-pool-item" key={ref.question_id}>
            <span>Câu {index + 1}. {ref.content_snapshot?.content}</span>
            <button type="button" className="icon-btn icon-btn--danger" onClick={() => handleRemove(ref.question_id)} disabled={readOnly}>×</button>
          </div>
        ))}
        {(exam.questions || []).length === 0 && <p className="empty-note">Chưa có câu hỏi nào trong đề.</p>}
      </div>
    </div>
  );
}

function VariantsStep({ exam }) {
  const [variants, setVariants] = useState([]);
  const [loading, setLoading] = useState(true);
  const [examCode, setExamCode] = useState('');
  const [shuffle, setShuffle] = useState(true);
  const [busy, setBusy] = useState(false);
  const finalized = examStatus(exam) === 'FINALIZED';

  const load = async () => {
    setLoading(true);
    try {
      const result = await listVariants(exam.id);
      setVariants(result || []);
    } catch (err) {
      notify('Tải mã đề thất bại: ' + err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [exam.id]);

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!finalized || !examCode.trim()) return;
    setBusy(true);
    try {
      await createVariant(exam.id, examCode.trim(), shuffle);
      setExamCode('');
      await load();
    } catch (err) {
      notify('Tạo mã đề thất bại: ' + err.message);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (variantId) => {
    if (!window.confirm('Xoá mã đề này?')) return;
    setBusy(true);
    try {
      await deleteVariant(exam.id, variantId);
      await load();
    } catch (err) {
      notify('Xoá mã đề thất bại: ' + err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h3 className="step-title">Quản lý mã đề (tối đa 4 mã đề / kỳ thi)</h3>
      {!finalized && (
        <p className="locked-note">Cần chốt đề trước khi tạo mã đề để khóa snapshot câu hỏi.</p>
      )}
      {loading ? (
        <p className="empty-note">Đang tải...</p>
      ) : (
        <div className="variant-list">
          {variants.map((variant) => (
            <div className="variant-item" key={variant.id}>
              <span>Mã đề <b>{variant.exam_code}</b> — {(variant.questions || []).length} câu</span>
              <button type="button" className="icon-btn icon-btn--danger" onClick={() => handleDelete(variant.id)} disabled={busy}>×</button>
            </div>
          ))}
          {variants.length === 0 && <p className="empty-note">Chưa có mã đề nào.</p>}
        </div>
      )}
      {variants.length < 4 ? (
        <form className="variant-form" onSubmit={handleCreate}>
          <input className="field-input" placeholder="Mã đề (VD: 132)" value={examCode} onChange={(e) => setExamCode(e.target.value)} disabled={!finalized} />
          <label className="variant-shuffle-check">
            <input type="checkbox" checked={shuffle} onChange={(e) => setShuffle(e.target.checked)} disabled={!finalized} />
            Xáo trộn thứ tự câu hỏi/đáp án
          </label>
          <button type="submit" className="btn btn--primary" disabled={busy || !finalized}>Tạo mã đề</button>
        </form>
      ) : (
        <p className="matrix-warning">Đã đạt tối đa 4 mã đề cho kỳ thi này.</p>
      )}
    </div>
  );
}

function PreviewStep({ exam }) {
  const [variants, setVariants] = useState([]);
  const [variantId, setVariantId] = useState('');
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    listVariants(exam.id).then((result) => {
      setVariants(result || []);
      if (result?.length) setVariantId(result[0].id);
    }).catch(() => {});
  }, [exam.id]);

  useEffect(() => {
    if (!variantId) return;
    setLoading(true);
    getVariantPreview(exam.id, variantId)
      .then(setPreview)
      .catch((err) => notify('Xem trước thất bại: ' + err.message))
      .finally(() => setLoading(false));
  }, [exam.id, variantId]);

  return (
    <div>
      <h3 className="step-title">Xem trước đề thi</h3>
      <div className="field-group">
        <label className="field-label">Chọn mã đề</label>
        <select className="field-select" value={variantId} onChange={(e) => setVariantId(e.target.value)}>
          {variants.map((v) => <option key={v.id} value={v.id}>{v.exam_code}</option>)}
        </select>
      </div>
      {loading && <p className="empty-note">Đang tải...</p>}
      {preview && (
        <div className="exam-preview">
          <div className="preview-header">
            <div>
              <b>{preview.header.school_name}</b>
              <div>{preview.header.faculty_name}</div>
            </div>
            <div className="preview-header-right">
              <div>{preview.header.exam_name}</div>
              <div>Thời gian: {preview.header.duration_minutes} phút</div>
              <div>Mã đề: {preview.exam_code}</div>
            </div>
          </div>
          <h4 className="preview-title">Môn: {preview.header.subject_name}</h4>
          {preview.questions.map((q) => (
            <div className="preview-question" key={q.number}>
              <p><b>Câu {q.number}.</b> {q.content}</p>
              {q.options.map((opt) => (
                <div className="preview-option" key={opt.label}>{opt.label}. {opt.text}</div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ExportStep({ exam }) {
  const [variants, setVariants] = useState([]);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    listVariants(exam.id).then((result) => setVariants(result || [])).catch(() => {});
  }, [exam.id]);

  const handleExport = async (variantId, type, format) => {
    const key = `${variantId}-${type}-${format}`;
    setBusy(key);
    try {
      if (format === 'docx') {
        await downloadVariantDocx(exam.id, variantId, type);
      } else {
        await downloadVariantPdf(exam.id, variantId, type);
      }
    } catch (err) {
      notify(`Xuất ${format.toUpperCase()} thất bại: ${err.message}`);
    } finally {
      setBusy('');
    }
  };

  return (
    <div>
      <h3 className="step-title">Xuất đề thi giấy</h3>
      <p className="empty-note">Bản PDF dùng khổ A4, lề in chuẩn, font serif hỗ trợ tiếng Việt và có số trang. Hãy kiểm tra bản PDF trước khi in chính thức.</p>
      {variants.length === 0 && <p className="empty-note">Chưa có mã đề nào, hãy tạo mã đề ở bước trước.</p>}
      <div className="export-list">
        {variants.map((variant) => (
          <div className="export-item" key={variant.id}>
            <span>Mã đề <b>{variant.exam_code}</b></span>
            <div className="export-actions">
              {EXPORT_VARIANT_TYPES.map((exportType) => (
                <div className="export-action-group" key={exportType.value}>
                  <span>{exportType.label}</span>
                  <div>
                    {EXPORT_FORMATS.map((format) => {
                      const key = `${variant.id}-${exportType.value}-${format.value}`;
                      return (
                        <button
                          type="button"
                          className="btn btn--outline"
                          disabled={busy === key || (variant.questions || []).length === 0}
                          onClick={() => handleExport(variant.id, exportType.value, format.value)}
                          key={format.value}
                        >
                          {busy === key ? 'Đang xuất' : format.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default ExamBuilderPage;
