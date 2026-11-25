import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Clock, MessageSquare } from "lucide-react";
import clsx from "clsx";
import { rfpApi } from "../api/client";


function sortByCreatedAtDesc(a, b) {
  return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
}

export default function Archive() {
  const [rfps, setRfps] = useState([]);
  const navigate = useNavigate();

  useEffect(() => {
    rfpApi
      .list()
      .then((r) => setRfps(Array.isArray(r.data) ? r.data : []))
      .catch(() => {});
  }, []);

  const sortedRfps = useMemo(() => [...rfps].sort(sortByCreatedAtDesc), [rfps]);

  return (
    <div className="p-8">
      <div className="flex items-end justify-between mb-8">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            ARCHIVE
          </div>
          <div className="text-ink-muted font-body text-sm mt-1">
            All RFPs in chronological order
          </div>
        </div>
      </div>

      <div className="card overflow-hidden">
        <div className="px-6 py-4 border-b border-steel flex items-center justify-between">
          <span className="font-heading text-lg text-ink">
            All RFP Packages
          </span>
          <span className="text-xs text-ink-muted font-mono">
            {sortedRfps.length} total
          </span>
        </div>
        {sortedRfps.length === 0 ? (
          <div className="py-20 text-center text-ink-muted font-body">
            No archived RFPs yet.
          </div>
        ) : (
          <table className="w-full text-sm font-body">
            <thead>
              <tr className="border-b border-steel bg-ivory">
                {["RFP title", "Requirements", "Vendors", "Created at", ""].map(
                  (h) => (
                    <th
                      key={h}
                      className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider"
                    >
                      {h}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {sortedRfps.map((rfp, i) => (
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
                  </td>
                  <td className="px-6 py-4 font-mono text-ink">
                    {rfp.requirement_count || "—"}
                  </td>
                  <td className="px-6 py-4 font-mono text-ink">
                    {rfp.vendor_count || 0}
                  </td>
                  <td className="px-6 py-4 text-ink-muted text-xs flex items-center gap-1">
                    <Clock size={10} /> {rfp.created_at}
                  </td>
                  <td className="px-6 py-4">
                    <div className="flex gap-2">
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          navigate(`/evaluation/${rfp.rfp_id}`);
                        }}
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
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
