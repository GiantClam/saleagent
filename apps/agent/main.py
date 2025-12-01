import os
import json
import uuid
import asyncio
from datetime import datetime
import random
import re
from dotenv import load_dotenv
import logging
import sys
from logging.handlers import RotatingFileHandler

# 首先加载 .env 文件，确保后续导入的模块能读取环境变量
load_dotenv()

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client
import httpx
from providers import get_image_provider, get_video_provider
from r2 import upload_url_to_r2
from openrouter_client import OpenRouterClient
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Tuple
from crewai_workflow import build_crew
from crewai_tools import plan_storyboard_impl
import logging
logger = logging.getLogger("workflow")

# 事件编码（简化版，与 AG-UI 兼容的数据结构）
def encode_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

app = FastAPI()

# 应用启动时启动 Supabase 队列 worker
@app.on_event("startup")
async def startup_event():
    """应用启动时初始化 Supabase 队列 worker"""
    try:
        from video_task_queue_supabase import start_supabase_queue_worker
        start_supabase_queue_worker()
    except Exception as e:
        logger.warning(f"[startup] Failed to start Supabase queue worker: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时停止 Supabase 队列 worker"""
    try:
        from video_task_queue_supabase import get_supabase_queue
        import asyncio
        
        queue = get_supabase_queue()
        if queue:
            queue.stop()
            # 等待 worker 任务完成取消（最多等待 2 秒）
            if queue._worker_task and not queue._worker_task.done():
                try:
                    await asyncio.wait_for(queue._worker_task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # 任务被取消或超时是正常的，忽略这些错误
                    pass
            logger.info("[shutdown] Supabase queue worker stopped")
    except asyncio.CancelledError:
        # 在 shutdown 期间，CancelledError 是正常的，不需要记录
        pass
    except Exception as e:
        logger.warning(f"[shutdown] Failed to stop Supabase queue worker: {e}")

# CORS 允许前端访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("CORS_ORIGIN", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

# 统一日志配置（stdout + 文件），确保 Railway 上可查看
def _configure_logging():
    logger = logging.getLogger()
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")
    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # 文件
    import os as _os
    log_dir = _os.getenv("LOG_DIR", "/app/logs")
    try:
        _os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(_os.path.join(log_dir, "app.log"), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # 文件日志不可用时不影响服务
        pass

_configure_logging()
logger = logging.getLogger("workflow")

# Supabase 客户端（可选）
# 后端服务应使用 SERVICE_ROLE_KEY 以绕过 RLS 策略
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY") 
    or os.getenv("SUPABASE_SERVICE_KEY") 
    or os.getenv("SUPABASE_ANON_KEY")
)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
# OpenRouter 配置（统一管理不同模型服务商）- 优先使用 OPENROUTER_*，兼容旧变量
OPENROUTER_BASE = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_MODEL = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")
EMBED_REFERER = os.getenv("EMBEDDING_REFERER", os.getenv("SITE_URL", "https://saleagent.app"))
PROMPT_LLM_MODEL = os.getenv("PROMPT_LLM_MODEL", "kimi/k2-think")
CF_WORKER_NOTIFY_URL = os.getenv("CF_WORKER_NOTIFY_URL")
CF_NOTIFY_TOKEN = os.getenv("CF_NOTIFY_TOKEN")

# 将 OpenRouter 映射为 OpenAI 兼容环境，供 CrewAI 内部 OpenAI Provider 使用
if OPENROUTER_KEY and not os.getenv("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = OPENROUTER_KEY
if OPENROUTER_BASE and not os.getenv("OPENAI_API_BASE"):
    os.environ["OPENAI_API_BASE"] = OPENROUTER_BASE
# 可选：有些路由要求携带 Referer/Title，但 CrewAI/OpenAI SDK 不一定支持额外头，这里仅记录日志
try:
    logging.getLogger("workflow").info(f"Using OpenRouter via OpenAI shim: base={os.getenv('OPENAI_API_BASE')}")
except Exception:
    pass

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
    """使用配置的视频提供商生成视频（默认 sora2）"""
    # 确保使用异步模式，避免长时间阻塞
    if hasattr(video_provider, 'generate'):
        result = await video_provider.generate(prompt, image_url, duration=10, async_mode=True)
        if isinstance(result, dict) and result.get("pending"):
            return result
        return result.get("video_url") if isinstance(result, dict) else result
    return await video_provider.generate(prompt, image_url, duration=10)


async def events(prompt: str, img: str | None, thread_id: str, run_id: str):
    # 开始
    async for chunk in emit("System", "run_started", run_id, thread_id, delta="开始执行…", progress={"current": 0, "total": 4}):
        yield chunk

    # 1 Prompt 优化
    async for chunk in emit("PromptAgent", "thought", run_id, thread_id, delta="🤔 优化提示词…", progress={"current": 1, "total": 4}):
        yield chunk
    # 使用 OpenRouter(kimi k2 thinking) 优化提示词（非 mock）
    if not (OPENROUTER_BASE and OPENROUTER_KEY):
        raise RuntimeError("未配置 OpenRouter LLM（OPENROUTER_BASE_URL/LLM_API_KEY）")
    or_client = OpenRouterClient(api_base=OPENROUTER_BASE, api_key=OPENROUTER_KEY, referer=EMBED_REFERER, title="SaleAgent")
    sys_prompt = "你是资深广告导演，请将用户的营销文案优化为更清晰、可拍摄的镜头脚本，包含镜头顺序、画面主体、景别、转场与结尾 CTA，时长控制在 10 秒。尽量避免有人脸出现。仅输出优化后的文本。"
    optimized = await or_client.chat_completions(
        model=PROMPT_LLM_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ],
        temperature=0.3,
        max_tokens=512,
    )
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

    # 完成（精简事件：仅发送一次 run_finished，包含最终 URL）
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
    async for chunk in emit("System", "run_finished", run_id, thread_id, delta=f"🎬 最终视频已生成：{cdn_url}", progress={"current": 4, "total": 4}, payload={"share_slug": share_slug, "video_url": cdn_url}):
        yield chunk

# ======================
# 工作流编排 REST 接口
# ======================

class PlanRequest(BaseModel):
    goal: str = Field(..., description="用户主体目标")
    total_duration: float = Field(..., gt=0, le=120.0, description="总时长(秒)，<=120s，支持0.1秒粒度")
    styles: List[str] = Field(default_factory=list, description="风格标签")
    image_control: bool = Field(default=False, description="是否启用图控（首/尾帧）")
    num_clips: int = Field(default=0, ge=0, description="可选：期望镜头数，0表示自动；每段不超过10s")

    @validator("total_duration")
    def norm_duration(cls, v: float) -> float:
        return round(v, 1)


class ClipSpec(BaseModel):
    idx: int
    desc: str
    begin_s: float
    end_s: float
    keyframes: Dict[str, Optional[str]] = Field(default_factory=dict)  # {"in":url?, "out":url?}


class PlanResponse(BaseModel):
    storyboards: List[ClipSpec]


def _even_split(total: float, n: int) -> List[Tuple[float, float]]:
    seg = round(total / n, 1)
    t = 0.0
    res: List[Tuple[float, float]] = []
    for i in range(n):
        start = round(t, 1)
        end = round(min(total, start + seg), 1)
        res.append((start, end))
        t = end
    res[-1] = (res[-1][0], round(total, 1))
    return res


@app.post("/workflow/plan", response_model=PlanResponse)
async def workflow_plan(body: PlanRequest):
    if not (OPENROUTER_BASE and OPENROUTER_KEY):
        raise RuntimeError("未配置 OpenRouter（OPENROUTER_API_BASE / OPENROUTER_API_KEY）")
    # 计算镜头数量，确保每段 <= 10s
    import math
    auto_n = max(1, math.ceil(body.total_duration / 10.0))
    n = body.num_clips if body.num_clips and body.num_clips > 0 else auto_n
    # 若用户给的镜头数导致单段 > 10s，则提升镜头数
    while round(body.total_duration / n, 1) > 10.0:
        n += 1
    # 使用 CrewAI 的 DirectorAgent 工具实现（直接调用实现函数，避免 Tool 不可调用问题）
    sb_json = await plan_storyboard_impl(body.goal, body.styles, float(body.total_duration), int(n))
    try:
        logger.info(f"[workflow_plan] crew_result_json(len={len(sb_json)}): {sb_json[:1000]}")
    except Exception:
        pass
    try:
        items = json.loads(sb_json)
    except Exception:
        raise RuntimeError("分镜生成失败：模型未返回有效 JSON")
    # 映射为 ClipSpec 列表（工具已按 total_duration 均分）
    storyboards: List[ClipSpec] = []
    for i, item in enumerate(items):
        storyboards.append(ClipSpec(
            idx=int(item.get("idx") or i + 1),
            desc=str(item.get("desc") or "").strip() or "镜头描述",
            begin_s=float(item.get("begin_s") or 0.0),
            end_s=float(item.get("end_s") or 0.0),
            keyframes=item.get("keyframes") or {"in": None, "out": None},
        ))
    try:
        logger.info(f"[workflow_plan] mapped_storyboards n={len(storyboards)} -> {[(s.idx, s.desc[:40]) for s in storyboards]}")
    except Exception:
        pass
    return PlanResponse(storyboards=storyboards)


class KeyframesRequest(BaseModel):
    storyboards: List[ClipSpec]
    image_control: bool = False


class KeyframesResponse(BaseModel):
    storyboards: List[ClipSpec]


@app.post("/workflow/keyframes", response_model=KeyframesResponse)
async def workflow_keyframes(body: KeyframesRequest):
    if body.image_control:
        for clip in body.storyboards:
            if not clip.keyframes.get("in"):
                clip.keyframes["in"] = await image_provider.generate(f"{clip.desc}，首帧海报")
            if not clip.keyframes.get("out"):
                clip.keyframes["out"] = await image_provider.generate(f"{clip.desc}，尾帧海报")
    return KeyframesResponse(storyboards=body.storyboards)


class ConfirmRequest(BaseModel):
    storyboards: List[ClipSpec]
    total_duration: float
    styles: List[str] = Field(default_factory=list)
    image_control: bool = False


@app.post("/workflow/confirm")
async def workflow_confirm(body: ConfirmRequest):
    run_id = f"r_{uuid.uuid4().hex[:8]}"
    if not supabase:
        return {"run_id": run_id, "warning": "Supabase not configured; data not persisted"}
    # 将 storyboards 转换为 JSON 格式存储
    storyboards_json = [{
        "idx": s.idx,
        "desc": s.desc,
        "begin_s": s.begin_s,
        "end_s": s.end_s,
        "keyframes": s.keyframes or {}
    } for s in body.storyboards]
    supabase.table("jobs").upsert({
        "run_id": run_id,
        "slogan": body.storyboards[0].desc if body.storyboards else "",
        "status": "planning_confirmed",
        "share_slug": _slugify(body.storyboards[0].desc if body.storyboards else run_id),
        "storyboards": storyboards_json,
        "total_duration": body.total_duration,
        "styles": body.styles or [],
        "image_control": body.image_control,
        "updated_at": datetime.utcnow().isoformat()
    }, on_conflict="run_id").execute()
    return {"run_id": run_id}


class RunClipsRequest(BaseModel):
    run_id: str
    storyboards: List[ClipSpec]


class ClipResult(BaseModel):
    idx: int
    status: str
    video_url: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None


class RunClipsResponse(BaseModel):
    results: List[ClipResult]


@app.post("/workflow/run-clips")
async def workflow_run_clips(request: Request):
    """生成镜头视频，支持 SSE 流式返回进度"""
    body = await request.json()
    run_id = body.get("run_id")
    storyboards_data = body.get("storyboards", [])
    use_sse = request.headers.get("accept") == "text/event-stream"
    
    if not run_id or not storyboards_data:
        return {"error": "缺少 run_id 或 storyboards"}
    
    storyboards = [ClipSpec(**s) for s in storyboards_data]
    sem = asyncio.Semaphore(4)
    results: List[ClipResult] = []
    total = len(storyboards)

    async def run_one(clip: ClipSpec) -> ClipResult:
        prompt = clip.desc
        ref_img = clip.keyframes.get("in") if clip.keyframes else None
        duration = max(1, int(round(clip.end_s - clip.begin_s)))
        async with sem:
            try:
                res = await video_provider.generate(prompt, ref_img or "", duration=duration)
                if isinstance(res, dict):
                    url = res.get("video_url")
                    detail = {k: v for k, v in res.items() if k != "video_url"}
                else:
                    url, detail = str(res), None
                cdn_url = await upload_url_to_r2(url, f"{run_id}_clip{clip.idx}.mp4")
                return ClipResult(idx=clip.idx, status="succeeded", video_url=cdn_url, detail=detail)
            except Exception as e:
                return ClipResult(idx=clip.idx, status="failed", video_url=None, detail={"error": str(e)})

    if use_sse:
        # SSE 流式返回
        async def generator():
            # 初始化所有任务为 "generating" 状态
            for clip in storyboards:
                yield encode_event({
                    "type": "progress",
                    "clip": {
                        "idx": clip.idx,
                        "status": "generating",
                        "video_url": None,
                    }
                })
            
            tasks = [run_one(c) for c in storyboards]
            completed = 0
            for coro in asyncio.as_completed(tasks):
                result = await coro
                results.append(result)
                completed += 1
                yield encode_event({
                    "type": "progress",
                    "completed": completed,
                    "total": total,
                    "clip": {
                        "idx": result.idx,
                        "status": result.status,
                        "video_url": result.video_url,
                    }
                })
            yield encode_event({
                "type": "done",
                "results": [{"idx": r.idx, "status": r.status, "video_url": r.video_url} for r in results]
            })
        return StreamingResponse(generator(), media_type="text/event-stream")
    else:
        # 同步返回（兼容旧接口）
        tasks = [run_one(c) for c in storyboards]
        done = await asyncio.gather(*tasks)
        results.extend(done)
        return RunClipsResponse(results=results)


class ClipStatusRequest(BaseModel):
    task_ids: List[str] = Field(default_factory=list)


@app.post("/workflow/clip-status")
async def workflow_clip_status(body: ClipStatusRequest):
    return {"results": [{"task_id": t, "status": "unknown"} for t in body.task_ids]}


class StitchRequest(BaseModel):
    run_id: str
    segments: List[str]
    output_key: Optional[str] = None


@app.post("/workflow/stitch")
async def workflow_stitch(body: StitchRequest):
    """
    拼接视频片段为最终视频
    
    使用独立的视频拼接函数，不通过 CrewAI 智能体。
    """
    if not body.segments or len(body.segments) == 0:
        return {"error": "no segments"}
    
    try:
        from video_stitcher import stitch_video_segments
        
        final_key = body.output_key or f"{body.run_id}_final.mp4"
        cdn_url = await stitch_video_segments(
            segment_urls=body.segments,
            run_id=body.run_id,
            output_key=final_key
        )
        
        return {
            "run_id": body.run_id,
            "segments": body.segments,
            "final_url": cdn_url
        }
    except Exception as e:
        logger.error(f"[workflow_stitch] Error: {e}", exc_info=True)
        return {"error": str(e)}


class CrewRunRequest(BaseModel):
    goal: str
    styles: List[str] = []
    total_duration: float = 6.0
    num_clips: int = 1
    image_control: bool = False
    run_id: Optional[str] = None


@app.post("/workflow/crew-run")
async def workflow_crew_run(body: CrewRunRequest):
    """
    使用 CrewAI 执行完整工作流（优化提示词 -> 分镜 -> 关键帧(可选) -> 生成片段 -> 拼接）
    
    注意：
    1. 任务在后台异步执行，不阻塞 HTTP 请求
    2. 如果视频生成任务还在处理中，会注册回调，当所有任务完成时自动触发拼接
    3. 前端关闭后，任务仍会继续执行，可通过 /workflow/crew-status/{run_id} 查询状态
    """
    payload = body.dict()
    if not payload.get("run_id"):
        payload["run_id"] = f"r_{uuid.uuid4().hex[:8]}"
    
    run_id = payload["run_id"]
    
    # 生成 session_id（用于回调关联）
    session_id = f"session_{run_id}_{int(datetime.utcnow().timestamp() * 1000)}"
    
    # 立即注册会话（在后台执行前）
    try:
        from crewai_session_manager import get_session_manager
        session_manager = get_session_manager()
        if session_manager:
                # 从 payload 中获取期望的视频任务数量（按10s一个任务计算）
                import math
                total_duration = float(payload.get("total_duration", 10.0))
                # 计算期望的视频任务数（按10s一个任务）
                expected_tasks = max(1, math.ceil(total_duration / 10.0))
                
                # 注册会话（状态为 running）
                await session_manager.register_session(
                    run_id=run_id,
                    session_id=session_id,
                    expected_clips=expected_tasks,  # 期望的视频任务数（按10s一个任务）
                    context={
                        "goal": payload.get("goal", ""),
                        "styles": payload.get("styles", []),
                        "total_duration": payload.get("total_duration", 10.0),
                        "expected_tasks": expected_tasks,  # 期望的视频任务数
                        "image_control": payload.get("image_control", False),
                        "status": "running"  # running, waiting_videos, ready_to_stitch, stitching, completed, failed
                    }
                )
                
                # 更新状态为 running
                session_manager.supabase.table("crew_sessions")\
                    .update({
                        "status": "running",
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("run_id", run_id)\
                    .execute()
                
                logger.info(
                    f"[workflow_crew_run] Registered callback for run_id={run_id}, "
                    f"session_id={session_id}, expected_tasks={expected_tasks} (calculated from total_duration={total_duration}s)"
                )
    except Exception as e:
        logger.warning(f"[workflow_crew_run] Failed to register session: {e}")
    
    # 在后台异步执行 CrewAI（不阻塞 HTTP 请求）
    async def execute_crew_async():
        """后台执行 CrewAI 工作流"""
        try:
            # 将 session_id 传递给 CrewAI（通过环境变量）
            import os
            os.environ[f"CREWAI_SESSION_ID_{run_id}"] = session_id
            
            crew = build_crew(payload)
            # CrewAI 为同步接口，需要在后台线程中执行
            import concurrent.futures
            loop = asyncio.get_event_loop()
            
            # 在线程池中执行同步的 CrewAI
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result = await loop.run_in_executor(
                    executor,
                    lambda: crew.kickoff(inputs={"storyboards_json": ""})
                )
            
            # 清理环境变量
            os.environ.pop(f"CREWAI_SESSION_ID_{run_id}", None)
            
            # 检查结果是否包含 pending 状态
            result_str = str(result)
            
            # 更新会话状态
            if session_manager:
                # 先检查当前状态，如果已经是 completed 或 stitching，说明拼接已完成或正在进行，不要覆盖
                current_session = session_manager.supabase.table("crew_sessions")\
                    .select("status, result")\
                    .eq("run_id", run_id)\
                    .single()\
                    .execute()
                
                current_status = None
                current_result = None
                if current_session.data:
                    current_status = current_session.data.get("status", "")
                    current_result = current_session.data.get("result", "")
                
                # 如果状态已经是 completed 或 stitching，说明拼接流程已经启动或完成，不要覆盖
                if current_status in {"stitching", "completed"}:
                    logger.info(
                        f"[workflow_crew_run] CrewAI execution completed, but video stitching is already "
                        f"{current_status} for run_id={run_id}. Current result: {current_result[:100] if current_result else 'N/A'}. "
                        f"Skipping status update to avoid overwriting."
                    )
                    return
                
                if "pending" in result_str.lower() or "处理中" in result_str:
                    # 任务还在处理中，更新状态为 waiting_videos（只有在当前状态不是 stitching/completed 时）
                    if current_status not in {"stitching", "completed"}:
                        session_manager.supabase.table("crew_sessions")\
                            .update({
                                "status": "waiting_videos",
                                "result": result_str,
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("run_id", run_id)\
                            .execute()
                        
                        logger.info(
                            f"[workflow_crew_run] CrewAI execution completed, waiting for videos: run_id={run_id}"
                        )
                    else:
                        logger.info(
                            f"[workflow_crew_run] CrewAI execution completed, but status is already {current_status}, "
                            f"skipping update for run_id={run_id}"
                        )
                else:
                    # 任务完成（可能是同步完成，或者失败）
                    # 只有在当前状态不是 stitching/completed 时才更新
                    if current_status not in {"stitching", "completed"}:
                        session_manager.supabase.table("crew_sessions")\
                            .update({
                                "status": "completed" if "http" in result_str.lower() else "failed",
                                "result": result_str,
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("run_id", run_id)\
                            .execute()
                        
                        logger.info(
                            f"[workflow_crew_run] CrewAI execution completed: run_id={run_id}, result={result_str[:100]}"
                        )
                    else:
                        logger.info(
                            f"[workflow_crew_run] CrewAI execution completed, but status is already {current_status}, "
                            f"skipping update for run_id={run_id}"
                        )
        except Exception as e:
            logger.error(f"[workflow_crew_run] Error executing CrewAI: {e}", exc_info=True)
            # 更新状态为失败
            if session_manager:
                try:
                    session_manager.supabase.table("crew_sessions")\
                        .update({
                            "status": "failed",
                            "error": str(e),
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("run_id", run_id)\
                        .execute()
                except Exception:
                    pass
    
    # 启动后台任务（不等待完成）
    asyncio.create_task(execute_crew_async())
    
    # 立即返回，不阻塞
    return {
        "run_id": run_id,
        "session_id": session_id,
        "status": "running",
        "message": "任务已在后台开始执行，可通过 /workflow/crew-status/{run_id} 查询状态"
    }


@app.get("/workflow/crew-status/{run_id}")
async def get_crew_status(run_id: str):
    """
    查询 CrewAI 工作流执行状态
    
    返回：
    - status: running, waiting_videos, ready_to_stitch, stitching, completed, failed
    - result: 最终结果（如果完成）
    - error: 错误信息（如果失败）
    """
    try:
        from crewai_session_manager import get_session_manager
        session_manager = get_session_manager()
        
        if not session_manager:
            return {"error": "Session manager not available"}
        
        result = session_manager.supabase.table("crew_sessions")\
            .select("*")\
            .eq("run_id", run_id)\
            .single()\
            .execute()
        
        if not result.data:
            return {"error": f"Session not found for run_id: {run_id}"}
        
        session = result.data
        return {
            "run_id": run_id,
            "status": session.get("status", "unknown"),
            "result": session.get("result"),
            "error": session.get("error"),
            "expected_clips": session.get("expected_clips"),
            "context": session.get("context", {}),
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at")
        }
    except Exception as e:
        logger.error(f"[get_crew_status] Error: {e}", exc_info=True)
        return {"error": str(e)}


# 上传图片文件到 RunningHub（返回 fileName，可直接用于 nodeInfoList）
@app.post("/workflow/upload-image")
async def workflow_upload_image(file: UploadFile = File(...)):
    from runninghub_client import RunningHubClient
    content = await file.read()
    if not content:
        return {"error": "empty file"}
    client = RunningHubClient()
    stored = await client.upload_bytes(content, file.filename or "image.png", file_type="input")
    return {"fileName": stored}


# 更新单个 scene 信息
@app.post("/crewai/scene/update")
async def update_scene(request: Request):
    """
    更新单个 scene 的脚本和图片
    """
    try:
        body = await request.json()
        message_id = body.get("message_id")
        scene_idx = int(body.get("scene_idx", 0))
        script = body.get("script")
        image_url = body.get("image_url")
        
        if not message_id or not scene_idx:
            return {"error": "缺少必要参数"}
        
        # 这里应该更新云端存储（Supabase 或其他存储）
        # 暂时返回成功，实际实现需要根据存储方案调整
        result = {
            "message_id": message_id,
            "scene_idx": scene_idx,
        }
        
        if script:
            # 解析脚本为 clips
            clips = []
            clip_descs = script.split("；")
            for idx, desc in enumerate(clip_descs, 1):
                clips.append({
                    "idx": idx,
                    "desc": desc.strip(),
                    "begin_s": 0.0,  # 需要根据实际情况计算
                    "end_s": 10.0,
                })
            result["clips"] = clips
        
        if image_url:
            result["image_url"] = image_url
        
        return result
    except Exception as e:
        logger.error(f"[update_scene] Error: {e}", exc_info=True)
        return {"error": str(e)}


# 重新生成单个 scene
@app.post("/crewai/storyboard/confirm")
async def confirm_storyboard(request: Request):
    """
    确认或拒绝 storyboard，继续或重新生成工作流
    """
    try:
        body = await request.json()
        run_id = body.get("run_id")
        confirmed = body.get("confirmed", True)
        feedback = body.get("feedback", "")
        
        if not run_id:
            return {"error": "缺少 run_id"}
        
        # 存储确认状态
        if not hasattr(confirm_storyboard, "_confirmations"):
            confirm_storyboard._confirmations = {}
        
        confirmation_key = f"{run_id}_storyboard"
        confirm_storyboard._confirmations[confirmation_key] = {
            "status": "confirmed" if confirmed else "rejected",
            "feedback": feedback,
        }
        
        logger.info(f"[confirm_storyboard] Run {run_id}: {'confirmed' if confirmed else 'rejected'}")
        
        return {
            "run_id": run_id,
            "status": "confirmed" if confirmed else "rejected",
            "message": "确认成功" if confirmed else "已标记为拒绝，将重新生成"
        }
    except Exception as e:
        logger.error(f"[confirm_storyboard] Error: {e}", exc_info=True)
        return {"error": str(e)}


@app.post("/crewai/scene/regenerate")
async def regenerate_scene(request: Request):
    """
    使用 CrewAI agent 重新生成单个 scene 的脚本和图片
    """
    try:
        body = await request.json()
        message_id = body.get("message_id")
        scene_idx = int(body.get("scene_idx", 0))
        script = body.get("script", "")
        context = body.get("context", {})
        
        if not message_id or not scene_idx:
            return {"error": "缺少必要参数"}
        
        # 使用 director_agent 重新生成 scene 脚本
        from crewai_agents import build_agents
        from crewai import Task, Crew, Process
        
        [creative_agent, director_agent, reviewer_agent, visual_agent, producer_agent] = build_agents()
        
        # 构建重新生成 scene 的任务
        regenerate_task = Task(
            description=(
                f"重新生成场景 {scene_idx} 的分镜脚本：\n"
                f"当前脚本：{script}\n"
                f"上下文信息：{context}\n"
                f"请基于当前脚本和上下文，生成更优化的场景描述和分镜。"
            ),
            agent=director_agent,
            expected_output="更新后的场景脚本（JSON 格式，包含 clips 数组）",
        )
        
        crew = Crew(
            agents=[director_agent],
            tasks=[regenerate_task],
            process=Process.sequential,
            verbose=True,
        )
        
        # 执行重新生成
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result = await loop.run_in_executor(
                executor,
                lambda: crew.kickoff(inputs={})
            )
        
        # 解析结果，提取 clips
        result_str = str(result)
        import json
        import re
        
        # 尝试从结果中提取 JSON
        clips = []
        try:
            json_match = re.search(r'\[.*?\]', result_str, re.DOTALL)
            if json_match:
                clips_data = json.loads(json_match.group(0))
                if isinstance(clips_data, list):
                    clips = clips_data
        except:
            # 如果解析失败，使用原始脚本创建 clips
            clip_descs = script.split("；") if script else []
            for idx, desc in enumerate(clip_descs, 1):
                clips.append({
                    "idx": idx,
                    "desc": desc.strip(),
                    "begin_s": 0.0,
                    "end_s": 10.0,
                })
        
        # 生成图片
        scene_desc = "；".join([clip.get("desc", "") for clip in clips if clip.get("desc")])
        image_provider = get_image_provider()
        # 【重要】避免生成带有人脸的图片，因为 sora2 不支持使用真人图片作为参考
        image_prompt = f"{scene_desc}，视频场景画面，无人脸、无真人，无人物形象"
        image_url = await image_provider.generate(image_prompt)
        
        return {
            "message_id": message_id,
            "scene_idx": scene_idx,
            "clips": clips,
            "image_url": image_url,
        }
    except Exception as e:
        logger.error(f"[regenerate_scene] Error: {e}", exc_info=True)
        return {"error": str(e)}


@app.post("/crewai-agent")
async def run_agent(request: Request):
    """
    使用 CrewAI 多智能体工作流生成视频（支持 SSE 流式返回）
    
    工作流步骤：
    1. 创意策划 - 优化提示词和策略
    2. 导演 - 规划分镜脚本
    3. 审核 - 审核分镜质量，合并镜头
    4. 视觉设计 - 生成关键帧（可选）
    5. 制片 - 提交视频生成任务（使用 sora2）
    6. 剪辑 - 拼接最终视频
    """
    body = await request.json()
    prompt = body.get("prompt", "")
    img = body.get("img")
    thread_id = body.get("thread_id") or f"t_{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"r_{uuid.uuid4().hex[:8]}"
    
    # 如果提供了完整参数，使用 CrewAI 工作流
    goal = body.get("goal") or prompt
    styles = body.get("styles", [])
    total_duration = float(body.get("total_duration", 10.0))
    num_clips = int(body.get("num_clips", 0))
    image_control = bool(body.get("image_control", False))
    
    # 检查是否使用 CrewAI 工作流（如果提供了 goal 或 styles，使用工作流）
    use_crewai_workflow = bool(body.get("goal") or body.get("styles") or body.get("total_duration") or body.get("use_crewai", True))
    
    if use_crewai_workflow and goal:
        # 使用 CrewAI 多智能体工作流
        async def generator():
            try:
                # 开始
                async for chunk in emit("System", "run_started", run_id, thread_id, 
                                       delta="🚀 开始 CrewAI 多智能体工作流…", 
                                       progress={"current": 0, "total": 6}):
                    yield chunk
            except Exception as e:
                logger.error(f"[crewai-agent] Error in generator start: {e}", exc_info=True)
                try:
                    async for chunk in emit("System", "error", run_id, thread_id, 
                                           delta=f"❌ 错误：{str(e)}"):
                        yield chunk
                except:
                    pass
                return
            
            try:
                
                # 准备 CrewAI 工作流参数
                payload = {
                    "goal": goal,
                    "styles": styles if isinstance(styles, list) else [],
                    "total_duration": total_duration,
                    "num_clips": num_clips,
                    "image_control": image_control,
                    "run_id": run_id,
                }
                
                # 注册会话（用于视频任务完成后的回调）
                try:
                    from crewai_session_manager import get_session_manager
                    session_manager = get_session_manager()
                    if session_manager:
                        session_id = f"session_{run_id}_{int(datetime.utcnow().timestamp() * 1000)}"
                        # 计算期望的视频任务数（按10s一个任务）
                        import math
                        expected_tasks = max(1, math.ceil(total_duration / 10.0))
                        
                        await session_manager.register_session(
                            run_id=run_id,
                            session_id=session_id,
                            expected_clips=expected_tasks,
                            context={
                                "goal": goal,
                                "styles": styles,
                                "total_duration": total_duration,
                                "expected_tasks": expected_tasks,
                                "image_control": image_control,
                                "status": "running"
                            }
                        )
                        logger.info(
                            f"[crewai-agent] Registered session: run_id={run_id}, "
                            f"session_id={session_id}, expected_tasks={expected_tasks}"
                        )
                except Exception as e:
                    logger.warning(f"[crewai-agent] Failed to register session: {e}")
                    # 继续执行，不阻塞工作流
                
                # 1. 创意策划
                async for chunk in emit("创意策划", "thought", run_id, thread_id, 
                                       delta="💡 创意策划：分析需求，制定创意策略…", 
                                       progress={"current": 1, "total": 6}):
                    yield chunk
                
                # 2. 导演分镜
                async for chunk in emit("导演", "thought", run_id, thread_id, 
                                       delta="🎬 导演：规划分镜脚本，拆分镜头…", 
                                       progress={"current": 2, "total": 6}):
                    yield chunk
                
                # 3. 审核
                async for chunk in emit("审核", "thought", run_id, thread_id, 
                                       delta="✅ 审核：检查分镜质量，合并镜头为视频任务…", 
                                       progress={"current": 3, "total": 6}):
                    yield chunk
                
                # 4. 视觉设计（可选）
                if image_control:
                    async for chunk in emit("视觉设计", "thought", run_id, thread_id, 
                                           delta="🎨 视觉设计：生成关键帧…", 
                                           progress={"current": 4, "total": 6}):
                        yield chunk
                
                # 5. 制片 - 提交视频生成任务
                async for chunk in emit("制片", "thought", run_id, thread_id, 
                                       delta="📹 制片：提交视频生成任务（使用 Sora2）…", 
                                       progress={"current": 5, "total": 6}):
                    yield chunk
                
                # 执行 CrewAI 工作流
                crew = build_crew(payload)
                
                # 在后台线程中执行同步的 CrewAI
                import concurrent.futures
                loop = asyncio.get_event_loop()
                
                async for chunk in emit("System", "info", run_id, thread_id, 
                                       delta="⚙️ 执行 CrewAI 工作流中…"):
                    yield chunk
                
                # 在线程池中执行同步的 CrewAI
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    result = await loop.run_in_executor(
                        executor,
                        lambda: crew.kickoff(inputs={"storyboards_json": ""})
                    )
                
                result_str = str(result)
                
                # 检查结果
                if "pending" in result_str.lower() or "处理中" in result_str:
                    async for chunk in emit("制片", "info", run_id, thread_id, 
                                           delta="⏳ 视频生成任务已提交，正在处理中（3-5分钟）…", 
                                           payload={"status": "processing"}):
                        yield chunk
                    
                    # 注册任务到数据库
                    if supabase:
                        supabase.table("jobs").upsert({
                            "run_id": run_id,
                            "slogan": goal,
                            "status": "processing",
                            "updated_at": datetime.utcnow().isoformat()
                        }, on_conflict="run_id").execute()
                    
                    # 等待任务完成
                    async for chunk in emit("System", "info", run_id, thread_id, 
                                           delta="📡 等待视频生成完成，完成后将自动拼接…"):
                        yield chunk
                    
                    # 发送完成事件，确保流正确关闭
                    async for chunk in emit("System", "run_finished", run_id, thread_id, 
                                           delta="⏳ 任务已提交，请等待处理完成…", 
                                           progress={"current": 5, "total": 6},
                                           payload={"status": "processing", "run_id": run_id}):
                        yield chunk
                    return
                elif "http" in result_str.lower() or ".mp4" in result_str.lower():
                    # 任务已完成，提取视频 URL
                    import re
                    url_match = re.search(r'https?://[^\s<>"{}|\\^`\[\]]+\.mp4', result_str)
                    if url_match:
                        video_url = url_match.group(0)
                        cdn_url = await upload_url_to_r2(video_url, f"{run_id}.mp4")
                        
                        # 持久化成功
                        share_slug = await persist_success(run_id, goal, "", cdn_url)
                        
                        async for chunk in emit("System", "run_finished", run_id, thread_id, 
                                               delta=f"🎬 最终视频已生成：{cdn_url}", 
                                               progress={"current": 6, "total": 6}, 
                                               payload={"share_slug": share_slug, "video_url": cdn_url}):
                            yield chunk
                    else:
                        async for chunk in emit("System", "error", run_id, thread_id, 
                                               delta=f"⚠️ 工作流完成，但未找到视频 URL。结果：{result_str[:200]}"):
                            yield chunk
                else:
                    async for chunk in emit("System", "error", run_id, thread_id, 
                                           delta=f"❌ 工作流执行失败：{result_str[:200]}"):
                        yield chunk
                        
            except Exception as e:
                logger.error(f"[crewai-agent] Error: {e}", exc_info=True)
                try:
                    async for chunk in emit("System", "error", run_id, thread_id, 
                                           delta=f"❌ 错误：{str(e)}"):
                        yield chunk
                    # 发送完成事件，确保流正确关闭
                    async for chunk in emit("System", "run_finished", run_id, thread_id, 
                                           delta="❌ 任务失败", 
                                           progress={"current": 6, "total": 6},
                                           payload={"status": "failed", "error": str(e)}):
                        yield chunk
                except Exception as inner_e:
                    logger.error(f"[crewai-agent] Error sending error message: {inner_e}", exc_info=True)
        
        return StreamingResponse(generator(), media_type="text/event-stream")
    else:
        # 使用旧的简单流程（向后兼容）
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
    
    # 优先从 video_tasks 表查找（新的队列系统）
    video_task = None
    try:
        # 先尝试用 provider_task_id 查找
        video_task_result = supabase.table("video_tasks")\
            .select("run_id, clip_idx")\
            .eq("provider_task_id", task_id)\
            .single()\
            .execute()
        if video_task_result.data:
            video_task = video_task_result.data
        else:
            # 如果没找到，尝试用 id 查找（可能是直接提交的任务，provider_task_id 可能还没设置）
            video_task_result = supabase.table("video_tasks")\
                .select("run_id, clip_idx, provider_task_id")\
                .eq("id", task_id)\
                .single()\
                .execute()
            if video_task_result.data:
                video_task = video_task_result.data
    except Exception as e:
        logger.debug(f"[webhook_runninghub] Error finding video_task: {e}")
        pass
    
    # 如果找到 video_task，使用新的队列系统处理
    if video_task:
        run_id = video_task.get("run_id")
        clip_idx = video_task.get("clip_idx")
        
        if status in {"success", "finished", "done"}:
            # 获取视频 URL
            video_url = None
            for item in outputs:
                url = (
                    item.get("fileUrl") 
                    or item.get("url") 
                    or item.get("ossUrl") 
                    or item.get("downloadUrl")
                    or (item.get("value") if isinstance(item.get("value"), str) else None)
                )
                ftype = (item.get("fileType") or item.get("type") or "").lower()
                if url and isinstance(url, str):
                    url_lower = url.lower()
                    if (
                        "mp4" in url_lower 
                        or url_lower.endswith(".mp4")
                        or ftype in {"mp4", "video", "video/mp4"}
                    ):
                        video_url = url
                        break
            
            if video_url:
                # 上传到 R2
                cdn_url = await upload_url_to_r2(video_url, f"{run_id}_clip{clip_idx}.mp4")
                
                # 更新 video_tasks 表
                supabase.table("video_tasks")\
                    .update({
                        "status": "succeeded",
                        "video_url": cdn_url,
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("provider_task_id", task_id)\
                    .execute()
                
                logger.info(f"[webhook_runninghub] Video task {task_id} completed: {cdn_url}")
                
                # 检查是否所有任务完成，如果完成则触发拼接回调
                try:
                    from crewai_session_manager import get_session_manager
                    session_manager = get_session_manager()
                    if session_manager:
                        # 异步检查并触发拼接（不阻塞）
                        asyncio.create_task(
                            session_manager.check_and_trigger_stitch(run_id)
                        )
                except Exception as e:
                    logger.debug(f"[webhook_runninghub] Failed to trigger stitch callback: {e}")
        
        return {"ok": True}
    
    # 降级：使用旧的 jobs 表查找（兼容旧系统）
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
    res = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, status, storyboards, total_duration, styles, image_control, created_at, updated_at").eq("run_id", run_id).single().execute()
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
    res = supabase.table("jobs").select("run_id, slogan, cover_url, video_url, share_slug, status, storyboards, total_duration, styles, image_control, created_at, updated_at").eq("share_slug", slug).single().execute()
    return res.data or {}


async def _get_embedding(text: str) -> list[float] | None:
    if not (OPENROUTER_BASE and OPENROUTER_KEY and text):
        return None
    try:
        # 构建请求头，支持 OpenRouter
        headers = {
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
        }
        # OpenRouter 需要 HTTP-Referer header
        if "openrouter.ai" in OPENROUTER_BASE:
            headers["HTTP-Referer"] = EMBED_REFERER
            headers["X-Title"] = "SaleAgent"
        
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{OPENROUTER_BASE}/embeddings",
                headers=headers,
                json={"input": text, "model": EMBED_MODEL},
            )
            r.raise_for_status()
            data = r.json()
            return data["data"][0]["embedding"]
    except Exception:
        return None


@app.post("/crewai-chat")
async def crewai_chat(request: Request):
    """
    对话式视频生成信息收集端点
    使用创意策划 agent 通过多轮对话收集信息，支持 HITL
    """
    body = await request.json()
    action = body.get("action", "start")
    thread_id = body.get("thread_id") or f"t_{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"r_{uuid.uuid4().hex[:8]}"
    user_message = body.get("message", "")
    
    # 存储对话状态（在实际应用中应该使用 Redis 或数据库）
    # 这里使用内存存储，仅用于演示
    if not hasattr(crewai_chat, "_conversation_states"):
        crewai_chat._conversation_states = {}
    
    state_key = f"{thread_id}_{run_id}"
    conversation_state = crewai_chat._conversation_states.get(state_key, {
        "collected_info": {},
        "current_question": None,
        "question_history": [],
    })
    
    async def generator():
        try:
            try:
                from creative_agent import (
                    build_creative_agent_for_chat,
                    generate_question_with_options,
                    get_next_question,
                )
            except ImportError:
                # 如果导入失败，使用内联实现
                def generate_question_with_options(question_type: str, current_info: dict):
                    VIDEO_TYPES = ["产品宣传视频", "品牌故事视频", "教程视频", "活动推广视频", "社交媒体短视频", "广告片", "产品演示视频", "其他"]
                    DURATION_OPTIONS = [10, 20, 30, 60, 90, 120]
                    STYLE_OPTIONS = ["现代简约", "科技感", "温馨生活", "时尚潮流", "商务专业", "创意艺术", "自然清新", "复古怀旧", "动感活力", "优雅高端"]
                    CONSISTENCY_ELEMENTS = ["品牌Logo", "产品外观", "人物形象", "色彩方案", "字体样式", "包装设计", "用户界面", "场景背景"]
                    
                    if question_type == "video_type":
                        return ("首先，请告诉我您想要制作什么类型的视频？", VIDEO_TYPES)
                    elif question_type == "duration":
                        return ("好的，您希望视频的时长是多少秒？", [f"{d}秒" for d in DURATION_OPTIONS])
                    elif question_type == "style":
                        return ("请选择视频的风格（可以选择多个）：", STYLE_OPTIONS)
                    elif question_type == "theme":
                        return ("请描述视频的主题或核心内容：", [])
                    elif question_type == "keywords":
                        return ("请提供一些关键词，这些关键词将帮助理解视频的核心信息（用逗号分隔）：", [])
                    elif question_type == "key_elements":
                        return ("请列出视频中需要重点展示的关键元素（用逗号分隔）：", [])
                    elif question_type == "consistency_elements":
                        return ("为了确保视频的一致性，请选择需要在所有场景中保持一致的元素（可以选择多个）：", CONSISTENCY_ELEMENTS)
                    return ("", [])
                
                def get_next_question(current_info: dict):
                    if "video_type" not in current_info:
                        return "video_type"
                    if "duration" not in current_info:
                        return "duration"
                    if "styles" not in current_info or len(current_info.get("styles", [])) == 0:
                        return "style"
                    if "theme" not in current_info:
                        return "theme"
                    if "keywords" not in current_info or len(current_info.get("keywords", [])) == 0:
                        return "keywords"
                    if "key_elements" not in current_info or len(current_info.get("key_elements", [])) == 0:
                        return "key_elements"
                    if "consistency_elements" not in current_info or len(current_info.get("consistency_elements", [])) == 0:
                        return "consistency_elements"
                    return None
                
                def collect_video_info_tool(current_info: str, question_type: str, user_response: str = None):
                    info = json.loads(current_info) if current_info else {}
                    if user_response:
                        if question_type == "video_type":
                            info["video_type"] = user_response
                        elif question_type == "duration":
                            try:
                                info["duration"] = float(user_response.replace("秒", ""))
                            except:
                                info["duration"] = 10.0
                        elif question_type == "style":
                            if "styles" not in info:
                                info["styles"] = []
                            if user_response not in info["styles"]:
                                info["styles"].append(user_response)
                        elif question_type == "theme":
                            info["theme"] = user_response
                        elif question_type == "keywords":
                            if "keywords" not in info:
                                info["keywords"] = []
                            keywords = [k.strip() for k in user_response.split(",") if k.strip()]
                            info["keywords"].extend(keywords)
                        elif question_type == "key_elements":
                            if "key_elements" not in info:
                                info["key_elements"] = []
                            elements = [e.strip() for e in user_response.split(",") if e.strip()]
                            info["key_elements"].extend(elements)
                        elif question_type == "consistency_elements":
                            if "consistency_elements" not in info:
                                info["consistency_elements"] = []
                            elements = [e.strip() for e in user_response.split(",") if e.strip()]
                            info["consistency_elements"].extend(elements)
                    return json.dumps(info, ensure_ascii=False)
            
            # 注意：这里不再需要创建 agent 实例，因为对话逻辑是直接实现的
            # 收集的信息会直接传递给后续的 build_crew 工作流
            
            if action == "start":
                # 开始对话
                async for chunk in emit("System", "info", run_id, thread_id, 
                                       delta="创意策划开始收集视频制作信息..."):
                    yield chunk
                
                # 生成第一个问题
                next_q = get_next_question(conversation_state["collected_info"])
                if next_q:
                    question, options = generate_question_with_options(
                        next_q, conversation_state["collected_info"]
                    )
                    conversation_state["current_question"] = next_q
                    
                    async for chunk in emit("创意策划", "question", run_id, thread_id,
                                           delta=question,
                                           payload={"options": options, "question_type": next_q}):
                        yield chunk
                
                crewai_chat._conversation_states[state_key] = conversation_state
                
            elif action == "message" and user_message:
                # 处理用户回答
                current_q = conversation_state.get("current_question")
                if current_q:
                    # 更新收集的信息（直接使用 Python 逻辑，不通过 Tool）
                    info = conversation_state["collected_info"]
                    if current_q == "video_type":
                        info["video_type"] = user_message
                    elif current_q == "duration":
                        try:
                            info["duration"] = float(user_message.replace("秒", "").strip())
                        except:
                            info["duration"] = 10.0
                    elif current_q == "style":
                        if "styles" not in info:
                            info["styles"] = []
                        if user_message not in info["styles"]:
                            info["styles"].append(user_message)
                    elif current_q == "theme":
                        info["theme"] = user_message
                    elif current_q == "keywords":
                        if "keywords" not in info:
                            info["keywords"] = []
                        keywords = [k.strip() for k in user_message.split(",") if k.strip()]
                        info["keywords"].extend(keywords)
                    elif current_q == "key_elements":
                        if "key_elements" not in info:
                            info["key_elements"] = []
                        elements = [e.strip() for e in user_message.split(",") if e.strip()]
                        info["key_elements"].extend(elements)
                    elif current_q == "consistency_elements":
                        if "consistency_elements" not in info:
                            info["consistency_elements"] = []
                        elements = [e.strip() for e in user_message.split(",") if e.strip()]
                        info["consistency_elements"].extend(elements)
                    
                    conversation_state["collected_info"] = info
                    conversation_state["question_history"].append({
                        "question": current_q,
                        "answer": user_message
                    })
                
                # 检查是否所有信息已收集完成
                next_q = get_next_question(conversation_state["collected_info"])
                
                if next_q:
                    # 还有问题要问
                    question, options = generate_question_with_options(
                        next_q, conversation_state["collected_info"]
                    )
                    conversation_state["current_question"] = next_q
                    
                    async for chunk in emit("创意策划", "question", run_id, thread_id,
                                           delta=question,
                                           payload={"options": options, "question_type": next_q}):
                        yield chunk
                else:
                    # 所有信息已收集完成
                    async for chunk in emit("System", "collected", run_id, thread_id,
                                           delta="信息收集完成！",
                                           payload=conversation_state["collected_info"]):
                        yield chunk
                    
                    # 开始生成视频
                    async for chunk in emit("System", "generating", run_id, thread_id,
                                           delta="开始生成视频..."):
                        yield chunk
                    
                    # 构建视频生成工作流
                    collected = conversation_state["collected_info"]
                    goal = collected.get("theme", "") or collected.get("video_type", "")
                    styles = collected.get("styles", [])
                    total_duration = float(collected.get("duration", 10.0))
                    
                    # 构建完整的目标描述，包含所有收集的信息
                    goal_parts = [goal] if goal else []
                    if collected.get("video_type"):
                        goal_parts.append(f"视频类型：{collected.get('video_type')}")
                    if collected.get("keywords"):
                        goal_parts.append(f"关键词：{', '.join(collected.get('keywords', []))}")
                    if collected.get("key_elements"):
                        goal_parts.append(f"关键元素：{', '.join(collected.get('key_elements', []))}")
                    if collected.get("consistency_elements"):
                        goal_parts.append(f"一致性元素：{', '.join(collected.get('consistency_elements', []))}")
                    
                    full_goal = " | ".join(goal_parts) if goal_parts else goal
                    
                    payload = {
                        "goal": full_goal,  # 使用完整的目标描述
                        "styles": styles,
                        "total_duration": total_duration,
                        "run_id": run_id,
                        "keywords": collected.get("keywords", []),
                        "key_elements": collected.get("key_elements", []),
                        "consistency_elements": collected.get("consistency_elements", []),
                        "video_type": collected.get("video_type", ""),  # 添加视频类型
                    }
                    
                    # 执行视频生成工作流
                    crew = build_crew(payload)
                    
                    import concurrent.futures
                    loop = asyncio.get_event_loop()
                    
                    async for chunk in emit("System", "info", run_id, thread_id,
                                           delta="⚙️ 执行视频生成工作流中…"):
                        yield chunk
                    
                    # 存储 storyboard 确认状态
                    if not hasattr(crewai_chat, "_storyboard_confirmations"):
                        crewai_chat._storyboard_confirmations = {}
                    
                    # 执行工作流，分阶段执行以支持 human input
                    # 第一阶段：执行到 director_agent 生成 storyboard
                    async for chunk in emit("导演", "thought", run_id, thread_id,
                                           delta="🎬 导演正在规划分镜脚本…"):
                        yield chunk
                    
                    # 直接调用 plan_storyboard_impl 生成 storyboard
                    from crewai_tools import plan_storyboard_impl
                    goal = payload.get("goal", "")
                    styles = payload.get("styles", [])
                    total_duration = float(payload.get("total_duration", 10.0))
                    # 根据总时长计算分镜数量，确保每段不超过10秒
                    import math
                    num_clips = max(1, math.ceil(total_duration / 10.0))
                    
                    # plan_storyboard_impl 是异步函数，直接 await
                    storyboard_json = await plan_storyboard_impl(goal, styles, total_duration, num_clips)
                    
                    # 解析 storyboard 数据
                    storyboard_data = None
                    try:
                        storyboard_data = json.loads(storyboard_json)
                    except Exception as e:
                        logger.warning(f"[crewai-chat] Failed to parse storyboard: {e}")
                        # 尝试从字符串中提取
                        json_match = re.search(r'\{.*?"scenes".*?\}', storyboard_json, re.DOTALL)
                        if json_match:
                            storyboard_data = json.loads(json_match.group(0))
                    
                    # 如果提取到了 storyboard 数据，发送给前端并等待确认
                    if storyboard_data and "scenes" in storyboard_data:
                        # 生成 scene 图片
                        async for chunk in emit("视觉设计", "thought", run_id, thread_id,
                                               delta="🎨 正在为场景生成预览图片…"):
                            yield chunk
                        
                        # 为每个 scene 生成图片
                        # 直接调用内部实现，不使用 Tool 包装
                        from providers import get_image_provider
                        image_provider = get_image_provider()
                        
                        # 为每个 scene 生成预览图片
                        scenes = storyboard_data.get("scenes", [])
                        for scene in scenes:
                            scene_idx = scene.get("scene_idx", 1)
                            clips = scene.get("clips", [])
                            # 合并所有 clips 的描述作为 scene 的描述
                            scene_desc = "；".join([clip.get("desc", "") for clip in clips if clip.get("desc")])
                            if not scene_desc:
                                scene_desc = f"场景{scene_idx}"
                            
                            try:
                                # 为 scene 生成一张代表性图片
                                # 【重要】避免生成带有人脸的图片，因为 sora2 不支持使用真人图片作为参考
                                image_prompt = f"{scene_desc}，视频场景画面，无人脸、无真人，无人物形象，无人物形象"
                                image_url = await image_provider.generate(image_prompt)
                                scene["image_url"] = image_url
                                logger.info(f"[crewai-chat] Generated image for scene {scene_idx}: {image_url}")
                            except Exception as e:
                                logger.warning(f"[crewai-chat] Failed to generate image for scene {scene_idx}: {e}")
                                scene["image_url"] = None
                        
                        # storyboard_data 已经更新，包含 image_url
                        
                        # 发送 storyboard 给前端，等待用户确认
                        async for chunk in emit("System", "storyboard_pending", run_id, thread_id,
                                               delta="故事板已生成，请审核并确认",
                                               payload={
                                                   "storyboard": storyboard_data,
                                                   "requires_confirmation": True
                                               }):
                            yield chunk
                        
                        # 等待用户确认
                        confirmation_key = f"{run_id}_storyboard"
                        crewai_chat._storyboard_confirmations[confirmation_key] = {
                            "status": "pending",
                            "storyboard": storyboard_data,
                        }
                        
                        # 轮询等待确认（最多等待 30 分钟，允许用户长时间不操作）
                        import time
                        max_wait_time = 1800  # 30 分钟，允许用户长时间思考
                        start_time = time.time()
                        confirmed = False
                        last_heartbeat = time.time()
                        heartbeat_interval = 30  # 每30秒发送一次心跳，保持连接活跃
                        
                        while time.time() - start_time < max_wait_time:
                            await asyncio.sleep(1)  # 每秒检查一次
                            
                            # 发送心跳消息，保持 SSE 连接活跃（每30秒一次）
                            current_time = time.time()
                            if current_time - last_heartbeat >= heartbeat_interval:
                                elapsed_minutes = int((current_time - start_time) / 60)
                                async for chunk in emit("System", "heartbeat", run_id, thread_id,
                                                       delta=f"⏳ 等待您的确认...（已等待 {elapsed_minutes} 分钟）"):
                                    yield chunk
                                last_heartbeat = current_time
                            
                            # 优先检查 confirm_storyboard 端点的确认状态（用户点击确认后存储在这里）
                            confirmation = None
                            if hasattr(confirm_storyboard, "_confirmations"):
                                confirmation = confirm_storyboard._confirmations.get(confirmation_key)
                            
                            # 如果 confirm_storyboard 中没有，再检查 crewai_chat 的存储
                            if not confirmation:
                                confirmation = crewai_chat._storyboard_confirmations.get(confirmation_key)
                            
                            # 如果找到确认状态，同步到 crewai_chat 的存储
                            if confirmation:
                                crewai_chat._storyboard_confirmations[confirmation_key] = confirmation
                            
                            if confirmation and confirmation.get("status") == "confirmed":
                                # 用户已确认，继续执行
                                confirmed = True
                                logger.info(f"[crewai-chat] Storyboard confirmed for {run_id}, continuing workflow...")
                                break
                            elif confirmation and confirmation.get("status") == "rejected":
                                # 用户拒绝，需要重新生成
                                async for chunk in emit("System", "info", run_id, thread_id,
                                                       delta="用户要求重新生成故事板，正在重新规划…"):
                                    yield chunk
                                # 重新生成 storyboard（plan_storyboard_impl 是异步函数）
                                import math
                                num_clips = max(1, math.ceil(total_duration / 10.0))
                                storyboard_json = await plan_storyboard_impl(goal, styles, total_duration, num_clips)
                                
                                # 重新解析和生成图片
                                try:
                                    storyboard_data = json.loads(storyboard_json)
                                except:
                                    json_match = re.search(r'\{.*?"scenes".*?\}', storyboard_json, re.DOTALL)
                                    if json_match:
                                        storyboard_data = json.loads(json_match.group(0))
                                
                                # 重新生成图片（直接调用内部实现）
                                from providers import get_image_provider
                                image_provider = get_image_provider()
                                
                                scenes = storyboard_data.get("scenes", [])
                                for scene in scenes:
                                    scene_idx = scene.get("scene_idx", 1)
                                    clips = scene.get("clips", [])
                                    scene_desc = "；".join([clip.get("desc", "") for clip in clips if clip.get("desc")])
                                    if not scene_desc:
                                        scene_desc = f"场景{scene_idx}"
                                    
                                    try:
                                        # 【重要】避免生成带有人脸的图片，因为 sora2 不支持使用真人图片作为参考
                                        image_prompt = f"{scene_desc}，视频场景画面，无人脸、无真人，无人物形象"
                                        image_url = await image_provider.generate(image_prompt)
                                        scene["image_url"] = image_url
                                        logger.info(f"[crewai-chat] Regenerated image for scene {scene_idx}: {image_url}")
                                    except Exception as e:
                                        logger.warning(f"[crewai-chat] Failed to regenerate image for scene {scene_idx}: {e}")
                                        scene["image_url"] = None
                                
                                # 再次发送给用户确认
                                async for chunk in emit("System", "storyboard_pending", run_id, thread_id,
                                                       delta="已重新生成故事板，请审核",
                                                       payload={
                                                           "storyboard": storyboard_data,
                                                           "requires_confirmation": True,
                                                           "run_id": run_id
                                                       }):
                                    yield chunk
                                
                                crewai_chat._storyboard_confirmations[confirmation_key] = {
                                    "status": "pending",
                                    "storyboard": storyboard_data,
                                }
                                start_time = time.time()  # 重置计时器
                                continue
                        else:
                            # 超时，继续执行
                            logger.warning(f"[crewai-chat] Storyboard confirmation timeout for {run_id}")
                            confirmed = True  # 超时后默认继续
                    
                    # 继续执行后续流程（审核、合并、生成视频等）
                    if confirmed and storyboard_data:
                        logger.info(f"[crewai-chat] Starting remaining workflow for {run_id}, storyboard_data exists: {bool(storyboard_data)}")
                        async for chunk in emit("System", "info", run_id, thread_id,
                                               delta="故事板已确认，继续执行后续流程…"):
                            yield chunk
                        
                        # 执行剩余的工作流（从审核开始）
                        # 使用已确认的 storyboard 继续执行完整工作流
                        storyboards_json = json.dumps(storyboard_data, ensure_ascii=False)
                        
                        # 发送审核阶段开始
                        async for chunk in emit("审核", "thought", run_id, thread_id,
                                               delta="✅ 审核：检查分镜质量，合并镜头为视频任务…"):
                            yield chunk
                        
                        # 执行审核和合并任务（跳过plan，因为storyboard已经确认）
                        from crewai_tools import review_storyboard_impl, merge_storyboards_to_video_tasks_impl
                        
                        try:
                            # 审核storyboard（同步函数，可以直接调用实现函数）
                            reviewed_storyboard_json = review_storyboard_impl(
                                storyboards_json, 
                                num_clips=1,  # 使用默认值
                                goal=payload.get("goal", ""), 
                                styles=payload.get("styles", []), 
                                total_duration=float(payload.get("total_duration", 10.0))
                            )
                            reviewed_storyboard = json.loads(reviewed_storyboard_json) if isinstance(reviewed_storyboard_json, str) else reviewed_storyboard_json
                            
                            async for chunk in emit("审核", "tool_result", run_id, thread_id,
                                                   delta="✅ 分镜脚本审核通过"):
                                yield chunk
                            
                            # 合并为视频任务（同步函数，可以直接调用实现函数）
                            async for chunk in emit("审核", "thought", run_id, thread_id,
                                                   delta="📋 正在合并镜头为视频任务…"):
                                yield chunk
                            
                            video_tasks_json = merge_storyboards_to_video_tasks_impl(
                                reviewed_storyboard_json, 
                                run_id, 
                                float(payload.get("total_duration", 10.0))
                            )
                            video_tasks = json.loads(video_tasks_json) if isinstance(video_tasks_json, str) else video_tasks_json
                            
                            async for chunk in emit("审核", "tool_result", run_id, thread_id,
                                                   delta=f"✅ 已创建 {len(video_tasks)} 个视频任务"):
                                yield chunk
                            
                            # 开始生成视频片段
                            async for chunk in emit("制片", "thought", run_id, thread_id,
                                                   delta="📹 制片：开始生成视频片段…"):
                                yield chunk
                            
                            # 调用视频生成工具，并实时发送进度
                            from crewai_tools import generate_video_clip_tool
                            
                            # 发送生成开始信息
                            async for chunk in emit("制片", "info", run_id, thread_id,
                                                   delta=f"正在为 {len(video_tasks)} 个场景生成视频片段…"):
                                yield chunk
                            
                            # 执行视频生成（这会提交任务到队列）
                            # 使用内部实现函数，可以直接await
                            from crewai_tools import generate_video_clip_impl
                            clip_results_json = await generate_video_clip_impl(video_tasks_json, run_id)
                            clip_results = json.loads(clip_results_json) if isinstance(clip_results_json, str) else clip_results_json
                            
                            # 发送任务提交结果
                            submitted_count = sum(1 for r in clip_results if r.get("status") in ["submitted", "pending"])
                            async for chunk in emit("制片", "tool_result", run_id, thread_id,
                                                   delta=f"✅ 已提交 {submitted_count} 个视频生成任务，正在处理中…"):
                                yield chunk
                            
                            # 轮询视频任务状态，直到所有任务完成
                            import time
                            import os
                            from supabase import create_client
                            
                            supabase_url = os.getenv("SUPABASE_URL")
                            supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
                            supabase_client = None
                            if supabase_url and supabase_key:
                                supabase_client = create_client(supabase_url, supabase_key)
                            
                            # 存储已完成的视频片段
                            completed_clips = {}
                            max_wait_time = 1800  # 最多等待30分钟，视频生成需要较长时间
                            start_time = time.time()
                            check_interval = 3  # 每3秒检查一次
                            last_heartbeat = time.time()
                            heartbeat_interval = 30  # 每30秒发送一次心跳，保持连接活跃
                            
                            while time.time() - start_time < max_wait_time:
                                await asyncio.sleep(check_interval)
                                
                                # 发送心跳消息，保持 SSE 连接活跃（每30秒一次）
                                current_time = time.time()
                                if current_time - last_heartbeat >= heartbeat_interval:
                                    elapsed_minutes = int((current_time - start_time) / 60)
                                    completed_count = len(completed_clips)
                                    total_count = len(video_tasks)
                                    async for chunk in emit("制片", "heartbeat", run_id, thread_id,
                                                           delta=f"⏳ 视频生成中... 已完成 {completed_count}/{total_count} 个片段（已等待 {elapsed_minutes} 分钟）"):
                                        yield chunk
                                    last_heartbeat = current_time
                                
                                # 查询video_tasks表获取最新状态
                                if supabase_client:
                                    try:
                                        tasks_res = supabase_client.table("video_tasks").select("*").eq("run_id", run_id).execute()
                                        tasks = tasks_res.data if tasks_res.data else []
                                        
                                        # 检查是否有新完成的片段
                                        for task in tasks:
                                            task_idx = task.get("clip_idx") or task.get("task_idx")
                                            status = task.get("status")
                                            video_url = task.get("video_url")
                                            
                                            if task_idx and status == "succeeded" and video_url:
                                                if task_idx not in completed_clips:
                                                    completed_clips[task_idx] = {
                                                        "task_idx": task_idx,
                                                        "video_url": video_url,
                                                        "status": "succeeded"
                                                    }
                                                    
                                                    # 发送视频片段完成事件
                                                    async for chunk in emit("制片", "video_clip_completed", run_id, thread_id,
                                                                           delta=f"✅ 场景 {task_idx} 视频片段生成完成",
                                                                           payload={
                                                                               "task_idx": task_idx,
                                                                               "video_url": video_url,
                                                                               "status": "succeeded",
                                                                               "requires_confirmation": True
                                                                           }):
                                                        yield chunk
                                        
                                        # 检查是否所有任务都完成了
                                        all_completed = all(
                                            task.get("status") in ["succeeded", "failed"] 
                                            for task in tasks
                                        )
                                        
                                        if all_completed and len(tasks) > 0:
                                            # 发送所有完成的视频片段给前端确认
                                            all_clips = []
                                            for task in tasks:
                                                if task.get("status") == "succeeded" and task.get("video_url"):
                                                    all_clips.append({
                                                        "task_idx": task.get("clip_idx") or task.get("task_idx"),
                                                        "video_url": task.get("video_url"),
                                                        "status": "succeeded"
                                                    })
                                            
                                            if all_clips:
                                                async for chunk in emit("System", "video_clips_pending", run_id, thread_id,
                                                                       delta="所有视频片段已生成，请审核并确认",
                                                                       payload={
                                                                           "clips": all_clips,
                                                                           "requires_confirmation": True
                                                                       }):
                                                    yield chunk
                                            break
                                        
                                        # 发送进度更新
                                        completed_count = sum(1 for task in tasks if task.get("status") == "succeeded")
                                        total_count = len(tasks)
                                        if total_count > 0:
                                            progress_pct = int((completed_count / total_count) * 100)
                                            async for chunk in emit("制片", "progress", run_id, thread_id,
                                                                   delta=f"生成进度：{completed_count}/{total_count} ({progress_pct}%)",
                                                                   progress={"current": completed_count, "total": total_count}):
                                                yield chunk
                                    except Exception as e:
                                        logger.warning(f"[crewai-chat] Error checking video tasks: {e}")
                                
                                # 如果已经有所有片段，也退出
                                if len(completed_clips) >= len(video_tasks):
                                    break
                            
                            # 如果超时，发送当前状态
                            if time.time() - start_time >= max_wait_time:
                                async for chunk in emit("System", "info", run_id, thread_id,
                                                       delta="⏳ 视频生成仍在进行中，完成后将自动通知…"):
                                    yield chunk
                            
                        except Exception as e:
                            logger.error(f"[crewai-chat] Error executing remaining workflow: {e}", exc_info=True)
                            async for chunk in emit("System", "error", run_id, thread_id,
                                                   delta=f"执行后续流程时出错：{str(e)}"):
                                yield chunk
                            return
                    else:
                        logger.warning(f"[crewai-chat] Skipping remaining workflow: confirmed={confirmed}, storyboard_data exists={bool(storyboard_data)}")
                        if not confirmed:
                            async for chunk in emit("System", "error", run_id, thread_id,
                                                   delta="故事板未确认，无法继续执行"):
                                yield chunk
                        elif not storyboard_data:
                            async for chunk in emit("System", "error", run_id, thread_id,
                                                   delta="故事板数据缺失，无法继续执行"):
                                yield chunk
                        return
                    
                    # 注意：视频生成流程已经通过轮询和事件发送给前端
                    # 视频片段的完成状态会通过 video_clip_completed 和 video_clips_pending 事件发送
                    # 最终视频的拼接会在所有片段确认后自动触发
                    # 这里不需要检查 result，因为我们已经改变了工作流执行方式
                
                crewai_chat._conversation_states[state_key] = conversation_state
            
        except Exception as e:
            logger.error(f"[crewai-chat] Error: {e}", exc_info=True)
            async for chunk in emit("System", "error", run_id, thread_id,
                                   delta=f"❌ 错误：{str(e)}"):
                yield chunk
    
    return StreamingResponse(generator(), media_type="text/event-stream")


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


class VideoClipsConfirmRequest(BaseModel):
    run_id: str
    confirmed: bool = True


@app.post("/crewai/video-clips/confirm")
async def crewai_video_clips_confirm(body: VideoClipsConfirmRequest):
    """
    确认所有视频片段并触发拼接
    
    从数据库获取所有已完成的视频片段，然后调用拼接函数。
    """
    import os
    import json
    from supabase import create_client
    
    if not body.confirmed:
        return {"error": "需要确认所有视频片段"}
    
    try:
        # 从数据库获取所有视频片段
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        
        if not supabase_url or not supabase_key:
            return {"error": "Supabase 配置缺失"}
        
        supabase = create_client(supabase_url, supabase_key)
        
        # 先检查 crew_sessions 表的状态，如果已经在拼接或已完成，直接返回结果
        try:
            session_result = supabase.table("crew_sessions")\
                .select("status, result")\
                .eq("run_id", body.run_id)\
                .execute()
            
            if session_result.data and len(session_result.data) > 0:
                session = session_result.data[0]
                current_status = session.get("status", "")
                result = session.get("result", "")
                
                # 如果已经完成且有有效结果，直接返回
                if current_status == "completed" and result and "http" in result.lower() and "example.com" not in result.lower():
                    logger.info(
                        f"[crewai_video_clips_confirm] Session already completed for run_id={body.run_id}, "
                        f"returning existing result: {result[:100]}"
                    )
                    return {
                        "run_id": body.run_id,
                        "final_url": result,
                        "status": "completed",
                        "from_cache": True
                    }
                
                # 如果正在拼接，返回提示信息
                if current_status == "stitching":
                    logger.info(
                        f"[crewai_video_clips_confirm] Session is already stitching for run_id={body.run_id}, "
                        f"please wait for completion"
                    )
                    return {
                        "run_id": body.run_id,
                        "status": "stitching",
                        "message": "视频正在拼接中，请稍候..."
                    }
        except Exception as e:
            logger.debug(f"[crewai_video_clips_confirm] Failed to check crew_sessions: {e}")
            # 继续执行，不阻塞
        
        # 查询所有已完成的视频任务
        tasks_res = supabase.table("video_tasks")\
            .select("*")\
            .eq("run_id", body.run_id)\
            .order("clip_idx", desc=False)\
            .execute()
        
        tasks = tasks_res.data if tasks_res.data else []
        
        if not tasks:
            return {"error": f"未找到 run_id={body.run_id} 的视频任务"}
        
        # 检查是否所有任务都已完成
        completed_tasks = [t for t in tasks if t.get("status") == "succeeded" and t.get("video_url")]
        pending_tasks = [t for t in tasks if t.get("status") in ["pending", "submitted", "processing"]]
        failed_tasks = [t for t in tasks if t.get("status") == "failed"]
        
        if pending_tasks:
            return {
                "error": f"仍有 {len(pending_tasks)} 个视频任务在处理中，无法拼接",
                "pending_count": len(pending_tasks),
                "completed_count": len(completed_tasks),
                "total_count": len(tasks)
            }
        
        if not completed_tasks:
            return {
                "error": "没有已完成的视频片段",
                "failed_count": len(failed_tasks),
                "total_count": len(tasks)
            }
        
        # 构建 clip_results 格式（用于 stitch_video_tool）
        clip_results = []
        for task in completed_tasks:
            clip_results.append({
                "task_idx": task.get("clip_idx") or task.get("task_idx"),
                "status": "succeeded",
                "video_url": task.get("video_url"),
                "task_id": task.get("id")
            })
        
        # 按 task_idx 排序
        clip_results.sort(key=lambda x: x.get("task_idx", 0) or 0)
        
        # 提取所有视频片段URL
        segments = [r.get("video_url") for r in clip_results if r.get("video_url")]
        
        if not segments:
            return {"error": "没有可用的视频片段URL"}
        
        # 再次检查状态（防止在查询任务期间状态发生变化）
        try:
            session_check = supabase.table("crew_sessions")\
                .select("status, result")\
                .eq("run_id", body.run_id)\
                .execute()
            
            if session_check.data and len(session_check.data) > 0:
                session = session_check.data[0]
                current_status = session.get("status", "")
                result = session.get("result", "")
                
                # 如果已经完成且有有效结果，直接返回
                if current_status == "completed" and result and "http" in result.lower() and "example.com" not in result.lower():
                    logger.info(
                        f"[crewai_video_clips_confirm] Session completed during task query for run_id={body.run_id}, "
                        f"returning existing result: {result[:100]}"
                    )
                    return {
                        "run_id": body.run_id,
                        "final_url": result,
                        "status": "completed",
                        "from_cache": True
                    }
                
                # 如果正在拼接，返回提示信息
                if current_status == "stitching":
                    logger.info(
                        f"[crewai_video_clips_confirm] Session started stitching during task query for run_id={body.run_id}, "
                        f"please wait for completion"
                    )
                    return {
                        "run_id": body.run_id,
                        "status": "stitching",
                        "message": "视频正在拼接中，请稍候..."
                    }
        except Exception as e:
            logger.debug(f"[crewai_video_clips_confirm] Failed to re-check crew_sessions: {e}")
            # 继续执行，不阻塞
        
        logger.info(
            f"[crewai_video_clips_confirm] Starting stitch for run_id={body.run_id}: "
            f"{len(segments)} segments"
        )
        
        # 更新状态为 stitching（防止重复拼接）
        try:
            from datetime import datetime
            supabase.table("crew_sessions")\
                .update({
                    "status": "stitching",
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", body.run_id)\
                .execute()
        except Exception as e:
            logger.warning(f"[crewai_video_clips_confirm] Failed to update crew_sessions status to stitching: {e}")
        
        # 调用拼接函数
        from video_stitcher import stitch_video_segments
        
        final_key = f"{body.run_id}_final.mp4"
        cdn_url = await stitch_video_segments(
            segment_urls=segments,
            run_id=body.run_id,
            output_key=final_key
        )
        
        # 更新 crew_sessions 表（如果存在）
        try:
            from datetime import datetime
            supabase.table("crew_sessions")\
                .update({
                    "status": "completed",
                    "result": cdn_url,
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", body.run_id)\
                .execute()
        except Exception as e:
            logger.warning(f"[crewai_video_clips_confirm] Failed to update crew_sessions: {e}")
        
        logger.info(
            f"[crewai_video_clips_confirm] Stitch completed for run_id={body.run_id}: {cdn_url}"
        )
        
        return {
            "run_id": body.run_id,
            "segments": segments,
            "final_url": cdn_url,
            "status": "completed"
        }
    except Exception as e:
        logger.error(f"[crewai_video_clips_confirm] Error: {e}", exc_info=True)
        return {"error": str(e)}


