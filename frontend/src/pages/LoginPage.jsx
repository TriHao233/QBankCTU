import React, { useState, useContext, useEffect, useRef } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import "../css/LoginPage.css";
import { AuthContext } from "../context/AuthContext";
import {
  getRedirectResult,
  signInWithEmailAndPassword,
  signInWithPopup,
  signInWithRedirect,
  signOut,
} from "firebase/auth";
import { auth, googleProvider } from "../firebase";
import { apiRequest } from "../services/apiClient";
import { landingPathForRole } from "../auth/permissions";
import {
  demoUsernameFor,
  loginErrorMessage,
  normalizeLoginEmail,
  validateLoginIdentifier,
} from "../auth/loginIdentifier";

const DEMO_LOGIN_ENABLED = String(import.meta.env.VITE_DEMO_MODE).toLowerCase() === "true";

const POPUP_FALLBACK_CODES = new Set([
  "auth/operation-not-supported-in-this-environment",
  "auth/popup-blocked",
]);

function googleLoginErrorMessage(error) {
  if (error?.code === "auth/not-configured") {
    return "Firebase web app chưa khởi tạo được với project nckh-e6817. Vui lòng rebuild frontend và tải lại trang.";
  }
  if (error?.code === "auth/unauthorized-domain") {
    return "Miền hiện tại chưa được Firebase cho phép. Hãy mở ứng dụng bằng localhost hoặc thêm domain này vào Firebase Authorized domains.";
  }
  return error?.message || "Lỗi không xác định";
}

