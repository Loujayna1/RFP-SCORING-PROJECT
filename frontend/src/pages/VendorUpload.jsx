import { useCallback, useState } from "react";
import { useDropzone } from "react-dropzone";
import { useNavigate, useParams } from "react-router-dom";
import {
  Upload,
  FileArchive,
  CheckCircle,
  Loader2,
  AlertCircle,
} from "lucide-react";
import clsx from "clsx";
import { rfpApi } from "../api/client";

function Dropzone({ label, hint, onFile, file, accent }) {
  const onDrop = useCallback(
    (accepted) => accepted[0] && onFile(accepted[0]),
    [onFile],
  );
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { "application/zip": [".zip"] },
    maxFiles: 1,
  });

  return (
    <div
      {...getRootProps()}
      className={clsx(
        "relative border-2 border-dashed rounded-xl p-8 text-center cursor-pointer transition-all duration-200",
        isDragActive
          ? "border-crimson bg-crimson/5 scale-[1.01]"
          : "border-steel hover:border-ink-muted hover:bg-ivory-warm",
        file && "border-solid border-emerald-300 bg-emerald-50",
      )}
    >
      <input {...getInputProps()} />
      {file ? (
        <div className="flex flex-col items-center gap-2">
          <CheckCircle size={32} className="text-emerald-500" />
          <div className="font-medium text-ink text-sm">{file.name}</div>
          <div className="text-xs text-ink-muted">
            {(file.size / 1024 / 1024).toFixed(2)} MB
          </div>
        </div>
      ) : (
        <div className="flex flex-col items-center gap-2">
          <div
            className={clsx(
              "w-12 h-12 rounded-full flex items-center justify-center mb-1",
              accent,
            )}
          >
            <FileArchive size={22} className="text-white" />
          </div>
          <div className="font-medium text-ink text-sm">{label}</div>
          <div className="text-xs text-ink-muted">{hint}</div>
          <div className="text-xs text-ink-muted/60 mt-1">
            Drop ZIP here or click to browse
          </div>
        </div>
      )}
    </div>
  );
}

