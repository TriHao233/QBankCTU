import React, { useContext, useEffect, useMemo, useState, useCallback } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import '../css/ContactPage.css';
import { AuthContext } from '../context/AuthContext';
import {
  CONTACT_CATEGORIES,
  CONTACT_STATUSES,
  createContactRequest,
  deleteContactRequest,
  editContactRequest,
  getContactRequest,
  labelForCategory,
  labelForStatus,
  listContactRequests,
  replyContactRequest,
  submitContactResponse,
  restoreContactRequest,
  withdrawContactRequest,
} from '../api/contact';

const CONTACT_EMAIL = 'haob2203553@student.ctu.edu.vn';
const SENDER_ROLE_LABEL = { Admin: 'Quản trị', Teacher: 'Giảng viên', Reviewer: 'Người duyệt' };

const PAGE_SIZE = 10;

function StatusBadge({ status }) {
  const cls = `contact-badge status-${status?.toLowerCase()}`;
  return <span className={cls}>{labelForStatus(status)}</span>;
}

function CategoryBadge({ category }) {
  return <span className={`contact-badge cat-${category?.toLowerCase()}`}>{labelForCategory(category)}</span>;
}

function formatDate(value) {
  if (!value) return '';
  try {
    return new Date(value).toLocaleString('vi-VN');
  } catch {
    return value;
  }
}

const CATEGORY_GUIDANCE = {
  REVIEW_REQUEST: 'Ghi mã câu hỏi và lý do cần hỗ trợ duyệt.',
  BUG: 'Nêu trang gặp lỗi, thao tác vừa thực hiện và thông báo lỗi nếu có.',
  SUPPORT: 'Cho biết bạn đang cần hỗ trợ ở bước nào.',
  FEEDBACK: 'Mô tả đề xuất và điều bạn mong muốn cải thiện.',
  CONTENT_ISSUE: 'Ghi mã câu hỏi hoặc học phần và nội dung cần kiểm tra.',
};

function CreateTicketForm({ onCreated, teacherView = false }) {
  const [category, setCategory] = useState('');
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  async function handleSubmit(e) {
    e.preventDefault();
    setError('');
    setSuccess('');
    if (!category || !title.trim() || !content.trim()) {
      setError('Vui lòng chọn loại liên hệ, nhập tiêu đề và mô tả ngắn');
      return;
    }
    setSubmitting(true);
    try {
      const created = await createContactRequest({ category, title: title.trim(), content: content.trim() });
      setSuccess(`Đã gửi yêu cầu ${created.ticket_code}`);
      setCategory('');
      setTitle('');
      setContent('');
      if (onCreated) onCreated(created);
    } catch (err) {
      setError(err.message || 'Không gửi được yêu cầu');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="contact-form" onSubmit={handleSubmit}>
      <h3 className="form-card-title">Gửi yêu cầu mới</h3>
      <p className="form-card-sub">Chọn loại liên hệ và mô tả ngắn để quản trị viên hỗ trợ nhanh nhất.</p>

      <div className="field-row-2">
        <div className="field-group">
          <label className="field-label" htmlFor="contact-category">Loại liên hệ</label>
          <select id="contact-category" className="field-select" value={category} onChange={(e) => setCategory(e.target.value)} required>
            <option value="" disabled>Chọn loại liên hệ</option>
            {CONTACT_CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>{c.label}</option>
            ))}
          </select>
          {teacherView && category && <p className="teacher-field-hint">{CATEGORY_GUIDANCE[category]}</p>}
        </div>
        <div className="field-group">
          <label className="field-label" htmlFor="contact-title">Tiêu đề</label>
          <input
            id="contact-title"
            className="field-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Tóm tắt ngắn gọn"
            maxLength={300}
            required
          />
        </div>
      </div>

      <div className="field-group">
        <label className="field-label" htmlFor="contact-description">Mô tả ngắn</label>
        <textarea
          id="contact-description"
          className="field-input"
          rows="4"
          value={content}
          onChange={(e) => setContent(e.target.value)}
          placeholder={category === 'REVIEW_REQUEST'
            ? 'Ghi mã câu hỏi cần duyệt và lý do yêu cầu...'
            : 'Mô tả ngắn vấn đề hoặc đề xuất của bạn...'}
          maxLength={1000}
          required
        />
      </div>

      {category === 'REVIEW_REQUEST' && (
        <p className="form-card-sub teacher-review-note">
          Yêu cầu này chỉ gửi lời nhắn tới quản trị viên. Để đưa câu hỏi vào hàng kiểm duyệt, dùng chức năng “Gửi duyệt” tại{' '}
          {teacherView ? <Link to="/quan-ly">Quản lý câu hỏi</Link> : 'trang Quản lý câu hỏi'}.
        </p>
      )}

      {teacherView && <p className="teacher-character-count">{content.length}/1000 ký tự</p>}

      {error && <div className="contact-alert contact-alert--error">{error}</div>}
      {success && <div className="contact-alert contact-alert--success">{success}</div>}

      <button className="btn btn--primary" type="submit" disabled={submitting}>
        {submitting ? 'Đang gửi...' : 'Gửi yêu cầu'}
      </button>
    </form>
  );
}

