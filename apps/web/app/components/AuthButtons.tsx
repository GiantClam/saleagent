"use client";
import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

export function AuthButtons() {
  // 使用 mounted 状态确保服务端和客户端初始渲染一致
  const [mounted, setMounted] = useState(false);
  const [email, setEmail] = useState<string | null>(null);
  const [supabase, setSupabase] = useState<any>(null);

  useEffect(() => {
    // 标记组件已挂载（仅在客户端执行）
    setMounted(true);
    
    // 初始化 Supabase 客户端（仅在客户端）
    if (typeof window !== 'undefined' && process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY) {
      const client = createClient(
        process.env.NEXT_PUBLIC_SUPABASE_URL, 
        process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
      );
      setSupabase(client);
      
      // 获取用户信息
      (async () => {
        const { data } = await client.auth.getUser();
        setEmail(data.user?.email ?? null);
      })();
    }
  }, []);

  async function signIn(provider: 'google' | 'github') {
    if (!supabase || typeof window === 'undefined') return;
    await supabase.auth.signInWithOAuth({ provider, options: { redirectTo: window.location.origin } });
  }
  
  async function signOut() {
    if (!supabase) return;
    await supabase.auth.signOut();
    setEmail(null);
  }

  // 在服务端或未挂载时，显示占位内容（避免 hydration 不匹配）
  if (!mounted) {
    return (
      <div style={{ display: 'flex', gap: 8, minHeight: 32 }}>
        <div style={{ width: 100, height: 24 }} /> {/* 占位，避免布局跳动 */}
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', gap: 8 }}>
      {email ? (
        <>
          <span style={{ fontSize: 12, color: '#666' }}>{email}</span>
          <a href="/profile" style={{ padding: 6, border: '1px solid #ddd', borderRadius: 6 }}>个人中心</a>
          <button onClick={signOut} style={{ padding: 6, border: '1px solid #ddd', borderRadius: 6 }}>退出</button>
        </>
      ) : (
        <>
          <button onClick={() => signIn('google')} style={{ padding: 6, border: '1px solid #ddd', borderRadius: 6 }}>Google 登录</button>
          <button onClick={() => signIn('github')} style={{ padding: 6, border: '1px solid #ddd', borderRadius: 6 }}>GitHub 登录</button>
        </>
      )}
    </div>
  );
}