function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login, loginWithDemoSession, user, loading, sessionNotice } = useContext(AuthContext);
  const requestedPath = location.state?.from;
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [authNotice, setAuthNotice] = useState(null);
  const notice = authNotice || (sessionNotice ? { type: 'info', message: sessionNotice } : null);
  const redirectChecked = useRef(false);

  const [formData, setFormData] = useState({
    email: "",
    password: "",
    remember: false,
  });

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: type === "checkbox" ? checked : value,
    }));
  };

  const handleTogglePassword = () => {
    setShowPassword(!showPassword);
  };

  useEffect(() => {
    if (!loading && user) {
      navigate(landingPathForRole(user.role, requestedPath), { replace: true });
    }
  }, [loading, navigate, requestedPath, user]);

  useEffect(() => {
    if (redirectChecked.current) return undefined;
    redirectChecked.current = true;
    let active = true;
    if (!auth) {
      return () => {
        active = false;
      };
    }
    getRedirectResult(auth)
      .then(async (result) => {
        if (!active || !result?.user) return;
        setIsLoading(true);
        const appUser = await login(result.user);
        navigate(landingPathForRole(appUser.role, requestedPath), { replace: true });
      })
      .catch(async (error) => {
        if (!active) return;
        await signOut(auth).catch(() => {});
        setAuthNotice({
          type: "error",
          message: "Đăng nhập Google thất bại: " + googleLoginErrorMessage(error),
        });
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [login, navigate, requestedPath]);

  // Xử lý đăng nhập bằng Google
  const handleGoogleAuth = async () => {
    setIsLoading(true);
    setAuthNotice(null);
    try {
      if (!auth || !googleProvider) {
        const error = new Error("Firebase web app chưa được cấu hình");
        error.code = "auth/not-configured";
        throw error;
      }
      const result = await signInWithPopup(auth, googleProvider);
      const appUser = await login(result.user);
      navigate(landingPathForRole(appUser.role, requestedPath), { replace: true });
    } catch (error) {
      if (auth) {
        await signOut(auth).catch(() => {});
      }
      if (POPUP_FALLBACK_CODES.has(error?.code)) {
        setAuthNotice({
          type: "info",
          message: "Trình duyệt đang chuyển sang đăng nhập Google...",
        });
        await signInWithRedirect(auth, googleProvider);
        return;
      }
      setAuthNotice({
        type: "error",
        message: "Đăng nhập Google thất bại: " + googleLoginErrorMessage(error),
      });
    } finally {
      setIsLoading(false);
    }
  };

  // Xử lý đăng nhập bằng Email/Password
  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!formData.email || !formData.password) {
      setAuthNotice({ type: "error", message: "Vui lòng nhập đầy đủ email và mật khẩu." });
      return;
    }
    const identifierError = validateLoginIdentifier(formData.email, DEMO_LOGIN_ENABLED);
    if (identifierError) {
      setAuthNotice({ type: "error", message: identifierError });
      return;
    }

    setIsLoading(true);
    setAuthNotice(null);

    try {
      const demoUsername = demoUsernameFor(formData.email, DEMO_LOGIN_ENABLED);
      if (!demoUsername && !auth) {
        const error = new Error("Firebase web app chưa được cấu hình");
        error.code = "auth/not-configured";
        throw error;
      }
      const userCredential = demoUsername
        ? await apiRequest("/auth/demo-login", {
            method: "POST",
            body: {
              username: demoUsername,
              password: formData.password,
            },
            authRequired: false,
          }).then((data) => ({ user: null, appUser: data }))
        : await signInWithEmailAndPassword(
            auth,
            normalizeLoginEmail(formData.email),
            formData.password
      );
      const appUser = demoUsername
        ? await loginWithDemoSession(userCredential.appUser)
        : await login(userCredential.user);
      navigate(landingPathForRole(appUser.role, requestedPath), { replace: true });
    } catch (error) {
      if (auth) {
        await signOut(auth).catch(() => {});
      }
      setAuthNotice({ type: "error", message: loginErrorMessage(error, DEMO_LOGIN_ENABLED) });
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="login-page">
      <header className="topbar">
        <Link to="/trang-chu" className="topbar-brand">
          <img
            src="https://www.ctu.edu.vn/images/upload/logo.png"
            alt="Logo Đại học Cần Thơ"
            className="topbar-logo"
          />
          <div>
            <span className="topbar-title">QBankCTU</span>
            <span className="topbar-sub">Đại Học Cần Thơ</span>
          </div>
        </Link>
        <Link to="/trang-chu" className="topbar-back">
          <i className="fa-solid fa-arrow-left"></i>
          Trang chủ
        </Link>
      </header>

      <main className="auth-wrapper">
        <div className="auth-card">
          <div className="auth-panel auth-panel--left">
            <div className="panel-content">
              <div className="panel-badge">Ngân hàng câu hỏi AI</div>
              <h2 className="panel-heading">
                Chào mừng bạn
                <br />
                trở lại QBankCTU
              </h2>
              <p className="panel-desc">
                Đăng nhập để truy cập hệ thống sinh và quản lý ngân hàng câu hỏi
                thông minh của Đại học Cần Thơ.
              </p>
              <div className="panel-stats">
                <div className="ps-item">
                  <span className="ps-num">Multi-LLM</span>
                  <span className="ps-label">Đánh giá AI đa mô hình</span>
                </div>
                <div className="ps-divider"></div>
                <div className="ps-item">
                  <span className="ps-num">Bloom</span>
                  <span className="ps-label">Phân loại tư duy</span>
                </div>
              </div>
            </div>
            <div className="panel-deco" aria-hidden="true">
              <div className="deco-circle deco-circle--1"></div>
              <div className="deco-circle deco-circle--2"></div>
            </div>
          </div>

          <div className="auth-panel auth-panel--right">
            <div className="form-wrap">
              <div className="form-header">
                <h1 className="form-title">Đăng nhập</h1>
                <p className="form-subtitle">
                  Chưa có tài khoản?{" "}
                  <Link to="/dang-ky" className="form-link">
                    Đăng ký ngay
                  </Link>
                </p>
              </div>

              {notice && (
                <p className={`auth-notice auth-notice--${notice.type}`} role="alert">
                  {notice.message}
                </p>
              )}

              <button 
                className="btn-google" 
                type="button" 
                onClick={handleGoogleAuth} 
                disabled={isLoading}
              >
                <svg
                  width="20"
                  height="20"
                  viewBox="0 0 48 48"
                  fill="none"
                  xmlns="http://www.w3.org/2000/svg"
                >
                  <path d="M47.532 24.552c0-1.636-.132-3.204-.388-4.704H24.48v9.02h12.952c-.572 2.996-2.24 5.54-4.764 7.244v5.988h7.704c4.52-4.164 7.16-10.3 7.16-17.548z" fill="#4285F4" />
                  <path d="M24.48 48c6.48 0 11.92-2.148 15.896-5.82l-7.704-5.988c-2.148 1.44-4.9 2.292-8.192 2.292-6.3 0-11.636-4.256-13.548-9.972H3.04v6.18C6.996 42.836 15.104 48 24.48 48z" fill="#34A853" />
                  <path d="M10.932 28.512A14.4 14.4 0 0 1 10.18 24c0-1.572.272-3.1.752-4.512v-6.18H3.04A23.956 23.956 0 0 0 .48 24c0 3.876.924 7.548 2.56 10.692l7.892-6.18z" fill="#FBBC05" />
                  <path d="M24.48 9.516c3.548 0 6.736 1.22 9.244 3.62l6.916-6.916C36.396 2.38 30.956 0 24.48 0 15.104 0 6.996 5.164 3.04 13.308l7.892 6.18c1.912-5.716 7.248-9.972 13.548-9.972z" fill="#EA4335" />
                </svg>
                Đăng nhập với Google
              </button>

              

              <form className="auth-form" onSubmit={handleSubmit} noValidate>
                <div className="field-group">
                  <label className="field-label" htmlFor="email">
                    {DEMO_LOGIN_ENABLED ? "Tài khoản hoặc email" : "Email"}
                  </label>
                  <div className="field-wrap">
                    <i className="fa-regular fa-envelope field-icon"></i>
                    <input
                      type="text"
                      id="email"
                      name="email"
                      value={formData.email}
                      onChange={handleChange}
                      className="field-input"
                      placeholder={DEMO_LOGIN_ENABLED ? "admin, reviewer hoặc example@ctu.edu.vn" : "example@ctu.edu.vn"}
                      autoComplete="username"
                    />
                  </div>
                </div>

                <div className="field-group">
                  <div className="field-label-row">
                    <label className="field-label" htmlFor="password">
                      Mật khẩu
                    </label>
                    <Link
                      to="/lien-he"
                      className="form-link form-link--small"
                    >
                      Quên mật khẩu?
                    </Link>
                  </div>
                  <div className="field-wrap">
                    <i className="fa-solid fa-lock field-icon"></i>
                    <input
                      type={showPassword ? "text" : "password"}
                      id="password"
                      name="password"
                      value={formData.password}
                      onChange={handleChange}
                      className="field-input"
                      placeholder="••••••••"
                      autoComplete="current-password"
                    />
                    <button
                      type="button"
                      className="field-toggle"
                      onClick={handleTogglePassword}
                      aria-label="Hiện mật khẩu"
                    >
                      <i
                        className={`fa-regular ${showPassword ? "fa-eye-slash" : "fa-eye"}`}
                      ></i>
                    </button>
                  </div>
                </div>

                <div className="field-check">
                  <label className="check-label">
                    <input
                      type="checkbox"
                      name="remember"
                      checked={formData.remember}
                      onChange={handleChange}
                      className="check-input"
                    />
                    <span className="check-box"></span>
                    <span className="check-text">Ghi nhớ đăng nhập</span>
                  </label>
                </div>

                <button
                  type="submit"
                  className={`btn-submit ${isLoading ? "loading" : ""}`}
                  disabled={isLoading}
                >
                  {isLoading ? (
                    <>
                      <i className="fa-solid fa-spinner fa-spin"></i>
                      <span>Đang xử lý…</span>
                    </>
                  ) : (
                    <>
                      <span>Đăng nhập</span>
                      <i className="fa-solid fa-arrow-right"></i>
                    </>
                  )}
                </button>
              </form>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default LoginPage;
