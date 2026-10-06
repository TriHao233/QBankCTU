import React, { useContext, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  createCalendarTask,
  deleteCalendarTask,
  getMyCalendar,
  updateCalendarTask,
} from '../api/users';
import { AuthContext } from '../context/AuthContext';
import { canAccessPath, hasEffectivePermission } from '../auth/permissions';
import '../css/TaskCalendarPage.css';

const PRIORITY_LABEL = { low: 'Thấp', medium: 'Trung bình', high: 'Cao' };
const STATUS_LABEL = { todo: 'Cần làm', done: 'Đã xong', overdue: 'Quá hạn' };
const VIEW_TABS = [
  { id: 'list', label: 'Danh sách' },
  { id: 'week', label: 'Tuần' },
  { id: 'month', label: 'Tháng' },
];

function formatDateTime(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString('vi-VN', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDate(value) {
  if (!value) return '—';
  return new Date(value).toLocaleDateString('vi-VN', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  });
}

function startOfWeek(date) {
  const d = new Date(date);
  const day = (d.getDay() + 6) % 7; // Monday = 0
  d.setDate(d.getDate() - day);
  d.setHours(0, 0, 0, 0);
  return d;
}

function isSameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function eventPrimaryDate(item) {
  return item.due_date || item.date;
}

function TaskModal({ initial, onClose, onSave, onDelete }) {
  const [form, setForm] = useState(() => ({
    title: initial?.title || '',
    description: initial?.description || '',
    priority: initial?.priority || 'medium',
    due_date: initial?.due_date ? new Date(initial.due_date).toISOString().slice(0, 16) : '',
    status: initial?.status === 'done' ? 'done' : 'todo',
  }));
  const [error, setError] = useState(null);
  const [isSaving, setIsSaving] = useState(false);
  const isEdit = Boolean(initial?.id);

  const handleChange = (field) => (e) => {
    setForm((prev) => ({ ...prev, [field]: e.target.value }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!form.title.trim()) {
      setError('Tiêu đề không được để trống.');
      return;
    }
    setIsSaving(true);
    setError(null);
    try {
      const payload = {
        title: form.title.trim(),
        description: form.description.trim(),
        priority: form.priority,
        due_date: form.due_date ? new Date(form.due_date).toISOString() : null,
      };
      if (isEdit) {
        await onSave(initial.id, { ...payload, status: form.status });
      } else {
        await onSave(null, payload);
      }
      onClose();
    } catch (err) {
      setError(err.message || 'Lưu việc cần làm thất bại.');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <form className="modal-card" onClick={(e) => e.stopPropagation()} onSubmit={handleSubmit}>
        <h3 className="profile-card-title">{isEdit ? 'Sửa việc cần làm' : 'Tạo việc mới'}</h3>
        {error && <div className="profile-banner profile-banner--error">{error}</div>}

        <div className="field-group">
          <label className="field-label">Tiêu đề</label>
          <input className="field-input" value={form.title} onChange={handleChange('title')} disabled={isSaving} />
        </div>

        <div className="field-group">
          <label className="field-label">Mô tả ngắn</label>
          <textarea
            className="field-input"
            rows={3}
            value={form.description}
            onChange={handleChange('description')}
            disabled={isSaving}
          />
        </div>

        <div className="field-row-2">
          <div className="field-group">
            <label className="field-label">Mức ưu tiên</label>
            <select className="field-select" value={form.priority} onChange={handleChange('priority')} disabled={isSaving}>
              <option value="low">Thấp</option>
              <option value="medium">Trung bình</option>
              <option value="high">Cao</option>
            </select>
          </div>
          <div className="field-group">
            <label className="field-label">Hạn hoàn thành</label>
            <input
              className="field-input"
              type="datetime-local"
              value={form.due_date}
              onChange={handleChange('due_date')}
              disabled={isSaving}
            />
          </div>
        </div>

        {isEdit && (
          <div className="field-group">
            <label className="field-label">Trạng thái</label>
            <select className="field-select" value={form.status} onChange={handleChange('status')} disabled={isSaving}>
              <option value="todo">Cần làm</option>
              <option value="done">Đã xong</option>
            </select>
          </div>
        )}

        <div className="modal-actions">
          {isEdit && (
            <button
              type="button"
              className="btn btn--outline"
              style={{ marginRight: 'auto', color: '#DC2626', borderColor: '#FECACA' }}
              disabled={isSaving}
              onClick={async () => {
                setIsSaving(true);
                try {
                  await onDelete(initial.id);
                  onClose();
                } catch (err) {
                  setError(err.message || 'Xóa thất bại.');
                  setIsSaving(false);
                }
              }}
            >
              Xóa
            </button>
          )}
          <button type="button" className="btn btn--outline" onClick={onClose} disabled={isSaving}>
            Hủy
          </button>
          <button type="submit" className="btn btn--primary" disabled={isSaving}>
            {isSaving ? 'Đang lưu...' : 'Lưu'}
          </button>
        </div>
      </form>
    </div>
  );
}

function EventRow({ item, onClick }) {
  return (
    <div className={`calendar-item calendar-item--${item.status}`} onClick={() => onClick(item)}>
      <div className="calendar-item-main">
        <div className="calendar-item-meta-row">
          <span className={`status-badge status--${item.status}`}>{STATUS_LABEL[item.status]}</span>
          <span className={`priority-pill priority--${item.priority}`}>{PRIORITY_LABEL[item.priority]}</span>
          {item.source === 'system' && <span className="source-tag">Tự động</span>}
        </div>
        <p className="calendar-item-title">{item.title}</p>
        {item.description && <p className="calendar-item-desc">{item.description}</p>}
      </div>
      <div className="calendar-item-side">
        <span className="calendar-item-date">{item.due_date ? formatDateTime(item.due_date) : formatDate(item.date)}</span>
      </div>
    </div>
  );
}

function TaskCalendarPage() {
  const navigate = useNavigate();
  const { user } = useContext(AuthContext);
  const [view, setView] = useState('list');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [isLoading, setIsLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState('all');
  const [priorityFilter, setPriorityFilter] = useState('all');
  const [modalState, setModalState] = useState(null); // null | {} | item
  const [weekAnchor, setWeekAnchor] = useState(() => new Date());
  const [monthAnchor, setMonthAnchor] = useState(() => new Date());
  const canManageQuestions = ['questions.manage_own', 'questions.manage_all'].some((key) => hasEffectivePermission(user, key));
  const canManageDocuments = ['documents.manage_own', 'documents.manage_all'].some((key) => hasEffectivePermission(user, key));
  const isReviewerOnly = hasEffectivePermission(user, 'reviews.manage') && !canManageQuestions;
  const statCount = 3 + Number(canManageQuestions) + Number(canManageDocuments);

  const loadCalendar = () => {
    setIsLoading(true);
    setError(null);
    getMyCalendar({ status: statusFilter, priority: priorityFilter })
      .then((res) => setData(res))
      .catch((err) => setError(err.message || 'Không tải được lịch công việc.'))
      .finally(() => setIsLoading(false));
  };

  useEffect(() => {
    loadCalendar();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusFilter, priorityFilter]);

  const items = useMemo(() => data?.items || [], [data?.items]);
  const summary = data?.summary;

  const upcoming = useMemo(
    () =>
      items
        .filter((i) => i.status === 'todo' && i.due_date)
        .sort((a, b) => new Date(a.due_date) - new Date(b.due_date))
        .slice(0, 5),
    [items],
  );
  const overdueList = useMemo(() => items.filter((i) => i.status === 'overdue').slice(0, 5), [items]);

  const handleEventClick = (item) => {
    if (item.source === 'system') {
      const query = item.related_entity_id ? `?questionId=${item.related_entity_id}` : '';
      if (item.related_entity_type === 'document') {
        if (canAccessPath(user, '/quan-ly-tai-lieu')) {
          navigate('/quan-ly-tai-lieu');
          return;
        }
      } else if (item.related_entity_type === 'review_queue') {
        navigate('/kiem-duyet');
        return;
      } else if (item.related_entity_type === 'question') {
        navigate(canAccessPath(user, '/kiem-duyet') ? `/kiem-duyet${query}` : `/quan-ly${query}`);
        return;
      }
    }
    setModalState(item);
  };

  const handleSave = async (id, payload) => {
    if (id) {
      await updateCalendarTask(id, payload);
    } else {
      await createCalendarTask(payload);
    }
    loadCalendar();
  };

  const handleDelete = async (id) => {
    await deleteCalendarTask(id);
    loadCalendar();
  };

  const weekDays = useMemo(() => {
    const start = startOfWeek(weekAnchor);
    return Array.from({ length: 7 }, (_, i) => {
      const d = new Date(start);
      d.setDate(d.getDate() + i);
      return d;
    });
  }, [weekAnchor]);

  const monthDays = useMemo(() => {
    const year = monthAnchor.getFullYear();
    const month = monthAnchor.getMonth();
    const first = new Date(year, month, 1);
    const startOffset = (first.getDay() + 6) % 7;
    const gridStart = new Date(first);
    gridStart.setDate(gridStart.getDate() - startOffset);
    return Array.from({ length: 42 }, (_, i) => {
      const d = new Date(gridStart);
      d.setDate(d.getDate() + i);
      return d;
    });
  }, [monthAnchor]);

  const itemsByDay = (day) =>
    items.filter((item) => {
      const raw = eventPrimaryDate(item);
      if (!raw) return false;
      return isSameDay(new Date(raw), day);
    });

  return (
    <main className="task-calendar-page">
      <section className="page-hero">
        <div className="container">
          <h1 className="page-hero-title">Lịch công việc</h1>
          <p className="page-hero-desc">
            Theo dõi tài liệu, câu hỏi và việc cần làm trong hệ thống — không phải lịch cá nhân ngoài dự án.
          </p>
          <div className="calendar-tabs" role="tablist">
            {VIEW_TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={view === tab.id}
                className={`calendar-tab ${view === tab.id ? 'calendar-tab--active' : ''}`}
                onClick={() => setView(tab.id)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        </div>
      </section>

      <section className="calendar-body">
        <div className="container">
          {summary && (
            <div className="stats-row" style={{ '--calendar-stat-count': statCount }}>
              <div className={`stat-card ${statusFilter === 'todo' ? 'stat-card--active' : ''}`} onClick={() => setStatusFilter(statusFilter === 'todo' ? 'all' : 'todo')}>
                <b>{summary.todo}</b>
                <span>Việc cần làm</span>
              </div>
              <div className={`stat-card ${statusFilter === 'overdue' ? 'stat-card--active' : ''}`} onClick={() => setStatusFilter(statusFilter === 'overdue' ? 'all' : 'overdue')}>
                <b>{summary.overdue}</b>
                <span>Việc quá hạn</span>
              </div>
              {canManageQuestions && (
                <div className="stat-card">
                  <b>{summary.questions_need_revision}</b>
                  <span>Câu cần sửa</span>
                </div>
              )}
              <div className="stat-card">
                <b>{summary.questions_pending_review}</b>
                <span>{isReviewerOnly ? 'Câu được giao / đang duyệt' : 'Câu chờ duyệt'}</span>
              </div>
              {canManageDocuments && (
                <div className="stat-card">
                  <b>{summary.documents_waiting}</b>
                  <span>Tài liệu cần xử lý</span>
                </div>
              )}
            </div>
          )}

          <div className="calendar-grid">
            <div className="calendar-main card">
              <div className="list-card-header">
                <h3>Danh sách việc</h3>
                <div className="list-toolbar">
                  <select className="field-select" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                    <option value="all">Tất cả trạng thái</option>
                    <option value="todo">Cần làm</option>
                    <option value="overdue">Quá hạn</option>
                    <option value="done">Đã xong</option>
                  </select>
                  <select className="field-select" value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)}>
                    <option value="all">Tất cả ưu tiên</option>
                    <option value="high">Cao</option>
                    <option value="medium">Trung bình</option>
                    <option value="low">Thấp</option>
                  </select>
                </div>
              </div>

              {error && <p className="manage-error">{error}</p>}
              {isLoading && <p className="empty-note">Đang tải...</p>}

              {!isLoading && !error && view === 'list' && (
                <div className="calendar-list">
                  {items.length === 0 && <p className="empty-note">Không có việc nào phù hợp bộ lọc.</p>}
                  {items.map((item) => (
                    <EventRow key={item.id} item={item} onClick={handleEventClick} />
                  ))}
                </div>
              )}

              {!isLoading && !error && view === 'week' && (
                <div className="week-view">
                  <div className="week-nav">
                    <button type="button" className="btn btn--outline btn--small" onClick={() => setWeekAnchor((d) => { const n = new Date(d); n.setDate(n.getDate() - 7); return n; })}>← Tuần trước</button>
                    <span>{formatDate(weekDays[0])} - {formatDate(weekDays[6])}</span>
                    <button type="button" className="btn btn--outline btn--small" onClick={() => setWeekAnchor((d) => { const n = new Date(d); n.setDate(n.getDate() + 7); return n; })}>Tuần sau →</button>
                  </div>
                  <div className="week-grid">
                    {weekDays.map((day) => (
                      <div key={day.toISOString()} className="week-day">
                        <div className="week-day-label">{day.toLocaleDateString('vi-VN', { weekday: 'short', day: '2-digit', month: '2-digit' })}</div>
                        {itemsByDay(day).map((item) => (
                          <div key={item.id} className={`week-day-event week-day-event--${item.status}`} onClick={() => handleEventClick(item)}>
                            {item.title}
                          </div>
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {!isLoading && !error && view === 'month' && (
                <div className="month-view">
                  <div className="week-nav">
                    <button type="button" className="btn btn--outline btn--small" onClick={() => setMonthAnchor((d) => new Date(d.getFullYear(), d.getMonth() - 1, 1))}>← Tháng trước</button>
                    <span>{monthAnchor.toLocaleDateString('vi-VN', { month: 'long', year: 'numeric' })}</span>
                    <button type="button" className="btn btn--outline btn--small" onClick={() => setMonthAnchor((d) => new Date(d.getFullYear(), d.getMonth() + 1, 1))}>Tháng sau →</button>
                  </div>
                  <div className="month-grid">
                    {monthDays.map((day) => {
                      const dayItems = itemsByDay(day);
                      const inMonth = day.getMonth() === monthAnchor.getMonth();
                      return (
                        <div key={day.toISOString()} className={`month-cell ${inMonth ? '' : 'month-cell--muted'}`}>
                          <div className="month-cell-date">{day.getDate()}</div>
                          {dayItems.slice(0, 3).map((item) => (
                            <div key={item.id} className={`month-cell-event month-cell-event--${item.status}`} onClick={() => handleEventClick(item)}>
                              {item.title}
                            </div>
                          ))}
                          {dayItems.length > 3 && <div className="month-cell-more">+{dayItems.length - 3} khác</div>}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>

            <aside className="calendar-side">
              <div className="card side-card">
                <button type="button" className="btn btn--primary" style={{ width: '100%' }} onClick={() => setModalState({})}>
                  + Tạo việc mới
                </button>
              </div>

              <div className="card side-card">
                <h3>Sắp đến hạn</h3>
                {upcoming.length === 0 && <p className="side-note">Không có việc nào sắp đến hạn.</p>}
                {upcoming.map((item) => (
                  <div key={item.id} className="side-mini-item" onClick={() => handleEventClick(item)}>
                    <span>{item.title}</span>
                    <span className="side-mini-date">{formatDate(item.due_date)}</span>
                  </div>
                ))}
              </div>

              <div className="card side-card">
                <h3>Quá hạn</h3>
                {overdueList.length === 0 && <p className="side-note">Không có việc quá hạn.</p>}
                {overdueList.map((item) => (
                  <div key={item.id} className="side-mini-item side-mini-item--overdue" onClick={() => handleEventClick(item)}>
                    <span>{item.title}</span>
                    <span className="side-mini-date">{formatDate(item.due_date || item.date)}</span>
                  </div>
                ))}
              </div>
            </aside>
          </div>
        </div>
      </section>

      {modalState && (
        <TaskModal
          initial={modalState.id ? modalState : null}
          onClose={() => setModalState(null)}
          onSave={handleSave}
          onDelete={handleDelete}
        />
      )}
    </main>
  );
}

export default TaskCalendarPage;
