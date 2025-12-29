import os
import json
import uuid
import asyncio
from datetime import datetime
import random
import re
from dotenv import load_dotenv
import os
import logging
import sys
from logging.handlers import RotatingFileHandler

# 首先加载 .env 文件，确保后续导入的模块能读取环境变量
# 优先查找当前目录 apps/agent/.env，如果不存在则查找项目根目录 .env
local_env = os.path.join(os.path.dirname(__file__), '.env')
root_env = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../.env'))

if os.path.exists(local_env):
    load_dotenv(dotenv_path=local_env)
    print(f"[main] Loaded .env from {local_env}")
elif os.path.exists(root_env):
    load_dotenv(dotenv_path=root_env)
    print(f"[main] Loaded .env from {root_env}")
else:
    load_dotenv() # Fallback to default search
    print("[main] Warning: No specific .env found, used default search")

# DEBUG: Verify Env loading
print(f"[main] DEBUG: PROVIDER_IMAGE = {os.getenv('PROVIDER_IMAGE')}")
print(f"[main] DEBUG: RUNNINGHUB_API_KEY IS SET = {bool(os.getenv('RUNNINGHUB_API_KEY'))}")
print(f"[main] DEBUG: RUNNINGHUB_IMAGE_WORKFLOW_ID = {os.getenv('RUNNINGHUB_IMAGE_WORKFLOW_ID')}")

# Add current directory to sys.path to support running as a script
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, UploadFile, File
from dotenv import load_dotenv
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from supabase import create_client
import httpx

# Use absolute imports since we added the directory to sys.path
from providers import get_image_provider, get_video_provider
from r2 import upload_url_to_r2, presign_put_url
from openrouter_client import OpenRouterClient

from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any, Tuple

# from crewai_workflow import build_crew
from langgraph_workflow import start_video_generation, get_workflow_app
from crewai_tools import plan_storyboard_impl, generate_video_clip_impl
from crewai_tools import plan_storyboard_impl, generate_video_clip_impl
from crewai_tools import synthesize_voice_impl, synthesize_bgm_impl
# Import JobManager
from job_manager import job_manager

import logging
logger = logging.getLogger("workflow")

