"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { AgUiChat } from "../components/AgUiChat";
import { AgentSidebar } from "../components/AgentSidebar";
import { AgentInspector } from "../components/AgentInspector";
import { useRouter } from "next/navigation";
import { useSearchParams } from "next/navigation";

export default function Home() {
  const [slogan, setSlogan] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [lastEvent, setLastEvent] = useState<any>(null);
  const [allEvents, setAllEvents] = useState<any[]>([]);
  const [runId, setRunId] = useState<string | undefined>(undefined);
  const router = useRouter();
  const search = useSearchParams();
  const [autoPoll, setAutoPoll] = useState(false);
  const [notify, setNotify] = useState(false);
  const [autoStart, setAutoStart] = useState(false);

  useEffect(() => {
    const s = search.get("s");
    if (s) setSlogan(s);
    const img = search.get("img");
    if (img) setImageUrl(img);
    const r = search.get("run");
    if (r) { setRunId(r); setAutoStart(true); }
  }, [search]);

  useEffect(() => {
    if (!autoPoll || !runId || !process.env.NEXT_PUBLIC_AGENT_URL) return;
    const timer = setInterval(async () => {
      const j = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs/${runId}`).then(r => r.json()).catch(() => null);
      if (j?.status === 'succeeded' && j?.share_slug) {
        clearInterval(timer);
        router.push(`/j/${j.share_slug}`);
      }
    }, 10000);
    return () => clearInterval(timer);
  }, [autoPoll, runId]);

  return (
    <div style={{ display: "grid", gridTemplateColumns: "260px 1fr 360px", height: "100vh" }}>
      <AgentSidebar apiUrl={process.env.NEXT_PUBLIC_AGENT_URL} onNew={async () => {
        setLastEvent(null); setAllEvents([]);
        if (!process.env.NEXT_PUBLIC_AGENT_URL) return;
        const r = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ slogan })
        }).then(r => r.json()).catch(() => null);
        if (r?.run_id) setRunId(r.run_id);
      }} />
      <main style={{ padding: 24, overflow: "auto" }}>
        <script dangerouslySetInnerHTML={{ __html: `function jobIcon(){return 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2248%22 height=%2248%22 viewBox=%220 0 24 24%22 fill=%22%23000%22%3E%3Cpath d=%22M14 2H6a2 2 0 0 0-2 2v3h2V4h8V2zm4 5V4a2 2 0 0 0-2-2h-2v2h2v3h2zM4 9h16v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9zm8 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z%22/%3E%3C/svg%3E';}` }} />
        <h1 style={{ fontSize: 18, fontWeight: 700 }}>对 nanosoravideo.com 进行分析，如何补充素材、提示 SEO、优化性能</h1>
        <p style={{ color: "#666", marginTop: 8 }}>样式参考 MiniMax Agent 工作台。输入口号与参考图，观察实时过程与右侧请求/响应。</p>

        <div style={{ display: "grid", gap: 12, marginTop: 16, gridTemplateColumns: "1fr 1fr" }}>
          <input
            placeholder="例如：3C新品发布：轻薄长续航"
            value={slogan}
            onChange={(e) => setSlogan(e.target.value)}
            style={{ padding: 10, border: "1px solid #ddd", borderRadius: 8 }}
          />
          <input
            placeholder="参考图 URL（可选）"
            value={imageUrl}
            onChange={(e) => setImageUrl(e.target.value)}
            style={{ padding: 10, border: "1px solid #ddd", borderRadius: 8 }}
          />
        </div>

        <AgUiChat slogan={slogan} imageUrl={imageUrl} runId={runId} autoStart={autoStart} onEvent={(e) => setLastEvent(e)} onEventsChange={(arr) => setAllEvents(arr)} onFinished={async (info: any) => {
          const rid = typeof info === 'string' ? info : info?.runId;
          const slug = typeof info === 'object' ? info?.shareSlug : undefined;
          if (slug) { router.push(`/j/${slug}`); return; }
          if (!process.env.NEXT_PUBLIC_AGENT_URL || !rid) return;
          const j = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs/${rid}`).then(r => r.json()).catch(() => null);
          if (j?.share_slug) {
            if (notify && "Notification" in window && Notification.permission === 'granted') {
              new Notification('生成完成', { body: slogan || '视频已生成，点击查看', icon: jobIcon() });
            }
            router.push(`/j/${j.share_slug}`);
          }
        }} />

        {runId && (
          <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
            <button onClick={async () => {
              if (!process.env.NEXT_PUBLIC_AGENT_URL) return;
              const j = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs/${runId}`).then(r => r.json()).catch(() => null);
              if (j?.share_slug) router.push(`/j/${j.share_slug}`);
            }} style={{ padding: 8, border: '1px solid #ddd', borderRadius: 8 }}>检查任务状态</button>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <input type="checkbox" checked={autoPoll} onChange={(e) => setAutoPoll(e.target.checked)} />
              10 秒自动轮询
            </label>
          </div>
        )}

        <div style={{ marginTop: 8 }}>
          <label style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            <input type="checkbox" checked={notify} onChange={async (e) => {
              const next = e.target.checked;
              setNotify(next);
              if (next && "Notification" in window && Notification.permission !== 'granted') {
                try { await Notification.requestPermission(); } catch {}
              }
            }} />
            生成完成时推送浏览器通知
          </label>
        </div>

        <footer style={{ marginTop: 24, color: "#888" }}>
          <Link href="/templates">行业模板</Link>
        </footer>
      </main>
      <AgentInspector data={{ request: { prompt: slogan, img: imageUrl }, lastEvent, events: allEvents }} />
    </div>
  );
}


