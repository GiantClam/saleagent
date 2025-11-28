import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

export function middleware(req: NextRequest) {
  const flag = (process.env.DISABLE_FRONTEND || '').toLowerCase()
  const disabled = flag === '1' || flag === 'true'
  if (!disabled) return NextResponse.next()
  const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Site Disabled</title><style>html,body{height:100%;margin:0}body{display:flex;align-items:center;justify-content:center;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b0b0b;color:#eee} .box{max-width:640px;padding:24px;text-align:center;border:1px solid #222;border-radius:12px;background:#111;box-shadow:0 10px 30px rgba(0,0,0,.3)} h1{font-size:20px;margin:0 0 12px} p{margin:0;color:#aaa}</style></head><body><div class="box"><h1>Frontend is disabled</h1><p>当前环境未开放前端页面</p></div></body></html>`
  return new NextResponse(html, { status: 404, headers: { 'content-type': 'text/html; charset=utf-8' } })
}

export const config = { matcher: '/:path*' }

