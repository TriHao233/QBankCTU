import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faClone, faTrashCan } from '@fortawesome/free-solid-svg-icons';
import { createExam, deleteExam, duplicateExam, listExams } from '../api/exams';
import { listSubjects } from '../api/catalog';
import '../css/ExamListPage.css';
import { notify } from '../utils/notify';

function refId(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  return value.id || value._id || '';
}

const STATUS_LABEL = {
  DRAFT: 'Nháp',
  READY: 'Sẵn sàng',
  FINALIZED: 'Đã chốt',
  ARCHIVED: 'Lưu trữ',
};

const LOCKED_STATUSES = new Set(['FINALIZED', 'ARCHIVED']);

function statusKey(status) {
  return String(status || 'DRAFT').toUpperCase();
}

function statusClass(status) {
  return statusKey(status).toLowerCase();
}

function ExamListPage() {
  const navigate = useNavigate();
  const [exams, setExams] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [subjects, setSubjects] = useState([]);
  const [creating, setCreating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState(null);
  const [duplicatingId, setDuplicatingId] = useState(null);

  const [name, setName] = useState('');
  const [examTitle, setExamTitle] = useState('');
  const [subjectId, setSubjectId] = useState('');
  const [questionCount, setQuestionCount] = useState(25);

  const fetchExams = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await listExams({ page: 1, pageSize: 100 });
      setExams(result.items || []);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách đề thi');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchExams();
    listSubjects().then(setSubjects).catch(() => {});
  }, []);

  const subjectLabel = (id) => {
    const subject = subjects.find((item) => (item.id || item._id) === id);
    return subject ? `${subject.subject_code} · ${subject.subject_name}` : '';
  };

  const deleteBlockedReason = (exam) => {
    if (LOCKED_STATUSES.has(statusKey(exam.status))) return 'Đề đã chốt hoặc lưu trữ, không thể xoá';
    if ((exam.variant_count || 0) > 0) return 'Đề đã có mã đề, hãy xoá mã đề trước';
    return '';
  };

  const openCreate = () => {
    setName('');
    setExamTitle('');
    setSubjectId('');
    setQuestionCount(25);
    setCreating(true);
  };

  const closeCreate = () => {
    if (saving) return;
    setCreating(false);
  };

  const handleCreate = async (e) => {
    e.preventDefault();
    if (!name.trim() || !examTitle.trim() || !subjectId) {
      notify('Vui lòng nhập đầy đủ tên đề thi, tên kỳ thi và môn học.');
      return;
    }
    const questionCountNumber = Number(questionCount);
    if (!Number.isInteger(questionCountNumber) || questionCountNumber < 1 || questionCountNumber > 200) {
      notify('Số lượng câu hỏi phải là số nguyên từ 1 đến 200.');
      return;
    }
    setSaving(true);
    try {
      const exam = await createExam({
        name: name.trim(),
        exam_title: examTitle.trim(),
        subject_id: subjectId,
        question_count: questionCountNumber,
        header: {},
      });
      setCreating(false);
      navigate(`/lam-de-thi/${exam.id}`);
    } catch (err) {
      notify('Tạo đề thi thất bại: ' + err.message);
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (exam) => {
    if (!window.confirm(`Xoá đề thi "${exam.name}"?`)) return;
    setDeletingId(exam.id);
    try {
      await deleteExam(exam.id);
      await fetchExams();
    } catch (err) {
      notify('Xoá đề thi thất bại: ' + err.message);
    } finally {
      setDeletingId(null);
    }
  };

  const handleDuplicate = async (exam) => {
    setDuplicatingId(exam.id);
    try {
      const duplicate = await duplicateExam(exam.id);
      navigate(`/lam-de-thi/${duplicate.id}`);
    } catch (err) {
      notify('Nhân bản đề thi thất bại: ' + err.message);
    } finally {
      setDuplicatingId(null);
    }
  };

  return (
    <main className="exam-list-page">
      <section className="page-hero">
        <div className="container exam-hero-row">
          <div>
            <div className="page-hero-badge">Làm đề thi</div>
            <h1 className="page-hero-title">Danh sách đề thi</h1>
            <p className="page-hero-desc">
              Tạo đề thi trắc nghiệm hoàn chỉnh từ ngân hàng câu hỏi đã được duyệt, cấu hình ma trận đề
              và xuất PDF/DOCX theo chuẩn đề thi giấy.
            </p>
          </div>
          <button type="button" className="btn btn--primary" onClick={openCreate}>
            + Tạo đề thi mới
          </button>
        </div>
      </section>

      <section className="exam-list-body">
        <div className="container">
          {error && <p className="exam-error">{error}</p>}
          {loading ? (
            <p className="empty-note">Đang tải danh sách đề thi...</p>
          ) : (
            <div className="exam-grid">
              {exams.map((exam) => (
                <div className="card exam-card" key={exam.id}>
                  <div className="exam-card-header">
                    <h3>{exam.name}</h3>
                    <span className={`status-badge status--${statusClass(exam.status)}`}>
                      {STATUS_LABEL[statusKey(exam.status)] || exam.status}
                    </span>
                  </div>
                  <p className="exam-card-meta">
                    {[exam.exam_title, subjectLabel(exam.subject_id)].filter(Boolean).join(' · ')}
                  </p>
                  <div className="exam-card-stats">
                    <span>{exam.question_selected_count ?? exam.questions?.length ?? 0}/{exam.question_count} câu hỏi</span>
                    <span>{exam.variant_count ?? 0}/4 mã đề</span>
                  </div>
                  <div className="exam-card-actions">
	                    <button type="button" className="btn btn--outline" onClick={() => navigate(`/lam-de-thi/${exam.id}`)}>
	                      Mở đề thi
	                    </button>
	                    <button
	                      type="button"
	                      className="icon-btn"
	                      title="Nhân bản"
	                      disabled={duplicatingId === exam.id}
	                      onClick={() => handleDuplicate(exam)}
	                    >
	                      <FontAwesomeIcon icon={faClone} />
	                    </button>
	                    <button
	                      type="button"
	                      className="icon-btn icon-btn--danger"
                      title={deleteBlockedReason(exam) || 'Xoá'}
                      disabled={deletingId === exam.id || Boolean(deleteBlockedReason(exam))}
                      onClick={() => handleDelete(exam)}
                    >
                      <FontAwesomeIcon icon={faTrashCan} />
                    </button>
                  </div>
                </div>
              ))}
              {exams.length === 0 && (
                <p className="empty-note">Chưa có đề thi nào. Bấm "Tạo đề thi mới" để bắt đầu.</p>
              )}
            </div>
          )}
        </div>
      </section>

      {creating && (
        <div className="modal-overlay" onClick={closeCreate}>
          <form className="modal-card" onClick={(e) => e.stopPropagation()} onSubmit={handleCreate}>
            <h3 className="profile-card-title">Tạo đề thi mới</h3>

            <div className="field-group">
              <label className="field-label">Tên đề thi</label>
              <input className="field-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Đề thi cuối kỳ - Cấu trúc dữ liệu" />
            </div>

            <div className="field-group">
              <label className="field-label">Tên kỳ thi</label>
              <input className="field-input" value={examTitle} onChange={(e) => setExamTitle(e.target.value)} placeholder="Thi cuối học kỳ I 2025-2026" />
            </div>

            <div className="field-group">
              <label className="field-label">Môn học/học phần</label>
              <select className="field-select" value={subjectId} onChange={(e) => setSubjectId(e.target.value)}>
                <option value="">Chọn môn học</option>
                {subjects.map((subject) => (
                  <option key={refId(subject)} value={refId(subject)}>
                    {subject.subject_name}
                  </option>
                ))}
              </select>
            </div>

            <div className="field-group">
              <label className="field-label">Số lượng câu hỏi</label>
              <input
                type="number"
                min={1}
                max={200}
                className="field-input"
                value={questionCount}
                onChange={(e) => setQuestionCount(e.target.value)}
              />
            </div>

            <div className="modal-actions">
              <button type="button" className="btn btn--outline" onClick={closeCreate} disabled={saving}>Huỷ</button>
              <button type="submit" className="btn btn--primary" disabled={saving}>
                {saving ? 'Đang tạo...' : 'Tạo đề thi'}
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}

export default ExamListPage;
