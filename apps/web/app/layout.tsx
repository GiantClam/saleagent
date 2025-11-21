import "./globals.css";
import type { Metadata } from "next";
import { AuthButtons } from "./components/AuthButtons";
import { ErrorBoundary } from "./components/ErrorBoundary";

export const metadata: Metadata = {
  title: "AI 营销视频生成",
  description: "多智能体实时生成营销视频与封面，支持分享页与站点地图",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body>
        <ErrorBoundary>
          <header style={{ 
            display: 'flex', 
            justifyContent: 'space-between', 
            alignItems: 'center', 
            padding: '12px 24px', 
            borderBottom: '1px solid #e5e7eb',
            background: 'white',
            boxShadow: '0 1px 3px rgba(0, 0, 0, 0.05)'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 24 }}>
              <a href="/" style={{ fontWeight: 700, fontSize: 18, color: '#1a1a1a' }}>SaleAgent</a>
              <a href="/workflow" style={{ fontSize: 14, color: '#6b7280', textDecoration: 'none' }}>工作流</a>
            </div>
            <div>
              <AuthButtons />
            </div>
          </header>
          <ErrorBoundary>
            {children}
          </ErrorBoundary>
        </ErrorBoundary>
      </body>
    </html>
  );
}


