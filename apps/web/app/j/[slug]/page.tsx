import type { Metadata } from "next";

async function getJob(slug: string) {
  const api = process.env.NEXT_PUBLIC_AGENT_URL;
  if (!api) return null;
  const res = await fetch(`${api}/share/${slug}`, { next: { revalidate: 60 } });
  if (!res.ok) return null;
  return await res.json();
}

export async function generateMetadata({ params }: { params: { slug: string } }): Promise<Metadata> {
  const job = await getJob(params.slug);
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

export default async function JobPage({ params }: { params: { slug: string } }) {
  const job = await getJob(params.slug);
  if (!job) {
    return (
      <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
        <h1 style={{ fontSize: 18, fontWeight: 700 }}>未找到该分享页</h1>
      </main>
    );
  }
  const api = process.env.NEXT_PUBLIC_AGENT_URL;
  const recs = api ? await fetch(`${api}/recommend/${params.slug}`, { next: { revalidate: 300 } }).then(r => r.ok ? r.json() : []) : [];
  const jsonLd = {
    "@context": "https://schema.org",
    "@type": "VideoObject",
    name: job.slogan,
    thumbnailUrl: job.cover_url,
    uploadDate: job.created_at,
    description: `AI 生成营销视频 - ${job.slogan}`,
    contentUrl: job.video_url
  };

  return (
    <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
      <h1 style={{ fontSize: 28, fontWeight: 700 }}>{job.slogan}</h1>
      <video src={job.video_url} controls style={{ width: "100%", marginTop: 16, borderRadius: 12 }} />
      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <button onClick={() => navigator.clipboard.writeText(job.video_url)} style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>复制视频链接</button>
        <button onClick={() => navigator.clipboard.writeText(job.cover_url)} style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>复制封面链接</button>
        <a href={job.video_url} download style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>下载 MP4</a>
        <a href={`/?s=${encodeURIComponent(job.slogan || "")}${job.cover_url ? `&img=${encodeURIComponent(job.cover_url)}` : ""}`} style={{ padding: 8, border: "1px solid #111", borderRadius: 8, background: "#111", color: "#fff" }}>以此模板生成</a>
      </div>
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


