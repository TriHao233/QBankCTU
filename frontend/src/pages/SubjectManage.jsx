import React, { useContext, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  addSubjectChapter,
  addSubjectLearningOutcome,
  deactivateSubject,
  listSubjects,
  saveSubject,
  updateSubject,
  updateSubjectChapter,
  updateSubjectLearningOutcome,
} from '../api/catalog';
import { listQuestions } from '../api/questions';
import { permissionsForUser } from '../auth/permissions';
import { AuthContext } from '../context/AuthContext';
import { difficultyLabel, questionTypeLabel } from '../constants/generationEnums';
import '../css/SubjectManage.css';

const EMPTY_SUBJECT = { subject_code: '', subject_name: '', description: '', is_active: true };
const EMPTY_CHAPTER = { chapter_code: '', chapter_name: '', sequence_no: 1, is_active: true };
const OUTCOME_TYPE_OPTIONS = [
  { value: 'kien_thuc', label: 'Kiến thức (CTĐT)' },
  { value: 'ky_nang', label: 'Kỹ năng (CTĐT)' },
  { value: 'thai_do', label: 'Thái độ (CTĐT)' },
];
const EMPTY_CLO = {
  clo_code: '',
  description: '',
  outcome_type: 'kien_thuc',
  target_weight: 1,
  is_active: true,
};

const REVIEW_STATUS_LABEL = {
  DRAFT: 'Nháp',
  PENDING: 'Chờ duyệt',
  APPROVED: 'Đã duyệt',
  NEEDS_REVISION: 'Cần sửa',
  REJECTED: 'Từ chối',
};

const STATUS_FILTERS = [
  { value: 'active', label: 'Đang dùng' },
  { value: 'mine', label: 'Của tôi' },
  { value: 'all', label: 'Tất cả' },
];

function refId(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return value.id || value._id || '';
}

function usageTotal(counts) {
  if (!counts) return 0;
  return Object.values(counts).reduce((sum, value) => sum + (Number(value) || 0), 0);
}

function subjectSearchText(subject) {
  return `${subject.subject_code || ''} ${subject.subject_name || ''} ${subject.description || ''}`.toLowerCase();
}

function sortActiveChildren(items = []) {
  return [...items].sort((left, right) => {
    const leftOff = left.is_active === false ? 1 : 0;
    const rightOff = right.is_active === false ? 1 : 0;
    return leftOff - rightOff || (left.sequence_no || 0) - (right.sequence_no || 0);
  });
}

function questionKind(question) {
  return questionTypeLabel((question.classification?.assessment_type || '').toLowerCase()) || 'Câu hỏi';
}

function questionBloom(question) {
  return question.classification?.bloom?.name || 'Chưa gắn Bloom';
}

function questionDifficulty(question) {
  return difficultyLabel(question.classification?.difficulty) || 'Chưa ước lượng';
}

