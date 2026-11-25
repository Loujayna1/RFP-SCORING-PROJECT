import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useAuth } from "../context/useAuth";
import {
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  Radar,
  ResponsiveContainer,
  Tooltip,
} from "recharts";
import {
  MessageSquare,
  Download,
  ChevronRight,
  AlertTriangle,
  CheckCircle,
  MinusCircle,
  HelpCircle,
  Sliders,
  Eye,
  CheckCircle2,
  Trash2,
  Users,
  Lock,
  Unlock,
  ClipboardCheck,
  Save,
  FileText,
  FileSpreadsheet,
  X,
} from "lucide-react";
import { rfpApi } from "../api/client";
import MatrixRequirementsSection from "../components/MatrixRequirementsSection";
import DocumentRequirementsSection from "../components/DocumentRequirementsSection";
import DocumentViewerModal from "../components/DocumentViewerModal";
import clsx from "clsx";


const ACTIVE_REPORT_STATUSES = new Set(["queued", "pending", "processing"]);

const COMPLIANCE_COLORS = {
  FullyCompliant: {
    cls: "compliance-full",
    icon: CheckCircle,
    label: "Fully compliant",
  },
  PartiallyCompliant: {
    cls: "compliance-partial",
    icon: MinusCircle,
    label: "Partially compliant",
  },
  NonCompliant: {
    cls: "compliance-none",
    icon: AlertTriangle,
    label: "Non-compliant",
  },
  NotAddressed: {
    cls: "compliance-na",
    icon: HelpCircle,
    label: "Not addressed",
  },
};

function ScoreBar({ score, max = 5 }) {
  const safeScore = Number.isFinite(Number(score)) ? Number(score) : 0;
  const pct = Math.round((safeScore / max) * 100);
  const color =
    safeScore >= 4 ? "bg-emerald-500" : safeScore >= 3 ? "bg-amber-400" : "bg-crimson";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-steel-light rounded-full overflow-hidden">
        <div
          className={clsx(
            "h-full rounded-full transition-all duration-700",
            color,
          )}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="font-mono text-xs text-ink w-6 text-right">
        {safeScore.toFixed(1)}
      </span>
    </div>
  );
}

// Effective weight (share of the total) when the API provides it; parents
// are shown as subtotal headings, leaves carry the scores.
const effWeight = (c) => Number(c?.effective_weight ?? c?.weight ?? 0);
const isLeaf = (c) => c?.is_leaf !== false;
const pct = (w) => `${(w * 100).toFixed(w * 100 < 10 && w > 0 ? 1 : 0)}%`;

function VendorCard({
  name,
  total,
  rank,
  criteria,
  responseId,
  navigate,
  rfpId,
  onDelete,
  status,
  onAskAI,
}) {
  const isFirst = rank === 1;
  const safeTotal = Number.isFinite(Number(total)) ? Number(total) : 0;
  return (
    <div
      className={clsx(
        "card p-5 relative overflow-hidden transition-all hover:shadow-md",
        isFirst && "ring-2 ring-crimson",
      )}
    >
      {isFirst && (
        <div className="absolute top-0 right-0 bg-crimson text-white text-[10px] font-mono px-2 py-0.5 rounded-bl-lg">
          RANK #1
        </div>
      )}
      <div className="flex items-start justify-between mb-4">
        <div>
          <div className="flex items-center gap-2">
            <div className="font-heading text-lg text-ink">{name}</div>
            {status === "error" && (
              <span className="bg-crimson/10 text-crimson text-[9px] font-mono px-1.5 py-0.5 rounded border border-crimson/20 uppercase tracking-tighter">
                Error
              </span>
            )}
            {status === "ingesting" && (
              <span className="bg-blue-50 text-blue-600 text-[9px] font-mono px-1.5 py-0.5 rounded border border-blue-200 uppercase tracking-tighter animate-pulse">
                Processing
              </span>
            )}
          </div>
          <div className="text-xs text-ink-muted font-mono">Rank #{rank}</div>
        </div>
        <div className="text-right">
          <div className="font-display text-4xl text-ink leading-none">
            {safeTotal.toFixed(2)}
          </div>
          <div className="text-xs text-ink-muted">/ 5.00</div>
        </div>
      </div>
      {criteria.map((c) => (
        <div
          key={c.criterion_id || c.label}
          className={clsx("mb-2", !isLeaf(c) && "mt-3")}
          style={{ paddingLeft: `${(c.depth || 0) * 12}px` }}
        >
          <div className={clsx("flex justify-between text-xs mb-0.5", isLeaf(c) ? "text-ink-muted" : "text-ink font-semibold")}>
            <span>{c.label}{!isLeaf(c) && " (subtotal)"}</span>
            <span className="font-mono" title="Share of the total score">{pct(effWeight(c))}</span>
          </div>
          <ScoreBar score={c.scores?.[name]} />
        </div>
      ))}
      <div className="flex gap-2 mt-4 pt-4 border-t border-steel">
        <button
          onClick={() => navigate(`/evaluation/${rfpId}/vendor/${responseId}`)}
          disabled={status !== "ready"}
          className="btn-ghost text-xs py-1.5 px-3 flex-1 flex items-center justify-center gap-1 disabled:opacity-30"
        >
          <ChevronRight size={12} /> Details
        </button>
        <button
          onClick={() => onAskAI(name)}
          disabled={status !== "ready"}
          className="btn-primary text-xs py-1.5 px-3 flex-1 flex items-center justify-center gap-1 disabled:opacity-30"
        >
          <MessageSquare size={12} /> Ask AI
        </button>
        <button
          onClick={() => onDelete(responseId, name)}
          className="btn-ghost text-xs py-1.5 px-3 hover:text-crimson flex items-center justify-center gap-1"
          title="Delete Vendor"
        >
          <Trash2 size={12} />
        </button>
      </div>
    </div>
  );
}

