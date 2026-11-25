import { FileText, CheckCircle2, AlertTriangle } from "lucide-react";
import clsx from "clsx";

/**
 * LLM-extracted document requirements (Dataset B) — separate from official matrix.
 */
export default function DocumentRequirementsSection({
  requirements,
  totalCount,
  loading,
  page,
  pageSize,
  onPageChange,
  onViewSource,
}) {
  if (loading) {
    return (
      <div className="card p-10 text-center text-sm text-ink-muted">
        Loading document requirements…
      </div>
    );
  }

  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  return (
    <section>
      <div className="mb-4">
        <h2 className="font-heading text-lg text-ink">Document requirements</h2>
        <p className="text-xs text-ink-muted mt-1 max-w-2xl">
          Requirements inferred by the AI from PDF and Word specification documents only — not
          from Excel. Wording may differ from the official matrix and is not ground truth.
        </p>
      </div>

      <div className="card overflow-hidden">
        <div className="px-6 py-4 border-b border-steel bg-crimson/5 flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-2 text-crimson">
            <FileText size={18} />
            <span className="font-heading text-base text-ink">LLM extraction</span>
          </div>
          <span className="text-xs font-mono text-ink-muted">
            {totalCount} requirement{totalCount === 1 ? "" : "s"}
          </span>
        </div>

        {requirements.length === 0 ? (
          <div className="px-6 py-12 text-center text-sm text-ink-muted">
            No document requirements were extracted. Upload PDF or specification documents.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm font-body">
              <thead className="sticky top-0 bg-ivory z-10">
                <tr className="border-b border-steel">
                  <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-32">
                    Req ID
                  </th>
                  <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                    Requirement text
                  </th>
                  <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-36">
                    Category
                  </th>
                  <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-28">
                    Mandatory
                  </th>
                  <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-52">
                    Source
                  </th>
                </tr>
              </thead>
              <tbody>
                {requirements.map((req, i) => (
                  <tr
                    key={req.req_id}
                    className={clsx(
                      "border-b border-steel/50",
                      i % 2 === 0 ? "bg-white" : "bg-ivory",
                    )}
                  >
                    <td className="px-6 py-4 font-mono text-ink align-top">
                      {req.req_code || "—"}
                    </td>
                    <td className="px-6 py-4 text-ink align-top max-w-xl">
                      {req.description}
                    </td>
                    <td className="px-6 py-4 align-top">
                      <span className="text-[10px] font-mono border border-steel px-2 py-0.5 rounded uppercase text-ink-muted">
                        {req.category || "—"}
                      </span>
                    </td>
                    <td className="px-6 py-4 text-center align-top">
                      {req.is_mandatory ? (
                        <CheckCircle2 size={16} className="text-emerald-500 mx-auto" />
                      ) : (
                        <span className="text-ink-muted text-xs">—</span>
                      )}
                    </td>
                    <td className="px-6 py-4 align-top">
                      <button
                        type="button"
                        onClick={() =>
                          onViewSource?.({
                            doc_id: req.source_doc_id,
                            page: req.source_page,
                            text: req.original_text ?? req.description,
                            filename: req.file_name || "Document",
                            type: "textual",
                          })
                        }
                        className="flex items-start gap-2 text-crimson hover:underline text-left"
                      >
                        <FileText size={14} className="shrink-0 mt-0.5" />
                        <span className="text-xs font-mono">
                          {req.file_name || "Document"}
                          {req.source_page != null ? ` · p.${req.source_page}` : ""}
                        </span>
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {totalCount > pageSize && (
          <div className="flex justify-center items-center gap-4 py-4 border-t border-steel/50">
            <button
              type="button"
              onClick={() => onPageChange(Math.max(0, page - 1))}
              disabled={page === 0}
              className="btn-ghost text-sm disabled:opacity-50"
            >
              Previous
            </button>
            <span className="text-xs font-mono text-ink-muted">
              Page {page + 1} of {totalPages}
            </span>
            <button
              type="button"
              onClick={() => onPageChange(page + 1)}
              disabled={(page + 1) * pageSize >= totalCount}
              className="btn-ghost text-sm disabled:opacity-50"
            >
              Next
            </button>
          </div>
        )}
      </div>

      <p className="mt-3 flex items-start gap-2 text-[11px] text-ink-muted max-w-2xl">
        <AlertTriangle size={14} className="shrink-0 text-amber-500 mt-0.5" />
        These rows are AI-generated from document text and are kept separate from matrix
        requirements.
      </p>
    </section>
  );
}
