"use client";
import { useMemo, useState } from "react";

type InspectData = {
  request?: { prompt?: string; img?: string };
  lastEvent?: any;
  events?: any[];
};

export function AgentExecution({ data }: { data: InspectData }) {
  const req = data.request || {};
  const last = data.lastEvent || {};
  const events = data.events || [];
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    if (!q) return events;
    return events.filter((e) => JSON.stringify(e).toLowerCase().includes(q.toLowerCase()));
  }, [q, events]);

  // 提取执行步骤
  const executionSteps = useMemo(() => {
    const steps: Array<{ agent: string; type: string; status: "pending" | "running" | "completed"; delta?: string; timestamp?: number }> = [];
    
    events.forEach((e) => {
      if (e.type === "agent_start" || e.type === "task_start") {
        steps.push({
          agent: e.agent || "System",
          type: e.type,
          status: "running",
          delta: e.delta,
          timestamp: e.ts
        });
      } else if (e.type === "agent_end" || e.type === "task_end" || e.type === "run_finished") {
        const lastStep = steps[steps.length - 1];
        if (lastStep) {
          lastStep.status = "completed";
        }
      }
    });

    return steps;
  }, [events]);

  function exportJson() {
    const blob = new Blob([JSON.stringify(events, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `events-${Date.now()}.json`; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div style={{ 
      height: "100%",
      display: "flex",
      flexDirection: "column",
      background: "white"
    }}>
      {/* 头部 */}
      <div style={{ 
        padding: 20,
        borderBottom: "1px solid #e5e7eb",
        background: "white"
      }}>
        <h3 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16, color: "#1f2937" }}>
          执行过程
        </h3>
        <div style={{ display: "flex", gap: 8 }}>
          <input 
            value={q} 
            onChange={(e) => setQ(e.target.value)} 
            placeholder="搜索事件..." 
            style={{ 
              flex: 1, 
              padding: "8px 12px", 
              border: "1px solid #d1d5db", 
              borderRadius: 6,
              fontSize: 13,
              background: "white"
            }} 
          />
          <button 
            onClick={exportJson} 
            style={{ 
              padding: "8px 16px", 
              border: "1px solid #d1d5db", 
              borderRadius: 6,
              background: "white",
              fontSize: 13,
              fontWeight: 500
            }}
          >
            导出
          </button>
        </div>
      </div>

      {/* 执行步骤列表 */}
      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {executionSteps.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {executionSteps.map((step, idx) => (
              <div 
                key={idx}
                style={{
                  display: "flex",
                  gap: 12,
                  padding: 16,
                  background: step.status === "completed" ? "#f0fdf4" : step.status === "running" ? "#fef3c7" : "#f9fafb",
                  border: `1px solid ${step.status === "completed" ? "#bbf7d0" : step.status === "running" ? "#fde68a" : "#e5e7eb"}`,
                  borderRadius: 8,
                  position: "relative"
                }}
              >
                {/* 步骤指示器 */}
                <div style={{
                  width: 24,
                  height: 24,
                  borderRadius: "50%",
                  background: step.status === "completed" ? "#10b981" : step.status === "running" ? "#f59e0b" : "#d1d5db",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "white",
                  fontSize: 12,
                  fontWeight: 600,
                  flexShrink: 0
                }}>
                  {step.status === "completed" ? "✓" : step.status === "running" ? "⟳" : idx + 1}
                </div>

                {/* 步骤内容 */}
                <div style={{ flex: 1 }}>
                  <div style={{ 
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    marginBottom: 6
                  }}>
                    <span style={{ 
                      fontSize: 13,
                      fontWeight: 600,
                      color: "#1f2937"
                    }}>
                      {step.agent}
                    </span>
                    <span style={{ color: "#9ca3af" }}>·</span>
                    <span style={{ 
                      fontSize: 12,
                      color: "#6b7280"
                    }}>
                      {step.type}
                    </span>
                    {step.status === "running" && (
                      <span style={{
                        padding: "2px 8px",
                        background: "#fef3c7",
                        color: "#92400e",
                        borderRadius: 4,
                        fontSize: 11,
                        fontWeight: 500
                      }}>
                        执行中
                      </span>
                    )}
                  </div>
                  {step.delta && (
                    <div style={{ 
                      fontSize: 13,
                      color: "#374151",
                      lineHeight: 1.5,
                      marginTop: 4
                    }}>
                      {step.delta}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div style={{ 
            padding: 40,
            textAlign: "center",
            color: "#9ca3af",
            fontSize: 14
          }}>
            <div style={{ fontSize: 48, marginBottom: 16 }}>⚙️</div>
            <div>等待任务开始...</div>
          </div>
        )}

        {/* 最近事件 */}
        {filtered.length > 0 && (
          <div style={{ marginTop: 24 }}>
            <div style={{ fontSize: 13, fontWeight: 500, color: "#374151", marginBottom: 12 }}>
              事件日志 <span style={{ color: "#6b7280", fontWeight: 400 }}>({filtered.length})</span>
            </div>
            <div style={{ 
              border: "1px solid #e5e7eb", 
              borderRadius: 8, 
              padding: 12, 
              background: "#f9fafb", 
              maxHeight: 200, 
              overflow: "auto"
            }}>
              {filtered.slice(-20).map((e, i) => (
                <div 
                  key={i} 
                  style={{ 
                    borderBottom: i < filtered.slice(-20).length - 1 ? "1px solid #e5e7eb" : "none", 
                    padding: "8px 0",
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
                      marginTop: 4,
                      lineHeight: 1.4
                    }}>
                      {e.delta}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