function TicketDetail({ ticketId, currentUser, onBack, onChanged, onDeleted, onRestored }) {
  const [detail, setDetail] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [replyText, setReplyText] = useState('');
  const [replying, setReplying] = useState(false);
  const [pendingStatus, setPendingStatus] = useState(null);
  const [editing, setEditing] = useState(false);
  const [editDraft, setEditDraft] = useState(null);
  const [acting, setActing] = useState(false);
  const [confirmAction, setConfirmAction] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await getContactRequest(ticketId);
      setDetail(data);
      setPendingStatus(data.status);
    } catch (err) {
      setError(err.message || 'Không tải được yêu cầu');
    } finally {
      setLoading(false);
    }
  }, [ticketId]);

  useEffect(() => {
    load();
  }, [load]);

  async function handleReply(e) {
    e.preventDefault();
    const message = replyText.trim();
    const status = pendingStatus !== detail?.status ? pendingStatus : undefined;
    if (!message && (!currentUser || currentUser.role !== 'Admin' || !status)) return;
    setReplying(true);
    try {
      if (currentUser?.role === 'Admin') {
        await submitContactResponse(ticketId, { status, message: message || undefined });
      } else {
        await replyContactRequest(ticketId, message);
      }
      setReplyText('');
      await load();
      if (onChanged) onChanged();
    } catch (err) {
      setError(err.message || 'Không gửi được phản hồi');
    } finally {
      setReplying(false);
    }
  }

  async function handleEdit(e) {
    e.preventDefault();
    setActing(true);
    setError('');
    try {
      const changes = canEditOwn
        ? { category: editDraft.category, title: editDraft.title.trim(), content: editDraft.content.trim() }
        : { category: editDraft.category };
      await editContactRequest(ticketId, changes);
      setEditing(false);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.message || 'Không sửa được yêu cầu');
    } finally {
      setActing(false);
    }
  }

  async function handleLifecycleAction() {
    const action = confirmAction;
    setActing(true);
    setError('');
    try {
      if (action === 'withdraw') await withdrawContactRequest(ticketId);
      if (action === 'delete') await deleteContactRequest(ticketId);
      if (action === 'restore') await restoreContactRequest(ticketId);
      setConfirmAction('');
      if (action === 'delete') onDeleted();
      else if (action === 'restore') onRestored();
      else {
        await load();
        onChanged?.();
      }
    } catch (err) {
      setError(err.message || 'Không thực hiện được thao tác');
    } finally {
      setActing(false);
    }
  }

  if (loading) return <div className="contact-panel">Đang tải chi tiết...</div>;
  if (error && !detail) return (
    <div className="contact-panel">
      <div className="contact-alert contact-alert--error">{error}</div>
      <button className="btn btn--outline" onClick={onBack}>Quay lại</button>
    </div>
  );
  if (!detail) return null;

  const isAdmin = currentUser?.role === 'Admin';
  const isOwner = String(detail.user_id) === String(currentUser?.id);
  const isDeleted = Boolean(detail.deleted_at);
  const canEditOwn = isOwner && detail.status === 'NEW' && !detail.message_count && !isDeleted;
  const canReclassify = isAdmin && !isDeleted && detail.status !== 'WITHDRAWN';
  const canWithdraw = isOwner && !isDeleted && ['NEW', 'IN_PROGRESS'].includes(detail.status);
  const canDelete = !isDeleted && (
    (isOwner && detail.status === 'NEW' && !detail.message_count)
    || ['WITHDRAWN', 'RESOLVED', 'CLOSED'].includes(detail.status)
  );
  const isClosed = isDeleted || ['CLOSED', 'WITHDRAWN'].includes(detail.status);

  return (
    <div className="contact-panel ticket-detail">
      <div className="ticket-detail__header">
        <button className="link-button" onClick={onBack}>&larr; Quay lại danh sách</button>
        <div className="ticket-detail__meta">
          <StatusBadge status={detail.status} />
          {isDeleted && <span className="contact-badge status-deleted">Đã xóa</span>}
          <CategoryBadge category={detail.category} />
        </div>
      </div>

      <h3 className="ticket-detail__title">
        <span className="ticket-code">{detail.ticket_code}</span> {detail.title}
      </h3>
      <div className="ticket-detail__submeta">
        Người gửi: <strong>{detail.user_name || detail.user_email}</strong>
        {' · '}Tạo lúc: {formatDate(detail.created_at)}
        {detail.resolved_at && (<>{' · '}Xử lý xong: {formatDate(detail.resolved_at)}</>)}
      </div>

      {error && <div className="contact-alert contact-alert--error" role="alert">{error}</div>}

      <div className="ticket-lifecycle-actions">
        {(canEditOwn || canReclassify) && !editing && (
          <button className="btn btn--outline btn--sm" type="button" onClick={() => {
            setEditDraft({ category: detail.category, title: detail.title, content: detail.content });
            setEditing(true);
          }}>{canEditOwn ? 'Sửa yêu cầu' : 'Đổi loại liên hệ'}</button>
        )}
        {canWithdraw && <button className="btn btn--outline btn--sm" type="button" onClick={() => setConfirmAction('withdraw')}>Thu hồi</button>}
        {canDelete && <button className="btn btn--outline btn--sm" type="button" onClick={() => setConfirmAction('delete')}>Xóa yêu cầu</button>}
        {isDeleted && <button className="btn btn--outline btn--sm" type="button" onClick={() => setConfirmAction('restore')}>Khôi phục</button>}
      </div>

      {editing && <form className="ticket-edit-form" onSubmit={handleEdit}>
        <h4>{canEditOwn ? 'Sửa yêu cầu' : 'Phân loại lại yêu cầu'}</h4>
        <label className="field-label" htmlFor="edit-contact-category">Loại liên hệ</label>
        <select id="edit-contact-category" className="field-select" value={editDraft.category} onChange={(e) => setEditDraft({ ...editDraft, category: e.target.value })}>
          {CONTACT_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </select>
        {canEditOwn && <>
          <label className="field-label" htmlFor="edit-contact-title">Tiêu đề</label>
          <input id="edit-contact-title" className="field-input" value={editDraft.title} maxLength={300} required onChange={(e) => setEditDraft({ ...editDraft, title: e.target.value })} />
          <label className="field-label" htmlFor="edit-contact-content">Mô tả ngắn</label>
          <textarea id="edit-contact-content" className="field-input" rows="5" value={editDraft.content} maxLength={1000} required onChange={(e) => setEditDraft({ ...editDraft, content: e.target.value })} />
        </>}
        <div className="ticket-edit-form__actions">
          <button className="btn btn--primary" type="submit" disabled={acting}>Lưu thay đổi</button>
          <button className="btn btn--outline" type="button" onClick={() => setEditing(false)}>Hủy</button>
        </div>
      </form>}

      {confirmAction && <div className="ticket-confirm-action" role="group" aria-label="Xác nhận thao tác">
        <p>{confirmAction === 'withdraw' ? 'Thu hồi yêu cầu này? Admin sẽ thấy trạng thái đã thu hồi và không thể phản hồi thêm.' : confirmAction === 'delete' ? 'Xóa yêu cầu khỏi danh sách? Yêu cầu sẽ được lưu trong mục Đã xóa và có thể khôi phục.' : 'Khôi phục yêu cầu này về danh sách?'}</p>
        <button className="btn btn--primary" type="button" disabled={acting} onClick={handleLifecycleAction}>Xác nhận</button>
        <button className="btn btn--outline" type="button" disabled={acting} onClick={() => setConfirmAction('')}>Hủy</button>
      </div>}

      <div className="ticket-detail__content">
        <div className="msg-bubble msg-bubble--origin">
          <div className="msg-bubble__author">
            {detail.user_name || 'Người gửi'}
            <span className="msg-bubble__role">{detail.user_id === currentUser?.id && isAdmin ? 'Quản trị' : 'Giảng viên'}</span>
          </div>
          <div className="msg-bubble__body-wrap">
            <div className="msg-bubble__label">Mô tả ngắn</div>
            <div className="msg-bubble__body">{detail.content}</div>
          </div>
          <div className="msg-bubble__time">{formatDate(detail.created_at)}</div>
        </div>
        {detail.messages?.map((m) => (
          <div
            key={m.id}
            className={`msg-bubble ${m.sender_role === 'Admin' ? 'msg-bubble--admin' : 'msg-bubble--user'}`}
          >
            <div className="msg-bubble__author">
              {m.sender_name || 'Người dùng'}
              <span className="msg-bubble__role">{SENDER_ROLE_LABEL[m.sender_role] || 'Người dùng'}</span>
            </div>
            <div className="msg-bubble__body-wrap">
              <div className="msg-bubble__body">{m.message}</div>
            </div>
            <div className="msg-bubble__time">{formatDate(m.created_at)}</div>
          </div>
        ))}
      </div>

      {isAdmin && !isClosed && (
        <div className="ticket-actions">
          <span className="ticket-actions__label">Trạng thái khi gửi phản hồi:</span>
          {CONTACT_STATUSES.filter((s) => s.value !== 'WITHDRAWN').map((s) => (
            <button
              key={s.value}
              className={`btn btn--chip ${pendingStatus === s.value ? 'is-active' : ''}`}
              disabled={replying || pendingStatus === s.value}
              onClick={() => setPendingStatus(s.value)}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      {detail.events?.length > 0 && <div className="ticket-events">
        <h4>Lịch sử thay đổi</h4>
        {detail.events.map((event) => <div className="ticket-event" key={event.id}>
          <strong>{({ EDITED: 'Đã sửa nội dung', RECLASSIFIED: 'Đã đổi loại liên hệ', WITHDRAWN: 'Đã thu hồi', DELETED: 'Đã xóa', RESTORED: 'Đã khôi phục' })[event.action] || event.action}</strong>
          <span>{event.actor_name || 'Người dùng'} · {formatDate(event.created_at)}</span>
        </div>)}
      </div>}

      {!isClosed ? (
        <form className="ticket-reply" onSubmit={handleReply}>
          <label className="field-label">{isAdmin ? 'Nội dung phản hồi (có thể để trống khi đổi trạng thái)' : 'Phản hồi'}</label>
          <textarea
            className="field-input"
            rows="4"
            value={replyText}
            onChange={(e) => setReplyText(e.target.value)}
            placeholder="Nhập phản hồi..."
            maxLength={5000}
          />
          <button className="btn btn--primary" type="submit" disabled={replying || (!replyText.trim() && (!isAdmin || pendingStatus === detail.status))}>
            {replying ? 'Đang gửi...' : 'Gửi phản hồi'}
          </button>
        </form>
      ) : (
        <div className="contact-alert">Yêu cầu đã đóng, không thể phản hồi thêm.</div>
      )}
    </div>
  );
}

function TicketList({ scope, currentUser, onOpen, onCreate, reloadKey, teacherView = false }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('');
  const [categoryFilter, setCategoryFilter] = useState('');
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const isAdminList = currentUser?.role === 'Admin';

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await listContactRequests({
        page,
        pageSize: PAGE_SIZE,
        status: statusFilter || undefined,
        category: categoryFilter || undefined,
        search: search.trim() || undefined,
        scope,
      });
      setItems(data.items || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err.message || 'Không tải được danh sách');
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter, categoryFilter, search, scope]);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const hasFilters = Boolean(search || statusFilter || categoryFilter);

  return (
    <div className={`contact-panel ticket-list ${isAdminList ? 'ticket-list--admin' : ''}`}>
      <div className="ticket-list__heading">
        <h2>{scope === 'deleted' ? 'Yêu cầu đã xóa' : scope === 'all' ? 'Yêu cầu gửi đến' : 'Yêu cầu của tôi'}</h2>
        {!loading && <span>{total} yêu cầu</span>}
      </div>
      <div className="ticket-list__filters">
        <input
          className="field-input"
          aria-label="Tìm yêu cầu liên hệ"
          placeholder="Tìm theo mã, tiêu đề hoặc mô tả..."
          value={search}
          onChange={(e) => { setPage(1); setSearch(e.target.value); }}
        />
        <select className="field-select" aria-label="Lọc theo trạng thái" value={statusFilter} onChange={(e) => { setPage(1); setStatusFilter(e.target.value); }}>
          <option value="">Mọi trạng thái</option>
          {CONTACT_STATUSES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
        </select>
        <select className="field-select" aria-label="Lọc theo loại liên hệ" value={categoryFilter} onChange={(e) => { setPage(1); setCategoryFilter(e.target.value); }}>
          <option value="">Mọi loại</option>
          {CONTACT_CATEGORIES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
        </select>
        {isAdminList && <button className="btn btn--outline contact-clear-filter" type="button" disabled={!hasFilters} onClick={() => {
          setSearch(''); setStatusFilter(''); setCategoryFilter(''); setPage(1);
        }}>Bỏ lọc</button>}
      </div>

      {error && <div className="contact-alert contact-alert--error">{error}</div>}
      {loading ? (
        <div className="contact-empty">Đang tải...</div>
      ) : items.length === 0 ? (
        <div className="contact-empty">
          <p>{hasFilters ? 'Không tìm thấy yêu cầu phù hợp.' : scope === 'deleted' ? 'Chưa có yêu cầu đã xóa.' : isAdminList ? 'Chưa có yêu cầu gửi đến.' : 'Bạn chưa gửi yêu cầu nào.'}</p>
          {teacherView && scope !== 'deleted' && !search && !statusFilter && !categoryFilter && (
            <button className="btn btn--primary" type="button" onClick={onCreate}>Gửi yêu cầu đầu tiên</button>
          )}
        </div>
      ) : teacherView ? (
        <div className="teacher-ticket-cards">
          {items.map((t) => (
            <button className="teacher-ticket-card" type="button" key={t.id} onClick={() => onOpen(t.id)}>
              <span className="teacher-ticket-card__top">
                <span className="ticket-code">{t.ticket_code}</span>
                {scope === 'deleted' ? <span className="contact-badge status-deleted">Đã xóa</span> : <StatusBadge status={t.status} />}
              </span>
              <strong className="teacher-ticket-card__title">{t.title}</strong>
              <span className="teacher-ticket-card__description">{t.content}</span>
              <span className="teacher-ticket-card__bottom">
                <CategoryBadge category={t.category} />
                <span>Cập nhật {formatDate(t.updated_at)}</span>
                <span className="teacher-ticket-card__arrow" aria-hidden="true">→</span>
              </span>
            </button>
          ))}
        </div>
      ) : (
        <div className="ticket-table-wrap"><table className="ticket-table">
          <colgroup>
            <col className="col-code" />
            <col className="col-title" />
            <col className="col-cat" />
            {(scope === 'all' || scope === 'deleted') && <col className="col-sender" />}
            <col className="col-status" />
            <col className="col-date" />
            <col className="col-action" />
          </colgroup>
          <thead>
            <tr>
              <th>Mã</th>
              <th>Tiêu đề &amp; Mô tả</th>
              <th>Loại</th>
              {(scope === 'all' || scope === 'deleted') && <th>Người gửi</th>}
              <th>Trạng thái</th>
              <th>Cập nhật</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((t) => (
              <tr
                key={t.id}
                className="ticket-row"
                tabIndex={0}
                aria-label={`Mở yêu cầu ${t.ticket_code}: ${t.title}`}
                onClick={() => onOpen(t.id)}
                onKeyDown={(event) => {
                  if (event.target !== event.currentTarget) return;
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onOpen(t.id);
                  }
                }}
              >
                <td data-label="Mã" className="ticket-code">{t.ticket_code}</td>
                <td data-label="Tiêu đề">
                  <span className="ticket-compound">
                    <span className="ticket-compound__title">{t.title}</span>
                    <span className="ticket-compound__desc">{t.content}</span>
                  </span>
                </td>
                <td data-label="Loại"><CategoryBadge category={t.category} /></td>
                {(scope === 'all' || scope === 'deleted') && (
                  <td data-label="Người gửi" className="ticket-sender-cell">{t.user_name || t.user_email}</td>
                )}
                <td data-label="Trạng thái">
                  {scope === 'deleted'
                    ? <span className="contact-badge status-deleted">Đã xóa</span>
                    : <StatusBadge status={t.status} />}
                </td>
                <td data-label="Cập nhật" className="ticket-date-cell">{formatDate(t.updated_at)}</td>
                <td className="ticket-action-cell">
                  <button
                    className="btn-view-detail"
                    type="button"
                    aria-label={`Xem chi tiết yêu cầu ${t.ticket_code}: ${t.title}`}
                    onClick={(event) => { event.stopPropagation(); onOpen(t.id); }}
                  >
                    Chi tiết
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}

      {totalPages > 1 && (
        <div className="ticket-pagination">
          <button className="btn btn--outline btn--sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>Trước</button>
          <span>Trang {page} / {totalPages}</span>
          <button className="btn btn--outline btn--sm" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>Sau</button>
        </div>
      )}
    </div>
  );
}

const IconEmail = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="4" width="20" height="16" rx="2" /><path d="M22 6l-10 7L2 6" /></svg>
);
const IconPhone = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" /></svg>
);
const IconPin = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" /><circle cx="12" cy="10" r="3" /></svg>
);

function PublicContactInfo({ teacherView = false }) {
  const projectLeadCard = (
    <div className="info-card" key="project-lead">
      <span className="info-label">Chủ nhiệm đề tài</span>
      <h3>Trương Trí Hào</h3>
      <p className="info-card__sub">B2203553 · Lớp DI2296F1</p>
      <p className="info-card__detail">Kỹ Thuật Phần Mềm (CT. CLC) · Khoá 48</p>
      <ul className="info-list">
        <li><IconEmail /><a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a></li>
        <li><IconPhone /><span>0399 348 365</span></li>
      </ul>
    </div>
  );

  const supervisorCard = (
    <div className="info-card info-card--muted" key="supervisor">
      <span className="info-label">Cán bộ hướng dẫn</span>

      <div className="supervisor-entry">
        <h3>TS. Phan Phương Lan</h3>
        <p className="info-card__detail">Tiến sĩ, GVCC Khoa Công Nghệ Phần Mềm, Trường Công Nghệ Thông Tin &amp; Truyền Thông, Đại học Cần Thơ</p>
      </div>

      <div className="supervisor-divider" />

      <div className="supervisor-entry">
        <h3>KS. Trương Phúc Vĩnh</h3>
        <p className="info-card__detail">Kỹ sư ngành Kỹ Thuật Phần Mềm, Trường Công Nghệ Thông Tin &amp; Truyền Thông, Đại học Cần Thơ</p>
      </div>
    </div>
  );

  return (
    <div className="contact-info">
      <div className="info-card">
        <span className="info-label">Đơn vị thực hiện</span>
        <h3>Trường Công Nghệ Thông Tin &amp; Truyền Thông</h3>
        <p className="info-card__sub">Đại học Cần Thơ</p>
        <ul className="info-list">
          <li>
            <IconPin />
            <span>Khu II, Đường 3/2, phường Xuân Khánh, quận Ninh Kiều, TP Cần Thơ</span>
          </li>
          <li className="info-list__item-stack">
            <IconEmail />
            <span>
              <small className="info-list__label">Email tiếp nhận liên hệ</small>
              <a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a>
            </span>
          </li>
        </ul>
      </div>

      {teacherView ? [supervisorCard, projectLeadCard] : [projectLeadCard, supervisorCard]}
    </div>
  );
}

function TeacherTicketConfirmation({ ticket, onOpen, onBack }) {
  return (
    <div className="contact-panel teacher-confirmation" role="status">
      <span className="teacher-confirmation__mark" aria-hidden="true">✓</span>
      <p className="teacher-confirmation__eyebrow">Đã gửi thành công</p>
      <h2>Yêu cầu của bạn đã được ghi nhận</h2>
      <p>Quản trị viên sẽ xem và phản hồi tại trang Liên hệ. Bạn cũng sẽ nhận được thông báo khi có phản hồi.</p>
      <div className="teacher-confirmation__code">Mã yêu cầu <strong>{ticket.ticket_code}</strong></div>
      <div className="teacher-confirmation__actions">
        <button className="btn btn--primary" type="button" onClick={() => onOpen(ticket.id)}>Xem yêu cầu</button>
        <button className="btn btn--outline" type="button" onClick={onBack}>Về danh sách</button>
      </div>
    </div>
  );
}

const StatIconDoc = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/><line x1="10" y1="9" x2="8" y2="9"/>
  </svg>
);
const StatIconClock = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
  </svg>
);
const StatIconCheck = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
  </svg>
);
const StatIconBell = () => (
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>
  </svg>
);

function AdminContactOverview({ reloadKey }) {
  const [counts, setCounts] = useState(null);

  useEffect(() => {
    let active = true;
    Promise.all([
      listContactRequests({ scope: 'all', pageSize: 1 }),
      listContactRequests({ scope: 'all', status: 'IN_PROGRESS', pageSize: 1 }),
      listContactRequests({ scope: 'all', status: 'RESOLVED', pageSize: 1 }),
      listContactRequests({ scope: 'all', status: 'NEW', pageSize: 1 }),
    ]).then(([all, progress, resolved, fresh]) => {
      if (active) setCounts([all.total, progress.total, resolved.total, fresh.total]);
    }).catch(() => { if (active) setCounts(null); });
    return () => { active = false; };
  }, [reloadKey]);

  const cards = [
    { label: 'Tổng yêu cầu',   Icon: StatIconDoc,   tone: 'blue'  },
    { label: 'Đang xử lý',     Icon: StatIconClock, tone: 'amber' },
    { label: 'Đã hoàn thành',  Icon: StatIconCheck, tone: 'green' },
    { label: 'Yêu cầu mới',    Icon: StatIconBell,  tone: 'cyan'  },
  ];

  return (
    <div className="admin-contact-stats" aria-label="Thống kê yêu cầu liên hệ">
      {cards.map(({ label, Icon, tone }, i) => (
        <div className={`admin-contact-stat admin-contact-stat--${tone}`} key={label}>
          <span className="admin-contact-stat__icon" aria-hidden="true"><Icon /></span>
          <span className="admin-contact-stat__text">
            <strong>{counts?.[i] ?? '—'}</strong>
            <small>{label}</small>
          </span>
        </div>
      ))}
    </div>
  );
}

function AdminContactSidebar() {
  return <aside className="admin-contact-sidebar" aria-label="Thông tin và hướng dẫn liên hệ">
    <section className="admin-contact-sidecard">
      <h2><span className="admin-contact-sidecard__icon" aria-hidden="true">⌂</span>Thông tin liên hệ</h2>
      <strong><span>Trường Công Nghệ Thông Tin</span><span>&amp; Truyền Thông</span></strong>
      <p>Đại học Cần Thơ</p>
      <div className="admin-contact-sidecard__line"><IconPin /><span>Khu II, Đường 3/2, phường Xuân Khánh, quận Ninh Kiều, TP Cần Thơ</span></div>
      <div className="admin-contact-sidecard__line admin-contact-sidecard__line--email"><IconEmail /><span><a href={`mailto:${CONTACT_EMAIL}`}>{CONTACT_EMAIL}</a><small>Email tiếp nhận liên hệ</small></span></div>
    </section>
    <section className="admin-contact-sidecard">
      <h2><span className="admin-contact-sidecard__icon" aria-hidden="true">?</span>Quy trình xử lý</h2>
      <ol className="admin-contact-steps">
        <li><strong>Tiếp nhận</strong><span>Đọc yêu cầu và kiểm tra nội dung người gửi cung cấp.</span></li>
        <li><strong>Phân loại</strong><span>Điều chỉnh loại liên hệ khi cần để dễ theo dõi.</span></li>
        <li><strong>Phản hồi</strong><span>Trao đổi trực tiếp trong từng yêu cầu.</span></li>
        <li><strong>Hoàn tất</strong><span>Cập nhật trạng thái sau khi đã xử lý.</span></li>
      </ol>
    </section>
  </aside>;
}

function ContactPage() {
  const { user, loading: authLoading } = useContext(AuthContext);
  const [searchParams, setSearchParams] = useSearchParams();
  const openTicketId = searchParams.get('ticket');
  const initialTab = searchParams.get('tab');
  const isAdmin = !authLoading && user?.role === 'Admin';
  const isMember = !authLoading && ['Teacher', 'Reviewer'].includes(user?.role);
  const canUseTickets = isAdmin || isMember;

  const [tab, setTab] = useState(isAdmin ? 'all' : 'mine');
  const [reloadKey, setReloadKey] = useState(0);
  const [createdTicket, setCreatedTicket] = useState(null);

  useEffect(() => {
    if (isAdmin) {
      if (initialTab === 'deleted') setTab('deleted');
      else if (initialTab === 'new') setTab('new');
      else setTab('all');
    } else if (canUseTickets) {
      if (initialTab === 'deleted') setTab('deleted');
      else if (initialTab === 'new') setTab('new');
      else setTab('mine');
    }
  }, [initialTab, isAdmin, canUseTickets]);

  const setOpenTicket = useCallback((ticketId) => {
    setCreatedTicket(null);
    const next = new URLSearchParams(searchParams);
    if (ticketId) next.set('ticket', ticketId);
    else next.delete('ticket');
    setSearchParams(next, { replace: true });
  }, [searchParams, setSearchParams]);

  const selectTab = (key) => {
    setCreatedTicket(null);
    setTab(key);
    const next = new URLSearchParams(searchParams);
    if (key === 'mine') next.delete('tab');
    else next.set('tab', key);
    next.delete('ticket');
    setSearchParams(next, { replace: true });
  };

  const bump = () => setReloadKey((k) => k + 1);

  const tabs = useMemo(() => {
    if (isAdmin) {
      return [
        { key: 'all', label: 'Yêu cầu gửi đến' },
        { key: 'deleted', label: 'Đã xóa' },
      ];
    }
    const list = [];
    if (canUseTickets) {
      list.push({ key: 'mine', label: 'Yêu cầu của tôi' });
      list.push({ key: 'new', label: 'Gửi yêu cầu mới' });
      list.push({ key: 'deleted', label: 'Đã xóa' });
    }
    return list;
  }, [canUseTickets, isAdmin]);

  return (
    <main className={`contact-page ${isAdmin ? 'contact-page--admin' : ''} ${isMember ? 'contact-page--teacher' : ''}`}>
      <section className="page-hero">
        <div className="container">
          <div className="page-hero-badge">Liên hệ</div>
          <h1 className="page-hero-title">{isAdmin ? 'Quản lý liên hệ' : isMember ? 'Trung tâm hỗ trợ' : 'Kết nối với nhóm nghiên cứu QBankCTU'}</h1>
          <p className="page-hero-desc">
            {isAdmin
              ? 'Tiếp nhận, phân loại và phản hồi các yêu cầu hỗ trợ từ giảng viên và người duyệt.'
              : canUseTickets
              ? 'Gửi yêu cầu cho quản trị viên và theo dõi phản hồi của bạn tại đây.'
              : 'Góp ý về đề tài hoặc đề xuất hợp tác, vui lòng gửi email cho nhóm nghiên cứu. Người dùng hệ thống đăng nhập để gửi và theo dõi yêu cầu hỗ trợ.'}
          </p>
        </div>
      </section>

      <section className="contact-body">
        {isAdmin && <div className="container"><AdminContactOverview reloadKey={reloadKey} /></div>}
        <div className="container contact-grid">

          {isMember ? (
            <details className="teacher-contact-other" open>
              <summary>
                <span className="teacher-contact-other__heading">
                  <strong>Thông tin liên hệ khác</strong>
                  <small>Kênh liên hệ của đơn vị và nhóm thực hiện</small>
                </span>
                <span className="teacher-contact-other__toggle" aria-hidden="true">⌄</span>
              </summary>
              <PublicContactInfo teacherView />
            </details>
          ) : isAdmin ? <AdminContactSidebar /> : <PublicContactInfo />}

          <div className="contact-right">
            {!canUseTickets ? (
              <div className="contact-panel contact-anonymous">
                <h3 className="form-card-title">Gửi yêu cầu hỗ trợ</h3>
                <p className="form-card-sub">
                  Giảng viên và người duyệt đăng nhập để tạo, theo dõi yêu cầu hỗ trợ và nhận phản hồi từ quản trị viên ngay trong hệ thống.
                </p>
                <div className="contact-anonymous__actions">
                  <Link to="/dang-nhap" state={{ from: '/lien-he' }} className="btn btn--primary">Đăng nhập để gửi yêu cầu</Link>
                  <a href={`mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent('[QBankCTU] Góp ý / hợp tác')}`} className="btn btn--outline">Gửi email cho nhóm</a>
                </div>
              </div>
            ) : (
              <>
                <div className="contact-tabs" aria-label="Điều hướng yêu cầu liên hệ">
                  {tabs.map((t) => (
                    <button
                      key={t.key}
                      type="button"
                      className={`contact-tab ${tab === t.key ? 'is-active' : ''}`}
                      aria-current={tab === t.key ? 'page' : undefined}
                      onClick={() => selectTab(t.key)}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>

                {openTicketId ? (
                  <TicketDetail
                    ticketId={openTicketId}
                    currentUser={user}
                    onBack={() => {
                      if (isMember) selectTab(tab === 'deleted' ? 'deleted' : 'mine');
                      else setOpenTicket(null);
                    }}
                    onChanged={bump}
                    onDeleted={() => { selectTab('deleted'); bump(); }}
                    onRestored={() => { selectTab(isAdmin ? 'all' : 'mine'); bump(); }}
                  />
                ) : isMember && createdTicket ? (
                  <TeacherTicketConfirmation
                    ticket={createdTicket}
                    onOpen={setOpenTicket}
                    onBack={() => selectTab('mine')}
                  />
                ) : tab === 'new' ? (
                  <CreateTicketForm
                    teacherView={isMember}
                    onCreated={(created) => {
                      if (isMember) setCreatedTicket(created);
                      else selectTab('all');
                      bump();
                    }}
                  />
                ) : (
                  <TicketList
                    scope={tab === 'all' || tab === 'deleted' ? tab : 'mine'}
                    currentUser={user}
                    onOpen={setOpenTicket}
                    onCreate={() => selectTab('new')}
                    reloadKey={reloadKey}
                    teacherView={isMember}
                  />
                )}
              </>
            )}
          </div>
        </div>
      </section>
    </main>
  );
}

export default ContactPage;
