import { useState, useEffect, useRef } from "react";
import { useParams } from "react-router-dom";
import {
  Send,
  ExternalLink,
  FileText,
  User,
  Cpu,
  Loader2,
  RotateCcw,
} from "lucide-react";
import { rfpApi, getToken } from "../api/client";
import clsx from "clsx";

const SUGGESTED = [
  "Which vendor scored best on security requirements?",
  "What are the main gaps in the top vendor responses?",
  "Compare the technical architectures of all vendors",
  "Summarize the key strengths of the leading candidate",
];


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
      <div className={clsx("max-w-[78%]", isUser && "items-end flex flex-col")}>
        <div
          className={clsx(
            "rounded-2xl px-4 py-3 text-sm font-body leading-relaxed",
            isUser
              ? "bg-ink text-white rounded-tr-sm"
              : "bg-white border border-steel rounded-tl-sm text-ink",
          )}
        >
          {msg.content.split("\n").map((line, i) => {
            // Safe markdown: parse **text** to <strong> without XSS risk
            const parts = line.split(/(\*\*[^*]*\*\*)/g);
            return (
              <div key={i}>
                {parts.map((part, j) => {
                  if (part.startsWith("**") && part.endsWith("**")) {
                    return <strong key={j}>{part.slice(2, -2)}</strong>;
                  }
                  return part || "\u00A0"; // nbsp if empty
                })}
              </div>
            );
          })}
          {msg.thinking && (
            <div className="flex items-center gap-1 mt-2 text-crimson text-xs">
              <Loader2 size={12} className="animate-spin" />
              <span className="font-medium italic opacity-80">{msg.thought || "Agent searching..."}</span>
            </div>
          )}
          {msg.streaming && !msg.thinking && (
            <span className="inline-block w-1 h-3.5 bg-crimson ml-0.5 animate-pulse rounded-sm" />
          )}
        </div>
        {msg.citations?.map((c, i) => (
          <CitationCard key={i} citation={c} />
        ))}
        <div className="text-[10px] text-ink-muted mt-1 px-1 font-mono">
          {msg.time}
        </div>
      </div>
    </div>
  );
}


export default function ChatPage() {
  const { rfpId } = useParams();
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      content:
        "Hello! I'm your RFP evaluation assistant. I can answer questions about requirements, vendor scores, and gaps — always with evidence cited from the source documents.",
      citations: [],
      time: new Date().toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      }),
    },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const endRef = useRef(null);
  const wsRef = useRef(null);

  useEffect(() => {
    rfpApi
      .chatSession(rfpId, {})
      .then((r) => {
        const sid = r.data.session_id;
        setSessionId(sid);
        // Load history
        rfpApi
          .chatHistory(rfpId, sid)
          .then((hist) => {
            if (hist.data?.messages?.length > 0) {
              setMessages([
                messages[0], // Keep welcome
                ...hist.data.messages.map((m) => ({
                  ...m,
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
        const detail = err?.response?.data?.detail;
        const msg =
          typeof detail === "string"
            ? detail
            : "Could not start chat. Sign in again or wait until the RFP status is Ready.";
        setMessages((prev) => [
          prev[0],
          { role: "assistant", content: msg, citations: [], time: "Now" },
        ]);
      });
    return () => wsRef.current?.close();
  }, [rfpId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send(text) {
    const q = (text || input).trim();
    if (!q || loading) return;
    setInput("");
    const time = new Date().toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    setMessages((m) => [
      ...m,
      { role: "user", content: q, citations: [], time },
    ]);
    setLoading(true);

    if (sessionId) {
      // Dynamically construct WebSocket URL to work in Docker and production
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const host = window.location.host;
      const ws = new WebSocket(`${protocol}//${host}/ws/chat/${sessionId}?token=${encodeURIComponent(getToken() || "")}`);
      wsRef.current = ws;
      let buf = "";
      let firstTokenReceived = false;

      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: "",
          citations: [],
          thinking: true,
          streaming: false,
          time,
        },
      ]);

      ws.onmessage = (e) => {
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
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, citations: d.data, streaming: false }
                : msg,
            ),
          );
          setLoading(false);
        } else if (d.type === "thought") {
           setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, thinking: true, thought: d.content }
                : msg,
            ),
          );
        } else if (d.type === "done") {
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1 ? { ...msg, streaming: false, thinking: false } : msg,
            ),
          );
          setLoading(false);
        } else if (d.type === "error") {
          setMessages((m) =>
            m.map((msg, i) =>
              i === m.length - 1
                ? { ...msg, content: "Error: " + d.message, streaming: false, thinking: false }
                : msg,
            ),
          );
          setLoading(false);
        }
      };
      ws.onopen = () =>
        ws.send(JSON.stringify({ type: "question", content: q }));
      ws.onerror = () => {
        setMessages((m) => [
          ...m.slice(0, -1),
          { role: "assistant", content: "Connection to AI assistant failed. Please try again later.", time }
        ]);
        setLoading(false);
      };
    } else {
      setMessages((m) => [
        ...m,
        { role: "assistant", content: "Could not establish a chat session. Please ensure you have selected a valid RFP.", time }
      ]);
      setLoading(false);
    }
  }


  return (
    <div className="h-screen flex flex-col">
      {/* Header */}
      <div className="flex-shrink-0 px-6 py-4 border-b border-steel bg-white flex items-center justify-between">
        <div>
          <div className="font-display text-2xl text-ink tracking-wide">
            RFP ASSISTANT
          </div>
          <div className="text-xs text-ink-muted font-mono">
            RAG · {rfpId}
          </div>
        </div>
        <button
          onClick={() => setMessages((m) => m.slice(0, 1))}
          className="btn-ghost text-xs py-1.5 px-3 flex items-center gap-1.5"
        >
          <RotateCcw size={12} /> Clear
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
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
        <div className="flex-shrink-0 px-6 pb-2">
          <div className="text-xs text-ink-muted font-medium uppercase tracking-wider mb-2">
            Suggested questions
          </div>
          <div className="flex flex-wrap gap-2">
            {SUGGESTED.map((s) => (
              <button
                key={s}
                onClick={() => send(s)}
                className="text-xs bg-white border border-steel rounded-full px-3 py-1.5 text-ink hover:border-crimson hover:text-crimson transition-colors font-body"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Input */}
      <div className="flex-shrink-0 px-6 py-4 border-t border-steel bg-white">
        <div className="flex gap-3">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
            placeholder="Ask about vendors, requirements, CVs, scores…"
            disabled={loading}
            className="flex-1 border border-steel rounded-xl px-4 py-2.5 text-sm font-body bg-white focus:outline-none focus:border-crimson transition-colors disabled:opacity-50"
          />
          <button
            onClick={() => send()}
            disabled={loading || !input.trim()}
            className="btn-primary px-4 py-2.5 flex items-center gap-2 text-sm"
          >
            <Send size={15} />
          </button>
        </div>
        <div className="text-[10px] text-ink-muted font-mono mt-2 text-center">
          Answers are grounded in source documents · every claim includes a page
          citation
        </div>
      </div>
    </div>
  );
}
