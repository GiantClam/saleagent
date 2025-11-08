"use client";
import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

export default function ProfilePage() {
  const supabase = (typeof window !== 'undefined' && process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    ? createClient(process.env.NEXT_PUBLIC_SUPABASE_URL, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    : null as any;
  const [user, setUser] = useState<any>(null);
  const [jobs, setJobs] = useState<any[]>([]);

  useEffect(() => {
    (async () => {
      if (!supabase) return;
      const { data } = await supabase.auth.getUser();
      setUser(data.user || null);
      if (data.user && process.env.NEXT_PUBLIC_AGENT_URL) {
        const url = new URL(`${process.env.NEXT_PUBLIC_AGENT_URL}/my-jobs`);
        url.searchParams.set('user_id', data.user.id);
        const list = await fetch(url.toString()).then(r => r.json()).catch(() => []);
        setJobs(list);
      }
    })();
  }, []);

  if (!user) {
    return (
      <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
        <h1 style={{ fontSize: 20, fontWeight: 700 }}>个人中心</h1>
        <p style={{ color: '#666' }}>尚未登录，请先在右上角使用 Google/GitHub 登录。</p>
      </main>
    );
  }

  return (
    <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
      <h1 style={{ fontSize: 20, fontWeight: 700 }}>个人中心</h1>
      <div style={{ marginTop: 12 }}>
        <div>用户 ID：{user.id}</div>
        <div>邮箱：{user.email || '（未提供）'}</div>
      </div>
      <div style={{ marginTop: 20 }}>
        <div style={{ fontWeight: 600 }}>我的任务</div>
        <ul>
          {jobs.map((j) => (
            <li key={j.run_id}>
              [{j.status}] {j.slogan || j.run_id} — {j.share_slug ? <a href={`/j/${j.share_slug}`}>查看</a> : <span>处理中</span>}
              <button onClick={async () => {
                if (!process.env.NEXT_PUBLIC_AGENT_URL) return;
                const r = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs/${j.run_id}/retry`, { method: 'POST' }).then(r => r.json()).catch(() => null);
                if (r?.slogan) window.location.href = `/?s=${encodeURIComponent(r.slogan || '')}${r.cover_url ? `&img=${encodeURIComponent(r.cover_url)}` : ''}&run=${encodeURIComponent(r.run_id)}`;
              }} style={{ marginLeft: 8, padding: 4, border: '1px solid #ddd', borderRadius: 6 }}>重试</button>
            </li>
          ))}
          {jobs.length === 0 && <li>暂无任务</li>}
        </ul>
      </div>
    </main>
  );
}


