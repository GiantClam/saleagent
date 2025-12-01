"use client";
import { Suspense, useEffect, useState } from "react";
import { AgentSidebar } from "./components/AgentSidebar";
import { AgentDialog } from "./components/AgentDialog";
import { AgentExecution } from "./components/AgentExecution";
import { useRouter, useSearchParams } from "next/navigation";

function HomeContent() {
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
    <div style={{ 
      display: "grid", 
      gridTemplateColumns: "280px 1fr 400px", 
      height: "calc(100vh - 60px)", 
      background: "#fafafa"
    }}>
      {/* 左侧：任务/会话工具栏 */}
      <AgentSidebar 
        apiUrl={process.env.NEXT_PUBLIC_AGENT_URL || "https://api.aimarketingsite.com"} 
        onNew={async () => {
          setLastEvent(null); 
          setAllEvents([]);
          setSlogan("");
          setImageUrl("");
          setRunId(undefined);
        }} 
      />

      {/* 中间：多智能体对话界面 */}
      <main style={{ 
        display: "flex",
        flexDirection: "column",
        background: "white",
        borderLeft: "1px solid #e5e7eb",
        borderRight: "1px solid #e5e7eb"
      }}>
        {/* 顶部信息栏 */}
        <div style={{ 
          padding: "20px 24px",
          borderBottom: "1px solid #e5e7eb",
          background: "white"
        }}>
          <h1 style={{ fontSize: 20, fontWeight: 700, marginBottom: 4, color: "#1a1a1a" }}>
            AI 营销视频生成
          </h1>
          <p style={{ color: "#6b7280", fontSize: 13, lineHeight: 1.5 }}>
            与多智能体协作，自动生成专业的营销视频和封面图
          </p>
        </div>

        {/* 对话区域 */}
        <div style={{ flex: 1, overflow: "hidden" }}>
          <AgentDialog 
            slogan={slogan} 
            imageUrl={imageUrl} 
            runId={runId} 
            autoStart={autoStart} 
            onEvent={(e) => setLastEvent(e)} 
            onEventsChange={(arr) => setAllEvents(arr)} 
            onFinished={async (info: any) => {
              const rid = typeof info === 'string' ? info : info?.runId;
              const slug = typeof info === 'object' ? info?.shareSlug : undefined;
              if (slug) { 
                router.push(`/j/${slug}`); 
                return; 
              }
              if (!process.env.NEXT_PUBLIC_AGENT_URL || !rid) return;
              const j = await fetch(`${process.env.NEXT_PUBLIC_AGENT_URL}/jobs/${rid}`).then(r => r.json()).catch(() => null);
              if (j?.share_slug) {
                if (notify && "Notification" in window && Notification.permission === 'granted') {
                  const jobIcon = () => 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2248%22 height=%2248%22 viewBox=%220 0 24 24%22 fill=%22%23000%22%3E%3Cpath d=%22M14 2H6a2 2 0 0 0-2 2v3h2V4h8V2zm4 5V4a2 2 0 0 0-2-2h-2v2h2v3h2zM4 9h16v10a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V9zm8 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6z%22/%3E%3C/svg%3E';
                  new Notification('生成完成', { body: slogan || '视频已生成，点击查看', icon: jobIcon() });
                }
                router.push(`/j/${j.share_slug}`);
              }
            }} 
          />
        </div>
      </main>

      {/* 右侧：智能体执行过程展示 */}
      <AgentExecution 
        data={{ 
          request: { prompt: slogan, img: imageUrl }, 
          lastEvent, 
          events: allEvents 
        }} 
      />
    </div>
  );
}

export default function Home() {
  return (
    <Suspense fallback={
      <div style={{ 
        display: "grid", 
        gridTemplateColumns: "280px 1fr 400px", 
        height: "calc(100vh - 60px)", 
        background: "#fafafa"
      }}>
        <div style={{ background: "white", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ color: "#9ca3af", fontSize: 14 }}>加载中...</div>
        </div>
        <div style={{ background: "white", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ color: "#9ca3af", fontSize: 14 }}>加载中...</div>
        </div>
        <div style={{ background: "white", display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ color: "#9ca3af", fontSize: 14 }}>加载中...</div>
        </div>
      </div>
    }>
      <HomeContent />
    </Suspense>
  );
}


