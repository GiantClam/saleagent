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
        queue = get_supabase_queue()
        if queue:
            queue.stop()
            logger.info("[shutdown] Supabase queue worker stopped")
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
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
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