export default function VendorUpload() {
  const { rfpId } = useParams();
  const navigate = useNavigate();
  const [entries, setEntries] = useState([
    { name: "", file: null, status: "idle", error: null, progress: 0 },
  ]);
  const [processingAll, setProcessingAll] = useState(false);

  function setEntry(idx, patch) {
    setEntries((prev) =>
      prev.map((e, i) => (i === idx ? { ...e, ...patch } : e)),
    );
  }

  function addEntry() {
    setEntries((prev) => [
      ...prev,
      { name: "", file: null, status: "idle", error: null, progress: 0 },
    ]);
  }

  function removeEntry(idx) {
    setEntries((prev) => prev.filter((_, i) => i !== idx));
  }

  async function pollTask(taskId, idx) {
    const start = Date.now();
    while (true) {
      const { data: task } = await rfpApi.taskStatus(taskId);
      // update UI progress for this entry
      setEntry(idx, {
        status: task.status === "running" ? "processing" : task.status,
        progress: task.progress || 0,
      });
      if (task.status === "done") return task;
      if (task.status === "error")
        throw new Error(task.error || "Processing failed.");
      if (Date.now() - start > 30 * 60 * 1000)
        throw new Error("Task polling timed out.");
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }

  async function uploadEntry(idx, { overrideRubric = false } = {}) {
    const entry = entries[idx];
    if (!entry || !entry.name.trim() || !entry.file) {
      setEntry(idx, { error: "Missing name or file" });
      return false;
    }

    setEntry(idx, { status: "processing", error: null, needsRubricOverride: false });
    try {
      const form = new FormData();
      form.append("file", entry.file);
      form.append("vendor_name", entry.name.trim());
      // Issue #6: by default the backend refuses to grade against an unlocked
      // rubric (HTTP 409). Setting this acknowledges the draft on purpose.
      if (overrideRubric) form.append("acknowledge_unlocked_rubric", "true");
      const { data } = await rfpApi.addVendor(rfpId, form);
      await pollTask(data.task_id, idx);
      setEntry(idx, { status: "done" });
      return true;
    } catch (e) {
      setEntry(idx, {
        status: "error",
        error: e?.response?.data?.detail || String(e),
        // A 409 means the rubric isn't locked — offer a deliberate override.
        needsRubricOverride: e?.response?.status === 409,
      });
      return false;
    }
  }

  async function handleUploadAll() {
    setProcessingAll(true);
    for (let i = 0; i < entries.length; i++) {
      // skip already successful
      if (entries[i].status === "done") continue;
      // eslint-disable-next-line no-await-in-loop
      await uploadEntry(i);
    }
    setProcessingAll(false);
    // navigate back to evaluation after uploads complete
    navigate(`/evaluation/${rfpId}`);
  }

  const anyMissing = entries.some((e) => !e.name.trim() || !e.file);

  return (
    <div className="p-8 max-w-4xl mx-auto">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <div className="font-display text-5xl text-ink tracking-wide leading-none">
            VENDOR UPLOAD
          </div>
          <div className="text-ink-muted font-body text-sm mt-1">
            Upload one or more vendor responses for this RFP.
          </div>
        </div>
        <div>
          <button onClick={addEntry} className="btn-ghost">
            Add vendor
          </button>
        </div>
      </div>

      <div className="space-y-6">
        {entries.map((entry, i) => (
          <div key={i} className="card p-5">
            <div className="flex items-start gap-4">
              <div className="flex-1">
                <label className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-1.5 block">
                  Vendor name *
                </label>
                <input
                  value={entry.name}
                  onChange={(e) => setEntry(i, { name: e.target.value })}
                  placeholder="Vendor name"
                  className="w-full border border-steel rounded-lg px-3 py-2 text-sm bg-white"
                />

                <div className="mt-4">
                  <label className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-1.5 block">
                    Vendor ZIP *
                  </label>
                  <Dropzone
                    label="Vendor ZIP"
                    hint="Response ZIP"
                    onFile={(f) => setEntry(i, { file: f })}
                    file={entry.file}
                    accent="bg-ink-soft"
                  />
                </div>

                {entry.error && (
                  <div className="text-crimson text-sm mt-2">{entry.error}</div>
                )}
                {entry.needsRubricOverride && (
                  <button
                    onClick={() => uploadEntry(i, { overrideRubric: true })}
                    className="mt-2 text-xs font-mono underline text-amber-700 hover:text-amber-900"
                  >
                    Grade against the draft (unlocked) rubric anyway
                  </button>
                )}
              </div>

              <div className="w-40 flex flex-col gap-2">
                <button
                  onClick={() => uploadEntry(i)}
                  disabled={entry.status === "processing"}
                  className="btn-primary"
                >
                  {entry.status === "processing" ? "Uploading…" : "Upload"}
                </button>
                <button onClick={() => removeEntry(i)} className="btn-ghost">
                  Remove
                </button>
                <div className="text-xs text-ink-muted mt-2">
                  Status:{" "}
                  <span
                    className={
                      entry.status === "done"
                        ? "text-emerald-600"
                        : entry.status === "error"
                          ? "text-crimson"
                          : "text-ink-muted"
                    }
                  >
                    {entry.status}
                    {entry.progress ? ` (${entry.progress}%)` : null}
                  </span>
                </div>
                {entry.status === "processing" && (
                  <div className="w-full mt-2">
                    <div className="h-2 bg-ivory-warm rounded overflow-hidden">
                      <div
                        style={{ width: `${entry.progress || 0}%` }}
                        className="h-2 bg-emerald-500"
                      />
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        ))}

        <div className="flex gap-3">
          <button
            onClick={handleUploadAll}
            disabled={processingAll || anyMissing}
            className="btn-primary"
          >
            {processingAll ? "Processing all…" : "Upload all"}
          </button>
          <button
            onClick={() => navigate(`/evaluation/${rfpId}`)}
            className="btn-ghost"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
