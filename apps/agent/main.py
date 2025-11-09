import os
import json
import uuid
import asyncio
from datetime import datetime
import random
import re

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client
import httpx
from .providers import get_image_provider, get_video_provider
from .r2 import upload_url_to_r2

# 事件编码（简化版，与 AG-UI 兼容的数据结构）
def encode_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

app = FastAPI()

# CORS 允许前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CORS_ORIGIN", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Supabase 客户端（可选）
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
# OpenRouter 配置（统一管理不同模型服务商）
EMBED_BASE = os.getenv("EMBEDDING_API_BASE", os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"))
EMBED_KEY = os.getenv("EMBEDDING_API_KEY", os.getenv("LLM_API_KEY"))
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBED_REFERER = os.getenv("EMBEDDING_REFERER", os.getenv("SITE_URL", "https://saleagent.app"))
CF_WORKER_NOTIFY_URL = os.getenv("CF_WORKER_NOTIFY_URL")
CF_NOTIFY_TOKEN = os.getenv("CF_NOTIFY_TOKEN")


async def emit(agent: str, etype: str, run_id: str, thread_id: str, delta: str | None = None, payload: dict | None = None, progress: dict | None = None):
    yield encode_event({
        "thread_id": thread_id,
        "run_id": run_id,
        "agent": agent,
        "type": etype,
        "delta": delta,
        "payload": payload or {},
        "progress": progress,
        "ts": int(datetime.utcnow().timestamp() * 1000),
    })


image_provider = get_image_provider()
video_provider = get_video_provider()


async def simulate_video(prompt: str, image_url: str) -> str:
    return await video_provider.generate(prompt, image_url, duration=6)


async def events(prompt: str, img: str | None, thread_id: str, run_id: str):
    # 开始
    async for chunk in emit("System", "run_started", run_id, thread_id, delta="开始执行…", progress={"current": 0, "total": 4}):
        yield chunk

    # 1 Prompt 优化
    async for chunk in emit("PromptAgent", "thought", run_id, thread_id, delta="🤔 优化提示词…", progress={"current": 1, "total": 4}):
        yield chunk
    await asyncio.sleep(0.8)
    optimized = f"【优化】{prompt}｜镜头：品牌→特写→场景→CTA｜时长 6s"
    async for chunk in emit("PromptAgent", "partial", run_id, thread_id, delta=f"✅ 优化后：{optimized}"):
        yield chunk

    # 2 封面生成（或复用）
    async for chunk in emit("ImageAgent", "thought", run_id, thread_id, delta="🖼  生成封面中…", progress={"current": 2, "total": 4}):
        yield chunk
    cover_url = img or await image_provider.generate(optimized)
    async for chunk in emit("ImageAgent", "tool_result", run_id, thread_id, delta=f"🖼  封面地址：{cover_url}"):
        yield chunk

    # 3 视频生成
    async for chunk in emit("VideoAgent", "thought", run_id, thread_id, delta="🎞  生成视频中…", progress={"current": 3, "total": 4}):
        yield chunk
    provider_res = await simulate_video(optimized, cover_url)
    if isinstance(provider_res, dict) and provider_res.get("pending"):
        # 记录任务处理中，等待 webhook 或后台轮询
        task_id = provider_res.get("task_id")
        if supabase:
            supabase.table("jobs").upsert({
                "run_id": run_id,
                "slogan": prompt,
                "cover_url": cover_url,
                "status": "processing",
                "provider_task_id": task_id,
                "updated_at": datetime.utcnow().isoformat()
            }, on_conflict="run_id").execute()
        async for chunk in emit("VideoAgent", "info", run_id, thread_id, delta="🎞  任务已提交，等待回调/轮询…", payload={"task_id": task_id}):
            yield chunk
        # 此路径下不再直接完成，由 webhook 完成持久化并可在前端列表/分享页查看
        return
    else:
        provider_url = provider_res.get("video_url") if isinstance(provider_res, dict) else provider_res
        # 4 上传到 R2（未配置则回退为 provider_url）
        cdn_url = await upload_url_to_r2(provider_url, f"{run_id}.mp4")
        async for chunk in emit("VideoAgent", "tool_result", run_id, thread_id, delta=f"🎬 CDN 视频：{cdn_url}"):
            yield chunk

    # 完成
    # 持久化成功并回传 share_slug
    share_slug = await persist_success(run_id, prompt, cover_url, cdn_url)
    if supabase:
        j = supabase.table("jobs").select("user_id").eq("run_id", run_id).single().execute()
        user_id = (j.data or {}).get("user_id") if j and j.data else None
        email = None
        if user_id:
            u = supabase.table("users").select("email").eq("id", user_id).single().execute()
            email = (u.data or {}).get("email") if u and u.data else None
            if not email:
                p = supabase.table("profiles").select("email").eq("id", user_id).single().execute()
                email = (p.data or {}).get("email") if p and p.data else None
        await send_email(email, "视频生成完成", f"您的视频已生成：{cdn_url}", f"<p>您的视频已生成：<a href='{cdn_url}'>{cdn_url}</a></p>")
    async for chunk in emit("System", "run_finished", run_id, thread_id, delta="完成", progress={"current": 4, "total": 4}, payload={"share_slug": share_slug}):
        yield chunk


@app.post("/crewai-agent")
async def run_agent(request: Request):
    body = await request.json()
    prompt = body.get("prompt", "")
    img = body.get("img")
    thread_id = body.get("thread_id") or f"t_{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"r_{uuid.uuid4().hex[:8]}"

    async def generator():
        async for chunk in events(prompt, img, thread_id, run_id):
            yield chunk

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/webhook/runninghub")
async def webhook_runninghub(request: Request):
    body = await request.json()
    task_id = body.get("taskId") or body.get("id")
    status = (body.get("status") or "").lower()
    outputs = body.get("outputs") or body.get("result") or []
    if not (supabase and task_id):
        return {"ok": True}
    # 找 run_id
    job = supabase.table("jobs").select("run_id, slogan, cover_url").eq("provider_task_id", task_id).single().execute()
    if not job or not job.data:
        return {"ok": True}
    run_id = job.data.get("run_id")
    slogan = job.data.get("slogan")
    cover_url = job.data.get("cover_url")
    # 成功则获取视频链接
    video_url = None
    if status in {"success", "finished", "done"}:
        for item in outputs:
            url = item.get("fileUrl") or item.get("url")
            ftype = (item.get("fileType") or "").lower()
            if url and ("mp4" in url or ftype in {"mp4", "video"}):
                video_url = url
                break
    if video_url:
        cdn_url = await upload_url_to_r2(video_url, f"{run_id}.mp4")
        await persist_success(run_id, slogan or "", cover_url or "", cdn_url)
        j = supabase.table("jobs").select("user_id").eq("run_id", run_id).single().execute()
        user_id = (j.data or {}).get("user_id") if j and j.data else None
        email = None
        if user_id:
            u = supabase.table("users").select("email").eq("id", user_id).single().execute()
            email = (u.data or {}).get("email") if u and u.data else None
            if not email:
                p = supabase.table("profiles").select("email").eq("id", user_id).single().execute()
                email = (p.data or {}).get("email") if p and p.data else None
        await send_email(email, "视频生成完成", f"您的视频已生成：{cdn_url}", f"<p>您的视频已生成：<a href='{cdn_url}'>{cdn_url}</a></p>")
    else:
        await persist_failure(run_id, status or "failed")
        j = supabase.table("jobs").select("user_id").eq("run_id", run_id).single().execute()
        user_id = (j.data or {}).get("user_id") if j and j.data else None
        email = None
        if user_id:
            u = supabase.table("users").select("email").eq("id", user_id).single().execute()
            email = (u.data or {}).get("email") if u and u.data else None
            if not email:
                p = supabase.table("profiles").select("email").eq("id", user_id).single().execute()
                email = (p.data or {}).get("email") if p and p.data else None
        await send_email(email, "视频生成失败", f"任务 {run_id} 失败：{status}")
    return {"ok": True}


@app.get("/public-jobs")
async def public_jobs(page: int = 1, limit: int = 20, q: str | None = None):
    if not supabase:
        # 未配置 Supabase，返回占位数据
        return [{
            "share_slug": "demo",
            "slogan": "示例作业",
            "cover_url": "https://picsum.photos/seed/demo/800/450",
            "video_url": "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4",
            "created_at": datetime.utcnow().isoformat()
        }]
    query = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, created_at").not_.is_("share_slug", None)
    if q:
        query = query.ilike("slogan", f"%{q}%")
    res = query.order("created_at", desc=True).range((page-1)*limit, (page-1)*limit + limit - 1).execute()
    return res.data or []


@app.get("/my-jobs")
async def my_jobs(user_id: str, page: int = 1, limit: int = 20):
    if not supabase:
        return []
    res = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, status, created_at").eq("user_id", user_id).order("created_at", desc=True).range((page-1)*limit, (page-1)*limit + limit - 1).execute()
    return res.data or []


def _slugify(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip()).strip("-").lower()
    suffix = (uuid.uuid4().hex)[:6]
    return (text[:12] + "-" + suffix) if text else ("j-" + suffix)


async def persist_success(run_id: str, slogan: str, cover_url: str, video_url: str) -> str | None:
    if not supabase:
        return None
    share_slug = _slugify(slogan or run_id)
    # 计算并写入 embedding（用于后续相似推荐）
    embedding = await _get_embedding(slogan or "")
    supabase.table("jobs").upsert({
        "run_id": run_id,
        "slogan": slogan,
        "cover_url": cover_url,
        "video_url": video_url,
        "status": "succeeded",
        "share_slug": share_slug,
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="run_id").execute()
    try:
        if embedding:
            # 将本次 slogan 作为模板候选写入 prompts_library（去重按标题）
            supabase.table("prompts_library").upsert({
                "title": slogan[:200],
                "prompt": slogan,
                "embedding": embedding,
                "cover_url": cover_url,
                "category": None
            }, on_conflict="title").execute()
    except Exception:
        pass
    return share_slug


async def persist_failure(run_id: str, error: str):
    if not supabase:
        return
    supabase.table("jobs").upsert({
        "run_id": run_id,
        "status": "failed",
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="run_id").execute()


async def send_email(to: str | None, subject: str, text: str, html: str | None = None):
    if not (to and CF_WORKER_NOTIFY_URL and CF_NOTIFY_TOKEN):
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                CF_WORKER_NOTIFY_URL,
                headers={"x-signature": CF_NOTIFY_TOKEN, "content-type": "application/json"},
                json={"to": to, "subject": subject, "text": text, "html": html},
            )
    except Exception:
        pass


@app.post("/jobs")
async def create_job(request: Request):
    if not supabase:
        return {"error": "Supabase not configured"}
    body = await request.json()
    slogan = (body.get("slogan") or "").strip()
    user_id = body.get("user_id") or None
    run_id = body.get("run_id") or f"r_{uuid.uuid4().hex[:8]}"
    share_slug = _slugify(slogan or run_id)
    supabase.table("jobs").insert({
        "run_id": run_id,
        "slogan": slogan,
        "status": "running",
        "user_id": user_id,
        "share_slug": share_slug
    }).execute()
    return {"run_id": run_id, "share_slug": share_slug}


@app.get("/jobs/{run_id}")
async def get_job(run_id: str):
    if not supabase:
        return {"error": "Supabase not configured"}
    res = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, status, created_at, updated_at").eq("run_id", run_id).single().execute()
    return res.data or {}


@app.post("/jobs/{run_id}/retry")
async def retry_job(run_id: str):
    if not supabase:
        return {"error": "Supabase not configured"}
    old = supabase.table("jobs").select("slogan, cover_url, user_id").eq("run_id", run_id).single().execute()
    if not old or not old.data:
        return {"error": "not found"}
    new_run = f"r_{uuid.uuid4().hex[:8]}"
    supabase.table("jobs").insert({
        "run_id": new_run,
        "slogan": old.data.get("slogan"),
        "cover_url": old.data.get("cover_url"),
        "user_id": old.data.get("user_id"),
        "status": "running",
        "share_slug": _slugify(old.data.get("slogan") or new_run)
    }).execute()
    return {"run_id": new_run, "slogan": old.data.get("slogan"), "cover_url": old.data.get("cover_url")}


@app.get("/share/{slug}")
async def get_share(slug: str):
    if not supabase:
        return {"error": "Supabase not configured"}
    res = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, status, created_at, updated_at").eq("share_slug", slug).single().execute()
    return res.data or {}


async def _get_embedding(text: str) -> list[float] | None:
    if not (EMBED_BASE and EMBED_KEY and text):
        return None
    try:
        # 构建请求头，支持 OpenRouter
        headers = {
            "Authorization": f"Bearer {EMBED_KEY}",
            "Content-Type": "application/json",
        }
        # OpenRouter 需要 HTTP-Referer header
        if "openrouter.ai" in EMBED_BASE:
            headers["HTTP-Referer"] = EMBED_REFERER
            headers["X-Title"] = "SaleAgent"
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{EMBED_BASE}/embeddings",
                headers=headers,
                json={"input": text, "model": EMBED_MODEL},
            )
            r.raise_for_status()
            data = r.json()
            return data["data"][0]["embedding"]
    except Exception:
        return None


@app.get("/recommend/{slug}")
async def recommend(slug: str, limit: int = 8):
    if not supabase:
        return []
    # 获取当前作业 slogan
    job_res = supabase.table("jobs").select("slogan").eq("share_slug", slug).single().execute()
    slogan = (job_res.data or {}).get("slogan") if job_res and job_res.data else None
    if not slogan:
        rec = supabase.table("prompts_library").select("id, title, category").order("created_at", desc=True).limit(limit).execute()
        return rec.data or []

    # 优先尝试向量检索：如果存在 RPC match_prompts 则调用
    emb = await _get_embedding(slogan)
    if emb:
        try:
            rpc = supabase.rpc("match_prompts", {"query": emb, "match_count": limit}).execute()
            if rpc.data:
                # 若 RPC 未返回 cover_url，则补充查询
                if len(rpc.data) > 0 and "cover_url" not in rpc.data[0]:
                    ids = [row.get("id") for row in rpc.data if row.get("id")]
                    if ids:
                        detail = supabase.table("prompts_library").select("id, title, category, cover_url").in_("id", ids).execute()
                        if detail.data:
                            # 以 id 为键合并 cover_url
                            cover_map = {d["id"]: d.get("cover_url") for d in detail.data}
                            for row in rpc.data:
                                row["cover_url"] = cover_map.get(row.get("id"))
                return rpc.data
        except Exception:
            pass
    q = supabase.table("prompts_library").select("id, title, category, cover_url").ilike("title", f"%{slogan.split()[0]}%").limit(limit).execute()
    if q.data:
        return q.data
    rec = supabase.table("prompts_library").select("id, title, category, cover_url").order("created_at", desc=True).limit(limit).execute()
    return rec.data or []


