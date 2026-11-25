import { useState, useEffect, useRef } from "react";
import {
  Send,
  ExternalLink,
  FileText,
  User,
  Cpu,
  Loader2,
  RotateCcw,
  MessageSquare,
  Minimize2,
  X,
  Pin,
  Bookmark,
  Download,
  Sparkles,
} from "lucide-react";
import { rfpApi, openChatSocket } from "../api/client";
import clsx from "clsx";

const BASE_PROMPTS = [
  "Which vendor scored best on security requirements?",
  "What are the main gaps in the top vendor responses?",
  "Compare the technical architectures of all vendors",
  "Summarize the key strengths of the leading candidate",
];

function safeStorageParse(value, fallback = []) {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function messageToSummary(msg) {
  return {
    content: msg.content,
    citations: msg.citations || [],
    time: msg.time,
    savedAt: new Date().toISOString(),
  };
}

function formatArtifactTime(savedAt, fallback = "Recently") {
  if (!savedAt) return fallback;
  try {
    return new Date(savedAt).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return fallback;
  }
}

function normalizeArtifact(artifact) {
  return {
    ...artifact,
    time: artifact.time || formatArtifactTime(artifact.saved_at || artifact.savedAt),
    savedAt: artifact.savedAt || artifact.saved_at || new Date().toISOString(),
    citations: Array.isArray(artifact.citations) ? artifact.citations : [],
  };
}

function CitationCard({ citation }) {
  return (
    <div className="mt-1 bg-emerald-50 border border-emerald-200 rounded-lg p-3">
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <div className="flex items-center gap-1.5">
          <FileText size={12} className="text-emerald-600 flex-shrink-0" />
          <span className="text-[11px] font-mono text-emerald-800 font-medium">
            {citation.source}
          </span>
          <span className="text-[10px] text-emerald-600">
            p.{citation.page_number}
          </span>
        </div>
        <a
          href={citation.deep_link}
          target="_blank"
          rel="noopener noreferrer"
          className="text-[10px] text-emerald-700 hover:underline flex items-center gap-0.5"
        >
          Open <ExternalLink size={9} />
        </a>
      </div>
      <div className="text-[11px] text-emerald-900 italic border-l-2 border-emerald-400 pl-2 leading-relaxed">
        "{citation.excerpt}"
      </div>
    </div>
  );
}

function Message({ msg }) {
  const isUser = msg.role === "user";
  return (
    <div
      className={clsx(
        "flex gap-3 animate-slide-up",
        isUser && "flex-row-reverse",
      )}
    >
      <div
        className={clsx(
          "w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 mt-0.5",
          isUser ? "bg-ink text-white" : "bg-crimson text-white",
        )}
      >
        {isUser ? <User size={13} /> : <Cpu size={13} />}
      </div>
      <div className={clsx("max-w-[85%]", isUser && "items-end flex flex-col")}>
        <div
          className={clsx(
            "rounded-2xl px-4 py-3 text-sm font-body leading-relaxed shadow-sm",
            isUser
              ? "bg-ink text-white rounded-tr-sm"
              : "bg-white border border-steel rounded-tl-sm text-ink",
          )}
        >
          {msg.content.split("\n").map((line, i) => {
            const parts = line.split(/(\*\*[^*]*\*\*)/g);
            return (
              <div key={i}>
                {parts.map((part, j) => {
                  if (part.startsWith("**") && part.endsWith("**")) {
                    return <strong key={j}>{part.slice(2, -2)}</strong>;
                  }
                  return part || "\u00A0";
                })}
              </div>
            );
          })}
          {msg.thinking && (
            <div className="flex items-center gap-1 mt-2 text-crimson text-xs">
              <Loader2 size={12} className="animate-spin" />
              <span className="font-medium italic opacity-80">
                {msg.thought || "Agent searching..."}
              </span>
            </div>
          )}
          {msg.streaming && !msg.thinking && (
            <span className="inline-block w-1 h-3.5 bg-crimson ml-0.5 animate-pulse rounded-sm" />
          )}
        </div>
        {msg.citations?.map((c, i) => (
          <CitationCard key={i} citation={c} />
        ))}
        {!isUser && msg.content && !msg.thinking && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            {msg.isPinned ? (
              <span className="text-[10px] font-mono uppercase tracking-widest text-emerald-700 bg-emerald-50 border border-emerald-200 px-1.5 py-0.5 rounded-full inline-flex items-center gap-1">
                <Pin size={9} /> Pinned
              </span>
            ) : null}
          </div>
        )}
        <div className="text-[10px] text-ink-muted mt-1 px-1 font-mono">
          {msg.time}
        </div>
      </div>
    </div>
  );
}

