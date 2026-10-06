import test from "node:test";
import assert from "node:assert/strict";

import { ROLE_DEFAULT_PERMISSIONS, ASSIGNABLE_PERMISSIONS, assignablePermissions, hasEffectivePermission, PROTECTED_ROUTE_ROLES, canAccessPath, landingPathForRole, rolesForPath } from "./permissions.js";

const PROTECTED_APP_ROUTES = [
  "/sinh-cau-hoi",
  "/quan-ly",
  "/lam-de-thi",
  "/lam-de-thi/:examId",
  "/quan-ly-hoc-phan",
  "/quan-ly-tai-lieu",
  "/kiem-duyet",
  "/duyet-ai",
  "/tong-quan",
  "/danh-muc",
  "/quan-ly-nguoi-dung",
  "/nhat-ky-he-thong",
  "/quan-ly-job",
  "/quan-ly-moodle",
  "/lich-cong-viec",
  "/ho-so",
];

test("admin can access reviewer supervision, admin, question management, and exam routes", () => {
  assert.equal(canAccessPath("Admin", "/sinh-cau-hoi"), false);
  assert.equal(canAccessPath("Admin", "/quan-ly"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-tai-lieu"), true);
  assert.equal(canAccessPath("Admin", "/lam-de-thi/abc123"), true);
  assert.equal(canAccessPath("Admin", "/kiem-duyet"), true);
  assert.equal(canAccessPath("Admin", "/duyet-ai"), true);
  assert.equal(canAccessPath("Admin", "/tong-quan"), true);
  assert.equal(canAccessPath("Admin", "/nhat-ky-he-thong"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-job"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-moodle"), true);
});

test("teacher can access teacher workspace routes", () => {
  assert.equal(canAccessPath("Teacher", "/sinh-cau-hoi"), true);
  assert.equal(canAccessPath("Teacher", "/quan-ly"), true);
  assert.equal(canAccessPath("Teacher", "/quan-ly-tai-lieu"), true);
  assert.equal(canAccessPath("Teacher", "/lam-de-thi/abc123"), true);
});

test("calendar is available only to reviewers and admins", () => {
  assert.equal(canAccessPath("Teacher", "/lich-cong-viec"), false);
  assert.equal(canAccessPath(null, "/lich-cong-viec"), false);
  assert.equal(canAccessPath("Reviewer", "/lich-cong-viec"), true);
  assert.equal(canAccessPath("Admin", "/lich-cong-viec"), true);
});

test("teacher cannot access reviewer or admin-only routes", () => {
  assert.equal(canAccessPath("Teacher", "/kiem-duyet"), false);
  assert.equal(canAccessPath("Teacher", "/duyet-ai"), false);
  assert.equal(canAccessPath("Teacher", "/tong-quan"), false);
  assert.equal(canAccessPath("Teacher", "/quan-ly-nguoi-dung"), false);
  assert.equal(canAccessPath("Teacher", "/nhat-ky-he-thong"), false);
  assert.equal(canAccessPath("Teacher", "/quan-ly-job"), false);
  assert.equal(canAccessPath("Teacher", "/quan-ly-moodle"), false);
});

test("explicit permissions can grant access outside the base role", () => {
  assert.equal(
    canAccessPath({ role: "Teacher", permissions: ["admin.users"] }, "/quan-ly-nguoi-dung"),
    true,
  );
  assert.equal(
    canAccessPath({ role: "Reviewer", permissions: ["questions.generate"] }, "/sinh-cau-hoi"),
    true,
  );
  assert.equal(
    canAccessPath({ role: "Teacher", permissions: ["admin.users"] }, "/nhat-ky-he-thong"),
    false,
  );
});

test("anonymous users only access public routes", () => {
  assert.equal(canAccessPath(null, "/trang-chu"), true);
  assert.equal(canAccessPath(null, "/gioi-thieu"), true);
  assert.equal(canAccessPath(null, "/sinh-cau-hoi"), false);
  assert.equal(canAccessPath(null, "/quan-ly"), false);
});

test("direct URL permission and landing path use the same route map", () => {
  assert.deepEqual(rolesForPath("/lam-de-thi/abc123"), ["Teacher", "Admin"]);
  assert.equal(landingPathForRole("Admin"), "/tong-quan");
  assert.equal(landingPathForRole("Admin", "/kiem-duyet?status=PENDING"), "/kiem-duyet?status=PENDING");
  assert.equal(landingPathForRole("Admin", "/sinh-cau-hoi"), "/tong-quan");
  assert.equal(landingPathForRole("Admin", "/lam-de-thi"), "/lam-de-thi");
  assert.equal(landingPathForRole("Teacher", "/kiem-duyet"), "/sinh-cau-hoi");
});

test("protected app routes are declared in the central permission map", () => {
  assert.deepEqual(Object.keys(PROTECTED_ROUTE_ROLES), PROTECTED_APP_ROUTES);
});

test('role defaults and user editor only use backend assignable permissions', () => {
  for (const role of ['Teacher', 'Reviewer']) assert.ok(ROLE_DEFAULT_PERMISSIONS[role].every((key) => ASSIGNABLE_PERMISSIONS.includes(key)));
  assert.deepEqual(assignablePermissions(['reviews.manage', 'admin.users', 'questions.comment']), ['reviews.manage']);
});
test('effective reviewer permissions respect revokes and retain legacy fallback', () => {
  assert.equal(hasEffectivePermission({ role: 'Admin', permissions: [] }, 'reviews.manage'), true);
  assert.equal(hasEffectivePermission({ role: 'Reviewer', permissions: [] }, 'reviews.manage'), false);
  assert.equal(hasEffectivePermission({ role: 'Reviewer', permissions: ['reviews.manage'] }, 'reviews.manage'), true);
  assert.equal(hasEffectivePermission({ role: 'Reviewer' }, 'reviews.manage'), true);
  assert.equal(hasEffectivePermission(null, 'reviews.manage'), false);
});
test('revoked permissions close pages even when the role matches', () => {
  const teacherWithoutGenerate = {
    role: 'Teacher',
    permissions: ROLE_DEFAULT_PERMISSIONS.Teacher.filter((key) => key !== 'questions.generate'),
  };
  assert.equal(canAccessPath(teacherWithoutGenerate, '/sinh-cau-hoi'), false);
  assert.equal(canAccessPath(teacherWithoutGenerate, '/quan-ly'), true);
  assert.equal(canAccessPath({ role: 'Reviewer', permissions: [] }, '/kiem-duyet'), false);
  assert.equal(canAccessPath({ role: 'Reviewer', permissions: [] }, '/lich-cong-viec'), true);
  assert.equal(canAccessPath({ role: 'Teacher' }, '/sinh-cau-hoi'), true);
  assert.equal(canAccessPath({ role: 'Admin', permissions: [] }, '/kiem-duyet'), true);
});
