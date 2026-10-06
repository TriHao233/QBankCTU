import { useEffect, useState } from 'react';
import { listUsers } from '../api/users';

const ROLE_LABEL = { Admin: 'Quản trị viên', Teacher: 'Giảng viên', Reviewer: 'Người duyệt' };

export function userOptionText(user) {
  const name = user.display_name || user.email || user.id;
  return `${name} · ${ROLE_LABEL[user.role] || user.role}`;
}

export function useUserOptions() {
  const [users, setUsers] = useState([]);

  useEffect(() => {
    let cancelled = false;
    listUsers({ page: 1, pageSize: 100 })
      .then((result) => {
        if (!cancelled) setUsers(result.items || []);
      })
      .catch(() => {
        if (!cancelled) setUsers([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return users;
}
