import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { ThumbsUp, ThumbsDown, HelpCircle, CheckCircle, Loader2 } from "lucide-react";
import { rfpApi } from "../api/client";
import clsx from "clsx";

const VOTES = [
  { value: "yes",   label: "Recommend",  icon: ThumbsUp,    cls: "border-emerald-300 bg-emerald-50 text-emerald-700",  activeCls: "border-emerald-500 bg-emerald-500 text-white" },
  { value: "maybe", label: "Unsure",     icon: HelpCircle,  cls: "border-amber-300 bg-amber-50 text-amber-700",        activeCls: "border-amber-500 bg-amber-500 text-white" },
  { value: "no",    label: "Do not recommend", icon: ThumbsDown, cls: "border-red-200 bg-red-50 text-crimson",         activeCls: "border-crimson bg-crimson text-white" },
];

function VendorVoteCard({ vendor, rfpId, onVoted }) {
  const [currentVote, setCurrentVote] = useState(vendor.my_vote || null);
  const [comment,     setComment]     = useState(vendor.my_comment || "");
  const [saving,      setSaving]      = useState(false);
  const [saved,       setSaved]       = useState(false);

  async function handleVote(value) {
    setSaving(true); setSaved(false);
    try {
      await rfpApi.submitVote(rfpId, vendor.response_id, { vote: value, comment });
      setCurrentVote(value);
      setSaved(true);
      onVoted();
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="card p-6 mb-4">
      <div className="flex items-center justify-between mb-5">
        <div>
          <div className="font-heading text-xl text-ink">{vendor.vendor_name}</div>
          {vendor.summary && (
            <div className="text-xs text-ink-muted mt-1 leading-relaxed max-w-md">{vendor.summary}</div>
          )}
        </div>
        {saved && (
          <div className="flex items-center gap-1 text-emerald-600 text-xs font-mono">
            <CheckCircle size={13} /> Saved
          </div>
        )}
        {saving && <Loader2 size={16} className="animate-spin text-ink-muted" />}
      </div>

      {/* Vote buttons */}
      <div className="flex gap-3 mb-4">
        {VOTES.map(({ value, label, icon: Icon, cls, activeCls }) => (
          <button
            key={value}
            onClick={() => handleVote(value)}
            disabled={saving}
            className={clsx(
              "flex-1 flex flex-col items-center gap-2 py-4 rounded-xl border-2 transition-all",
              currentVote === value ? activeCls : cls,
              "hover:opacity-90 disabled:opacity-50",
            )}
          >
            <Icon size={22} />
            <span className="text-xs font-medium">{label}</span>
          </button>
        ))}
      </div>

      {/* Comment */}
      <textarea
        value={comment}
        onChange={e => setComment(e.target.value)}
        onBlur={() => currentVote && handleVote(currentVote)}
        placeholder="Add a comment (optional)…"
        rows={2}
        className="w-full border border-steel rounded-lg px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson resize-none"
      />
    </div>
  );
}

export default function BusinessRepVote() {
  const { rfpId } = useParams();
  const [rfp,      setRfp]     = useState(null);
  const [vendors,  setVendors] = useState([]);
  const [loading,  setLoading] = useState(true);

  async function load() {
    try {
      const [rRes, vRes] = await Promise.all([
        rfpApi.get(rfpId),
        rfpApi.compare(rfpId),
      ]);
      setRfp(rRes.data);
      // Build vendor list from compare data (no scores shown to business reps)
      setVendors(
        (vRes.data.vendor_details || []).map(v => ({
          vendor_name: v.name,
          response_id: v.response_id,
          my_vote:     null,
          my_comment:  null,
        }))
      );
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [rfpId]);

  const allVoted = vendors.length > 0 && vendors.every(v => v.my_vote);

  if (loading) return (
    <div className="flex items-center justify-center min-h-[400px] flex-col gap-3 text-ink-muted">
      <Loader2 size={28} className="animate-spin text-crimson" />
    </div>
  );

  return (
    <div className="p-8 max-w-2xl mx-auto">
      <div className="mb-8">
        <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">
          Business Representative Review
        </div>
        <div className="font-display text-5xl text-ink tracking-wide leading-none">VENDOR VOTE</div>
        <div className="text-ink-muted font-body text-sm mt-1.5">{rfp?.title}</div>
        <div className="text-xs text-ink-muted mt-3 leading-relaxed max-w-md">
          Review each vendor and share your recommendation. Your vote will be
          considered alongside technical and financial evaluations.
        </div>
      </div>

      {vendors.map(v => (
        <VendorVoteCard
          key={v.response_id}
          vendor={v}
          rfpId={rfpId}
          onVoted={load}
        />
      ))}

      {vendors.length === 0 && (
        <div className="text-center py-20 text-ink-muted text-sm italic">
          No vendors uploaded yet.
        </div>
      )}

      {allVoted && (
        <div className="mt-6 flex items-center gap-3 px-4 py-4 bg-emerald-50 border border-emerald-200 rounded-xl text-sm text-emerald-800">
          <CheckCircle size={18} className="flex-shrink-0 text-emerald-500" />
          <span>You have voted on all vendors. Thank you for your input.</span>
        </div>
      )}
    </div>
  );
}
