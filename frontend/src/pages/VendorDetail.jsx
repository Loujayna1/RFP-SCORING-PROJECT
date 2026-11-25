import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  CheckCircle,
  AlertTriangle,
  MinusCircle,
  HelpCircle,
  ChevronDown,
  Loader2,
  Flag,
  Edit3,
  X,
  Save,
  Eye,
  ShieldAlert,
  ShieldCheck,
  Shield,
  Info,
  MessageSquare,
  Zap,
} from "lucide-react";
import { rfpApi } from "../api/client";
import DocumentViewerModal from "../components/DocumentViewerModal";
import clsx from "clsx";

function parseDeepLink(deepLink) {
  if (!deepLink) return { docId: null, page: 1 };
  const m      = deepLink.match(/\/docs\/([a-f0-9-]{8,})\/view/i);
  const search = deepLink.includes("?") ? deepLink.split("?")[1] : "";
  const params = new URLSearchParams(search);
  return {
    docId: m ? m[1] : null,
    page:  parseInt(params.get("page") || "1", 10),
  };
}

const COMPLIANCE = {
  FullyCompliant:     { icon: CheckCircle,   cls: "compliance-full",    short: "Full",    color: "emerald" },
  PartiallyCompliant: { icon: MinusCircle,   cls: "compliance-partial", short: "Partial", color: "amber"   },
  NonCompliant:       { icon: AlertTriangle, cls: "compliance-none",    short: "None",    color: "crimson" },
  RoadmapCompliant:   { icon: MinusCircle,   cls: "compliance-partial", short: "Roadmap", color: "amber"   },
  NotAddressed:       { icon: HelpCircle,    cls: "compliance-na",      short: "N/A",     color: "steel"   },
  ScoringFailed:      { icon: AlertTriangle, cls: "compliance-na",      short: "Failed",  color: "steel"   },
};

// ─── Pure computation helpers ────────────────────────────────────────────────

function computeCategoryStats(category) {
  const all = [
    ...(category.requirements_uncategorized || []),
    ...(category.child_criteria || []).flatMap((c) => c.requirements || []),
  ];
  if (!all.length) return null;

  const fully    = all.filter((r) => r.compliance_level === "FullyCompliant").length;
  const partial  = all.filter((r) => r.compliance_level === "PartiallyCompliant").length;
  const none     = all.filter((r) => r.compliance_level === "NonCompliant").length;
  const na       = all.filter((r) => !r.compliance_level || r.compliance_level === "NotAddressed").length;
  const flagged  = all.filter((r) => r.confidence_flag && !r.human_score).length;
  const reviewed = all.filter((r) => r.human_score != null).length;
  const avgScore = all.reduce((s, r) => s + (r.final_score ?? r.raw_score ?? 0), 0) / all.length;

  const fullyPct   = Math.round((fully   / all.length) * 100);
  const partialPct = Math.round((partial / all.length) * 100);
  const nonePct    = Math.round((none    / all.length) * 100);
  const naPct      = Math.round((na      / all.length) * 100);
  const flaggedPct = Math.round((flagged / all.length) * 100);

  let insight, insightLevel;
  if (avgScore >= 4.0) {
    insightLevel = "strong";
    insight      = `Strong — ${fullyPct}% of requirements fully met. Clear strength area.`;
  } else if (avgScore >= 3.0) {
    insightLevel = "acceptable";
    insight      = `Acceptable — mostly compliant, with ${partial} partial response${partial !== 1 ? "s" : ""}. Minor clarification needed.`;
  } else if (naPct >= 40) {
    insightLevel = "gap";
    insight      = `${na} of ${all.length} requirements (${naPct}%) have no supporting evidence. Coverage is insufficient to score reliably.`;
  } else if (nonePct >= 30) {
    insightLevel = "gap";
    insight      = `Major gap — ${none} requirement${none !== 1 ? "s" : ""} (${nonePct}%) are explicitly non-compliant.`;
  } else if (avgScore >= 2.0) {
    insightLevel = "weak";
    insight      = `Mixed — ${fullyPct}% fully met, ${nonePct}% non-compliant. Clarification is required before a decision.`;
  } else {
    insightLevel = "gap";
    insight      = `Weak performance — average ${avgScore.toFixed(1)}/5. This criterion represents a significant risk.`;
  }

  return {
    total: all.length, fully, partial, none, na, flagged, reviewed,
    avgScore, fullyPct, partialPct, nonePct, naPct, flaggedPct,
    insight, insightLevel,
  };
}

function computeSummary(data, rank) {
  const total    = data?.vendor_score?.total_score ?? 0;
  const coverage = data?.coverage_rate ?? 0;
  const scores   = data?.requirement_scores ?? [];
  const totalReqs = scores.length || 1;

  const fully    = scores.filter((s) => s.compliance_level === "FullyCompliant").length;
  const partial  = scores.filter((s) => s.compliance_level === "PartiallyCompliant").length;
  const roadmap  = scores.filter((s) => s.compliance_level === "RoadmapCompliant").length;
  const none     = scores.filter((s) => s.compliance_level === "NonCompliant").length;
  const na       = scores.filter((s) => !s.compliance_level || s.compliance_level === "NotAddressed").length;
  const flagged  = scores.filter((s) => s.confidence_flag && !s.human_score).length;

  let scoreLevel, scoreText;
  if (total >= 4.0) {
    scoreLevel = "strong";
    scoreText  = "Strong submission — vendor demonstrates solid capability across most criteria.";
  } else if (total >= 3.0) {
    scoreLevel = "acceptable";
    scoreText  = "Acceptable submission — core requirements met, some gaps need clarification.";
  } else if (total >= 2.0) {
    scoreLevel = "weak";
    scoreText  = "Weak submission — significant gaps identified; high dependency on vendor clarification.";
  } else {
    scoreLevel = "insufficient";
    scoreText  = "Insufficient submission — vendor fails to address the majority of requirements.";
  }

  let coverageText;
  if (coverage >= 0.7) {
    coverageText = "Well-documented — strong evidence found across vendor documents.";
  } else if (coverage >= 0.4) {
    coverageText = "Partial — some areas lack supporting evidence in vendor documents.";
  } else {
    coverageText = "Poorly documented — insufficient evidence found. Scoring confidence is limited.";
  }

  let riskLevel;
  if (total >= 3.5 && coverage >= 0.6 && flagged / totalReqs < 0.2) {
    riskLevel = "low";
  } else if (total < 2.5 || coverage < 0.25 || flagged / totalReqs > 0.4) {
    riskLevel = "high";
  } else {
    riskLevel = "medium";
  }

  const fullyPct = Math.round((fully / totalReqs) * 100);
  const nonePct  = Math.round((none  / totalReqs) * 100);
  const naPct    = Math.round((na    / totalReqs) * 100);

  let mainFinding;
  if (riskLevel === "high") {
    if (none > na) {
      mainFinding = `${nonePct}% of requirements are explicitly non-compliant — this vendor is unable to meet core RFP mandates.`;
    } else {
      mainFinding = `${naPct}% of requirements have no supporting evidence, making accurate scoring impossible without additional documentation.`;
    }
  } else if (riskLevel === "medium") {
    mainFinding = `${fullyPct}% fully compliant, with key gaps in ${none + partial} requirement${(none + partial) !== 1 ? "s" : ""}. Targeted clarification is recommended before a final decision.`;
  } else {
    mainFinding = `${fullyPct}% of requirements fully met. Minor clarifications on ${none + partial} item${(none + partial) !== 1 ? "s" : ""} would strengthen confidence.`;
  }

  return {
    total, coverage, fully, partial, roadmap, none, na, flagged, totalReqs,
    scoreLevel, scoreText, coverageText, riskLevel, mainFinding,
  };
}

