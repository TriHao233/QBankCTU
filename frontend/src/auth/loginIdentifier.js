export const DEMO_LOGIN_ALIASES = {
  admin: 'admin',
  'admin@qbankctu.edu.vn': 'admin',
  reviewer: 'reviewer',
  'reviewer@qbankctu.edu.vn': 'reviewer',
  teacher: 'teacher',
  'teacher@qbankctu.edu.vn': 'teacher',
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const PENDING_APPROVAL_PATTERN = /chờ quản trị viên kích hoạt/i;

export function normalizeLoginEmail(value) {
  return String(value || '').trim().toLowerCase();
}

export function isValidEmail(value) {
  return EMAIL_PATTERN.test(normalizeLoginEmail(value));
}

export function demoUsernameFor(value, demoLoginEnabled = false) {
  if (!demoLoginEnabled) return null;
  return DEMO_LOGIN_ALIASES[normalizeLoginEmail(value)] || null;
}

export function validateLoginIdentifier(value, demoLoginEnabled = false) {
  const identifier = normalizeLoginEmail(value);
  if (!identifier) return 'Vui lòng nhập email hoặc tài khoản demo.';
  if (demoUsernameFor(identifier, demoLoginEnabled)) return '';
  if (isValidEmail(identifier)) return '';
  return demoLoginEnabled
    ? 'Tài khoản demo chỉ hỗ trợ admin, teacher hoặc reviewer. Nếu đăng nhập thường, vui lòng nhập email hợp lệ.'
    : 'Vui lòng nhập email hợp lệ, ví dụ: example@ctu.edu.vn.';
}

function readableErrorDetail(value) {
  if (!value) return '';
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) {
    return value.map(readableErrorDetail).filter(Boolean).join('; ');
  }
  if (typeof value === 'object') {
    return readableErrorDetail(value.message)
      || readableErrorDetail(value.msg)
      || readableErrorDetail(value.detail)
      || (() => {
        try {
          return JSON.stringify(value);
        } catch {
          return '';
        }
      })();
  }
  return String(value);
}

function errorDetailText(error) {
  return readableErrorDetail(error?.payload?.detail)
    || readableErrorDetail(error?.payload?.message)
    || readableErrorDetail(error?.message)
    || '';
}

export function isPendingApprovalError(error) {
  return PENDING_APPROVAL_PATTERN.test(errorDetailText(error));
}

export function loginErrorMessage(error, demoLoginEnabled = false) {
  if (error?.code === 'auth/not-configured') {
    return 'Firebase web app chưa khởi tạo được với project nckh-e6817. Vui lòng rebuild frontend hoặc dùng tài khoản demo.';
  }
  if (
    error?.code === 'auth/invalid-credential'
    || error?.code === 'auth/user-not-found'
    || error?.code === 'auth/wrong-password'
  ) {
    return 'Email hoặc mật khẩu không chính xác!';
  }
  if (error?.code === 'auth/invalid-email') {
    return demoLoginEnabled
      ? 'Email không hợp lệ. Bạn có thể nhập email đầy đủ hoặc tài khoản demo.'
      : 'Email không hợp lệ. Vui lòng nhập email đầy đủ, ví dụ: example@ctu.edu.vn.';
  }
  if (error?.code === 'auth/operation-not-allowed') {
    return 'Đăng nhập email/mật khẩu chưa được bật trong Firebase project hiện tại. Hãy bật Email/Password provider trong Firebase Authentication, hoặc dùng tài khoản demo.';
  }
  if (error?.code === 'auth/user-disabled') {
    return 'Tài khoản đã bị khóa. Vui lòng liên hệ quản trị viên.';
  }
  if (error?.code === 'auth/popup-closed-by-user' || error?.code === 'auth/cancelled-popup-request') {
    return 'Bạn đã đóng cửa sổ đăng nhập Google.';
  }
  const message = errorDetailText(error) || 'Lỗi không xác định';
  if (PENDING_APPROVAL_PATTERN.test(message)) {
    return 'Tài khoản của bạn đang chờ quản trị viên kích hoạt. Bạn sẽ đăng nhập được sau khi được duyệt.';
  }
  return `Đăng nhập thất bại: ${message}`;
}
