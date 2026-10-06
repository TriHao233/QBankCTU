import { useState, useContext, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { AuthContext } from "../context/AuthContext";
import { signInWithPopup, signOut } from "firebase/auth";
import { auth, googleProvider } from "../firebase";
import { apiRequest } from "../services/apiClient";
import { isPublicRegistrationEnabled } from "../api/users";

import {
  faArrowLeft,
  faArrowRight,
  faLock,
  faCheck,
  faSpinner,
} from "@fortawesome/free-solid-svg-icons";
import {
  faUser,
  faEnvelope,
  faEye,
  faEyeSlash,
} from "@fortawesome/free-regular-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import "../css/RegisterPage.css";
import { landingPathForRole } from "../auth/permissions";

function DangKy() {
  const navigate = useNavigate();
  const { login } = useContext(AuthContext); // Dùng Context tương tự LoginPage

  const [showPassword, setShowPassword] = useState(false);
  const [showPassword2, setShowPassword2] = useState(false);
  const [loading, setLoading] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState(null);

  useEffect(() => {
    let active = true;
    isPublicRegistrationEnabled().then((enabled) => {
      if (active) setRegistrationEnabled(enabled);
    });
    return () => {
      active = false;
    };
  }, []);

  const [formData, setFormData] = useState({
    fullName: "",
    email: "",
    password: "",
    confirmPassword: ""
  });

  const handleChange = (e) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const getStrength = (pw) => {
    let score = 0;
    if (pw.length >= 8) score++;
    if (/[A-Z]/.test(pw)) score++;
    if (/[0-9]/.test(pw)) score++;
    if (/[^A-Za-z0-9]/.test(pw)) score++;
    return score;
  };

  const score = formData.password.length === 0 ? 0 : Math.max(1, getStrength(formData.password));
  
  const levels = [
    { label: "", color: "" },
    { label: "Yếu", color: "#EF4444" },
    { label: "Trung bình", color: "#F59E0B" },
    { label: "Tốt", color: "#3B82F6" },
    { label: "Mạnh", color: "#22C55E" },
  ];

  // Xử lý Đăng ký/Đăng nhập bằng Google
  const handleGoogleAuth = async () => {
    setLoading(true);
    try {
      if (!auth || !googleProvider) {
        throw new Error("Firebase web app chưa khởi tạo được với project nckh-c0ef6. Vui lòng rebuild frontend và tải lại trang.");
      }
      const result = await signInWithPopup(auth, googleProvider);
      const appUser = await login(result.user);
      navigate(landingPathForRole(appUser.role), { replace: true });
    } catch (error) {
      if (auth) {
        await signOut(auth).catch(() => {});
      }
      alert("Đăng nhập/Đăng ký Google thất bại: " + error.message);
    } finally {
      setLoading(false);
    }
  };

  // Xử lý Đăng ký bằng Email/Password
  const handleSubmit = async (e) => {
    e.preventDefault();
    if (formData.password !== formData.confirmPassword) {
      alert("Mật khẩu xác nhận không khớp!");
      return;
    }
    if (formData.password.length < 6) {
      alert("Mật khẩu phải có ít nhất 6 ký tự theo yêu cầu hệ thống!");
      return;
    }

    setLoading(true);

    try {
      await apiRequest("/auth/register", {
        method: "POST",
        body: {
          email: formData.email,
          password: formData.password,
          full_name: formData.fullName
        },
        authRequired: false,
      });

      alert("Đăng ký thành công! Đang chuyển hướng đến trang đăng nhập...");
      navigate("/dang-nhap");
    } catch (error) {
      alert("Lỗi: " + error.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="register-page">
      <header className="topbar">
        <Link to="/" className="topbar-brand">
          <img
            src="https://www.ctu.edu.vn/images/upload/logo.png"
            alt="CTU"
            className="topbar-logo"
          />
          <div>
            <span className="topbar-title">QBankCTU</span>
            <span className="topbar-sub">Đại Học Cần Thơ</span>
          </div>
        </Link>
        <Link to="/" className="topbar-back">
          <FontAwesomeIcon icon={faArrowLeft} />
          Trang chủ
        </Link>
      </header>

      <main className="auth-wrapper">
        <div className="auth-card">
          <div className="auth-panel auth-panel--left">
            <div className="panel-content">
              <h2 className="panel-heading">
                Bắt đầu <br />
                <em>ngay hôm nay</em>
              </h2>
              <p className="panel-desc">
                Tạo tài khoản để trải nghiệm hệ thống sinh câu hỏi AI đầu tiên
                tích hợp thang phân loại Bloom tại Đại học Cần Thơ.
              </p>
              <ul className="panel-perks">
                <li className="perk-item">
                  <span className="perk-icon">
                    <FontAwesomeIcon icon={faCheck} />
                  </span>
                  Sinh câu hỏi đa dạng từ PDF, DOCX
                </li>
                <li className="perk-item">
                  <span className="perk-icon">
                    <FontAwesomeIcon icon={faCheck} />
                  </span>
                  Phân loại tự động theo 6 cấp Bloom
                </li>
                <li className="perk-item">
                  <span className="perk-icon">
                    <FontAwesomeIcon icon={faCheck} />
                  </span>
                  Đánh giá chất lượng bằng Multi-LLM
                </li>
                <li className="perk-item">
                  <span className="perk-icon">
                    <FontAwesomeIcon icon={faCheck} />
                  </span>
                  Xuất DOCX, JSON
                </li>
              </ul>
            </div>
          </div>

          <div className="auth-panel auth-panel--right">
            <div className="form-wrap">
              <div className="form-header">
                <h1 className="form-title">Tạo tài khoản</h1>
                <p className="form-subtitle">
                  Đã có tài khoản?{" "}
                  <Link to="/dang-nhap" className="form-link">
                    Đăng nhập
                  </Link>
                </p>
              </div>

              {registrationEnabled === false && (
                <p className="form-subtitle" role="alert">
                  Đăng ký công khai đang tắt. Tài khoản do quản trị viên cấp; vui lòng liên hệ quản trị viên.
                </p>
              )}

              {registrationEnabled && (<>
              {/* NÚT GOOGLE ĐÃ GẮN SỰ KIỆN */}
              <button 
                className="btn-google" 
                type="button" 
                onClick={handleGoogleAuth}
                disabled={loading}
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
                Đăng ký với Google
              </button>

              <div className="divider">
                <span>hoặc đăng ký bằng email</span>
              </div>

              <form className="auth-form" onSubmit={handleSubmit}>
                <div className="field-group">
                  <label className="field-label">Họ và tên</label>
                  <div className="field-wrap">
                    <FontAwesomeIcon icon={faUser} className="field-icon" />
                    <input
                      type="text"
                      name="fullName"
                      className="field-input"
                      placeholder="Nguyễn Văn A"
                      value={formData.fullName}
                      onChange={handleChange}
                      required
                    />
                  </div>
                </div>

                <div className="field-group">
                  <label className="field-label">Email</label>
                  <div className="field-wrap">
                    <FontAwesomeIcon icon={faEnvelope} className="field-icon" />
                    <input
                      type="email"
                      name="email"
                      className="field-input"
                      placeholder="example@ctu.edu.vn"
                      value={formData.email}
                      onChange={handleChange}
                      required
                    />
                  </div>
                </div>

                <div className="field-group">
                  <label className="field-label">Mật khẩu</label>
                  <div className="field-wrap">
                    <FontAwesomeIcon icon={faLock} className="field-icon" />
                    <input
                      type={showPassword ? "text" : "password"}
                      name="password"
                      className="field-input"
                      placeholder="Tối thiểu 6 ký tự"
                      value={formData.password}
                      onChange={handleChange}
                      required
                    />
                    <button
                      type="button"
                      className="field-toggle"
                      onClick={() => setShowPassword(!showPassword)}
                    >
                      <FontAwesomeIcon
                        icon={showPassword ? faEyeSlash : faEye}
                      />
                    </button>
                  </div>

                  <div className="pw-strength">
                    <div className="pw-bars">
                      {[1, 2, 3, 4].map((bar) => (
                        <div
                          key={bar}
                          className="pw-bar"
                          style={{
                            background: bar <= score ? levels[score].color : "",
                          }}
                        />
                      ))}
                    </div>
                    <span
                      className="pw-label"
                      style={{
                        color: levels[score].color,
                      }}
                    >
                      {levels[score].label}
                    </span>
                  </div>
                </div>

                <div className="field-group">
                  <label className="field-label">Xác nhận mật khẩu</label>
                  <div className="field-wrap">
                    <FontAwesomeIcon icon={faLock} className="field-icon" />
                    <input
                      type={showPassword2 ? "text" : "password"}
                      name="confirmPassword"
                      className="field-input"
                      placeholder="Nhập lại mật khẩu"
                      value={formData.confirmPassword}
                      onChange={handleChange}
                      required
                    />
                    <button
                      type="button"
                      className="field-toggle"
                      onClick={() => setShowPassword2(!showPassword2)}
                    >
                      <FontAwesomeIcon
                        icon={showPassword2 ? faEyeSlash : faEye}
                      />
                    </button>
                  </div>
                </div>

                <button type="submit" className="btn-submit" disabled={loading}>
                  {loading ? (
                    <>
                      <FontAwesomeIcon icon={faSpinner} spin />
                      Đang xử lý...
                    </>
                  ) : (
                    <>
                      Tạo tài khoản
                      <FontAwesomeIcon icon={faArrowRight} />
                    </>
                  )}
                </button>
              </form>
              </>)}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}

export default DangKy;
