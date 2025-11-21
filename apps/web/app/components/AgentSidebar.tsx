"use client";
import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

type Job = { share_slug?: string; slogan?: string; created_at?: string };

export function AgentSidebar({ onNew, apiUrl }: { onNew: () => void; apiUrl?: string }) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const limit = 20;
  const supabase = (typeof window !== 'undefined' && process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    ? createClient(process.env.NEXT_PUBLIC_SUPABASE_URL, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    : null as any;
  const [userId, setUserId] = useState<string | undefined>(undefined);

  useEffect(() => {
    (async () => {
      if (!supabase) return;
      const { data } = await supabase.auth.getUser();
      setUserId(data.user?.id);
    })();
    if (!apiUrl) return;
    const url = new URL(`${apiUrl}/public-jobs`);
    url.searchParams.set("page", String(page));
    url.searchParams.set("limit", String(limit));
    if (q) url.searchParams.set("q", q);
    fetch(url.toString()).then(r => r.json()).then(setJobs).catch(() => {});
  }, [apiUrl, page, q]);

  return (
    <aside style={{ 
      borderRight: "1px solid #e5e7eb", 
      height: "100%",
      background: "white",
      display: "flex",
      flexDirection: "column"
    }}>
      <div style={{ padding: 20, borderBottom: "1px solid #e5e7eb" }}>
        <button 
          onClick={async () => {
            if (!apiUrl) return;
            await fetch(`${apiUrl}/jobs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slogan: '', user_id: userId }) });
            onNew();
          }} 
          style={{ 
            width: "100%", 
            padding: "12px 16px", 
            borderRadius: 8, 
            border: "1px solid #d1d5db",
            background: "#4f46e5",
            color: "white",
            fontSize: 14,
            fontWeight: 600
          }}
        >
          + 新建任务
        </button>
      </div>

      {/* 任务列表区域 - 可滚动 */}
      <div style={{ flex: 1, overflow: "auto", padding: "20px" }}>
        <h3 style={{ fontSize: 14, fontWeight: 600, color: "#1f2937", marginBottom: 12 }}>任务列表</h3>
        <div style={{ marginBottom: 12 }}>
          <input 
            value={q} 
            onChange={(e) => { setQ(e.target.value); setPage(1); }} 
            placeholder="搜索任务..." 
            style={{ 
              width: "100%",
              padding: "10px 12px", 
              border: "1px solid #d1d5db", 
              borderRadius: 8,
              fontSize: 13,
              background: "white"
            }} 
          />
        </div>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {jobs.slice(0, 10).map((j, i) => (
            <li key={i} style={{ marginBottom: 8 }}>
              {j.share_slug ? (
                <a 
                  href={`/j/${j.share_slug}`}
                  style={{ 
                    display: "block",
                    padding: "12px",
                    borderRadius: 8,
                    fontSize: 13,
                    color: "#374151",
                    background: "#f9fafb",
                    transition: "all 0.2s ease",
                    border: "1px solid #e5e7eb"
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.background = "#f3f4f6";
                    e.currentTarget.style.borderColor = "#d1d5db";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.background = "#f9fafb";
                    e.currentTarget.style.borderColor = "#e5e7eb";
                  }}
                >
                  <div style={{ fontWeight: 500, marginBottom: 4 }}>{j.slogan || j.share_slug}</div>
                  {j.created_at && (
                    <div style={{ fontSize: 11, color: "#9ca3af" }}>
                      {new Date(j.created_at).toLocaleDateString()}
                    </div>
                  )}
                </a>
              ) : (
                <div style={{ 
                  padding: "12px",
                  fontSize: 13,
                  color: "#9ca3af",
                  background: "#f9fafb",
                  borderRadius: 8
                }}>
                  {j.slogan || "任务"}
                </div>
              )}
            </li>
          ))}
          {jobs.length === 0 && (
            <li style={{ padding: "40px", textAlign: "center", color: "#9ca3af", fontSize: 13 }}>
              <div style={{ fontSize: 32, marginBottom: 8 }}>📋</div>
              <div>暂无任务</div>
            </li>
          )}
        </ul>
        {jobs.length > 0 && (
          <div style={{ display: "flex", gap: 8, marginTop: 16, alignItems: "center", justifyContent: "center" }}>
            <button 
              onClick={() => setPage((p) => Math.max(1, p - 1))} 
              disabled={page === 1}
              style={{ 
                padding: "6px 12px", 
                border: "1px solid #d1d5db", 
                borderRadius: 6,
                background: "white",
                fontSize: 12,
                fontWeight: 500
              }}
            >
              上一页
            </button>
            <span style={{ color: "#6b7280", fontSize: 12 }}>第 {page} 页</span>
            <button 
              onClick={() => setPage((p) => p + 1)} 
              style={{ 
                padding: "6px 12px", 
                border: "1px solid #d1d5db", 
                borderRadius: 6,
                background: "white",
                fontSize: 12,
                fontWeight: 500
              }}
            >
              下一页
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}


