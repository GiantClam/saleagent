export default function TemplatesPage() {
  const items = [
    { slug: "tech", title: "科技", desc: "高对比、品牌色、硬件特写" },
    { slug: "food", title: "美食", desc: "柔光、近景质感、慢动作" },
    { slug: "baby", title: "母婴", desc: "温柔色调、居家场景、安心感" }
  ];
  return (
    <main style={{ maxWidth: 880, margin: "0 auto", padding: 24 }}>
      <h1 style={{ fontSize: 28, fontWeight: 700 }}>行业模板</h1>
      <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
        {items.map((it) => (
          <div key={it.slug} style={{ border: "1px solid #eee", padding: 12, borderRadius: 8 }}>
            <div style={{ fontWeight: 600 }}>{it.title}</div>
            <div style={{ color: "#666", marginTop: 6 }}>{it.desc}</div>
          </div>
        ))}
      </div>
    </main>
  );
}


