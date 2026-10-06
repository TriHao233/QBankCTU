export const PERMISSIONS = Object.freeze({
  teacherWorkspace: Object.freeze(["Teacher"]),
  teacherAdminWorkspace: Object.freeze(["Teacher", "Admin"]),
  reviewerWorkspace: Object.freeze(["Reviewer", "Admin"]),
  adminWorkspace: Object.freeze(["Admin"]),
  authenticated: Object.freeze(["Admin", "Teacher", "Reviewer"]),
});

export const ROLE_DEFAULT_PERMISSIONS = Object.freeze({
  Admin: Object.freeze([
    "admin.overview",
    "admin.users",
    "admin.catalog",
    "admin.audit",
    "admin.jobs",
    "admin.moodle",
    "admin.ai_review",
    "documents.manage_all",
    "questions.manage_all",
    "questions.use_shared_bank",
    "reviews.manage",
  ]),
  Teacher: Object.freeze([
    "documents.manage_own",
    "questions.generate",
    "questions.manage_own",
    "questions.share_bank",
    "questions.use_shared_bank",
    "exams.manage_own",
    "catalog.subjects.manage_own",
  ]),
  Reviewer: Object.freeze([
    "reviews.manage",
    "questions.export_moodle",
  ]),
});

// Keep in sync with backend/core/dependencies.py.
export const TEACHER_ASSIGNABLE_PERMISSIONS = Object.freeze([
  'questions.generate', 'questions.manage_own', 'documents.manage_own', 'exams.manage_own',
  'catalog.subjects.manage_own', 'questions.share_bank', 'questions.use_shared_bank',
]);
export const REVIEW_ASSIGNABLE_PERMISSIONS = Object.freeze(['reviews.manage', 'questions.export_moodle']);
export const ASSIGNABLE_PERMISSIONS = Object.freeze([
  ...TEACHER_ASSIGNABLE_PERMISSIONS, ...REVIEW_ASSIGNABLE_PERMISSIONS,
]);
export const PERMISSION_LABELS = Object.freeze({
  'documents.manage_own': 'Quản lý tài liệu của mình',
  'questions.generate': 'Sinh câu hỏi bằng AI',
  'questions.manage_own': 'Soạn và quản lý câu hỏi của mình',
  'questions.share_bank': 'Chia sẻ câu hỏi và tài liệu',
  'questions.use_shared_bank': 'Dùng câu hỏi, tài liệu được chia sẻ',
  'exams.manage_own': 'Làm đề thi',
  'catalog.subjects.manage_own': 'Tạo và sửa học phần của mình',
  'reviews.manage': 'Kiểm duyệt câu hỏi',
  'questions.export_moodle': 'Đưa câu đã duyệt lên Moodle',
});
export function assignablePermissions(list) {
  return [...new Set((list || []).filter((item) => ASSIGNABLE_PERMISSIONS.includes(item)))];
}
export function hasEffectivePermission(user, permission) {
  if (!user) return false;
  if (user.role === 'Admin') return true;
  return (Array.isArray(user.permissions) ? user.permissions : ROLE_DEFAULT_PERMISSIONS[user.role] || []).includes(permission);
}

export const ROUTE_PERMISSION_KEYS = Object.freeze({
  "/sinh-cau-hoi": Object.freeze(["questions.generate"]),
  "/quan-ly": Object.freeze(["questions.manage_own"]),
  "/lam-de-thi": Object.freeze(["exams.manage_own"]),
  "/quan-ly-hoc-phan": Object.freeze(["catalog.subjects.manage_own"]),
  "/quan-ly-tai-lieu": Object.freeze(["documents.manage_own"]),
  "/lam-de-thi/:examId": Object.freeze(["exams.manage_own"]),
  "/kiem-duyet": Object.freeze(["reviews.manage"]),
  "/duyet-ai": Object.freeze(["admin.ai_review"]),
  "/tong-quan": Object.freeze(["admin.overview"]),
  "/danh-muc": Object.freeze(["admin.catalog"]),
  "/quan-ly-nguoi-dung": Object.freeze(["admin.users"]),
  "/nhat-ky-he-thong": Object.freeze(["admin.audit"]),
  "/quan-ly-job": Object.freeze(["admin.jobs"]),
  "/quan-ly-moodle": Object.freeze(["admin.moodle"]),
});