# 定义视频生成后台任务
async def execute_video_generation_workflow(run_id: str, payload: dict):
    """
    后台执行视频生成工作流：
    1. 场景图片生成 (if image_control enabled)
    2. 生成视频片段并提交任务
    3. 轮询进度并实时广播
    4. 自动拼接最终视频
    """
    try:
        from crewai_tools import merge_storyboards_to_video_tasks_impl, generate_video_clip_impl
        from providers import get_image_provider
        
        thread_id = payload.get("thread_id", f"t_{run_id}")
        storyboard = payload.get("storyboard")
        
        if not storyboard or "scenes" not in storyboard:
            await job_manager.broadcast(run_id, "System", "error", "缺少分镜数据")
            return
            
        # 0. Register Session for internal tracking (frontend polling)
        try:
            from crewai_session_manager import get_session_manager
            session_manager = get_session_manager()
            if session_manager:
                session_id = f"session_{run_id}_{int(datetime.utcnow().timestamp() * 1000)}"
                total_duration = payload.get("total_duration", 10.0)
                # Calculate expected tasks based on storyboard scenes or duration
                expected_tasks = len(storyboard.get("scenes", []))
                if expected_tasks == 0:
                     import math
                     expected_tasks = max(1, math.ceil(total_duration / 10.0))
                     
                await session_manager.register_session(
                    run_id=run_id,
                    session_id=session_id,
                    expected_clips=expected_tasks,
                    context={
                        "goal": payload.get("goal", ""),
                        "styles": payload.get("styles", []),
                        "total_duration": total_duration,
                        "expected_tasks": expected_tasks,
                        "image_control": payload.get("image_control", False),
                        "status": "running"
                    }
                )
                logger.info(f"[execute_video_generation_workflow] Registered session {session_id} for run {run_id}")
        except Exception as e:
            logger.warning(f"[execute_video_generation_workflow] Failed to register session: {e}")
        
        # 1. 场景图片生成 (if image_control enabled)
        image_control = payload.get("image_control", False)
        if image_control:
            try:
                await job_manager.broadcast(run_id, "视觉设计", "thought", "正在生成场景预览图...")
                ip = get_image_provider()
                for scene in storyboard["scenes"]:
                    scene_desc = scene.get("narration") or scene.get("desc", "")
                    if scene_desc and not scene.get("keyframes", {}).get("in"):
                        img_url = await ip.generate_scene(scene_desc)
                        if not scene.get("keyframes"):
                            scene["keyframes"] = {}
                        scene["keyframes"]["in"] = img_url
                await job_manager.broadcast(run_id, "视觉设计", "tool_result", "场景图片生成完成")
            except Exception as e:
                logger.warning(f"Scene image generation failed: {e}")
        
        # 2. 转换为视频任务
        await job_manager.broadcast(run_id, "审核", "thought", "正在规划视频任务...")
        storyboard_json = json.dumps(storyboard)
        total_duration = payload.get("total_duration", 10.0)
        video_tasks_json = merge_storyboards_to_video_tasks_impl(storyboard_json, run_id, total_duration)
        video_tasks = json.loads(video_tasks_json) if isinstance(video_tasks_json, str) else video_tasks_json
        
        await job_manager.broadcast(run_id, "审核", "tool_result", f"已创建 {len(video_tasks)} 个视频任务")
        
        # 3. 提交视频生成任务
        await job_manager.broadcast(run_id, "制片", "thought", "正在提交视频生成任务...")
        clip_results_json = await generate_video_clip_impl(video_tasks_json, run_id)
        clip_results = json.loads(clip_results_json) if isinstance(clip_results_json, str) else clip_results_json
        
        submitted_count = sum(1 for r in clip_results if r.get("status") in ["submitted", "pending"])
        await job_manager.broadcast(run_id, "制片", "tool_result", f"已提交 {submitted_count} 个视频生成任务，正在处理中...")
        
        # 4. 轮询视频任务状态
        import time
        from supabase import create_client
        
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        supabase_client = None
        if supabase_url and supabase_key:
            supabase_client = create_client(supabase_url, supabase_key)
        
        if not supabase_client:
            await job_manager.broadcast(run_id, "System", "error", "Supabase未配置，无法轮询任务状态")
            return
        
        completed_clips = {}
        max_wait_time = 1800  # 30分钟超时
        start_time = time.time()
        check_interval = 3
        last_heartbeat = time.time()
        heartbeat_interval = 30
        
        while time.time() - start_time < max_wait_time:
            await asyncio.sleep(check_interval)
            
            # 发送心跳
            current_time = time.time()
            if current_time - last_heartbeat >= heartbeat_interval:
                elapsed_minutes = int((current_time - start_time) / 60)
                completed_count = len(completed_clips)
                total_count = len(video_tasks)
                await job_manager.broadcast(
                    run_id, "制片", "heartbeat", 
                    f"视频生成中... 已完成 {completed_count}/{total_count} 个片段（已等待 {elapsed_minutes} 分钟）"
                )
                last_heartbeat = current_time
            
            # 查询任务状态
            try:
                res = supabase_client.table("video_tasks").select("clip_idx, status, video_url, error").eq("run_id", run_id).execute()
                tasks = res.data or []
                all_completed = True
                
                for task in tasks:
                    status = task.get("status")
                    if status == "failed":
                        error_msg = task.get("error") or "Unknown error"
                        await job_manager.broadcast(
                            run_id, "制片", "error",
                            f"片段 {task.get('clip_idx')+1} 生成失败: {error_msg}",
                            {"clip": task, "run_id": run_id}
                        )
                        # Optionally stop everything if one fails? 
                        # For now, we report it. The user will see the error.
                        # We might want to break if we can't recover.
                        # But maybe other clips are still running.
                        # We let the loop continue to report other statuses, but the final stitching will fail or be partial.
                        
                    if status != "succeeded":
                        all_completed = False
                    else:
                        c_idx = task.get("clip_idx")
                        if c_idx not in completed_clips:
                            completed_clips[c_idx] = True
                            # 广播单个片段完成
                            await job_manager.broadcast(
                                run_id, "制片", "video_clip_completed",
                                f"片段 {c_idx+1} 生成完成",
                                {"clip": task, "run_id": run_id}
                            )
                
                if all_completed and len(tasks) > 0:
                    break
                
                # 广播进度
                completed_count = sum(1 for task in tasks if task.get("status") == "succeeded")
                total_count = len(tasks)
                if total_count > 0:
                    await job_manager.broadcast(
                        run_id, "制片", "progress",
                        f"生成进度：{completed_count}/{total_count}",
                        {"current": completed_count, "total": total_count}
                    )
            except Exception as e:
                logger.warning(f"Error checking video tasks: {e}")
            
            if len(completed_clips) >= len(video_tasks):
                break
        
        # 5. 等待用户确认拼接 (Manual Review Flow)
        if len(completed_clips) >= len(video_tasks):
            # Check if already completed or stitching (handled by polling)
            # But here we just want to signal ready_to_stitch
            
            # Update status to ready_to_stitch
            if supabase_client:
                supabase_client.table("crew_sessions").update({
                    "status": "ready_to_stitch",
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("run_id", run_id).execute()
            
            await job_manager.broadcast(run_id, "System", "thought", "所有片段已生成，等待用户确认合并...")
            # We exit the loop here. The actual stitching will be triggered by /crewai/video/stitch endpoint
            return
        else:
            await job_manager.broadcast(run_id, "System", "info", "视频生成超时，部分片段未完成")

    except Exception as e:
        logger.error(f"Workflow failed: {e}", exc_info=True)
        # Update status to failed
        try:
             # Re-init client if needed (it might be None if failed early)
             if not locals().get("supabase_client"):
                from supabase import create_client
                s_url = os.getenv("SUPABASE_URL")
                s_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
                if s_url and s_key:
                    supabase_client = create_client(s_url, s_key)
             
             if supabase_client:
                supabase_client.table("crew_sessions").update({
                    "status": "failed",
                    "error": str(e),
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("run_id", run_id).execute()
        except:
             pass
             
        await job_manager.broadcast(run_id, "System", "error", f"Error: {str(e)}")

# 事件编码（简化版，与 AG-UI 兼容的数据结构）

# 事件编码（简化版，与 AG-UI 兼容的数据结构）
def encode_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

load_dotenv()
app = FastAPI()

# 应用启动时启动 Supabase 队列 worker
@app.on_event("startup")
async def startup_event():
    """应用启动时初始化 Supabase 队列 worker"""
    try:
        try:
            from video_task_queue_supabase import start_supabase_queue_worker
        except ImportError:
            from video_task_queue_supabase import start_supabase_queue_worker
        start_supabase_queue_worker()
    except Exception as e:
        logger.warning(f"[startup] Failed to start Supabase queue worker: {e}")

@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时停止 Supabase 队列 worker"""
    try:
        try:
            from video_task_queue_supabase import get_supabase_queue
        except ImportError:
            # Fallback if needed, though with sys.path hack it should work
            from video_task_queue_supabase import get_supabase_queue
        
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
    run_id: Optional[str] = None
    ref_image_url: Optional[str] = None

    @validator("total_duration")
    def norm_duration(cls, v: float) -> float:
        return round(v, 1)


class ClipSpec(BaseModel):
    idx: int
    desc: str
    begin_s: float
    end_s: float
    keyframes: Dict[str, Optional[str]] = Field(default_factory=dict)
    error: Optional[str] = None  # {"in":url?, "out":url?}



class PlanResponse(BaseModel):
    storyboards: List[ClipSpec]


@app.post("/workflow/plan", response_model=PlanResponse)
async def workflow_plan(body: PlanRequest):
    logger.info(f"Planning storyboard for goal={body.goal} run_id={body.run_id}")
    
    # Replace CrewAI/Procedural logic with LangGraph
    state = await start_video_generation(body.dict())
    data = state.get("storyboard", {"scenes": []})
    
    scenes_data = []
    if isinstance(data, dict):
        scenes_data = data.get("scenes") or data.get("storyboards") or data.get("clips") or []
    elif isinstance(data, list):
        scenes_data = data
        
    clips = []
    # Support both flat list of clips/scenes AND nested scenes->clips structure
    # Support both flat list of clips/scenes AND nested scenes->clips structure
    for i, s in enumerate(scenes_data):
        # 1. Try to get nested clips
        sub_clips_data = s.get("clips")
        if sub_clips_data and isinstance(sub_clips_data, list):
            # Nested structure (Scenes containing clips)
            # We want to treat the SCENE as the unit for video generation (10s video)
            # So we create ONE clip spec for the whole SCENE, merging descriptions
            
            # Merge descriptions from sub-clips
            merged_desc_parts = []
            for sc in sub_clips_data:
                d = sc.get("desc") or sc.get("description", "")
                if d:
                    merged_desc_parts.append(d)
            
            scene_desc = "；".join(merged_desc_parts) or s.get("narration", "")
            
            # Use scene index
            scene_idx = s.get("scene_idx") or s.get("idx") or (i + 1)
            
            c = ClipSpec(
                idx=scene_idx,
                desc=scene_desc,
                begin_s=s.get("begin_s", 0),
                end_s=s.get("end_s", 10.0), # Default to 10s for scene
                keyframes=s.get("keyframes", {})
            )
            clips.append(c)
        else:
            # Flat structure: Scene is a Clip
            c = ClipSpec(
                idx=s.get("idx") or s.get("scene_idx", i+1),
                desc=s.get("desc") or s.get("narration", ""),
                begin_s=s.get("begin_s", 0),
                end_s=s.get("end_s", 0),
                keyframes=s.get("keyframes", {})
            )
            clips.append(c)
        
    # 2. (NEW) Generate Images if enabled
    if body.image_control and body.run_id:
        logger.info(f"Generating scene images for {len(clips)} clips (Sequential). Provider: {type(image_provider).__name__}")
        
        success_count = 0
        failure_count = 0
        
        for clip in clips:
            url = None
            last_error = None
            for attempt in range(3):
                try:
                    # Check if provider has generate_scene, else use generate
                    if hasattr(image_provider, 'generate_scene'):
                        res = await image_provider.generate_scene(
                            image_url=body.ref_image_url or "",
                            text=f"{clip.desc}, style: {','.join(body.styles)}"
                        )
                        # Handle dict return
                        if isinstance(res, dict):
                            url = res.get("image_url")
                        else:
                            url = str(res)
                    else:
                        url = await image_provider.generate(f"{clip.desc}, {','.join(body.styles)}")
                    
                    if url:
                        clip.keyframes["in"] = url
                        success_count += 1
                        break
                except Exception as e:
                    last_error = e
                    logger.warning(f"Scene {clip.idx} generation attempt {attempt + 1}/3 failed: {e}")
                    if attempt < 2:
                        await asyncio.sleep(1)
            
            if not url:
                failure_count += 1
                error_msg = f"Failed to generate scene {clip.idx} image after 3 attempts. Last error: {last_error}"
                logger.error(error_msg)
                clip.error = str(last_error) or "Image generation failed"
        
        if failure_count == 0:
            logger.info(f"All {success_count} scene images generated successfully.")
        else:
            logger.warning(f"Scene images generated with {failure_count} failures. Success: {success_count}.")
        
    return PlanResponse(storyboards=clips)



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
    use_voice_agent: bool = False
    use_bgm_agent: bool = False
    mute_model_audio: bool = False






@app.post("/workflow/confirm")
async def workflow_confirm(request: Request):
    """
    Phase 3: Trigger Generation
    Merge request payload with saved job details (like image_control)
    """
    try:
        body = await request.json()
        run_id = body.get("run_id")
        
        # Merge payload with saved job details (image_control etc)
        payload = body.copy()
        
        if supabase and run_id:
             try:
                 j = supabase.table("jobs").select("image_control, styles").eq("run_id", run_id).single().execute()
                 if j and j.data:
                     # Only set if not already in payload (though payload usually doesn't have it)
                     if "image_control" not in payload:
                         payload["image_control"] = j.data.get("image_control", False)
                     if "styles" not in payload and j.data.get("styles"):
                         payload["styles"] = j.data.get("styles")
             except Exception as e:
                 logger.warning(f"Failed to fetch job details for confirm: {e}")
        
        # Start background job
        await job_manager.start_job(run_id, start_video_generation(payload))
        
        return {"status": "started", "run_id": run_id}
    except Exception as e:
        logger.error(f"Error in workflow_confirm: {e}")
        return {"error": str(e)}

@app.post("/workflow/update")
async def workflow_update(request: Request):
    """
    Update the storyboard from the editor and trigger regeneration.
    """
    from langgraph_workflow import update_video_generation
    try:
        body = await request.json()
        run_id = body.get("run_id")
        thread_id = body.get("thread_id") or f"thread_{run_id}"
        updates = body.get("updates", {})
        
        if not run_id:
            return {"error": "Missing run_id"}
            
        # Update state and continue
        await update_video_generation(run_id, thread_id, updates)
        
        return {"status": "updated", "run_id": run_id}
    except Exception as e:
        logger.error(f"Error in workflow_update: {e}")
        return {"error": str(e)}


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
    
    require_confirm = os.getenv("REQUIRE_HUMAN_CONFIRM", "true").lower() == "true"
    if require_confirm:
        try:
            if supabase:
                j = supabase.table("jobs").select("status").eq("run_id", run_id).single().execute()
                status = (j.data or {}).get("status") if j and j.data else None
                if status != "planning_confirmed":
                    return {"error": "需要确认分镜方案后才能生成镜头", "code": "confirmation_required"}
            else:
                try:
                    conf = getattr(confirm_storyboard, "_confirmations", {})
                except Exception:
                    conf = {}
                s = conf.get(f"{run_id}_storyboard", {}).get("status")
                if s != "confirmed":
                    return {"error": "需要确认分镜方案后才能生成镜头", "code": "confirmation_required"}
        except Exception:
            pass
    
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


class RHScenesRequest(BaseModel):
    image_url: str
    styles: List[str] = Field(default_factory=list)
    total_duration: float = 10.0
    storyboards: List[ClipSpec]


class RHScenesResponse(BaseModel):
    storyboard: Dict[str, Any]
    clips: List[ClipSpec]


@app.post("/workflow/rh-scenes")
async def workflow_rh_scenes(body: RHScenesRequest):
    try:
        if not body.image_url:
            return {"error": "严格模式：缺少参考图 image_url"}
        try:
            from providers import get_image_provider
        except ImportError:
             from providers import get_image_provider

        try:
            from crewai_tools import refine_storyboard_from_scene_descriptions
        except ImportError:
            from crewai_tools import refine_storyboard_from_scene_descriptions
        ip = get_image_provider()
        scene_texts: List[str] = []
        scene_images: Dict[int, str] = {}
        by_scene: Dict[int, List[ClipSpec]] = {}
        for s in body.storyboards:
            scene_idx = int(max(1, int(s.begin_s // 10)))
            by_scene.setdefault(scene_idx, []).append(s)
        for scene_idx in sorted(by_scene.keys()):
            parts = [c.desc for c in by_scene[scene_idx] if c.desc]
            text_for_scene = "；".join(parts) or f"场景{scene_idx}"
            if hasattr(ip, "generate_scene"):
                try:
                    fixed_hint = "根据参考图生成产品的广告片分镜 6格分镜图 影视大片感。要求：图片中的商品宽高比例、瓶子形状、外观及细节请务必保持不变。画面没有字幕、没有中文，如果画面中出现中文字那么中文字的书写要准确无误，人物特征要保持一致性，人物清晰且没有崩坏"
                    text_final = f"{fixed_hint}。{text_for_scene}。"
                    logger.info(f"[workflow_rh_scenes] RH generate_scene params: image_url={body.image_url}, text={text_final}")
                    res = await ip.generate_scene(body.image_url, text_final)
                    if isinstance(res, dict):
                        # if res.get("desc_text"):
                        #     scene_texts.append(res["desc_text"])
                        # else:
                        
                        # 兜底：使用原始 scene 文本，保证场景数一致
                        scene_texts.append(text_for_scene)

                        if res.get("image_url"):
                            scene_images[scene_idx] = res["image_url"]
                except Exception:
                    return {"error": "严格模式：场景工作流失败，请检查工作流与入参"}
            else:
                return {"error": "严格模式：Provider 不支持 generate_scene"}
        sb_json = await refine_storyboard_from_scene_descriptions(scene_texts, body.styles, body.total_duration)
        try:
            sb_obj = json.loads(sb_json)
        except Exception:
            jm = re.search(r"\{.*?\"scenes\".*?\}", sb_json, re.DOTALL)
            sb_obj = json.loads(jm.group(0)) if jm else {"scenes": []}
        # 注入每个 scene 的 image_url，便于后续作为关键帧使用
        try:
            for sc in sb_obj.get("scenes", []):
                idx = int(sc.get("scene_idx") or 0)
                img = scene_images.get(idx)
                if img:
                    sc["image_url"] = img
        except Exception:
            pass
        clips: List[ClipSpec] = []
        cur = 0.0
        for scene in sb_obj.get("scenes", []):
            for clip in scene.get("clips", []):
                desc = str(clip.get("desc", "")).strip()
                dur = float(clip.get("duration", 0) or 0)
                dur = dur if dur > 0 else max(1.0, body.total_duration / max(1, len(body.storyboards)))
                begin_s = cur
                end_s = min(body.total_duration, cur + dur)
                img_in = scene.get("image_url")
                keyframes = {"in": img_in} if img_in else {}
                clips.append(ClipSpec(idx=len(clips), desc=desc, begin_s=begin_s, end_s=end_s, keyframes=keyframes))
                cur = end_s
        return RHScenesResponse(storyboard=sb_obj, clips=clips)
    except Exception as e:
        logger.error(f"[workflow_rh_scenes] Error: {e}", exc_info=True)
        return {"error": str(e)}


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
        # Update status to stitching
        if supabase:
             supabase.table("crew_sessions").update({
                 "status": "stitching",
                 "updated_at": datetime.utcnow().isoformat()
             }).eq("run_id", body.run_id).execute()

        try:
            from .video_stitcher import stitch_video_segments
        except ImportError:
            from video_stitcher import stitch_video_segments
        
        final_key = body.output_key or f"{body.run_id}_final.mp4"
        cdn_url = await stitch_video_segments(
            segment_urls=body.segments,
            run_id=body.run_id,
            output_key=final_key
        )
        
        # Update status to completed
        if supabase:
             supabase.table("crew_sessions").update({
                 "status": "completed",
                 "result": cdn_url,
                 "updated_at": datetime.utcnow().isoformat()
             }).eq("run_id", body.run_id).execute()

        return {
            "run_id": body.run_id,
            "segments": body.segments,
            "final_url": cdn_url
        }
    except Exception as e:
        logger.error(f"[workflow_stitch] Error: {e}", exc_info=True)
        if supabase:
             supabase.table("crew_sessions").update({
                 "status": "failed",
                 "error": str(e),
                 "updated_at": datetime.utcnow().isoformat()
             }).eq("run_id", body.run_id).execute()
        return {"error": str(e)}


# 辅助工具端点：直接触发旁白与 BGM 合成，便于验证完整体验
class SynthesizeVoiceRequest(BaseModel):
    run_id: str
    scene_idx: int
    narration: str
    voice_id: Optional[str] = None
    emotion: str = "calm"
    speed: float = 1.0
    vol: float = 1.0
    pitch: int = 0


@app.post("/tools/synthesize-voice")
async def tools_synthesize_voice(body: SynthesizeVoiceRequest):
    vid = body.voice_id or os.getenv("MINIMAX_VOICE_ID", "zh_female_01")
    try:
        res = await synthesize_voice_impl(body.scene_idx, body.narration, vid, body.emotion, body.speed, body.vol, body.pitch, body.run_id)
        try:
            payload = json.loads(res)
        except Exception:
            payload = {"result": res}
        return payload
    except Exception as e:
        return {"error": str(e)}


@app.get("/tools/env-check")
async def tools_env_check():
    return {
        "MINIMAX_MCP_BASE": bool(os.getenv("MINIMAX_MCP_BASE")),
        "MINIMAX_API_KEY": bool(os.getenv("MINIMAX_API_KEY")),
        "MINIMAX_VOICE_ID": os.getenv("MINIMAX_VOICE_ID") is not None,
        "R2_BUCKET": os.getenv("R2_BUCKET") or "",
        "R2_PUBLIC_BASE": bool(os.getenv("R2_PUBLIC_BASE")),
        "R2_ACCOUNT_ID": bool(os.getenv("R2_ACCOUNT_ID")),
    }


class SynthesizeBgmRequest(BaseModel):
    run_id: str
    prompt: str


@app.post("/tools/synthesize-bgm")
async def tools_synthesize_bgm(body: SynthesizeBgmRequest):
    try:
        res = await synthesize_bgm_impl(body.prompt, body.run_id)
        try:
            payload = json.loads(res)
        except Exception:
            payload = {"result": res}
        return payload
    except Exception as e:
        return {"error": str(e)}


class UploadUrlRequest(BaseModel):
    run_id: str
    url: str
    key: Optional[str] = None


@app.post("/tools/upload-url")
async def tools_upload_url(body: UploadUrlRequest):
    try:
        try:
            from r2 import upload_url_to_r2
        except ImportError:
            from r2 import upload_url_to_r2
        from urllib.parse import urlparse
        k = body.key
        if not k:
            p = urlparse(body.url)
            base = os.path.basename(p.path) or "clip.mp4"
            k = f"{body.run_id}_{base}"
        cdn = await upload_url_to_r2(body.url, k)
        return {"cdn_url": cdn, "key": k}
    except Exception as e:
        return {"error": str(e)}


class PresignUploadRequest(BaseModel):
    key: Optional[str] = None
    content_type: Optional[str] = "application/octet-stream"
    bucket: Optional[str] = None
    expires: Optional[int] = 3600


@app.post("/tools/r2/presign-upload")
async def tools_r2_presign_upload(body: PresignUploadRequest):
    try:
        try:
            from r2 import presign_put_url
        except ImportError:
            from r2 import presign_put_url
        key = body.key or f"upload_{uuid.uuid4().hex}"
        data = presign_put_url(key, bucket=body.bucket, content_type=body.content_type or "application/octet-stream", expires=int(body.expires or 3600))
        return data
    except Exception as e:
        return {"error": str(e)}


class CrewRunRequest(BaseModel):
    goal: str
    styles: List[str] = []
    total_duration: float = 6.0
    num_clips: int = 1
    image_control: bool = False
    run_id: Optional[str] = None


@app.post("/workflow/plan")
async def workflow_plan(request: Request):
    """
    Phase 2: Plan Generation (Storyboard)
    Synchronous (or fast async) generation of text storyboard.
    """
    try:
        body = await request.json()
        run_id = body.get("run_id")
        
        # Load job state from DB or body
        # For MVP, assume client passes necessary context or we load from DB
        # To be safe, we can reuse 'collected_info' if frontend passes it back, or fetch from DB.
        collected = body.get("collected_info")
        if not collected and run_id and supabase:
             res = supabase.table("jobs").select("slogan, styles, total_duration").eq("run_id", run_id).single().execute()
             if res.data:
                 collected = {
                     "theme": res.data.get("slogan"),
                     "styles": res.data.get("styles"),
                     "duration": res.data.get("total_duration")
                 }
        
        if not collected:
            return {"error": "Missing collected info"}

        # Calculate goals
        goal = collected.get("theme", "")
        styles = collected.get("styles", [])
        total_duration = float(collected.get("duration", 10.0))
        num_clips = max(1, int(total_duration / 10.0) + (1 if total_duration % 10 > 0 else 0))

        # Call LLM
        storyboard_json = await plan_storyboard_impl(goal, styles, total_duration, num_clips)
        
        # Parse
        try:
            storyboard_data = json.loads(storyboard_json)
        except:
             # Fallback parsing
             match = re.search(r'\{.*?"scenes".*?\}', storyboard_json, re.DOTALL)
             storyboard_data = json.loads(match.group(0)) if match else {"scenes": []}

        # Update DB
        if run_id and supabase:
             supabase.table("jobs").update({
                 "storyboards": storyboard_data,
                 "status": "planning_complete"
             }).eq("run_id", run_id).execute()

        return {"run_id": run_id, "storyboard": storyboard_data}
    except Exception as e:
        logger.error(f"Plan failed: {e}")
        return {"error": str(e)}

@app.post("/workflow/confirm")
async def workflow_confirm(request: Request):
    """
    Phase 3: Trigger Generation
    """
    try:
        body = await request.json()
        run_id = body.get("run_id")
        storyboard = body.get("storyboard")
        payload = body # Pass full body as payload
        
        # Start background job
        await job_manager.start_job(run_id, execute_video_generation_workflow(run_id, payload))
        
        return {"status": "started", "run_id": run_id}
    except Exception as e:
        logger.error(f"Confirm failed: {e}")
        return {"error": str(e)}

@app.post("/workflow/run-clips")
async def workflow_run_clips_sse(request: Request):
    """
    Phase 3 Monitor: streaming progress (POST for compatibility with proxy)
    """
    try:
        body = await request.json()
        run_id = body.get("run_id")
        if not run_id:
             return {"error": "Missing run_id"}
        return StreamingResponse(job_manager.subscribe(run_id), media_type="text/event-stream")
    except Exception as e:
        logger.error(f"Run-clips failed: {e}")
        return {"error": str(e)}

# Legacy run endpoint (keep for compatibility if needed, but we are moving away)
@app.post("/workflow/crew-run")
async def run_workflow(request: Request):
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
    
    require_confirm = os.getenv("REQUIRE_HUMAN_CONFIRM", "true").lower() == "true"
    if require_confirm:
        try:
            if supabase:
                j = supabase.table("jobs").select("status").eq("run_id", run_id).single().execute()
                status = (j.data or {}).get("status") if j and j.data else None
                if status != "planning_confirmed":
                    return {"error": "需要人工确认后再启动工作流", "code": "confirmation_required", "run_id": run_id}
            else:
                try:
                    conf = getattr(confirm_storyboard, "_confirmations", {})
                except Exception:
                    conf = {}
                s = conf.get(f"{run_id}_storyboard", {}).get("status")
                if s != "confirmed":
                    return {"error": "需要人工确认后再启动工作流", "code": "confirmation_required", "run_id": run_id}
        except Exception:
            pass
    
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
                current_session_res = session_manager.supabase.table("crew_sessions")\
                    .select("status, result")\
                    .eq("run_id", run_id)\
                    .order("created_at", desc=True)\
                    .limit(1)\
                    .execute()
                
                current_status = None
                current_result = None
                if current_session_res and current_session_res.data:
                    current_session_data = current_session_res.data[0]
                    current_status = current_session_data.get("status", "")
                    current_result = current_session_data.get("result", "")
                
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


@app.get("/workflow/list")
async def get_workflow_list(limit: int = 50):
    """
    获取历史工作流列表
    """
    try:
        from crewai_session_manager import get_session_manager
        session_manager = get_session_manager()
        
        if not session_manager:
            return {"error": "Session manager not available"}
            
        # Select necessary fields, order by created_at desc
        result = session_manager.supabase.table("crew_sessions")\
            .select("run_id, status, context, created_at, expected_clips, result")\
            .order("created_at", desc=True)\
            .limit(limit)\
            .execute()
        
        if not result or not result.data:
            return {"workflows": []}
            
        workflows = []
        for session in result.data:
            ctx = session.get("context") or {}
            # Try to determine a title/goal
            goal = ctx.get("goal") or ctx.get("theme") or session.get("run_id")
            
            workflows.append({
                "run_id": session.get("run_id"),
                "status": session.get("status"),
                "created_at": session.get("created_at"),
                "goal": goal,
                "result": session.get("result")
            })
            
        return {"workflows": workflows}
    except Exception as e:
        logger.error(f"[get_workflow_list] Error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/workflow/crew-status/{run_id}")
async def get_crew_status(run_id: str):
    """
    查询 CrewAI 工作流执行状态
    
    返回：
    - status: running, waiting_videos, ready_to_stitch, stitching, completed, failed
    - result: 最终结果（如果完成）
    - error: 错误信息（如果失败）
    - video_tasks: 视频片段列表 (新增)
    """
    try:
        from crewai_session_manager import get_session_manager
        session_manager = get_session_manager()
        
        if not session_manager:
            return {"error": "Session manager not available"}
            
        # Use limit(1) instead of maybe_single to avoid 406 error if duplicates exist
        result = session_manager.supabase.table("crew_sessions")\
            .select("*")\
            .eq("run_id", run_id)\
            .order("created_at", desc=True)\
            .limit(1)\
            .execute()
        
        if not result or not result.data:
            return {"status": "unknown", "error": "not_found", "message": f"Session not found for run_id: {run_id}"}
        
        session = result.data[0]
        
        # [NEW] Fetch video tasks for this run
        video_tasks = []
        try:
             vt_result = session_manager.supabase.table("video_tasks")\
                .select("*")\
                .eq("run_id", run_id)\
                .order("clip_idx", desc=False)\
                .execute()
             if vt_result and vt_result.data:
                 video_tasks = vt_result.data
        except Exception as e:
            logger.warning(f"[get_crew_status] Failed to fetch video_tasks: {e}")

        return {
            "run_id": run_id,
            "status": session.get("status", "unknown"),
            "result": session.get("result"),
            "error": session.get("error"),
            "expected_clips": session.get("expected_clips"),
            "context": session.get("context", {}),
            "video_tasks": video_tasks, # New field
            "created_at": session.get("created_at"),
            "updated_at": session.get("updated_at")
        }
    except Exception as e:
        logger.error(f"[get_crew_status] Error: {e}", exc_info=True)
        return {"error": str(e), "status": "failed"}


# 上传图片文件到 RunningHub（返回 fileName，可直接用于 nodeInfoList）
@app.post("/workflow/upload-image")
async def workflow_upload_image(file: UploadFile = File(...)):
    try:
        from .runninghub_client import RunningHubClient
    except ImportError:
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

        # Vercel 模式：在确认后后台继续执行后续流程（审核/合并/提交任务），避免长时间 SSE
        try:
            vercel_mode = os.getenv("VERCEL_MODE", "false").lower() == "true"
            if vercel_mode and confirmed:
                # 读取在对话阶段缓存的 storyboard
                confirmation_key = f"{run_id}_storyboard"
                storyboard_data = None
                try:
                    if hasattr(confirm_storyboard, "_confirmations"):
                        pass
                    # 优先从 crewai_chat 的缓存中获取完整 storyboard
                    if hasattr(crewai_chat, "_storyboard_confirmations"):
                        cached = crewai_chat._storyboard_confirmations.get(confirmation_key)
                        storyboard_data = (cached or {}).get("storyboard")
                except Exception:
                    storyboard_data = None

                if storyboard_data and isinstance(storyboard_data, dict):
                    # 后台异步继续执行：审核、合并、提交视频任务
                    async def _background_continue():
                        try:
                            styles = storyboard_data.get("styles", []) or []
                            # 通过 scene 数量推断总时长（每个 scene 10s），如失败则回退到 10s
                            scenes = storyboard_data.get("scenes", []) or []
                            total_duration = float(os.getenv("DEFAULT_TOTAL_DURATION", str(len(scenes) * 10 or 10)))
                            try:
                                from .crewai_tools import review_storyboard_impl, merge_storyboards_to_video_tasks_impl, generate_video_clip_impl
                            except ImportError:
                                from crewai_tools import review_storyboard_impl, merge_storyboards_to_video_tasks_impl, generate_video_clip_impl
                            reviewed_storyboard_json = review_storyboard_impl(
                                json.dumps(storyboard_data, ensure_ascii=False),
                                num_clips=1,
                                goal="",
                                styles=styles,
                                total_duration=total_duration,
                            )
                            video_tasks_json = merge_storyboards_to_video_tasks_impl(
                                reviewed_storyboard_json,
                                run_id,
                                total_duration,
                            )
                            # 提交视频生成任务到队列
                            start_clip_submission = datetime.utcnow()
                            await generate_video_clip_impl(video_tasks_json, run_id)
                            
                            # CRITICAL FIX: Update crew_sessions expected_clips with ACTUAL number of tasks
                            # Ensure stiching trigger is reachable if clip count changed
                            try:
                                actual_tasks = json.loads(video_tasks_json) if isinstance(video_tasks_json, str) else video_tasks_json
                                if isinstance(actual_tasks, list) and supabase:
                                    supabase.table("crew_sessions").update({
                                        "expected_clips": len(actual_tasks),
                                        "updated_at": datetime.utcnow().isoformat()
                                    }).eq("run_id", run_id).execute()
                                    logger.info(f"[confirm_storyboard] Updated expected_clips to {len(actual_tasks)} for run {run_id}")
                            except Exception as e:
                                logger.warning(f"[confirm_storyboard] Failed to update expected_clips: {e}")

                            # 标记 jobs 为 processing（可选）
                            try:
                                if supabase:
                                    supabase.table("jobs").upsert({
                                        "run_id": run_id,
                                        "status": "processing",
                                        "updated_at": datetime.utcnow().isoformat(),
                                    }, on_conflict="run_id").execute()
                            except Exception:
                                pass
                        except Exception as e:
                            logger.warning(f"[confirm_storyboard] Background continue failed: {e}")
                    asyncio.create_task(_background_continue())
        except Exception:
            # 后台失败不影响确认响应
            pass

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
            
        regenerate_type = body.get("type", "full")  # full or image

        # 如果只是重新生成图片，跳过 CrewAI 流程
        if regenerate_type == "image":
             # 1. 尝试从上下文中获取 script
             if not script:
                 return {"error": "重新生成图片需要提供 script 参数"}
             
             # 2. 从 script 提取 clip 描述
             clip_descs = script.split("；") if script else []
             clips = []
             for idx, desc in enumerate(clip_descs, 1):
                clips.append({
                    "idx": idx,
                    "desc": desc.strip(),
                    "begin_s": 0.0,
                    "end_s": 10.0,
                })
             
             # 3. 生成图片
             scene_desc = "；".join([clip.get("desc", "") for clip in clips if clip.get("desc")])
             image_provider = get_image_provider()
             # 【重要】避免生成带有人脸的图片
             image_prompt = f"{scene_desc}，视频场景画面，无人脸、无真人，无人物形象"
             image_url = await image_provider.generate(image_prompt)
             
             return {
                "message_id": message_id,
                "scene_idx": scene_idx,
                "clips": clips,
                "image_url": image_url,
             }

        # 使用 director_agent 重新生成 scene 脚本
        from crewai_agents import build_agents
        from crewai import Task, Crew, Process
        
        agents = build_agents()
        # 兼容不同数量的 agents 返回
        director_agent = next((a for a in agents if a.role == "导演"), None)
        if not director_agent:
             # Fallback logic if role name changes or agent missing
             director_agent = agents[1] if len(agents) > 1 else agents[0]
        
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
                
                require_confirm = os.getenv("REQUIRE_HUMAN_CONFIRM", "true").lower() == "true"
                if require_confirm:
                    try:
                        if supabase:
                            j = supabase.table("jobs").select("status").eq("run_id", run_id).single().execute()
                            status = (j.data or {}).get("status") if j and j.data else None
                            if status != "planning_confirmed":
                                async for chunk in emit("System", "info", run_id, thread_id, delta="📝 请先确认分镜方案（storyboard）后再继续执行"):
                                    yield chunk
                                async for chunk in emit("System", "run_finished", run_id, thread_id, delta="⏳ 等待人工确认…", payload={"code": "confirmation_required", "run_id": run_id}):
                                    yield chunk
                                return
                        else:
                            try:
                                conf = getattr(confirm_storyboard, "_confirmations", {})
                            except Exception:
                                conf = {}
                            s = conf.get(f"{run_id}_storyboard", {}).get("status")
                            if s != "confirmed":
                                async for chunk in emit("System", "info", run_id, thread_id, delta="📝 请先确认分镜方案（storyboard）后再继续执行"):
                                    yield chunk
                                async for chunk in emit("System", "run_finished", run_id, thread_id, delta="⏳ 等待人工确认…", payload={"code": "confirmation_required", "run_id": run_id}):
                                    yield chunk
                                return
                    except Exception:
                        pass
                
                # 准备 CrewAI 工作流参数
                payload = {
                    "goal": goal,
                    "styles": styles if isinstance(styles, list) else [],
                    "total_duration": total_duration,
                    "num_clips": num_clips,
                    "image_control": image_control,
                    "run_id": run_id,
                    "enable_narration": bool(body.get("enable_narration")),
                    "enable_bgm": bool(body.get("enable_bgm")),
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
                try:
                    
                    require_confirm_mid = os.getenv("REQUIRE_HUMAN_CONFIRM", "true").lower() == "true"
                    if require_confirm_mid:
                        if supabase:
                            j = supabase.table("jobs").select("status").eq("run_id", run_id).single().execute()
                            status = (j.data or {}).get("status") if j and j.data else None
                            if status != "planning_confirmed":
                                async for chunk in emit("System", "info", run_id, thread_id, delta="📝 请确认分镜方案以继续执行"):
                                    yield chunk
                                async for chunk in emit("System", "run_finished", run_id, thread_id, delta="⏳ 等待人工确认…", payload={"code": "confirmation_required", "run_id": run_id}):
                                    yield chunk
                                return
                        else:
                            try:
                                conf = getattr(confirm_storyboard, "_confirmations", {})
                            except Exception:
                                conf = {}
                            s = conf.get(f"{run_id}_storyboard", {}).get("status")
                            if s != "confirmed":
                                async for chunk in emit("System", "info", run_id, thread_id, delta="📝 请确认分镜方案以继续执行"):
                                    yield chunk
                                async for chunk in emit("System", "run_finished", run_id, thread_id, delta="⏳ 等待人工确认…", payload={"code": "confirmation_required", "run_id": run_id}):
                                    yield chunk
                                return
                except Exception:
                    pass
                
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
                
                # 在线程池中执行同步的 CrewAI，并周期性发送心跳以保持连接
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    import time
                    fut = loop.run_in_executor(
                        executor,
                        lambda: crew.kickoff(inputs={"storyboards_json": ""})
                    )
                    last_hb = time.time()
                    hb_interval = 20
                    while True:
                        if fut.done():
                            result = await fut
                            break
                        now = time.time()
                        if now - last_hb >= hb_interval:
                            async for chunk in emit("System", "heartbeat", run_id, thread_id, 
                                                   delta="⏳ 工作流执行中..."):
                                yield chunk
                            last_hb = now
                        await asyncio.sleep(2)
                
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
                    try:
                        from .crewai_session_manager import get_session_manager
                    except ImportError:
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
    
    # 1. 获取 Job 信息
    job_res = supabase.table("jobs").select("*").eq("run_id", run_id).single().execute()
    if not job_res or not job_res.data:
        return {"error": "Job not found"}
    job_data = job_res.data

    # 2. 重置失败的任务状态为 pending
    try:
        # 只重置 failed 的任务
        supabase.table("video_tasks").update({
            "status": "pending",
            "provider_task_id": None, # 清除旧的 provider id 以便重新提交
            "error": None,
            "updated_at": datetime.utcnow().isoformat()
        }).eq("run_id", run_id).eq("status", "failed").execute()
        
        # 也可以选择重置卡住的 processing 任务? 暂时只重置 failed
    except Exception as e:
        logger.error(f"[retry_job] Failed to reset tasks: {e}")
        return {"error": f"Failed to reset tasks: {e}"}

    # 3. 构造 payload 并重启工作流
    # execute_video_generation_workflow 需要: storyboard, thread_id, image_control, total_duration, styles
    # 这些存储在 jobs 表中
    # 注意: jobs 表中的 storyboards 可能是 PlanResponse 格式 (List[ClipSpec]) 或 原始 storyboard json
    # execute_video_generation_workflow 在 line 100 json.dumps(storyboard)
    # 如果 job_data['storyboards'] 已经是 list，可以直接用
    
    payload = {
        "run_id": run_id,
        "thread_id": f"t_{run_id}", # 假设
        "storyboard": {"scenes": []}, # 构造兼容结构
        "image_control": job_data.get("image_control", False),
        "total_duration": job_data.get("total_duration", 10.0),
        "styles": job_data.get("styles", [])
    }
    
    # 尝试还原 storyboard 结构
    sb_data = job_data.get("storyboards")
    if sb_data:
        if isinstance(sb_data, list):
             # 转换为 execute_video_generation_workflow 期望的格式
             # 它期望 payload['storyboard']['scenes']...
             # 但 wait, execute_.. line 102 merge_storyboards_to_video_tasks_impl(storyboard_json)
             # 如果传入的是 list，json.dumps 会生成 list json
             # merge_storyboards... 能处理 list json 吗?
             # 查看 crewai_tools.py merge_storyboards_to_video_tasks_impl 实现?
             # 假设之前存入 jobs 的 storyboards 是可以直接用的
             payload["storyboard"] = sb_data
        elif isinstance(sb_data, dict):
             payload["storyboard"] = sb_data
    
    # 重新启动后台任务 (它会轮询并等待 pending 任务完成)
    await job_manager.start_job(run_id, execute_video_generation_workflow(run_id, payload))
    
    return {"status": "retrying", "run_id": run_id, "message": "已重置失败任务并重启工作流"}


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
        # 代理支持（与 OpenRouterClient 保持一致）
        proxy = os.getenv("OPENROUTER_PROXY")
        http_proxy = os.getenv("OPENROUTER_HTTP_PROXY") or os.getenv("HTTP_PROXY")
        https_proxy = os.getenv("OPENROUTER_HTTPS_PROXY") or os.getenv("HTTPS_PROXY")
        proxies = None
        if proxy:
            proxies = {"http://": proxy, "https://": proxy}
        elif http_proxy or https_proxy:
            proxies = {}
            if http_proxy:
                proxies["http://"] = http_proxy
            if https_proxy:
                proxies["https://"] = https_proxy

        async with httpx.AsyncClient(timeout=15, proxies=proxies) as client:
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
    try:
        body = await request.json()
    except Exception:
        body = {}
    
    except Exception:
        body = {}
    
    action = body.get("action", "start")
    thread_id = body.get("thread_id") or f"t_{uuid.uuid4().hex[:8]}"
    run_id = body.get("run_id") or f"r_{uuid.uuid4().hex[:8]}"
    
    print(f"[AG-DEBUG] Request received: action={action}, run_id={run_id}")
    
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

    try:
        if action == "start" and supabase:
            supabase.table("jobs").upsert({
                "run_id": run_id,
                "status": "planning",
                "slogan": user_message or "New Project",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }, on_conflict="run_id").execute()
            
            # [FIX] Also register in crew_sessions so get_crew_status works during planning
            try:
                from crewai_session_manager import get_session_manager
                session_manager = get_session_manager()
                if session_manager:
                     session_id = f"session_{run_id}_{int(datetime.utcnow().timestamp() * 1000)}"
                     await session_manager.register_session(
                        run_id=run_id,
                        session_id=session_id,
                        expected_clips=0,
                        context={
                            "goal": user_message or "New Project",
                            "status": "planning"
                        }
                     )
            except Exception as e:
                print(f"[crewai-chat] Failed to register session: {e}")
                
    except Exception as e:
        print(f"Failed to init job row: {e}")
    
    # Define emit helper for SSE events
    async def emit(agent: str, event_type: str, run_id: str, thread_id: str, delta: str = "", payload: dict = None):
        """
        Generate chunks for useChat. 
        0: text delta
        2: data (metadata)
        """
        if delta:
            yield f'0:{json.dumps(delta, ensure_ascii=False)}\n'
        if payload:
            # Wrap payload in a list for Vercel AI SDK data protocol
            yield f'2:[{json.dumps(payload, ensure_ascii=False)}]\n'
    
    async def generator():
        from langgraph_workflow import get_workflow_app
        from langchain_core.messages import HumanMessage, AIMessage
        
        app = get_workflow_app()
        config = {"configurable": {"thread_id": thread_id}}
        
        try:
            if action == "start":
                initial_info = body.get("initial_info") or {}
                # Start new workflow
                inputs = {
                    "goal": user_message or initial_info.get("theme") or "New Project",
                    "styles": initial_info.get("styles", []),
                    "total_duration": initial_info.get("total_duration", 10.0),
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "status": "gathering",
                    "collected_info": initial_info,
                    "messages": [HumanMessage(content=user_message)] if user_message else []
                }
                
                # Persistence (Optional but good for tracking)
                if supabase:
                    try:
                        supabase.table("jobs").upsert({
                            "run_id": run_id,
                            "status": "gathering",
                            "slogan": inputs["goal"],
                            "created_at": datetime.utcnow().isoformat(),
                            "updated_at": datetime.utcnow().isoformat()
                        }, on_conflict="run_id").execute()
                    except: pass

                async for chunk in app.astream(inputs, config, stream_mode="values"):
                    if "messages" in chunk and chunk["messages"]:
                        last_msg = chunk["messages"][-1]
                        if isinstance(last_msg, AIMessage):
                            # Emit question with options
                            payload = {
                                "options": chunk.get("options", []),
                                "next_question": chunk.get("next_question"),
                                "collected_info": chunk.get("collected_info")
                            }
                            # Sync state to DB for UI restoration
                            if supabase:
                                try:
                                    new_messages = chunk.get("messages", [])
                                    serializable_messages = [{"role": "user" if m.type == "human" else "assistant", "content": m.content} for m in new_messages]
                                    supabase.table("crew_sessions").upsert({
                                        "run_id": run_id,
                                        "status": chunk.get("status") or "gathering",
                                        "context": {
                                            "messages": serializable_messages,
                                            "collected_info": chunk.get("collected_info"),
                                            "storyboard": chunk.get("storyboard")
                                        },
                                        "updated_at": datetime.utcnow().isoformat()
                                    }, on_conflict="run_id").execute()
                                except Exception as e:
                                    logger.warning(f"Failed to persist state: {e}")

                            async for sse in emit("创意策划", "question", run_id, thread_id, delta=last_msg.content, payload=payload):
                                yield sse
            else:
                # Continue with user response
                await app.aupdate_state(config, {"messages": [HumanMessage(content=user_message)]})
                async for chunk in app.astream(None, config, stream_mode="values"):
                     if "messages" in chunk and chunk["messages"]:
                        last_msg = chunk["messages"][-1]
                        if isinstance(last_msg, AIMessage):
                            payload = {
                                "options": chunk.get("options", []),
                                "next_question": chunk.get("next_question"),
                                "collected_info": chunk.get("collected_info")
                            }
                            # Sync state to DB for UI restoration
                            if supabase:
                                try:
                                    new_messages = chunk.get("messages", [])
                                    serializable_messages = [{"role": "user" if m.type == "human" else "assistant", "content": m.content} for m in new_messages]
                                    supabase.table("crew_sessions").upsert({
                                        "run_id": run_id,
                                        "status": chunk.get("status") or "gathering",
                                        "context": {
                                            "messages": serializable_messages,
                                            "collected_info": chunk.get("collected_info"),
                                            "storyboard": chunk.get("storyboard")
                                        },
                                        "updated_at": datetime.utcnow().isoformat()
                                    }, on_conflict="run_id").execute()
                                except Exception as e:
                                    logger.warning(f"Failed to persist state: {e}")

                            async for sse in emit("创意策划", "question", run_id, thread_id, delta=last_msg.content, payload=payload):
                                yield sse
                     
                     # Check if we transitioned to planning (storyboard ready)
                     if chunk.get("status") == "awaiting_approval" and "storyboard" in chunk:
                         async for sse in emit("System", "collected", run_id, thread_id, delta="计划生成完成", payload=chunk["storyboard"]):
                             yield sse
        except Exception as e:
            logger.error(f"Error in crewai_chat generator: {e}", exc_info=True)
            async for chunk in emit("System", "error", run_id, thread_id, delta=str(e)):
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
        try:
            from .video_stitcher import stitch_video_segments
        except ImportError:
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



class UploadPresignRequest(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"

@app.post("/upload/presign")
async def upload_presign(body: UploadPresignRequest):
    try:
        key = f"uploads/{uuid.uuid4().hex[:8]}_{body.filename}"
        res = presign_put_url(key=key, content_type=body.content_type)
        return res
    except Exception as e:
        logger.error(f"[upload_presign] Failed: {e}")
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


# [NEW] Scene Regenerate Endpoint
class SceneRegenerateRequest(BaseModel):
    run_id: str
    scene_idx: int
    type: str = "image"
    message_id: Optional[str] = None
    script: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

@app.post("/crewai/scene/regenerate")
async def crewai_scene_regenerate(body: SceneRegenerateRequest):
    try:
        # Initialize Supabase
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        if not supabase_url or not supabase_key:
            return {"error": "Missing Supabase configuration"}
        supabase = create_client(supabase_url, supabase_key)
        
        # 1. Fetch Scene Prompt and Image
        prompt = None
        # Try video_tasks first
        res = supabase.table("video_tasks").select("*").eq("run_id", body.run_id).eq("scene_idx", body.scene_idx).execute()
        if res.data:
            task = res.data[0]
            prompt = task.get("prompt") or task.get("desc")
        
        # Try finding in session context if not in tasks or to verify
        ref_image_url = None
        session_res = supabase.table("crew_sessions").select("context").eq("run_id", body.run_id).execute()
        if session_res.data:
            ctx = session_res.data[0].get("context", {})
            ref_image_url = ctx.get("collected_info", {}).get("image_url")
            
            # If prompt missing, extract from context storyboard
            if not prompt:
                sb = ctx.get("storyboard", {})
                scenes = sb.get("scenes", []) if isinstance(sb, dict) else sb
                if isinstance(scenes, list):
                    # Try to match scene_idx (assuming 1-based)
                    scene = next((s for s in scenes if s.get("scene_idx") == body.scene_idx or s.get("idx") == body.scene_idx), None)
                    if scene:
                        prompt = scene.get("desc") or scene.get("narration")

        if not prompt:
            return {"error": f"Scene {body.scene_idx} description not found"}

        # 2. Generate
        logger.info(f"Regenerating scene {body.scene_idx} (type={body.type}) for run {body.run_id}")
        new_url = None
        
        if body.type == 'image':
            ip = get_image_provider()
            # If provider supports scene generation (RunningHub), use it
            if hasattr(ip, 'generate_scene') and ref_image_url:
                # Use scene description + styles? Styles are in context.
                styles = ctx.get("collected_info", {}).get("styles", []) if 'ctx' in locals() else []
                full_prompt = f"{prompt}, style: {','.join(styles)}" if styles else prompt
                
                res = await ip.generate_scene(text=full_prompt, image_url=ref_image_url)
                if isinstance(res, dict):
                    new_url = res.get("image_url")
                else:
                    new_url = str(res)
            else:
                new_url = await ip.generate(prompt)

        elif body.type == 'video':
            # Not fully supported yet for synchronous regeneration
            return {"error": "Video regeneration not implemented synchronously"}

        if not new_url:
            return {"error": "Generation returned no URL"}

        return {
            "run_id": body.run_id,
            "scene_idx": body.scene_idx,
            "image_url": new_url,
            "type": body.type
        }

    except Exception as e:
        logger.error(f"Scene regeneration failed: {e}", exc_info=True)
        return {"error": str(e)}


# [NEW] Video Stitch Endpoint
class VideoStitchRequest(BaseModel):
    run_id: str
    clips: List[str]

@app.post("/crewai/video/stitch")
async def crewai_video_stitch(body: VideoStitchRequest):
    try:
        # Initialize Supabase
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        if not supabase_url or not supabase_key:
            return {"error": "Missing Supabase configuration"}
        supabase = create_client(supabase_url, supabase_key)

        logger.info(f"Manual stitching requested for run {body.run_id} with {len(body.clips)} clips")

        # Update status to stitching
        supabase.table("crew_sessions").update({
            "status": "stitching",
            "updated_at": datetime.utcnow().isoformat()
        }).eq("run_id", body.run_id).execute()

        # Stitch
        try:
            from video_stitcher import stitch_video_segments
            
            # Fallback: if clips list is empty, fetch from database
            clips_to_stitch = body.clips
            if not clips_to_stitch:
                logger.info(f"Clips list from request is empty for run {body.run_id}, fetching from database")
                vt_res = supabase.table("video_tasks")\
                    .select("video_url")\
                    .eq("run_id", body.run_id)\
                    .eq("status", "succeeded")\
                    .order("clip_idx", desc=False)\
                    .execute()
                if vt_res.data:
                    clips_to_stitch = [r["video_url"] for r in vt_res.data if r.get("video_url")]
            
            if not clips_to_stitch:
                logger.error(f"No successful clips found to stitch for run {body.run_id}")
                return {"error": "No successful clips found to stitch", "status": "failed"}

            logger.info(f"Stitching {len(clips_to_stitch)} clips for run {body.run_id}...")
            final_url = await stitch_video_segments(clips_to_stitch, body.run_id)
            
            # Final Status Update
            supabase.table("crew_sessions").update({
                "status": "completed",
                "result": final_url,
                "updated_at": datetime.utcnow().isoformat()
            }).eq("run_id", body.run_id).execute()
            
            return {
                "run_id": body.run_id,
                "final_url": final_url,
                "status": "completed"
            }
        except Exception as e:
            # Revert status or mark failed
            logger.error(f"Stitching failed: {e}")
            supabase.table("crew_sessions").update({
                "status": "failed",
                "error": str(e),
                "updated_at": datetime.utcnow().isoformat()
            }).eq("run_id", body.run_id).execute()
            return {"error": str(e), "status": "failed"}

    except Exception as e:
        logger.error(f"Stitch endpoint failed: {e}", exc_info=True)
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

