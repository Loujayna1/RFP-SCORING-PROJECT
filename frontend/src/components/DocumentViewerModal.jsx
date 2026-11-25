import { useState, useEffect, useRef } from "react";
import {
  X,
  ChevronLeft,
  ChevronRight,
  FileText,
  FileSpreadsheet,
  Loader2,
  AlertCircle,
} from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

const isPdf   = (name) => /\.pdf$/i.test(name || "");
const isExcel = (name) => /\.(xlsx|xls|csv|ods)$/i.test(name || "");

/**
 * In-page document viewer.
 *
 * Props
 *   open        boolean   — show/hide
 *   onClose     fn        — called when user dismisses
 *   docId       string    — document UUID for /api/v1/docs/{id}/content
 *   filename    string    — used for type detection and header display
 *   page        number    — PDF: target page (1-based); default 1
 *   sheet       string    — Excel: target sheet name
 *   row         number    — Excel: target row index (0-based, matches matrix_row_order)
 *   evidenceText string   — optional excerpt shown in footer
 */
export default function DocumentViewerModal({
  open,
  onClose,
  docId,
  filename,
  page = 1,
  sheet = null,
  row = null,
  evidenceText = null,
}) {
  const [phase, setPhase] = useState("idle"); // idle | loading | ready | error
  const [errMsg, setErrMsg] = useState("");
  const [docMeta, setDocMeta] = useState(null); // { filename, content_base64, content_type }

  // PDF
  const [pdfDoc,      setPdfDoc]      = useState(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages,  setTotalPages]  = useState(0);
  const canvasRef      = useRef(null);
  const renderTaskRef  = useRef(null);

  // Excel
  const [workbook,    setWorkbook]    = useState(null);
  const [activeSheet, setActiveSheet] = useState(null);
  const [sheetData,   setSheetData]   = useState([]);
  const tableBodyRef   = useRef(null);

  const displayName = docMeta?.filename || filename || "Document";
  const docIsPdf    = isPdf(displayName);
  const docIsExcel  = isExcel(displayName);

  // ── Fetch document content ──────────────────────────────────────────────
  useEffect(() => {
    if (!open || !docId) return;
    setPhase("loading");
    setErrMsg("");
    setDocMeta(null);
    setPdfDoc(null);
    setWorkbook(null);
    setSheetData([]);
    setCurrentPage(page || 1);

    rfpApi.docContent(docId)
      .then(r => { setDocMeta(r.data); setPhase("ready"); })
      .catch(e => {
        setErrMsg(e?.response?.data?.detail || "Failed to load document.");
        setPhase("error");
      });
  }, [open, docId]); // eslint-disable-line react-hooks/exhaustive-deps

  // Sync target page when prop changes (e.g. user clicks a different citation)
  useEffect(() => { if (open) setCurrentPage(page || 1); }, [open, page]);

  // ── Initialise PDF ──────────────────────────────────────────────────────
  useEffect(() => {
    if (phase !== "ready" || !docMeta || !docIsPdf) return;
    const lib = window.pdfjsLib;
    if (!lib) { setErrMsg("PDF.js did not load. Check your internet connection."); setPhase("error"); return; }

    const binary = atob(docMeta.content_base64);
    const bytes  = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

    lib.getDocument({ data: bytes }).promise
      .then(doc => { setPdfDoc(doc); setTotalPages(doc.numPages); })
      .catch(e => { setErrMsg("Failed to parse PDF: " + e.message); setPhase("error"); });
  }, [phase, docMeta, docIsPdf]);

  // ── Render PDF page ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!pdfDoc || !canvasRef.current) return;
    renderTaskRef.current?.cancel();

    pdfDoc.getPage(currentPage).then(pg => {
      const canvas   = canvasRef.current;
      if (!canvas) return;
      const viewport = pg.getViewport({ scale: 1.6 });
      canvas.width   = viewport.width;
      canvas.height  = viewport.height;
      const task     = pg.render({ canvasContext: canvas.getContext("2d"), viewport });
      renderTaskRef.current = task;
      return task.promise.catch(e => {
        if (e?.name !== "RenderingCancelledException") console.error("PDF render error", e);
      });
    });
  }, [pdfDoc, currentPage]);

  // ── Initialise Excel workbook ───────────────────────────────────────────
  useEffect(() => {
    if (phase !== "ready" || !docMeta || !docIsExcel) return;
    const XLSX = window.XLSX;
    if (!XLSX) { setErrMsg("SheetJS did not load. Check your internet connection."); setPhase("error"); return; }
    try {
      const wb = XLSX.read(docMeta.content_base64, { type: "base64" });
      setWorkbook(wb);
      setActiveSheet(sheet || wb.SheetNames[0]);
    } catch (e) {
      setErrMsg("Failed to parse spreadsheet: " + e.message);
      setPhase("error");
    }
  }, [phase, docMeta, docIsExcel, sheet]);

  // ── Extract active sheet data ───────────────────────────────────────────
  useEffect(() => {
    if (!workbook || !activeSheet) return;
    const XLSX = window.XLSX;
    const ws   = workbook.Sheets[activeSheet];
    if (!ws) return;
    setSheetData(XLSX.utils.sheet_to_json(ws, { header: 1, defval: "" }));
  }, [workbook, activeSheet]);

  // ── Auto-scroll to target row ───────────────────────────────────────────
  useEffect(() => {
    if (!tableBodyRef.current || row == null || sheetData.length === 0) return;
    const t = setTimeout(() => {
      tableBodyRef.current
        ?.querySelector(`tr[data-row-idx="${row}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 80);
    return () => clearTimeout(t);
  }, [sheetData, row]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 bg-ink/70 backdrop-blur-md flex items-center justify-center z-[300] p-4">
      <div
        className="bg-white w-full max-w-6xl rounded-2xl shadow-2xl flex flex-col overflow-hidden animate-slide-up"
        style={{ height: "90vh" }}
      >
        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="px-6 py-4 border-b border-steel flex items-center justify-between flex-shrink-0">
          <div className="flex items-center gap-3 min-w-0">
            <div className={clsx(
              "p-2 rounded flex-shrink-0",
              docIsPdf ? "bg-crimson/5 text-crimson" : "bg-emerald-50 text-emerald-600"
            )}>
              {docIsPdf ? <FileText size={20} /> : <FileSpreadsheet size={20} />}
            </div>
            <div className="min-w-0">
              <h3 className="font-heading text-base text-ink leading-tight truncate">{displayName}</h3>
              <div className="text-[10px] font-mono text-ink-muted uppercase tracking-wide">
                {docIsPdf
                  ? `Page ${currentPage}${totalPages ? ` of ${totalPages}` : ""}`
                  : `Sheet: ${activeSheet || "—"}${row != null ? ` · Row ${row + 1}` : ""}`}
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2 flex-shrink-0">
            {/* PDF prev/next */}
            {docIsPdf && pdfDoc && (
              <div className="flex items-center gap-1 border border-steel rounded-lg px-2 py-1">
                <button
                  onClick={() => setCurrentPage(p => Math.max(1, p - 1))}
                  disabled={currentPage <= 1}
                  className="p-0.5 rounded hover:bg-ivory disabled:opacity-30 transition-colors"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="text-xs font-mono w-16 text-center select-none">{currentPage} / {totalPages}</span>
                <button
                  onClick={() => setCurrentPage(p => Math.min(totalPages, p + 1))}
                  disabled={currentPage >= totalPages}
                  className="p-0.5 rounded hover:bg-ivory disabled:opacity-30 transition-colors"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
            <button
              onClick={onClose}
              className="p-2 hover:bg-ivory rounded-full transition-colors text-ink-muted hover:text-ink"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* ── Excel sheet tabs ────────────────────────────────────────────── */}
        {docIsExcel && workbook && workbook.SheetNames.length > 1 && (
          <div className="flex gap-1 px-4 pt-2 border-b border-steel/30 flex-shrink-0 overflow-x-auto pb-0">
            {workbook.SheetNames.map(s => (
              <button
                key={s}
                onClick={() => setActiveSheet(s)}
                className={clsx(
                  "text-xs font-mono px-3 py-1.5 rounded-t border-x border-t transition-colors whitespace-nowrap",
                  s === activeSheet
                    ? "bg-white border-steel text-emerald-700 font-semibold relative top-px"
                    : "bg-ivory/60 border-transparent text-ink-muted hover:text-ink hover:bg-ivory"
                )}
              >
                {s}
              </button>
            ))}
          </div>
        )}

        {/* ── Content ─────────────────────────────────────────────────────── */}
        <div className="flex-1 overflow-auto bg-steel/10 flex items-start justify-center">
          {phase === "loading" && (
            <div className="flex flex-col items-center justify-center h-full gap-3 text-ink-muted">
              <Loader2 size={28} className="animate-spin" />
              <span className="text-sm font-mono">Loading document…</span>
            </div>
          )}

          {phase === "error" && (
            <div className="flex items-center justify-center h-full p-8">
              <div className="bg-red-50 border border-red-200 rounded-xl p-6 text-red-600 text-sm max-w-md text-center">
                <AlertCircle size={24} className="mx-auto mb-2 text-red-400" />
                {errMsg}
              </div>
            </div>
          )}

          {phase === "ready" && docIsPdf && (
            <div className="p-4">
              <canvas ref={canvasRef} className="shadow-xl rounded block" />
            </div>
          )}

          {phase === "ready" && docIsExcel && sheetData.length > 0 && (
            <div className="w-full p-4 overflow-x-auto">
              <table className="text-xs border-collapse bg-white shadow-sm min-w-full">
                <tbody ref={tableBodyRef}>
                  {sheetData.map((rowArr, rowIdx) => {
                    const isHeader = rowIdx === 0;
                    const isTarget = row != null && rowIdx === row;
                    return (
                      <tr
                        key={rowIdx}
                        data-row-idx={rowIdx}
                        className={clsx(
                          "transition-colors",
                          isTarget
                            ? "bg-amber-200 outline outline-2 outline-amber-400"
                            : isHeader
                            ? "bg-slate-100"
                            : rowIdx % 2 === 0 ? "bg-white" : "bg-slate-50/60",
                          "hover:bg-amber-50/40"
                        )}
                      >
                        <td className="px-2 py-1 text-[9px] text-ink-muted/40 border border-slate-200 select-none text-right w-7 font-mono">
                          {rowIdx + 1}
                        </td>
                        {rowArr.map((cell, colIdx) => {
                          const text = String(cell);
                          return (
                            <td
                              key={colIdx}
                              className={clsx(
                                "px-3 py-1.5 border border-slate-200 whitespace-nowrap max-w-xs",
                                isHeader && "font-semibold text-slate-700",
                                isTarget  && "font-medium"
                              )}
                              title={text}
                            >
                              {text.length > 120 ? text.slice(0, 120) + "…" : text}
                            </td>
                          );
                        })}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {phase === "ready" && !docIsPdf && !docIsExcel && (
            <div className="flex items-center justify-center h-full text-ink-muted text-sm">
              Preview not available for this file type ({displayName}).
            </div>
          )}
        </div>

        {/* ── Evidence footer ──────────────────────────────────────────────── */}
        {evidenceText && (
          <div className="border-t border-amber-200 bg-amber-50/60 px-6 py-3 flex-shrink-0">
            <span className="text-[10px] font-mono uppercase text-amber-700 mr-2 tracking-wide">Matched evidence:</span>
            <span className="text-xs italic text-ink">"{evidenceText}"</span>
          </div>
        )}
      </div>
    </div>
  );
}