export default function ChatWidget({ rfpId }) {
  const [isOpen, setIsOpen]       = useState(false);
  const [messages, setMessages]   = useState([]);
  const [input, setInput]         = useState("");
  const [loading, setLoading]     = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [compareData, setCompareData] = useState(null);
  const [savedSummaries, setSavedSummaries] = useState([]);
  const [pinnedAnswers, setPinnedAnswers] = useState([]);
  const [queuedPrompt, setQueuedPrompt] = useState("");
  const endRef   = useRef(null);
  const wsRef    = useRef(null);
  // FIX — guard against state updates after unmount / navigation
  const mountedRef = useRef(true);

  const storageKey = rfpId ? `rfp-chat-state:${rfpId}` : null;
  const pinnedKey = rfpId ? `rfp-chat-pins:${rfpId}` : null;
  const summaryKey = rfpId ? `rfp-chat-summaries:${rfpId}` : null;

  const welcomeMessage = {
    role: "assistant",
    content:
      "Hello! I'm your RFP evaluation assistant. I can answer questions about requirements, vendor scores, and gaps — always with evidence cited from the source documents.",
    citations: [],
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!rfpId) return;

    setMessages([welcomeMessage]);
    setSessionId(null);
    setQueuedPrompt("");
    wsRef.current?.close();

    try {
      const storedState = window.localStorage.getItem(storageKey);
      if (storedState) {
        const parsed = JSON.parse(storedState);
        if (parsed?.draft) setInput(parsed.draft);
      }
    } catch {
      setPinnedAnswers([]);
      setSavedSummaries([]);
    }

    rfpApi.compare(rfpId).then((r) => {
      if (!mountedRef.current) return;
      setCompareData({
        vendors: Array.isArray(r.data?.vendors) ? r.data.vendors : [],
        ranking: Array.isArray(r.data?.ranking) ? r.data.ranking : [],
        criteria: Array.isArray(r.data?.criteria) ? r.data.criteria : [],
        totals: r.data?.totals && typeof r.data.totals === "object" ? r.data.totals : {},
      });
    }).catch(() => {});

    rfpApi.chatArtifacts(rfpId).then((r) => {
      if (!mountedRef.current) return;
      setPinnedAnswers(Array.isArray(r.data?.pinned) ? r.data.pinned.map(normalizeArtifact) : []);
      setSavedSummaries(Array.isArray(r.data?.summaries) ? r.data.summaries.map(normalizeArtifact) : []);
    }).catch(() => {
      try {
        setPinnedAnswers(safeStorageParse(window.localStorage.getItem(pinnedKey), []).map(normalizeArtifact));
        setSavedSummaries(safeStorageParse(window.localStorage.getItem(summaryKey), []).map(normalizeArtifact));
      } catch {
        setPinnedAnswers([]);
        setSavedSummaries([]);
      }
    });

    rfpApi
      .chatSession(rfpId, {})
      .then((r) => {
        const sid = r.data.session_id;
        if (!mountedRef.current) return;
        setSessionId(sid);
        rfpApi
          .chatHistory(rfpId, sid)
          .then((hist) => {
            if (!mountedRef.current) return;
            if (hist.data?.messages?.length > 0) {
                    setMessages([
                welcomeMessage,
                ...hist.data.messages.map((m) => ({
                  ...m,
                  citations: Array.isArray(m.citations) ? m.citations : [],
                  time: m.created_at
                    ? new Date(m.created_at).toLocaleTimeString([], {
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : "Recently",
                })),
              ]);
            }
          })
          .catch(() => {});
      })
      .catch((err) => {
        if (!mountedRef.current) return;
        const detail = err?.response?.data?.detail;
        const msg =
          typeof detail === "string"
            ? detail
            : "Could not start chat. Sign in again or wait until the RFP status is Ready.";
        setMessages([
          welcomeMessage,
          { role: "assistant", content: msg, citations: [], time: "Now" },
        ]);
      });

    return () => wsRef.current?.close();
  }, [rfpId, pinnedKey, summaryKey, storageKey]);

  useEffect(() => {
    if (!rfpId || !storageKey) return;
    try {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({ draft: input, updatedAt: new Date().toISOString() }),
      );
    } catch {
      // ignore storage failures
    }
  }, [input, rfpId, storageKey]);

  useEffect(() => {
    if (!pinnedKey) return;
    try {
      window.localStorage.setItem(pinnedKey, JSON.stringify(pinnedAnswers));
    } catch {
      // ignore storage failures
    }
  }, [pinnedAnswers, pinnedKey]);

  useEffect(() => {
    if (!summaryKey) return;
    try {
      window.localStorage.setItem(summaryKey, JSON.stringify(savedSummaries));
    } catch {
      // ignore storage failures
    }
  }, [savedSummaries, summaryKey]);

  useEffect(() => {
    const handleOpen = (event) => {
      setIsOpen(true);
      const prompt = event?.detail?.prompt;
      const autoSend = Boolean(event?.detail?.autoSend);
      if (prompt) {
        setInput(prompt);
        setQueuedPrompt(autoSend ? prompt : "");
        if (autoSend) {
          setLoading(false);
        }
      }
    };
    window.addEventListener("open-chat", handleOpen);
    return () => window.removeEventListener("open-chat", handleOpen);
  }, []);

  useEffect(() => {
    if (!queuedPrompt || !sessionId || loading) return;
    const prompt = queuedPrompt;
    setQueuedPrompt("");
    send(prompt);
  }, [queuedPrompt, sessionId, loading]);

  useEffect(() => {
    if (isOpen) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isOpen]);

  async function send(text) {
    const q = (text || input).trim();
    if (!q || loading) return;
    setInput("");
    const time = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });

    setMessages((m) => [...m, { role: "user", content: q, citations: [], time }]);
    setLoading(true);

    if (!sessionId) {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: "Could not establish a chat session. Please ensure the RFP is ready.",
          time,
        },
      ]);
      setLoading(false);
      return;
    }

    // Optimistic assistant message while connecting
    setMessages((m) => [
      ...m,
      { role: "assistant", content: "", citations: [], thinking: true, streaming: false, time },
    ]);

    // FIX 1 — openChatSocket() attaches ?token=... automatically
    // FIX 2 — setLoading(false) only fires on "done" or "error", not on "citations"
    // FIX 3 — mountedRef.current guard prevents state updates after unmount
    let buf = "";
    let firstTokenReceived = false;

    const ws = openChatSocket(sessionId, {
      onOpen: () => ws.send(JSON.stringify({ type: "question", content: q })),

      onMessage: (e) => {
        if (!mountedRef.current) return;
        const d = JSON.parse(e.data);

        if (d.type === "token") {
          if (!firstTokenReceived) {
            firstTokenReceived = true;
            setMessages((m) =>
              m.map((msg, i) =>
                i === m.length - 1
                  ? { ...msg, thinking: false, streaming: true, content: d.content }
                  : msg,
              ),
            );
            buf = d.content;
          } else {
            buf += d.content;
            setMessages((m) =>
              m.map((msg, i) =>
                i === m.length - 1 ? { ...msg, content: buf } : msg,
              ),
            );
          }
        } else if (d.type === "citations") {
          // FIX 2 — attach citations but do NOT clear loading here;
          // citations arrive before tokens (backend fix), so streaming continues
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1 ? { ...msg, citations: d.data } : msg,
            ),
          );
        } else if (d.type === "thought") {
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, thinking: true, thought: d.content }
                : msg,
            ),
          );
        } else if (d.type === "done") {
          // FIX 2 — loading cleared here, after the full stream
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, streaming: false, thinking: false }
                : msg,
            ),
          );
          setLoading(false);
        } else if (d.type === "error") {
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? {
                    ...msg,
                    content: "Error: " + d.message,
                    streaming: false,
                    thinking: false,
                  }
                : msg,
            ),
          );
          setLoading(false);
        }
      },

      onError: () => {
        if (!mountedRef.current) return;
        setMessages((m) => [
          ...m.slice(0, -1),
          {
            role: "assistant",
            content: "Connection to AI assistant failed. Please try again.",
            time,
          },
        ]);
        setLoading(false);
      },

      // FIX 3 — if WS closes unexpectedly mid-stream, clear loading state
      onClose: (e) => {
        if (!mountedRef.current) return;
        // Normal close codes (1000, 1001) happen after "done" — ignore.
        // Abnormal close (e.g. 1006 network drop) while still loading → reset.
        if (loading && e.code !== 1000 && e.code !== 1001) {
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, streaming: false, thinking: false }
                : msg,
            ),
          );
          setLoading(false);
        }
      },
    });

    wsRef.current = ws;
  }

  function upsertArtifact(nextArtifact, setter) {
    const normalized = normalizeArtifact(nextArtifact);
    setter((prev) => {
      const filtered = prev.filter((item) => item.artifact_id !== normalized.artifact_id);
      return [normalized, ...filtered].slice(0, 10);
    });
  }

  function togglePinLatest() {
    const latestAssistant = [...messages].reverse().find((msg) => msg.role === "assistant" && msg.content && !msg.thinking);
    if (!latestAssistant) return;
    const summary = messageToSummary(latestAssistant);
    const existing = pinnedAnswers.find((item) => item.content === summary.content);
    if (existing?.artifact_id) {
      rfpApi.deleteChatArtifact(rfpId, existing.artifact_id).then(() => {
        setPinnedAnswers((prev) => prev.filter((item) => item.artifact_id !== existing.artifact_id));
      }).catch(() => {});
      return;
    }

    rfpApi.createChatArtifact(rfpId, {
      artifact_type: "pin",
      content: summary.content,
      citations: summary.citations,
    }).then((r) => {
      if (!mountedRef.current) return;
      upsertArtifact(r.data, setPinnedAnswers);
    }).catch(() => {});
  }

  function saveLatestSummary() {
    const latestAssistant = [...messages].reverse().find((msg) => msg.role === "assistant" && msg.content && !msg.thinking);
    if (!latestAssistant) return;
    const summary = messageToSummary(latestAssistant);
    rfpApi.createChatArtifact(rfpId, {
      artifact_type: "summary",
      content: summary.content,
      citations: summary.citations,
    }).then((r) => {
      if (!mountedRef.current) return;
      upsertArtifact(r.data, setSavedSummaries);
    }).catch(() => {});
  }

  function exportCitations(msg) {
    if (!msg?.citations?.length) return;
    const rows = msg.citations.map((citation) => ({
      source: citation.source,
      page_number: citation.page_number,
      excerpt: citation.excerpt,
      deep_link: citation.deep_link,
    }));
    const blob = new Blob([JSON.stringify(rows, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `rfp-${rfpId}-citations.json`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  const promptSets = {
    core: BASE_PROMPTS,
    compare: compareData?.ranking?.length >= 2
      ? [
          `Compare ${compareData.ranking[0]} vs ${compareData.ranking[1]} on strengths, risks, and likely winner.`,
          `Which vendor has the clearest security advantage?`,
          `What are the biggest deltas in the vendor comparison table?`,
        ]
      : [],
  };

  const currentVendorNames = compareData?.vendors || [];

  const compareQuickPrompt = currentVendorNames.length >= 2
    ? `Compare ${currentVendorNames[0]} vs ${currentVendorNames[1]} and summarize the decision trade-offs.`
    : "Compare the top two vendors and summarize the decision trade-offs.";

  if (!rfpId) return null;

  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col items-end">
      {/* Chat Window */}
      {isOpen && (
        <div className="w-[400px] h-[600px] bg-white rounded-2xl shadow-2xl border border-steel flex flex-col overflow-hidden mb-4 animate-fade-in origin-bottom-right">
          {/* Header */}
          <div className="flex-shrink-0 px-4 py-3 bg-ink text-white flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="w-6 h-6 bg-crimson rounded flex items-center justify-center">
                <FileText size={12} />
              </div>
              <span className="font-display text-sm tracking-wide">RFP ASSISTANT</span>
            </div>
            <div className="flex items-center gap-1">
              <button
                onClick={() => setMessages([welcomeMessage])}
                className="p-1.5 hover:bg-white/10 rounded-md text-white/60 transition-colors"
              >
                <RotateCcw size={14} />
              </button>
              <button
                onClick={() => setIsOpen(false)}
                className="p-1.5 hover:bg-white/10 rounded-md text-white transition-colors"
              >
                <Minimize2 size={14} />
              </button>
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4 bg-ivory/30">
            {messages.map((m, i) => (
              <Message key={i} msg={m} />
            ))}
            {loading && messages[messages.length - 1]?.role !== "assistant" && (
              <div className="flex gap-3">
                <div className="w-7 h-7 rounded-full bg-crimson flex items-center justify-center">
                  <Loader2 size={13} className="text-white animate-spin" />
                </div>
                <div className="bg-white border border-steel rounded-2xl rounded-tl-sm px-4 py-3">
                  <div className="flex gap-1">
                    {[0, 1, 2].map((i) => (
                      <div
                        key={i}
                        className="w-1.5 h-1.5 rounded-full bg-crimson/40 animate-bounce"
                        style={{ animationDelay: `${i * 150}ms` }}
                      />
                    ))}
                  </div>
                </div>
              </div>
            )}
            <div ref={endRef} />
          </div>

          {/* Suggested */}
          {messages.length === 1 && (
            <div className="flex-shrink-0 px-4 pb-3 bg-ivory/30">
              <div className="flex items-center justify-between mb-2">
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">Reusable prompts</div>
                <button
                  onClick={() => send(compareQuickPrompt)}
                  className="text-[10px] font-mono uppercase tracking-widest text-crimson hover:underline inline-flex items-center gap-1"
                  title="Generate a compare-vendors workflow prompt"
                >
                  <Sparkles size={10} /> Compare vendors
                </button>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {promptSets.core.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="text-[11px] bg-white border border-steel rounded-full px-2.5 py-1 text-ink hover:border-crimson hover:text-crimson transition-colors font-body shadow-sm"
                  >
                    {s}
                  </button>
                ))}
                {promptSets.compare.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="text-[11px] bg-emerald-50 border border-emerald-200 rounded-full px-2.5 py-1 text-emerald-800 hover:border-emerald-400 hover:text-emerald-900 transition-colors font-body shadow-sm"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Input */}
          <div className="flex-shrink-0 p-4 border-t border-steel bg-white">
            <div className="flex gap-2">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
                placeholder="Ask about vendors, gaps, scores…"
                className="flex-1 border border-steel rounded-xl px-3 py-2 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors"
              />
              <button
                onClick={() => send()}
                disabled={loading || !input.trim()}
                className="btn-primary w-10 h-10 flex items-center justify-center p-0 rounded-xl"
              >
                <Send size={16} />
              </button>
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                onClick={togglePinLatest}
                className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-crimson inline-flex items-center gap-1"
              >
                <Pin size={10} /> Pin latest answer
              </button>
              <button
                onClick={saveLatestSummary}
                className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-crimson inline-flex items-center gap-1"
              >
                <Bookmark size={10} /> Save summary
              </button>
              <button
                onClick={() => {
                  const latestAssistant = [...messages].reverse().find((msg) => msg.role === "assistant" && msg.citations?.length);
                  if (latestAssistant) exportCitations(latestAssistant);
                }}
                className="text-[10px] font-mono uppercase tracking-widest text-ink-muted hover:text-crimson inline-flex items-center gap-1"
              >
                <Download size={10} /> Export citations
              </button>
            </div>
            <div className="text-[10px] text-ink-muted font-mono mt-2 text-center">
              Answers are grounded in source documents · every claim includes a page
              citation
            </div>
          </div>

          {savedSummaries.length > 0 && (
            <div className="flex-shrink-0 px-4 pb-3 bg-ivory/30 border-t border-steel/60">
              <div className="flex items-center justify-between mb-2 mt-3">
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">Saved summaries</div>
                <button
                  onClick={() => setSavedSummaries([])}
                  className="text-[10px] font-mono text-ink-muted hover:text-crimson"
                >
                  Clear
                </button>
              </div>
              <div className="space-y-2 max-h-28 overflow-y-auto pr-1">
                {savedSummaries.slice(0, 3).map((summary, index) => (
                  <div key={summary.artifact_id || `${summary.savedAt}-${index}`} className="text-[11px] bg-white border border-steel rounded-lg px-3 py-2">
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <span className="font-mono uppercase tracking-widest text-ink-muted">{summary.time}</span>
                      <button
                        onClick={() => {
                          navigator.clipboard?.writeText(summary.content).catch(() => {});
                        }}
                        className="text-[10px] text-crimson hover:underline"
                      >
                        Copy
                      </button>
                    </div>
                    <div className="text-ink line-clamp-2">{summary.content}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Toggle Button */}
      <button
        onClick={() => setIsOpen(!isOpen)}
        className={clsx(
          "w-14 h-14 rounded-full shadow-lg flex items-center justify-center transition-all duration-300 hover:scale-110 active:scale-95",
          isOpen ? "bg-white text-ink border border-steel" : "bg-crimson text-white",
        )}
      >
        {isOpen ? <X size={24} /> : <MessageSquare size={24} />}
      </button>

      {pinnedAnswers.length > 0 && isOpen && (
        <div className="w-[400px] mb-3 bg-white rounded-2xl shadow-xl border border-steel p-3">
          <div className="flex items-center justify-between mb-2">
            <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted">Pinned answers</div>
            <div className="text-[10px] text-ink-muted">{pinnedAnswers.length}</div>
          </div>
          <div className="space-y-2 max-h-40 overflow-y-auto pr-1">
            {pinnedAnswers.map((item, index) => (
              <button
                key={item.artifact_id || `${item.savedAt}-${index}`}
                onClick={() => setInput(item.content)}
                className="w-full text-left bg-ivory/70 hover:bg-ivory border border-steel rounded-lg px-3 py-2 transition-colors"
              >
                <div className="text-[10px] font-mono uppercase tracking-widest text-ink-muted mb-1">Pinned</div>
                <div className="text-[11px] text-ink line-clamp-3">{item.content}</div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
