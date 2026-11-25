import { useEffect, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  Lock,
  Unlock,
  Save,
  AlertTriangle,
  CheckCircle,
  ChevronLeft,
  Loader2,
  Info,
} from "lucide-react";
import { rfpApi } from "../api/client";
import { useAuth } from "../context/useAuth";
import clsx from "clsx";

// ─── Helpers ─────────────────────────────────────────────────────────────────
function weightSum(criteria) {
  return criteria.reduce((s, c) => s + (parseFloat(c.weight) || 0), 0);
}

function pct(w) {
  // Weights are stored two ways across rubrics: as fractions (0–1, e.g. 0.5 = 50%)
  // or already as percentages (e.g. 50 = 50%). A value > 1 is already a percentage,
  // so don't multiply it by 100 again (that produced "5000%"). Display only — the
  // stored weight and the scoring normalisation are unchanged.
  const n = parseFloat(w) || 0;
  return `${Math.round(n > 1 ? n : n * 100)}%`;
}

// ─── Criterion type badge ─────────────────────────────────────────────────────
function TypeBadge({ type }) {
  const map = {
    scored:      "bg-blue-50 text-blue-700 border-blue-200",
    pass_fail:   "bg-purple-50 text-purple-700 border-purple-200",
    mandatory:   "bg-crimson/5 text-crimson border-crimson/20",
    informational:"bg-gray-100 text-gray-600 border-gray-200",
  };
  return (
    <span className={clsx("text-[10px] font-mono border px-2 py-0.5 rounded-full", map[type] || map.scored)}>
      {type || "scored"}
    </span>
  );
}

