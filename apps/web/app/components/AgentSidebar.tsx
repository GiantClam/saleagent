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
    <aside style={{ borderRight: "1px solid #eee", padding: 16, height: "100%" }}>
      <button onClick={async () => {
        if (!apiUrl) return;
        await fetch(`${apiUrl}/jobs`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ slogan: '', user_id: userId }) });
        onNew();
      }} style={{ width: "100%", padding: 10, borderRadius: 8, border: "1px solid #ddd" }}>新建任务</button>
      <div style={{ marginTop: 16, color: "#666", fontSize: 12 }}>任务列表：
        <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
          <input value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} placeholder="搜索口号" style={{ flex: 1, padding: 8, border: "1px solid #ddd", borderRadius: 8 }} />
        </div>
        <ul style={{ paddingLeft: 16 }}>
          {jobs.slice(0,8).map((j, i) => (
            <li key={i}>
              {j.share_slug ? <a href={`/j/${j.share_slug}`}>{j.slogan || j.share_slug}</a> : (j.slogan || "任务")}
            </li>
          ))}
          {jobs.length === 0 && <li>暂无数据</li>}
        </ul>
        <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
          <button onClick={() => setPage((p) => Math.max(1, p - 1))} style={{ padding: 6, border: "1px solid #ddd", borderRadius: 6 }}>上一页</button>
          <span style={{ alignSelf: "center", color: "#666", fontSize: 12 }}>第 {page} 页</span>
          <button onClick={() => setPage((p) => p + 1)} style={{ padding: 6, border: "1px solid #ddd", borderRadius: 6 }}>下一页</button>
        </div>
      </div>
      <div style={{ marginTop: 16, color: "#666", fontSize: 12 }}>素材：图像</div>
    </aside>
  );
}