export default function EvaluationDetail() {
  const { rfpId } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const [rfp, setRfp] = useState(null);
  const [compare, setCompare] = useState(null);
  const [activeTab, setActiveTab] = useState("evaluation");
  const [requirementsLoading, setRequirementsLoading] = useState(false);
  const [requirementsError, setRequirementsError] = useState(null);
  const [matrixSections, setMatrixSections] = useState([]);
  const [matrixTotal, setMatrixTotal] = useState(0);
  const [textualReqs, setTextualReqs] = useState([]);
  const [textualTotal, setTextualTotal] = useState(0);
  const [textualPage, setTextualPage] = useState(0);
  const [requirementsRefetchTick, setRequirementsRefetchTick] = useState(0);
  const [sourceModal, setSourceModal] = useState(null); // { doc_id, page, text, filename }
  const [deleteVendorTarget, setDeleteVendorTarget] = useState(null); // { responseId, name }
  const [manageModalOpen, setManageModalOpen] = useState(false);
  const [reportQueue, setReportQueue] = useState([]);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportMessage, setReportMessage] = useState("");
  const [compareArtifacts, setCompareArtifacts] = useState([]);
  const [compareFilter, setCompareFilter] = useState("");

  const pageSize = 50;
  const compareVendorIds = compare?.vendors || [];
  const canAuditPacket = user?.role === "admin";
  const canVendorDebriefs = user?.role === "admin";

  // Download via the authenticated axios client (blob), then trigger a browser
  // save. A plain <a href> navigation does not carry the in-memory bearer token
  // and the backend returns 401.
  async function handleReportDownload(item) {
    if (!item.reportId) return;
    try {
      const res = await rfpApi.reportDownload(item.reportId);
      const blob = new Blob([res.data], {
        type: res.headers?.["content-type"] || "application/octet-stream",
      });
      // Prefer the server's filename, else derive one from the label.
      let filename = `${(item.label || "report").toLowerCase().replace(/[^\w.-]+/g, "-")}`;
      const cd = res.headers?.["content-disposition"];
      const match = cd && /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd);
      if (match) filename = decodeURIComponent(match[1].replace(/"/g, ""));
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setReportMessage(
        e?.response?.status === 401
          ? "Your session expired — please log in again, then retry the download."
          : "Download failed. The report may have expired; try regenerating it.",
      );
    }
  }

  async function queueReport(label, requestPromise) {
    const response = await requestPromise;
    setReportQueue((prev) => [
      {
        label,
        reportId: response.data?.report_id,
        downloadUrl: response.data?.download_url,
        statusUrl: response.data?.status_url,
        status: response.data?.status || "queued",
      },
      ...prev,
    ]);
    return response;
  }

  async function saveCompareSnapshot() {
    if (!compare?.ranking?.length) return;
    const snapshot = {
      type: "evaluation_compare",
      rfpId,
      savedAt: new Date().toISOString(),
      ranking: compare.ranking,
      vendors: compare.vendors,
      topVendor: compare.ranking[0] || compare.vendors[0] || null,
      runnerUp: compare.ranking[1] || compare.vendors[1] || null,
      highlights: (compare?.criteria || []).map((criterion) => {
        const scores = (compare?.vendors || []).map((vendorName) => Number(criterion?.scores?.[vendorName] || 0));
        return {
          label: criterion.label,
          spread: scores.length ? Math.max(...scores) - Math.min(...scores) : 0,
          scores: criterion.scores || {},
        };
      }).sort((a, b) => b.spread - a.spread).slice(0, 4),
    };

    try {
      const response = await rfpApi.createChatArtifact(rfpId, {
        artifact_type: "compare",
        content: JSON.stringify(snapshot),
      });
      setCompareArtifacts((prev) => [response.data, ...prev].slice(0, 10));
      setReportMessage("Compare snapshot saved.");
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to save compare snapshot.");
    }
  }

  async function deleteCompareSnapshot(artifactId) {
    if (!window.confirm("Delete this saved compare snapshot?")) return;
    try {
      await rfpApi.deleteChatArtifact(rfpId, artifactId);
      setCompareArtifacts((prev) => prev.filter((item) => item.artifact_id !== artifactId));
      setReportMessage("Compare snapshot deleted.");
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to delete compare snapshot.");
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

  async function handleExecutiveSummary() {
    setReportBusy(true);
    setReportMessage("");
    try {
      await queueReport(
        "Executive summary",
        rfpApi.genReport(rfpId, {
        report_type: "comparison",
        format: "pdf",
        vendor_ids: compareVendorIds,
        }),
      );
      setReportMessage("Executive summary queued.");
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to queue executive summary.");
    } finally {
      setReportBusy(false);
    }
  }

  async function handleComparisonTables() {
    setReportBusy(true);
    setReportMessage("");
    try {
      await queueReport(
        "Comparison tables",
        rfpApi.genReport(rfpId, {
          report_type: "comparison",
          format: "pdf",
          vendor_ids: compareVendorIds,
        }),
      );
      setReportMessage("Comparison tables queued.");
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to queue comparison tables.");
    } finally {
      setReportBusy(false);
    }
  }

  async function handleVendorDebriefs() {
    setReportBusy(true);
    setReportMessage("");
    try {
      const response = await rfpApi.genVendorDebriefs(rfpId);
      const queued = Array.isArray(response.data?.queued) ? response.data.queued : [];
      setReportQueue((prev) => [
        ...queued.map((item) => ({
          label: `Vendor debrief: ${item.vendor_name}`,
          reportId: item.report_id,
          downloadUrl: item.download_url,
          statusUrl: null,
          status: "queued",
        })),
        ...prev,
      ]);
      setReportMessage(`${queued.length} vendor debrief report(s) queued.`);
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to queue vendor debriefs.");
    } finally {
      setReportBusy(false);
    }
  }

  async function handleAuditPacket() {
    setReportBusy(true);
    setReportMessage("");
    try {
      const summary = await rfpApi.genReport(rfpId, {
          report_type: "comparison",
          format: "pdf",
          vendor_ids: compareVendorIds,
      });

      const auditCsv = await rfpApi.genAuditCsv(rfpId);
      const bundle = [
        {
          label: "Audit-ready packet - audit CSV",
          reportId: auditCsv.data?.report_id,
          downloadUrl: auditCsv.data?.download_url,
          statusUrl: auditCsv.data?.status_url,
          status: auditCsv.data?.status || "queued",
        },
        {
          label: "Audit-ready packet - executive summary",
          reportId: summary.data?.report_id,
          downloadUrl: summary.data?.download_url,
          statusUrl: summary.data?.status_url,
          status: summary.data?.status || "queued",
        },
      ];

      if (rfp?.status === "locked") {
        const cert = await rfpApi.genComplianceCert(rfpId);
        bundle.unshift({
          label: "Audit-ready packet - compliance certificate",
          reportId: cert.data?.report_id,
          downloadUrl: cert.data?.download_url,
          statusUrl: cert.data?.status_url,
          status: cert.data?.status || "queued",
        });
      }

      setReportQueue((prev) => [...bundle, ...prev]);
      setReportMessage("Audit-ready packet queued.");
    } catch (e) {
      setReportMessage(e?.response?.data?.detail || "Failed to queue audit-ready packet.");
    } finally {
      setReportBusy(false);
    }
  }

  useEffect(() => {
    const hasActive = reportQueue.some(
      (item) => item.reportId && ACTIVE_REPORT_STATUSES.has(item.status),
    );
    if (!hasActive) return;

    const timer = setTimeout(async () => {
      const updated = await Promise.all(
        reportQueue.map(async (item) => {
          if (!item.reportId || !ACTIVE_REPORT_STATUSES.has(item.status)) return item;
          try {
            const r = await rfpApi.reportStatus(item.reportId);
            return { ...item, status: r.data.status, errorMessage: r.data.error_message || null };
          } catch {
            return item;
          }
        }),
      );
      setReportQueue(updated);
    }, 3000);

    return () => clearTimeout(timer);
  }, [reportQueue]);

  function openChatWithPrompt(prompt, autoSend = true) {
    window.dispatchEvent(new CustomEvent("open-chat", { detail: { prompt, autoSend } }));
  }

  function askVendorQuestion(vendorName) {
    openChatWithPrompt(
      `Compare ${vendorName} against the other vendors and summarize strengths, gaps, and decision risks.`,
      true,
    );
  }

  useEffect(() => {
    rfpApi
      .get(rfpId)
      .then((r) => setRfp(r.data))
      .catch(() => {});
    rfpApi
      .compare(rfpId)
      .then((r) =>
        setCompare({
          vendors: Array.isArray(r.data?.vendors) ? r.data.vendors : [],
          vendorDetails: Array.isArray(r.data?.vendor_details) ? r.data.vendor_details : [],
          criteria: Array.isArray(r.data?.criteria) ? r.data.criteria : [],
          totals: r.data?.totals && typeof r.data.totals === "object" ? r.data.totals : {},
          ranking: Array.isArray(r.data?.ranking) ? r.data.ranking : [],
        }),
      )
      .catch(() => {});

    rfpApi.chatArtifacts(rfpId)
      .then((r) => {
        const compares = Array.isArray(r.data?.compares) ? r.data.compares : [];
        setCompareArtifacts(compares);
      })
      .catch(() => {});
  }, [rfpId]);

  useEffect(() => {
    if (rfp && compare && !compare.vendors.length && (rfp.requirement_count || 0) > 0) {
      setActiveTab("requirements");
    }
  }, [rfp, compare]);

  // Dataset A — matrix (all sections, no pagination)
  useEffect(() => {
    if (activeTab !== "requirements") return;
    setRequirementsLoading(true);
    setRequirementsError(null);
    rfpApi
      .matrixRequirements(rfpId)
      .then((mr) => {
        setMatrixSections(mr.data?.sections || []);
        setMatrixTotal(Number(mr.data?.total) || 0);
      })
      .catch((err) => {
        console.error("Failed to fetch matrix requirements:", err);
        setRequirementsError(
          "Failed to load requirements. Please check your connection or try again later.",
        );
        setMatrixSections([]);
        setMatrixTotal(0);
      })
      .finally(() => setRequirementsLoading(false));
  }, [activeTab, rfpId, requirementsRefetchTick]);

  // Dataset B — document / LLM (paginated)
  useEffect(() => {
    if (activeTab !== "requirements") return;
    rfpApi
      .documentRequirements(rfpId, textualPage * pageSize, pageSize)
      .then((dr) => {
        setTextualReqs(dr.data?.requirements || []);
        setTextualTotal(Number(dr.data?.total) || 0);
      })
      .catch(() => {
        setTextualReqs([]);
        setTextualTotal(0);
      });
  }, [activeTab, rfpId, textualPage, requirementsRefetchTick]);

  // Reset requirements pagination when RFP changes
  useEffect(() => {
    setMatrixSections([]);
    setTextualReqs([]);
    setTextualPage(0);
    setMatrixTotal(0);
    setTextualTotal(0);
    setRequirementsRefetchTick(0);
  }, [rfpId]);

  const fetchData = () => {
    rfpApi
      .get(rfpId)
      .then((r) => setRfp(r.data))
      .catch(() => {});
    rfpApi
      .compare(rfpId)
      .then((r) =>
        setCompare({
          vendors: Array.isArray(r.data?.vendors) ? r.data.vendors : [],
          vendorDetails: Array.isArray(r.data?.vendor_details) ? r.data.vendor_details : [],
          criteria: Array.isArray(r.data?.criteria) ? r.data.criteria : [],
          totals: r.data?.totals && typeof r.data.totals === "object" ? r.data.totals : {},
          ranking: Array.isArray(r.data?.ranking) ? r.data.ranking : [],
        }),
      )
      .catch(() => {});
  };

  const handleDeleteVendor = async () => {
    if (!deleteVendorTarget) return;
    try {
      await rfpApi.deleteVendor(rfpId, deleteVendorTarget.responseId);
      setDeleteVendorTarget(null);
      fetchData(); // Refresh list
    } catch (err) {
      console.error(err);
      alert("Failed to delete vendor evaluation.");
    }
  };

  const radarData = (compare?.criteria || []).filter(isLeaf).map((c) => ({
    section: (c?.label || "").slice(0, 5),
    ...Object.fromEntries((compare?.vendors || []).map((v) => [v, c?.scores?.[v] || 0])),
  }));

  const topVendorName = compare?.ranking?.[0] || compare?.vendors?.[0] || null;
  const runnerUpName = compare?.ranking?.[1] || compare?.vendors?.[1] || null;
  const topVendorDetails = compare?.vendorDetails?.find((v) => v.name === topVendorName);
  const runnerUpDetails = compare?.vendorDetails?.find((v) => v.name === runnerUpName);

  const compareHighlights = [...(compare?.criteria || [])]
    .filter(isLeaf)
    .map((criterion) => {
      const scores = (compare?.vendors || []).map((v) => Number(criterion?.scores?.[v] || 0));
      const sorted = [...(compare?.vendors || [])].sort((a, b) => Number(criterion?.scores?.[b] || 0) - Number(criterion?.scores?.[a] || 0));
      return {
        label: criterion.label,
        weight: effWeight(criterion),
        spread: scores.length ? Math.max(...scores) - Math.min(...scores) : 0,
        leader: sorted[0],
        runnerUp: sorted[1] || null,
        scoreMap: criterion.scores || {},
      };
    })
    .sort((a, b) => b.spread - a.spread)
    .slice(0, 4);

  const VENDOR_COLORS = ["#ED1C24", "#2A1A1D"];

  if (!rfp || !compare) {
    return (
      <div className="flex flex-col items-center justify-center min-h-[400px] text-ink-muted gap-3">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-crimson"></div>
        <div className="text-sm font-mono tracking-widest uppercase">Loading evaluation...</div>
      </div>
    );
  }

  return (
    <div className="p-8">
      {/* Source Viewer Modal */}
      <DocumentViewerModal
        open={!!sourceModal}
        onClose={() => setSourceModal(null)}
        docId={sourceModal?.doc_id}
        filename={sourceModal?.filename}
        page={sourceModal?.page}
        sheet={sourceModal?.sheet}
        row={sourceModal?.row}
        evidenceText={sourceModal?.text}
      />

      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            EVALUATION
          </div>
          <div className="font-heading text-lg text-ink-muted mt-1 italic">
            {rfp?.title}
          </div>
          <div className="text-xs text-ink-muted font-mono mt-0.5">
            {rfp?.requirement_count} requirements
          </div>
        </div>
        <div className="flex gap-3">
          {/* FIX 3 — rubric approval button: visible to admin when rubric not yet locked,
              or shows locked state. Directs to the rubric approval page. */}
          {user?.role === "admin" && (
            <button
              onClick={() => navigate(`/evaluation/${rfpId}/rubric`)}
              className={
                rfp?.rubric_locked
                  ? "btn-ghost flex items-center gap-2 text-sm text-emerald-700 border-emerald-200 bg-emerald-50"
                  : "btn-ghost flex items-center gap-2 text-sm text-amber-700 border-amber-300 bg-amber-50"
              }
            >
              {rfp?.rubric_locked
                ? <><Lock size={14} /> Rubric locked</>
                : <><Unlock size={14} /> Review rubric</>
              }
            </button>
          )}
          <button
            onClick={() => navigate(`/score-editor/${rfpId}`)}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <Sliders size={15} /> Edit scores
          </button>
          <button
            onClick={() => setManageModalOpen(true)}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <Users size={15} /> Manage vendors
          </button>
          <button
            onClick={() => openChatWithPrompt(
              "Summarize the key decision trade-offs for this RFP and call out the strongest vendor.",
              true,
            )}
            className="btn-ghost flex items-center gap-2 text-sm"
          >
            <MessageSquare size={15} /> Chat with AI
          </button>
        </div>
      </div>

      <div className="card p-5 mb-8 border border-steel/70">
        <div className="flex items-start justify-between gap-4 mb-4">
          <div>
          <button
            onClick={() => openChatWithPrompt(
              "Build an executive summary of the current evaluation, highlighting ranking, risks, and unresolved questions.",
              true,
            )}
            disabled={reportBusy}
            className="btn-ghost flex items-center justify-center gap-2 text-sm"
          >
            <MessageSquare size={14} /> Ask AI for summary
          </button>
            <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Reports & export</div>
            <div className="font-heading text-lg text-ink">One-click export hub</div>
          </div>
          <div className="text-xs font-mono text-ink-muted text-right">
            {reportBusy ? "Queuing reports..." : "Ready to export"}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <button
            onClick={handleExecutiveSummary}
            disabled={reportBusy || !compareVendorIds.length}
            className="btn-primary flex items-center justify-center gap-2 text-sm disabled:opacity-50"
          >
            <FileText size={14} /> Executive summary
          </button>
          <button
            onClick={handleComparisonTables}
            disabled={reportBusy || !compareVendorIds.length}
            className="btn-ghost flex items-center justify-center gap-2 text-sm disabled:opacity-50"
          >
            <FileSpreadsheet size={14} /> Comparison tables
          </button>
          <button
            onClick={handleVendorDebriefs}
            disabled={reportBusy || !canVendorDebriefs || !compareVendorIds.length}
            className="btn-ghost flex items-center justify-center gap-2 text-sm disabled:opacity-50"
            title={!canVendorDebriefs ? "Admin only" : "Generate vendor debrief PDFs for all vendors"}
          >
            <Users size={14} /> Vendor debriefs
          </button>
          <button
            onClick={handleAuditPacket}
            disabled={reportBusy || !canAuditPacket || !compareVendorIds.length}
            className="btn-ghost flex items-center justify-center gap-2 text-sm disabled:opacity-50"
            title={!canAuditPacket ? "Admin only" : rfp?.status === "locked" ? "Generate audit-ready packet (with compliance certificate)" : "Generate audit-ready packet (lock RFP first for compliance certificate)"}
          >
            <ClipboardCheck size={14} /> Audit-ready packet
          </button>
        </div>

        <div className="mt-3 text-[11px] text-ink-muted leading-relaxed">
          Executive summary and comparison tables use the existing comparison report template. Vendor debriefs and audit-ready packets are only available to admins, and audit-ready packets include the audit CSV plus compliance certificate when the RFP is locked.
        </div>

        {reportMessage && (
          <div className="mt-3 text-sm text-ink bg-ivory border border-steel/60 rounded-xl px-4 py-3">
            {reportMessage}
          </div>
        )}

        {reportQueue.length > 0 && (
          <div className="mt-4 space-y-2">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">Queued exports</div>
            {reportQueue.slice(0, 5).map((item) => (
              <div key={`${item.label}-${item.reportId}`} className="rounded-lg border border-steel bg-white px-3 py-2 space-y-1">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm text-ink truncate">{item.label}</div>
                    <div className="flex items-center gap-1.5 mt-0.5">
                      <span
                        className={clsx("text-[10px] font-mono", {
                          "text-green-600": item.status === "completed",
                          "text-red-500":   item.status === "failed",
                          "text-yellow-600": ACTIVE_REPORT_STATUSES.has(item.status),
                        })}
                      >
                        {item.status}
                      </span>
                      {ACTIVE_REPORT_STATUSES.has(item.status) && (
                        <span className="text-[10px] text-ink-muted animate-pulse">processing…</span>
                      )}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    {item.status === "completed" && item.reportId && (
                      <button
                        type="button"
                        onClick={() => handleReportDownload(item)}
                        className="text-xs text-crimson hover:underline flex items-center gap-1"
                      >
                        <Download size={11} /> Download
                      </button>
                    )}
                  </div>
                </div>
                {item.status === "failed" && item.errorMessage && (
                  <div className="text-[10px] text-red-500 font-mono truncate">{item.errorMessage}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-4 mb-6 border-b border-steel">
        <button
          onClick={() => setActiveTab("evaluation")}
          className={clsx(
            "py-3 px-4 font-medium text-sm border-b-2 transition-all",
            activeTab === "evaluation"
              ? "text-ink border-crimson"
              : "text-ink-muted border-transparent hover:text-ink"
          )}
        >
          <div className="flex items-center gap-2">
            <Sliders size={14} />
            Evaluation
          </div>
        </button>
        <button
          onClick={() => setActiveTab("requirements")}
          className={clsx(
            "py-3 px-4 font-medium text-sm border-b-2 transition-all",
            activeTab === "requirements"
              ? "text-ink border-crimson"
              : "text-ink-muted border-transparent hover:text-ink"
          )}
        >
          <div className="flex items-center gap-2">
            <FileText size={14} />
            Requirements ({rfp?.requirement_count || 0})
          </div>
        </button>
      </div>

      {/* Evaluation Tab */}
      {activeTab === "evaluation" && (
        <>
          {!compare?.vendors?.length ? (
            <div className="card p-12 text-center">
              <div className="flex justify-center mb-4">
                <div className="w-16 h-16 bg-steel-light rounded-full flex items-center justify-center text-ink-muted">
                  <AlertTriangle size={32} />
                </div>
              </div>
              <div className="font-heading text-xl text-ink mb-2">No vendors evaluated yet</div>
              <div className="text-sm text-ink-muted mb-6 max-w-sm mx-auto">
                You have successfully indexed the RFP. Now you need to upload a vendor response 
                to see evaluation scores and compliance comparison.
              </div>
              <button 
                onClick={() => navigate(`/evaluation/${rfpId}/vendor-upload`)}
                className="btn-primary"
              >
                Go to Vendor Upload
              </button>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-1 gap-5 mb-6 xl:grid-cols-3">
                {/* Radar */}
                <div className="card p-5 xl:col-span-1">
                  <div className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-4">
                    Coverage radar
                  </div>
                  <ResponsiveContainer width="100%" height={220}>
                    <RadarChart data={radarData}>
                      <PolarGrid stroke="#D8CED0" />
                      <PolarAngleAxis
                        dataKey="section"
                        tick={{ fontSize: 10, fontFamily: "DM Sans", fill: "#6B4B51" }}
                      />
                      {(compare?.vendors || []).map((v, i) => (
                        <Radar
                          key={v}
                          name={v}
                          dataKey={v}
                          stroke={VENDOR_COLORS[i]}
                          fill={VENDOR_COLORS[i]}
                          fillOpacity={0.1}
                          strokeWidth={1.5}
                        />
                      ))}
                      <Tooltip
                        contentStyle={{
                          fontSize: 11,
                          fontFamily: "DM Sans",
                          borderColor: "#D8CED0",
                          borderRadius: 8,
                        }}
                      />
                    </RadarChart>
                  </ResponsiveContainer>
                  <div className="flex gap-3 justify-center mt-1">
                    {(compare?.vendors || []).map((v, i) => (
                      <div key={v} className="flex items-center gap-1.5">
                        <div
                          className="w-3 h-0.5 rounded-full"
                          style={{ background: VENDOR_COLORS[i] }}
                        />
                        <span className="text-xs text-ink-muted font-body">{v}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {topVendorName && runnerUpName && (
                  <div className="card p-5 border border-steel/70 overflow-hidden xl:col-span-2">
                    <div className="flex flex-col gap-4 mb-4 xl:flex-row xl:items-start xl:justify-between">
                      <div className="min-w-0">
                        <div className="text-xs font-mono uppercase tracking-widest text-ink-muted mb-1">Compare workflow</div>
                        <div className="font-heading text-base sm:text-lg text-ink leading-tight">Top-vendor decision view</div>
                      </div>
                      <div className="flex flex-wrap gap-2 sm:gap-3 xl:justify-end">
                        <button
                          onClick={() => openChatWithPrompt(
                            `Compare ${topVendorName} vs ${runnerUpName} on strengths, risks, and likely winner.`,
                            true,
                          )}
                          className="btn-ghost inline-flex min-w-[150px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none"
                        >
                          <MessageSquare size={14} className="shrink-0" /> <span>Compare in chat</span>
                        </button>
                        <button
                          onClick={() => navigate(`/evaluation/${rfpId}/compare`)}
                          className="btn-ghost inline-flex min-w-[150px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none"
                        >
                          <ChevronRight size={14} className="shrink-0" /> <span>Open workspace</span>
                        </button>
                        <button
                          onClick={saveCompareSnapshot}
                          className="btn-ghost inline-flex min-w-[140px] flex-1 items-center justify-center gap-2 px-3 text-sm sm:flex-none"
                        >
                          <Save size={14} className="shrink-0" /> <span>Save compare</span>
                        </button>
                      </div>
                    </div>

                    <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-4 mb-4">
                      {[topVendorDetails, runnerUpDetails].filter(Boolean).map((vendor, index) => (
                        <div key={vendor.response_id} className={clsx(
                          "min-w-0 rounded-2xl border p-4",
                          index === 0 ? "border-emerald-200 bg-emerald-50/50" : "border-amber-200 bg-amber-50/40",
                        )}>
                          <div className="flex items-start justify-between gap-3 mb-2">
                            <div className="min-w-0">
                              <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">
                                {index === 0 ? "Current leader" : "Runner-up"}
                              </div>
                              <div className="font-heading text-xl text-ink break-words">{vendor.name}</div>
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
                              ? "Use this as the current baseline for the decision conversation."
                              : "Use this vendor as the strongest alternative in the side-by-side review."}
                          </div>
                        </div>
                      ))}
                    </div>

                    <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
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
                          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Leader</div>
                          <div className="text-sm text-ink mb-2">{highlight.leader || "—"}</div>
                          <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Scores</div>
                          <div className="text-xs text-ink-muted leading-relaxed">
                            {(compare?.vendors || []).map((vendorName) => (
                              <div key={`${highlight.label}-${vendorName}`} className="flex items-center justify-between gap-3">
                                <span className="min-w-0 truncate">{vendorName}</span>
                                <span className="shrink-0 font-mono">{Number(highlight.scoreMap?.[vendorName] || 0).toFixed(2)}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>

                    {compareArtifacts.length > 0 && (
                      <div className="mt-4 pt-4 border-t border-steel/60">
                        <div className="flex flex-col gap-3 mb-3 lg:flex-row lg:items-end lg:justify-between">
                          <div>
                            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">Saved compare snapshots</div>
                            <div className="font-heading text-base text-ink">Search and reopen prior decision frames</div>
                          </div>
                          <div className="flex items-center gap-3">
                            <input
                              value={compareFilter}
                              onChange={(e) => setCompareFilter(e.target.value)}
                              placeholder="Filter by vendor or criterion"
                              className="min-w-[240px] border border-steel rounded-lg px-3 py-2 text-sm bg-white"
                            />
                            <div className="text-[10px] text-ink-muted font-mono">{visibleCompareArtifacts.length}/{compareArtifacts.length}</div>
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
                                      {parsed?.runnerUp ? `vs ${parsed.runnerUp}` : artifact.saved_at || "Recently"}
                                    </div>
                                  </div>
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
                  </div>
                )}

                {/* Vendor cards */}
                <div className="grid grid-cols-[repeat(auto-fit,minmax(280px,1fr))] gap-4 xl:col-span-3">
                  {(compare?.vendors || []).map((v, i) => (
                    <VendorCard
                      key={v}
                      name={v}
                      total={compare?.totals?.[v]}
                      rank={(compare?.ranking || []).indexOf(v) + 1}
                      criteria={compare?.criteria || []}
                      navigate={navigate}
                      rfpId={rfpId}
                      responseId={
                        compare.vendorDetails?.find(vd => vd.name === v)?.response_id
                      }
                      status={
                        compare.vendorDetails?.find(vd => vd.name === v)?.status
                      }
                      onDelete={(id, name) => setDeleteVendorTarget({ responseId: id, name })}
                      onAskAI={askVendorQuestion}
                    />
                  ))}
                </div>
              </div>

              {/* Section comparison table */}
              <div className="card overflow-hidden">
                <div className="px-6 py-4 border-b border-steel">
                  <span className="font-heading text-base text-ink">
                    Section breakdown
                  </span>
                </div>
                <table className="w-full text-sm font-body">
                  <thead>
                    <tr className="border-b border-steel bg-ivory">
                      <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                        Section
                      </th>
                      <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                        Weight
                      </th>
                      {(compare?.vendors || []).map((v) => (
                        <th
                          key={v}
                          className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider"
                        >
                          {v}
                        </th>
                      ))}
                      <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                        Delta
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {(compare?.criteria || []).map((c, i) => {
                      const scores = (compare?.vendors || [])
                        .map((v) => c.scores?.[v])
                        .filter((x) => Number.isFinite(Number(x)) && x !== null);
                      const delta = scores.length > 1
                        ? (Math.max(...scores) - Math.min(...scores)).toFixed(2)
                        : "—";
                      return (
                        <tr
                          key={c.criterion_id || c.label}
                          className={clsx(
                            "border-b border-steel/50",
                            !isLeaf(c) ? "bg-ivory-warm" : i % 2 === 0 ? "bg-white" : "bg-ivory",
                          )}
                        >
                          <td
                            className={clsx("px-6 py-3 text-ink", isLeaf(c) ? "font-medium" : "font-bold")}
                            style={{ paddingLeft: `${24 + (c.depth || 0) * 16}px` }}
                          >
                            {c.label}
                            {!isLeaf(c) && <span className="ml-2 text-[10px] font-mono text-ink-muted uppercase">subtotal</span>}
                          </td>
                          <td className="px-6 py-3 font-mono text-xs text-ink-muted" title="Share of the total score">
                            {pct(effWeight(c))}
                          </td>
                          {(compare?.vendors || []).map((v) => (
                            <td key={v} className="px-6 py-3">
                              <ScoreBar score={c.scores[v]} />
                            </td>
                          ))}
                          <td
                            className={clsx(
                              "px-6 py-3 font-mono text-xs",
                              parseFloat(delta) > 1
                                ? "text-crimson"
                                : parseFloat(delta) > 0.5
                                  ? "text-amber-600"
                                  : "text-emerald-600",
                            )}
                          >
                            {delta > 0 ? `+${delta}` : delta}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}

      {/* Delete Vendor Confirmation Modal */}
      {deleteVendorTarget && (
        <div className="fixed inset-0 z-[300] flex items-center justify-center bg-ink/60 backdrop-blur-sm px-4">
          <div className="w-full max-w-lg rounded-2xl bg-white p-6 shadow-2xl border border-steel animate-slide-up">
            <div className="text-xs uppercase tracking-widest text-ink-muted font-mono mb-2">
              Confirm Deletion
            </div>
            <div className="font-heading text-2xl text-ink">
              Delete evaluation for <span className="text-crimson">{deleteVendorTarget.name}</span>?
            </div>
            <div className="mt-3 text-sm text-ink-muted font-body leading-relaxed">
              This will permanently remove all scores, extracted claims, and processed documents for this vendor. This action cannot be undone.
            </div>
            <div className="mt-8 flex items-center justify-end gap-3">
              <button 
                className="btn-secondary text-sm" 
                onClick={() => setDeleteVendorTarget(null)}
              >
                Cancel
              </button>
              <button
                className="btn-primary bg-crimson hover:bg-crimson/90 text-sm flex items-center gap-2"
                onClick={handleDeleteVendor}
              >
                <Trash2 size={14} /> Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Manage Vendors Modal */}
      {manageModalOpen && (
        <div className="fixed inset-0 z-[300] flex items-center justify-center bg-ink/60 backdrop-blur-sm px-4">
          <div className="w-full max-w-2xl rounded-2xl bg-white p-8 shadow-2xl border border-steel animate-slide-up flex flex-col max-h-[80vh]">
            <div className="flex items-center justify-between mb-6">
              <div>
                <div className="text-xs uppercase tracking-widest text-ink-muted font-mono mb-1">
                  System Administration
                </div>
                <div className="font-display text-3xl text-ink">
                  MANAGE VENDORS
                </div>
              </div>
              <button onClick={() => setManageModalOpen(false)} className="p-2 hover:bg-ivory rounded-full">
                <X size={20} />
              </button>
            </div>
            
            <div className="flex-1 overflow-y-auto pr-2">
              {(!compare?.vendorDetails || compare.vendorDetails.length === 0) ? (
                <div className="py-12 text-center text-ink-muted italic">
                  No vendor responses found for this RFP.
                </div>
              ) : (
                <div className="space-y-3">
                  {compare.vendorDetails.map((v) => (
                    <div key={v.response_id} className="flex items-center justify-between p-4 bg-ivory rounded-xl border border-steel/50">
                      <div>
                        <div className="font-heading text-ink">{v.name}</div>
                        <div className="text-[10px] font-mono text-ink-muted uppercase">
                          ID: {v.response_id}
                        </div>
                      </div>
                      <button 
                        onClick={() => {
                          setDeleteVendorTarget({ responseId: v.response_id, name: v.name });
                          setManageModalOpen(false);
                        }}
                        className="p-2 hover:text-crimson transition-colors"
                        title="Delete Response"
                      >
                        <Trash2 size={18} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
            
            <div className="mt-8 pt-6 border-t border-steel flex justify-end">
              <button 
                onClick={() => navigate(`/evaluation/${rfpId}/vendor-upload`)}
                className="btn-primary"
              >
                Upload New Vendor
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Requirements Tab */}
      {activeTab === "requirements" && (
        <div>
          {requirementsLoading && (
            <div className="card p-12 text-center">
              <div className="inline-block">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-crimson"></div>
              </div>
              <p className="text-ink-muted text-sm mt-4">Loading requirements...</p>
            </div>
          )}
          
          {!requirementsLoading && requirementsError && (
            <div className="card p-12 text-center border-crimson/20 bg-red-50/10">
              <div className="flex justify-center mb-4 text-crimson">
                <AlertTriangle size={32} />
              </div>
              <div className="font-heading text-xl text-ink mb-2">Error loading requirements</div>
              <div className="text-sm text-ink-muted">{requirementsError}</div>
              <button
                type="button"
                onClick={() => {
                  setRequirementsError(null);
                  setRequirementsRefetchTick((t) => t + 1);
                }}
                className="btn-secondary mt-4"
              >
                Retry
              </button>
            </div>
          )}

          {!requirementsLoading && !requirementsError && matrixTotal === 0 && textualTotal === 0 && (
            <div className="card p-12 text-center">
              <div className="flex justify-center mb-4">
                <div className="w-16 h-16 bg-steel-light rounded-full flex items-center justify-center text-ink-muted">
                  <FileText size={32} />
                </div>
              </div>
              <div className="font-heading text-xl text-ink mb-2">No requirements found</div>
              <div className="text-sm text-ink-muted">
                {rfp?.requirement_count > 0
                  ? "Requirements exist in the database but could not be loaded, or your account cannot access this RFP. Try refreshing."
                  : "No requirements were extracted from this RFP. Upload an .xlsx matrix (with a Requirement/Description column) and/or PDF specifications."}
              </div>
            </div>
          )}

          {!requirementsLoading && !requirementsError && (matrixTotal > 0 || textualTotal > 0) && (
            <>
              <MatrixRequirementsSection
                sections={matrixSections}
                loading={false}
                onViewSource={setSourceModal}
              />

              <div className="my-10 border-t border-steel/80" aria-hidden />

              <DocumentRequirementsSection
                requirements={textualReqs}
                totalCount={textualTotal}
                loading={false}
                page={textualPage}
                pageSize={pageSize}
                onPageChange={setTextualPage}
                onViewSource={setSourceModal}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}