function computeActionableInsights(scores, categories, coverageRate, flaggedCount) {
  const missing   = [];
  const risks     = [];
  const questions = [];
  const totalReqs = scores.length || 1;

  // Missing capabilities — NonCompliant first, then NotAddressed
  scores
    .filter((r) => r.compliance_level === "NonCompliant")
    .slice(0, 5)
    .forEach((r) => {
      const desc = (r.description || r.req_code || "Requirement").slice(0, 110);
      missing.push({
        category: r.category || "General",
        description: desc + ((r.description?.length ?? 0) > 110 ? "…" : ""),
        weak: false,
      });
    });

  scores
    .filter((r) => r.compliance_level === "NotAddressed" || !r.compliance_level)
    .slice(0, 3)
    .forEach((r) => {
      const desc = (r.description || r.req_code || "Requirement").slice(0, 110);
      missing.push({
        category: r.category || "General",
        description: desc + ((r.description?.length ?? 0) > 110 ? "…" : ""),
        weak: true,
      });
    });

  // Coverage risk
  if (coverageRate < 0.3) {
    risks.push(
      "Very low evidence coverage — AI scoring is based on limited documentation. Manual review is strongly recommended.",
    );
  } else if (coverageRate < 0.5) {
    risks.push(
      "Below-average evidence coverage — some scores may not reflect the vendor's full capability.",
    );
  }

  // Confidence risk
  const flaggedRate = flaggedCount / totalReqs;
  if (flaggedRate > 0.3) {
    risks.push(
      `${flaggedCount} requirements (${Math.round(flaggedRate * 100)}%) have low-confidence AI scores. Human review is strongly advised before awarding.`,
    );
  }

  // Category-level gap risks
  categories.forEach((cat) => {
    const stats = computeCategoryStats(cat);
    if (stats && stats.nonePct >= 40) {
      risks.push(
        `"${cat.parent_criterion_label}": ${stats.none} non-compliant requirement${stats.none !== 1 ? "s" : ""} — this criterion is a critical risk area.`,
      );
    }
  });

  // Follow-up questions from gap categories
  categories
    .map((cat) => ({ cat, stats: computeCategoryStats(cat) }))
    .filter(({ stats }) => stats && stats.none > 0)
    .sort((a, b) => (b.stats?.none ?? 0) - (a.stats?.none ?? 0))
    .slice(0, 3)
    .forEach(({ cat, stats }) => {
      questions.push(
        `Clarify non-compliance in "${cat.parent_criterion_label}" (${stats.none} failed requirement${stats.none !== 1 ? "s" : ""})`,
      );
    });

  if (coverageRate < 0.4) {
    questions.push(
      "Request additional supporting documentation for low-coverage areas to enable accurate scoring",
    );
  }

  categories
    .map((cat) => ({ cat, stats: computeCategoryStats(cat) }))
    .filter(({ stats }) => stats && stats.flaggedPct > 40)
    .slice(0, 2)
    .forEach(({ cat }) => {
      questions.push(
        `Validate "${cat.parent_criterion_label}" scoring manually — AI evidence confidence is below threshold`,
      );
    });

  const avgOverall = scores.reduce((s, r) => s + (r.final_score ?? r.raw_score ?? 0), 0) / totalReqs;
  if (avgOverall < 2.5 && questions.length < 4) {
    questions.push("Consider requesting a structured clarification session before evaluation closure");
  }

  return { missing, risks, questions };
}

// ─── Executive Summary Card ───────────────────────────────────────────────────

