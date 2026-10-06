import React, { useContext, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import {
  faArrowsRotate,
  faBook,
  faFileLines,
  faMagnifyingGlass,
  faPen,
  faRotateRight,
  faTrashCan,
  faUpload,
  faXmark,
} from '@fortawesome/free-solid-svg-icons';
import {
  deleteDocument,
  listDocumentJobs,
  listDocumentPages,
  listDocuments,
  reindexDocument,
  retryDocumentJob,
  updateDocument,
} from '../api/documents';
import { listSubjects } from '../api/catalog';
import { canAccessPath } from '../auth/permissions';
import { AuthContext } from '../context/AuthContext';
import '../css/DocumentManagePage.css';

const PAGE_SIZE = 12;
const STATUS_OPTIONS = [
  ['', 'Tất cả trạng thái'],
  ['READY', 'Sẵn sàng'],
  ['PROCESSING', 'Đang xử lý'],
  ['UPLOADED', 'Đã tải lên'],
  ['FAILED', 'Xử lý lỗi'],
];
const STATUS_LABELS = Object.fromEntries(STATUS_OPTIONS.slice(1));

function idOf(value) {
  if (!value) return '';
  return typeof value === 'string' ? value : value.id || value._id || '';
}

function documentSubjectIds(document) {
  if (Array.isArray(document.subject_ids)) {
    return [...new Set(document.subject_ids.map(idOf).filter(Boolean))];
  }
  const single = idOf(document.subject_id);
  return single ? [single] : [];
}

function formatDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('vi-VN');
}