export const PROTECTED_ROUTE_ROLES = Object.freeze({
  "/sinh-cau-hoi": PERMISSIONS.teacherWorkspace,
  "/quan-ly": PERMISSIONS.teacherAdminWorkspace,
  "/lam-de-thi": PERMISSIONS.teacherAdminWorkspace,
  "/lam-de-thi/:examId": PERMISSIONS.teacherAdminWorkspace,
  "/quan-ly-hoc-phan": PERMISSIONS.teacherWorkspace,
  "/quan-ly-tai-lieu": PERMISSIONS.teacherAdminWorkspace,
  "/kiem-duyet": PERMISSIONS.reviewerWorkspace,
  "/duyet-ai": PERMISSIONS.adminWorkspace,
  "/tong-quan": PERMISSIONS.adminWorkspace,
  "/danh-muc": PERMISSIONS.adminWorkspace,
  "/quan-ly-nguoi-dung": PERMISSIONS.adminWorkspace,
  "/nhat-ky-he-thong": PERMISSIONS.adminWorkspace,
  "/quan-ly-job": PERMISSIONS.adminWorkspace,
  "/quan-ly-moodle": PERMISSIONS.adminWorkspace,
  "/lich-cong-viec": PERMISSIONS.reviewerWorkspace,
  "/ho-so": PERMISSIONS.authenticated,
});

export const ROLE_LANDING_PATHS = Object.freeze({
  Admin: "/tong-quan",
  Reviewer: "/kiem-duyet",
  Teacher: "/sinh-cau-hoi",
});

function splitPath(pathname) {
  return pathname.replace(/\/+$/, "").split("/").filter(Boolean);
}

export function pathMatches(pattern, pathname) {
  const patternParts = splitPath(pattern);
  const pathParts = splitPath(pathname);
  if (patternParts.length !== pathParts.length) return false;
  return patternParts.every((part, index) => (
    part.startsWith(":") || part === pathParts[index]
  ));
}

export function rolesForPath(pathname) {
  const entry = Object.entries(PROTECTED_ROUTE_ROLES).find(([pattern]) => (
    pathMatches(pattern, pathname)
  ));
  return entry?.[1] || null;
}

// A synced profile carries the backend's effective permissions (defaults minus revokes plus grants),
// so it replaces the role defaults instead of being added to them.
export function permissionsForUser(userOrRole) {
  const role = typeof userOrRole === "string" ? userOrRole : userOrRole?.role;
  if (role === "Admin") return [...ROLE_DEFAULT_PERMISSIONS.Admin];
  if (typeof userOrRole !== "string" && Array.isArray(userOrRole?.permissions)) {
    return Array.from(new Set(userOrRole.permissions));
  }
  return [...(ROLE_DEFAULT_PERMISSIONS[role] || [])];
}

export function permissionsForPath(pathname) {
  const entry = Object.entries(ROUTE_PERMISSION_KEYS).find(([pattern]) => (
    pathMatches(pattern, pathname)
  ));
  return entry?.[1] || null;
}

export function canAccessPath(userOrRole, pathname) {
  const roles = rolesForPath(pathname);
  if (!roles) return true;
  const role = typeof userOrRole === "string" ? userOrRole : userOrRole?.role;
  const requiredPermissions = permissionsForPath(pathname);
  if (roles.includes(role)) {
    // A matching role is not enough once the permission behind the page has been revoked.
    if (role === "Admin" || typeof userOrRole === "string" || !requiredPermissions?.length) return true;
  } else if (!requiredPermissions?.length) {
    return false;
  }
  const permissions = permissionsForUser(userOrRole);
  return requiredPermissions.every((permission) => permissions.includes(permission));
}

export function landingPathForRole(role, requestedPath) {
  const requestedPathname = typeof requestedPath === "string"
    ? requestedPath.split("?")[0]
    : null;
  if (requestedPathname && canAccessPath(role, requestedPathname)) {
    return requestedPath;
  }
  return ROLE_LANDING_PATHS[role] || "/trang-chu";
}
