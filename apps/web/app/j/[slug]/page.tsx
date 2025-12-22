import type { Metadata } from "next";
import { Suspense } from "react";
import { ShareActions } from "./ShareActions";

async function getJob(slug: string) {
  const api = process.env.NEXT_PUBLIC_AGENT_URL;
  if (!api) return null;
  const res = await fetch(`${api}/share/${slug}`, { next: { revalidate: 60 } });
  if (!res.ok) return null;
  return await res.json();
}

export async function generateMetadata({ params }: { params: Promise<{ slug: string }> }): Promise<Metadata> {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) return { robots: { index: false, follow: false } };
  const title = `${job.slogan}｜AI 营销视频`;
  const description = `基于 ${job.slogan} 生成的营销视频与封面，可下载投放。`;
  const images = [{ url: job.cover_url, width: 1200, height: 630, alt: job.slogan }];
  const canonical = `${process.env.NEXT_PUBLIC_SITE_URL || ""}/j/${job.share_slug}`;

  return {
    title,
    description,
    openGraph: { title, description, images, type: "video.other", videos: [{ url: job.video_url }] },
    twitter: { card: "player", title, description, images },
    alternates: { canonical }
  };
}

export default async function JobPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  const job = await getJob(slug);
  if (!job) {
    return (
      <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700 }}>未找到该分享页</h1>
      </main>
    );
  }
  const api = process.env.NEXT_PUBLIC_AGENT_URL;
  const recs = api ? await fetch(`${api}/recommend/${slug}`, { next: { revalidate: 300 } }).then(r => r.ok ? r.json() : []) : [];
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "VideoObject",
    name: job.slogan,
    thumbnailUrl: job.cover_url,
    uploadDate: job.created_at,
    description: `AI 生成营销视频 - ${job.slogan}`,
    contentUrl: job.video_url
  };

  const storyboards = job.storyboards && Array.isArray(job.storyboards) ? job.storyboards : [];
  const hasStoryboards = storyboards.length > 0;

  return (
    <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
      <h1 style={{ fontSize: 28, fontWeight: 700 }}>{job.slogan}</h1>

      {/* 视频信息 */}
      {job.total_duration && (
        <div style={{ marginTop: 12, fontSize: 14, color: "#6b7280" }}>
          总时长: {job.total_duration.toFixed(1)}秒
          {job.styles && job.styles.length > 0 && (
            <span style={{ marginLeft: 16 }}>
              风格: {job.styles.join("、")}
            </span>
          )}
        </div>
      )}

      <video src={job.video_url} controls style={{ width: "100%", marginTop: 16, borderRadius: 12 }} />
      <Suspense fallback={<div style={{ marginTop: 12 }}>加载中...</div>}>
        <ShareActions videoUrl={job.video_url} coverUrl={job.cover_url} slogan={job.slogan} />
      </Suspense>

      {/* 分镜信息展示 */}
      {hasStoryboards && (
        <div style={{ marginTop: 32, padding: 24, background: "#f9fafb", borderRadius: 12 }}>
          <h2 style={{ fontSize: 20, fontWeight: 600, marginBottom: 20 }}>分镜脚本</h2>
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            {storyboards.map((sb: any, idx: number) => (
              <div key={idx} style={{ padding: 16, background: "white", borderRadius: 8, border: "1px solid #e5e7eb" }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                  <span style={{ fontSize: 14, fontWeight: 600, color: "#1f2937" }}>镜头 {sb.idx || idx + 1}</span>
                  <span style={{ fontSize: 12, color: "#6b7280" }}>
                    {sb.begin_s?.toFixed(1) || "0.0"}s - {sb.end_s?.toFixed(1) || "0.0"}s
                  </span>
                </div>
                <p style={{ fontSize: 14, color: "#374151", lineHeight: 1.6, margin: 0 }}>
                  {sb.desc || "无描述"}
                </p>
                {sb.keyframes && (sb.keyframes.in || sb.keyframes.out) && (
                  <div style={{ marginTop: 12, display: "flex", gap: 12 }}>
                    {sb.keyframes.in && (
                      <div>
                        <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>首帧</div>
                        <img src={sb.keyframes.in} alt="首帧" style={{ maxWidth: 120, borderRadius: 6, border: "1px solid #e5e7eb" }} />
                      </div>
                    )}
                    {sb.keyframes.out && (
                      <div>
                        <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 4 }}>尾帧</div>
                        <img src={sb.keyframes.out} alt="尾帧" style={{ maxWidth: 120, borderRadius: 6, border: "1px solid #e5e7eb" }} />
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 20 }}>
        <div style={{ fontWeight: 600 }}>相似模板推荐</div>
        <ul>
          {Array.isArray(recs) && recs.length > 0 ? recs.map((r: any) => (
            <li key={r.id}>
              {r.title}{r.category ? ` · ${r.category}` : ""} —— <a href={`/?s=${encodeURIComponent(r.title || "")}${r.cover_url ? `&img=${encodeURIComponent(r.cover_url)}` : ""}`}>以此生成</a>
            </li>
          )) : <li><a href="/templates">前往行业模板库</a></li>}
        </ul>
      </div>
      {/* JSON-LD */}
      <script
        type="application/ld+json"
        dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
      />
    </main>
  );
}


