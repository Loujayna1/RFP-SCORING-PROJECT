import { Suspense, lazy, Component } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { AuthProvider } from "./context/AuthContext";
import { useAuth } from "./context/useAuth";
import Layout           from "./components/Layout";

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: "2rem", fontFamily: "monospace", color: "#c00" }}>
          <h2>Something went wrong</h2>
          <pre style={{ whiteSpace: "pre-wrap", fontSize: "0.8rem", color: "#333", marginTop: "1rem" }}>
            {this.state.error?.message}
            {"\n"}
            {this.state.error?.stack}
          </pre>
          <button
            style={{ marginTop: "1rem", padding: "0.5rem 1rem", cursor: "pointer" }}
            onClick={() => { this.setState({ error: null }); window.location.href = "/login"; }}
          >
            Back to Login
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

const LoginPage = lazy(() => import("./pages/LoginPage"));
const TenantSignup = lazy(() => import("./pages/TenantSignup"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const TenantDashboard = lazy(() => import("./pages/TenantDashboard"));
const UploadRFP = lazy(() => import("./pages/UploadRFP"));
const EvaluationDetail = lazy(() => import("./pages/EvaluationDetail"));
const VendorUpload = lazy(() => import("./pages/VendorUpload"));
const VendorDetail = lazy(() => import("./pages/VendorDetail"));
const Archive = lazy(() => import("./pages/Archive"));
const ScoreEditor = lazy(() => import("./pages/ScoreEditor"));
const AuditLog = lazy(() => import("./pages/AuditLog"));
const RubricApproval = lazy(() => import("./pages/RubricApproval"));
const EvaluatorDashboard = lazy(() => import("./pages/EvaluatorDashboard"));
const BusinessRepVote = lazy(() => import("./pages/BusinessRepVote"));
const ConsensusView = lazy(() => import("./pages/ConsensusView"));
const CompareWorkspace = lazy(() => import("./pages/CompareWorkspace"));

function RouteFallback() {
  return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", background: "#FAF8F5" }}>
      <div style={{ fontSize: "11px", fontFamily: "monospace", color: "#6B4B51", letterSpacing: "0.1em", textTransform: "uppercase" }}>
        Loading…
      </div>
    </div>
  );
}

// ── ProtectedRoute ────────────────────────────────────────────────────────────
function ProtectedRoute({ children, roles }) {
  const { user, initialising } = useAuth();
  const location = useLocation();

  if (initialising) {
    return (
      <div className="min-h-screen bg-ivory flex items-center justify-center text-ink-muted">
        <div className="text-xs font-mono uppercase tracking-widest">Loading…</div>
      </div>
    );
  }
  if (!user) return <Navigate to="/login" state={{ from: location }} replace />;

  if (roles && !roles.includes(user.role)) {
    // Role-based redirect: evaluators → their dashboard home
    const evaluatorRoles = [
      "technical_evaluator","financial_evaluator",
      "operations_evaluator","legal_compliance",
    ];
    if (evaluatorRoles.includes(user.role)) {
      return <Navigate to="/my-evaluations" replace />;
    }
    if (user.role === "business_rep") {
      return <Navigate to="/dashboard" replace />;
    }
    return <Navigate to="/dashboard" replace />;
  }

  return children;
}

// ── Smart index redirect based on role ───────────────────────────────────────
function RoleIndex() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  const evaluatorRoles = [
    "technical_evaluator","financial_evaluator",
    "operations_evaluator","legal_compliance",
  ];
  if (evaluatorRoles.includes(user.role)) return <Navigate to="/my-evaluations" replace />;
  if (user.role === "business_rep")       return <Navigate to="/dashboard" replace />;
  return <Navigate to="/tenant" replace />;
}

// ── Routes ────────────────────────────────────────────────────────────────────
function AppRoutes() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        {/* Public */}
        <Route path="/login"  element={<LoginPage />} />
        <Route path="/signup" element={<TenantSignup />} />

        {/* Protected — all inside Layout */}
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Layout />
            </ProtectedRoute>
          }
        >
          <Route index element={<RoleIndex />} />

          {/* Admin routes */}
          <Route path="tenant"    element={<ProtectedRoute roles={["admin","ai_auditor"]}><TenantDashboard /></ProtectedRoute>} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="archive"   element={<Archive />} />
          <Route path="upload"    element={<ProtectedRoute roles={["admin"]}><UploadRFP /></ProtectedRoute>} />
          <Route path="audit-log" element={<ProtectedRoute roles={["admin","ai_auditor"]}><AuditLog /></ProtectedRoute>} />

          {/* RFP flows */}
          <Route path="evaluation/:rfpId"                    element={<EvaluationDetail />} />
          <Route path="evaluation/:rfpId/compare"            element={<CompareWorkspace />} />
          <Route path="evaluation/:rfpId/vendor-upload"      element={<ProtectedRoute roles={["admin"]}><VendorUpload /></ProtectedRoute>} />
          <Route path="evaluation/:rfpId/rubric"             element={<ProtectedRoute roles={["admin"]}><RubricApproval /></ProtectedRoute>} />
          <Route path="evaluation/:rfpId/consensus"          element={<ProtectedRoute roles={["admin"]}><ConsensusView /></ProtectedRoute>} />
          <Route path="evaluation/:rfpId/vendor/:responseId" element={<VendorDetail />} />
          <Route path="score-editor/:rfpId"                  element={<ProtectedRoute roles={["admin"]}><ScoreEditor /></ProtectedRoute>} />

          {/* Evaluator routes */}
          <Route path="my-evaluations"                       element={<Dashboard />} />
          <Route path="evaluation/:rfpId/my-review"          element={<EvaluatorDashboard />} />

          {/* Business rep routes */}
          <Route path="evaluation/:rfpId/vote"               element={<ProtectedRoute roles={["business_rep","admin"]}><BusinessRepVote /></ProtectedRoute>} />
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </ErrorBoundary>
  );
}