function ExecutiveSummaryCard({ summary }) {
  const {
    total, coverage, fully, partial, roadmap = 0, none, na, flagged, totalReqs,
    scoreLevel, scoreText, coverageText, riskLevel, mainFinding,
  } = summary;

  const RISK_STYLE = {
    low:    { label: "Low Risk",    cls: "text-emerald-700 bg-emerald-50 border-emerald-200",  Icon: ShieldCheck },
    medium: { label: "Medium Risk", cls: "text-amber-700 bg-amber-50 border-amber-200",        Icon: Shield      },
    high:   { label: "High Risk",   cls: "text-crimson bg-crimson/5 border-crimson/20",        Icon: ShieldAlert },
  };
  const SCORE_COLOR = {
    strong:      "text-emerald-600",
    acceptable:  "text-blue-600",
    weak:        "text-amber-600",
    insufficient:"text-crimson",
  };
  const COVERAGE_COLOR =
    coverage >= 0.7 ? "text-emerald-600" : coverage >= 0.4 ? "text-amber-600" : "text-crimson";

  const { label: riskLabel, cls: riskCls, Icon: RiskIcon } = RISK_STYLE[riskLevel];

  return (
    <div className="card bg-white border border-steel/50 shadow-sm mb-6 overflow-hidden">
      <div className="px-6 py-4 border-b border-steel/30 bg-ivory/40 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Zap size={13} className="text-ink-muted" />
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-muted font-bold">
            Executive Summary
          </span>
        </div>
        <span className={clsx("inline-flex items-center gap-1.5 text-[10px] font-mono font-bold uppercase px-2.5 py-1 rounded-full border", riskCls)}>
          <RiskIcon size={11} />
          {riskLabel}
        </span>
      </div>

      <div className="p-6">
        {/* Main finding */}
        <p className="text-sm text-ink font-body leading-relaxed mb-6 border-l-4 border-steel/40 pl-4">
          {mainFinding}
        </p>

        {/* 3-column grid */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-5">
          {/* Score */}
          <div className="bg-ivory/60 rounded-xl p-4 border border-steel/20">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1.5">
              Score assessment
            </div>
            <div className={clsx("font-display text-3xl leading-none mb-2", SCORE_COLOR[scoreLevel])}>
              {total.toFixed(2)}
              <span className="text-base text-ink-muted font-mono font-normal">/5</span>
            </div>
            <p className="text-[11px] text-ink-muted font-body leading-relaxed">{scoreText}</p>
          </div>

          {/* Coverage */}
          <div className="bg-ivory/60 rounded-xl p-4 border border-steel/20">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1.5">
              Evidence found
            </div>
            <div className={clsx("font-display text-3xl leading-none mb-2", COVERAGE_COLOR)}>
              {(coverage * 100).toFixed(1)}
              <span className="text-base text-ink-muted font-mono font-normal">%</span>
            </div>
            <p className="text-[11px] text-ink-muted font-body leading-relaxed">{coverageText}</p>
          </div>

          {/* Compliance breakdown */}
          <div className="bg-ivory/60 rounded-xl p-4 border border-steel/20">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-2">
              Compliance breakdown
            </div>
            <div className="h-2.5 rounded-full overflow-hidden flex bg-steel/10 mb-3">
              {fully   > 0 && <div className="bg-emerald-500 transition-all" style={{ width: `${(fully   / totalReqs) * 100}%` }} />}
              {partial > 0 && <div className="bg-amber-400 transition-all"   style={{ width: `${(partial / totalReqs) * 100}%` }} />}
              {roadmap > 0 && <div className="bg-blue-400 transition-all"    style={{ width: `${(roadmap / totalReqs) * 100}%` }} />}
              {none    > 0 && <div className="bg-crimson transition-all"     style={{ width: `${(none    / totalReqs) * 100}%` }} />}
              {na      > 0 && <div className="bg-steel/40 transition-all"    style={{ width: `${(na      / totalReqs) * 100}%` }} />}
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
              {[
                { label: "Full",    count: fully,   dot: "bg-emerald-500" },
                { label: "Partial", count: partial, dot: "bg-amber-400"   },
                { label: "Roadmap", count: roadmap, dot: "bg-blue-400"    },
                { label: "Non-compliant", count: none, dot: "bg-crimson"  },
                { label: "Not addressed", count: na,   dot: "bg-steel/40" },
              ].map(({ label, count, dot }) => (
                <div key={label} className="flex items-center gap-1.5">
                  <span className={clsx("w-2 h-2 rounded-full flex-shrink-0", dot)} />
                  <span className="text-[10px] font-mono text-ink-muted">
                    {label}: <strong className="text-ink">{count}</strong>
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* AI confidence banner */}
        {flagged > 0 && (
          <div className="flex items-start gap-2.5 text-[11px] text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2.5 font-body">
            <Flag size={13} className="flex-shrink-0 mt-0.5 text-amber-500" />
            <span>
              <strong>{flagged} score{flagged !== 1 ? "s" : ""}</strong> flagged for low AI
              confidence — the system found weak or insufficient evidence in vendor documents
              to justify these scores.{" "}
              <span className="text-amber-700 font-medium">
                Review and override where needed before finalizing.
              </span>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Actionable Insights Section ─────────────────────────────────────────────

function ActionableInsightsSection({ insights }) {
  const { missing, risks, questions } = insights;
  const [open, setOpen] = useState(true);

  if (!missing.length && !risks.length && !questions.length) return null;

  const totalItems = missing.length + risks.length + questions.length;

  return (
    <div className="card bg-white border border-steel/50 shadow-sm mb-8 overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full px-6 py-4 border-b border-steel/30 bg-ivory/40 flex items-center justify-between hover:bg-ivory/60 transition-colors"
      >
        <div className="flex items-center gap-2">
          <MessageSquare size={13} className="text-ink-muted" />
          <span className="font-mono text-[10px] uppercase tracking-widest text-ink-muted font-bold">
            Actionable Insights
          </span>
          <span className="text-[9px] font-mono text-ink-muted bg-steel/20 px-1.5 py-0.5 rounded-full">
            {totalItems} item{totalItems !== 1 ? "s" : ""}
          </span>
        </div>
        <ChevronDown size={15} className={clsx("text-steel transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="p-6 grid grid-cols-1 md:grid-cols-3 gap-6">
          {/* Missing capabilities */}
          {missing.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 mb-3">
                <AlertTriangle size={12} className="text-crimson" />
                <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-crimson">
                  Missing Capabilities
                </span>
              </div>
              <div className="space-y-1.5">
                {missing.map((m, i) => (
                  <div
                    key={i}
                    className={clsx(
                      "flex gap-2 items-start text-[11px] font-body leading-snug rounded-lg px-3 py-2",
                      m.weak
                        ? "bg-amber-50/70 border border-amber-100 text-amber-900"
                        : "bg-crimson/5 border border-crimson/15 text-ink",
                    )}
                  >
                    <span
                      className={clsx(
                        "flex-shrink-0 w-1.5 h-1.5 rounded-full mt-1",
                        m.weak ? "bg-amber-400" : "bg-crimson",
                      )}
                    />
                    <div>
                      <span className="text-[9px] font-mono uppercase tracking-widest text-ink-muted block mb-0.5">
                        {m.category}
                      </span>
                      {m.description}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Risks */}
          {risks.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 mb-3">
                <ShieldAlert size={12} className="text-amber-600" />
                <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-amber-600">
                  Risks Identified
                </span>
              </div>
              <div className="space-y-1.5">
                {risks.map((r, i) => (
                  <div
                    key={i}
                    className="flex gap-2 items-start text-[11px] font-body leading-snug bg-amber-50/70 border border-amber-100 rounded-lg px-3 py-2 text-amber-900"
                  >
                    <span className="flex-shrink-0 w-1.5 h-1.5 rounded-full bg-amber-400 mt-1" />
                    {r}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Follow-up questions */}
          {questions.length > 0 && (
            <div>
              <div className="flex items-center gap-1.5 mb-3">
                <MessageSquare size={12} className="text-blue-600" />
                <span className="text-[10px] font-mono font-bold uppercase tracking-widest text-blue-600">
                  Suggested Follow-up
                </span>
              </div>
              <div className="space-y-1.5">
                {questions.map((q, i) => (
                  <div
                    key={i}
                    className="flex gap-2 items-start text-[11px] font-body leading-snug bg-blue-50/70 border border-blue-100 rounded-lg px-3 py-2 text-blue-900"
                  >
                    <span className="flex-shrink-0 w-1.5 h-1.5 rounded-full bg-blue-400 mt-1" />
                    {q}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Override modal ───────────────────────────────────────────────────────────

function OverrideModal({ req, rfpId, responseId, onClose, onSaved }) {
  const [newScore, setNewScore] = useState(req.final_score ?? req.raw_score ?? 0);
  const [reason,   setReason]   = useState("");
  const [saving,   setSaving]   = useState(false);
  const [error,    setError]    = useState("");

  async function handleSave() {
    if (!reason.trim()) { setError("Override reason is required."); return; }
    setSaving(true);
    setError("");
    try {
      await rfpApi.overrideScore(rfpId, responseId, req.score_id, {
        human_score:     Number(newScore),
        override_reason: reason.trim(),
      });
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to save override.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[400] flex items-center justify-center bg-ink/60 backdrop-blur-sm px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-lg bg-white rounded-2xl shadow-2xl border border-steel p-6 animate-slide-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-4">
          <div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Score Override</div>
            <div className="font-heading text-lg text-ink leading-snug">
              {req.req_code || req.req_id?.slice(0, 8)}
            </div>
            <div className="text-xs text-ink-muted mt-0.5 max-w-xs leading-relaxed">{req.description}</div>
          </div>
          <button onClick={onClose} className="p-1 hover:bg-ivory rounded-full"><X size={18} /></button>
        </div>

        <div className="grid grid-cols-3 gap-3 mb-5 p-3 bg-ivory rounded-lg border border-steel/50">
          <div className="text-center">
            <div className="text-[10px] font-mono text-ink-muted uppercase mb-1">AI score</div>
            <div className="text-xl font-display text-ink">{(req.ai_score ?? req.raw_score ?? 0).toFixed(1)}</div>
          </div>
          <div className="text-center">
            <div className="text-[10px] font-mono text-ink-muted uppercase mb-1">Current</div>
            <div className="text-xl font-display text-ink">{(req.final_score ?? req.raw_score ?? 0).toFixed(1)}</div>
          </div>
          <div className="text-center">
            <div className="text-[10px] font-mono text-crimson uppercase mb-1">New score</div>
            <input
              type="number" min={0} max={5} step={0.5}
              value={newScore}
              onChange={(e) => setNewScore(parseFloat(e.target.value) || 0)}
              className="w-full text-center text-xl font-display text-crimson border border-crimson/30 rounded-lg px-2 py-1 focus:outline-none focus:border-crimson bg-white"
            />
          </div>
        </div>

        <div className="mb-4">
          <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
            Override reason *
          </label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Vendor clarified this during Q&A session on 2025-06-01. Evidence on p.12 of addendum."
            rows={3}
            className="w-full border border-steel rounded-lg px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson resize-none"
          />
        </div>

        {error && (
          <div className="mb-4 text-crimson text-sm bg-crimson/5 border border-crimson/20 rounded-lg px-3 py-2">{error}</div>
        )}

        <div className="flex gap-3 justify-end">
          <button onClick={onClose} className="btn-ghost text-sm">Cancel</button>
          <button
            onClick={handleSave} disabled={saving}
            className="btn-primary text-sm flex items-center gap-2"
          >
            <Save size={14} />
            {saving ? "Saving…" : "Save override"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Requirement row ──────────────────────────────────────────────────────────

function RequirementRow({ req, rfpId, responseId, onOverrideSaved }) {
  const [open,         setOpen]         = useState(false);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [docViewer,    setDocViewer]    = useState(null);

  const c    = COMPLIANCE[req.compliance_level] || COMPLIANCE.NotAddressed;
  const Icon = c.icon;

  const finalScore   = req.final_score   ?? req.raw_score ?? 0;
  const aiScore      = req.ai_score      ?? req.raw_score ?? 0;
  const isOverridden = req.human_score != null;
  const isUncertain  = req.confidence_flag === true;
  // No evidence retrieved: the 0 is provisional (coverage gap), not a judged
  // non-compliance. Shown distinctly and never as a real score until reviewed.
  const noEvidence   = req.no_evidence === true && !isOverridden;

  const citations = req.evidence ? [req.evidence] : [];

  return (
    <>
      <tr
        className="border-b border-steel/50 hover:bg-ivory-warm cursor-pointer transition-colors"
        onClick={() => setOpen(!open)}
      >
        <td className="px-5 py-3 font-mono text-[10px] text-ink-muted">
          {req.req_code || req.req_id?.slice(0, 8)}
        </td>
        <td className="px-5 py-3 text-sm text-ink max-w-xs truncate">{req.description}</td>
        <td className="px-5 py-3">
          {noEvidence ? (
            <span
              title="No evidence was retrieved from the vendor's documents for this requirement. The 0 is provisional and flagged for review — it is not a judged non-compliance."
              className="inline-flex items-center gap-1 text-[10px] border px-2 py-0.5 rounded-full font-mono uppercase tracking-tight border-amber-300 bg-amber-50 text-amber-700"
            >
              <HelpCircle size={10} />No evidence
            </span>
          ) : (
            <span className={clsx("inline-flex items-center gap-1 text-[10px] border px-2 py-0.5 rounded-full font-mono uppercase tracking-tight", c.cls)}>
              <Icon size={10} />{c.short}
            </span>
          )}
        </td>
        <td className="px-5 py-3">
          <div className="flex items-center gap-2">
            <span className="font-mono text-sm font-medium text-ink">
              {noEvidence ? "—" : `${finalScore.toFixed(1)}/5`}
            </span>
            {isOverridden && (
              <span className="text-[10px] font-mono text-ink-muted line-through">
                AI:{aiScore.toFixed(1)}
              </span>
            )}
            {/* Subtle dot for uncertain scores — not a noisy text label */}
            {isUncertain && !isOverridden && (
              <span
                title="Low confidence — insufficient evidence found in vendor documents to justify this score."
                className="w-2 h-2 rounded-full bg-amber-400 flex-shrink-0 cursor-help"
              />
            )}
            {isOverridden && (
              <span className="inline-flex items-center gap-0.5 text-[10px] font-mono text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded">
                Reviewed
              </span>
            )}
          </div>
        </td>
        <td className="px-5 py-3">
          <button
            onClick={(e) => { e.stopPropagation(); setOverrideOpen(true); }}
            className="p-1.5 hover:bg-ivory rounded-lg text-ink-muted hover:text-crimson transition-colors"
            title="Override score"
          >
            <Edit3 size={13} />
          </button>
        </td>
        <td className="px-5 py-3 text-ink-muted">
          <ChevronDown size={14} className={clsx("transition-transform", open && "rotate-180")} />
        </td>
      </tr>

      {open && (
        <tr className="bg-ivory border-b border-steel/50">
          <td colSpan={6} className="px-5 py-4">
            <div className="text-sm text-ink mb-3 font-body leading-relaxed">{req.justification}</div>
            {req.override_reason && (
              <div className="mb-3 text-[11px] font-mono text-emerald-800 bg-emerald-50 border border-emerald-200 rounded px-3 py-2">
                Override reason: {req.override_reason}
              </div>
            )}
            {citations.length === 0 ? (
              <div className="text-[11px] text-ink-muted italic bg-white/50 rounded p-2 border border-dashed border-steel">
                No supporting evidence found in vendor documents.
              </div>
            ) : (
              citations.map((cit, i) => {
                const { docId, page: citPage } = parseDeepLink(cit.deep_link);
                return (
                  <div key={i} className="bg-emerald-50/50 border border-emerald-100 rounded-lg p-3 mb-1">
                    <div className="flex items-center justify-between mb-1.5">
                      <span className="text-[10px] font-mono font-bold text-emerald-800 uppercase tracking-widest">
                        {cit.source || "Reference"} · p.{cit.evidence_page || citPage || "?"}
                      </span>
                      {docId && (
                        <button
                          onClick={() => setDocViewer({ docId, page: citPage, filename: cit.source, evidenceText: cit.evidence_text })}
                          className="text-[10px] text-emerald-700 flex items-center gap-1 hover:underline font-bold"
                        >
                          VIEW SOURCE <Eye size={9} />
                        </button>
                      )}
                    </div>
                    <div className="text-[11px] text-ink italic border-l-2 border-emerald-400 pl-3 py-1 font-body leading-relaxed">
                      "{cit.evidence_text}"
                    </div>
                  </div>
                );
              })
            )}
          </td>
        </tr>
      )}

      {docViewer && (
        <DocumentViewerModal
          open={!!docViewer}
          onClose={() => setDocViewer(null)}
          docId={docViewer.docId}
          filename={docViewer.filename}
          page={docViewer.page}
          evidenceText={docViewer.evidenceText}
        />
      )}

      {overrideOpen && (
        <OverrideModal
          req={req}
          rfpId={rfpId}
          responseId={responseId}
          onClose={() => setOverrideOpen(false)}
          onSaved={onOverrideSaved}
        />
      )}
    </>
  );
}

// ─── Category section (collapsed by default, insight-first layout) ────────────

function CategorySection({ category, filter, rfpId, responseId, onOverrideSaved }) {
  const [expanded,     setExpanded]     = useState(false); // collapsed by default
  const [childExpanded, setChildExpanded] = useState({});

  const stats = computeCategoryStats(category);

  const fMap = {
    Full: "FullyCompliant", Partial: "PartiallyCompliant",
    None: "NonCompliant",   "N/A": "NotAddressed",
  };

  const allReqs = [
    ...(category.requirements_uncategorized || []),
    ...(category.child_criteria || []).flatMap((c) => c.requirements || []),
  ];

  if (allReqs.length === 0) return null;

  const INSIGHT_BADGE = {
    strong:     "bg-emerald-50 border-emerald-200 text-emerald-700",
    acceptable: "bg-blue-50 border-blue-200 text-blue-700",
    weak:       "bg-amber-50 border-amber-200 text-amber-700",
    gap:        "bg-crimson/5 border-crimson/20 text-crimson",
  };
  const INSIGHT_TEXT = {
    strong:     "text-emerald-700",
    acceptable: "text-blue-700",
    weak:       "text-amber-700",
    gap:        "text-crimson",
  };
  const INSIGHT_LABEL = {
    strong: "Strength", acceptable: "Acceptable", weak: "Weak", gap: "Gap",
  };

  const applyFilter = (reqs) =>
    filter === "all"
      ? reqs
      : reqs.filter((r) => r.compliance_level === (fMap[filter] || filter));

  const visibleUncategorized = applyFilter(category.requirements_uncategorized || []);

  return (
    <div className="border border-steel/40 rounded-xl overflow-hidden mb-3 bg-white shadow-sm">
      {/* Header — always visible */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center justify-between w-full px-5 py-4 hover:bg-ivory/50 transition-colors text-left"
      >
        <div className="flex-1 min-w-0">
          {/* Title row */}
          <div className="flex items-center gap-2.5 flex-wrap mb-1.5">
            <span className="font-heading text-sm text-ink font-bold">
              {category.parent_criterion_label}
            </span>
            <span className="text-[9px] font-mono uppercase tracking-widest text-ink-muted">
              {category.parent_criterion_weight}
            </span>
            {stats && (
              <span className={clsx("text-[9px] font-mono font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border", INSIGHT_BADGE[stats.insightLevel])}>
                {INSIGHT_LABEL[stats.insightLevel]}
              </span>
            )}
            {stats && stats.flaggedPct > 30 && (
              <span
                title="Low confidence — insufficient evidence found in vendor documents to justify these scores."
                className="text-[9px] font-mono text-amber-600 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full flex items-center gap-1"
              >
                <Flag size={8} /> {stats.flaggedPct}% low confidence
              </span>
            )}
          </div>

          {/* One-line insight */}
          {stats && (
            <p className={clsx("text-[11px] font-body leading-relaxed mb-2", INSIGHT_TEXT[stats.insightLevel])}>
              {stats.insight}
            </p>
          )}

          {/* Mini compliance bar + counts */}
          {stats && (
            <div className="flex items-center gap-3">
              <div className="h-1.5 rounded-full overflow-hidden flex w-28 bg-steel/15">
                {stats.fully   > 0 && <div className="bg-emerald-500" style={{ width: `${stats.fullyPct}%`   }} />}
                {stats.partial > 0 && <div className="bg-amber-400"   style={{ width: `${stats.partialPct}%` }} />}
                {stats.none    > 0 && <div className="bg-crimson"     style={{ width: `${stats.nonePct}%`    }} />}
                {stats.na      > 0 && <div className="bg-steel/40"    style={{ width: `${stats.naPct}%`      }} />}
              </div>
              <span className="text-[10px] font-mono text-ink-muted">
                {allReqs.length} req{allReqs.length !== 1 ? "s" : ""}
              </span>
              {stats.reviewed > 0 && (
                <span className="text-[10px] font-mono text-emerald-600">
                  {stats.reviewed} reviewed
                </span>
              )}
              <span className="text-[10px] font-mono text-ink-muted">
                avg {stats.avgScore.toFixed(1)}/5
              </span>
            </div>
          )}
        </div>

        <ChevronDown
          size={18}
          className={clsx("text-steel transition-transform flex-shrink-0 ml-4", expanded && "rotate-180")}
        />
      </button>

      {/* Expanded content: requirement tables */}
      {expanded && (
        <div className="border-t border-steel/30 bg-ivory/20">
          {/* Child criteria */}
          {(category.child_criteria || []).map((child) => {
            const visibleChildReqs = applyFilter(child.requirements || []);
            return (
              <div key={child.criterion_id} className="border-b border-steel/20 last:border-0">
                <button
                  onClick={() =>
                    setChildExpanded((prev) => ({
                      ...prev,
                      [child.criterion_id]: !prev[child.criterion_id],
                    }))
                  }
                  className="flex items-center justify-between w-full px-5 py-3 hover:bg-ivory/60 transition-colors text-left"
                >
                  <div className="flex-1">
                    <span className="font-heading text-xs text-ink/80">{child.criterion_label}</span>
                    <span className="ml-2 text-[9px] font-mono text-ink-muted uppercase">
                      {child.criterion_weight} · {child.requirements.length} req{child.requirements.length !== 1 ? "s" : ""}
                    </span>
                  </div>
                  <ChevronDown
                    size={14}
                    className={clsx("text-steel/60 transition-transform flex-shrink-0", childExpanded[child.criterion_id] && "rotate-180")}
                  />
                </button>

                {childExpanded[child.criterion_id] && visibleChildReqs.length > 0 && (
                  <div className="mx-4 mb-3 bg-white rounded border border-steel/20 overflow-hidden">
                    <table className="w-full text-sm font-body">
                      <RequirementTableHeader />
                      <tbody>
                        {visibleChildReqs.map((r) => (
                          <RequirementRow
                            key={r.req_id} req={r} rfpId={rfpId}
                            responseId={responseId} onOverrideSaved={onOverrideSaved}
                          />
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {childExpanded[child.criterion_id] && visibleChildReqs.length === 0 && (
                  <p className="px-5 pb-3 text-[11px] font-mono text-ink-muted italic">
                    No requirements match the current filter.
                  </p>
                )}
              </div>
            );
          })}

          {/* Uncategorized requirements */}
          {(category.requirements_uncategorized || []).length > 0 && (
            <div className="p-4">
              {category.child_criteria?.length > 0 && (
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-2 px-1">
                  Other Requirements
                </div>
              )}
              {visibleUncategorized.length > 0 ? (
                <div className="bg-white rounded border border-steel/20 overflow-hidden">
                  <table className="w-full text-sm font-body">
                    <RequirementTableHeader />
                    <tbody>
                      {visibleUncategorized.map((r) => (
                        <RequirementRow
                          key={r.req_id} req={r} rfpId={rfpId}
                          responseId={responseId} onOverrideSaved={onOverrideSaved}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-[11px] font-mono text-ink-muted italic px-1">
                  No requirements match the current filter.
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RequirementTableHeader() {
  return (
    <thead>
      <tr className="border-b border-steel/30 bg-ivory/30">
        <th className="px-5 py-2 text-left text-[10px] font-mono uppercase tracking-widest text-ink-muted w-24">Code</th>
        <th className="px-5 py-2 text-left text-[10px] font-mono uppercase tracking-widest text-ink-muted">Requirement</th>
        <th className="px-5 py-2 text-left text-[10px] font-mono uppercase tracking-widest text-ink-muted w-28">Compliance</th>
        <th className="px-5 py-2 text-left text-[10px] font-mono uppercase tracking-widest text-ink-muted w-28">Score</th>
        <th className="px-5 py-2 w-10" />
        <th className="px-5 py-2 w-10" />
      </tr>
    </thead>
  );
}

// ─── Score trace (declared vs verified, per requirement) ──────────────────────

const LEVEL = {
  FullyCompliant:     { short: "FC", label: "Fully compliant",     cls: "bg-emerald-50 border-emerald-200 text-emerald-700" },
  PartiallyCompliant: { short: "PC", label: "Partially compliant", cls: "bg-amber-50 border-amber-200 text-amber-700" },
  RoadmapCompliant:   { short: "RC", label: "Roadmap",             cls: "bg-blue-50 border-blue-200 text-blue-700" },
  NonCompliant:       { short: "NC", label: "Non-compliant",       cls: "bg-crimson/5 border-crimson/20 text-crimson" },
  NotAddressed:       { short: "NA", label: "Not addressed",       cls: "bg-steel/10 border-steel text-ink-muted" },
  ScoringFailed:      { short: "!",  label: "Scoring failed",      cls: "bg-amber-100 border-amber-300 text-amber-800" },
};

const VERIFY = {
  confirmed:      { label: "Confirmed",      cls: "text-emerald-700 bg-emerald-50 border-emerald-200",
                    help: "The proposal supports the level the vendor declared." },
  over_claim:     { label: "Over-claim",     cls: "text-crimson bg-crimson/5 border-crimson/30",
                    help: "The proposal supports a lower level than the vendor declared." },
  unsupported:    { label: "Unsupported",    cls: "text-crimson bg-crimson/10 border-crimson/40",
                    help: "The vendor declared a level but nothing in the proposal addresses this requirement." },
  under_claim:    { label: "Under-claim",    cls: "text-blue-700 bg-blue-50 border-blue-200",
                    help: "The proposal supports a higher level than the vendor declared." },
  not_declared:   { label: "Not declared",   cls: "text-ink-muted bg-steel/10 border-steel",
                    help: "The vendor's compliance matrix gives no level for this requirement." },
  scoring_failed: { label: "Review needed",  cls: "text-amber-800 bg-amber-50 border-amber-300",
                    help: "The automated assessment failed; this requirement is not counted until reviewed." },
};

const fmt = (x, d = 2) => (x === null || x === undefined || Number.isNaN(Number(x)) ? "—" : Number(x).toFixed(d));
const pctW = (w) => (w === null || w === undefined ? "—" : `${(Number(w) * 100).toFixed(Number(w) * 100 < 10 ? 1 : 0)}%`);

function LevelPill({ level, points }) {
  if (!level) return <span className="text-[10px] font-mono text-ink-muted">—</span>;
  const l = LEVEL[level] || { short: level, label: level, cls: "bg-steel/10 border-steel text-ink-muted" };
  return (
    <span title={l.label} className={clsx("inline-flex items-center gap-1 text-[10px] border px-2 py-0.5 rounded-full font-mono", l.cls)}>
      {l.short}{points !== undefined && points !== null && <span className="opacity-70">· {fmt(points, 0)}</span>}
    </span>
  );
}

function VerifyPill({ status }) {
  const v = VERIFY[status] || VERIFY.not_declared;
  return (
    <span title={v.help} className={clsx("inline-flex text-[10px] border px-2 py-0.5 rounded-full font-mono uppercase tracking-tight whitespace-nowrap", v.cls)}>
      {v.label}
    </span>
  );
}

function ScoreCompositionCard({ trace }) {
  const rows = [];
  const walk = (nodes, depth) => nodes.forEach((n) => { rows.push({ ...n, depth }); walk(n.children || [], depth + 1); });
  walk(trace.criteria || [], 0);
  const lp = trace.level_points || {};
  return (
    <div className="card bg-white border-none shadow-sm mb-6 overflow-hidden">
      <div className="px-6 py-4 border-b border-steel/50 bg-ivory/30 flex items-center justify-between">
        <span className="font-heading text-sm text-ink uppercase tracking-widest font-bold">How the score is computed</span>
        <span className="text-[10px] font-mono text-ink-muted">
          Points: FC {fmt(lp.FullyCompliant, 0)} · PC {fmt(lp.PartiallyCompliant, 0)} · RC {fmt(lp.RoadmapCompliant, 0)} · NC {fmt(lp.NonCompliant, 0)} · Not addressed {fmt(lp.NotAddressed, 0)}
        </span>
      </div>
      <table className="w-full text-sm font-body">
        <thead>
          <tr className="border-b border-steel/30 text-[10px] font-mono uppercase tracking-widest text-ink-muted">
            <th className="px-6 py-2 text-left">Criterion</th>
            <th className="px-3 py-2 text-right" title="Weight in the evaluation grid">Grid weight</th>
            <th className="px-3 py-2 text-right" title="Share of the total once parent weights are applied">Share of total</th>
            <th className="px-3 py-2 text-right">Reqs</th>
            <th className="px-3 py-2 text-right" title="Score if every declaration were true">Declared</th>
            <th className="px-3 py-2 text-right">Verified</th>
            <th className="px-6 py-2 text-right" title="Verified score × share of total">Contribution</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((n) => (
            <tr key={n.criterion_id} className={clsx("border-b border-steel/20", !n.is_leaf && "bg-ivory/60")}>
              <td className={clsx("px-6 py-2", n.is_leaf ? "text-ink" : "font-bold text-ink")} style={{ paddingLeft: `${24 + n.depth * 18}px` }}>
                {n.label}
                {n.is_leaf && n.requirement_count > 0 && (
                  <span className="ml-2 text-[10px] font-mono text-ink-muted">
                    = {fmt(n.points_sum, 0)} pts / {n.requirements_counted} reqs
                  </span>
                )}
                {n.is_leaf && n.scored_from === "direct" && (
                  <span className="ml-2 text-[10px] font-mono text-amber-700">scored directly (no requirements)</span>
                )}
                {n.is_leaf && n.scored_from === "not_scored" && (
                  <span className="ml-2 text-[10px] font-mono text-crimson">not scored — excluded from total</span>
                )}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-ink-muted">
                {n.local_weight == null ? "—" : Number(n.local_weight) > 1 ? fmt(n.local_weight, 0) : fmt(n.local_weight, 2)}
              </td>
              <td className="px-3 py-2 text-right font-mono text-xs text-ink-muted">{pctW(n.effective_weight)}</td>
              <td className="px-3 py-2 text-right font-mono text-xs text-ink-muted">{n.requirement_count || "—"}</td>
              <td className="px-3 py-2 text-right font-mono text-xs text-ink-muted">{fmt(n.declared_score)}</td>
              <td className={clsx("px-3 py-2 text-right font-mono text-sm", n.is_leaf ? "text-ink" : "font-bold text-ink")}>{fmt(n.score)}</td>
              <td className="px-6 py-2 text-right font-mono text-sm text-ink">{fmt(n.contribution, 3)}</td>
            </tr>
          ))}
          <tr className="bg-ink text-white">
            <td className="px-6 py-2.5 font-bold uppercase tracking-widest text-xs" colSpan={4}>Total</td>
            <td className="px-3 py-2.5 text-right font-mono text-sm opacity-80" title="Total if every declaration were true">{fmt(trace.declared_total)}</td>
            <td className="px-3 py-2.5 text-right font-mono text-sm" />
            <td className="px-6 py-2.5 text-right font-mono text-base font-bold">{fmt(trace.total_score, 3)}</td>
          </tr>
        </tbody>
      </table>
      {(trace.unmapped_requirements || []).length > 0 && (
        <div className="px-6 py-3 text-[11px] text-crimson bg-crimson/5 border-t border-crimson/20">
          {trace.unmapped_requirements.length} requirement(s) match no criterion of the grid and are not counted:
          {" "}{trace.unmapped_requirements.map((r) => r.req_code).join(", ")}
        </div>
      )}
    </div>
  );
}

function VerificationSummaryCard({ trace, onFilter, active }) {
  const v = trace.summary?.verification || {};
  const total = trace.summary?.requirements || 0;
  const order = ["confirmed", "over_claim", "unsupported", "under_claim", "not_declared", "scoring_failed"];
  const refs = trace.summary || {};
  return (
    <div className="card bg-white border-none shadow-sm mb-6 p-5">
      <div className="flex items-center justify-between mb-3">
        <span className="font-heading text-sm text-ink uppercase tracking-widest font-bold">Declared vs verified</span>
        <span className="text-[11px] font-mono text-ink-muted">
          Declared total {fmt(trace.declared_total)} → verified {fmt(trace.total_score)}
        </span>
      </div>
      <div className="flex flex-wrap gap-2 mb-3">
        {order.filter((k) => v[k]).map((k) => (
          <button
            key={k}
            onClick={() => onFilter(active === k ? "all" : k)}
            className={clsx("flex items-center gap-2 border rounded-lg px-3 py-2 transition-all", VERIFY[k].cls,
              active === k && "ring-2 ring-ink")}
            title={VERIFY[k].help}
          >
            <span className="font-display text-2xl leading-none">{v[k]}</span>
            <span className="text-[10px] font-mono uppercase tracking-wider">{VERIFY[k].label}</span>
          </button>
        ))}
      </div>
      <p className="text-[11px] text-ink-muted font-body leading-relaxed">
        Each requirement compares the level the vendor declared in its compliance matrix with the level its
        Technical Proposal supports ({total} requirements). Evidence was found in the proposal for {refs.evidence_found} of them.
        {refs.references_checked > 0 && (
          <> The vendor's matrix reference points to the page of the evidence for {refs.references_matching} of {refs.references_checked} requirements.</>
        )}
        {" "}Click a box to list those requirements.
      </p>
    </div>
  );
}

function TraceRequirementRow({ req, rfpId, responseId, onOverrideSaved }) {
  const [open, setOpen] = useState(false);
  const [overrideOpen, setOverrideOpen] = useState(false);
  const [docViewer, setDocViewer] = useState(null);
  const isOverridden = req.human_score != null;
  const link = parseDeepLink(req.evidence_link);

  return (
    <>
      <tr className="border-b border-steel/40 hover:bg-ivory-warm cursor-pointer" onClick={() => setOpen(!open)}>
        <td className="px-4 py-2.5 font-mono text-[10px] text-ink-muted whitespace-nowrap">{req.req_code}</td>
        <td className="px-4 py-2.5 text-[13px] text-ink">{req.description}</td>
        <td className="px-3 py-2.5"><LevelPill level={req.declared_level} points={req.declared_points} /></td>
        <td className="px-3 py-2.5"><LevelPill level={req.compliance_level} /></td>
        <td className="px-3 py-2.5 font-mono text-sm text-ink whitespace-nowrap">
          {req.compliance_level === "ScoringFailed" && !isOverridden ? "—" : fmt(req.final_score ?? req.raw_score, 1)}
          {isOverridden && <span className="ml-1 text-[10px] text-ink-muted line-through">{fmt(req.ai_score, 1)}</span>}
          {req.confidence_flag && !isOverridden && (
            <span title="Flagged for review" className="inline-block ml-1.5 w-2 h-2 rounded-full bg-amber-400 align-middle" />
          )}
        </td>
        <td className="px-3 py-2.5"><VerifyPill status={req.verification} /></td>
        <td className="px-3 py-2.5 text-[10px] font-mono text-ink-muted whitespace-nowrap">
          {req.evidence_page ? `p.${req.evidence_page}` : "—"}
        </td>
        <td className="px-2 py-2.5">
          <button
            onClick={(e) => { e.stopPropagation(); setOverrideOpen(true); }}
            className="p-1 hover:bg-ivory rounded text-ink-muted hover:text-crimson" title="Override score"
          >
            <Edit3 size={12} />
          </button>
        </td>
      </tr>
      {open && (
        <tr className="bg-ivory/60 border-b border-steel/40">
          <td colSpan={8} className="px-5 py-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Vendor's claim (compliance matrix)</div>
                <div className="text-[12px] text-ink bg-white border border-steel/40 rounded p-2.5 leading-relaxed">
                  <div className="mb-1"><LevelPill level={req.declared_level} /> <span className="text-[11px] text-ink-muted ml-1">{req.declared_raw}</span></div>
                  {req.vendor_response || <span className="italic text-ink-muted">No detailed response.</span>}
                  {req.vendor_reference && (
                    <div className="mt-1.5 text-[11px] font-mono text-ink-muted">
                      Reference: {req.vendor_reference}
                      {req.reference_matches_evidence === true && <span className="ml-2 text-emerald-700">✓ matches evidence page</span>}
                      {req.reference_matches_evidence === false && <span className="ml-2 text-amber-700">✗ evidence found on p.{req.evidence_page}</span>}
                    </div>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Evidence in the proposal</div>
                {req.evidence_quote ? (
                  <div className="bg-emerald-50/60 border border-emerald-100 rounded p-2.5">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-[10px] font-mono font-bold text-emerald-800 uppercase tracking-widest">
                        {req.evidence_source || "Document"} · p.{req.evidence_page ?? "?"}
                      </span>
                      {link.docId && (
                        <button
                          onClick={() => setDocViewer({ docId: link.docId, page: req.evidence_page || link.page,
                            filename: req.evidence_source, evidenceText: req.evidence_quote })}
                          className="text-[10px] text-emerald-700 flex items-center gap-1 hover:underline font-bold"
                        >
                          VIEW SOURCE <Eye size={9} />
                        </button>
                      )}
                    </div>
                    <div className="text-[12px] text-ink italic border-l-2 border-emerald-400 pl-2.5 leading-relaxed">"{req.evidence_quote}"</div>
                  </div>
                ) : (
                  <div className="text-[12px] text-ink-muted italic bg-white/60 rounded p-2.5 border border-dashed border-steel">
                    No passage of the proposal addresses this requirement.
                  </div>
                )}
              </div>
            </div>
            <div className="mt-3 text-[12px] text-ink leading-relaxed">
              <span className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mr-2">Verdict</span>
              {req.justification}
            </div>
            {req.override_reason && (
              <div className="mt-2 text-[11px] font-mono text-emerald-800 bg-emerald-50 border border-emerald-200 rounded px-3 py-2">
                Override reason: {req.override_reason}
              </div>
            )}
            {req.reasoning && (
              <details className="mt-2">
                <summary className="text-[10px] font-mono uppercase tracking-widest text-ink-muted cursor-pointer">Reasoning</summary>
                <pre className="mt-1 whitespace-pre-wrap text-[11px] text-ink-muted font-body leading-relaxed">{req.reasoning}</pre>
              </details>
            )}
          </td>
        </tr>
      )}
      {docViewer && (
        <DocumentViewerModal open onClose={() => setDocViewer(null)} docId={docViewer.docId}
          filename={docViewer.filename} page={docViewer.page} evidenceText={docViewer.evidenceText} />
      )}
      {overrideOpen && (
        <OverrideModal req={req} rfpId={rfpId} responseId={responseId}
          onClose={() => setOverrideOpen(false)} onSaved={onOverrideSaved} />
      )}
    </>
  );
}

function TraceCriterionSection({ node, parentLabel, filter, rfpId, responseId, onOverrideSaved }) {
  const [open, setOpen] = useState(false);
  const reqs = node.requirements || [];
  const visible = filter === "all" ? reqs
    : filter === "flagged" ? reqs.filter((r) => r.confidence_flag && r.human_score == null)
    : reqs.filter((r) => r.verification === filter);
  const counts = node.level_counts || {};
  const n = reqs.length || 1;
  const bars = ["FullyCompliant", "PartiallyCompliant", "RoadmapCompliant", "NonCompliant", "NotAddressed"];
  const barCls = { FullyCompliant: "bg-emerald-500", PartiallyCompliant: "bg-amber-400", RoadmapCompliant: "bg-blue-400",
                   NonCompliant: "bg-crimson", NotAddressed: "bg-steel/50" };
  const overClaims = reqs.filter((r) => r.verification === "over_claim" || r.verification === "unsupported").length;
  if (filter !== "all" && visible.length === 0) return null;

  return (
    <div className="border border-steel/40 rounded-xl overflow-hidden mb-3 bg-white shadow-sm">
      <button onClick={() => setOpen(!open)} className="flex items-center justify-between w-full px-5 py-4 hover:bg-ivory/50 text-left">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2.5 flex-wrap mb-1.5">
            {parentLabel && <span className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">{parentLabel} ›</span>}
            <span className="font-heading text-sm text-ink font-bold">{node.label}</span>
            <span className="text-[10px] font-mono text-ink-muted" title="Share of the total score">{pctW(node.effective_weight)} of total</span>
            {overClaims > 0 && (
              <span className="text-[9px] font-mono font-bold uppercase tracking-widest px-2 py-0.5 rounded-full border text-crimson bg-crimson/5 border-crimson/20">
                {overClaims} false declaration{overClaims !== 1 ? "s" : ""}
              </span>
            )}
          </div>
          <div className="flex items-center gap-3">
            <div className="h-1.5 rounded-full overflow-hidden flex w-32 bg-steel/15">
              {bars.map((b) => counts[b] ? <div key={b} className={barCls[b]} style={{ width: `${(counts[b] / n) * 100}%` }} /> : null)}
            </div>
            <span className="text-[10px] font-mono text-ink-muted">
              {bars.filter((b) => counts[b]).map((b) => `${LEVEL[b].short} ${counts[b]}`).join(" · ")}
              {counts.ScoringFailed ? ` · failed ${counts.ScoringFailed}` : ""}
            </span>
          </div>
        </div>
        <div className="text-right ml-4">
          <div className="font-display text-3xl text-ink leading-none">{fmt(node.score)}</div>
          <div className="text-[10px] font-mono text-ink-muted mt-1">
            {node.requirement_count > 0 ? `${fmt(node.points_sum, 0)} pts / ${node.requirements_counted} reqs` : node.scored_from}
            {node.declared_score != null && <> · declared {fmt(node.declared_score)}</>}
          </div>
        </div>
        <ChevronDown size={18} className={clsx("text-steel transition-transform ml-4", open && "rotate-180")} />
      </button>
      {open && (
        <div className="border-t border-steel/30 overflow-x-auto">
          {visible.length === 0 ? (
            <p className="px-5 py-3 text-[11px] font-mono text-ink-muted italic">
              {reqs.length === 0 ? "This criterion has no requirements; it was scored directly." : "No requirements match the current filter."}
            </p>
          ) : (
            <table className="w-full text-sm font-body">
              <thead>
                <tr className="border-b border-steel/30 bg-ivory/30 text-[10px] font-mono uppercase tracking-widest text-ink-muted">
                  <th className="px-4 py-2 text-left w-20">Code</th>
                  <th className="px-4 py-2 text-left">Requirement</th>
                  <th className="px-3 py-2 text-left w-20" title="Level declared in the vendor's compliance matrix">Declared</th>
                  <th className="px-3 py-2 text-left w-20" title="Level supported by the proposal">Verified</th>
                  <th className="px-3 py-2 text-left w-16">Points</th>
                  <th className="px-3 py-2 text-left w-28">Check</th>
                  <th className="px-3 py-2 text-left w-14">Page</th>
                  <th className="px-2 py-2 w-8" />
                </tr>
              </thead>
              <tbody>
                {visible.map((r) => (
                  <TraceRequirementRow key={r.req_id} req={r} rfpId={rfpId} responseId={responseId} onOverrideSaved={onOverrideSaved} />
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

function ScoreTrace({ trace, rfpId, responseId, onOverrideSaved }) {
  const [filter, setFilter] = useState("all");
  const sections = [];
  const walk = (nodes, parent) => nodes.forEach((n) => {
    if (n.is_leaf) sections.push({ node: n, parentLabel: parent });
    else walk(n.children || [], n.label);
  });
  walk(trace.criteria || [], null);
  const chips = [["all", "All"], ["over_claim", "Over-claims"], ["unsupported", "Unsupported"],
                 ["confirmed", "Confirmed"], ["under_claim", "Under-claims"], ["flagged", "Flagged"]];

  return (
    <>
      <ScoreCompositionCard trace={trace} />
      <VerificationSummaryCard trace={trace} onFilter={setFilter} active={filter} />
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-3">
          <h2 className="font-heading text-sm text-ink uppercase tracking-widest font-bold">Requirement scores by criterion</h2>
          <ConfidenceLegend />
        </div>
        <div className="flex gap-1.5 flex-wrap">
          {chips.map(([k, label]) => (
            <button key={k} onClick={() => setFilter(k)}
              className={clsx("text-[10px] px-3 py-1 rounded-full border font-mono uppercase tracking-wider",
                filter === k ? "bg-ink text-white border-ink" : "border-steel text-ink-muted hover:border-ink-muted bg-white")}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <p className="text-[11px] text-ink-muted font-body mb-4">
        A criterion's score is the average of the points of its requirements. Open a criterion to see, for every
        requirement, the declared level, the verified level, the points counted and the evidence with its page.
      </p>
      <div className="mb-8">
        {sections.map(({ node, parentLabel }) => (
          <TraceCriterionSection key={node.criterion_id} node={node} parentLabel={parentLabel} filter={filter}
            rfpId={rfpId} responseId={responseId} onOverrideSaved={onOverrideSaved} />
        ))}
      </div>
    </>
  );
}

// ─── Group flat scores into criteria buckets ──────────────────────────────────

function groupByCriteria(requirementScores, compareData) {
  if (!compareData?.criteria?.length) {
    return [
      {
        parent_criterion_id:     "uncategorized",
        parent_criterion_label:  "All Requirements",
        parent_criterion_weight: "—",
        child_criteria:          [],
        requirements_uncategorized: requirementScores,
      },
    ];
  }

  const criteriaMap = {};
  for (const c of compareData.criteria) {
    criteriaMap[c.label.toLowerCase()] = c;
  }

  const buckets       = {};
  const uncategorized = [];

  for (const score of requirementScores) {
    const cat     = (score.category || "").toLowerCase();
    const matched =
      criteriaMap[cat] ||
      Object.values(criteriaMap).find(
        (c) => cat.includes(c.label.toLowerCase()) || c.label.toLowerCase().includes(cat),
      );
    if (matched) {
      (buckets[matched.label] = buckets[matched.label] || []).push(score);
    } else {
      uncategorized.push(score);
    }
  }

  const groups = compareData.criteria.map((c) => ({
    parent_criterion_id:     c.label,
    parent_criterion_label:  c.label,
    parent_criterion_weight: `${Math.round(c.weight > 1 ? c.weight : (c.weight || 0) * 100)}%`,
    child_criteria:          [],
    requirements_uncategorized: buckets[c.label] || [],
  }));

  if (uncategorized.length) {
    groups.push({
      parent_criterion_id:     "other",
      parent_criterion_label:  "Other",
      parent_criterion_weight: "—",
      child_criteria:          [],
      requirements_uncategorized: uncategorized,
    });
  }

  return groups.filter(
    (g) => g.requirements_uncategorized.length || g.child_criteria.length,
  );
}

// ─── Confidence legend tooltip ────────────────────────────────────────────────

function ConfidenceLegend() {
  const [show, setShow] = useState(false);
  return (
    <div className="relative inline-block">
      <button
        onMouseEnter={() => setShow(true)}
        onMouseLeave={() => setShow(false)}
        onClick={() => setShow(!show)}
        className="flex items-center gap-1 text-[10px] font-mono text-ink-muted hover:text-ink transition-colors"
      >
        <Info size={11} />
        What does low confidence mean?
      </button>
      {show && (
        <div className="absolute left-0 top-5 z-50 w-72 bg-white border border-steel shadow-lg rounded-xl px-4 py-3 text-[11px] font-body text-ink leading-relaxed">
          <strong className="font-mono text-[10px] uppercase tracking-widest text-ink block mb-1">
            Low Confidence
          </strong>
          The system found weak or insufficient evidence in vendor documents to confidently justify
          this score. The AI still produced a score based on available text, but it may not accurately
          reflect the vendor's true capability. These scores are marked with an amber dot (
          <span className="inline-block w-2 h-2 rounded-full bg-amber-400 align-middle mx-0.5" />
          ) and should be reviewed by a procurement officer before finalizing.
        </div>
      )}
    </div>
  );
}

// ─── Page ────────────────────────────────────────────────────────────────────

export default function VendorDetail() {
  const { rfpId, responseId } = useParams();
  const navigate              = useNavigate();
  const [data,    setData]    = useState(null);
  const [team,    setTeam]    = useState([]);
  const [compare, setCompare] = useState(null);
  const [filter,  setFilter]  = useState("all");
  const [loading, setLoading] = useState(true);

  function loadData() {
    setLoading(true);
    Promise.all([
      rfpApi.score(rfpId, responseId),
      rfpApi.team(rfpId, responseId),
      rfpApi.compare(rfpId),
    ])
      .then(([sRes, tRes, cRes]) => {
        setData(sRes.data);
        setTeam(tRes.data.members || []);
        setCompare(cRes.data);
      })
      .finally(() => setLoading(false));
  }

  useEffect(() => { loadData(); }, [rfpId, responseId]);

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] text-ink-muted gap-3">
        <Loader2 className="animate-spin text-crimson" size={32} />
        <div className="text-sm font-mono tracking-widest uppercase">
          Analyzing evaluation data...
        </div>
      </div>
    );
  }

  const requirementScores = data?.requirement_scores || [];
  const categories        = groupByCriteria(requirementScores, compare);

  const vendorDetail = compare?.vendor_details?.find((vd) => vd.response_id === responseId);
  const vendorName   = vendorDetail?.name || "Vendor";
  const rank         = (compare?.ranking || []).indexOf(vendorName) + 1 || "?";

  const flaggedCount = requirementScores.filter((s) => s.confidence_flag && !s.human_score).length;

  // Computed summaries — derived from fetched data only, no new API calls
  const summary    = computeSummary(data, rank);
  const actionable = computeActionableInsights(requirementScores, categories, summary.coverage, flaggedCount);
  // Declared vs verified: false declarations are the first risk to report
  const ver = data?.trace?.summary?.verification || {};
  const falseDecl = (ver.over_claim || 0) + (ver.unsupported || 0);
  if (falseDecl > 0) {
    actionable.risks.unshift(
      `${falseDecl} declaration${falseDecl !== 1 ? "s" : ""} not supported by the proposal ` +
      `(${ver.over_claim || 0} over-claim${(ver.over_claim || 0) !== 1 ? "s" : ""}, ${ver.unsupported || 0} with no evidence). ` +
      `Declared total ${data.trace.declared_total?.toFixed(2)} vs verified ${data.trace.total_score?.toFixed(2)}.`,
    );
    actionable.questions.unshift("Ask the vendor to justify the over-claimed requirements with evidence");
  }

  return (
    <div className="p-8 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-6xl text-ink tracking-tight leading-none uppercase">
            {vendorName}
          </div>
          <div className="text-[11px] text-ink-muted font-mono mt-2 tracking-widest uppercase">
            Evaluation Result · {requirementScores.length} Requirements ·{" "}
            {data?.vendor_score?.total_score?.toFixed(2)} Final Score
          </div>
        </div>
        <button
          onClick={() => navigate(`/evaluation/${rfpId}`)}
          className="btn-ghost text-xs"
        >
          Back to summary
        </button>
      </div>

      {/* Stats bar */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        {[
          { label: "Weighted score", value: `${data?.vendor_score?.total_score?.toFixed(2)}/5`, color: "text-ink" },
          { label: data?.trace ? "Evidence found" : "Coverage rate",
            value: `${((data?.coverage_rate || 0) * 100).toFixed(1)}%`, color: "text-emerald-600" },
          { label: "Requirements",   value: requirementScores.length, color: "text-ink" },
          { label: "Global rank",    value: `#${rank}`, color: "text-crimson" },
        ].map((s) => (
          <div key={s.label} className="card p-5 border-none shadow-sm bg-white">
            <div className="text-[10px] text-ink-muted font-bold uppercase tracking-widest mb-2">{s.label}</div>
            <div className={clsx("font-display text-4xl leading-none", s.color)}>{s.value}</div>
          </div>
        ))}
      </div>

      {/* ── NEW: Executive Summary ── */}
      <ExecutiveSummaryCard summary={summary} />

      {/* ── NEW: Actionable Insights ── */}
      <ActionableInsightsSection insights={actionable} />

      {data?.trace ? (
        <ScoreTrace trace={data.trace} rfpId={rfpId} responseId={responseId} onOverrideSaved={loadData} />
      ) : (
        <>
      {/* ── Scoring Breakdown section header ── */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <h2 className="font-heading text-sm text-ink uppercase tracking-widest font-bold">
            Score Breakdown by Criterion
          </h2>
          <ConfidenceLegend />
        </div>
        {/* Filter chips */}
        <div className="flex gap-1.5 flex-wrap">
          {["all", "FullyCompliant", "PartiallyCompliant", "NonCompliant", "NotAddressed"].map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={clsx(
                "text-[10px] px-3 py-1 rounded-full border transition-all font-mono uppercase tracking-wider",
                filter === f
                  ? "bg-ink text-white border-ink"
                  : "border-steel text-ink-muted hover:border-ink-muted bg-white",
              )}
            >
              {f === "all" ? "All" : COMPLIANCE[f]?.short}
            </button>
          ))}
        </div>
      </div>

      <p className="text-[11px] text-ink-muted font-body mb-4">
        Click any criterion to expand its requirements. Categories are collapsed by default.
      </p>

      {/* Criteria categories */}
      <div className="mb-8">
        {categories.map((cat) => (
          <CategorySection
            key={cat.parent_criterion_id}
            category={cat}
            filter={filter}
            rfpId={rfpId}
            responseId={responseId}
            onOverrideSaved={loadData}
          />
        ))}
      </div>

        </>
      )}

      {/* Team */}
      {team.length > 0 && (
        <div className="card overflow-hidden border-none shadow-sm bg-white">
          <div className="px-6 py-5 border-b border-steel/50 bg-ivory/30">
            <span className="font-heading text-sm text-ink uppercase tracking-widest font-bold">
              Proposed Team (AI Extracted)
            </span>
          </div>
          <div className="grid grid-cols-2 gap-4 p-5">
            {team.map((m) => (
              <div
                key={m.member_id || m.full_name}
                className="border border-steel/50 rounded-xl p-5 hover:border-crimson transition-all group bg-white"
              >
                <div className="flex items-center gap-4 mb-4">
                  <div className="w-12 h-12 rounded-full bg-crimson flex items-center justify-center text-white font-mono text-lg shadow-sm group-hover:scale-110 transition-transform">
                    {m.full_name.split(" ").map((n) => n[0]).join("")}
                  </div>
                  <div>
                    <div className="font-bold text-ink text-base">{m.full_name}</div>
                    <div className="text-[11px] text-ink-muted font-mono uppercase tracking-wider">
                      {m.role} · {m.years_experience} yrs exp
                    </div>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5 mb-4">
                  {(m.skills || []).map((s) => (
                    <span key={s} className="text-[10px] bg-ivory border border-steel rounded px-2 py-0.5 font-mono text-ink-muted">
                      {s}
                    </span>
                  ))}
                </div>
                <div className="flex items-center justify-between pt-4 border-t border-steel/50">
                  <div className="flex gap-1.5">
                    {(m.certifications || []).map((c) => (
                      <span key={c} className="text-[10px] bg-crimson/5 border border-crimson/20 text-crimson rounded px-2 py-0.5 font-mono font-bold">
                        {c}
                      </span>
                    ))}
                  </div>
                  <div className="text-[10px] text-ink-muted flex items-center gap-1 font-mono uppercase font-bold">
                    p.{m.source_page}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

