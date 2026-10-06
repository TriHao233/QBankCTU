import { lazy, Suspense, useContext, useEffect } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import Layout from './components/Layout';
import ToastHost from './components/ToastHost';
import HomePage from './pages/HomePage';
import { AuthContext } from './context/AuthContext';
import { canAccessPath, landingPathForRole } from './auth/permissions';

const pageImports = {
    AboutPage: () => import('./pages/AboutPage'),
    GeneratePage: () => import('./pages/GeneratePage'),
    ManagePage: () => import('./pages/ManagePage'),
    ReviewQueuePage: () => import('./pages/ReviewQueuePage'),
    AdminAiReviewPage: () => import('./pages/AdminAiReviewPage'),
    AdminOverviewPage: () => import('./pages/AdminOverviewPage'),
    CatalogAdminPage: () => import('./pages/CatalogAdminPage'),
    AdminAuditPage: () => import('./pages/AdminAuditPage'),
    AdminJobsPage: () => import('./pages/AdminJobsPage'),
    AdminMoodlePage: () => import('./pages/AdminMoodlePage'),
    UsersAdminPage: () => import('./pages/UsersAdminPage'),
    LoginPage: () => import('./pages/LoginPage'),
    RegisterPage: () => import('./pages/RegisterPage'),
    GuidePage: () => import('./pages/GuidePage'),
    ContactPage: () => import('./pages/ContactPage'),
    UserProfile: () => import('./pages/UserProfile'),
    TaskCalendarPage: () => import('./pages/TaskCalendarPage'),
    ExamListPage: () => import('./pages/ExamListPage'),
    SubjectManage: () => import('./pages/SubjectManage'),
    DocumentManagePage: () => import('./pages/DocumentManagePage'),
    ExamBuilderPage: () => import('./pages/ExamBuilderPage'),
};

const AboutPage = lazy(pageImports.AboutPage);
const GeneratePage = lazy(pageImports.GeneratePage);
const ManagePage = lazy(pageImports.ManagePage);
const ReviewQueuePage = lazy(pageImports.ReviewQueuePage);
const AdminAiReviewPage = lazy(pageImports.AdminAiReviewPage);
const AdminOverviewPage = lazy(pageImports.AdminOverviewPage);
const CatalogAdminPage = lazy(pageImports.CatalogAdminPage);
const AdminAuditPage = lazy(pageImports.AdminAuditPage);
const AdminJobsPage = lazy(pageImports.AdminJobsPage);
const AdminMoodlePage = lazy(pageImports.AdminMoodlePage);
const UsersAdminPage = lazy(pageImports.UsersAdminPage);
const LoginPage = lazy(pageImports.LoginPage);
const RegisterPage = lazy(pageImports.RegisterPage);
const GuidePage = lazy(pageImports.GuidePage);
const ContactPage = lazy(pageImports.ContactPage);
const UserProfile = lazy(pageImports.UserProfile);
const TaskCalendarPage = lazy(pageImports.TaskCalendarPage);
const ExamListPage = lazy(pageImports.ExamListPage);
const SubjectManage = lazy(pageImports.SubjectManage);
const DocumentManagePage = lazy(pageImports.DocumentManagePage);
const ExamBuilderPage = lazy(pageImports.ExamBuilderPage);

function preloadDevPages() {
    if (!import.meta.env.DEV) return;

    const preload = () => {
        Object.values(pageImports).forEach((loadPage) => {
            loadPage().catch(() => {});
        });
    };

    if ('requestIdleCallback' in window) {
        const idleId = window.requestIdleCallback(preload, { timeout: 1500 });
        return () => window.cancelIdleCallback(idleId);
    }

    const timerId = window.setTimeout(preload, 500);
    return () => window.clearTimeout(timerId);
}

function RequireAccess({ path, children }) {
    const { user, loading } = useContext(AuthContext);
    const location = useLocation();
    if (loading) return <div className="route-loading">Đang kiểm tra phiên đăng nhập...</div>;
    if (!user) return <Navigate to="/dang-nhap" replace state={{ from: `${location.pathname}${location.search}` }} />;
    if (!canAccessPath(user, path)) return <Navigate to={landingPathForRole(user)} replace />;
    return children;
}

function ProtectedPage({ path, children }) {
    return (
        <RequireAccess path={path}>
            {children}
        </RequireAccess>
    );
}

function App() {
    useEffect(() => preloadDevPages(), []);

    return (
      <>
      <ToastHost />
      <Suspense fallback={<div className="route-loading">Đang tải trang...</div>}>
        <Routes>
            <Route element={<Layout />}>
                <Route path="/" element={<Navigate to="/trang-chu" replace />} />
                <Route path="/trang-chu" element={<HomePage />} />
                <Route path="/gioi-thieu" element={<AboutPage />} />
                <Route
                    path="/sinh-cau-hoi"
                    element={(
                        <ProtectedPage path="/sinh-cau-hoi">
                            <GeneratePage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly"
                    element={(
                        <ProtectedPage path="/quan-ly">
                            <ManagePage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly-hoc-phan"
                    element={(
                        <ProtectedPage path="/quan-ly-hoc-phan">
                            <SubjectManage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly-tai-lieu"
                    element={(
                        <ProtectedPage path="/quan-ly-tai-lieu">
                            <DocumentManagePage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/lam-de-thi"
                    element={(
                        <ProtectedPage path="/lam-de-thi">
                            <ExamListPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/lam-de-thi/:examId"
                    element={(
                        <ProtectedPage path="/lam-de-thi/:examId">
                            <ExamBuilderPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/kiem-duyet"
                    element={(
                        <ProtectedPage path="/kiem-duyet">
                            <ReviewQueuePage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/duyet-ai"
                    element={(
                        <ProtectedPage path="/duyet-ai">
                            <AdminAiReviewPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/tong-quan"
                    element={(
                        <ProtectedPage path="/tong-quan">
                            <AdminOverviewPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/danh-muc"
                    element={(
                        <ProtectedPage path="/danh-muc">
                            <CatalogAdminPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly-nguoi-dung"
                    element={(
                        <ProtectedPage path="/quan-ly-nguoi-dung">
                            <UsersAdminPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/nhat-ky-he-thong"
                    element={(
                        <ProtectedPage path="/nhat-ky-he-thong">
                            <AdminAuditPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly-job"
                    element={(
                        <ProtectedPage path="/quan-ly-job">
                            <AdminJobsPage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/quan-ly-moodle"
                    element={(
                        <ProtectedPage path="/quan-ly-moodle">
                            <AdminMoodlePage />
                        </ProtectedPage>
                    )}
                />
                <Route
                    path="/lich-cong-viec"
                    element={(
                        <ProtectedPage path="/lich-cong-viec">
                            <TaskCalendarPage />
                        </ProtectedPage>
                    )}
                />
                <Route path="/huong-dan" element={<GuidePage />} />
                <Route path="/lien-he" element={<ContactPage />} />
                <Route
                    path="/ho-so"
                    element={(
                        <ProtectedPage path="/ho-so">
                            <UserProfile />
                        </ProtectedPage>
                    )}
                />
            </Route>

            <Route path="/dang-nhap" element={<LoginPage />} />
            <Route path="/dang-ky" element={<RegisterPage />} />
            <Route path="*" element={<Navigate to="/trang-chu" replace />} />
        </Routes>
      </Suspense>
      </>
    );
}

export default App;
