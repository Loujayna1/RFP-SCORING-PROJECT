import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  CheckCircle, AlertTriangle, Flag, Send,
  Loader2, ChevronLeft, Lock,
} from "lucide-react";
import { rfpApi } from "../api/client";
import { useAuth } from "../context/useAuth";
import clsx from "clsx";

const ROLE_LABELS = {
  technical_evaluator:  "Technical",
  financial_evaluator:  "Financial",
  operations_evaluator: "Operations",
  legal_compliance:     "Legal / Compliance",
  business_rep:         "Business Representative",
};

function ScoreBar({ score, max = 5 }) {
  const pct = Math.min((score / max) * 100, 100);
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-steel-light rounded-full overflow-hidden">
        <div
          className={clsx("h-full rounded-full transition-all",
            score >= 4 ? "bg-emerald-500" : score >= 3 ? "bg-amber-400" : "bg-crimson")}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-xs font-mono text-ink w-8 text-right">{score?.toFixed(1)}</span>
    </div>
  );
}

function CriterionCard({ criterion, vendors }) {
  return (
    <div className="card overflow-hidden mb-4">
      <div className="px-5 py-3 border-b border-steel bg-ivory/40 flex items-center justify-between">
        <div>
          <span className="font-heading text-sm text-ink">{criterion.criterion_label}</span>
          <span className={clsx(
            "ml-2 text-[10px] font-mono border px-1.5 py-0.5 rounded-full",
            criterion.criterion_type === "mandatory"
              ? "bg-crimson/5 text-crimson border-crimson/20"
              : "bg-blue-50 text-blue-700 border-blue-200",
          )}>
            {criterion.criterion_type || "scored"}
          </span>
        </div>
        <span className="text-[10px] font-mono text-ink-muted">Max: {criterion.max_score}</span>
      </div>
      <div className="divide-y divide-steel/30">
        {vendors.map(v => {
          const s = v.scores.find(sc => sc.criterion_id === criterion.criterion_id);
          if (!s) return null;
          return (
            <div key={v.response_id} className="px-5 py-3 flex items-center gap-4">
              <div className="w-32 flex-shrink-0 text-sm font-medium text-ink truncate">
                {v.vendor_name}
              </div>
              <div className="flex-1">
                <ScoreBar score={s.final_score ?? s.ai_score ?? 0} max={criterion.max_score} />
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                {s.confidence_flag && !s.human_score && (
                  <span title="Low AI confidence — review recommended"
                    className="inline-flex items-center gap-0.5 text-[10px] font-mono text-amber-600 bg-amber-50 border border-amber-200 px-1.5 py-0.5 rounded">
                    <Flag size={9} /> Low confidence
                  </span>
                )}
                {s.human_score != null && (
                  <span className="text-[10px] font-mono text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded">
                    Reviewed
                  </span>
                )}
                {s.is_submitted && (
                  <Lock size={11} className="text-ink-muted" title="Submitted — frozen" />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function EvaluatorDashboard() {
  const { rfpId }    = useParams();
  const navigate     = useNavigate();
  const { user }     = useAuth();
  const [data,       setData]       = useState(null);
  const [loading,    setLoading]    = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [confirm,    setConfirm]    = useState(false);
  const [error,      setError]      = useState("");

  async function load() {
    try {
      const r = await rfpApi.getEvaluatorDashboard(rfpId);
      setData(r.data);
    } catch (e) {
      setError(e?.response?.data?.detail || "Could not load your evaluation dashboard");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [rfpId]);

  async function handleSubmit() {
    setSubmitting(true); setError("");
    try {
      await rfpApi.submitEvaluation(rfpId);
      await load();
      setConfirm(false);
    } catch (e) {
      setError(e?.response?.data?.detail || "Submit failed");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) return (
    <div className="flex items-center justify-center min-h-[400px] flex-col gap-3 text-ink-muted">
      <Loader2 size={28} className="animate-spin text-crimson" />
      <div className="text-xs font-mono uppercase tracking-widest">Loading your evaluation…</div>
    </div>
  );

  if (error && !data) return (
    <div className="p-8 text-center">
      <AlertTriangle size={32} className="text-crimson mx-auto mb-3" />
      <div className="text-sm text-ink-muted">{error}</div>
    </div>
  );

  const { rfp_title, rfp_status, my_role, is_submitted, submitted_at, vendors } = data;

  // Build unique criteria list from first vendor (all vendors have same criteria)
  const criteria = vendors[0]?.scores || [];

  const flaggedCount = vendors.flatMap(v => v.scores).filter(s => s.confidence_flag && !s.human_score).length;

  return (
    <div className="p-8 max-w-5xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-8">
        <div>
          <button onClick={() => navigate(`/evaluation/${rfpId}`)}
            className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink font-mono uppercase tracking-wider mb-3 transition-colors">
            <ChevronLeft size={14} /> Back
          </button>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">MY REVIEW</div>
          <div className="text-ink-muted font-body text-sm mt-1.5">{rfp_title}</div>
          <div className="flex items-center gap-2 mt-2">
            <span className="text-[10px] font-mono text-white bg-crimson px-2 py-0.5 rounded-full">
              {ROLE_LABELS[my_role] || my_role}
            </span>
            <span className="text-[10px] font-mono text-ink-muted">
              {criteria.length} criteria · {vendors.length} vendors
            </span>
          </div>
        </div>

        {is_submitted ? (
          <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 text-emerald-700 text-xs font-mono px-4 py-2.5 rounded-xl">
            <CheckCircle size={14} />
            Submitted {submitted_at ? new Date(submitted_at).toLocaleDateString() : ""}
          </div>
        ) : (
          <button
            onClick={() => setConfirm(true)}
            disabled={submitting}
            className="btn-primary flex items-center gap-2">
            <Send size={15} /> Submit evaluation
          </button>
        )}
      </div>

      {/* Banners */}
      {is_submitted && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-emerald-50 border border-emerald-200 rounded-xl text-sm text-emerald-800">
          <Lock size={16} className="flex-shrink-0 text-emerald-500" />
          Your scores are frozen and cannot be changed. The admin will review all evaluations in the consensus phase.
        </div>
      )}
      {flaggedCount > 0 && !is_submitted && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
          <Flag size={16} className="flex-shrink-0 text-amber-500" />
          <span><strong>{flaggedCount} score{flaggedCount !== 1 ? "s" : ""}</strong> flagged for low AI confidence. Consider reviewing before submitting.</span>
        </div>
      )}
      {error && (
        <div className="mb-6 text-crimson text-sm bg-crimson/5 border border-crimson/20 rounded-xl px-4 py-3">
          {error}
        </div>
      )}

      {/* Criteria cards */}
      {criteria.map(c => (
        <CriterionCard key={c.criterion_id} criterion={c} vendors={vendors} />
      ))}

      {criteria.length === 0 && (
        <div className="text-center py-20 text-ink-muted text-sm italic">
          No criteria assigned to your role yet. Wait for the admin to configure the rubric.
        </div>
      )}

      {/* Confirm submit modal */}
      {confirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 px-4"
             onClick={() => !submitting && setConfirm(false)}>
          <div className="w-full max-w-sm bg-white rounded-2xl shadow-2xl border border-steel p-6"
               onClick={e => e.stopPropagation()}>
            <div className="text-center mb-5">
              <Send size={32} className="text-crimson mx-auto mb-3" />
              <div className="font-heading text-xl text-ink">Submit your evaluation?</div>
              <div className="text-xs text-ink-muted mt-2 leading-relaxed">
                This is <strong>irreversible</strong>. Your scores will be locked and
                shared with the admin during the consensus review.
              </div>
            </div>
            <div className="flex gap-3">
              <button onClick={() => setConfirm(false)} disabled={submitting} className="btn-ghost flex-1">
                Cancel
              </button>
              <button onClick={handleSubmit} disabled={submitting}
                className="btn-primary flex-1 flex items-center justify-center gap-2">
                {submitting ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                {submitting ? "Submitting…" : "Confirm submit"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
