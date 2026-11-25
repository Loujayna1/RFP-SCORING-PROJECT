import { useEffect, useState } from "react";
import { RefreshCw, ShieldAlert } from "lucide-react";
import { rfpApi } from "../api/client";

const KNOWN_ACTIONS = [
  "create_rfp",
  "add_vendor",
  "update_config",
  "update_requirement_score",
];

function formatDate(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export default function AuditLog() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filters, setFilters] = useState({ rfpId: "", action: "" });
  const [draftFilters, setDraftFilters] = useState({ rfpId: "", action: "" });

  const loadAuditLog = async (nextFilters = filters) => {
    setLoading(true);
    setError("");
    try {
      const params = { limit: 100 };
      if (nextFilters.rfpId.trim()) {
        params.rfp_id = nextFilters.rfpId.trim();
      }
      if (nextFilters.action.trim()) {
        params.action = nextFilters.action.trim();
      }
      const { data } = await rfpApi.auditLog(params);
      setItems(data.items || []);
    } catch (err) {
      setError(err?.response?.data?.detail || "Failed to load audit log");
      setItems([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadAuditLog();
  }, []);

  const applyFilters = () => {
    const normalized = {
      rfpId: draftFilters.rfpId.trim(),
      action: draftFilters.action.trim(),
    };
    setFilters(normalized);
    loadAuditLog(normalized);
  };

  const resetFilters = () => {
    const cleared = { rfpId: "", action: "" };
    setDraftFilters(cleared);
    setFilters(cleared);
    loadAuditLog(cleared);
  };

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="flex items-end justify-between gap-4 mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            AUDIT LOG
          </div>
          <div className="text-ink-muted font-body text-sm mt-1 max-w-2xl">
            Track configuration changes, uploads, and score edits across the
            system.
          </div>
        </div>
        <button onClick={loadAuditLog} className="btn-ghost flex items-center gap-2">
          <RefreshCw size={16} /> Refresh
        </button>
      </div>

      <div className="card p-4 mb-6">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 items-end">
          <div>
            <label className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-1.5 block">
              RFP ID
            </label>
            <input
              value={draftFilters.rfpId}
              onChange={(e) =>
                setDraftFilters((prev) => ({ ...prev, rfpId: e.target.value }))
              }
              placeholder="Filter by RFP UUID"
              className="w-full border border-steel rounded-lg px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors"
            />
          </div>
          <div>
            <label className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-1.5 block">
              Action
            </label>
            <input
              list="audit-action-options"
              value={draftFilters.action}
              onChange={(e) =>
                setDraftFilters((prev) => ({ ...prev, action: e.target.value }))
              }
              placeholder="e.g. update_config"
              className="w-full border border-steel rounded-lg px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors"
            />
            <datalist id="audit-action-options">
              {KNOWN_ACTIONS.map((action) => (
                <option key={action} value={action} />
              ))}
            </datalist>
          </div>
          <div className="flex gap-2 md:justify-end">
            <button onClick={applyFilters} className="btn-primary">Apply filters</button>
            <button onClick={resetFilters} className="btn-ghost">Reset</button>
          </div>
        </div>
        {(filters.rfpId || filters.action) && (
          <div className="mt-3 text-xs text-ink-muted font-mono">
            Active filters: {filters.rfpId ? `rfp_id=${filters.rfpId}` : ""}
            {filters.rfpId && filters.action ? " | " : ""}
            {filters.action ? `action=${filters.action}` : ""}
          </div>
        )}
      </div>

      {error && (
        <div className="mb-6 flex items-center gap-2 text-crimson text-sm font-body bg-crimson/5 border border-crimson/20 rounded-lg px-4 py-3">
          {error}
        </div>
      )}

      <div className="card overflow-hidden">
        {loading ? (
          <div className="p-8 text-sm text-ink-muted font-body">Loading audit events...</div>
        ) : items.length === 0 ? (
          <div className="p-10 text-center">
            <ShieldAlert size={28} className="mx-auto text-ink-muted mb-3" />
            <div className="font-medium text-ink">No audit events yet</div>
            <div className="text-sm text-ink-muted mt-1">
              Changes will appear here once users upload RFPs, adjust scoring
              settings, or edit scores.
            </div>
          </div>
        ) : (
          <div className="divide-y divide-steel/60">
            {items.map((item) => (
              <div key={item.audit_id} className="p-5 hover:bg-ivory/60 transition-colors">
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                  <div>
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-xs uppercase tracking-wider font-mono px-2 py-1 rounded-full bg-ink text-white">
                        {item.action}
                      </span>
                      <span className="text-sm font-medium text-ink">
                        {item.entity_type}
                      </span>
                      {item.entity_id && (
                        <span className="text-xs text-ink-muted font-mono break-all">
                          {item.entity_id}
                        </span>
                      )}
                    </div>
                    <div className="mt-2 text-sm text-ink-muted">
                      Actor: {item.actor || "system"}
                      {item.rfp_id ? ` · RFP ${item.rfp_id}` : ""}
                    </div>
                  </div>
                  <div className="text-sm text-ink-muted lg:text-right">
                    {formatDate(item.created_at)}
                  </div>
                </div>

                {item.details && Object.keys(item.details).length > 0 && (
                  <pre className="mt-4 overflow-x-auto rounded-lg bg-ivory px-4 py-3 text-xs text-ink-muted font-mono border border-steel/60 whitespace-pre-wrap">
{JSON.stringify(item.details, null, 2)}
                  </pre>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
