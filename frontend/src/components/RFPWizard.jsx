import React, { useState, useEffect } from "react";
import { 
  FileUp, 
  ChevronRight, 
  CheckCircle2, 
  AlertCircle, 
  Trash2, 
  Plus, 
  Save,
  ArrowRight,
  Settings2,
  FileText
} from "lucide-react";
import clsx from "clsx";
import { rfpApi } from "../api/client";

export default function RFPWizard({ onComplete, onCancel }) {
  const [step, setStep] = useState(1); // 1: Upload, 2: Analyze, 3: Confirm, 4: Ingesting
  const [file, setFile] = useState(null);
  const [metadata, setMetadata] = useState({ 
    title: "", 
    company: "Ooredoo", 
    issuingDate: new Date().toISOString().split('T')[0],
    deadline: "",
    scoringMargin: 10,
    complianceLevels: [
      { label: "Fully Compliant", value: 5 },
      { label: "Partially Compliant", value: 3 },
      { label: "Non-Compliant", value: 1 }
    ]
  });
  const [schema, setSchema] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [taskId, setTaskId] = useState(null);
  const [task, setTask] = useState(null);

  const addComplianceLevel = () => {
    setMetadata(prev => ({
      ...prev,
      complianceLevels: [...prev.complianceLevels, { label: "", value: 1 }]
    }));
  };

  const removeComplianceLevel = (idx) => {
    setMetadata(prev => ({
      ...prev,
      complianceLevels: prev.complianceLevels.filter((_, i) => i !== idx)
    }));
  };

  const updateComplianceLevel = (idx, field, val) => {
    setMetadata(prev => {
      const next = [...prev.complianceLevels];
      next[idx] = { ...next[idx], [field]: val };
      return { ...prev, complianceLevels: next };
    });
  };

  // Auto-fill title from filename
  useEffect(() => {
    if (file && !metadata.title) {
      setMetadata(prev => ({ ...prev, title: file.name.replace(/\.[^/.]+$/, "") }));
    }
  }, [file]);

  const handleFileChange = (e) => {
    const selected = e.target.files[0];
    if (selected) {
      setFile(selected);
      setStep(2);
    }
  };

  const handleAnalyze = async () => {
    setLoading(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await rfpApi.analyze(form);
      setSchema(res.data.schema || []);
      setStep(3);
    } catch (err) {
      setError("Failed to analyze Evaluation Matrix. Ensure the ZIP contains an XLSX with weights.");
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async () => {
    setLoading(true);
    setError(null);
    setTask(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("title", metadata.title);
      form.append("issuing_company", metadata.company);
      form.append("issuing_date", metadata.issuingDate);
      form.append("submission_deadline", metadata.deadline);
      form.append("scoring_margin", metadata.scoringMargin);
      form.append("compliance_levels", JSON.stringify(metadata.complianceLevels));
      form.append("confirmed_criteria", JSON.stringify(schema));
      
      const res = await rfpApi.create(form);
      setTaskId(res.data.task_id);
      setTask({ status: res.data.status, progress: 0 });
      setStep(4);
    } catch (err) {
      setError("Failed to start ingestion pipeline.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (step !== 4 || !taskId) return;

    let cancelled = false;
    let timeoutId;

    const pollTask = async () => {
      try {
        const res = await rfpApi.taskStatus(taskId);
        if (cancelled) return;

        const nextTask = res.data;
        setTask(nextTask);

        if (nextTask.status === "done") {
          return;
        }
        if (nextTask.status === "error") {
          setError(nextTask.error || "Ingestion failed.");
          return;
        }

        timeoutId = window.setTimeout(pollTask, 1500);
      } catch (err) {
        if (!cancelled) {
          setError("Lost connection while checking ingestion status.");
          timeoutId = window.setTimeout(pollTask, 3000);
        }
      }
    };

    pollTask();

    return () => {
      cancelled = true;
      if (timeoutId) window.clearTimeout(timeoutId);
    };
  }, [step, taskId]);

  const updateNodeWeight = (path, newWeight) => {
    const nextSchema = JSON.parse(JSON.stringify(schema));
    let target = nextSchema;
    for (let i = 0; i < path.length; i++) {
      if (i === path.length - 1) {
        target[path[i]].weight = parseFloat(newWeight) || 0;
      } else {
        target = target[path[i]].children;
      }
    }
    setSchema(nextSchema);
  };

  const renderSchemaRow = (node, path = []) => {
    const isParent = node.depth === 0;
    return (
      <React.Fragment key={path.join("-")}>
        <div className={clsx(
          "grid grid-cols-[1fr,120px,200px] gap-4 py-3 border-b border-steel/50 items-center transition-colors",
          isParent ? "bg-ivory/50 font-medium" : "pl-8 text-sm"
        )}>
          <div className="flex items-center gap-2">
            {!isParent && <ChevronRight size={14} className="text-steel" />}
            <span>{node.label}</span>
          </div>
          <div>
            <input 
              type="number" 
              step="0.01"
              value={node.weight}
              onChange={(e) => updateNodeWeight(path, e.target.value)}
              className="w-full bg-white border border-steel rounded px-2 py-1 text-right font-mono text-xs focus:ring-1 focus:ring-crimson outline-none"
            />
          </div>
          <div className="text-[10px] text-ink-muted font-mono truncate uppercase tracking-tighter">
            {node.source_file_hint || "Detecting from role..."}
          </div>
        </div>
        {node.children && node.children.map((child, i) => renderSchemaRow(child, [...path, i]))}
      </React.Fragment>
    );
  };

  return (
    <div className="fixed inset-0 bg-ink/40 backdrop-blur-sm flex items-center justify-center z-[100] p-4">
      <div className="bg-white w-full max-w-6xl rounded-2xl shadow-2xl overflow-hidden flex flex-col max-h-[90vh] animate-slide-up">
        
        {/* Header */}
        <div className="px-8 py-6 border-b border-steel flex items-center justify-between bg-white">
          <div>
            <h2 className="font-display text-3xl text-ink tracking-wide leading-none">RFP INGESTION</h2>
            <div className="flex items-center gap-2 mt-1">
              {[1, 2, 3, 4].map(s => (
                <div key={s} className={clsx(
                  "h-1.5 rounded-full transition-all duration-300",
                  step === s ? "w-8 bg-crimson" : step > s ? "w-4 bg-ink" : "w-4 bg-steel"
                )} />
              ))}
              <span className="text-[10px] font-mono text-ink-muted uppercase ml-2 tracking-widest">
                Step {step} of 4
              </span>
            </div>
          </div>
          <button onClick={onCancel} className="text-ink-muted hover:text-ink transition-colors">
            <Trash2 size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-12">
          {step === 1 && (
            <div className="flex flex-col items-center justify-center h-full py-16 border-2 border-dashed border-steel rounded-xl bg-ivory/30 max-w-4xl mx-auto">
              <div className="w-16 h-16 bg-crimson/10 text-crimson rounded-full flex items-center justify-center mb-4">
                <FileUp size={32} />
              </div>
              <h3 className="font-heading text-xl text-ink mb-2">Upload RFP Package</h3>
              <p className="text-sm text-ink-muted mb-6 text-center max-w-sm">
                Upload a ZIP file containing the RFP documents and the Evaluation Matrix (XLSX).
              </p>
              <label className="btn-primary cursor-pointer">
                Select Files
                <input type="file" className="hidden" onChange={handleFileChange} accept=".zip,.xlsx" />
              </label>
            </div>
          )}

          {step === 2 && (
            <div className="max-w-2xl mx-auto py-8">
              <div className="space-y-6">
                <div className="p-4 bg-ivory rounded-lg border border-steel flex items-center gap-4">
                  <div className="p-2 bg-white rounded border border-steel">
                    <FileText className="text-crimson" size={24} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm font-medium text-ink truncate">{file?.name}</div>
                    <div className="text-xs text-ink-muted">{(file?.size / 1024 / 1024).toFixed(2)} MB</div>
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">Issuing Company</label>
                    <div className="w-full bg-ivory border border-steel rounded-lg px-4 py-2 text-sm text-ink font-medium">
                      Ooredoo
                    </div>
                  </div>
                  <div>
                    <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">RFP Title</label>
                    <input 
                      type="text" 
                      value={metadata.title}
                      onChange={e => setMetadata(prev => ({ ...prev, title: e.target.value }))}
                      className="w-full bg-white border border-steel rounded-lg px-4 py-2 text-sm focus:ring-1 focus:ring-crimson outline-none"
                    />
                  </div>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">Issuing Date</label>
                    <input 
                      type="date" 
                      value={metadata.issuingDate}
                      onChange={e => setMetadata(prev => ({ ...prev, issuingDate: e.target.value }))}
                      className="w-full bg-white border border-steel rounded-lg px-4 py-2 text-sm focus:ring-1 focus:ring-crimson outline-none"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">Submission Deadline</label>
                    <input 
                      type="date" 
                      value={metadata.deadline}
                      onChange={e => setMetadata(prev => ({ ...prev, deadline: e.target.value }))}
                      className="w-full bg-white border border-steel rounded-lg px-4 py-2 text-sm focus:ring-1 focus:ring-crimson outline-none"
                    />
                  </div>
                </div>

                {/* Advanced Configuration */}
                <div className="mt-8 pt-8 border-t border-steel">
                  <div className="flex items-center gap-2 mb-6">
                    <Settings2 size={16} className="text-ink-muted" />
                    <h4 className="font-heading text-sm text-ink uppercase tracking-wider">Advanced Evaluation Logic</h4>
                  </div>
                  
                  <div className="grid grid-cols-3 gap-8">
                    {/* Scoring Margin */}
                    <div className="col-span-1">
                      <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted mb-1.5">Auto-Edit Margin (%)</label>
                      <p className="text-[10px] text-ink-muted mb-3 leading-relaxed">Threshold for manual score overrides vs AI suggestions.</p>
                      <input 
                        type="number" 
                        value={metadata.scoringMargin}
                        onChange={e => setMetadata(prev => ({ ...prev, scoringMargin: parseInt(e.target.value) || 0 }))}
                        className="w-24 bg-white border border-steel rounded-lg px-4 py-2 text-sm focus:ring-1 focus:ring-crimson outline-none"
                      />
                    </div>

                    {/* Compliance Levels */}
                    <div className="col-span-2">
                      <div className="flex items-center justify-between mb-3">
                        <label className="block text-xs font-mono uppercase tracking-widest text-ink-muted">Compliance Mapping *</label>
                        <button
                          type="button"
                          onClick={addComplianceLevel}
                          className="text-xs text-crimson hover:underline font-medium flex items-center gap-1"
                        >
                          <Plus size={14} /> Add level
                        </button>
                      </div>
                      <p className="text-[10px] text-ink-muted mb-3 leading-relaxed">Define names and ratings (1-5) for requirement compliance levels.</p>
                      
                      <div className="space-y-2">
                        {metadata.complianceLevels.map((lvl, idx) => (
                          <div key={idx} className="flex items-center gap-2">
                            <input 
                              type="text" 
                              value={lvl.label}
                              onChange={(e) => updateComplianceLevel(idx, "label", e.target.value)}
                              placeholder="e.g. Fully Compliant"
                              className="flex-1 bg-white border border-steel rounded-lg px-3 py-2 text-sm focus:ring-1 focus:ring-crimson outline-none"
                            />
                            <div className="flex items-center gap-2">
                              <input 
                                type="number" 
                                min="1"
                                max="5"
                                value={lvl.value}
                                onChange={(e) => updateComplianceLevel(idx, "value", Math.max(1, Math.min(5, parseInt(e.target.value) || 1)))}
                                className="w-16 bg-white border border-steel rounded-lg px-3 py-2 text-sm text-right font-mono focus:ring-1 focus:ring-crimson outline-none"
                              />
                              <span className="text-xs text-ink-muted">/5</span>
                            </div>
                            {metadata.complianceLevels.length > 1 && (
                              <button
                                type="button"
                                onClick={() => removeComplianceLevel(idx)}
                                className="p-2 text-crimson hover:bg-crimson/5 rounded-lg transition-colors"
                              >
                                <Trash2 size={16} />
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>

                {error && (
                  <div className="mt-4 p-4 bg-red-50 text-crimson border border-crimson/20 rounded-lg flex items-center gap-3 text-sm animate-shake">
                    <AlertCircle size={18} /> {error}
                  </div>
                )}

                <button 
                  onClick={handleAnalyze}
                  disabled={loading || !metadata.title}
                  className="w-full btn-primary flex items-center justify-center gap-2 mt-8 py-3"
                >
                  {loading ? "Analyzing..." : "Analyze Matrix"}
                  {!loading && <ArrowRight size={18} />}
                </button>
              </div>
            </div>
          )}

          {step === 3 && (
            <div className="animate-fade-in">
              <div className="flex items-center justify-between mb-6">
                <div>
                  <h3 className="font-heading text-xl text-ink">Confirm Scoring Schema</h3>
                  <p className="text-sm text-ink-muted">We found the following hierarchy in your Evaluation Matrix. Review the weights below.</p>
                </div>
                <div className="flex items-center gap-2 p-2 bg-emerald-50 text-emerald-700 rounded-lg border border-emerald-100 text-[10px] font-mono uppercase">
                  <CheckCircle2 size={14} /> Total: 100%
                </div>
              </div>

              <div className="border border-steel rounded-xl overflow-hidden bg-white shadow-sm">
                <div className="grid grid-cols-[1fr,120px,200px] gap-4 px-6 py-3 bg-ivory border-b border-steel text-[10px] font-mono text-ink-muted uppercase tracking-widest">
                  <span>Criteria Hierarchy</span>
                  <span className="text-right">Weight (%)</span>
                  <span>Data Source</span>
                </div>
                <div className="max-h-[400px] overflow-y-auto px-6">
                  {schema.length > 0 ? (
                    schema.map((node, i) => renderSchemaRow(node, [i]))
                  ) : (
                    <div className="py-20 text-center text-ink-muted text-sm italic">
                      No schema found. Try adding criteria manually or re-extracting.
                    </div>
                  )}
                </div>
              </div>

              {error && (
                <div className="mt-6 p-4 bg-red-50 text-crimson border border-crimson/20 rounded-lg flex items-center gap-3 text-sm">
                  <AlertCircle size={18} /> {error}
                </div>
              )}

              <div className="flex gap-4 mt-8">
                <button onClick={() => setStep(2)} className="flex-1 btn-ghost">Back</button>
                <button onClick={handleConfirm} disabled={loading} className="flex-[2] btn-primary flex items-center justify-center gap-2">
                  {loading ? "Initializing..." : "Confirm & Start Ingestion"}
                  {!loading && <ArrowRight size={18} />}
                </button>
              </div>
            </div>
          )}

          {step === 4 && (
            <div className="flex flex-col items-center justify-center py-20 animate-fade-in">
              <div className="relative mb-8">
                <div className="w-24 h-24 border-4 border-steel rounded-full" />
                <div className="absolute inset-0 w-24 h-24 border-4 border-crimson rounded-full border-t-transparent animate-spin" />
                <div className="absolute inset-0 flex items-center justify-center text-crimson">
                  <Settings2 size={32} />
                </div>
              </div>
              <h3 className="font-heading text-2xl text-ink mb-2">
                {task?.status === "done" ? "Ingestion Complete" : task?.status === "error" ? "Ingestion Failed" : "Ingesting RFP"}
              </h3>
              <p className="text-sm text-ink-muted mb-4 text-center max-w-sm">
                {task?.status === "done"
                  ? `Extracted ${task?.result?.requirement_count ?? 0} requirements.`
                  : task?.status === "error"
                    ? "The backend could not finish this RFP."
                    : "The AI is extracting requirements and mapping them to your confirmed weights."}
              </p>

              <div className="w-full max-w-xs h-2 bg-steel rounded-full overflow-hidden mb-4">
                <div
                  className={clsx(
                    "h-full transition-all duration-500",
                    task?.status === "error" ? "bg-crimson" : "bg-emerald-500"
                  )}
                  style={{ width: `${task?.status === "done" ? 100 : task?.progress || 10}%` }}
                />
              </div>

              {error && (
                <div className="mb-6 max-w-md rounded-lg border border-crimson/20 bg-red-50 px-4 py-3 text-sm text-crimson">
                  {error}
                </div>
              )}

              <button
                onClick={() => onComplete(task)}
                disabled={!task || task.status === "running" || task.status === "ingesting"}
                className="btn-primary disabled:opacity-50"
              >
                {task?.status === "done" ? "Open Evaluation" : task?.status === "error" ? "Return to Dashboard" : "Processing..."}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
