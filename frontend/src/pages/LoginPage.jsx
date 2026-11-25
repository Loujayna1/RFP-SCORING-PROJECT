import { useState } from "react";
import { useNavigate, useLocation, Navigate } from "react-router-dom";
import { Loader2, LogIn } from "lucide-react";
import { useAuth } from "../context/useAuth";
import logo from "../assets/logo.png";
export default function LoginPage() {
  const { login, user }  = useAuth();
  const navigate         = useNavigate();
  const location         = useLocation();

  // Sanitise the redirect target so we never send the user back to /login
  const rawFrom = location.state?.from?.pathname;
  const from    = (rawFrom && rawFrom !== "/login" && rawFrom !== "/signup")
    ? rawFrom
    : "/";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error,    setError]    = useState("");
  const [loading,  setLoading]  = useState(false);

  // Already logged in → go to the role-based home
  if (user) return <Navigate to="/" replace />;

  async function handleSubmit(e) {
    e.preventDefault();
    if (!email.trim() || !password) return;
    setError("");
    setLoading(true);
    try {
      await login(email.trim(), password);
      navigate(from, { replace: true });
    } catch (err) {
      setError(
        err?.response?.data?.detail ||
        "Invalid email or password. Please try again.",
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen bg-ivory flex items-center justify-center px-4"
         style={{
           backgroundImage: "url(\"data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ED1C24' fill-opacity='0.03'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E\")",
         }}
    >
      <div className="w-full max-w-sm">
        {/* Brand mark */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-crimson rounded-2xl shadow-lg mb-5">
            <img
  src={logo}
  alt="RFP Response Evaluator Logo"
  className="w-20 h-20 object-contain"
/>
          </div>
          <div className="font-display text-3xl text-ink tracking-wide leading-none">
            RFP RESPONSE EVALUATOR
          </div>
          <div className="text-xs text-ink-muted font-mono uppercase tracking-widest mt-2">
            Internal Procurement Tool
          </div>
        </div>

        {/* Card */}
        <div className="card p-8 shadow-xl">
          <div className="text-sm text-ink-muted font-body mb-6 text-center">
            Sign in with your procurement credentials
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Email
              </label>
              <input
                type="email"
                autoComplete="email"
                autoFocus
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@company.com"
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors"
              />
            </div>

            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Password
              </label>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors"
              />
            </div>

            {error && (
              <div className="text-crimson text-xs font-body bg-crimson/5 border border-crimson/20 rounded-lg px-3 py-2.5">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !email.trim() || !password}
              className="btn-primary w-full flex items-center justify-center gap-2 mt-2"
            >
              {loading ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <LogIn size={16} />
              )}
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
