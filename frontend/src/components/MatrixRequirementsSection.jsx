import { useState } from "react";
import { ChevronLeft, ChevronRight, FileSpreadsheet, CheckCircle2 } from "lucide-react";
import clsx from "clsx";

/**
 * Official matrix requirements (Dataset A): one tab per Excel worksheet / category.
 * Content is verbatim from the spreadsheet — not LLM-generated.
 */
export default function MatrixRequirementsSection({ sections, loading, onViewSource }) {
  const [activeIndex, setActiveIndex] = useState(0);

  if (loading) {
    return (
      <div className="card p-10 text-center text-sm text-ink-muted">
        Loading matrix requirements…
      </div>
    );
  }

  if (!sections?.length) {
    return (
      <div className="card p-10 text-center text-sm text-ink-muted border-dashed border-steel">
        <FileSpreadsheet className="mx-auto mb-3 text-emerald-600/60" size={28} />
        <p className="font-heading text-ink mb-1">No matrix requirements</p>
        <p className="text-xs max-w-md mx-auto">
          Upload an Excel matrix (.xlsx) with one requirement table per worksheet. Each sheet
          becomes a category tab with unchanged requirement text and IDs.
        </p>
      </div>
    );
  }

  const safeIndex = Math.min(activeIndex, sections.length - 1);
  const section = sections[safeIndex];
  const reqs = section.requirements || [];

  const go = (delta) => {
    setActiveIndex((i) => {
      const next = i + delta;
      if (next < 0) return sections.length - 1;
      if (next >= sections.length) return 0;
      return next;
    });
  };

  return (
    <section className="mb-12">
      <div className="mb-4">
        <h2 className="font-heading text-lg text-ink">Matrix requirements</h2>
        <p className="text-xs text-ink-muted mt-1 max-w-2xl">
          Official ground truth from the compliance matrix Excel file. Text and IDs match the
          spreadsheet exactly; only mandatory status and source file are added as metadata.
        </p>
      </div>

      <div className="card overflow-hidden">
        {/* Category tabs / carousel controls */}
        <div className="px-4 py-3 border-b border-steel bg-emerald-50/40 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => go(-1)}
            className="p-2 rounded-lg border border-steel/60 bg-white hover:bg-ivory transition-colors shrink-0"
            aria-label="Previous category"
          >
            <ChevronLeft size={18} />
          </button>

          <div className="flex-1 flex flex-wrap gap-2 justify-center min-w-0">
            {sections.map((s, idx) => (
              <button
                key={s.sheet_name || s.category || idx}
                type="button"
                onClick={() => setActiveIndex(idx)}
                className={clsx(
                  "text-xs font-mono px-3 py-1.5 rounded-full border transition-colors truncate max-w-[200px]",
                  idx === safeIndex
                    ? "bg-emerald-600 text-white border-emerald-600"
                    : "bg-white text-ink-muted border-steel/70 hover:border-emerald-400",
                )}
                title={s.category}
              >
                {s.category}
                <span className="ml-1 opacity-80">({s.count})</span>
              </button>
            ))}
          </div>

          <button
            type="button"
            onClick={() => go(1)}
            className="p-2 rounded-lg border border-steel/60 bg-white hover:bg-ivory transition-colors shrink-0"
            aria-label="Next category"
          >
            <ChevronRight size={18} />
          </button>
        </div>

        {/* Active category header */}
        <div className="px-6 py-5 border-b border-steel bg-ivory/30">
          <div className="flex items-center gap-2 text-emerald-700 mb-1">
            <FileSpreadsheet size={18} />
            <span className="text-[10px] font-mono uppercase tracking-widest">
              Category
            </span>
          </div>
          <h3 className="font-heading text-xl text-ink">{section.category}</h3>
          <p className="text-sm text-ink-muted font-mono mt-1">
            {section.count} requirement{section.count === 1 ? "" : "s"}
            {section.sheet_name && section.sheet_name !== section.category ? (
              <span className="text-ink-muted/70"> · Sheet: {section.sheet_name}</span>
            ) : null}
          </p>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm font-body">
            <thead className="sticky top-0 bg-ivory z-10">
              <tr className="border-b border-steel">
                <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-36">
                  Req ID
                </th>
                <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider">
                  Requirement text
                </th>
                <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-28">
                  Mandatory
                </th>
                <th className="text-left text-xs font-medium text-ink-muted px-6 py-3 uppercase tracking-wider w-48">
                  Source document
                </th>
              </tr>
            </thead>
            <tbody>
              {reqs.map((req, i) => (
                <tr
                  key={req.req_id}
                  className={clsx(
                    "border-b border-steel/50",
                    i % 2 === 0 ? "bg-white" : "bg-ivory",
                  )}
                >
                  <td className="px-6 py-4 font-mono text-ink align-top whitespace-nowrap">
                    {req.req_code || "—"}
                  </td>
                  <td className="px-6 py-4 text-ink align-top max-w-2xl whitespace-pre-wrap">
                    {req.original_text ?? req.description}
                  </td>
                  <td className="px-6 py-4 text-center align-top">
                    {req.is_mandatory ? (
                      <span className="inline-flex items-center gap-1 text-xs text-emerald-700 font-mono">
                        <CheckCircle2 size={14} /> Mandatory
                      </span>
                    ) : (
                      <span className="text-xs font-mono text-ink-muted">Optional</span>
                    )}
                  </td>
                  <td className="px-6 py-4 align-top">
                    <button
                      type="button"
                      onClick={() =>
                        onViewSource?.({
                          doc_id:   req.source_doc_id || null,
                          filename: req.file_name || "Matrix",
                          text:     req.original_text ?? req.description,
                          type:     "matrix",
                          sheet:    req.source_sheet || section.sheet_name,
                          row:      req.matrix_row_order ?? null,
                        })
                      }
                      className="text-xs font-mono text-emerald-700 hover:underline text-left"
                    >
                      {req.file_name || "—"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="px-6 py-2 border-t border-steel/50 text-[10px] font-mono text-ink-muted text-center">
          {safeIndex + 1} / {sections.length} categories
        </div>
      </div>
    </section>
  );
}