function SubjectManage() {
  const navigate = useNavigate();
  const { user } = useContext(AuthContext);
  const permissions = permissionsForUser(user);
  const canCreateSubjects = permissions.includes('catalog.subjects.manage_own');

  const [subjects, setSubjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState(null);

  const [keyword, setKeyword] = useState('');
  const [statusFilter, setStatusFilter] = useState('active');
  const [selectedSubjectId, setSelectedSubjectId] = useState('');
  const [questionsBySubject, setQuestionsBySubject] = useState({});

  const [subjectModalOpen, setSubjectModalOpen] = useState(false);
  const [editingSubject, setEditingSubject] = useState(null);
  const [subjectForm, setSubjectForm] = useState(EMPTY_SUBJECT);
  const [createCloDrafts, setCreateCloDrafts] = useState([]);

  const [childModal, setChildModal] = useState(null);
  const [chapterForm, setChapterForm] = useState(EMPTY_CHAPTER);
  const [cloForm, setCloForm] = useState(EMPTY_CLO);

  const fetchSubjects = async () => {
    setLoading(true);
    setError('');
    try {
      const items = await listSubjects();
      setSubjects(items);
      setSelectedSubjectId((current) => {
        if (current && items.some((subject) => refId(subject) === current)) return current;
        return refId(items.find((subject) => subject.is_active !== false) || items[0]);
      });
    } catch (err) {
      setError(err.message || 'Không tải được danh sách học phần');
    } finally {
      setLoading(false);
    }
  };

  const fetchSubjectQuestions = async (subjectId, { force = false } = {}) => {
    if (!subjectId) return;
    if (!force && questionsBySubject[subjectId]?.loaded) return;
    setQuestionsBySubject((current) => ({
      ...current,
      [subjectId]: { ...(current[subjectId] || {}), loading: true, error: '' },
    }));
    try {
      const result = await listQuestions({ subjectId, page: 1, pageSize: 6 });
      setQuestionsBySubject((current) => ({
        ...current,
        [subjectId]: {
          items: result.items || [],
          total: result.total || 0,
          loading: false,
          error: '',
          loaded: true,
        },
      }));
    } catch (err) {
      setQuestionsBySubject((current) => ({
        ...current,
        [subjectId]: {
          ...(current[subjectId] || {}),
          loading: false,
          error: err.message || 'Không tải được câu hỏi của học phần',
          loaded: true,
        },
      }));
    }
  };

  useEffect(() => {
    fetchSubjects();
  }, []);

  useEffect(() => {
    if (!selectedSubjectId) return;
    fetchSubjectQuestions(selectedSubjectId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSubjectId]);

  useEffect(() => {
    if (!notice) return undefined;
    const timer = window.setTimeout(() => setNotice(''), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const visibleSubjects = useMemo(() => {
    const needle = keyword.trim().toLowerCase();
    return subjects.filter((subject) => {
      if (statusFilter === 'active' && subject.is_active === false) return false;
      if (statusFilter === 'mine' && !subject.can_manage) return false;
      if (!needle) return true;
      return subjectSearchText(subject).includes(needle);
    });
  }, [subjects, keyword, statusFilter]);

  const selectedSubject = useMemo(
    () => subjects.find((subject) => refId(subject) === selectedSubjectId) || null,
    [subjects, selectedSubjectId],
  );

  const selectedQuestionState = questionsBySubject[selectedSubjectId] || {
    items: [],
    total: selectedSubject?.usage_counts?.questions || 0,
    loading: false,
    error: '',
    loaded: false,
  };

  const stats = useMemo(() => ({
    total: subjects.length,
    active: subjects.filter((item) => item.is_active !== false).length,
    owned: subjects.filter((item) => item.can_manage).length,
    questions: subjects.reduce((sum, item) => sum + (Number(item.usage_counts?.questions) || 0), 0),
  }), [subjects]);

  const activeChapters = sortActiveChildren(selectedSubject?.chapters || []);
  const activeClos = sortActiveChildren(selectedSubject?.learning_outcomes || []);

  const openCreateSubject = () => {
    setEditingSubject(null);
    setSubjectForm(EMPTY_SUBJECT);
    setCreateCloDrafts([]);
    setSubjectModalOpen(true);
  };

  const openEditSubject = (subject) => {
    setEditingSubject(subject);
    setSubjectForm({
      subject_code: subject.subject_code || '',
      subject_name: subject.subject_name || '',
      description: subject.description || '',
      is_active: subject.is_active !== false,
    });
    setCreateCloDrafts([]);
    setSubjectModalOpen(true);
  };

  const closeSubjectModal = () => {
    if (saving) return;
    setSubjectModalOpen(false);
    setCreateCloDrafts([]);
  };

  const handleSubmitSubject = async (event) => {
    event.preventDefault();
    if (!subjectForm.subject_code.trim() || !subjectForm.subject_name.trim()) {
      setError('Vui lòng nhập mã và tên học phần.');
      return;
    }
    const initialClos = createCloDrafts
      .map((item) => ({
        clo_code: String(item.clo_code || '').trim(),
        description: String(item.description || '').trim(),
        outcome_type: item.outcome_type || 'kien_thuc',
        target_weight: Number(item.target_weight) || 1,
        is_active: item.is_active !== false,
      }))
      .filter((item) => item.clo_code && item.description);
    setSaving(true);
    setError('');
    try {
      const payload = {
        subject_code: subjectForm.subject_code.trim(),
        subject_name: subjectForm.subject_name.trim(),
        description: subjectForm.description.trim(),
        is_active: subjectForm.is_active,
        ...(!editingSubject && initialClos.length ? { learning_outcomes: initialClos } : {}),
      };
      const saved = editingSubject
        ? await updateSubject(refId(editingSubject), payload)
        : await saveSubject(payload);
      const savedId = refId(saved) || refId(editingSubject);
      if (savedId) setSelectedSubjectId(savedId);
      setNotice(editingSubject ? 'Đã cập nhật học phần.' : 'Đã tạo học phần mới.');
      setSubjectModalOpen(false);
      setCreateCloDrafts([]);
      await fetchSubjects();
    } catch (err) {
      setError(err.message || 'Lưu học phần thất bại');
    } finally {
      setSaving(false);
    }
  };

  const handleDeactivate = async (subject) => {
    const used = usageTotal(subject.usage_counts);
    const warning = used > 0
      ? `\n\nHọc phần này đang gắn với ${used} tài liệu/câu hỏi/đề thi. Dữ liệu đó vẫn được giữ nguyên.`
      : '';
    const confirmed = window.confirm(
      `Ngừng sử dụng học phần "${subject.subject_name}"?`
      + `\nHọc phần sẽ bị ẩn khỏi các danh sách chọn nhưng có thể bật lại bất cứ lúc nào.${warning}`,
    );
    if (!confirmed) return;
    setBusyId(refId(subject));
    setError('');
    try {
      await deactivateSubject(refId(subject));
      setNotice('Đã ngừng sử dụng học phần.');
      await fetchSubjects();
    } catch (err) {
      setError(err.message || 'Không thể ngừng sử dụng học phần');
    } finally {
      setBusyId(null);
    }
  };

  const handleRestore = async (subject) => {
    const originalId = refId(subject);
    setBusyId(originalId);
    setError('');
    try {
      const updated = await updateSubject(originalId, { is_active: true });
      setSelectedSubjectId(refId(updated) || originalId);
      setNotice('Đã khôi phục học phần.');
      await fetchSubjects();
    } catch (err) {
      setError(err.message || 'Không thể khôi phục học phần');
    } finally {
      setBusyId(null);
    }
  };

  const openChildModal = (subject, mode, item = null) => {
    if (mode === 'chapter') {
      setChapterForm(item ? {
        chapter_code: item.chapter_code || '',
        chapter_name: item.chapter_name || '',
        sequence_no: item.sequence_no || 1,
        is_active: item.is_active !== false,
      } : {
        ...EMPTY_CHAPTER,
        sequence_no: (subject.chapters?.length || 0) + 1,
      });
    } else {
      setCloForm(item ? {
        clo_code: item.clo_code || '',
        description: item.description || '',
        outcome_type: item.outcome_type || 'kien_thuc',
        target_weight: item.target_weight ?? 1,
        is_active: item.is_active !== false,
      } : EMPTY_CLO);
    }
    setChildModal({ subjectId: refId(subject), mode, itemId: item ? refId(item) : null });
  };

  const closeChildModal = () => {
    if (saving) return;
    setChildModal(null);
  };

  const handleSubmitChild = async (event) => {
    event.preventDefault();
    if (!childModal) return;
    const { subjectId, mode, itemId } = childModal;
    setSaving(true);
    setError('');
    try {
      if (mode === 'chapter') {
        if (!chapterForm.chapter_code.trim() || !chapterForm.chapter_name.trim()) {
          setError('Vui lòng nhập mã và tên chương.');
          return;
        }
        const payload = {
          chapter_code: chapterForm.chapter_code.trim(),
          chapter_name: chapterForm.chapter_name.trim(),
          sequence_no: Number(chapterForm.sequence_no) || 1,
          is_active: chapterForm.is_active,
        };
        if (itemId) await updateSubjectChapter(subjectId, itemId, payload);
        else await addSubjectChapter(subjectId, payload);
        setNotice(itemId ? 'Đã cập nhật chương.' : 'Đã thêm chương mới.');
      } else {
        if (!cloForm.clo_code.trim() || !cloForm.description.trim()) {
          setError('Vui lòng nhập mã CLO và mô tả.');
          return;
        }
        const payload = {
          clo_code: cloForm.clo_code.trim(),
          description: cloForm.description.trim(),
          outcome_type: cloForm.outcome_type || 'kien_thuc',
          target_weight: Number(cloForm.target_weight) || 0,
          is_active: cloForm.is_active,
        };
        if (itemId) await updateSubjectLearningOutcome(subjectId, itemId, payload);
        else await addSubjectLearningOutcome(subjectId, payload);
        setNotice(itemId ? 'Đã cập nhật CLO.' : 'Đã thêm CLO mới.');
      }
      setChildModal(null);
      await fetchSubjects();
    } catch (err) {
      setError(err.message || 'Lưu thất bại');
    } finally {
      setSaving(false);
    }
  };

  const toggleChildActive = async (subject, mode, item) => {
    setBusyId(refId(item));
    setError('');
    try {
      const payload = { is_active: item.is_active === false };
      if (mode === 'chapter') {
        await updateSubjectChapter(refId(subject), refId(item), payload);
      } else {
        await updateSubjectLearningOutcome(refId(subject), refId(item), payload);
      }
      await fetchSubjects();
    } catch (err) {
      setError(err.message || 'Không thể đổi trạng thái');
    } finally {
      setBusyId(null);
    }
  };

  const goToQuestionManager = (subjectId = selectedSubjectId) => {
    if (!subjectId) return;
    navigate(`/quan-ly?subject_id=${encodeURIComponent(subjectId)}`);
  };

  return (
    <main className="subject-manage-page">
      <section className="subject-workspace">
        <div className="container">
          <div className="subject-topbar">
            <div>
              <h1>Quản lý học phần</h1>
              <p>
                Tạo học phần cá nhân, duy trì chương/CLO và xem nhanh ngân hàng câu hỏi đang gắn với từng học phần.
              </p>
            </div>
            <div className="subject-topbar-actions">
              <button type="button" className="btn btn--outline" onClick={fetchSubjects} disabled={loading}>
                Làm mới
              </button>
              {canCreateSubjects && (
                <button type="button" className="btn btn--primary" onClick={openCreateSubject}>
                  Thêm học phần
                </button>
              )}
            </div>
          </div>

          <div className="subject-stat-grid">
            <div><b>{stats.total}</b><span>Tổng học phần</span></div>
            <div><b>{stats.active}</b><span>Đang dùng</span></div>
            <div><b>{stats.owned}</b><span>Bạn quản lý</span></div>
            <div><b>{stats.questions}</b><span>Câu hỏi đã gắn</span></div>
          </div>

          {error && <p className="subject-alert subject-alert--error">{error}</p>}
          {notice && <p className="subject-alert subject-alert--ok">{notice}</p>}

          <div className="subject-shell">
            <aside className="subject-sidebar" aria-label="Danh sách học phần">
              <div className="subject-sidebar-head">
                <input
                  className="field-input subject-search"
                  placeholder="Tìm mã, tên hoặc mô tả..."
                  value={keyword}
                  onChange={(event) => setKeyword(event.target.value)}
                />
                <div className="subject-segments">
                  {STATUS_FILTERS.map((filter) => (
                    <button
                      type="button"
                      key={filter.value}
                      className={statusFilter === filter.value ? 'is-active' : ''}
                      onClick={() => setStatusFilter(filter.value)}
                    >
                      {filter.label}
                    </button>
                  ))}
                </div>
              </div>

              {loading ? (
                <p className="empty-note">Đang tải học phần...</p>
              ) : visibleSubjects.length === 0 ? (
                <p className="empty-note">
                  {keyword
                    ? 'Không tìm thấy học phần phù hợp.'
                    : canCreateSubjects
                      ? 'Chưa có học phần nào trong bộ lọc này.'
                      : 'Chưa có học phần nào được phân quyền cho bạn.'}
                </p>
              ) : (
                <div className="subject-list">
                  {visibleSubjects.map((subject) => {
                    const id = refId(subject);
                    const counts = subject.usage_counts || {};
                    const activeChapterCount = (subject.chapters || []).filter((chapter) => chapter.is_active !== false).length;
                    const activeCloCount = (subject.learning_outcomes || []).filter((clo) => clo.is_active !== false).length;
                    return (
                      <button
                        type="button"
                        className={`subject-row ${selectedSubjectId === id ? 'is-selected' : ''} ${subject.is_active === false ? 'is-muted' : ''}`}
                        key={id}
                        onClick={() => setSelectedSubjectId(id)}
                      >
                        <span className="subject-row-code">{subject.subject_code}</span>
                        <span className="subject-row-main">
                          <b>{subject.subject_name}</b>
                          <small>{counts.questions || 0} câu hỏi · {activeChapterCount} chương · {activeCloCount} CLO</small>
                        </span>
                        <span className="subject-row-tags">
                          {!subject.can_manage && <span>Chỉ xem</span>}
                          {subject.is_active === false && <span>Đã ngừng</span>}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </aside>

            <section className="subject-panel" aria-label="Chi tiết học phần">
              {!selectedSubject ? (
                <div className="subject-placeholder">
                  <h2>Chọn một học phần</h2>
                  <p>Chi tiết học phần, chương, CLO và câu hỏi sẽ hiển thị tại đây.</p>
                </div>
              ) : (
                <>
                  <header className="subject-detail-header">
                    <div>
                      <span className="subject-code-pill">{selectedSubject.subject_code}</span>
                      <h2>{selectedSubject.subject_name}</h2>
                      {selectedSubject.description && <p>{selectedSubject.description}</p>}
                      <div className="subject-meta-strip">
                        <span>{selectedSubject.owner_email || 'Chưa có email chủ sở hữu'}</span>
                        <span>{selectedSubject.is_active === false ? 'Đã ngừng sử dụng' : 'Đang sử dụng'}</span>
                        <span>{selectedSubject.can_manage ? 'Có quyền chỉnh sửa' : 'Chỉ xem'}</span>
                      </div>
                    </div>
                    <div className="subject-action-stack">
                      <button type="button" className="btn btn--outline" onClick={() => goToQuestionManager()}>
                        Quản lý câu hỏi
                      </button>
                      {selectedSubject.can_manage && (
                        <>
                          <button type="button" className="btn btn--outline" onClick={() => openEditSubject(selectedSubject)}>
                            Sửa học phần
                          </button>
                          {selectedSubject.is_active !== false ? (
                            <button
                              type="button"
                              className="btn btn--danger"
                              disabled={busyId === selectedSubjectId}
                              onClick={() => handleDeactivate(selectedSubject)}
                            >
                              Ngừng dùng
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="btn btn--outline"
                              disabled={busyId === selectedSubjectId}
                              onClick={() => handleRestore(selectedSubject)}
                            >
                              Khôi phục
                            </button>
                          )}
                        </>
                      )}
                    </div>
                  </header>

                  <div className="subject-summary-grid">
                    <div><b>{activeChapters.length}</b><span>Chương</span></div>
                    <div><b>{activeClos.length}</b><span>CLO</span></div>
                    <div><b>{selectedSubject.usage_counts?.documents || 0}</b><span>Tài liệu</span></div>
                    <div><b>{selectedSubject.usage_counts?.questions || 0}</b><span>Câu hỏi</span></div>
                    <div><b>{selectedSubject.usage_counts?.exams || 0}</b><span>Đề thi</span></div>
                  </div>

                  <div className="subject-detail-grid">
                    <div className="detail-block">
                      <div className="detail-head">
                        <h3>Chương</h3>
                        {selectedSubject.can_manage && (
                          <button type="button" className="link-button" onClick={() => openChildModal(selectedSubject, 'chapter')}>
                            Thêm chương
                          </button>
                        )}
                      </div>
                      {activeChapters.length ? (
                        <ul className="detail-list">
                          {activeChapters.map((chapter) => (
                            <li key={refId(chapter)} className={chapter.is_active === false ? 'is-off' : ''}>
                              <div>
                                <b>{chapter.chapter_code}</b>
                                <span>{chapter.chapter_name}</span>
                                <small>{chapter.usage_counts?.questions || 0} câu hỏi</small>
                              </div>
                              {selectedSubject.can_manage && (
                                <div className="detail-actions">
                                  <button type="button" onClick={() => openChildModal(selectedSubject, 'chapter', chapter)}>Sửa</button>
                                  <button
                                    type="button"
                                    disabled={busyId === refId(chapter)}
                                    onClick={() => toggleChildActive(selectedSubject, 'chapter', chapter)}
                                  >
                                    {chapter.is_active === false ? 'Bật' : 'Tắt'}
                                  </button>
                                </div>
                              )}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="detail-empty">Chưa có chương nào.</p>
                      )}
                    </div>

                    <div className="detail-block">
                      <div className="detail-head">
                        <h3>Chuẩn đầu ra</h3>
                        {selectedSubject.can_manage && (
                          <button type="button" className="link-button" onClick={() => openChildModal(selectedSubject, 'clo')}>
                            Thêm CLO
                          </button>
                        )}
                      </div>
                      {activeClos.length ? (
                        <ul className="detail-list">
                          {activeClos.map((clo) => (
                            <li key={refId(clo)} className={clo.is_active === false ? 'is-off' : ''}>
                              <div>
                                <b>{clo.clo_code}</b>
                                <span>{clo.description}</span>
                                <small>Trọng số {clo.target_weight ?? 1}</small>
                              </div>
                              {selectedSubject.can_manage && (
                                <div className="detail-actions">
                                  <button type="button" onClick={() => openChildModal(selectedSubject, 'clo', clo)}>Sửa</button>
                                  <button
                                    type="button"
                                    disabled={busyId === refId(clo)}
                                    onClick={() => toggleChildActive(selectedSubject, 'clo', clo)}
                                  >
                                    {clo.is_active === false ? 'Bật' : 'Tắt'}
                                  </button>
                                </div>
                              )}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="detail-empty">Chưa có CLO nào.</p>
                      )}
                    </div>
                  </div>

                  <div className="question-preview">
                    <div className="detail-head">
                      <div>
                        <h3>Câu hỏi trong học phần</h3>
                        <p>Chỉ xem nhanh. Sửa và duyệt câu hỏi thực hiện ở module Quản lý câu hỏi.</p>
                      </div>
                      <div className="question-preview-actions">
                        <button
                          type="button"
                          className="btn btn--outline"
                          disabled={selectedQuestionState.loading}
                          onClick={() => fetchSubjectQuestions(selectedSubjectId, { force: true })}
                        >
                          Tải lại
                        </button>
                        <button type="button" className="btn btn--primary" onClick={() => goToQuestionManager()}>
                          Xem tất cả
                        </button>
                      </div>
                    </div>

                    {selectedQuestionState.error && (
                      <p className="subject-alert subject-alert--error">{selectedQuestionState.error}</p>
                    )}
                    {selectedQuestionState.loading ? (
                      <p className="empty-note">Đang tải câu hỏi...</p>
                    ) : selectedQuestionState.items.length ? (
                      <>
                        <div className="question-readonly-list">
                          {selectedQuestionState.items.map((question) => (
                            <article className="question-readonly-item" key={question.id}>
                              <div className="question-readonly-head">
                                <span>{question.question_code}</span>
                                <b>{REVIEW_STATUS_LABEL[question.review_status] || question.review_status}</b>
                              </div>
                              <p>{question.content}</p>
                              <div className="question-readonly-tags">
                                <span>{questionKind(question)}</span>
                                <span>{questionBloom(question)}</span>
                                <span>{questionDifficulty(question)}</span>
                                {(question.clos || []).slice(0, 3).map((clo, index) => (
                                  <span key={refId(clo.id || clo) || `${clo.code || clo.clo_code || 'clo'}-${index}`}>
                                    {clo.code || clo.clo_code || 'CLO'}
                                  </span>
                                ))}
                              </div>
                            </article>
                          ))}
                        </div>
                        {selectedQuestionState.total > selectedQuestionState.items.length && (
                          <p className="question-preview-more">
                            Còn {selectedQuestionState.total - selectedQuestionState.items.length} câu hỏi khác trong học phần này.
                          </p>
                        )}
                      </>
                    ) : (
                      <p className="detail-empty">Học phần này chưa có câu hỏi nào.</p>
                    )}
                  </div>
                </>
              )}
            </section>
          </div>
        </div>
      </section>

      {subjectModalOpen && (
        <div className="modal-overlay" onClick={closeSubjectModal}>
          <form className="modal-card" onClick={(event) => event.stopPropagation()} onSubmit={handleSubmitSubject}>
            <h3 className="modal-title">{editingSubject ? 'Sửa học phần' : 'Thêm học phần mới'}</h3>

            <div className="field-group">
              <label className="field-label">Mã học phần</label>
              <input
                className="field-input"
                value={subjectForm.subject_code}
                maxLength={40}
                placeholder="CT101"
                onChange={(event) => setSubjectForm({ ...subjectForm, subject_code: event.target.value })}
              />
            </div>

            <div className="field-group">
              <label className="field-label">Tên học phần</label>
              <input
                className="field-input"
                value={subjectForm.subject_name}
                maxLength={200}
                placeholder="Cấu trúc dữ liệu và giải thuật"
                onChange={(event) => setSubjectForm({ ...subjectForm, subject_name: event.target.value })}
              />
            </div>

            <div className="field-group">
              <label className="field-label">Mô tả</label>
              <textarea
                className="field-input field-textarea"
                rows={3}
                value={subjectForm.description}
                placeholder="Mô tả ngắn về nội dung học phần..."
                onChange={(event) => setSubjectForm({ ...subjectForm, description: event.target.value })}
              />
            </div>

            {!editingSubject && (
              <div className="field-group">
                <div className="field-label-row">
                  <label className="field-label">CLO khởi tạo (tuỳ chọn)</label>
                  <button
                    type="button"
                    className="btn btn--ghost"
                    onClick={() => setCreateCloDrafts((current) => [...current, { ...EMPTY_CLO }])}
                  >
                    Thêm dòng CLO
                  </button>
                </div>
                {createCloDrafts.length === 0 ? (
                  <p className="source-note">Có thể chọn CLO ngay khi tạo học phần, hoặc bổ sung sau.</p>
                ) : createCloDrafts.map((draft, index) => (
                  <div className="clo-draft-row" key={`create-clo-${index}`}>
                    <input
                      className="field-input"
                      placeholder="CLO1"
                      value={draft.clo_code}
                      onChange={(event) => setCreateCloDrafts((current) => current.map((item, i) => (
                        i === index ? { ...item, clo_code: event.target.value } : item
                      )))}
                    />
                    <select
                      className="field-select"
                      value={draft.outcome_type}
                      onChange={(event) => setCreateCloDrafts((current) => current.map((item, i) => (
                        i === index ? { ...item, outcome_type: event.target.value } : item
                      )))}
                    >
                      {OUTCOME_TYPE_OPTIONS.map((option) => (
                        <option key={option.value} value={option.value}>{option.label}</option>
                      ))}
                    </select>
                    <input
                      className="field-input"
                      placeholder="Mô tả chuẩn đầu ra"
                      value={draft.description}
                      onChange={(event) => setCreateCloDrafts((current) => current.map((item, i) => (
                        i === index ? { ...item, description: event.target.value } : item
                      )))}
                    />
                    <button
                      type="button"
                      className="btn btn--ghost"
                      onClick={() => setCreateCloDrafts((current) => current.filter((_, i) => i !== index))}
                    >
                      Xóa
                    </button>
                  </div>
                ))}
              </div>
            )}

            <label className="subject-switch">
              <input
                type="checkbox"
                checked={subjectForm.is_active}
                onChange={(event) => setSubjectForm({ ...subjectForm, is_active: event.target.checked })}
              />
              Đang sử dụng
            </label>

            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={closeSubjectModal} disabled={saving}>Hủy</button>
              <button type="submit" className="btn btn--primary" disabled={saving}>
                {saving ? 'Đang lưu...' : 'Lưu học phần'}
              </button>
            </div>
          </form>
        </div>
      )}

      {childModal && (
        <div className="modal-overlay" onClick={closeChildModal}>
          <form className="modal-card" onClick={(event) => event.stopPropagation()} onSubmit={handleSubmitChild}>
            <h3 className="modal-title">
              {childModal.mode === 'chapter'
                ? (childModal.itemId ? 'Sửa chương' : 'Thêm chương')
                : (childModal.itemId ? 'Sửa CLO' : 'Thêm CLO')}
            </h3>

            {childModal.mode === 'chapter' ? (
              <>
                <div className="field-group">
                  <label className="field-label">Mã chương</label>
                  <input
                    className="field-input"
                    value={chapterForm.chapter_code}
                    maxLength={40}
                    placeholder="C1"
                    onChange={(event) => setChapterForm({ ...chapterForm, chapter_code: event.target.value })}
                  />
                </div>
                <div className="field-group">
                  <label className="field-label">Tên chương</label>
                  <input
                    className="field-input"
                    value={chapterForm.chapter_name}
                    maxLength={200}
                    placeholder="Giới thiệu về cấu trúc dữ liệu"
                    onChange={(event) => setChapterForm({ ...chapterForm, chapter_name: event.target.value })}
                  />
                </div>
                <div className="field-group">
                  <label className="field-label">Thứ tự</label>
                  <input
                    type="number"
                    min={1}
                    className="field-input"
                    value={chapterForm.sequence_no}
                    onChange={(event) => setChapterForm({ ...chapterForm, sequence_no: event.target.value })}
                  />
                </div>
                <label className="subject-switch">
                  <input
                    type="checkbox"
                    checked={chapterForm.is_active}
                    onChange={(event) => setChapterForm({ ...chapterForm, is_active: event.target.checked })}
                  />
                  Đang sử dụng
                </label>
              </>
            ) : (
              <>
                <div className="field-group">
                  <label className="field-label">Mã CLO</label>
                  <input
                    className="field-input"
                    value={cloForm.clo_code}
                    maxLength={40}
                    placeholder="CLO1"
                    onChange={(event) => setCloForm({ ...cloForm, clo_code: event.target.value })}
                  />
                </div>
                <div className="field-group">
                  <label className="field-label">Mô tả chuẩn đầu ra</label>
                  <textarea
                    className="field-input field-textarea"
                    rows={3}
                    maxLength={500}
                    value={cloForm.description}
                    placeholder="Sinh viên có khả năng phân tích độ phức tạp thuật toán..."
                    onChange={(event) => setCloForm({ ...cloForm, description: event.target.value })}
                  />
                </div>
                <div className="field-group">
                  <label className="field-label">Loại CLO theo CTĐT</label>
                  <select
                    className="field-select"
                    value={cloForm.outcome_type || 'kien_thuc'}
                    onChange={(event) => setCloForm({ ...cloForm, outcome_type: event.target.value })}
                  >
                    {OUTCOME_TYPE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                </div>
                <div className="field-group">
                  <label className="field-label">Trọng số mục tiêu (0 - 1)</label>
                  <input
                    type="number"
                    min={0}
                    max={1}
                    step={0.05}
                    className="field-input"
                    value={cloForm.target_weight}
                    onChange={(event) => setCloForm({ ...cloForm, target_weight: event.target.value })}
                  />
                </div>
                <label className="subject-switch">
                  <input
                    type="checkbox"
                    checked={cloForm.is_active}
                    onChange={(event) => setCloForm({ ...cloForm, is_active: event.target.checked })}
                  />
                  Đang sử dụng
                </label>
              </>
            )}

            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={closeChildModal} disabled={saving}>Hủy</button>
              <button type="submit" className="btn btn--primary" disabled={saving}>
                {saving ? 'Đang lưu...' : 'Lưu'}
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}

export default SubjectManage;
