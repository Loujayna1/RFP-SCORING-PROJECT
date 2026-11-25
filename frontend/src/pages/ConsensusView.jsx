import { Fragment, useEffect, useState, useCallback } from "react";
import { useParams, useNavigate } from "react-router-dom";
import {
  AlertTriangle, CheckCircle, Lock, Flag, Save,
  Trophy, ChevronDown, ChevronUp, Loader2, Edit3, X, Download, MessageSquare, Trash2,
} from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

function safeNumber(value, fallback = 0) {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

function formatScore(value) {
  return safeNumber(value).toFixed(2);
}

function getScoreTier(score) {
  if (score >= 4) return "strong";
  if (score >= 3) return "mid";
  return "weak";
}

function scoreTierClass(score) {
  const tier = getScoreTier(score);
  if (tier === "strong") return "bg-emerald-50 text-emerald-700 border-emerald-200";
  if (tier === "mid") return "bg-amber-50 text-amber-700 border-amber-200";
  return "bg-crimson/5 text-crimson border-crimson/20";
}

function scoreHeatClass(score, maxScore = 5) {
  const pct = maxScore > 0 ? safeNumber(score) / maxScore : 0;
  if (pct >= 0.8) return "bg-emerald-500 text-white";
  if (pct >= 0.6) return "bg-emerald-200 text-emerald-900";
  if (pct >= 0.4) return "bg-amber-100 text-amber-900";
  return "bg-crimson/10 text-crimson";
}

function criterionSpread(criterionId, vendors) {
  const scores = vendors
    .map((vendor) => vendor.criteria?.find((entry) => entry.criterion_id === criterionId))
    .filter(Boolean)
    .map((entry) => safeNumber(entry.final_score));
  if (!scores.length) return 0;
  return Math.max(...scores) - Math.min(...scores);
}

function buildHeadToHeadRows(vendorA, vendorB, criteria) {
  if (!vendorA || !vendorB) return [];
  return criteria
    .map((criterion) => {
      const a = vendorA.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id);
      const b = vendorB.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id);
      const aScore = safeNumber(a?.final_score);
      const bScore = safeNumber(b?.final_score);
      const delta = aScore - bScore;
      return {
        criterion_id: criterion.criterion_id,
        criterion_label: criterion.criterion_label,
        max_score: criterion.max_score,
        aScore,
        bScore,
        delta,
      };
    })
    .sort((x, y) => Math.abs(y.delta) - Math.abs(x.delta));
}

function buildExplanation(vendor, vendors, criteria) {
  const criteriaIds = new Set(criteria.map((criterion) => criterion.criterion_id));
  const peerAvgMap = new Map();

  criteria.forEach((criterion) => {
    const scores = vendors
      .map((other) => other.criteria?.find((c) => c.criterion_id === criterion.criterion_id))
      .filter(Boolean)
      .map((entry) => safeNumber(entry.final_score));
    const total = scores.reduce((sum, value) => sum + value, 0);
    peerAvgMap.set(criterion.criterion_id, scores.length ? total / scores.length : 0);
  });

  const scoredCriteria = (vendor.criteria || [])
    .filter((criterion) => criteriaIds.has(criterion.criterion_id))
    .map((criterion) => ({
      ...criterion,
      peer_avg: peerAvgMap.get(criterion.criterion_id) ?? 0,
      weighted_score: safeNumber(criterion.final_score) * safeNumber(criterion.weight),
    }));

  const strengths = [...scoredCriteria]
    .sort((a, b) => b.final_score - a.final_score || b.weighted_score - a.weighted_score)
    .slice(0, 3);

  const risks = [...scoredCriteria]
    .sort((a, b) => a.final_score - b.final_score || b.weighted_score - a.weighted_score)
    .slice(0, 3);

  const differentiators = [...scoredCriteria]
    .map((criterion) => ({
      ...criterion,
      delta_vs_peer: safeNumber(criterion.final_score) - safeNumber(criterion.peer_avg),
    }))
    .sort((a, b) => Math.abs(b.delta_vs_peer) - Math.abs(a.delta_vs_peer))
    .slice(0, 3);

  const total = safeNumber(vendor.total_score);
  const rank = vendor.preliminary_rank;
  const leader = vendors[0];
  const leadGap = leader ? safeNumber(leader.total_score) - total : 0;

  return {
    summary:
      rank === 1
        ? `This vendor is currently leading with a ${formatScore(total)} total score.`
        : `${vendor.vendor_name} trails the leader by ${formatScore(leadGap)} points and needs stronger performance in its weakest criteria.`,
    strengths,
    risks,
    differentiators,
  };
}

function openChatWithPrompt(prompt, autoSend = true) {
  window.dispatchEvent(new CustomEvent("open-chat", { detail: { prompt, autoSend } }));
}

