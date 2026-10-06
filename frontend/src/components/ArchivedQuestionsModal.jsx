import { useEffect, useState } from 'react';
import { listArchivedQuestions, restoreQuestion } from '../api/questions';

const PAGE_SIZE = 20;

export default function ArchivedQuestionsModal({ onClose, onRestored }) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState({ items: [], total: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [restoringId, setRestoringId] = useState(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    listArchivedQuestions({ page, pageSize: PAGE_SIZE })
      .then((result) => { if (active) { setData(result); setError(''); } })
      .catch((err) => { if (active) setError(err.message || 'Không tải được danh sách lưu trữ.'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [page]);

  const handleRestore = async (item) => {
    setRestoringId(item.id);
    try {
      await restoreQuestion(item.id);
      setData((current) => ({
        items: current.items.filter((question) => question.id !== item.id),
        total: Math.max(0, current.total - 1),
      }));
      onRestored?.(item);
    } catch (err) {
      setError(err.message || 'Khôi phục thất bại.');
    } finally {
      setRestoringId(null);
    }
  };

  const pageCount = Math.max(1, Math.ceil((data.total || 0) / PAGE_SIZE));

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-card" onClick={(event) => event.stopPropagation()}>
        <h3 className="modal-title">Câu hỏi đã lưu trữ</h3>
        <p className="empty-note">Khôi phục đưa câu hỏi về đúng trạng thái trước khi lưu trữ; câu đã duyệt giữ nguyên bản đã duyệt.</p>
        {error && <p className="form-error">{error}</p>}
        {loading ? (
          <p className="empty-note">Đang tải...</p>
        ) : data.items.length === 0 ? (
          <p className="empty-note">Không có câu hỏi nào trong lưu trữ.</p>
        ) : (
          <ul className="archived-question-list">
            {data.items.map((item) => (
              <li key={item.id}>
                <div>
                  <b>{item.question_code}</b>
                  <span>{item.content}</span>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={restoringId === item.id}
                  onClick={() => handleRestore(item)}
                >
                  {restoringId === item.id ? 'Đang khôi phục...' : 'Khôi phục'}
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="modal-actions">
          {pageCount > 1 && (
            <>
              <button type="button" className="btn btn-secondary" disabled={page <= 1} onClick={() => setPage(page - 1)}>Trước</button>
              <span>{page}/{pageCount}</span>
              <button type="button" className="btn btn-secondary" disabled={page >= pageCount} onClick={() => setPage(page + 1)}>Sau</button>
            </>
          )}
          <button type="button" className="btn btn-primary" onClick={onClose}>Đóng</button>
        </div>
      </div>
    </div>
  );
}
