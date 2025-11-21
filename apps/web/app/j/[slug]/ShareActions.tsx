"use client";

export function ShareActions({ videoUrl, coverUrl, slogan }: { videoUrl?: string; coverUrl?: string; slogan?: string }) {
  const copyToClipboard = (text: string) => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(text);
    }
  };

  return (
    <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
      <button onClick={() => videoUrl && copyToClipboard(videoUrl)} style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>复制视频链接</button>
      <button onClick={() => coverUrl && copyToClipboard(coverUrl)} style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>复制封面链接</button>
      <a href={videoUrl} download style={{ padding: 8, border: "1px solid #ddd", borderRadius: 8 }}>下载 MP4</a>
      <a href={`/?s=${encodeURIComponent(slogan || "")}${coverUrl ? `&img=${encodeURIComponent(coverUrl)}` : ""}`} style={{ padding: 8, border: "1px solid #111", borderRadius: 8, background: "#111", color: "#fff" }}>以此模板生成</a>
    </div>
  );
}

