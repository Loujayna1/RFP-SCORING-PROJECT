import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Plus,
  FileText,
  Users,
  MessageSquare,
  Clock,
  CheckCircle,
  AlertTriangle,
  Calendar,
  Lock,
  Unlock,
  ClipboardList,
  ShieldAlert,
  UserRoundX,
  Siren,
} from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

function StatusBadge({ status }) {
  const map = {
    ready:     { label: "Ready",     cls: "bg-emerald-50 text-emerald-700 border-emerald-200" },
    scoring:   { label: "Scoring",   cls: "bg-amber-50 text-amber-700 border-amber-200" },
    ingesting: { label: "Ingesting", cls: "bg-blue-50 text-blue-700 border-blue-200" },
    error:     { label: "Error",     cls: "bg-red-50 text-crimson border-crimson/20" },
  };
  const { label, cls } = map[status] || map.ingesting;
  return (
    <span className={clsx("text-xs font-mono border px-2 py-0.5 rounded-full", cls)}>
      {label}
    </span>
  );
}

// FIX — rubric lock badge shown per-row so procurement officers can see
// at a glance which RFPs still need rubric approval before scoring is trusted.
function RubricBadge({ locked }) {
  if (locked) {
    return (
      <span
        title="Rubric approved and locked"
        className="inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded-full border bg-emerald-50 text-emerald-700 border-emerald-200"
      >
        <Lock size={9} /> Locked
      </span>
    );
  }
  return (
    <span
      title="Rubric not yet approved — scores computed against unreviewed criteria weights"
      className="inline-flex items-center gap-1 text-[10px] font-mono px-2 py-0.5 rounded-full border bg-amber-50 text-amber-700 border-amber-200"
    >
      <Unlock size={9} /> Pending
    </span>
  );
}

function Stat({ icon: Icon, label, value, accent }) {
  return (
    <div className="card p-5 flex items-start gap-4 animate-slide-up">
      <div className={clsx("w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0", accent)}>
        <Icon size={18} className="text-white" />
      </div>
      <div>
        <div className="text-2xl font-heading text-ink leading-none">{value}</div>
        <div className="text-xs text-ink-muted mt-1 font-body">{label}</div>
      </div>
    </div>
  );
}

function getRfpYear(rfp) {
  const date = new Date(rfp.created_at);
  return Number.isNaN(date.getTime()) ? null : date.getFullYear();
}