// ─── Single editable criterion row ───────────────────────────────────────────
function CriterionRow({ criterion, locked, onChange }) {
  return (
    <tr className="border-b border-steel/50 hover:bg-ivory transition-colors">
      <td className="px-5 py-4">
        <div className="font-medium text-sm text-ink">{criterion.label}</div>
        {criterion.section && criterion.section !== criterion.label && (
          <div className="text-[10px] text-ink-muted font-mono mt-0.5">{criterion.section}</div>
        )}
        {criterion.extraction_source && (
          <div className="text-[10px] text-ink-muted/60 font-mono mt-0.5 italic">
            Source: {criterion.extraction_source}
          </div>
        )}
      </td>

      {/* Weight */}
      <td className="px-5 py-4">
        <div className="flex items-center gap-2">
          {locked ? (
            <span className="font-mono text-sm text-ink">{pct(criterion.weight)}</span>
          ) : (
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={0}
                max={1}
                step={0.01}
                value={criterion.weight}
                onChange={(e) => onChange(criterion.criterion_id, "weight", parseFloat(e.target.value) || 0)}
                className="w-20 border border-steel rounded px-2 py-1 text-sm font-mono bg-white focus:outline-none focus:border-crimson"
              />
              <span className="text-xs text-ink-muted font-mono">
                ({pct(criterion.weight)})
              </span>
            </div>
          )}
        </div>
      </td>

      {/* Max score */}
      <td className="px-5 py-4">
        {locked ? (
          <span className="font-mono text-sm text-ink">{criterion.max_score}</span>
        ) : (
          <input
            type="number"
            min={1}
            max={10}
            step={1}
            value={criterion.max_score}
            onChange={(e) => onChange(criterion.criterion_id, "max_score", parseInt(e.target.value) || 5)}
            className="w-16 border border-steel rounded px-2 py-1 text-sm font-mono bg-white focus:outline-none focus:border-crimson"
          />
        )}
      </td>

      {/* Type */}
      <td className="px-5 py-4">
        {locked ? (
          <TypeBadge type={criterion.criterion_type} />
        ) : (
          <select
            value={criterion.criterion_type || "scored"}
            onChange={(e) => onChange(criterion.criterion_id, "criterion_type", e.target.value)}
            className="border border-steel rounded px-2 py-1 text-xs font-mono bg-white focus:outline-none focus:border-crimson"
          >
            <option value="scored">Scored</option>
            <option value="pass_fail">Pass / Fail</option>
            <option value="mandatory">Mandatory</option>
            <option value="informational">Informational</option>
          </select>
        )}
      </td>

      {/* Evaluator role */}
      <td className="px-5 py-4">
        {locked ? (
          <span className="text-[10px] font-mono text-ink-muted">
            {criterion.owner_role
              ? criterion.owner_role.replace("_evaluator", "").replace(/_/g, " ")
              : "All roles"}
          </span>
        ) : (
          <select
            value={criterion.owner_role || ""}
            onChange={(e) =>
              onChange(criterion.criterion_id, "owner_role", e.target.value || null)
            }
            className="border border-steel rounded px-2 py-1 text-xs font-mono bg-white focus:outline-none focus:border-crimson"
          >
            <option value="">All evaluators</option>
            <option value="technical_evaluator">Technical</option>
            <option value="financial_evaluator">Financial</option>
            <option value="operations_evaluator">Operations</option>
            <option value="legal_compliance">Legal</option>
          </select>
        )}
      </td>

      {/* Confidence */}
      <td className="px-5 py-4">
        {criterion.confidence_score != null ? (
          <div className="flex items-center gap-1.5">
            <div className="w-16 h-1.5 bg-steel-light rounded-full overflow-hidden">
              <div
                className={clsx(
                  "h-full rounded-full",
                  criterion.confidence_score >= 0.7
                    ? "bg-emerald-500"
                    : criterion.confidence_score >= 0.4
                    ? "bg-amber-400"
                    : "bg-crimson",
                )}
                style={{ width: `${(criterion.confidence_score * 100).toFixed(0)}%` }}
              />
            </div>
            <span className="text-[10px] font-mono text-ink-muted">
              {(criterion.confidence_score * 100).toFixed(0)}%
            </span>
          </div>
        ) : (
          <span className="text-[10px] text-ink-muted/40 font-mono">—</span>
        )}
      </td>
    </tr>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────
export default function RubricApproval() {
  const { rfpId }   = useParams();
  const navigate    = useNavigate();
  const { user }    = useAuth();
  // Only admins may edit weights and lock the rubric — this matches the route
  // guard (roles=["admin"]) and the backend POST /rubric/lock (require_role
  // "admin"). The old `isProcurementOfficer` flag was never provided by the
  // auth context, so it was always undefined and the lock button never showed.
  const canManageRubric = user?.role === "admin";

  const [rfp,      setRfp]      = useState(null);
  const [rubric,   setRubric]   = useState(null);   // {criteria, locked, locked_at, approved_by, flagged_count}
  const [criteria, setCriteria] = useState([]);      // local editable copy
  const [dirty,    setDirty]    = useState(false);
  const [saving,   setSaving]   = useState(false);
  const [locking,  setLocking]  = useState(false);
  const [error,    setError]    = useState("");
  const [success,  setSuccess]  = useState("");
  const [loading,  setLoading]  = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    Promise.all([
      rfpApi.get(rfpId),
      rfpApi.getRubric(rfpId),
    ])
      .then(([rfpRes, rubricRes]) => {
        setRfp(rfpRes.data);
        setRubric(rubricRes.data);
        setCriteria(
          (rubricRes.data.criteria || []).map((c) => ({ ...c })),
        );
        setDirty(false);
      })
      .catch(() => setError("Failed to load rubric. Please refresh."))
      .finally(() => setLoading(false));
  }, [rfpId]);

  useEffect(() => { load(); }, [load]);

  // Update a single field on a criterion row
  function handleChange(criterionId, field, value) {
    setCriteria((prev) =>
      prev.map((c) =>
        c.criterion_id === criterionId ? { ...c, [field]: value } : c,
      ),
    );
    setDirty(true);
    setError("");
  }

  // Weights do NOT need to sum to any fixed value: the scoring pipeline
  // normalizes them (hierarchical rubrics sum to >1, percentage rubrics to 100).
  // We only require that some positive weight exists and none are negative.
  function validateWeights() {
    if (criteria.some((c) => (parseFloat(c.weight) || 0) < 0)) {
      setError("Weights cannot be negative.");
      return false;
    }
    if (weightSum(criteria) <= 0) {
      setError("At least one criterion must have a weight greater than 0 before locking.");
      return false;
    }
    return true;
  }

  async function handleSave() {
    if (!validateWeights()) return;
    setSaving(true);
    setError("");
    try {
      await rfpApi.updateRubric(rfpId, { criteria });
      setSuccess("Rubric saved successfully.");
      setDirty(false);
      setTimeout(() => setSuccess(""), 3000);
      load();
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to save rubric.");
    } finally {
      setSaving(false);
    }
  }

  async function handleLock() {
    if (dirty) {
      setError("Save your changes before locking the rubric.");
      return;
    }
    if (!validateWeights()) return;
    if (
      !window.confirm(
        "Lock the rubric? This cannot be undone. Criteria weights and types will be frozen — " +
        "any vendor scores computed after this point will use these exact settings.",
      )
    )
      return;

    setLocking(true);
    setError("");
    try {
      await rfpApi.lockRubric(rfpId, {});
      setSuccess("Rubric locked and approved. Vendor scoring is now trusted.");
      load();
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to lock rubric.");
    } finally {
      setLocking(false);
    }
  }

  // Informational total only — weights are normalized during scoring.
  const sum       = weightSum(criteria);
  const weightsOk = sum > 0 && criteria.every((c) => (parseFloat(c.weight) || 0) >= 0);
  const locked    = rubric?.locked ?? false;

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px] text-ink-muted gap-3 flex-col">
        <Loader2 className="animate-spin text-crimson" size={28} />
        <div className="text-xs font-mono uppercase tracking-widest">Loading rubric…</div>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-5xl mx-auto">

      {/* Header */}
      <div className="flex items-start justify-between mb-8">
        <div>
          <button
            onClick={() => navigate(`/evaluation/${rfpId}`)}
            className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink font-mono uppercase tracking-wider mb-3 transition-colors"
          >
            <ChevronLeft size={14} /> Back to evaluation
          </button>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            RUBRIC APPROVAL
          </div>
          <div className="text-ink-muted font-body text-sm mt-1.5">{rfp?.title}</div>
        </div>

        {/* Lock state indicator */}
        <div className="mt-1">
          {locked ? (
            <div className="flex flex-col items-end gap-1.5">
              <div className="inline-flex items-center gap-2 bg-emerald-50 border border-emerald-200 text-emerald-700 text-xs font-mono px-3 py-2 rounded-lg">
                <Lock size={13} /> Rubric Locked
              </div>
              {rubric?.locked_at && (
                <div className="text-[10px] text-ink-muted font-mono">
                  {new Date(rubric.locked_at).toLocaleString()} by {rubric.approved_by || "system"}
                </div>
              )}
            </div>
          ) : (
            <div className="inline-flex items-center gap-2 bg-amber-50 border border-amber-200 text-amber-700 text-xs font-mono px-3 py-2 rounded-lg">
              <Unlock size={13} /> Pending Approval
            </div>
          )}
        </div>
      </div>

      {/* Info banner when locked */}
      {locked && (
        <div className="mb-6 flex items-start gap-3 px-4 py-3 bg-blue-50 border border-blue-200 rounded-xl text-sm text-blue-800 font-body">
          <Info size={16} className="flex-shrink-0 mt-0.5 text-blue-500" />
          <span>
            This rubric is locked. Criteria weights and types cannot be changed. Vendor scores
            computed after the lock date use these exact settings.
          </span>
        </div>
      )}

      {/* Flagged criteria notice */}
      {!locked && rubric?.flagged_count > 0 && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800 font-body">
          <AlertTriangle size={16} className="flex-shrink-0 text-amber-500" />
          <span>
            <strong>{rubric.flagged_count} criteria</strong> were extracted with low AI
            confidence (shown below). Review their weights carefully before locking.
          </span>
        </div>
      )}

      {/* Weight total — informational. Weights are normalized at scoring time,
          so they need not sum to any fixed value (only proportions matter). */}
      {!locked && (
        <div className="mb-5 flex items-start gap-2 text-xs text-ink-muted font-mono">
          <Info size={13} className="flex-shrink-0 mt-0.5 text-ink-muted/60" />
          <span>
            Total weight across criteria: <strong>{sum.toFixed(2)}</strong>. Weights
            are normalized automatically during scoring, so they do not need to sum
            to 1.00 — only their relative proportions matter.
          </span>
        </div>
      )}

      {/* Error / success */}
      {error && (
        <div className="mb-5 flex items-center gap-2 text-crimson text-sm font-body bg-crimson/5 border border-crimson/20 rounded-lg px-4 py-3">
          <AlertTriangle size={15} className="flex-shrink-0" /> {error}
        </div>
      )}
      {success && (
        <div className="mb-5 flex items-center gap-2 text-emerald-700 text-sm font-body bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">
          <CheckCircle size={15} className="flex-shrink-0" /> {success}
        </div>
      )}

      {/* Criteria table */}
      <div className="card overflow-hidden mb-6">
        <table className="w-full text-sm font-body">
          <thead>
            <tr className="border-b border-steel bg-ivory">
              {["Criterion", "Weight (0–1)", "Max score", "Type", "Evaluator role", "AI confidence"].map((h) => (
                <th
                  key={h}
                  className="text-left text-xs font-medium text-ink-muted px-5 py-3 uppercase tracking-wider"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {criteria.length === 0 ? (
              <tr>
                <td colSpan={5} className="px-5 py-10 text-center text-ink-muted text-sm italic">
                  No criteria extracted yet. Wait for the RFP pipeline to complete.
                </td>
              </tr>
            ) : (
              criteria.map((c) => (
                <CriterionRow
                  key={c.criterion_id}
                  criterion={c}
                  locked={locked}
                  onChange={handleChange}
                />
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Action bar */}
      {!locked && canManageRubric && (
        <div className="flex items-center justify-between gap-4">
          <div className="text-xs text-ink-muted font-body max-w-sm leading-relaxed">
            Once locked, vendors can be scored against these criteria. Locking is permanent —
            only re-run the RFP pipeline to reset.
          </div>
          <div className="flex gap-3">
            <button
              onClick={handleSave}
              disabled={saving || !dirty}
              className="btn-ghost flex items-center gap-2 text-sm"
            >
              {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
              {saving ? "Saving…" : "Save changes"}
            </button>
            <button
              onClick={handleLock}
              disabled={locking || dirty || !weightsOk || criteria.length === 0}
              className="btn-primary flex items-center gap-2 text-sm"
              title={dirty ? "Save changes first" : !weightsOk ? "Add at least one criterion with a positive weight" : ""}
            >
              {locking ? <Loader2 size={14} className="animate-spin" /> : <Lock size={14} />}
              {locking ? "Locking…" : "Approve & lock rubric"}
            </button>
          </div>
        </div>
      )}

      {/* Read-only notice for non-admins */}
      {!locked && !canManageRubric && (
        <div className="flex items-center gap-2 text-xs text-ink-muted font-body bg-ivory border border-steel rounded-lg px-4 py-3">
          <Info size={14} className="flex-shrink-0" />
          Only an admin can edit weights and lock the rubric.
        </div>
      )}
    </div>
  );
}
