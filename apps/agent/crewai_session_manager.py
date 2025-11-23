"""
CrewAI 会话管理器：处理视频生成任务完成后的回调

功能：
1. 跟踪 CrewAI 执行状态（run_id -> session_id 映射）
2. 检查所有视频片段是否完成
3. 当所有片段完成时，触发拼接任务
4. 支持回调机制，避免 CrewAI agent 长时间轮询
"""

import os
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
from supabase import create_client, Client

logger = logging.getLogger("crewai_tools")


class CrewAISessionManager:
    """
    CrewAI 会话管理器
    
    功能：
    1. 管理 run_id 到 session_id 的映射
    2. 跟踪视频生成任务的完成状态
    3. 当所有任务完成时，触发拼接回调
    """
    
    def __init__(self):
        """初始化会话管理器"""
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        
        if not supabase_url or not supabase_key:
            raise RuntimeError("需要配置 SUPABASE_URL 和 SUPABASE_SERVICE_ROLE_KEY")
        
        # 创建 Supabase 客户端
        # 注意：Supabase Python 客户端可能不支持在 options 中传递 httpx.Client
        # 直接使用默认配置（Supabase 客户端内部会使用自己的 httpx 配置）
        self.supabase: Client = create_client(supabase_url, supabase_key)
        self.logger = logging.getLogger("crewai_tools")
        
        # 确保表存在
        self._ensure_table()
    
    def _ensure_table(self):
        """确保 crew_sessions 表存在"""
        try:
            # 使用 run_id 查询，因为表的主键是 run_id，不是 id
            self.supabase.table("crew_sessions").select("run_id").limit(1).execute()
            self.logger.info("[CrewAISessionManager] Table 'crew_sessions' exists")
        except Exception as e:
            self.logger.warning(
                f"[CrewAISessionManager] Table 'crew_sessions' may not exist. "
                f"Please create it. Error: {e}"
            )
    
    async def register_session(
        self, 
        run_id: str, 
        session_id: str, 
        expected_clips: int,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        注册 CrewAI 会话
        
        Args:
            run_id: 运行 ID
            session_id: 会话 ID（用于回调）
            expected_clips: 期望的视频片段数量
            context: 上下文信息（goal, styles, total_duration 等）
        """
        try:
            # 从 context 中获取初始状态，如果没有则默认为 waiting_videos
            initial_status = (context or {}).get("status", "waiting_videos")
            # 清理 context 中的 status（不应该存储在 context 中）
            clean_context = {k: v for k, v in (context or {}).items() if k != "status"}
            
            session_data = {
                "run_id": run_id,
                "session_id": session_id,
                "expected_clips": expected_clips,
                "status": initial_status,  # running, waiting_videos, ready_to_stitch, stitching, completed, failed
                "context": clean_context,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }
            
            result = self.supabase.table("crew_sessions").upsert(
                session_data,
                on_conflict="run_id"
            ).execute()
            
            self.logger.info(
                f"[CrewAISessionManager] Session registered: run_id={run_id}, "
                f"session_id={session_id}, expected_clips={expected_clips}"
            )
            
            return result.data[0] if result.data else None
        except Exception as e:
            self.logger.error(f"[CrewAISessionManager] Failed to register session: {e}", exc_info=True)
            raise
    
    async def check_and_trigger_stitch(self, run_id: str) -> bool:
        """
        检查所有视频任务是否完成，如果完成则触发拼接
        
        Args:
            run_id: 运行 ID
            
        Returns:
            True 如果已触发拼接，False 如果还有未完成的任务
        """
        try:
            # 获取会话信息（不使用 .single()，因为可能没有记录）
            session_result = self.supabase.table("crew_sessions")\
                .select("*")\
                .eq("run_id", run_id)\
                .execute()
            
            # 检查是否有数据
            if not session_result.data or len(session_result.data) == 0:
                self.logger.warning(
                    f"[CrewAISessionManager] No session found for run_id={run_id}. "
                    f"This might mean the session was not registered. "
                    f"Please check if register_session was called."
                )
                return False
            
            # 如果有多条记录，使用第一条（理论上应该只有一条）
            if len(session_result.data) > 1:
                self.logger.warning(
                    f"[CrewAISessionManager] Multiple sessions found for run_id={run_id}, "
                    f"using the first one. This should not happen."
                )
            
            session = session_result.data[0]
            expected_tasks = session.get("expected_clips", 0)  # 注意：这里存储的是期望的视频任务数，不是镜头数
            current_status = session.get("status", "waiting_videos")
            
            self.logger.info(
                f"[CrewAISessionManager] Checking tasks for run_id={run_id}: "
                f"expected_tasks={expected_tasks}, current_status={current_status}"
            )
            
            # 简单状态检查：如果已经在处理中或已完成，跳过
            if current_status in {"stitching", "completed"}:
                result = session.get("result", "")
                if current_status == "completed" and result and "http" in result.lower():
                    # 检查 result 是否是有效的 URL（不包含 example.com）
                    r2_public_base = os.getenv("R2_PUBLIC_BASE", "")
                    is_valid_url = (
                        result and 
                        ("http" in result.lower() or result.startswith("https://")) and
                        # 确保不是示例 URL（如 cdn.example.com）
                        "example.com" not in result.lower() and
                        # 如果配置了 R2_PUBLIC_BASE，确保 URL 以它开头
                        (not r2_public_base or result.startswith(r2_public_base.rstrip("/")))
                    )
                    
                    if is_valid_url:
                        self.logger.info(
                            f"[CrewAISessionManager] Session {run_id} already completed with valid result: {result[:100]}, skipping"
                        )
                        return True
                    else:
                        # result 包含 example.com 或无效，需要重新触发拼接
                        self.logger.warning(
                            f"[CrewAISessionManager] Session {run_id} status is 'completed' but result URL is invalid: {result[:100]}. "
                            f"Re-triggering stitch to generate correct URL."
                        )
                        # 重置状态为 waiting_videos，继续执行下面的检查逻辑
                        self.supabase.table("crew_sessions")\
                            .update({
                                "status": "waiting_videos",
                                "result": None,
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("run_id", run_id)\
                            .execute()
                        # 继续执行下面的检查逻辑
                elif current_status == "stitching":
                    # 如果正在拼接，检查是否已经完成（可能拼接在后台完成）
                    if result and "http" in result.lower() and "example.com" not in result.lower():
                        # 拼接已完成，更新状态
                        self.supabase.table("crew_sessions")\
                            .update({
                                "status": "completed",
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("run_id", run_id)\
                            .execute()
                        self.logger.info(
                            f"[CrewAISessionManager] Stitch already completed (found result), updated status to completed: {result[:100]}"
                        )
                        return True
                    else:
                        self.logger.debug(
                        f"[CrewAISessionManager] Session {run_id} already in status: {current_status}, skipping"
                    )
                    return False
            
            # 检查视频任务完成情况
            from video_task_queue_supabase import get_supabase_queue
            queue = get_supabase_queue()
            
            if queue:
                # 使用 Supabase 队列检查
                completed_tasks = await queue.get_completed_tasks(run_id)
                pending_tasks = await queue.get_pending_tasks(run_id)
                
                completed_count = len(completed_tasks)
                pending_count = len(pending_tasks)
                
                self.logger.info(
                    f"[CrewAISessionManager] Run {run_id}: "
                    f"completed={completed_count}, pending={pending_count}, expected={expected_tasks}"
                )
                
                # 调试：打印详细的任务信息
                if completed_tasks:
                    self.logger.debug(
                        f"[CrewAISessionManager] Completed tasks for {run_id}: "
                        f"{[t.get('id') for t in completed_tasks]}"
                    )
                if pending_tasks:
                    self.logger.debug(
                        f"[CrewAISessionManager] Pending tasks for {run_id}: "
                        f"{[t.get('id') for t in pending_tasks]}"
                    )
                
                # 检查是否所有任务都完成（按视频任务数，不是镜头数）
                # 注意：completed_count 应该等于 expected_tasks，且没有 pending 任务
                if completed_count >= expected_tasks and pending_count == 0:
                    # 所有任务完成，触发拼接
                    self.logger.info(
                        f"[CrewAISessionManager] All tasks completed for run_id={run_id} "
                        f"(completed={completed_count}, expected={expected_tasks}, pending={pending_count}), "
                        f"triggering stitch"
                    )
                    
                    # 简单更新状态并触发拼接（不需要复杂的锁机制）
                    try:
                        # 更新状态为 stitching（直接开始拼接）
                        self.supabase.table("crew_sessions")\
                            .update({
                                "status": "stitching",
                                "updated_at": datetime.utcnow().isoformat()
                            })\
                            .eq("run_id", run_id)\
                            .execute()
                        
                        # 触发拼接回调
                        await self._trigger_stitch_callback(run_id, session)
                        return True
                    except Exception as e:
                        self.logger.error(
                            f"[CrewAISessionManager] Error triggering stitch: {e}",
                            exc_info=True
                        )
                        return False
                else:
                    # 还有未完成的任务
                    self.logger.info(
                        f"[CrewAISessionManager] Tasks not all completed for run_id={run_id}: "
                        f"completed={completed_count}, expected={expected_tasks}, pending={pending_count}"
                    )
                    # 如果 completed_count >= expected_tasks 但还有 pending，可能是数据不一致
                    if completed_count >= expected_tasks and pending_count > 0:
                        self.logger.warning(
                            f"[CrewAISessionManager] Data inconsistency detected for run_id={run_id}: "
                            f"completed_count ({completed_count}) >= expected_tasks ({expected_tasks}) "
                            f"but still has {pending_count} pending tasks. "
                            f"This might be due to tasks being processed. Will retry later."
                        )
                    return False
            else:
                # 降级：直接查询 video_tasks 表
                result = self.supabase.table("video_tasks")\
                    .select("status")\
                    .eq("run_id", run_id)\
                    .execute()
                
                tasks = result.data if result.data else []
                completed = [t for t in tasks if t.get("status") == "succeeded"]
                pending = [t for t in tasks if t.get("status") in ["pending", "processing", "submitted"]]
                
                if len(completed) >= expected_clips and len(pending) == 0:
                    # 所有任务完成
                    self.supabase.table("crew_sessions")\
                        .update({
                            "status": "ready_to_stitch",
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("run_id", run_id)\
                        .execute()
                    
                    await self._trigger_stitch_callback(run_id, session)
                    return True
                
                return False
        
        except Exception as e:
            self.logger.error(
                f"[CrewAISessionManager] Error checking tasks for run_id={run_id}: {e}",
                exc_info=True
            )
            return False
    
    async def _trigger_stitch_callback(self, run_id: str, session: Dict[str, Any]):
        """
        触发拼接回调：执行拼接任务
        
        Args:
            run_id: 运行 ID
            session: 会话信息
        """
        try:
            session_id = session.get("session_id")
            context = session.get("context", {})
            
            self.logger.info(
                f"[CrewAISessionManager] Triggering stitch callback for run_id={run_id}, "
                f"session_id={session_id}"
            )
            
            # 更新状态为 stitching
            self.supabase.table("crew_sessions")\
                .update({
                    "status": "stitching",
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", run_id)\
                .execute()
            
            # 执行拼接任务（异步，不阻塞）
            asyncio.create_task(self._execute_stitch(run_id, session_id, context))
        
        except Exception as e:
            self.logger.error(
                f"[CrewAISessionManager] Error triggering stitch callback: {e}",
                exc_info=True
            )
    
    async def _execute_stitch(self, run_id: str, session_id: str, context: Dict[str, Any]):
        """
        执行拼接任务
        
        Args:
            run_id: 运行 ID
            session_id: 会话 ID（用于回调通知）
            context: 上下文信息
        
        注意：此方法应该只被调用一次，通过状态锁机制防止重复执行
        """
        try:
            # 再次检查状态，防止重复执行（双重检查锁定模式）
            session_check = self.supabase.table("crew_sessions")\
                .select("status, result")\
                .eq("run_id", run_id)\
                .execute()
            
            # 检查是否有数据
            if not session_check.data or len(session_check.data) == 0:
                self.logger.warning(
                    f"[CrewAISessionManager] No session found for run_id={run_id} during stitch check"
                )
                return
            
            # 使用第一条记录
            session_data = session_check.data[0]
            
            current_status = session_data.get("status", "")
            result = session_data.get("result", "")
            
            # 如果已经有 result 但状态不是 completed，先更新状态
            if result and "http" in result.lower() and "example.com" not in result.lower():
                if current_status != "completed":
                    self.logger.info(
                        f"[CrewAISessionManager] Found result but status is {current_status}, updating to completed: {result[:100]}"
                    )
                    self.supabase.table("crew_sessions")\
                        .update({
                            "status": "completed",
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("run_id", run_id)\
                        .execute()
                    # 更新 jobs 表
                    try:
                        goal = context.get("goal", "") if context else ""
                        cover_url = context.get("cover_url", "") if context else ""
                        self.supabase.table("jobs").upsert({
                            "run_id": run_id,
                            "slogan": goal,
                            "cover_url": cover_url,
                            "video_url": result,
                            "status": "succeeded",
                            "updated_at": datetime.utcnow().isoformat()
                        }, on_conflict="run_id").execute()
                    except Exception as e:
                        self.logger.warning(f"[CrewAISessionManager] Failed to update jobs table: {e}")
                    return
            
            # 如果已经完成且有有效结果，跳过
            if current_status == "completed" and result and "http" in result.lower():
                # 检查 result 是否是有效的 URL（不包含 example.com）
                r2_public_base = os.getenv("R2_PUBLIC_BASE", "")
                is_valid_url = (
                    result and 
                    ("http" in result.lower() or result.startswith("https://")) and
                    "example.com" not in result.lower() and
                    (not r2_public_base or result.startswith(r2_public_base.rstrip("/")))
                )
                
                if is_valid_url:
                    self.logger.info(
                        f"[CrewAISessionManager] Stitch already completed for run_id={run_id}, "
                        f"result={result[:100]}, skipping"
                    )
                    return
                else:
                    # result 包含 example.com 或无效，需要重新执行拼接
                    self.logger.warning(
                        f"[CrewAISessionManager] Stitch result is invalid (contains example.com): {result[:100]}. "
                        f"Re-executing stitch to generate correct URL."
                    )
                    # 重置状态，继续执行拼接
                    self.supabase.table("crew_sessions")\
                        .update({
                            "status": "stitching",
                            "updated_at": datetime.utcnow().isoformat()
                        })\
                        .eq("run_id", run_id)\
                        .execute()
                        # 继续执行拼接逻辑
            # 获取所有完成的视频片段
            from video_task_queue_supabase import get_supabase_queue
            queue = get_supabase_queue()
            
            if queue:
                completed_tasks = await queue.get_completed_tasks(run_id)
            else:
                # 降级：直接查询
                result = self.supabase.table("video_tasks")\
                    .select("*")\
                    .eq("run_id", run_id)\
                    .eq("status", "succeeded")\
                    .order("clip_idx")\
                    .execute()
                completed_tasks = result.data if result.data else []
            
            # 去重：确保每个 task_idx 只出现一次（防止重复拼接）
            # 使用 task_idx 作为唯一标识，保留最新的任务
            task_map = {}
            for task in completed_tasks:
                task_idx = task.get("clip_idx") or task.get("task_idx") or 0
                task_id = task.get("id")
                # 如果同一个 task_idx 有多个任务，保留 ID 最大的（最新的）
                if task_idx not in task_map or (task_id and task_id > task_map[task_idx].get("id", 0)):
                    task_map[task_idx] = task
            
            # 构建 clip_results JSON
            import json
            # 按 task_idx 排序，确保顺序正确（按镜头顺序拼接）
            sorted_task_indices = sorted([int(k) for k in task_map.keys() if k is not None])
            clip_results = [
                {
                    "task_idx": task_idx,
                    "status": "succeeded",
                    "video_url": task_map[task_idx].get("video_url")
                }
                for task_idx in sorted_task_indices
            ]
            
            self.logger.info(
                f"[CrewAISessionManager] Executing stitch for run_id={run_id}: "
                f"{len(clip_results)} unique video segments (deduplicated from {len(completed_tasks)} tasks), "
                f"task_indices={sorted_task_indices}"
            )
            
            # 使用独立的视频拼接函数（不通过 CrewAI tool，避免循环依赖）
            segments = [r.get("video_url") for r in clip_results if r.get("video_url")]
            
            if not segments:
                raise RuntimeError("没有可用的视频片段")
            
            # 调用独立的视频拼接函数
            from video_stitcher import stitch_video_segments
            
            self.logger.info(
                f"[CrewAISessionManager] Calling stitch_video_segments for run_id={run_id}: "
                f"{len(segments)} segments"
            )
            
            final_video_url = await stitch_video_segments(
                segment_urls=segments,
                run_id=run_id,
                output_key=f"{run_id}_final.mp4"
            )
            
            # 更新会话状态为完成
            self.supabase.table("crew_sessions")\
                .update({
                    "status": "completed",
                    "result": final_video_url,
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", run_id)\
                .execute()
            
            self.logger.info(
                f"[CrewAISessionManager] Stitch completed for run_id={run_id}: {final_video_url}"
            )
            
            # 更新 jobs 表的状态为 completed（前端通过 jobs 表检查任务状态）
            try:
                # 从 context 中获取 goal（slogan）和 cover_url
                goal = context.get("goal", "") if context else ""
                cover_url = context.get("cover_url", "") if context else ""
                
                # 如果 context 中没有 goal，尝试从 crew_sessions 表中获取
                if not goal:
                    session_result = self.supabase.table("crew_sessions")\
                        .select("context")\
                        .eq("run_id", run_id)\
                        .execute()
                    if session_result.data and len(session_result.data) > 0:
                        session_context = session_result.data[0].get("context", {})
                        if isinstance(session_context, dict):
                            goal = session_context.get("goal", "")
                
                # 更新 jobs 表
                self.supabase.table("jobs").upsert({
                    "run_id": run_id,
                    "slogan": goal,
                    "cover_url": cover_url,
                    "video_url": final_video_url,
                    "status": "succeeded",  # 使用 "succeeded" 而不是 "completed"，与 persist_success 保持一致
                    "updated_at": datetime.utcnow().isoformat()
                }, on_conflict="run_id").execute()
                
                self.logger.info(
                    f"[CrewAISessionManager] Updated jobs table for run_id={run_id}: status=succeeded, video_url={final_video_url}, goal={goal[:50] if goal else 'N/A'}"
                )
            except Exception as e:
                self.logger.warning(
                    f"[CrewAISessionManager] Failed to update jobs table for run_id={run_id}: {e}",
                    exc_info=True
                )
                # 不抛出异常，避免影响拼接完成的状态更新
            
            # 发送回调通知（如果有回调 URL）
            await self._send_callback_notification(session_id, run_id, final_video_url)
        
        except Exception as e:
            # 拼接失败
            self.supabase.table("crew_sessions")\
                .update({
                    "status": "failed",
                    "error": str(e),
                    "updated_at": datetime.utcnow().isoformat()
                })\
                .eq("run_id", run_id)\
                .execute()
            
            self.logger.error(
                f"[CrewAISessionManager] Stitch failed for run_id={run_id}: {e}",
                exc_info=True
            )
    
    async def _send_callback_notification(self, session_id: str, run_id: str, video_url: str):
        """
        发送回调通知（可选，用于通知前端或其他系统）
        
        Args:
            session_id: 会话 ID
            run_id: 运行 ID
            video_url: 最终视频 URL
        """
        # 这里可以实现回调逻辑，比如：
        # 1. 发送 WebSocket 消息
        # 2. 调用回调 API
        # 3. 发送通知到消息队列
        # 目前先记录日志
        self.logger.info(
            f"[CrewAISessionManager] Callback notification: "
            f"session_id={session_id}, run_id={run_id}, video_url={video_url}"
        )


# 单例
_session_manager: Optional[CrewAISessionManager] = None


def get_session_manager() -> Optional[CrewAISessionManager]:
    """获取会话管理器实例（单例）"""
    global _session_manager
    
    if _session_manager is None:
        try:
            _session_manager = CrewAISessionManager()
        except Exception as e:
            logger.warning(f"[get_session_manager] Failed to initialize: {e}")
            return None
    
    return _session_manager


"""
数据库表结构（需要在 Supabase 中创建）：

CREATE TABLE IF NOT EXISTS crew_sessions (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  expected_clips INTEGER NOT NULL,
  status TEXT DEFAULT 'waiting_videos',  -- waiting_videos, ready_to_stitch, stitching, completed, failed
  context JSONB,  -- 存储上下文信息（goal, styles, total_duration 等）
  result TEXT,  -- 最终视频 URL
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_crew_sessions_session_id ON crew_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_crew_sessions_status ON crew_sessions(status);
"""

