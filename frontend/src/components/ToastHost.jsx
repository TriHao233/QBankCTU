import React, { useEffect, useState } from 'react';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faCircleCheck, faCircleExclamation, faXmark } from '@fortawesome/free-solid-svg-icons';
import { subscribeNotifications } from '../utils/notify';
import './ToastHost.css';

const AUTO_DISMISS_MS = 7000;
const MAX_VISIBLE = 4;

function ToastHost() {
  const [toasts, setToasts] = useState([]);

  useEffect(() => subscribeNotifications((item) => {
    setToasts((current) => [...current, item].slice(-MAX_VISIBLE));
    window.setTimeout(() => {
      setToasts((current) => current.filter((toast) => toast.id !== item.id));
    }, AUTO_DISMISS_MS);
  }), []);

  const dismiss = (id) => setToasts((current) => current.filter((toast) => toast.id !== id));

  if (toasts.length === 0) return null;

  return (
    <div className="toast-host" aria-live="assertive">
      {toasts.map((toast) => (
        <div key={toast.id} className={`toast toast--${toast.tone}`} role={toast.tone === 'error' ? 'alert' : 'status'}>
          <FontAwesomeIcon icon={toast.tone === 'success' ? faCircleCheck : faCircleExclamation} className="toast__icon" />
          <p className="toast__message">{toast.message}</p>
          <button type="button" className="toast__close" aria-label="Đóng thông báo" onClick={() => dismiss(toast.id)}>
            <FontAwesomeIcon icon={faXmark} />
          </button>
        </div>
      ))}
    </div>
  );
}

export default ToastHost;
