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

export function AgUiChat({ slogan, imageUrl, runId, autoStart, onEvent, onEventsChange, onFinished }: { slogan: string; imageUrl?: string; runId?: string; autoStart?: boolean; onEvent?: (e: EventItem) => void; onEventsChange?: (arr: EventItem[]) => void; onFinished?: (runId: string) => void }) {
  const [events, setEvents] = useState<EventItem[]>([]);
  const [innerRunId, setInnerRunId] = useState<string>("");
  const [loading, setLoading] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const autoRef = useRef(false);

  const apiUrl = process.env.NEXT_PUBLIC_AGENT_URL;

  const canStart = useMemo(() => slogan.trim().length > 0 && !!apiUrl, [slogan, apiUrl]);

  const handleStart = useCallback(async () => {
    if (!canStart || !apiUrl) return;
    const thread_id = `t_${Date.now()}`;
    const newRunId = runId || `r_${Date.now()}`;
    setInnerRunId(newRunId);
    setEvents([]);
    setLoading(true);

    // 以 POST 拉起 SSE（后端将忽略 GET）
    const res = await fetch(`${apiUrl}/crewai-agent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: slogan, img: imageUrl || null, thread_id, run_id: newRunId })
    });

    if (!res.ok || !res.body) {
      setLoading(false);
      return;
    }

    // 手动读取 SSE（Server-Sent Events）流
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
          setEvents((prev) => {
            const next = [...prev, evt];
            onEventsChange?.(next);
            return next;
          });
          onEvent?.(evt);
          if (evt.type === "run_finished") {
            const slug = (evt as any)?.payload?.share_slug as string | undefined;
            onFinished?.(newRunId);
            if (slug) {
              // 额外触发一次基于 slug 的完成回调（向后兼容）
              try { (onFinished as any)?.({ runId: newRunId, shareSlug: slug }); } catch {}
            }
          }
        } catch {}
      }
    }
    setLoading(false);
  }, [apiUrl, imageUrl, slogan, canStart, runId, onEvent, onEventsChange, onFinished]);

  // 当传入 autoStart 时，自动触发一次开始
  useEffect(() => {
    if (autoStart && !autoRef.current && canStart && !loading) {
      autoRef.current = true;
      // 触发
      (async () => { await handleStart(); })();
    }
  }, [autoStart, canStart, loading, handleStart]);

  const progress = useMemo(() => {
    const last = [...events].reverse().find((e) => e.progress);
    return last?.progress || { current: 0, total: 0 };
  }, [events]);

  return (
    <section>
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
          width: "100%",
          marginBottom: 16
        }}
      >
        {loading ? "生成中…" : "开始生成"}
      </button>

      {progress.total > 0 && (
        <div style={{ marginBottom: 16, padding: 16, background: "#f9fafb", borderRadius: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
            <span style={{ fontSize: 14, fontWeight: 500, color: "#374151" }}>生成进度</span>
            <span style={{ fontSize: 14, color: "#6b7280" }}>{progress.current}/{progress.total}</span>
          </div>
          <div style={{ height: 8, background: "#e5e7eb", borderRadius: 999, overflow: "hidden" }}>
            <div
              style={{ 
                height: "100%", 
                background: "#4f46e5", 
                width: `${(progress.current / progress.total) * 100}%`, 
                borderRadius: 999,
                transition: "width 0.3s ease"
              }}
            />
          </div>
        </div>
      )}

      <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
        {events.length === 0 && (
          <div style={{ 
            padding: 24, 
            textAlign: "center", 
            color: "#9ca3af", 
            fontSize: 14,
            background: "#f9fafb",
            borderRadius: 8
          }}>
            等待开始生成...
          </div>
        )}
        {events.map((e, idx) => (
          <article 
            key={idx} 
            aria-live="polite" 
            style={{ 
              border: "1px solid #e5e7eb", 
              padding: 16, 
              borderRadius: 8,
              background: "white",
              boxShadow: "0 1px 2px rgba(0, 0, 0, 0.05)"
            }}
          >
            <div style={{ 
              fontSize: 12, 
              color: "#6b7280", 
              marginBottom: 8,
              fontWeight: 500,
              display: "flex",
              alignItems: "center",
              gap: 8
            }}>
              <span style={{ 
                padding: "2px 8px", 
                background: "#f3f4f6", 
                borderRadius: 4,
                fontSize: 11
              }}>
                {e.agent || "System"}
              </span>
              <span>·</span>
              <span>{e.type}</span>
            </div>
            {e.delta && (
              <div style={{ 
                marginTop: 8, 
                fontSize: 14, 
                color: "#1f2937",
                lineHeight: 1.6
              }}>
                {e.delta}
              </div>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}


