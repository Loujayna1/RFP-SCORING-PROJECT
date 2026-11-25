import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Save, X, AlertCircle } from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

// Weights are stored two ways across rubrics: as fractions (0–1, e.g. 0.5 = 50%)
// or already as percentages (e.g. 50 = 50%). A value > 1 is already a percentage,
// so don't multiply by 100 again (that produced "4000%"). Display only.
function weightPct(w) {
  const n = Number(w) || 0;
  return Math.round(n > 1 ? n : n * 100);
}

export default function ScoreEditor() {
  const { rfpId } = useParams();
  const navigate  = useNavigate();
  const [rfp,        setRfp]        = useState(null);
  const [compare,    setCompare]    = useState(null);
  const [config,     setConfig]     = useState(null);
  // FIX 1 — key is "responseId:criterionLabel", value is { score, reason }
  const [editScores, setEditScores] = useState({});
  // FIX 3 — single shared override_reason input, shown when any edit is pending
  const [reason,     setReason]     = useState("");
  const [saving,     setSaving]     = useState(false);
  const [error,      setError]      = useState("");
  const [successMsg, setSuccessMsg] = useState("");

  useEffect(() => {
    Promise.all([
      rfpApi.get(rfpId).then((r) => setRfp(r.data)).catch(() => {}),
      rfpApi.compare(rfpId).then((r) => setCompare(r.data)).catch(() => {}),
      rfpApi.getConfig(rfpId).then((r) => setConfig(r.data)).catch(() => {}),
    ]);
  }, [rfpId]);

  const scoringMargin = config?.scoring_margin ?? 10;
  const criteria      = compare?.criteria      || [];

  // Build a lookup: vendor name → response_id from vendor_details
  // FIX 1 — vendorDetails contains both name and response_id from the compare endpoint
  const vendorDetails = compare?.vendor_details || [];
  const responseIdByName = Object.fromEntries(
    vendorDetails.map((vd) => [vd.name, vd.response_id]),
  );

  // FIX 2 — margin is a percentage of the current score, not raw points.
  // e.g. score=3.0, margin=10 → allowed range [2.7, 3.3], not [−7, 13].
  function computeRange(currentScore) {
    const delta    = (currentScore * scoringMargin) / 100;
    const minScore = Math.max(0, currentScore - delta);
    const maxScore = Math.min(5, currentScore + delta);
    return { minScore, maxScore };
  }

  // FIX 1 — key uses responseId (UUID), not vendor name string
  function editKey(responseId, criterionLabel) {
    return `${responseId}::${criterionLabel}`;
  }

  function handleScoreChange(vendorName, criterionLabel, newScore) {
    const responseId = responseIdByName[vendorName];
    if (!responseId) return; // vendor not yet evaluated

    const currentScore = criteria.find((c) => c.label === criterionLabel)
      ?.scores?.[vendorName] ?? 0;
    const { minScore, maxScore } = computeRange(currentScore);
    const clamped = Math.min(maxScore, Math.max(minScore, newScore));

    const key = editKey(responseId, criterionLabel);
    setEditScores((prev) => ({ ...prev, [key]: { responseId, criterion: criterionLabel, score: clamped } }));
  }

  function getScoreColor(score) {
    if (score >= 4) return "bg-emerald-500";
    if (score >= 3) return "bg-amber-400";
    return "bg-crimson";
  }

  // FIX 3 — send override_reason with every score update (backend requires it)
  async function handleSave() {
    if (!reason.trim()) {
      setError("An override reason is required before saving changes.");
      return;
    }
    setSaving(true);
    setError("");
    setSuccessMsg("");
    try {
      for (const { responseId, criterion, score } of Object.values(editScores)) {
        await rfpApi.updateScore(rfpId, responseId, {
          criterion,
          score,
          override_reason: reason.trim(),
        });
      }
      setEditScores({});
      setReason("");
      setSuccessMsg("Scores updated successfully");
      setTimeout(() => setSuccessMsg(""), 3000);
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to save scores");
    } finally {
      setSaving(false);
    }
  }

  const hasPendingEdits = Object.keys(editScores).length > 0;

  if (!rfp || !compare || !config) {
    return (
      <div className="p-8 text-center text-ink-muted">
        Loading scoring configuration...
      </div>
    );
  }

  return (
    <div className="p-8">
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            SCORE EDITOR
          </div>
          <div className="text-ink-muted font-body text-sm mt-1">
            {rfp?.title}
          </div>
          <div className="text-xs text-ink-muted font-mono mt-2">
            Allowed margin: ±{scoringMargin}% of AI score
          </div>
        </div>
        <button
          onClick={() => navigate(`/evaluation/${rfpId}`)}
          className="btn-ghost"
        >
          <X size={20} />
        </button>
      </div>

      {error && (
        <div className="mb-6 flex items-center gap-2 text-crimson text-sm font-body bg-crimson/5 border border-crimson/20 rounded-lg px-4 py-3">
          <AlertCircle size={16} className="flex-shrink-0" />
          {error}
        </div>
      )}

      {successMsg && (
        <div className="mb-6 flex items-center gap-2 text-emerald-700 text-sm font-body bg-emerald-50 border border-emerald-200 rounded-lg px-4 py-3">
          {successMsg}
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm font-body">
          <thead>
            <tr className="border-b border-steel bg-ivory">
              <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                Vendor
              </th>
              {criteria.map((c) => (
                <th
                  key={c.label}
                  className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider"
                >
                  {c.label}
                  <span className="block font-mono text-[10px] normal-case tracking-normal text-ink-muted/60">
                    {weightPct(c.weight)}%
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {vendorDetails.map((vd, i) => (
              <tr
                key={vd.response_id}
                className={clsx(
                  "border-b border-steel/50",
                  i % 2 === 0 ? "bg-white" : "bg-ivory",
                )}
              >
                <td className="px-6 py-4 font-medium text-ink">
                  {vd.name}
                  {/* FIX 1 — show the UUID in muted text so devs can verify */}
                  <div className="text-[10px] font-mono text-ink-muted/60 mt-0.5 truncate max-w-[140px]">
                    {vd.response_id}
                  </div>
                </td>
                {criteria.map((c) => {
                  const currentScore = c.scores?.[vd.name] ?? 0;
                  const { minScore, maxScore } = computeRange(currentScore);
                  const key          = editKey(vd.response_id, c.label);
                  const displayScore = editScores[key]?.score ?? currentScore;
                  const isDirty      = key in editScores;

                  return (
                    <td key={c.label} className="px-6 py-4">
                      <div className="flex items-center gap-2">
                        <input
                          type="number"
                          min={minScore}
                          max={maxScore}
                          step="0.1"
                          value={displayScore.toFixed(1)}
                          onChange={(e) =>
                            handleScoreChange(
                              vd.name,
                              c.label,
                              parseFloat(e.target.value) || 0,
                            )
                          }
                          className={clsx(
                            "w-16 border rounded px-2 py-1 text-sm bg-white focus:outline-none transition-colors",
                            isDirty
                              ? "border-amber-400 focus:border-amber-500"
                              : "border-steel focus:border-crimson",
                          )}
                        />
                        <div className="flex-1 h-1.5 bg-steel-light rounded-full overflow-hidden">
                          <div
                            className={clsx(
                              "h-full rounded-full transition-all",
                              getScoreColor(displayScore),
                            )}
                            style={{ width: `${(displayScore / 5) * 100}%` }}
                          />
                        </div>
                        <span className="text-[10px] text-ink-muted font-mono whitespace-nowrap">
                          {minScore.toFixed(1)}–{maxScore.toFixed(1)}
                        </span>
                      </div>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* FIX 3 — override reason required by backend before any save */}
      {hasPendingEdits && (
        <div className="mt-6 card p-5 border-amber-200 bg-amber-50/30">
          <div className="text-xs font-mono uppercase tracking-widest text-amber-700 mb-2">
            Override reason required *
          </div>
          <p className="text-xs text-ink-muted mb-3 leading-relaxed">
            You have {Object.keys(editScores).length} pending score change
            {Object.keys(editScores).length !== 1 ? "s" : ""}. Provide a reason
            that will be recorded in the audit log before saving.
          </p>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. Vendor provided additional evidence during clarification round on 2025-06-01"
            rows={2}
            className="w-full border border-steel rounded-lg px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson resize-none"
          />
        </div>
      )}

      <div className="mt-6 flex justify-end gap-3">
        <button
          onClick={() => navigate(`/evaluation/${rfpId}`)}
          className="btn-ghost"
        >
          Cancel
        </button>
        <button
          onClick={handleSave}
          disabled={saving || !hasPendingEdits}
          className="btn-primary flex items-center gap-2"
        >
          <Save size={16} />
          {saving ? "Saving…" : `Save ${Object.keys(editScores).length || ""} change${Object.keys(editScores).length !== 1 ? "s" : ""}`}
        </button>
      </div>
    </div>
  );
}
