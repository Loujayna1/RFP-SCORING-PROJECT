import { useState } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Loader2, Building2, FileText } from "lucide-react";
import { rfpApi } from "../api/client";
import { useAuth } from "../context/useAuth";
import logo from "../assets/logo.png";

export default function TenantSignup() {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [form, setForm] = useState({
    company_name: "",
    admin_email: "",
    admin_password: "",
    confirm: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  function setField(key, value) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  async function handleSubmit(e) {
    e.preventDefault();
    if (form.admin_password !== form.confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (form.admin_password.length < 8) {
      setError("Password must be at least 8 characters.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      await rfpApi.registerTenant({
        company_name: form.company_name.trim(),
        admin_email: form.admin_email.trim().toLowerCase(),
        admin_password: form.admin_password,
      });
      await login(form.admin_email.trim().toLowerCase(), form.admin_password);
      navigate("/tenant", { replace: true });
    } catch (err) {
      const detail = err?.response?.data?.detail;
      const msg = Array.isArray(detail)
        ? detail.map((d) => d.msg).join(" ")
        : typeof detail === "string"
          ? detail
          : "Registration failed. Please try again.";
      setError(msg);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div
      className="min-h-screen bg-ivory flex items-center justify-center px-4"
      style={{
        backgroundImage:
          "url(\"data:image/svg+xml,%3Csvg width='60' height='60' viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg fill='%23ED1C24' fill-opacity='0.03'%3E%3Cpath d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g%3E%3C/svg%3E\")",
      }}
    >
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-14 h-14 bg-crimson rounded-2xl shadow-lg mb-5">
            <img src={logo} alt="RFP Response Evaluator Logo" className="w-20 h-20 object-contain" />
          </div>
          <div className="font-display text-3xl text-ink tracking-wide">RFP RESPONSE EVALUATOR</div>
          <div className="text-xs text-ink-muted font-mono uppercase tracking-widest mt-1">
            Procurement committee registration
          </div>
        </div>

        <div className="card p-8 shadow-xl">
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Company name *
              </label>
              <input
                required
                value={form.company_name}
                onChange={(e) => setField("company_name", e.target.value)}
                placeholder="Acme Corporation"
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson"
              />
            </div>

            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Admin email *
              </label>
              <input
                required
                type="email"
                value={form.admin_email}
                onChange={(e) => setField("admin_email", e.target.value)}
                placeholder="admin@company.com"
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson"
              />
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                  Password *
                </label>
                <input
                  required
                  type="password"
                  value={form.admin_password}
                  onChange={(e) => setField("admin_password", e.target.value)}
                  placeholder="Min 8 chars"
                  className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson"
                />
              </div>
              <div>
                <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                  Confirm *
                </label>
                <input
                  required
                  type="password"
                  value={form.confirm}
                  onChange={(e) => setField("confirm", e.target.value)}
                  placeholder="Repeat"
                  className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson"
                />
              </div>
            </div>

            {error && (
              <div className="text-crimson text-xs bg-crimson/5 border border-crimson/20 rounded-lg px-3 py-2.5">
                {error}
              </div>
            )}

            <button
              type="submit"
              disabled={loading || !form.company_name || !form.admin_email || !form.admin_password}
              className="btn-primary w-full flex items-center justify-center gap-2 mt-2"
            >
              {loading ? <Loader2 size={16} className="animate-spin" /> : <Building2 size={16} />}
              {loading ? "Creating workspace…" : "Create workspace"}
            </button>
          </form>
        </div>

        <div className="text-center mt-5 text-sm text-ink-muted">
          Already have a workspace?{" "}
          <Link to="/login" className="text-crimson hover:underline font-medium">
            Sign in
          </Link>
        </div>
      </div>
    </div>
  );
}
