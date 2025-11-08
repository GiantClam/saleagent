"use client";
import { useMemo, useState } from "react";

type InspectData = {
  request?: { prompt?: string; img?: string };
  lastEvent?: any;
  events?: any[];
};

export function AgentInspector({ data }: { data: InspectData }) {
  const req = data.request || {};
  const last = data.lastEvent || {};
  const events = data.events || [];
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    if (!q) return events;
    return events.filter((e) => JSON.stringify(e).toLowerCase().includes(q.toLowerCase()));
  }, [q, events]);

  function exportJson() {
    const blob = new Blob([JSON.stringify(events, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `events-${Date.now()}.json`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <aside style={{ borderLeft: "1px solid #eee", padding: 16, height: "100%", background: "#fafafa" }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>MiniMax 视窗（样式参考）</div>
      <div style={{ fontSize: 12, color: "#666" }}>Request</div>
      <div style={{ border: "1px solid #eee", borderRadius: 8, padding: 8, marginTop: 6, background: "#fff" }}>
        <div>prompt: {req.prompt || ""}</div>
        {req.img && <div>img: {req.img}</div>}
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索事件关键词" style={{ flex: 1, padding: 8, border: "1px solid #ddd", borderRadius: 8 }} />
        <button onClick={exportJson} style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>导出</button>
      </div>

      <div style={{ fontSize: 12, color: "#666", marginTop: 8 }}>Response（最近/筛选事件）</div>
      <div style={{ border: "1px solid #eee", borderRadius: 8, padding: 8, marginTop: 6, background: "#fff", maxHeight: 260, overflow: "auto" }}>
        <pre style={{ margin: 0, whiteSpace: "pre-wrap", wordBreak: "break-all" }}>{JSON.stringify(last, null, 2)}</pre>
      </div>

      <div style={{ fontSize: 12, color: "#666", marginTop: 8 }}>事件列表（{filtered.length}）</div>
      <div style={{ border: "1px solid #eee", borderRadius: 8, padding: 8, marginTop: 6, background: "#fff", maxHeight: 220, overflow: "auto" }}>
        {filtered.slice(-50).map((e, i) => (
          <div key={i} style={{ borderBottom: "1px dashed #eee", padding: "6px 0" }}>
            <div style={{ fontSize: 12, color: "#666" }}>{e.agent} · {e.type}</div>
            {e.delta && <div style={{ fontSize: 12 }}>{e.delta}</div>}
          </div>
        ))}
        {filtered.length === 0 && <div style={{ color: "#999" }}>无匹配事件</div>}
      </div>
    </aside>
  );
}