function formatArtifactTime(savedAt) {
  if (!savedAt) return "Recently";
  try {
    return new Date(savedAt).toLocaleString([], {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "Recently";
  }
}

function VendorInsightCard({ vendor, vendors, criteria }) {
  const insight = buildExplanation(vendor, vendors, criteria);
  return (
    <div className="card p-5 border border-steel/70">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Decision support</div>
          <div className="font-heading text-lg text-ink">{vendor.vendor_name}</div>
        </div>
        <div className={clsx("text-xs font-mono border px-2 py-1 rounded-full", scoreTierClass(safeNumber(vendor.total_score)))}>
          Rank #{vendor.preliminary_rank} · {formatScore(vendor.total_score)}
        </div>
      </div>

      <p className="text-sm text-ink-muted leading-relaxed mb-4">{insight.summary}</p>

      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="rounded-xl border border-steel bg-ivory/60 p-3">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Strengths</div>
          <div className="space-y-1.5">
            {insight.strengths.map((criterion) => (
              <div key={criterion.criterion_id} className="text-xs text-ink flex items-center justify-between gap-2">
                <span className="truncate">{criterion.criterion_label}</span>
                <span className={clsx("font-mono border px-1.5 py-0.5 rounded", scoreTierClass(safeNumber(criterion.final_score)))}>
                  {formatScore(criterion.final_score)}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl border border-steel bg-ivory/60 p-3">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Risks</div>
          <div className="space-y-1.5">
            {insight.risks.map((criterion) => (
              <div key={criterion.criterion_id} className="text-xs text-ink flex items-center justify-between gap-2">
                <span className="truncate">{criterion.criterion_label}</span>
                <span className={clsx("font-mono border px-1.5 py-0.5 rounded", scoreTierClass(safeNumber(criterion.final_score)))}>
                  {formatScore(criterion.final_score)}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="rounded-xl border border-steel bg-ivory/60 p-3">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Differentiators</div>
          <div className="space-y-1.5">
            {insight.differentiators.map((criterion) => (
              <div key={criterion.criterion_id} className="text-xs text-ink flex items-center justify-between gap-2">
                <span className="truncate">{criterion.criterion_label}</span>
                <span className={clsx(
                  "font-mono border px-1.5 py-0.5 rounded",
                  criterion.delta_vs_peer >= 0 ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-crimson/5 text-crimson border-crimson/20",
                )}>
                  {criterion.delta_vs_peer >= 0 ? "+" : ""}{criterion.delta_vs_peer.toFixed(2)}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function EvidenceDiffTable({ criteria, vendors }) {
  return (
    <div className="card p-5 border border-steel/70">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Evidence diff</div>
          <div className="font-heading text-lg text-ink">Per-criterion comparison</div>
        </div>
        <div className="text-xs text-ink-muted font-mono">AI score · Human score · Final score</div>
      </div>

      <div className="space-y-4">
        {criteria.map((criterion) => (
          <div key={criterion.criterion_id} className="rounded-xl border border-steel overflow-hidden">
            <div className="flex items-center justify-between px-4 py-3 bg-ivory/50 border-b border-steel">
              <div>
                <div className="font-medium text-ink">{criterion.criterion_label}</div>
                <div className="text-[10px] font-mono text-ink-muted uppercase tracking-widest">
                  Max score {criterion.max_score} · Weight {criterion.weight != null ? `${Math.round(safeNumber(criterion.weight) > 1 ? safeNumber(criterion.weight) : safeNumber(criterion.weight) * 100)}%` : "—"}
                </div>
              </div>
              <span className="text-[10px] font-mono px-2 py-1 rounded-full border bg-white text-ink-muted">
                {criterion.criterion_type || "scored"}
              </span>
            </div>
            <div className="divide-y divide-steel/40">
              {vendors.map((vendor) => {
                const scoreItem = vendor.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id);
                if (!scoreItem) return null;
                return (
                  <div key={`${vendor.response_id}-${criterion.criterion_id}`} className="px-4 py-3 grid grid-cols-[1.2fr_0.8fr_0.8fr_0.8fr_1.8fr] gap-3 items-start">
                    <div>
                      <div className="text-sm font-medium text-ink">{vendor.vendor_name}</div>
                      <div className="text-[10px] text-ink-muted font-mono">{scoreItem.override_reason ? "Overridden" : scoreItem.has_variance ? "Variance flagged" : "Reviewed"}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">AI</div>
                      <div className="font-mono text-sm text-ink">{formatScore(scoreItem.ai_score)}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Human</div>
                      <div className="font-mono text-sm text-ink">{scoreItem.human_score != null ? formatScore(scoreItem.human_score) : "—"}</div>
                    </div>
                    <div>
                      <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Final</div>
                      <div className={clsx("inline-flex text-sm font-mono border px-2 py-1 rounded", scoreTierClass(safeNumber(scoreItem.final_score)))}>
                        {formatScore(scoreItem.final_score)}
                      </div>
                    </div>
                    <div>
                      <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Evidence</div>
                      <div className="text-xs text-ink leading-relaxed">
                        {scoreItem.justification || "No justification available."}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ConflictHeatmap({ criteria, vendors }) {
  return (
    <div className="card p-5 border border-steel/70">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Conflict heatmap</div>
          <div className="font-heading text-lg text-ink">Normalized score spread by criterion</div>
        </div>
        <div className="text-xs text-ink-muted font-mono">Higher contrast = higher disagreement</div>
      </div>

      <div className="overflow-x-auto">
        <div className="min-w-[780px]">
          <div className="grid gap-2" style={{ gridTemplateColumns: `220px repeat(${vendors.length}, minmax(110px, 1fr))` }}>
            <div className="px-3 py-2 text-[10px] font-mono uppercase tracking-widest text-ink-muted">Criterion</div>
            {vendors.map((vendor) => (
              <div key={vendor.response_id} className="px-3 py-2 text-[10px] font-mono uppercase tracking-widest text-ink-muted text-center">
                {vendor.vendor_name}
              </div>
            ))}

            {criteria.map((criterion) => {
              const scores = vendors
                .map((vendor) => vendor.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id))
                .filter(Boolean)
                .map((entry) => safeNumber(entry.final_score));
              const min = scores.length ? Math.min(...scores) : 0;
              const max = scores.length ? Math.max(...scores) : 0;
              const spread = max - min;
              return (
                <Fragment key={criterion.criterion_id}>
                  <div key={`${criterion.criterion_id}-label`} className="px-3 py-3 rounded-lg border border-steel bg-ivory/50">
                    <div className="text-sm font-medium text-ink">{criterion.criterion_label}</div>
                    <div className="text-[10px] font-mono text-ink-muted uppercase tracking-widest mt-1">
                      Spread {spread.toFixed(2)} / {criterion.max_score || 5}
                    </div>
                  </div>
                  {vendors.map((vendor) => {
                    const scoreItem = vendor.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id);
                    const finalScore = safeNumber(scoreItem?.final_score);
                    const cellClass = scoreHeatClass(finalScore, safeNumber(criterion.max_score, 5));
                    const varianceClass = scoreItem?.has_variance ? "ring-2 ring-amber-400" : "";
                    return (
                      <div
                        key={`${criterion.criterion_id}-${vendor.response_id}`}
                        className={clsx("rounded-lg border border-steel px-3 py-3 text-center", cellClass, varianceClass)}
                        title={`${vendor.vendor_name}: ${formatScore(finalScore)} / ${criterion.max_score || 5}`}
                      >
                        <div className="font-mono text-sm font-medium">{formatScore(finalScore)}</div>
                        {scoreItem?.has_variance && (
                          <div className="mt-1 text-[10px] font-mono uppercase tracking-widest">Variance</div>
                        )}
                      </div>
                    );
                  })}
                </Fragment>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

function HeadToHeadPanel({ vendorA, vendorB, criteria }) {
  if (!vendorA || !vendorB) return null;
  const rows = buildHeadToHeadRows(vendorA, vendorB, criteria);

  function exportCsv() {
    const header = [
      "criterion",
      `${vendorA.vendor_name}_score`,
      `${vendorB.vendor_name}_score`,
      "delta",
      "max_score",
    ];
    const lines = rows.map((row) => [
      `"${String(row.criterion_label).replace(/"/g, '""')}"`,
      row.aScore.toFixed(2),
      row.bScore.toFixed(2),
      row.delta.toFixed(2),
      safeNumber(row.max_score, 5).toFixed(2),
    ].join(","));

    const csv = [header.join(","), ...lines].join("\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${vendorA.vendor_name}-vs-${vendorB.vendor_name}-head-to-head.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  function exportPdf() {
    const rowsHtml = rows.map((row) => `
      <tr>
        <td>${row.criterion_label}</td>
        <td>${row.aScore.toFixed(2)}</td>
        <td>${row.bScore.toFixed(2)}</td>
        <td>${row.delta >= 0 ? "+" : ""}${row.delta.toFixed(2)}</td>
        <td>${safeNumber(row.max_score, 5).toFixed(2)}</td>
      </tr>
    `).join("");

    const popup = window.open("", "_blank", "width=1000,height=760");
    if (!popup) return;
    popup.document.write(`
      <html>
        <head>
          <title>Head-to-head comparison</title>
          <style>
            body { font-family: Arial, sans-serif; padding: 24px; color: #20232a; }
            h1 { font-size: 18px; margin-bottom: 6px; }
            p { margin-top: 0; margin-bottom: 16px; color: #666; }
            table { border-collapse: collapse; width: 100%; font-size: 12px; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background: #f8f8f8; }
          </style>
        </head>
        <body>
          <h1>${vendorA.vendor_name} vs ${vendorB.vendor_name}</h1>
          <p>Criterion-level delta table (sorted by absolute difference)</p>
          <table>
            <thead>
              <tr>
                <th>Criterion</th>
                <th>${vendorA.vendor_name}</th>
                <th>${vendorB.vendor_name}</th>
                <th>Delta</th>
                <th>Max score</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </body>
      </html>
    `);
    popup.document.close();
    popup.focus();
    popup.print();
  }

  return (
    <div className="card p-5 border border-steel/70 mb-6">
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Head to head</div>
          <div className="font-heading text-lg text-ink">{vendorA.vendor_name} vs {vendorB.vendor_name}</div>
        </div>
        <div className="flex items-center gap-2">
          <div className="text-xs text-ink-muted font-mono">Positive delta favors {vendorA.vendor_name}</div>
          <button onClick={exportCsv} className="btn-ghost text-xs py-1.5 px-2.5 flex items-center gap-1">
            <Download size={12} /> CSV
          </button>
          <button onClick={exportPdf} className="btn-ghost text-xs py-1.5 px-2.5 flex items-center gap-1">
            <Download size={12} /> PDF
          </button>
        </div>
      </div>

      <div className="space-y-2">
        {rows.map((row) => (
          <div key={row.criterion_id} className="grid grid-cols-[1.8fr_0.7fr_0.7fr_0.8fr] gap-3 items-center rounded-lg border border-steel px-3 py-2.5">
            <div>
              <div className="text-sm font-medium text-ink">{row.criterion_label}</div>
              <div className="text-[10px] font-mono text-ink-muted uppercase tracking-widest">Max {row.max_score || 5}</div>
            </div>
            <div className="text-center">
              <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-0.5">{vendorA.vendor_name}</div>
              <div className="font-mono text-sm text-ink">{formatScore(row.aScore)}</div>
              <div className="mt-1 h-1.5 bg-steel-light rounded-full overflow-hidden">
                <div
                  className="h-full bg-emerald-500"
                  style={{ width: `${Math.min((row.aScore / safeNumber(row.max_score, 5)) * 100, 100)}%` }}
                />
              </div>
            </div>
            <div className="text-center">
              <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-0.5">{vendorB.vendor_name}</div>
              <div className="font-mono text-sm text-ink">{formatScore(row.bScore)}</div>
              <div className="mt-1 h-1.5 bg-steel-light rounded-full overflow-hidden">
                <div
                  className="h-full bg-crimson"
                  style={{ width: `${Math.min((row.bScore / safeNumber(row.max_score, 5)) * 100, 100)}%` }}
                />
              </div>
            </div>
            <div className="text-center">
              <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-0.5">Delta</div>
              <div className={clsx(
                "inline-flex px-2 py-1 rounded border font-mono text-sm",
                row.delta >= 0 ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-crimson/5 text-crimson border-crimson/20",
              )}>
                {row.delta >= 0 ? "+" : ""}{row.delta.toFixed(2)}
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function score(val) {
  return val != null ? Number(val).toFixed(2) : "—";
}

function RankBadge({ rank }) {
  const cls = rank === 1
    ? "bg-amber-400 text-white"
    : rank === 2
    ? "bg-gray-300 text-gray-800"
    : rank === 3
    ? "bg-orange-300 text-white"
    : "bg-steel-light text-ink-muted";
  return (
    <div className={clsx("w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0", cls)}>
      #{rank}
    </div>
  );
}

// ── Override modal ────────────────────────────────────────────────────────────
function OverrideModal({ scoreItem, rfpId, onClose, onSaved }) {
  const [newScore, setNewScore]   = useState(scoreItem.final_score ?? scoreItem.ai_score ?? 0);
  const [reason,   setReason]     = useState("");
  const [saving,   setSaving]     = useState(false);
  const [error,    setError]      = useState("");

  async function save() {
    if (!reason.trim()) { setError("Reason is required."); return; }
    setSaving(true);
    try {
      await rfpApi.consensusOverride(rfpId, {
        score_id:        scoreItem.score_id,
        final_score:     Number(newScore),
        override_reason: reason.trim(),
      });
      onSaved();
      onClose();
    } catch (e) {
      setError(e?.response?.data?.detail || "Override failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[500] flex items-center justify-center bg-ink/60 px-4"
         onClick={onClose}>
      <div className="w-full max-w-md bg-white rounded-2xl shadow-2xl border border-steel p-6"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <div className="font-heading text-lg text-ink">Admin Override</div>
          <button onClick={onClose}><X size={18} className="text-ink-muted" /></button>
        </div>
        <div className="grid grid-cols-3 gap-3 mb-5 p-3 bg-ivory rounded-lg border border-steel/50 text-center">
          <div>
            <div className="text-[10px] font-mono text-ink-muted uppercase mb-1">AI score</div>
            <div className="text-xl font-display">{score(scoreItem.ai_score)}</div>
          </div>
          <div>
            <div className="text-[10px] font-mono text-ink-muted uppercase mb-1">Human</div>
            <div className="text-xl font-display">{score(scoreItem.human_score)}</div>
          </div>
          <div>
            <div className="text-[10px] font-mono text-crimson uppercase mb-1">New</div>
            <input type="number" min={0} max={scoreItem.max_score || 5} step={0.5}
              value={newScore} onChange={e => setNewScore(e.target.value)}
              className="w-full text-center text-xl font-display text-crimson border border-crimson/30 rounded-lg px-2 py-1 bg-white focus:outline-none" />
          </div>
        </div>
        <textarea value={reason} onChange={e => setReason(e.target.value)}
          placeholder="Reason for admin override (required)…"
          rows={3}
          className="w-full border border-steel rounded-lg px-3 py-2 text-sm mb-3 focus:outline-none focus:border-crimson resize-none" />
        {error && <div className="text-crimson text-xs mb-3">{error}</div>}
        <div className="flex gap-3">
          <button onClick={onClose} className="btn-ghost flex-1">Cancel</button>
          <button onClick={save} disabled={saving} className="btn-primary flex-1 flex items-center justify-center gap-2">
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}
            {saving ? "Saving…" : "Save override"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Vendor accordion ──────────────────────────────────────────────────────────
function VendorSection({ vendor, rfpId, isWinner, onOverrideSaved }) {
  const [open,          setOpen]          = useState(vendor.preliminary_rank === 1);
  const [overrideTarget, setOverrideTarget] = useState(null);

  const varianceCount = (vendor.criteria || []).filter(c => c.has_variance).length;

  return (
    <div className={clsx(
      "card overflow-hidden mb-4 border-2 transition-all",
      isWinner ? "border-amber-400" : "border-transparent",
    )}>
      {/* Header */}
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center gap-4 px-6 py-4 hover:bg-ivory transition-colors text-left"
      >
        <RankBadge rank={vendor.preliminary_rank} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-heading text-lg text-ink">{vendor.vendor_name}</span>
            {isWinner && (
              <Trophy size={14} className="text-amber-500" title="Declared winner" />
            )}
          </div>
          <div className="text-[10px] text-ink-muted font-mono">
            Weighted total: {Number(vendor.total_score).toFixed(3)} / 5.000
            {varianceCount > 0 && (
              <span className="ml-3 text-amber-600">
                ⚠ {varianceCount} variance{varianceCount !== 1 ? "s" : ""} flagged
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="text-2xl font-display text-ink">
            {Number(vendor.total_score).toFixed(2)}
          </div>
          {open ? <ChevronUp size={16} className="text-ink-muted" /> : <ChevronDown size={16} className="text-ink-muted" />}
        </div>
      </button>

      {/* Criteria table */}
      {open && (
        <div className="border-t border-steel overflow-x-auto">
          <table className="w-full text-sm font-body">
            <thead>
              <tr className="bg-ivory border-b border-steel">
                <th className="text-left px-5 py-2 text-[10px] font-mono uppercase text-ink-muted">Criterion</th>
                <th className="text-center px-4 py-2 text-[10px] font-mono uppercase text-ink-muted">AI</th>
                <th className="text-center px-4 py-2 text-[10px] font-mono uppercase text-ink-muted">Human</th>
                <th className="text-center px-4 py-2 text-[10px] font-mono uppercase text-ink-muted">Δ Delta</th>
                <th className="text-center px-4 py-2 text-[10px] font-mono uppercase text-ink-muted">Final</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {(vendor.criteria || []).map(c => (
                <tr key={c.criterion_id}
                  className={clsx("border-b border-steel/40", c.has_variance && "bg-amber-50/40")}>
                  <td className="px-5 py-3">
                    <div className="font-medium text-ink text-sm">{c.criterion_label}</div>
                    {c.owner_role && (
                      <div className="text-[10px] text-ink-muted font-mono">{c.owner_role.replace("_", " ")}</div>
                    )}
                    {c.override_reason && (
                      <div className="text-[10px] text-emerald-700 mt-0.5 italic">
                        Override: {c.override_reason.replace("[CONSENSUS ADMIN OVERRIDE] ", "")}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center font-mono text-xs">{score(c.ai_score)}</td>
                  <td className="px-4 py-3 text-center font-mono text-xs">
                    {c.human_score != null
                      ? <span className="text-emerald-700 font-medium">{score(c.human_score)}</span>
                      : <span className="text-ink-muted">—</span>}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {c.delta != null ? (
                      <span className={clsx(
                        "text-[10px] font-mono px-1.5 py-0.5 rounded",
                        c.has_variance
                          ? "bg-amber-100 text-amber-700 font-bold"
                          : "text-ink-muted",
                      )}>
                        {c.has_variance && "⚠ "}{Number(c.delta).toFixed(2)}
                      </span>
                    ) : <span className="text-ink-muted text-[10px]">—</span>}
                  </td>
                  <td className="px-4 py-3 text-center">
                    <span className="font-mono text-sm font-bold text-ink">
                      {score(c.final_score)}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-center">
                    {c.score_id && (
                      <button
                        onClick={() => setOverrideTarget(c)}
                        className="p-1 hover:bg-ivory rounded text-ink-muted hover:text-crimson transition-colors"
                        title="Admin override"
                      >
                        <Edit3 size={12} />
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {overrideTarget && (
        <OverrideModal
          scoreItem={overrideTarget}
          rfpId={rfpId}
          onClose={() => setOverrideTarget(null)}
          onSaved={onOverrideSaved}
        />
      )}
    </div>
  );
}

// ── Declare winner modal ───────────────────────────────────────────────────────
function DeclareWinnerModal({ vendors, rfpId, currentWinnerId, onClose, onDeclared }) {
  const [selected, setSelected] = useState(currentWinnerId || "");
  const [reason,   setReason]   = useState("");
  const [tieNote,  setTieNote]  = useState("");
  const [saving,   setSaving]   = useState(false);
  const [error,    setError]    = useState("");

  async function save() {
    if (!selected) { setError("Select a vendor."); return; }
    if (reason.trim().length < 10) { setError("Reason must be at least 10 characters."); return; }
    setSaving(true);
    try {
      await rfpApi.declareWinner(rfpId, {
        vendor_id:         selected,
        reason:            reason.trim(),
        tie_breaker_notes: tieNote.trim() || undefined,
      });
      onDeclared();
      onClose();
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to declare winner");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-[500] flex items-center justify-center bg-ink/60 px-4"
         onClick={onClose}>
      <div className="w-full max-w-lg bg-white rounded-2xl shadow-2xl border border-steel p-6"
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center gap-3 mb-5">
          <Trophy size={24} className="text-amber-500" />
          <div className="font-heading text-xl text-ink">Declare Winner</div>
        </div>
        <div className="mb-4">
          <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-2">Select winning vendor</label>
          <div className="space-y-2">
            {vendors.map(v => (
              <label key={v.response_id}
                className={clsx("flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-all",
                  selected === v.vendor_id
                    ? "border-amber-400 bg-amber-50"
                    : "border-steel hover:border-ink-muted")}>
                <input type="radio" name="winner" value={v.vendor_id || v.response_id}
                  checked={selected === (v.vendor_id || v.response_id)}
                  onChange={() => setSelected(v.vendor_id || v.response_id)}
                  className="text-crimson" />
                <div className="flex-1">
                  <span className="font-medium text-ink">{v.vendor_name}</span>
                  <span className="ml-2 text-xs text-ink-muted font-mono">
                    Score: {Number(v.total_score).toFixed(2)}
                  </span>
                  {v.preliminary_rank === 1 && (
                    <span className="ml-2 text-[10px] text-amber-600 font-mono">Highest ranked</span>
                  )}
                </div>
              </label>
            ))}
          </div>
        </div>
        <div className="mb-3">
          <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
            Decision rationale * (min 10 chars)
          </label>
          <textarea value={reason} onChange={e => setReason(e.target.value)}
            placeholder="e.g. Highest combined technical and financial score with proven reference sites."
            rows={3}
            className="w-full border border-steel rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-crimson resize-none" />
        </div>
        <div className="mb-4">
          <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">
            Tie-breaker notes (optional)
          </label>
          <input value={tieNote} onChange={e => setTieNote(e.target.value)}
            placeholder="e.g. Vendor A selected over Vendor B due to faster implementation timeline"
            className="w-full border border-steel rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-crimson" />
        </div>
        {error && <div className="text-crimson text-xs mb-3">{error}</div>}
        <div className="flex gap-3">
          <button onClick={onClose} className="btn-ghost flex-1">Cancel</button>
          <button onClick={save} disabled={saving}
            className="btn-primary flex-1 flex items-center justify-center gap-2">
            {saving ? <Loader2 size={14} className="animate-spin" /> : <Trophy size={14} />}
            {saving ? "Saving…" : "Declare winner"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────
export default function ConsensusView() {
  const { rfpId }    = useParams();
  const navigate     = useNavigate();
  const [data,       setData]        = useState(null);
  const [loading,    setLoading]     = useState(true);
  const [notes,      setNotes]       = useState("");
  const [savingNotes,setSavingNotes] = useState(false);
  const [locking,    setLocking]     = useState(false);
  const [showWinner, setShowWinner]  = useState(false);
  const [error,      setError]       = useState("");
  const [lockDone,   setLockDone]    = useState(false);
  const [disputedOnly, setDisputedOnly] = useState(false);
  const [vendorAId, setVendorAId] = useState("");
  const [vendorBId, setVendorBId] = useState("");
  const [compareArtifacts, setCompareArtifacts] = useState([]);
  const [compareFilter, setCompareFilter] = useState("");

  const load = useCallback(async () => {
    try {
      const r = await rfpApi.getConsensus(rfpId);
      setData(r.data);
      setNotes(r.data.consensus_notes || "");
    } catch (e) {
      setError(e?.response?.data?.detail || "Could not load consensus view");
    } finally {
      setLoading(false);
    }

    try {
      const artifacts = await rfpApi.chatArtifacts(rfpId);
      setCompareArtifacts(Array.isArray(artifacts.data?.compares) ? artifacts.data.compares : []);
    } catch {
      setCompareArtifacts([]);
    }
  }, [rfpId]);

  useEffect(() => { load(); }, [load]);

  async function saveNotes() {
    setSavingNotes(true);
    try { await rfpApi.saveConsensusNotes(rfpId, { notes }); }
    finally { setSavingNotes(false); }
  }

  async function handleLock() {
    if (!window.confirm(
      "Lock this RFP permanently?\n\nThis is terminal and irreversible. " +
      "No scores, winner, or criteria can be changed after locking. " +
      "A final report will be generated automatically.",
    )) return;
    setLocking(true);
    try {
      await rfpApi.lockRfp(rfpId);
      setLockDone(true);
      await load();
    } catch (e) {
      setError(e?.response?.data?.detail || "Lock failed");
    } finally {
      setLocking(false);
    }
  }

  async function saveCompareSnapshot() {
    if (!data?.vendors?.length) return;
    const snapshot = {
      type: "consensus_compare",
      rfpId,
      savedAt: new Date().toISOString(),
      ranking: data.vendors.map((vendor) => vendor.vendor_name),
      topVendor: data.vendors[0]?.vendor_name || null,
      runnerUp: data.vendors[1]?.vendor_name || null,
      highlights: (data?.vendors?.[0]?.criteria || []).map((criterion) => {
        const scores = (data?.vendors || []).map((vendor) => Number(vendor?.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id)?.final_score || 0));
        return {
          label: criterion.criterion_label,
          spread: scores.length ? Math.max(...scores) - Math.min(...scores) : 0,
          scores: Object.fromEntries((data?.vendors || []).map((vendor) => [vendor.vendor_name, Number(vendor?.criteria?.find((entry) => entry.criterion_id === criterion.criterion_id)?.final_score || 0)])),
        };
      }).sort((a, b) => b.spread - a.spread).slice(0, 4),
    };

    try {
      await rfpApi.createChatArtifact(rfpId, {
        artifact_type: "compare",
        content: JSON.stringify(snapshot),
      });
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to save compare snapshot");
    }
  }

  async function deleteCompareSnapshot(artifactId) {
    if (!window.confirm("Delete this saved compare snapshot?")) return;
    try {
      await rfpApi.deleteChatArtifact(rfpId, artifactId);
      setCompareArtifacts((prev) => prev.filter((item) => item.artifact_id !== artifactId));
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to delete compare snapshot");
    }
  }

  const visibleCompareArtifacts = compareArtifacts.filter((artifact) => {
    if (!compareFilter.trim()) return true;
    const needle = compareFilter.trim().toLowerCase();
    let parsed = null;
    try {
      parsed = JSON.parse(artifact.content);
    } catch {
      parsed = null;
    }
    const haystack = [
      parsed?.topVendor,
      parsed?.runnerUp,
      ...(Array.isArray(parsed?.ranking) ? parsed.ranking : []),
      ...(Array.isArray(parsed?.highlights) ? parsed.highlights.map((item) => item.label) : []),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(needle);
  });

  if (loading) return (
    <div className="flex items-center justify-center min-h-[400px] flex-col gap-3 text-ink-muted">
      <Loader2 size={28} className="animate-spin text-crimson" />
      <div className="text-xs font-mono uppercase tracking-widest">Loading consensus view…</div>
    </div>
  );

  if (error && !data) return (
    <div className="p-8 text-center">
      <AlertTriangle size={32} className="text-crimson mx-auto mb-3" />
      <div className="text-sm text-ink-muted">{error}</div>
    </div>
  );

  const vendors         = data?.vendors || [];
  const isLocked        = data?.rfp_status === "locked";
  const winnerVendorId  = data?.winner_vendor_id;
  const totalVariance   = vendors.reduce((sum, v) => sum + (v.variance_flags || 0), 0);
  const criteria        = vendors[0]?.criteria || [];
  const leadVendor      = vendors[0] || null;
  const runnerUpVendor  = vendors[1] || null;
  const totalLeadGap    = leadVendor && runnerUpVendor ? safeNumber(leadVendor.total_score) - safeNumber(runnerUpVendor.total_score) : 0;
  const disputedCriteria = criteria.filter((criterion) => {
    const spread = criterionSpread(criterion.criterion_id, vendors);
    return spread > 0.75;
  });
  const visibleCriteria = disputedOnly ? disputedCriteria : criteria;

  useEffect(() => {
    if (!vendors.length) return;
    const first = vendors[0]?.response_id || "";
    const second = vendors[1]?.response_id || vendors[0]?.response_id || "";
    if (!vendorAId) setVendorAId(first);
    if (!vendorBId) setVendorBId(second);
  }, [vendors, vendorAId, vendorBId]);

  const vendorA = vendors.find((v) => v.response_id === vendorAId) || vendors[0] || null;
  const vendorB = vendors.find((v) => v.response_id === vendorBId) || vendors[1] || vendors[0] || null;

  return (
    <div className="p-8 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex items-start justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">CONSENSUS</div>
          <div className="text-ink-muted font-body text-sm mt-1.5">{data?.rfp_title}</div>
          <div className="flex items-center gap-3 mt-2">
            <span className="text-[10px] font-mono text-ink-muted">
              {vendors.length} vendors · {data?.evaluator_count} evaluators
            </span>
            {data?.all_submitted && (
              <span className="text-[10px] font-mono text-emerald-700 bg-emerald-50 border border-emerald-200 px-2 py-0.5 rounded-full">
                All submitted
              </span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button
            onClick={() => navigate(`/evaluation/${rfpId}/compare`)}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <ChevronRight size={15} /> Open workspace
          </button>
          <button
            onClick={saveCompareSnapshot}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <Save size={15} /> Save compare
          </button>
          <button
            onClick={() => openChatWithPrompt(
              `Compare ${leadVendor?.vendor_name || "the leading vendor"} and ${runnerUpVendor?.vendor_name || "the runner-up"} on strengths, risks, and the final recommendation.`,
              true,
            )}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <MessageSquare size={15} /> Ask AI
          </button>
          {!isLocked && (
            <>
              <button onClick={() => setShowWinner(true)}
                className="btn-ghost flex items-center gap-2 text-sm">
                <Trophy size={15} className="text-amber-500" />
                {winnerVendorId ? "Change winner" : "Declare winner"}
              </button>
              <button onClick={handleLock} disabled={locking || !winnerVendorId}
                className="btn-primary flex items-center gap-2 text-sm"
                title={!winnerVendorId ? "Declare a winner first" : ""}>
                {locking ? <Loader2 size={14} className="animate-spin" /> : <Lock size={14} />}
                {locking ? "Locking…" : "Lock RFP"}
              </button>
            </>
          )}
          {isLocked && (
            <div className="flex items-center gap-2 bg-emerald-50 border border-emerald-200 text-emerald-700 text-xs font-mono px-4 py-2.5 rounded-xl">
              <Lock size={13} /> Locked {data?.locked_at ? new Date(data.locked_at).toLocaleDateString() : ""}
            </div>
          )}
        </div>
      </div>

      {/* Winner banner */}
      {winnerVendorId && (
        <div className="mb-6 flex items-center gap-3 px-5 py-4 bg-amber-50 border border-amber-300 rounded-xl">
          <Trophy size={20} className="text-amber-500 flex-shrink-0" />
          <div>
            <div className="font-medium text-ink text-sm">
              Winner declared: {vendors.find(v => v.response_id === winnerVendorId || v.vendor_id === winnerVendorId)?.vendor_name || "—"}
            </div>
            {!isLocked && (
              <div className="text-[11px] text-ink-muted mt-0.5">
                Click "Lock RFP" to finalise the decision and generate reports.
              </div>
            )}
          </div>
        </div>
      )}

      {/* Variance summary */}
      {totalVariance > 0 && (
        <div className="mb-6 flex items-center gap-3 px-4 py-3 bg-amber-50 border border-amber-200 rounded-xl text-sm text-amber-800">
          <AlertTriangle size={16} className="flex-shrink-0 text-amber-500" />
          <span>
            <strong>{totalVariance} score{totalVariance !== 1 ? "s" : ""}</strong> show significant divergence between AI and human evaluations. Review and override where needed.
          </span>
        </div>
      )}

      {/* Decision support */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Leader gap</div>
          <div className="font-display text-3xl text-ink">{formatScore(totalLeadGap)}</div>
          <div className="text-xs text-ink-muted mt-1">Points between rank #1 and rank #2</div>
        </div>
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Disputed criteria</div>
          <div className="font-display text-3xl text-ink">{disputedCriteria.length}</div>
          <div className="text-xs text-ink-muted mt-1">Criteria with visible spread across vendors</div>
        </div>
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Winner status</div>
          <div className="font-display text-3xl text-ink">{winnerVendorId ? "Set" : "Open"}</div>
          <div className="text-xs text-ink-muted mt-1">Decision ready for declaration or lock</div>
        </div>
      </div>

      {compareArtifacts.length > 0 && (
        <div className="card p-5 border border-steel/70 mb-6">
          <div className="flex flex-col gap-3 mb-4 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Saved compare snapshots</div>
              <div className="font-heading text-lg text-ink">Reopen prior decision frames</div>
            </div>
            <div className="flex items-center gap-3">
              <input
                value={compareFilter}
                onChange={(e) => setCompareFilter(e.target.value)}
                placeholder="Filter by vendor or criterion"
                className="min-w-[240px] border border-steel rounded-lg px-3 py-2 text-sm bg-white"
              />
              <div className="text-xs text-ink-muted font-mono">{visibleCompareArtifacts.length}/{compareArtifacts.length} saved</div>
            </div>
          </div>
          <div className="grid gap-3 md:grid-cols-2">
            {visibleCompareArtifacts.slice(0, 4).map((artifact) => {
              let parsed = null;
              try {
                parsed = JSON.parse(artifact.content);
              } catch {
                parsed = null;
              }
              return (
                <div key={artifact.artifact_id} className="rounded-xl border border-steel bg-white p-3">
                  <div className="flex items-start justify-between gap-3 mb-2">
                    <div>
                      <div className="text-sm font-medium text-ink">
                        {parsed?.topVendor || "Saved compare"}
                      </div>
                      <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">
                        {parsed?.runnerUp ? `vs ${parsed.runnerUp}` : formatArtifactTime(artifact.saved_at)}
                      </div>
                    </div>
                    <button
                      onClick={() => openChatWithPrompt(
                        parsed?.topVendor && parsed?.runnerUp
                          ? `Compare ${parsed.topVendor} vs ${parsed.runnerUp} on strengths, risks, and likely winner.`
                          : "Compare the top vendors and summarize the decision trade-offs.",
                        true,
                      )}
                      className="text-[10px] font-mono uppercase tracking-widest text-crimson hover:underline"
                    >
                      Reopen in chat
                    </button>
                  </div>
                  <div className="text-xs text-ink-muted leading-relaxed">
                    {Array.isArray(parsed?.highlights) && parsed.highlights.length > 0
                      ? `${parsed.highlights[0].label} spread ${Number(parsed.highlights[0].spread || 0).toFixed(2)}`
                      : "Snapshot preserved for later review."}
                  </div>
                  <div className="mt-3 flex items-center justify-between gap-2">
                    <button
                      onClick={() => deleteCompareSnapshot(artifact.artifact_id)}
                      className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-crimson inline-flex items-center gap-1"
                    >
                      <Trash2 size={10} /> Delete
                    </button>
                    <button
                      onClick={() => openChatWithPrompt(
                        parsed?.topVendor && parsed?.runnerUp
                          ? `Compare ${parsed.topVendor} vs ${parsed.runnerUp} on strengths, risks, and likely winner.`
                          : "Compare the top vendors and summarize the decision trade-offs.",
                        true,
                      )}
                      className="text-[10px] font-mono uppercase tracking-widest text-crimson hover:underline"
                    >
                      Reopen in chat
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div className="card p-4 border border-steel/70 mb-6">
        <div className="flex flex-wrap items-end gap-4">
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              checked={disputedOnly}
              onChange={(e) => setDisputedOnly(e.target.checked)}
            />
            Show disputed criteria only
          </label>

          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-ink-muted uppercase tracking-widest">Vendor A</span>
            <select
              value={vendorAId}
              onChange={(e) => setVendorAId(e.target.value)}
              className="border border-steel rounded-lg px-2.5 py-1.5 text-sm bg-white"
            >
              {vendors.map((vendor) => (
                <option key={`a-${vendor.response_id}`} value={vendor.response_id}>{vendor.vendor_name}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-xs font-mono text-ink-muted uppercase tracking-widest">Vendor B</span>
            <select
              value={vendorBId}
              onChange={(e) => setVendorBId(e.target.value)}
              className="border border-steel rounded-lg px-2.5 py-1.5 text-sm bg-white"
            >
              {vendors.map((vendor) => (
                <option key={`b-${vendor.response_id}`} value={vendor.response_id}>{vendor.vendor_name}</option>
              ))}
            </select>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-6">
        {vendors.map((vendor) => (
          <VendorInsightCard key={`insight-${vendor.response_id}`} vendor={vendor} vendors={vendors} criteria={visibleCriteria} />
        ))}
      </div>

      <HeadToHeadPanel vendorA={vendorA} vendorB={vendorB} criteria={visibleCriteria} />

      <div className="mb-6">
        <EvidenceDiffTable criteria={visibleCriteria} vendors={vendors} />
      </div>

      <div className="mb-6">
        <ConflictHeatmap criteria={visibleCriteria} vendors={vendors} />
      </div>

      {error && (
        <div className="mb-6 text-crimson text-sm bg-crimson/5 border border-crimson/20 rounded-xl px-4 py-3">{error}</div>
      )}

      {/* Vendor sections */}
      {vendors.map(v => (
        <VendorSection
          key={v.response_id}
          vendor={v}
          rfpId={rfpId}
          isWinner={v.response_id === winnerVendorId || v.vendor_id === winnerVendorId}
          onOverrideSaved={load}
        />
      ))}

      {/* Meeting notes */}
      {!isLocked && (
        <div className="card p-6 mt-6">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-3">
            Meeting notes
          </div>
          <textarea value={notes} onChange={e => setNotes(e.target.value)}
            placeholder="Record discussion points, decisions, and rationale from the consensus meeting…"
            rows={5}
            className="w-full border border-steel rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-crimson resize-none mb-3" />
          <div className="flex justify-end">
            <button onClick={saveNotes} disabled={savingNotes}
              className="btn-ghost text-sm flex items-center gap-2">
              {savingNotes ? <Loader2 size={13} className="animate-spin" /> : <Save size={13} />}
              {savingNotes ? "Saving…" : "Save notes"}
            </button>
          </div>
        </div>
      )}

      {data?.consensus_notes && isLocked && (
        <div className="card p-6 mt-6">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-2">Meeting notes</div>
          <div className="text-sm text-ink leading-relaxed whitespace-pre-wrap">{data.consensus_notes}</div>
        </div>
      )}

      {showWinner && (
        <DeclareWinnerModal
          vendors={vendors}
          rfpId={rfpId}
          currentWinnerId={winnerVendorId}
          onClose={() => setShowWinner(false)}
          onDeclared={load}
        />
      )}
    </div>
  );
}
