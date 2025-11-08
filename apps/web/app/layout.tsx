import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AI 营销视频生成",
  description: "多智能体实时生成营销视频与封面，支持分享页与站点地图",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '10px 16px', borderBottom: '1px solid #eee' }}>
          <a href="/" style={{ fontWeight: 700 }}>SaleAgent</a>
          <div>
            {/* 登录/退出按钮 */}
            <span suppressHydrationWarning={true}>
              {/* 动态引入客户端组件避免 SSR 报错 */}
              {typeof window !== 'undefined' && require('./components/AuthButtons') && require('./components/AuthButtons').AuthButtons ? require('./components/AuthButtons').AuthButtons() : null}
            </span>
          </div>
        </header>
        {children}
      </body>
    </html>
  );
}


