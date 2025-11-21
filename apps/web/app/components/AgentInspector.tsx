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
    <aside style={{ 
      borderLeft: "1px solid #e5e7eb", 
      padding: 20, 
      height: "100%", 
      background: "white",
      overflow: "auto"
    }}>
      <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 20, color: "#1f2937" }}>请求/响应调试</h3>
      
      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 500, color: "#374151", marginBottom: 8 }}>请求参数</div>
        <div style={{ 
          border: "1px solid #e5e7eb", 
          borderRadius: 8, 
          padding: 12, 
          background: "#f9fafb",
          fontSize: 13
        }}>
          <div style={{ marginBottom: 6 }}>
            <span style={{ color: "#6b7280", fontWeight: 500 }}>prompt:</span>{" "}
            <span style={{ color: "#1f2937" }}>{req.prompt || "(空)"}</span>
          </div>
          {req.img && (
            <div>
              <span style={{ color: "#6b7280", fontWeight: 500 }}>img:</span>{" "}
              <span style={{ color: "#1f2937", wordBreak: "break-all" }}>{req.img}</span>
            </div>
          )}
        </div>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <input 
            value={q} 
            onChange={(e) => setQ(e.target.value)} 
            placeholder="搜索事件关键词..." 
            style={{ 
              flex: 1, 
              padding: "10px 12px", 
              border: "1px solid #d1d5db", 
              borderRadius: 8,
              fontSize: 13,
              background: "white"
            }} 
          />
          <button 
            onClick={exportJson} 
            style={{ 
              padding: "10px 16px", 
              border: "1px solid #d1d5db", 
              borderRadius: 8,
              background: "white",
              fontSize: 13,
              fontWeight: 500
            }}
          >
            导出
          </button>
        </div>
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ fontSize: 13, fontWeight: 500, color: "#374151", marginBottom: 8 }}>
          最近事件响应
        </div>
        <div style={{ 
          border: "1px solid #e5e7eb", 
          borderRadius: 8, 
          padding: 12, 
          marginTop: 6, 
          background: "#f9fafb", 
          maxHeight: 280, 
          overflow: "auto"
        }}>
          <pre style={{ 
            margin: 0, 
            whiteSpace: "pre-wrap", 
            wordBreak: "break-all",
            fontSize: 12,
            color: "#1f2937",
            fontFamily: "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
          }}>
            {Object.keys(last).length > 0 ? JSON.stringify(last, null, 2) : "暂无数据"}
          </pre>
        </div>
      </div>

      <div>
        <div style={{ fontSize: 13, fontWeight: 500, color: "#374151", marginBottom: 8 }}>
          事件列表 <span style={{ color: "#6b7280", fontWeight: 400 }}>({filtered.length})</span>
        </div>
        <div style={{ 
          border: "1px solid #e5e7eb", 
          borderRadius: 8, 
          padding: 12, 
          marginTop: 6, 
          background: "#f9fafb", 
          maxHeight: 300, 
          overflow: "auto"
        }}>
          {filtered.slice(-50).map((e, i) => (
            <div 
              key={i} 
              style={{ 
                borderBottom: i < filtered.slice(-50).length - 1 ? "1px solid #e5e7eb" : "none", 
                padding: "10px 0",
                fontSize: 12
              }}
            >
              <div style={{ 
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginBottom: 4
              }}>
                <span style={{ 
                  padding: "2px 6px",
                  background: "#e0e7ff",
                  color: "#4338ca",
                  borderRadius: 4,
                  fontSize: 11,
                  fontWeight: 500
                }}>
                  {e.agent || "System"}
                </span>
                <span style={{ color: "#6b7280" }}>·</span>
                <span style={{ color: "#6b7280" }}>{e.type}</span>
              </div>
              {e.delta && (
                <div style={{ 
                  color: "#1f2937",
                  marginTop: 6,
                  lineHeight: 1.5
                }}>
                  {e.delta}
                </div>
              )}
            </div>
          ))}
          {filtered.length === 0 && (
            <div style={{ 
              padding: "20px", 
              textAlign: "center", 
              color: "#9ca3af", 
              fontSize: 13 
            }}>
              无匹配事件
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}


