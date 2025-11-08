"use client";
import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

export function AuthButtons() {
  const supabase = (typeof window !== 'undefined' && process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    ? createClient(process.env.NEXT_PUBLIC_SUPABASE_URL, process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY)
    : null as any;
  const [email, setEmail] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      if (!supabase) return;
      const { data } = await supabase.auth.getUser();
      setEmail(data.user?.email ?? null);
    })();
  }, []);

  async function signIn(provider: 'google' | 'github') {
    if (!supabase) return;
    await supabase.auth.signInWithOAuth({ provider, options: { redirectTo: window.location.origin } });
  }
  async function signOut() {
    if (!supabase) return;
    await supabase.auth.signOut();
    setEmail(null);
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


