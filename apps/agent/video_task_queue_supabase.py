"""
基于 Supabase 数据库的视频任务队列实现

优势：
1. 持久化：任务不会因为服务重启而丢失
2. 多实例支持：可以在多个 Railway 实例间共享队列
3. 可靠性：数据库事务保证数据一致性
4. 免费版通常足够：Supabase 免费版提供 500MB 数据库存储，对于任务队列足够

成本分析：
- Supabase 免费版：500MB 数据库存储，通常足够小到中等规模使用
- 如果已有 Supabase 账号，无需额外付费
- 如果任务量很大，可以考虑 Railway 的 PostgreSQL（按使用量付费）

使用方式：
1. 在 Supabase 中创建 video_tasks 表
2. 配置 SUPABASE_URL 和 SUPABASE_SERVICE_ROLE_KEY
3. 替换当前的内存队列管理器
"""

import os
import asyncio
import logging
from typing import Optional, Dict, Any, Callable, List
from datetime import datetime, timedelta
from supabase import create_client, Client
import json
import httpx

logger = logging.getLogger("crewai_tools")


class SupabaseVideoTaskQueue:
    """
    基于 Supabase 数据库的视频任务队列
    
    功能：
    1. 将任务持久化到数据库
    2. 支持多实例并发处理
    3. 自动重试失败的任务（队列满的情况）
    4. 任务状态跟踪
    """
    
    def __init__(self, retry_interval: float = 10.0, max_concurrent: int = 2):
        """
        初始化队列
        
        Args:
            retry_interval: 重试间隔（秒）
            max_concurrent: 最大并发数
        """
        self.retry_interval = retry_interval
        self.max_concurrent = max_concurrent
        
        # 初始化 Supabase 客户端
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        
        if not supabase_url or not supabase_key:
            raise RuntimeError("需要配置 SUPABASE_URL 和 SUPABASE_SERVICE_ROLE_KEY")
        
        # 先初始化 logger，因为后面可能会用到
        self.logger = logging.getLogger("crewai_tools")
        
        # 创建 Supabase 客户端
        # 注意：Supabase Python 客户端可能不支持在 options 中传递 httpx.Client
        # 直接使用默认配置（Supabase 客户端内部会使用自己的 httpx 配置）
        self.supabase: Client = create_client(supabase_url, supabase_key)
        
        # 后台任务
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False
        
        # 确保表存在
        self._ensure_table()
    
    def _ensure_table(self):
        """确保 video_tasks 表存在（如果不存在，记录警告）"""
        try:
            # 尝试查询表，如果不存在会报错
            self.supabase.table("video_tasks").select("id").limit(1).execute()
            self.logger.info("[SupabaseVideoTaskQueue] Table 'video_tasks' exists")
        except Exception as e:
            self.logger.warning(
                f"[SupabaseVideoTaskQueue] Table 'video_tasks' may not exist. "
                f"Please create it with the SQL in the docstring. Error: {e}"
            )
    
    async def add_task(
        self, 
        run_id: str,
        clip_idx: int,
        prompt: str,
        ref_img: Optional[str] = None,
        duration: int = 10,
        retry_count: int = 0
    ) -> Dict[str, Any]:
        """
        添加任务到队列
        
        Args:
            run_id: 运行 ID
            clip_idx: 镜头序号
            prompt: 提示词
            ref_img: 参考图片
            duration: 视频时长
            retry_count: 重试次数
            
        Returns:
            任务信息字典
        """
        task_data = {
            "run_id": run_id,
            "clip_idx": clip_idx,
            "prompt": prompt,
            "ref_img": ref_img or "",
            "duration": duration,
            "status": "pending",
            "retry_count": retry_count,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        try:
            result = self.supabase.table("video_tasks").insert(task_data).execute()
            task_id = result.data[0].get("id") if result.data else None
            
            self.logger.info(
                f"[SupabaseVideoTaskQueue] Task added: run_id={run_id}, clip_idx={clip_idx}, task_id={task_id}"
            )
            
            # 启动 worker（如果还没启动）
            # 注意：worker 需要在应用启动时启动，而不是在这里
            # 这里只是确保 worker 在运行
            if not self._running:
                try:
                    # 尝试获取当前事件循环
                    loop = asyncio.get_running_loop()
                    self._running = True
                    self._worker_task = asyncio.create_task(self._worker_loop())
                    self.logger.info("[SupabaseVideoTaskQueue] Worker task created in add_task")
                except RuntimeError:
                    # 没有运行中的事件循环，worker 无法启动
                    # 这通常意味着需要在应用启动时启动 worker
                    self.logger.warning(
                        "[SupabaseVideoTaskQueue] No running event loop, worker cannot start. "
                        "Please start worker in application startup."
                    )
            
            return {
                "id": task_id,
                "run_id": run_id,
                "clip_idx": clip_idx,
                "status": "pending"
            }
        except Exception as e:
            self.logger.error(f"[SupabaseVideoTaskQueue] Failed to add task: {e}", exc_info=True)
            raise
    
    async def _worker_loop(self):
        """后台 worker 循环：处理队列中的任务"""
        self.logger.info("[SupabaseVideoTaskQueue] Worker loop started")
        
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while self._running:
            try:
                await asyncio.sleep(self.retry_interval)
                
                # 获取待处理的任务（按创建时间排序，FIFO）
                # 包括 pending（待提交）和 submitted（已提交，需要轮询状态）的任务
                # 限制数量，避免一次处理太多
                try:
                    # 添加重试机制处理网络错误
                    result = None
                    for retry in range(3):
                        try:
                            result = self.supabase.table("video_tasks")\
                                .select("*")\
                                .in_("status", ["pending", "submitted"])\
                                .order("created_at", desc=False)\
                                .limit(self.max_concurrent)\
                                .execute()
                            consecutive_errors = 0  # 重置错误计数
                            break
                        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.PoolTimeout) as e:
                            if retry < 2:
                                wait_time = (retry + 1) * 2  # 2s, 4s
                                self.logger.warning(
                                    f"[SupabaseVideoTaskQueue] Network error fetching tasks (attempt {retry + 1}/3): {e}, "
                                    f"retrying in {wait_time}s..."
                                )
                                await asyncio.sleep(wait_time)
                            else:
                                raise
                    
                    if result is None:
                        consecutive_errors += 1
                        if consecutive_errors >= max_consecutive_errors:
                            self.logger.error(
                                f"[SupabaseVideoTaskQueue] Too many consecutive errors ({consecutive_errors}), "
                                f"waiting longer before retry..."
                            )
                            await asyncio.sleep(self.retry_interval * 2)
                            consecutive_errors = 0
                        continue
                    
                    tasks = result.data if result.data else []
                    
                    if tasks:
                        pending_count = len([t for t in tasks if t.get("status") == "pending"])
                        submitted_count = len([t for t in tasks if t.get("status") == "submitted"])
                        self.logger.info(
                            f"[SupabaseVideoTaskQueue] Found {len(tasks)} tasks "
                            f"(pending={pending_count}, submitted={submitted_count})"
                        )
                        
                        # 打印 submitted 任务的详细信息，便于调试
                        if submitted_count > 0:
                            submitted_tasks = [t for t in tasks if t.get("status") == "submitted"]
                            for st in submitted_tasks:
                                self.logger.debug(
                                    f"[SupabaseVideoTaskQueue] Submitted task: "
                                    f"id={st.get('id')}, run_id={st.get('run_id')}, "
                                    f"provider_task_id={st.get('provider_task_id')}, clip_idx={st.get('clip_idx')}"
                                )
                        
                        # 并发处理任务
                        await asyncio.gather(*[
                            self._process_task(task) 
                            for task in tasks
                        ], return_exceptions=True)
                
                except Exception as e:
                    consecutive_errors += 1
                    error_type = type(e).__name__
                    
                    # 对于网络错误，记录但不中断循环
                    if isinstance(e, (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError, httpx.PoolTimeout)):
                        self.logger.warning(
                            f"[SupabaseVideoTaskQueue] Network error fetching tasks: {error_type}: {e}"
                        )
                        if consecutive_errors >= max_consecutive_errors:
                            self.logger.error(
                                f"[SupabaseVideoTaskQueue] Too many consecutive network errors ({consecutive_errors}), "
                                f"waiting longer before retry..."
                            )
                            await asyncio.sleep(self.retry_interval * 2)
                            consecutive_errors = 0
                    else:
                        self.logger.error(f"[SupabaseVideoTaskQueue] Error fetching tasks: {error_type}: {e}", exc_info=True)
                        if consecutive_errors >= max_consecutive_errors:
                            await asyncio.sleep(self.retry_interval * 2)
                            consecutive_errors = 0
                    
            except Exception as e:
                consecutive_errors += 1
                self.logger.error(f"[SupabaseVideoTaskQueue] Error in worker loop: {type(e).__name__}: {e}", exc_info=True)
                if consecutive_errors >= max_consecutive_errors:
                    await asyncio.sleep(self.retry_interval * 2)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(self.retry_interval)
    
    async def _process_task(self, task: Dict[str, Any]):
        """处理单个任务"""
        task_id = task.get("id")
        run_id = task.get("run_id")
        clip_idx = task.get("clip_idx")
        prompt = task.get("prompt")
        ref_img = task.get("ref_img") or ""
        duration = task.get("duration", 10)
        retry_count = task.get("retry_count", 0)
        
        try:
            # 如果任务状态是 submitted，需要轮询 RunningHub 状态（不更新为 processing）
            if task.get("status") == "submitted":
                # 轮询 RunningHub 任务状态
                provider_task_id = task.get("provider_task_id")
                if provider_task_id:
                    self.logger.info(
                        f"[SupabaseVideoTaskQueue] Processing submitted task: "
                        f"task_id={task_id}, provider_task_id={provider_task_id}, run_id={run_id}, clip_idx={clip_idx}"
                    )
                    await self._poll_runninghub_task(task_id, provider_task_id, run_id, clip_idx)
                else:
                    self.logger.warning(
                        f"[SupabaseVideoTaskQueue] Task {task_id} is submitted but has no provider_task_id, "
                        f"cannot poll RunningHub"
                    )
                return
            
            # 更新状态为 processing（仅对 pending 状态的任务）
            self.supabase.table("video_tasks")\
                .update({
                    "status": "processing",
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("id", task_id)\
                .execute()
            
            # 调用视频生成 provider
            # 使用相对导入，避免循环依赖
            import sys
            import importlib
            if 'apps.agent.providers' not in sys.modules and 'providers' not in sys.modules:
                from .providers import get_video_provider
            else:
                try:
                    providers_module = importlib.import_module('apps.agent.providers')
                except Exception:
                    providers_module = importlib.import_module('providers')
                get_video_provider = providers_module.get_video_provider
            
            video_provider = get_video_provider()
            
            # 使用异步模式提交任务
            result = await video_provider.generate(
                prompt=prompt,
                image_url=ref_img if ref_img else None,
                duration=duration,
                async_mode=True
            )
            
            if isinstance(result, dict) and result.get("pending"):
                # 任务已提交，更新状态
                task_id_runninghub = result.get("task_id")
                self.supabase.table("video_tasks")\
                    .update({
                        "status": "submitted",
                        "provider_task_id": task_id_runninghub,
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("id", task_id)\
                    .execute()
                
                self.logger.info(
                    f"[SupabaseVideoTaskQueue] Task {task_id} submitted to RunningHub: {task_id_runninghub}"
                )
            else:
                # 同步模式，直接完成
                video_url = result.get("video_url") if isinstance(result, dict) else str(result)
                if video_url:
                    # 上传到 R2
                    if 'r2' not in sys.modules:
                        from r2 import upload_url_to_r2
                    else:
                        r2_module = importlib.import_module('r2')
                        upload_url_to_r2 = r2_module.upload_url_to_r2
                    
                    cdn_url = await upload_url_to_r2(video_url, f"{run_id}_clip{clip_idx}.mp4")
                    
                    self.supabase.table("video_tasks")\
                        .update({
                            "status": "succeeded",
                            "video_url": cdn_url,
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("id", task_id)\
                        .execute()
                    
                    self.logger.info(f"[SupabaseVideoTaskQueue] Task {task_id} completed: {cdn_url}")
                    
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
                        self.logger.debug(
                            f"[SupabaseVideoTaskQueue] Failed to trigger stitch callback: {e}"
                        )
        
        except Exception as e:
            error_str = str(e)
            is_queue_full = "TASK_QUEUE_MAXED" in error_str or "421" in error_str or "队列" in error_str
            
            if is_queue_full:
                # 队列满，重新加入队列
                retry_count += 1
                max_retries = 10
                
                if retry_count < max_retries:
                    self.supabase.table("video_tasks")\
                        .update({
                            "status": "pending",
                            "retry_count": retry_count,
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("id", task_id)\
                        .execute()
                    
                    self.logger.warning(
                        f"[SupabaseVideoTaskQueue] Task {task_id} queue full, "
                        f"re-queued (retry_count={retry_count}/{max_retries})"
                    )
                else:
                    # 超过最大重试次数
                    self.supabase.table("video_tasks")\
                        .update({
                            "status": "failed",
                            "error": f"Max retries exceeded: {error_str}",
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("id", task_id)\
                        .execute()
                    
                    self.logger.error(
                        f"[SupabaseVideoTaskQueue] Task {task_id} failed after {max_retries} retries"
                    )
            else:
                # 其他错误
                self.supabase.table("video_tasks")\
                    .update({
                        "status": "failed",
                        "error": error_str,
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("id", task_id)\
                    .execute()
                
                self.logger.error(
                    f"[SupabaseVideoTaskQueue] Task {task_id} failed: {error_str}",
                    exc_info=True
                )
    
    async def _poll_runninghub_task(self, task_id: str, provider_task_id: str, run_id: str, clip_idx: int):
        """轮询 RunningHub 任务状态，如果完成则更新数据库"""
        try:
            from .runninghub_client import RunningHubClient
            client = RunningHubClient()
            
            # 检查任务状态
            self.logger.info(
                f"[SupabaseVideoTaskQueue] Polling RunningHub task: "
                f"task_id={task_id}, provider_task_id={provider_task_id}, run_id={run_id}, clip_idx={clip_idx}"
            )
            status = await client.get_status(provider_task_id)
            # 状态可能是字符串，转换为大写进行比较
            status_upper = str(status).upper() if status else ""
            self.logger.info(
                f"[SupabaseVideoTaskQueue] RunningHub task {provider_task_id} status: {status} (normalized: {status_upper})"
            )
            
            # 检查多种成功状态值
            if status_upper in {"SUCCESS", "SUCCEEDED", "FINISHED", "DONE", "COMPLETED"}:
                # 任务成功，获取视频 URL
                outputs = await client.get_outputs(provider_task_id)
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
                    import sys
                    import importlib
                    if 'r2' not in sys.modules:
                        from r2 import upload_url_to_r2
                    else:
                        r2_module = importlib.import_module('r2')
                        upload_url_to_r2 = r2_module.upload_url_to_r2
                    
                    cdn_url = await upload_url_to_r2(video_url, f"{run_id}_task{clip_idx}.mp4")
                    
                    # 更新数据库
                    self.supabase.table("video_tasks")\
                        .update({
                            "status": "succeeded",
                            "video_url": cdn_url,
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("id", task_id)\
                        .execute()
                    
                    self.logger.info(
                        f"[SupabaseVideoTaskQueue] Task {task_id} (RunningHub {provider_task_id}) "
                        f"completed: {cdn_url}"
                    )
                    
                    # 检查是否所有任务完成，如果完成则触发拼接回调
                    try:
                        from crewai_session_manager import get_session_manager
                        session_manager = get_session_manager()
                        if session_manager:
                            # 立即检查并触发拼接（不阻塞，使用 create_task）
                            asyncio.create_task(
                                session_manager.check_and_trigger_stitch(run_id)
                            )
                            self.logger.info(
                                f"[SupabaseVideoTaskQueue] Triggered stitch check for run_id={run_id}"
                            )
                    except Exception as e:
                        self.logger.warning(
                            f"[SupabaseVideoTaskQueue] Failed to trigger stitch callback: {e}",
                            exc_info=True
                        )
                else:
                    self.logger.warning(
                        f"[SupabaseVideoTaskQueue] Task {task_id} succeeded but no video URL found"
                    )
            elif status_upper in {"FAILED", "ERROR", "FAILURE"}:
                # 任务失败
                self.supabase.table("video_tasks")\
                    .update({
                        "status": "failed",
                        "error": f"RunningHub task failed: {status}",
                        "updated_at": datetime.utcnow().isoformat()
                    })\
                    .eq("id", task_id)\
                    .execute()
                
                self.logger.error(
                    f"[SupabaseVideoTaskQueue] Task {task_id} (RunningHub {provider_task_id}) failed: {status}"
                )
            # 如果状态是 PENDING, RUNNING, QUEUED，不做任何操作，等待下次轮询
        except Exception as e:
            self.logger.warning(
                f"[SupabaseVideoTaskQueue] Error polling RunningHub task {provider_task_id}: {e}"
            )
            # 不更新状态，等待下次轮询
    
    async def get_pending_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        """获取指定 run_id 的待处理任务（包括 pending, processing, submitted）"""
        try:
            result = self.supabase.table("video_tasks")\
                .select("*")\
                .eq("run_id", run_id)\
                .in_("status", ["pending", "processing", "submitted"])\
                .execute()
            
            return result.data if result.data else []
        except Exception as e:
            self.logger.error(f"[SupabaseVideoTaskQueue] Failed to get pending tasks: {e}", exc_info=True)
            return []
    
    async def get_completed_tasks(self, run_id: str) -> List[Dict[str, Any]]:
        """获取指定 run_id 的已完成任务"""
        try:
            result = self.supabase.table("video_tasks")\
                .select("*")\
                .eq("run_id", run_id)\
                .eq("status", "succeeded")\
                .execute()
            
            return result.data if result.data else []
        except Exception as e:
            self.logger.error(f"[SupabaseVideoTaskQueue] Failed to get completed tasks: {e}", exc_info=True)
            return []
    
    async def poll_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        轮询任务状态（用于检查 submitted 状态的任务是否完成）
        
        Args:
            task_id: Supabase 任务 ID 或 RunningHub task_id
            
        Returns:
            任务信息，如果完成则包含 video_url
        """
        try:
            # 先尝试用 Supabase task_id 查询
            result = self.supabase.table("video_tasks")\
                .select("*")\
                .eq("id", task_id)\
                .single()\
                .execute()
            
            task = result.data if result.data else None
            
            if not task:
                # 尝试用 provider_task_id 查询
                result = self.supabase.table("video_tasks")\
                    .select("*")\
                    .eq("provider_task_id", task_id)\
                    .single()\
                    .execute()
                
                task = result.data if result.data else None
            
            if not task:
                return None
            
            # 如果状态是 submitted，轮询 RunningHub 状态
            if task.get("status") == "submitted":
                provider_task_id = task.get("provider_task_id")
                if provider_task_id:
                    from .runninghub_client import RunningHubClient
                    client = RunningHubClient()
                    
                    status = await client.get_status(provider_task_id)
                    
                    if status == "SUCCESS":
                        # 获取视频 URL
                        outputs = await client.get_outputs(provider_task_id)
                        for item in outputs:
                            url = (
                                item.get("fileUrl") 
                                or item.get("url") 
                                or item.get("ossUrl") 
                                or item.get("downloadUrl")
                                or (item.get("value") if isinstance(item.get("value"), str) else None)
                            )
                            if url and isinstance(url, str) and ("mp4" in url.lower() or url.lower().endswith(".mp4")):
                                # 上传到 R2
                                import sys
                                import importlib
                                if 'r2' not in sys.modules:
                                    from r2 import upload_url_to_r2
                                else:
                                    r2_module = importlib.import_module('r2')
                                    upload_url_to_r2 = r2_module.upload_url_to_r2
                                
                                cdn_url = await upload_url_to_r2(url, f"{task.get('run_id')}_clip{task.get('clip_idx')}.mp4")
                                
                                # 更新数据库
                                self.supabase.table("video_tasks")\
                                    .update({
                                        "status": "succeeded",
                                        "video_url": cdn_url,
                                        "updated_at": datetime.utcnow().isoformat()
                                    })\
                                    .eq("id", task.get("id"))\
                                    .execute()
                                
                                task["status"] = "succeeded"
                                task["video_url"] = cdn_url
                                return task
                    
                    elif status in {"FAILED", "ERROR"}:
                        # 任务失败
                        self.supabase.table("video_tasks")\
                            .update({
                                "status": "failed",
                                "error": f"RunningHub task failed: {status}",
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("id", task.get("id"))\
                            .execute()
                        
                        task["status"] = "failed"
                        return task
            
            return task
        
        except Exception as e:
            self.logger.error(f"[SupabaseVideoTaskQueue] Failed to poll task status: {e}", exc_info=True)
            return None
    
    def stop(self):
        """停止 worker"""
        self._running = False
        if self._worker_task and not self._worker_task.done():
            try:
                self._worker_task.cancel()
            except Exception:
                # 忽略取消任务时的异常
                pass


# 单例
_supabase_queue: Optional[SupabaseVideoTaskQueue] = None


def get_supabase_queue() -> Optional[SupabaseVideoTaskQueue]:
    """获取 Supabase 队列实例（单例）"""
    global _supabase_queue
    
    if _supabase_queue is None:
        try:
            _supabase_queue = SupabaseVideoTaskQueue(
                retry_interval=20.0,
                max_concurrent=2
            )
            # 尝试启动 worker（如果有运行中的事件循环）
            try:
                loop = asyncio.get_running_loop()
                if not _supabase_queue._running:
                    _supabase_queue._running = True
                    _supabase_queue._worker_task = asyncio.create_task(_supabase_queue._worker_loop())
                    logger.info("[get_supabase_queue] Worker started")
            except RuntimeError:
                # 没有运行中的事件循环，worker 将在应用启动时启动
                logger.debug("[get_supabase_queue] No running event loop, worker will start on app startup")
        except Exception as e:
            logger.warning(f"[get_supabase_queue] Failed to initialize: {e}")
            return None
    
    return _supabase_queue


def start_supabase_queue_worker():
    """在应用启动时启动 Supabase 队列 worker"""
    global _supabase_queue
    
    if _supabase_queue is None:
        queue = get_supabase_queue()
        if queue is None:
            return
    
    if _supabase_queue and not _supabase_queue._running:
        try:
            loop = asyncio.get_running_loop()
            _supabase_queue._running = True
            _supabase_queue._worker_task = asyncio.create_task(_supabase_queue._worker_loop())
            logger.info("[start_supabase_queue_worker] Worker started on application startup")
        except RuntimeError:
            logger.warning("[start_supabase_queue_worker] No running event loop, cannot start worker")


"""
数据库表结构（需要在 Supabase 中创建）：

CREATE TABLE IF NOT EXISTS video_tasks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id TEXT NOT NULL,
  clip_idx INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  ref_img TEXT,
  duration INTEGER DEFAULT 10,
  status TEXT DEFAULT 'pending',  -- pending, processing, submitted, succeeded, failed
  provider_task_id TEXT,  -- RunningHub task_id
  video_url TEXT,
  error TEXT,
  retry_count INTEGER DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 创建索引
CREATE INDEX IF NOT EXISTS idx_video_tasks_run_id ON video_tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_video_tasks_status ON video_tasks(status);
CREATE INDEX IF NOT EXISTS idx_video_tasks_created_at ON video_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_video_tasks_provider_task_id ON video_tasks(provider_task_id);

-- 可选：添加约束
ALTER TABLE video_tasks ADD CONSTRAINT check_status 
  CHECK (status IN ('pending', 'processing', 'submitted', 'succeeded', 'failed'));
"""

