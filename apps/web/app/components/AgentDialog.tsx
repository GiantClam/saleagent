"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type EventItem = {
  thread_id: string;
  run_id: string;
  agent?: string;
  type: string;
  delta?: string | null;
  payload?: any;
  progress?: { current: number; total: number };
  ts?: number;
};

export function AgentDialog({ 
  slogan, 
  imageUrl, 
  runId, 
  autoStart, 
  onEvent, 
  onEventsChange, 
  onFinished 
}: { 
  slogan: string; 
  imageUrl?: string; 
  runId?: string; 
  autoStart?: boolean; 
  onEvent?: (e: EventItem) => void; 
  onEventsChange?: (arr: EventItem[]) => void; 
  onFinished?: (runId: string) => void;
}) {
  const [events, setEvents] = useState<EventItem[]>([]);
  const [innerRunId, setInnerRunId] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const [requiresConfirm, setRequiresConfirm] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const esRef = useRef<EventSource | null>(null);
  const autoRef = useRef(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const apiUrl = process.env.NEXT_PUBLIC_AGENT_URL;

  // 初始化时从 slogan 设置 inputValue
  useEffect(() => {
    if (slogan && !inputValue) {
      setInputValue(slogan);
    }
  }, [slogan]);

  const canStart = useMemo(() => (slogan.trim().length > 0 || inputValue.trim().length > 0) && !!apiUrl, [slogan, inputValue, apiUrl]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [events]);

  const handleStart = useCallback(async () => {
    const prompt = inputValue.trim() || slogan.trim();
    if (!prompt || !apiUrl) return;
    
    const thread_id = `t_${Date.now()}`;
    const newRunId = runId || `r_${Date.now()}`;
    setInnerRunId(newRunId);
    setEvents([]);
    setLoading(true);
    setInputValue("");

    const res = await fetch(`${apiUrl}/crewai-agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt, img: imageUrl || null, thread_id, run_id: newRunId })
    });

    if (!res.ok || !res.body) {
      setLoading(false);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() || "";
      for (const part of parts) {
        const line = part.split("data:").pop()?.trim();
        if (!line) continue;
        try {
          const evt = JSON.parse(line) as EventItem;
          try { console.log("[SSE/crewai-agent]", evt); } catch {}
          setEvents((prev) => {
            const next = [...prev, evt];
            onEventsChange?.(next);
            return next;
          });
          onEvent?.(evt);
          if (evt.type === "run_finished") {
            const payload = (evt as any)?.payload || {};
            const code = (payload?.code as string | undefined) || "";
            if (code === "confirmation_required") {
              setRequiresConfirm(true);
            } else {
              const slug = payload?.share_slug as string | undefined;
              onFinished?.(newRunId);
              if (slug) {
                try { (onFinished as any)?.({ runId: newRunId, shareSlug: slug }); } catch {}
              }
            }
          }
        } catch {}
      }
    }
    setLoading(false);
  }, [apiUrl, imageUrl, slogan, canStart, runId, onEvent, onEventsChange, onFinished, inputValue]);

  useEffect(() => {
    if (autoStart && !autoRef.current && canStart && !loading) {
      autoRef.current = true;
      (async () => { await handleStart(); })();
    }
  }, [autoStart, canStart, loading, handleStart]);

  const progress = useMemo(() => {
    const last = [...events].reverse().find((e) => e.progress);
    return last?.progress || { current: 0, total: 0 };
  }, [events]);

  // 按 agent 分组消息
  const groupedMessages = useMemo(() => {
    const groups: { agent: string; messages: EventItem[] }[] = [];
    let currentGroup: { agent: string; messages: EventItem[] } | null = null;

    events.forEach((e) => {
      const agent = e.agent || "System";
      if (!currentGroup || currentGroup.agent !== agent) {
        currentGroup = { agent, messages: [] };
        groups.push(currentGroup);
      }
      currentGroup.messages.push(e);
    });

    return groups;
  }, [events]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* 消息区域 */}
      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {events.length === 0 && (
          <div style={{ 
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            color: "#9ca3af",
            fontSize: 14
          }}>
            <div style={{ textAlign: "center" }}>
              <div style={{ fontSize: 48, marginBottom: 16 }}>💬</div>
              <div>开始与 AI 智能体对话</div>
            </div>
          </div>
        )}

        {groupedMessages.map((group, groupIdx) => (
          <div key={groupIdx} style={{ marginBottom: 24 }}>
            <div style={{ 
              display: "flex",
              alignItems: "center",
              gap: 8,
              marginBottom: 12
            }}>
              <div style={{
                width: 32,
                height: 32,
                borderRadius: "50%",
                background: "#e0e7ff",
                color: "#4338ca",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 14,
                fontWeight: 600
              }}>
                {group.agent.charAt(0).toUpperCase()}
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#1f2937" }}>
                {group.agent}
              </div>
            </div>
            <div style={{ marginLeft: 40 }}>
              {group.messages.map((e, idx) => (
                <div 
                  key={idx}
                  style={{
                    marginBottom: 8,
                    padding: 12,
                    background: "#f9fafb",
                    borderRadius: 8,
                    fontSize: 14,
                    color: "#1f2937",
                    lineHeight: 1.6
                  }}
                >
                  {e.delta && <div>{e.delta}</div>}
                  {e.type && !e.delta && (
                    <div style={{ fontSize: 12, color: "#6b7280" }}>{e.type}</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        ))}

        {loading && (
          <div style={{ 
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: 12,
            color: "#6b7280",
            fontSize: 14
          }}>
            <div style={{
              width: 20,
              height: 20,
              border: "2px solid #e5e7eb",
              borderTopColor: "#4f46e5",
              borderRadius: "50%",
              animation: "spin 1s linear infinite"
            }} />
            <span>智能体正在处理...</span>
          </div>
        )}

        {progress.total > 0 && (
          <div style={{ 
            marginTop: 16,
            padding: 16,
            background: "#f0f9ff",
            borderRadius: 8,
            border: "1px solid #bae6fd"
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
              <span style={{ fontSize: 13, fontWeight: 500, color: "#0369a1" }}>执行进度</span>
              <span style={{ fontSize: 13, color: "#0284c7" }}>{progress.current}/{progress.total}</span>
            </div>
            <div style={{ height: 6, background: "#e0f2fe", borderRadius: 999, overflow: "hidden" }}>
              <div
                style={{ 
                  height: "100%", 
                  background: "#0ea5e9", 
                  width: `${(progress.current / progress.total) * 100}%`, 
                  borderRadius: 999,
                  transition: "width 0.3s ease"
                }}
              />
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* 输入区域 */}
      <div style={{ 
        borderTop: "1px solid #e5e7eb",
        padding: 16,
        background: "white"
      }}>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyPress={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                handleStart();
              }
            }}
            placeholder="输入你的需求..."
            disabled={loading}
            style={{ 
              flex: 1,
              padding: "12px 16px", 
              border: "1px solid #d1d5db", 
              borderRadius: 8,
              fontSize: 14,
              background: "white"
            }}
          />
          <button
            onClick={handleStart}
            disabled={!canStart || loading}
            style={{ 
              padding: "12px 24px", 
              borderRadius: 8, 
              background: loading ? "#9ca3af" : "#4f46e5", 
              color: "#fff", 
              border: 0,
              fontSize: 14,
              fontWeight: 600,
              cursor: loading ? "not-allowed" : "pointer"
            }}
          >
            {loading ? "处理中" : "发送"}
          </button>
        </div>
      </div>

      <style jsx>{`
        @keyframes spin {
          to { transform: rotate(360deg); }
        }
      `}</style>
    </div>
  );
}

