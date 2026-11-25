import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  FileText, Users, Cpu, TrendingUp, Plus, Mail,
  CheckCircle, Clock, Lock, Unlock, AlertTriangle,
  MoreHorizontal, RefreshCw,
} from "lucide-react";
import { rfpApi } from "../api/client";
import { useAuth } from "../context/useAuth";
import clsx from "clsx";

// ── Status badge ──────────────────────────────────────────────────────────────
const STATUS = {
  draft:                  { label: "Draft",           cls: "bg-gray-100 text-gray-600 border-gray-200" },
  criteria_approved:      { label: "Criteria Set",    cls: "bg-blue-50 text-blue-700 border-blue-200" },
  vendors_uploaded:       { label: "Vendors In",      cls: "bg-violet-50 text-violet-700 border-violet-200" },
  ai_eval_complete:       { label: "AI Complete",     cls: "bg-amber-50 text-amber-700 border-amber-200" },
  human_eval_in_progress: { label: "Human Review",   cls: "bg-orange-50 text-orange-700 border-orange-200" },
  consensus:              { label: "Consensus",       cls: "bg-purple-50 text-purple-700 border-purple-200" },
  locked:                 { label: "Locked",          cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  ingesting:              { label: "Processing",      cls: "bg-blue-50 text-blue-700 border-blue-200" },
  ready:                  { label: "Ready",           cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  error:                  { label: "Error",           cls: "bg-red-50 text-crimson border-crimson/20" },
};

function StatusBadge({ status }) {
  const s = STATUS[status] || STATUS.draft;
  return (
    <span className={clsx("text-[10px] font-mono border px-2 py-0.5 rounded-full", s.cls)}>
      {s.label}
    </span>
  );
}

const USER_STATUS_CLS = {
  active:      "bg-emerald-50 text-emerald-700 border-emerald-200",
  pending:     "bg-amber-50 text-amber-700 border-amber-200",
  deactivated: "bg-gray-100 text-gray-500 border-gray-200",
};

const ROLE_LABELS = {
  admin:                "Admin",
  technical_evaluator:  "Technical",
  financial_evaluator:  "Financial",
  operations_evaluator: "Operations",
  legal_compliance:     "Legal",
  business_rep:         "Business Rep",
  ai_auditor:           "AI Auditor",
};

function StatCard({ icon: Icon, label, value, sub, accent }) {
  return (
    <div className="card p-5 flex items-start gap-4">
      <div className={clsx("w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0", accent)}>
        <Icon size={18} className="text-white" />
      </div>
      <div>
        <div className="text-2xl font-heading text-ink leading-none">{value}</div>
        <div className="text-xs text-ink-muted mt-0.5 font-body">{label}</div>
        {sub && <div className="text-[10px] text-ink-muted/60 mt-0.5 font-mono">{sub}</div>}
      </div>
    </div>
  );
}

// ── Invite modal ──────────────────────────────────────────────────────────────
function InviteModal({ onClose, onInvited }) {
  const [email,   setEmail]   = useState("");
  const [role,    setRole]    = useState("technical_evaluator");
  const [name,    setName]    = useState("");
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState("");
  const [result,  setResult]  = useState(null);

  async function handleSubmit(e) {
    e.preventDefault();
    setLoading(true); setError("");
    try {
      const r = await rfpApi.inviteUser({ email, role, display_name: name });
      setResult(r.data);
      onInvited();
    } catch (err) {
      setError(err?.response?.data?.detail || "Invite failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
         onClick={onClose}>
      <div className="w-full max-w-md bg-white rounded-2xl shadow-2xl border border-steel p-6"
           onClick={e => e.stopPropagation()}>
        <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Invite User</div>
        <div className="font-heading text-xl text-ink mb-5">Add a team member</div>

        {result ? (
          <div className="text-center py-4">
            <CheckCircle size={40} className="text-emerald-500 mx-auto mb-3" />
            <div className="font-medium text-ink mb-1">Invite sent to {result.email}</div>
            <div className="text-xs text-ink-muted mb-4">
              Share this link with them (expires in 48h):
            </div>
            <div className="bg-ivory border border-steel rounded-lg px-3 py-2 text-xs font-mono break-all text-ink-muted">
              {window.location.origin}{result.accept_url}
            </div>
            <button onClick={onClose} className="btn-primary mt-5 w-full">Done</button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Email *
              </label>
              <input type="email" required value={email} onChange={e => setEmail(e.target.value)}
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson" />
            </div>
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Display name
              </label>
              <input type="text" value={name} onChange={e => setName(e.target.value)}
                placeholder="Optional"
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson" />
            </div>
            <div>
              <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
                Role *
              </label>
              <select value={role} onChange={e => setRole(e.target.value)}
                className="w-full border border-steel rounded-lg px-3 py-2.5 text-sm bg-white focus:outline-none focus:border-crimson">
                {Object.entries(ROLE_LABELS).filter(([k]) => k !== "admin").map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </select>
            </div>
            {error && <div className="text-crimson text-xs bg-crimson/5 border border-crimson/20 rounded px-3 py-2">{error}</div>}
            <div className="flex gap-3 pt-1">
              <button type="button" onClick={onClose} className="btn-ghost flex-1">Cancel</button>
              <button type="submit" disabled={loading || !email}
                className="btn-primary flex-1 flex items-center justify-center gap-2">
                <Mail size={14} />{loading ? "Sending…" : "Send invite"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function TenantDashboard() {
  const { user }    = useAuth();
  const navigate    = useNavigate();
  const [tenant,    setTenant]    = useState(null);
  const [rfps,      setRfps]      = useState([]);
  const [users,     setUsers]     = useState([]);
  const [showInvite, setShowInvite] = useState(false);
  const [loading,   setLoading]   = useState(true);

  async function load() {
    setLoading(true);
    const [tenantRes, rfpsRes, usersRes] = await Promise.allSettled([
      rfpApi.getTenant(),
      rfpApi.list(),
      rfpApi.listUsers(),
    ]);
    if (tenantRes.status === "fulfilled") setTenant(tenantRes.value.data);
    if (rfpsRes.status === "fulfilled") {
      setRfps(Array.isArray(rfpsRes.value.data) ? rfpsRes.value.data : []);
    }
    if (usersRes.status === "fulfilled") {
      setUsers(Array.isArray(usersRes.value.data) ? usersRes.value.data : []);
    }
    setLoading(false);
  }

  useEffect(() => { load(); }, []);

  const activeRfps   = rfps.filter(r => !["locked","error"].includes(r.status));
  const lockedRfps   = rfps.filter(r => r.status === "locked");
  const pendingRubric = rfps.filter(r => r.status === "criteria_approved" || (r.status === "draft" && !r.rubric_locked)).length;

  if (loading) return (
    <div className="flex items-center justify-center min-h-[400px] text-ink-muted gap-3 flex-col">
      <RefreshCw size={24} className="animate-spin text-crimson" />
      <div className="text-xs font-mono uppercase tracking-widest">Loading tenant data…</div>
    </div>
  );

  return (
    <div className="p-8 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            {tenant?.name || "Overview"}
          </div>
        </div>
        <button onClick={() => navigate("/upload")} className="btn-primary flex items-center gap-2">
          <Plus size={16} /> New RFP
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-8">
        <StatCard icon={FileText}    label="Active RFPs"     value={activeRfps.length}                          accent="bg-crimson"      />
        <StatCard icon={Users}       label="Users"           value={`${users.length}/${tenant?.max_users || 25}`} sub="active / limit"  accent="bg-ink"          />
        <StatCard icon={Cpu}         label="Vendors active"  value={rfps.reduce((a,r) => a+(r.vendor_count||0),0)} accent="bg-violet-500" />
        <StatCard icon={Lock}        label="Completed RFPs"  value={lockedRfps.length}                          accent="bg-emerald-600"  />
      </div>

      {/* Attention banners */}
      {pendingRubric > 0 && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
          <Unlock size={16} className="flex-shrink-0 text-amber-500" />
          <span><strong>{pendingRubric} RFP{pendingRubric !== 1 ? "s" : ""}</strong> awaiting rubric approval</span>
        </div>
      )}

      <div className="grid grid-cols-5 gap-6">
        {/* Recent RFPs — 3 cols */}
        <div className="col-span-3 space-y-4">
          <div className="card overflow-hidden">
            <div className="px-6 py-4 border-b border-steel flex items-center justify-between">
              <span className="font-heading text-base text-ink">Recent RFPs</span>
              <button onClick={() => navigate("/dashboard")} className="text-xs text-crimson hover:underline font-mono">
                View all →
              </button>
            </div>
            <div className="divide-y divide-steel/50">
              {rfps.slice(0, 6).map(rfp => (
                <div key={rfp.rfp_id}
                  onClick={() => navigate(`/evaluation/${rfp.rfp_id}`)}
                  className="px-6 py-4 flex items-center justify-between cursor-pointer hover:bg-ivory transition-colors">
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-sm text-ink truncate">{rfp.title}</div>
                    <div className="text-[10px] text-ink-muted font-mono mt-0.5">
                      {rfp.vendor_count || 0} vendors · {rfp.requirement_count || 0} requirements
                    </div>
                  </div>
                  <div className="flex items-center gap-3 ml-4 flex-shrink-0">
                    <StatusBadge status={rfp.status} />
                    {rfp.status === "consensus" && (
                      <button
                        onClick={e => { e.stopPropagation(); navigate(`/evaluation/${rfp.rfp_id}/consensus`); }}
                        className="text-[10px] text-crimson font-mono border border-crimson/30 px-2 py-0.5 rounded hover:bg-crimson/5">
                        Review →
                      </button>
                    )}
                    {rfp.status === "locked" && (
                      <Lock size={12} className="text-emerald-500" />
                    )}
                  </div>
                </div>
              ))}
              {rfps.length === 0 && (
                <div className="px-6 py-10 text-center text-ink-muted text-sm italic">
                  No RFPs yet. <button onClick={() => navigate("/upload")} className="text-crimson underline">Create one.</button>
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Right column — 2 cols */}
        <div className="col-span-2 space-y-4">
          {/* User management */}
          <div className="card overflow-hidden">
            <div className="px-5 py-4 border-b border-steel flex items-center justify-between">
              <span className="font-heading text-sm text-ink">Team</span>
              <button onClick={() => setShowInvite(true)}
                className="text-[10px] font-mono text-white bg-crimson px-2.5 py-1 rounded-md flex items-center gap-1 hover:bg-crimson/90">
                <Plus size={10} /> Invite
              </button>
            </div>
            <div className="divide-y divide-steel/50 max-h-64 overflow-y-auto">
              {users.map(u => (
                <div key={u.user_id} className="px-5 py-3 flex items-center justify-between">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-ink truncate">{u.display_name || u.email}</div>
                    <div className="text-[10px] text-ink-muted font-mono">
                      {ROLE_LABELS[u.role] || u.role}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 ml-2 flex-shrink-0">
                    <span className={clsx("text-[9px] font-mono border px-1.5 py-0.5 rounded-full", USER_STATUS_CLS[u.status] || USER_STATUS_CLS.pending)}>
                      {u.status}
                    </span>
                    <div className="text-[9px] text-ink-muted font-mono">
                      {u.last_active_at
                        ? new Date(u.last_active_at).toLocaleDateString()
                        : u.status === "pending" ? "Never" : "—"}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* Usage */}
          <div className="card p-5">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-4">
              Usage & Plan
            </div>
            <div className="space-y-3">
              {[
                {
                  label: "Users",
                  used:  users.length,
                  max:   tenant?.max_users || 25,
                },
                {
                  label: "RFPs this month",
                  used:  rfps.length,
                  max:   tenant?.max_rfps_per_month || 10,
                },
              ].map(item => (
                <div key={item.label}>
                  <div className="flex justify-between text-xs font-body mb-1">
                    <span className="text-ink-muted">{item.label}</span>
                    <span className="font-mono text-ink">{item.used} / {item.max}</span>
                  </div>
                  <div className="h-1.5 bg-steel-light rounded-full overflow-hidden">
                    <div
                      className={clsx("h-full rounded-full transition-all",
                        item.used / item.max > 0.8 ? "bg-crimson" : "bg-emerald-500")}
                      style={{ width: `${Math.min((item.used / item.max) * 100, 100)}%` }}
                    />
                  </div>
                </div>
              ))}
              {tenant?.next_billing_date && (
                <div className="text-[10px] text-ink-muted font-mono pt-1 border-t border-steel/50">
                  Next billing: {new Date(tenant.next_billing_date).toLocaleDateString()}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {showInvite && (
        <InviteModal onClose={() => setShowInvite(false)} onInvited={load} />
      )}
    </div>
  );
}
