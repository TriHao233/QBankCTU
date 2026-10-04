import test from "node:test";
import assert from "node:assert/strict";

import {
  ROLE_DEFAULT_PERMISSIONS,
  ASSIGNABLE_PERMISSIONS,
  assignablePermissions,
  hasEffectivePermission,
  PROTECTED_ROUTE_ROLES,
  canAccessPath,
  landingPathForRole,
  rolesForPath,
} from "./permissions.js";

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

test("admin can access admin workspace and bank routes, but not human review", () => {
  assert.equal(canAccessPath("Admin", "/sinh-cau-hoi"), false);
  assert.equal(canAccessPath("Admin", "/quan-ly"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-tai-lieu"), true);
  assert.equal(canAccessPath("Admin", "/lam-de-thi/abc123"), true);
  assert.equal(canAccessPath("Admin", "/kiem-duyet"), false);
  assert.equal(canAccessPath("Admin", "/duyet-ai"), true);
  assert.equal(canAccessPath("Admin", "/tong-quan"), true);
  assert.equal(canAccessPath("Admin", "/nhat-ky-he-thong"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-job"), true);
  assert.equal(canAccessPath("Admin", "/quan-ly-moodle"), true);
  assert.equal(canAccessPath("Admin", "/lich-cong-viec"), true);
});

test("teacher can access teacher workspace routes including review by default", () => {
  assert.equal(canAccessPath("Teacher", "/sinh-cau-hoi"), true);
  assert.equal(canAccessPath("Teacher", "/quan-ly"), true);
  assert.equal(canAccessPath("Teacher", "/quan-ly-tai-lieu"), true);
  assert.equal(canAccessPath("Teacher", "/lam-de-thi/abc123"), true);
  assert.equal(canAccessPath("Teacher", "/kiem-duyet"), true);
  assert.equal(canAccessPath("Teacher", "/lich-cong-viec"), true);
});

test("teacher without reviews.manage cannot open review queue", () => {
  assert.equal(
    canAccessPath({ role: "Teacher", permissions: ["questions.generate", "questions.manage_own"] }, "/kiem-duyet"),
    false,
  );
});

test("teacher cannot access admin-only routes", () => {
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
  assert.equal(landingPathForRole("Admin", "/kiem-duyet?status=PENDING"), "/tong-quan");
  assert.equal(landingPathForRole("Admin", "/sinh-cau-hoi"), "/tong-quan");
  assert.equal(landingPathForRole("Admin", "/lam-de-thi"), "/lam-de-thi");
  assert.equal(landingPathForRole("Teacher", "/kiem-duyet"), "/kiem-duyet");
});

test("protected app routes are declared in the central permission map", () => {
  assert.deepEqual(Object.keys(PROTECTED_ROUTE_ROLES), PROTECTED_APP_ROUTES);
});

test("role defaults and user editor only use backend assignable permissions", () => {
  for (const role of ["Teacher", "Reviewer"]) {
    assert.ok(ROLE_DEFAULT_PERMISSIONS[role].every((key) => ASSIGNABLE_PERMISSIONS.includes(key)));
  }
  assert.deepEqual(assignablePermissions(["reviews.manage", "admin.users", "questions.comment"]), ["reviews.manage"]);
});

test("effective reviewer permissions respect admin exclusion and legacy fallback", () => {
  assert.equal(hasEffectivePermission({ role: "Admin", permissions: [] }, "reviews.manage"), false);
  assert.equal(hasEffectivePermission({ role: "Reviewer", permissions: [] }, "reviews.manage"), false);
  assert.equal(hasEffectivePermission({ role: "Reviewer", permissions: ["reviews.manage"] }, "reviews.manage"), true);
  assert.equal(hasEffectivePermission({ role: "Reviewer" }, "reviews.manage"), true);
  assert.equal(hasEffectivePermission(null, "reviews.manage"), false);
});