function DocumentManagePage() {
  const navigate = useNavigate();
  const { user } = useContext(AuthContext);
  const [documents, setDocuments] = useState([]);
  const [subjects, setSubjects] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState('');
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('');
  const [subjectId, setSubjectId] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [editing, setEditing] = useState(null);
  const [editTitle, setEditTitle] = useState('');
  const [editSubjectIds, setEditSubjectIds] = useState([]);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState('');
  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);

  const subjectById = useMemo(
    () => new Map(subjects.map((subject) => [idOf(subject), subject])),
    [subjects],
  );
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const loadDocuments = async () => {
    setLoading(true);
    setError('');
    try {
      const result = await listDocuments({
        page,
        pageSize: PAGE_SIZE,
        status: status || undefined,
        search: search || undefined,
        subjectId: subjectId || undefined,
      });
      setDocuments(result.items || []);
      setTotal(result.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách tài liệu');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    listSubjects().then(setSubjects).catch((err) => setError(err.message || 'Không tải được học phần'));
  }, []);

  useEffect(() => {
    loadDocuments();
  }, [page, search, status, subjectId]); // eslint-disable-line react-hooks/exhaustive-deps

  const canManage = (document) => (
    user?.role === 'Admin' || idOf(document.uploaded_by_user_id) === idOf(user?.id || user?._id)
  );

  const openEdit = (document) => {
    setEditing(document);
    setEditTitle(document.title || '');
    setEditSubjectIds(documentSubjectIds(document));
  };

  const toggleSubject = (nextSubjectId) => {
    setEditSubjectIds((current) => (
      current.includes(nextSubjectId)
        ? current.filter((value) => value !== nextSubjectId)
        : [...current, nextSubjectId]
    ));
  };

  const saveEdit = async (event) => {
    event.preventDefault();
    if (!editing || !editTitle.trim()) return;
    setSaving(true);
    try {
      await updateDocument(editing.id, {
        title: editTitle.trim(),
        subject_ids: editSubjectIds,
      });
      setEditing(null);
      setNotice('Đã cập nhật tên và học phần của tài liệu.');
      await loadDocuments();
    } catch (err) {
      setError(err.message || 'Không cập nhật được tài liệu');
    } finally {
      setSaving(false);
    }
  };

  const removeDocument = async (document) => {
    if (!window.confirm(`Xóa tài liệu “${document.title}” khỏi danh sách sử dụng?`)) return;
    setBusyId(document.id);
    try {
      await deleteDocument(document.id);
      setNotice('Đã xóa tài liệu khỏi danh sách sử dụng.');
      await loadDocuments();
    } catch (err) {
      setError(err.message || 'Không thể xóa tài liệu');
    } finally {
      setBusyId('');
    }
  };

  const reindex = async (document) => {
    setBusyId(document.id);
    try {
      await reindexDocument(document.id);
      setNotice('Đã đưa tác vụ lập chỉ mục lại vào hàng đợi.');
      await loadDocuments();
    } catch (err) {
      setError(err.message || 'Không thể lập chỉ mục lại tài liệu');
    } finally {
      setBusyId('');
    }
  };

  const retryProcessing = async (document) => {
    setBusyId(document.id);
    setError('');
    try {
      const result = await listDocumentJobs(document.id, { limit: 20 });
      const retryable = (result.items || []).filter((item) => item.can_retry);
      const job = retryable.find((item) => item.id === document.latest_error?.job_id) || retryable[0];
      if (!job) {
        setError('Không có tác vụ lỗi nào có thể chạy lại cho tài liệu này.');
        return;
      }
      await retryDocumentJob(document.id, job.id);
      setNotice(`Đã đưa tác vụ ${job.job_type || 'xử lý'} của “${document.title}” vào hàng đợi chạy lại.`);
      await loadDocuments();
    } catch (err) {
      setError(err.message || 'Không thể chạy lại xử lý tài liệu');
    } finally {
      setBusyId('');
    }
  };

  const openPreview = async (document) => {
    setPreview({ document, pages: [] });
    setPreviewLoading(true);
    try {
      const result = await listDocumentPages(document.id, { limit: 100 });
      setPreview({ document, pages: result.items || [] });
    } catch (err) {
      setError(err.message || 'Không tải được nội dung OCR');
      setPreview(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  return (
    <main className="document-manage-page">
      <header className="document-manage-hero">
        <div>
          <p className="document-manage-eyebrow">Thư viện học liệu</p>
          <h1>Quản lý tài liệu</h1>
          <p>Quản lý tài liệu đã tải lên, nội dung OCR và phạm vi sử dụng giữa các học phần.</p>
        </div>
        {canAccessPath(user, '/sinh-cau-hoi') && (
          <button type="button" className="document-primary-btn" onClick={() => navigate('/sinh-cau-hoi')} title="Tài liệu được tải lên ở bước đầu của trang Sinh câu hỏi">
            <FontAwesomeIcon icon={faUpload} /> Tải tài liệu mới
          </button>
        )}
      </header>

      <section className="document-stats" aria-label="Thống kê tài liệu">
        <div><b>{total}</b><span>Tài liệu tìm thấy</span></div>
        <div><b>{documents.filter((item) => item.status === 'READY').length}</b><span>Sẵn sàng trên trang này</span></div>
        <div><b>{subjects.filter((item) => item.is_active !== false).length}</b><span>Học phần đang dùng</span></div>
      </section>

      <section className="document-toolbar">
        <form onSubmit={(event) => { event.preventDefault(); setPage(1); setSearch(searchInput.trim()); }}>
          <FontAwesomeIcon icon={faMagnifyingGlass} />
          <input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Tìm theo tên tài liệu hoặc tên file gốc (nhấn Enter hoặc Tìm)" />
          <button type="submit" className="document-search-btn">Tìm</button>
        </form>
        <select value={subjectId} onChange={(event) => { setPage(1); setSubjectId(event.target.value); }}>
          <option value="">Tất cả học phần</option>
          {subjects.filter((item) => item.is_active !== false).map((subject) => (
            <option key={idOf(subject)} value={idOf(subject)}>{subject.subject_name}</option>
          ))}
        </select>
        <select value={status} onChange={(event) => { setPage(1); setStatus(event.target.value); }}>
          {STATUS_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </section>

      {notice && <div className="document-notice">{notice}<button type="button" onClick={() => setNotice('')}><FontAwesomeIcon icon={faXmark} /></button></div>}
      {error && <div className="document-error">{error}<button type="button" onClick={() => setError('')}><FontAwesomeIcon icon={faXmark} /></button></div>}

      <section className="document-grid">
        {loading ? <p className="document-empty">Đang tải tài liệu…</p> : documents.map((document) => {
          const assignedSubjects = documentSubjectIds(document).map((id) => subjectById.get(id)).filter(Boolean);
          const manageable = canManage(document);
          const canReindex = document.pipeline_summary?.chunk_status === 'COMPLETED';
          const failed = document.status === 'FAILED';
          return (
            <article className="document-card" key={document.id}>
              <div className="document-card-head">
                <span className="document-file-icon"><FontAwesomeIcon icon={faFileLines} /></span>
                <span className={`document-status document-status--${String(document.status).toLowerCase()}`}>
                  {STATUS_LABELS[document.status] || document.status}
                </span>
              </div>
              <h2>{document.title}</h2>
              <p className="document-original">File gốc: {document.original_filename}</p>
              <div className="document-meta">
                <span>{document.page_count || 0} trang</span>
                <span>Cập nhật {formatDate(document.updated_at)}</span>
              </div>
              <div className="document-subjects">
                {assignedSubjects.length > 0 ? assignedSubjects.map((subject) => (
                  <span key={idOf(subject)}><FontAwesomeIcon icon={faBook} /> {subject.subject_name}</span>
                )) : <em>Chưa gắn học phần</em>}
              </div>
              {failed && (document.latest_error?.message || document.latest_error?.detail) && (
                <p className="document-error-line">Lỗi xử lý: {document.latest_error.message || document.latest_error.detail}</p>
              )}
              <div className="document-card-actions">
                <button type="button" onClick={() => openPreview(document)}><FontAwesomeIcon icon={faFileLines} /> Xem OCR</button>
                <button type="button" disabled={!manageable} onClick={() => openEdit(document)}><FontAwesomeIcon icon={faPen} /> Chỉnh sửa</button>
                {failed ? (
                  <button type="button" disabled={!manageable || busyId === document.id} onClick={() => retryProcessing(document)}><FontAwesomeIcon icon={faRotateRight} /> Chạy lại xử lý</button>
                ) : (
                  <button type="button" disabled={!manageable || !canReindex || busyId === document.id} onClick={() => reindex(document)} title={canReindex ? 'Tạo lại chỉ mục tìm kiếm từ các đoạn đã tách' : 'Tài liệu cần xử lý xong trước khi lập chỉ mục lại'}><FontAwesomeIcon icon={faArrowsRotate} /> Lập chỉ mục lại</button>
                )}
                <button type="button" className="danger" disabled={!manageable || busyId === document.id} onClick={() => removeDocument(document)}><FontAwesomeIcon icon={faTrashCan} /> Xóa</button>
              </div>
            </article>
          );
        })}
        {!loading && documents.length === 0 && <p className="document-empty">Không có tài liệu phù hợp với bộ lọc.</p>}
      </section>

      {pageCount > 1 && (
        <nav className="document-pagination" aria-label="Phân trang">
          <button type="button" disabled={page === 1} onClick={() => setPage((value) => value - 1)}>‹ Trước</button>
          <span>Trang {page}/{pageCount}</span>
          <button type="button" disabled={page === pageCount} onClick={() => setPage((value) => value + 1)}>Sau ›</button>
        </nav>
      )}

      {editing && (
        <div className="document-modal-backdrop" onClick={() => !saving && setEditing(null)}>
          <form className="document-modal" onSubmit={saveEdit} onClick={(event) => event.stopPropagation()}>
            <div className="document-modal-head"><h2>Chỉnh sửa tài liệu</h2><button type="button" onClick={() => setEditing(null)}><FontAwesomeIcon icon={faXmark} /></button></div>
            <label>Tên tài liệu<input value={editTitle} maxLength={300} onChange={(event) => setEditTitle(event.target.value)} required /></label>
            <p className="document-origin-note">Tên file gốc được giữ nguyên để đảm bảo truy vết: <b>{editing.original_filename}</b></p>
            <fieldset>
              <legend>Học phần sử dụng</legend>
              <p>Có thể chọn nhiều học phần. Học phần đầu tiên là học phần chính.</p>
              <div className="document-subject-picker">
                {subjects.filter((item) => item.is_active !== false).map((subject) => (
                  <label key={idOf(subject)}>
                    <input type="checkbox" checked={editSubjectIds.includes(idOf(subject))} onChange={() => toggleSubject(idOf(subject))} />
                    <span>{subject.subject_code ? `${subject.subject_code} · ` : ''}{subject.subject_name}</span>
                  </label>
                ))}
              </div>
            </fieldset>
            <div className="document-modal-actions"><button type="button" onClick={() => setEditing(null)}>Hủy</button><button type="submit" className="primary" disabled={saving}>{saving ? 'Đang lưu…' : 'Lưu thay đổi'}</button></div>
          </form>
        </div>
      )}

      {preview && (
        <div className="document-modal-backdrop" onClick={() => setPreview(null)}>
          <section className="document-modal document-preview" onClick={(event) => event.stopPropagation()}>
            <div className="document-modal-head"><div><h2>{preview.document.title}</h2><p>Nội dung đã OCR</p></div><button type="button" onClick={() => setPreview(null)}><FontAwesomeIcon icon={faXmark} /></button></div>
            <div className="document-preview-pages">
              {previewLoading ? <p>Đang tải nội dung…</p> : preview.pages.map((item) => (
                <article key={item.id}><b>Trang {item.page_number || item.unit_number || '—'}</b><p>{item.cleaned_text || item.raw_text || 'Trang chưa có nội dung.'}</p></article>
              ))}
              {!previewLoading && preview.pages.length === 0 && <p>Chưa có trang OCR.</p>}
              {!previewLoading && preview.pages.length >= 100 && (
                <p className="document-preview-note">
                  Chỉ hiển thị 100 trang đầu. Xem đầy đủ nội dung OCR trong module Quản lý câu hỏi.
                </p>
              )}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

export default DocumentManagePage;
