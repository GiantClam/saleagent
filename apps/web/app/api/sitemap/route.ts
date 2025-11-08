import { NextResponse } from "next/server";

export async function GET() {
  const site = process.env.NEXT_PUBLIC_SITE_URL || "https://example.com";
  const urls = [
    { loc: "/", changefreq: "daily", priority: 1.0 },
    // 占位：生产中加入公开分享页，如 /j/xxx
  ];

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
  <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  ${urls
    .map(
      (u) =>
        `<url><loc>${site}${u.loc}</loc><changefreq>${u.changefreq}</changefreq><priority>${u.priority}</priority></url>`
    )
    .join("")}
  </urlset>`;

  return new NextResponse(xml, { headers: { "Content-Type": "application/xml" } });
}


