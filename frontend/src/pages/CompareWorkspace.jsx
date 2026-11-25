import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Download, MessageSquare, Save, Trash2, RefreshCcw, ChevronRight } from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

function safeNumber(value, fallback = 0) {
  const next = Number(value);
  return Number.isFinite(next) ? next : fallback;
}

function formatScore(value) {
  return safeNumber(value).toFixed(2);
}

function openChatWithPrompt(prompt, autoSend = true) {
  window.dispatchEvent(new CustomEvent("open-chat", { detail: { prompt, autoSend } }));
}

function formatTime(savedAt) {
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

function parseArtifact(artifact) {
  let parsed = null;
  try {
    parsed = JSON.parse(artifact.content);
  } catch {
    parsed = null;
  }
  return {
    ...artifact,
    parsed,
  };
}

function scoreSpread(criteria, vendorNames) {
  return criteria
    .map((criterion) => {
      const scores = vendorNames.map((vendorName) => Number(criterion?.scores?.[vendorName] || 0));
      return {
        label: criterion.label,
        spread: scores.length ? Math.max(...scores) - Math.min(...scores) : 0,
        scores: criterion.scores || {},
      };
    })
    .sort((a, b) => b.spread - a.spread);
}

export default function CompareWorkspace() {
  const { rfpId } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [compare, setCompare] = useState(null);
  const [artifacts, setArtifacts] = useState([]);
  const [compareFilter, setCompareFilter] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [compareRes, artifactsRes] = await Promise.all([
        rfpApi.compare(rfpId),
        rfpApi.chatArtifacts(rfpId),
      ]);
      setCompare({
        vendors: Array.isArray(compareRes.data?.vendors) ? compareRes.data.vendors : [],
        vendorDetails: Array.isArray(compareRes.data?.vendor_details) ? compareRes.data.vendor_details : [],
        criteria: Array.isArray(compareRes.data?.criteria) ? compareRes.data.criteria : [],
        totals: compareRes.data?.totals && typeof compareRes.data.totals === "object" ? compareRes.data.totals : {},
        ranking: Array.isArray(compareRes.data?.ranking) ? compareRes.data.ranking : [],
      });
      setArtifacts(
        Array.isArray(artifactsRes.data?.compares)
          ? artifactsRes.data.compares.map(parseArtifact)
          : [],
      );
    } catch (e) {
      setError(e?.response?.data?.detail || "Could not load compare workspace");
    } finally {
      setLoading(false);
    }
  }, [rfpId]);

  useEffect(() => { load(); }, [load]);

  async function saveSnapshot() {
    if (!compare?.ranking?.length) return;
    const snapshot = {
      type: "workspace_compare",
      rfpId,
      savedAt: new Date().toISOString(),
      topVendor: compare.ranking[0] || compare.vendors[0] || null,
      runnerUp: compare.ranking[1] || compare.vendors[1] || null,
      ranking: compare.ranking,
      vendors: compare.vendors,
      highlights: scoreSpread(compare.criteria || [], compare.vendors || []).slice(0, 6),
    };

    try {
      const response = await rfpApi.createChatArtifact(rfpId, {
        artifact_type: "compare",
        content: JSON.stringify(snapshot),
      });
      setArtifacts((prev) => [parseArtifact(response.data), ...prev]);
      setMessage("Compare snapshot saved.");
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to save compare snapshot.");
    }
  }

  async function deleteSnapshot(artifactId) {
    if (!window.confirm("Delete this compare snapshot?")) return;
    try {
      await rfpApi.deleteChatArtifact(rfpId, artifactId);
      setArtifacts((prev) => prev.filter((item) => item.artifact_id !== artifactId));
      setMessage("Compare snapshot deleted.");
    } catch (e) {
      setError(e?.response?.data?.detail || "Failed to delete compare snapshot.");
    }
  }

  const filteredArtifacts = artifacts.filter((artifact) => {
    if (!compareFilter.trim()) return true;
    const needle = compareFilter.trim().toLowerCase();
    const parsed = artifact.parsed || {};
    const haystack = [
      parsed.topVendor,
      parsed.runnerUp,
      ...(Array.isArray(parsed.ranking) ? parsed.ranking : []),
      ...(Array.isArray(parsed.highlights) ? parsed.highlights.map((item) => item.label) : []),
    ].filter(Boolean).join(" ").toLowerCase();
    return haystack.includes(needle);
  });

  const vendorNames = compare?.vendors || [];
  const topVendor = compare?.ranking?.[0] || vendorNames[0] || null;
  const runnerUp = compare?.ranking?.[1] || vendorNames[1] || null;
  const topVendorDetails = compare?.vendorDetails?.find((vendor) => vendor.name === topVendor);
  const runnerUpDetails = compare?.vendorDetails?.find((vendor) => vendor.name === runnerUp);

  const compareHighlights = scoreSpread(compare?.criteria || [], vendorNames).slice(0, 6);

  if (loading) {
    return (
      <div className="min-h-[50vh] flex items-center justify-center text-ink-muted">
        <div className="flex items-center gap-3">
          <RefreshCcw size={16} className="animate-spin" />
          <span className="text-xs font-mono uppercase tracking-widest">Loading compare workspace...</span>
        </div>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-7xl mx-auto">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between mb-8">
        <div>
          <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Compare workspace</div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">DECISION FRAME</div>
          <div className="font-heading text-lg text-ink-muted mt-2 italic">Side-by-side vendor comparison and saved snapshots</div>
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          <button onClick={() => navigate(`/evaluation/${rfpId}`)} className="btn-ghost inline-flex min-w-[150px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none">
            <ChevronRight size={14} className="shrink-0" /> <span>Back to evaluation</span>
          </button>
          <button onClick={() => openChatWithPrompt(`Compare ${topVendor || "the leading vendor"} vs ${runnerUp || "the runner-up"} on strengths, risks, and likely winner.`, true)} className="btn-ghost inline-flex min-w-[150px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none">
            <MessageSquare size={14} className="shrink-0" /> <span>Compare in chat</span>
          </button>
          <button onClick={saveSnapshot} className="btn-primary inline-flex min-w-[140px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none">
            <Save size={14} className="shrink-0" /> <span>Save snapshot</span>
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 text-crimson text-sm bg-crimson/5 border border-crimson/20 rounded-xl px-4 py-3">{error}</div>
      )}
      {message && (
        <div className="mb-6 text-ink text-sm bg-ivory border border-steel/60 rounded-xl px-4 py-3">{message}</div>
      )}

      <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-4 mb-6">
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Vendors</div>
          <div className="font-display text-3xl text-ink">{vendorNames.length}</div>
          <div className="text-xs text-ink-muted mt-1">Currently loaded into the comparison model</div>
        </div>
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Criteria</div>
          <div className="font-display text-3xl text-ink">{compare?.criteria?.length || 0}</div>
          <div className="text-xs text-ink-muted mt-1">Weighted sections in the decision frame</div>
        </div>
        <div className="card p-5 border border-steel/70">
          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Saved snapshots</div>
          <div className="font-display text-3xl text-ink">{artifacts.length}</div>
          <div className="text-xs text-ink-muted mt-1">Stored compare records for later review</div>
        </div>
      </div>

      <div className="card p-5 border border-steel/70 mb-6">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between mb-4">
          <div>
            <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Saved compare snapshots</div>
            <div className="font-heading text-lg text-ink">Search, reopen, delete</div>
          </div>
          <div className="flex items-center gap-3">
            <input
              value={compareFilter}
              onChange={(e) => setCompareFilter(e.target.value)}
              placeholder="Filter by vendor or criterion"
              className="min-w-[240px] border border-steel rounded-lg px-3 py-2 text-sm bg-white"
            />
            <div className="text-xs text-ink-muted font-mono">{filteredArtifacts.length}/{artifacts.length}</div>
          </div>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          {filteredArtifacts.slice(0, 6).map((artifact) => (
            <div key={artifact.artifact_id} className="rounded-xl border border-steel bg-white p-3">
              <div className="flex items-start justify-between gap-3 mb-2">
                <div>
                  <div className="text-sm font-medium text-ink">{artifact.parsed?.topVendor || "Saved compare"}</div>
                  <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">
                    {artifact.parsed?.runnerUp ? `vs ${artifact.parsed.runnerUp}` : formatTime(artifact.saved_at)}
                  </div>
                </div>
                <button
                  onClick={() => openChatWithPrompt(
                    artifact.parsed?.topVendor && artifact.parsed?.runnerUp
                      ? `Compare ${artifact.parsed.topVendor} vs ${artifact.parsed.runnerUp} on strengths, risks, and likely winner.`
                      : "Compare the top vendors and summarize the decision trade-offs.",
                    true,
                  )}
                  className="text-[10px] font-mono uppercase tracking-widest text-crimson hover:underline"
                >
                  Reopen in chat
                </button>
              </div>
              <div className="text-xs text-ink-muted leading-relaxed mb-3">
                {Array.isArray(artifact.parsed?.highlights) && artifact.parsed.highlights.length > 0
                  ? `${artifact.parsed.highlights[0].label} spread ${Number(artifact.parsed.highlights[0].spread || 0).toFixed(2)}`
                  : "Snapshot preserved for later review."}
              </div>
              <div className="flex items-center justify-between gap-2">
                <button
                  onClick={() => deleteSnapshot(artifact.artifact_id)}
                  className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-crimson inline-flex items-center gap-1"
                >
                  <Trash2 size={10} /> Delete
                </button>
                <button
                  onClick={() => navigate(`/evaluation/${rfpId}/consensus`)}
                  className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-ink inline-flex items-center gap-1"
                >
                  Consensus view <ChevronRight size={10} />
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))] gap-4 mb-6">
        {[topVendorDetails, runnerUpDetails].filter(Boolean).map((vendor, index) => (
          <div key={vendor.response_id} className={clsx(
            "min-w-0 rounded-2xl border p-5",
            index === 0 ? "border-emerald-200 bg-emerald-50/50" : "border-amber-200 bg-amber-50/40",
          )}>
            <div className="flex items-start justify-between gap-3 mb-2">
              <div className="min-w-0">
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">
                  {index === 0 ? "Current leader" : "Runner-up"}
                </div>
                <div className="font-heading text-2xl text-ink break-words">{vendor.name}</div>
              </div>
              <button
                onClick={() => navigate(`/evaluation/${rfpId}/vendor/${vendor.response_id}`)}
                className="btn-ghost shrink-0 text-xs py-1.5 px-3"
              >
                Details
              </button>
            </div>
            <div className="text-sm text-ink-muted leading-relaxed">
              {index === 0
                ? "Use this as the current baseline for chat prompts, snapshot comparisons, and final recommendation framing."
                : "Use this vendor as the strongest alternative in side-by-side review and consensus discussion."}
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(240px,1fr))] gap-3 mb-6">
        {compareHighlights.map((highlight) => (
          <div key={highlight.label} className="min-w-0 rounded-xl border border-steel bg-white p-3">
            <div className="flex flex-col gap-2 mb-2 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0 font-medium text-ink text-sm leading-snug break-words">{highlight.label}</div>
              <div className={clsx(
                "w-fit shrink-0 text-[10px] font-mono border px-2 py-0.5 rounded-full",
                highlight.spread > 1 ? "bg-crimson/5 text-crimson border-crimson/20" : "bg-ivory text-ink-muted border-steel",
              )}>
                Spread {highlight.spread.toFixed(2)}
              </div>
            </div>
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Scores</div>
            <div className="text-xs text-ink-muted leading-relaxed">
              {vendorNames.map((vendorName) => (
                <div key={`${highlight.label}-${vendorName}`} className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate">{vendorName}</span>
                  <span className="shrink-0 font-mono">{Number(highlight.scores?.[vendorName] || 0).toFixed(2)}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="card p-5 border border-steel/70">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
            <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Decision context</div>
            <div className="font-heading text-lg text-ink">Current comparison table</div>
          </div>
          <button onClick={() => navigate(`/evaluation/${rfpId}/consensus`)} className="btn-ghost flex items-center gap-2 text-sm">
            <ChevronRight size={14} /> Go to consensus
          </button>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm font-body">
            <thead>
              <tr className="border-b border-steel bg-ivory">
                <th className="text-left text-xs font-medium text-ink-muted px-4 py-3 uppercase tracking-wider">Criterion</th>
                <th className="text-left text-xs font-medium text-ink-muted px-4 py-3 uppercase tracking-wider">Weight</th>
                {vendorNames.map((vendor) => (
                  <th key={vendor} className="text-left text-xs font-medium text-ink-muted px-4 py-3 uppercase tracking-wider">{vendor}</th>
                ))}
                <th className="text-left text-xs font-medium text-ink-muted px-4 py-3 uppercase tracking-wider">Spread</th>
              </tr>
            </thead>
            <tbody>
              {(compare?.criteria || []).map((criterion, index) => {
                const scores = vendorNames.map((vendor) => Number(criterion.scores?.[vendor] || 0));
                const spread = scores.length ? Math.max(...scores) - Math.min(...scores) : 0;
                return (
                  <tr key={criterion.label} className={clsx("border-b border-steel/50", index % 2 === 0 ? "bg-white" : "bg-ivory") }>
                    <td className="px-4 py-3 font-medium text-ink">{criterion.label}</td>
                    <td className="px-4 py-3 text-xs font-mono text-ink-muted">{Math.round(criterion.weight > 1 ? criterion.weight : (criterion.weight || 0) * 100)}%</td>
                    {vendorNames.map((vendor) => (
                      <td key={`${criterion.label}-${vendor}`} className="px-4 py-3 font-mono text-sm text-ink">{formatScore(criterion.scores?.[vendor] || 0)}</td>
                    ))}
                    <td className={clsx("px-4 py-3 font-mono text-sm", spread > 1 ? "text-crimson" : spread > 0.5 ? "text-amber-600" : "text-emerald-600")}>{formatScore(spread)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {!compare?.vendors?.length && (
        <div className="mt-6 text-center text-ink-muted text-sm">
          No vendors are available for comparison yet.
        </div>
      )}
    </div>
  );
}