function sortByCreatedAtDesc(a, b) {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

function buildYearOptions(rfps, currentYear, yearsBack = 9) {
  const dataYears   = [...new Set(rfps.map(getRfpYear).filter(Boolean))];
  const rollingYears = Array.from({ length: yearsBack + 1 }, (_, i) => currentYear - i);
  return [...new Set([...dataYears, ...rollingYears])].sort((a, b) => b - a);
}

const REQUIRED_EVALUATOR_ROLES = [
  "technical_evaluator",
  "financial_evaluator",
  "operations_evaluator",
  "legal_compliance",
];

function ActionRow({ action, navigate }) {
  const Icon = action.icon;
  return (
    <div className="rounded-xl border border-steel/60 bg-white p-3.5 flex items-start justify-between gap-3">
      <div className="flex items-start gap-2.5 min-w-0">
        <span className={clsx("mt-0.5 inline-flex items-center justify-center w-7 h-7 rounded-lg", action.iconBg)}>
          <Icon size={14} className={action.iconFg} />
        </span>
        <div className="min-w-0">
          <div className="text-sm font-medium text-ink truncate">{action.title}</div>
          <div className="text-xs text-ink-muted mt-0.5 leading-relaxed">{action.detail}</div>
          <div className="flex items-center gap-2 mt-1.5">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">
              Priority {action.priority}
            </div>
            <span className="text-[10px] font-mono uppercase tracking-widest text-ink-muted bg-ivory px-1.5 py-0.5 rounded border border-steel/60">
              {action.signal_label}
            </span>
          </div>
        </div>
      </div>
      <button
        onClick={() => navigate(action.path)}
        className="text-xs text-crimson hover:underline font-medium whitespace-nowrap"
      >
        {action.cta}
      </button>
    </div>
  );
}

export default function Dashboard() {
  const [rfps,         setRfps]         = useState([]);
  const [selectedYear, setSelectedYear] = useState(new Date().getFullYear());
  const [deleteTarget, setDeleteTarget] = useState(null);
  const [attentionItems, setAttentionItems] = useState([]);
  const [attentionLoading, setAttentionLoading] = useState(false);
  const [attentionFilter, setAttentionFilter] = useState("all");
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    let intervalId;

    const fetchRfps = () => {
      rfpApi
        .list()
        .then((r) => {
          if (cancelled) return;
          const nextRfps = Array.isArray(r.data) ? r.data : [];
          setRfps(nextRfps);
          const hasActive = nextRfps.some((rfp) =>
            ["ingesting", "scoring"].includes(rfp.status),
          );
          if (hasActive && !intervalId) {
            intervalId = window.setInterval(fetchRfps, 3000);
          }
          if (!hasActive && intervalId) {
            window.clearInterval(intervalId);
            intervalId = null;
          }
        })
        .catch(() => {});
    };

    fetchRfps();
    return () => {
      cancelled = true;
      if (intervalId) window.clearInterval(intervalId);
    };
  }, []);

  const currentYear  = new Date().getFullYear();
  const yearOptions  = buildYearOptions(rfps, currentYear);
  const fallbackYear = yearOptions.includes(currentYear) ? currentYear : (yearOptions[0] ?? currentYear);
  const activeYear   = yearOptions.includes(selectedYear) ? selectedYear : fallbackYear;
  const now          = new Date();

  const upcomingRfps = rfps
    .filter((r) => r.submission_deadline && new Date(r.submission_deadline) > now)
    .sort((a, b) => new Date(a.submission_deadline) - new Date(b.submission_deadline))
    .slice(0, 3);

  const overdueRfps = rfps
    .filter((r) => r.submission_deadline && new Date(r.submission_deadline) < now)
    .sort((a, b) => new Date(b.submission_deadline) - new Date(a.submission_deadline))
    .slice(0, 3);

  const filteredRfps = rfps
    .filter((rfp) => getRfpYear(rfp) === activeYear)
    .sort(sortByCreatedAtDesc);

  // FIX — count how many ready RFPs still have unlocked rubrics (attention needed)
  const pendingRubricCount = filteredRfps.filter(
    (r) => r.status === "ready" && !r.rubric_locked,
  ).length;

  useEffect(() => {
    let cancelled = false;

    async function buildAttentionQueue() {
      if (!filteredRfps.length) {
        setAttentionItems([]);
        return;
      }

      setAttentionLoading(true);

      const now = new Date();
      const itemsByRfp = await Promise.all(
        filteredRfps.map(async (rfp) => {
          const actions = [];

          if (!rfp.rubric_locked) {
            actions.push({
              key: `${rfp.rfp_id}-rubric`,
              title: `${rfp.title}`,
              detail: "Rubric approval is pending; scoring confidence is reduced until locked.",
              priority: 95,
              signal: "rubric",
              signal_label: "Rubric",
              path: `/evaluation/${rfp.rfp_id}/rubric`,
              cta: "Review rubric",
              icon: ClipboardList,
              iconBg: "bg-amber-50",
              iconFg: "text-amber-600",
            });
          }

          try {
            const assignRes = await rfpApi.listAssignments(rfp.rfp_id);
            const assigned = new Set((assignRes.data || []).map((a) => a.evaluator_role));
            const missing = REQUIRED_EVALUATOR_ROLES.filter((role) => !assigned.has(role));
            if (missing.length) {
              actions.push({
                key: `${rfp.rfp_id}-assignments`,
                title: `${rfp.title}`,
                detail: `${missing.length} evaluator role(s) missing assignments (${missing.join(", ").replaceAll("_", " ")}).`,
                priority: 90,
                signal: "assignments",
                signal_label: "Assignments",
                path: `/evaluation/${rfp.rfp_id}`,
                cta: "Assign reviewers",
                icon: UserRoundX,
                iconBg: "bg-red-50",
                iconFg: "text-crimson",
              });
            }
          } catch {
            // Skip assignment signal if endpoint is temporarily unavailable.
          }

          try {
            const conflicts = await rfpApi.getConflicts(rfp.rfp_id);
            const conflictCount = Number(conflicts.data?.conflict_count) || 0;
            if (conflictCount > 0) {
              actions.push({
                key: `${rfp.rfp_id}-conflicts`,
                title: `${rfp.title}`,
                detail: `${conflictCount} unresolved cross-vendor conflict(s) require adjudication.`,
                priority: 85,
                signal: "conflicts",
                signal_label: "Conflicts",
                path: `/evaluation/${rfp.rfp_id}/consensus`,
                cta: "Resolve conflicts",
                icon: Siren,
                iconBg: "bg-red-50",
                iconFg: "text-crimson",
              });
            }
          } catch {
            // Skip conflict signal if endpoint is temporarily unavailable.
          }

          try {
            const compare = await rfpApi.compare(rfp.rfp_id);
            const vendorDetails = compare.data?.vendor_details || [];
            if (vendorDetails.length) {
              const flaggedCounts = await Promise.all(
                vendorDetails.map(async (v) => {
                  try {
                    const review = await rfpApi.getVendorReview(rfp.rfp_id, v.response_id);
                    return Number(review.data?.total) || 0;
                  } catch {
                    return 0;
                  }
                }),
              );
              const lowConfidenceCount = flaggedCounts.reduce((sum, n) => sum + n, 0);
              if (lowConfidenceCount > 0) {
                actions.push({
                  key: `${rfp.rfp_id}-confidence`,
                  title: `${rfp.title}`,
                  detail: `${lowConfidenceCount} low-confidence score(s) are pending human review.`,
                  priority: 80,
                  signal: "confidence",
                  signal_label: "Confidence",
                  path: `/evaluation/${rfp.rfp_id}`,
                  cta: "Review flagged",
                  icon: ShieldAlert,
                  iconBg: "bg-amber-50",
                  iconFg: "text-amber-700",
                });
              }
            }
          } catch {
            // Skip low-confidence signal if compare/review endpoints fail.
          }

          const isOverdue = rfp.submission_deadline && new Date(rfp.submission_deadline) < now;
          const stillOpen = !["ready", "locked"].includes((rfp.status || "").toLowerCase());
          if (isOverdue && stillOpen && (rfp.vendor_count || 0) > 0) {
            actions.push({
              key: `${rfp.rfp_id}-overdue`,
              title: `${rfp.title}`,
              detail: "Vendor review is overdue and the package is still unresolved.",
              priority: 78,
              signal: "overdue",
              signal_label: "Overdue",
              path: `/evaluation/${rfp.rfp_id}`,
              cta: "Prioritize now",
              icon: AlertTriangle,
              iconBg: "bg-red-50",
              iconFg: "text-crimson",
            });
          }

          return actions;
        }),
      );

      if (cancelled) return;

      const sorted = itemsByRfp
        .flat()
        .sort((a, b) => b.priority - a.priority)
        .slice(0, 12);
      setAttentionItems(sorted);
      setAttentionLoading(false);
    }

    buildAttentionQueue().catch(() => {
      if (!cancelled) {
        setAttentionItems([]);
        setAttentionLoading(false);
      }
    });

    return () => {
      cancelled = true;
    };
  }, [filteredRfps]);

  const attentionFilters = [
    { id: "all", label: "All" },
    { id: "blockers", label: "Blockers" },
    { id: "overdue", label: "Overdue" },
    { id: "confidence", label: "Confidence" },
    { id: "assignments", label: "Assignments" },
    { id: "rubric", label: "Rubric" },
    { id: "conflicts", label: "Conflicts" },
  ];

  const attentionCounts = attentionItems.reduce((acc, item) => {
    acc[item.signal] = (acc[item.signal] || 0) + 1;
    const isBlocker = ["rubric", "assignments", "conflicts"].includes(item.signal);
    if (isBlocker) acc.blockers = (acc.blockers || 0) + 1;
    return acc;
  }, {});

  const filteredAttentionItems = attentionItems.filter((item) => {
    if (attentionFilter === "all") return true;
    if (attentionFilter === "blockers") {
      return ["rubric", "assignments", "conflicts"].includes(item.signal);
    }
    return item.signal === attentionFilter;
  });

  return (
    <div className="p-8">
      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            DASHBOARD
          </div>
          <div className="text-ink-muted font-body text-sm mt-1">
            Manage your year-to-date RFP evaluations
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => navigate("/archive")} className="btn-secondary">
            Archive
          </button>
          <button
            onClick={() => navigate("/upload")}
            className="btn-primary flex items-center gap-2"
          >
            <Plus size={16} /> New RFP
          </button>
        </div>
      </div>

      {/* Year selector */}
      <div className="mb-6 flex items-center gap-3">
        <label htmlFor="dashboard-year" className="text-xs uppercase tracking-widest text-ink-muted font-mono">
          Year
        </label>
        <select
          id="dashboard-year"
          value={activeYear}
          onChange={(e) => setSelectedYear(Number(e.target.value))}
          className="border border-steel rounded-lg px-3 py-2 bg-white text-sm text-ink"
        >
          {yearOptions.map((year) => (
            <option key={year} value={year}>{year}</option>
          ))}
        </select>
      </div>

      {/* FIX — attention banner when rubrics need approval */}
      {pendingRubricCount > 0 && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800 font-body">
          <Unlock size={16} className="flex-shrink-0 text-amber-500" />
          <span>
            <strong>{pendingRubricCount} RFP{pendingRubricCount !== 1 ? "s" : ""}</strong>{" "}
            {pendingRubricCount !== 1 ? "have" : "has"} unlocked rubrics — vendor scores may be
            computed against unreviewed criteria weights.
          </span>
        </div>
      )}

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-8">
        <Stat icon={FileText}      label="Total RFPs (YTD)"  value={filteredRfps.length}                                     accent="bg-crimson"      />
        <Stat icon={Users}         label="Vendors evaluated"  value={filteredRfps.reduce((a, r) => a + (r.vendor_count || 0), 0)} accent="bg-ink-soft"  />
        <Stat icon={CheckCircle}   label="Completed"          value={filteredRfps.filter((r) => r.status === "ready").length}  accent="bg-emerald-600"  />
        <Stat icon={AlertTriangle} label="In progress"        value={filteredRfps.filter((r) => r.status !== "ready").length}  accent="bg-amber-500"    />
      </div>

      {/* Action queue */}
      <div className="card p-6 mb-8">
        <div className="flex items-center justify-between mb-4">
          <h3 className="font-heading text-base text-ink flex items-center gap-2">
            <Siren size={18} className="text-crimson" /> Needs Attention Next
          </h3>
          <span className="text-[10px] font-mono text-ink-muted uppercase">Priority queue</span>
        </div>

        <div className="flex flex-wrap items-center gap-2 mb-4">
          {attentionFilters.map((f) => {
            const count = f.id === "all" ? attentionItems.length : (attentionCounts[f.id] || 0);
            return (
              <button
                key={f.id}
                onClick={() => setAttentionFilter(f.id)}
                className={clsx(
                  "text-xs font-mono uppercase tracking-widest px-2.5 py-1.5 rounded-lg border transition-colors",
                  attentionFilter === f.id
                    ? "bg-crimson text-white border-crimson"
                    : "bg-white text-ink-muted border-steel hover:text-ink",
                )}
              >
                {f.label} ({count})
              </button>
            );
          })}
        </div>

        {attentionLoading ? (
          <div className="text-xs text-ink-muted italic py-4">Refreshing attention signals...</div>
        ) : filteredAttentionItems.length === 0 ? (
          <div className="text-xs text-ink-muted italic py-4">No urgent blockers detected for this year.</div>
        ) : (
          <div className="space-y-2.5">
            {filteredAttentionItems.map((action) => (
              <ActionRow key={action.key} action={action} navigate={navigate} />
            ))}
          </div>
        )}
      </div>

      {/* Time-sensitive sections */}
      <div className="grid grid-cols-2 gap-6 mb-8">
        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-heading text-base text-ink flex items-center gap-2">
              <Calendar size={18} className="text-emerald-500" /> Upcoming Deadlines
            </h3>
            <span className="text-[10px] font-mono text-ink-muted uppercase">Top 3 Priority</span>
          </div>
          <div className="space-y-3">
            {upcomingRfps.length === 0 ? (
              <div className="text-xs text-ink-muted italic py-4">No upcoming deadlines</div>
            ) : (
              upcomingRfps.map((r) => (
                <div
                  key={r.rfp_id}
                  onClick={() => navigate(`/evaluation/${r.rfp_id}`)}
                  className="group cursor-pointer p-3 bg-ivory rounded-lg border border-steel/50 hover:border-emerald-200 transition-all"
                >
                  <div className="flex justify-between items-start mb-1">
                    <div className="text-sm font-medium text-ink group-hover:text-emerald-700 truncate max-w-[200px]">
                      {r.title}
                    </div>
                    <div className="text-[10px] font-mono font-bold text-emerald-600 bg-emerald-50 px-1.5 py-0.5 rounded">
                      {Math.ceil((new Date(r.submission_deadline) - now) / (1000 * 60 * 60 * 24))} days left
                    </div>
                  </div>
                  <div className="text-[10px] text-ink-muted flex items-center gap-1">
                    <Clock size={10} /> Deadline: {new Date(r.submission_deadline).toLocaleDateString()}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>

        <div className="card p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-heading text-base text-ink flex items-center gap-2">
              <AlertTriangle size={18} className="text-crimson" /> Recently Overdue
            </h3>
            <span className="text-[10px] font-mono text-ink-muted uppercase">Review Needed</span>
          </div>
          <div className="space-y-3">
            {overdueRfps.length === 0 ? (
              <div className="text-xs text-ink-muted italic py-4">No overdue items</div>
            ) : (
              overdueRfps.map((r) => (
                <div
                  key={r.rfp_id}
                  onClick={() => navigate(`/evaluation/${r.rfp_id}`)}
                  className="group cursor-pointer p-3 bg-red-50/30 rounded-lg border border-crimson/10 hover:border-crimson/30 transition-all"
                >
                  <div className="flex justify-between items-start mb-1">
                    <div className="text-sm font-medium text-ink group-hover:text-crimson truncate max-w-[200px]">
                      {r.title}
                    </div>
                    <div className="text-[10px] font-mono font-bold text-crimson bg-crimson/10 px-1.5 py-0.5 rounded">
                      Overdue
                    </div>
                  </div>
                  <div className="text-[10px] text-crimson/60 flex items-center gap-1">
                    <Clock size={10} /> Missed: {new Date(r.submission_deadline).toLocaleDateString()}
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      </div>

      {/* RFP table */}
      <div className="card overflow-hidden">
        <div className="px-6 py-4 border-b border-steel flex items-center justify-between">
          <span className="font-heading text-lg text-ink">RFP Packages ({activeYear})</span>
          <span className="text-xs text-ink-muted font-mono">{filteredRfps.length} total</span>
        </div>

        {filteredRfps.length === 0 ? (
          <div className="py-20 text-center text-ink-muted font-body">
            No RFPs for {activeYear}.{" "}
            <button onClick={() => navigate("/upload")} className="text-crimson underline">
              Upload one.
            </button>
          </div>
        ) : (
          <table className="w-full text-sm font-body">
            <thead>
              <tr className="border-b border-steel bg-ivory">
                {/* FIX — added Rubric column */}
                {["RFP title", "Deadline", "Requirements", "Vendors", "Rubric", "Status", ""].map((h) => (
                  <th
                    key={h}
                    className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredRfps.map((rfp, i) => (
                <tr
                  key={rfp.rfp_id}
                  className={clsx(
                    "border-b border-steel/50 hover:bg-ivory-warm transition-colors cursor-pointer",
                    i % 2 === 0 ? "bg-white" : "bg-ivory",
                  )}
                  onClick={() => navigate(`/evaluation/${rfp.rfp_id}`)}
                >
                  <td className="px-6 py-4">
                    <div className="font-medium text-ink">{rfp.title}</div>
                    <div className="text-xs text-ink-muted mt-0.5 flex items-center gap-1">
                      <Clock size={10} /> {new Date(rfp.created_at).toLocaleDateString()}
                    </div>
                  </td>
                  <td className="px-6 py-4 text-ink">
                    {rfp.submission_deadline
                      ? new Date(rfp.submission_deadline).toLocaleDateString()
                      : "—"}
                  </td>
                  <td className="px-6 py-4 font-mono text-ink">
                    {rfp.requirement_count || "—"}
                  </td>
                  <td className="px-6 py-4 font-mono text-ink">
                    {rfp.vendor_count || 0}
                  </td>
                  {/* FIX — rubric lock badge per row */}
                  <td className="px-6 py-4">
                    <RubricBadge locked={rfp.rubric_locked} />
                  </td>
                  <td className="px-6 py-4">
                    <StatusBadge status={rfp.status} />
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex gap-2 flex-wrap">
                      <button
                        onClick={(e) => { e.stopPropagation(); navigate(`/evaluation/${rfp.rfp_id}`); }}
                        className="text-xs text-crimson hover:underline font-medium"
                      >
                        View
                      </button>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/evaluation/${rfp.rfp_id}`);
                          setTimeout(() => window.dispatchEvent(new CustomEvent("open-chat")), 100);
                        }}
                        className="text-xs text-ink-muted hover:text-ink flex items-center gap-1"
                      >
                        <MessageSquare size={11} /> Chat
                      </button>
                      <button
                        onClick={(e) => { e.stopPropagation(); setDeleteTarget(rfp); }}
                        className="text-xs text-ink-muted hover:text-crimson"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Delete confirmation modal */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 px-4"
          onClick={() => setDeleteTarget(null)}
        >
          <div
            className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl border border-steel"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="text-sm uppercase tracking-widest text-ink-muted font-mono">
              Delete RFP
            </div>
            <div className="mt-2 font-heading text-2xl text-ink">{deleteTarget.title}</div>
            <div className="mt-3 text-sm text-ink-muted font-body">
              This removes the RFP, vendor uploads, scores, reports, and related
              embeddings. This cannot be undone.
            </div>
            <div className="mt-6 flex items-center justify-end gap-3">
              <button className="btn-secondary" onClick={() => setDeleteTarget(null)}>
                Cancel
              </button>
              <button
                className="btn-primary bg-crimson hover:bg-crimson/90"
                onClick={async () => {
                  const target = deleteTarget;
                  setDeleteTarget(null);
                  try {
                    await rfpApi.deleteRfp(target.rfp_id);
                    const res = await rfpApi.list();
                    setRfps(Array.isArray(res.data) ? res.data : []);
                  } catch {
                    alert("Failed to delete RFP");
                  }
                }}
              >
                Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
