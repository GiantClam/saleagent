"""
CrewAI Tools - 封装视频生成工作流的工具函数
"""
import os
from typing import Optional, List, Dict, Any, Callable
from crewai.tools import tool
from openrouter_client import OpenRouterClient, OpenRouterError
import httpx
from providers import get_image_provider, get_video_provider
from r2 import upload_url_to_r2
from runninghub_client import RunningHubClient

# 尝试导入 Supabase 队列（可选）
try:
    from video_task_queue_supabase import get_supabase_queue
    _supabase_queue_available = True
except ImportError:
    _supabase_queue_available = False
    get_supabase_queue = None

# 环境变量
OPENROUTER_BASE = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY")
EMBED_REFERER = os.getenv("EMBEDDING_REFERER", os.getenv("SITE_URL", "https://saleagent.app"))
PROMPT_LLM_MODEL = os.getenv("PROMPT_LLM_MODEL", "openai/gpt-5-mini")  # 默认使用 gpt-5-mini，结构化输出更稳定
STORYBOARD_LLM_MODEL = os.getenv("STORYBOARD_LLM_MODEL", "openai/gpt-4o-mini")  # 分镜规划专用模型

# 全局客户端（懒加载）
_image_provider = None
_video_provider = None
_runninghub_client = None

# 任务队列管理器
_task_queue_manager = None


def _get_image_provider():
    global _image_provider
    if _image_provider is None:
        _image_provider = get_image_provider()
    return _image_provider


def _get_video_provider():
    global _video_provider
    if _video_provider is None:
        _video_provider = get_video_provider()
    return _video_provider


def _get_runninghub_client():
    global _runninghub_client
    if _runninghub_client is None:
        _runninghub_client = RunningHubClient()
    return _runninghub_client


class VideoTaskQueueManager:
    """
    视频任务队列管理器：处理 RunningHub 任务队列满的情况
    
    功能：
    1. 缓存失败的任务（队列满或超过并发数）
    2. 定时（10秒）重试队列中的任务
    3. 管理并发数限制
    """
    def __init__(self, retry_interval: float = 10.0, max_concurrent: int = 2):
        import asyncio
        import logging
        from collections import deque
        from datetime import datetime
        
        self.retry_interval = retry_interval  # 重试间隔（秒）
        self.max_concurrent = max_concurrent  # 最大并发数
        self.queue: deque = deque()  # 任务队列
        self.active_tasks: set = set()  # 正在处理的任务
        self.logger = logging.getLogger("crewai_tools")
        self._retry_task: Optional[asyncio.Task] = None
        # 不在初始化时创建 Lock，而是在使用时创建，避免事件循环绑定问题
        self._lock = None
        
    async def add_task(self, task_func: Callable, *args, **kwargs) -> Any:
        """
        添加任务到队列或直接执行
        
        Args:
            task_func: 异步任务函数
            *args, **kwargs: 任务参数
            
        Returns:
            任务结果
        """
        import asyncio
        from datetime import datetime
        
        # 如果队列为空且活跃任务数未达上限，直接执行
        # 延迟创建 Lock，避免事件循环绑定问题
        if self._lock is None:
            self._lock = asyncio.Lock()
        
        async with self._lock:
            if len(self.active_tasks) < self.max_concurrent and len(self.queue) == 0:
                task_id = id(task_func)
                self.active_tasks.add(task_id)
                try:
                    result = await task_func(*args, **kwargs)
                    return result
                finally:
                    self.active_tasks.discard(task_id)
        
        # 否则加入队列
        queue_item = {
            "task_func": task_func,
            "args": args,
            "kwargs": kwargs,
            "added_at": datetime.utcnow(),
            "retry_count": 0
        }
        self.queue.append(queue_item)
        self.logger.info(f"[VideoTaskQueueManager] Task added to queue, queue size: {len(self.queue)}")
        
        # 启动后台重试任务（如果还没启动）
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self._retry_loop())
        
        # 返回 pending 状态，表示任务已加入队列
        return {"pending": True, "queued": True, "queue_position": len(self.queue)}
    
    async def _retry_loop(self):
        """后台重试循环：每10秒处理一次队列"""
        import asyncio
        from datetime import datetime
        
        self.logger.info("[VideoTaskQueueManager] Retry loop started")
        
        while True:
            try:
                await asyncio.sleep(self.retry_interval)
                
                # 处理队列中的任务
                processed = 0
                # 延迟创建 Lock，避免事件循环绑定问题
                if self._lock is None:
                    self._lock = asyncio.Lock()
                
                async with self._lock:
                    # 检查是否有可用槽位
                    available_slots = self.max_concurrent - len(self.active_tasks)
                    
                    while available_slots > 0 and len(self.queue) > 0:
                        queue_item = self.queue.popleft()
                        task_func = queue_item["task_func"]
                        args = queue_item["args"]
                        kwargs = queue_item["kwargs"]
                        retry_count = queue_item["retry_count"]
                        
                        task_id = id(task_func)
                        self.active_tasks.add(task_id)
                        
                        # 异步执行任务
                        asyncio.create_task(self._execute_task(task_id, task_func, args, kwargs, retry_count))
                        processed += 1
                        available_slots -= 1
                
                if processed > 0:
                    self.logger.info(f"[VideoTaskQueueManager] Processed {processed} tasks from queue, remaining: {len(self.queue)}")
                    
            except Exception as e:
                self.logger.error(f"[VideoTaskQueueManager] Error in retry loop: {e}", exc_info=True)
    
    async def _execute_task(self, task_id: int, task_func: Callable, args: tuple, kwargs: dict, retry_count: int):
        """执行单个任务"""
        import asyncio
        from datetime import datetime
        
        try:
            result = await task_func(*args, **kwargs)
            self.logger.info(f"[VideoTaskQueueManager] Task executed successfully (retry_count={retry_count})")
            return result
        except Exception as e:
            error_str = str(e)
            is_queue_full = "TASK_QUEUE_MAXED" in error_str or "421" in error_str or "队列" in error_str
            
            if is_queue_full:
                # 队列满，重新加入队列
                retry_count += 1
                max_retries = 10  # 最多重试10次
                
                if retry_count < max_retries:
                    queue_item = {
                        "task_func": task_func,
                        "args": args,
                        "kwargs": kwargs,
                        "added_at": datetime.utcnow(),
                        "retry_count": retry_count
                    }
                    async with self._lock:
                        self.queue.append(queue_item)
                    self.logger.warning(
                        f"[VideoTaskQueueManager] Task failed (queue full), re-queued "
                        f"(retry_count={retry_count}/{max_retries}, queue_size={len(self.queue)})"
                    )
                else:
                    self.logger.error(
                        f"[VideoTaskQueueManager] Task failed after {max_retries} retries: {e}"
                    )
            else:
                # 其他错误，记录但不重试
                self.logger.error(
                    f"[VideoTaskQueueManager] Task failed with non-queue error: {e}",
                    exc_info=True
                )
        finally:
            # 延迟创建 Lock，避免事件循环绑定问题
            if self._lock is None:
                self._lock = asyncio.Lock()
            
            async with self._lock:
                self.active_tasks.discard(task_id)


def _get_task_queue_manager() -> VideoTaskQueueManager:
    """获取任务队列管理器（单例）"""
    global _task_queue_manager
    if _task_queue_manager is None:
        _task_queue_manager = VideoTaskQueueManager(
            retry_interval=10.0,  # 10秒重试间隔
            max_concurrent=2  # 最大并发数
        )
    return _task_queue_manager


def _run_async_safe(coro):
    """
    安全地在同步函数中运行异步代码，支持标准 asyncio 和 uvloop。
    
    Args:
        coro: 协程对象
        
    Returns:
        协程的执行结果
    """
    import asyncio
    import concurrent.futures
    import threading
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    # 首先尝试获取运行中的事件循环
    try:
        loop = asyncio.get_running_loop()
        # 有运行中的事件循环，需要在新线程中运行
        logger.debug(f"[_run_async_safe] Found running event loop, using thread pool")
        future = concurrent.futures.Future()
        
        def run_in_thread():
            try:
                # 在新线程中创建新的事件循环
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                result = new_loop.run_until_complete(coro)
                future.set_result(result)
                new_loop.close()
            except Exception as e:
                future.set_exception(e)
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join(timeout=300)  # 5分钟超时
        
        if thread.is_alive():
            raise TimeoutError("异步操作超时")
        
        return future.result()
    except RuntimeError:
        # 没有运行中的事件循环，尝试获取或创建事件循环
        try:
            loop = asyncio.get_event_loop()
            # 检测是否是 uvloop（通过模块名或类型名）
            loop_type_name = type(loop).__name__
            loop_module = type(loop).__module__
            is_uvloop = (
                "uvloop" in loop_type_name.lower() or 
                "uvloop" in loop_module.lower() or
                "uvloop" in str(type(loop)).lower()
            )
            
            if loop.is_running():
                if is_uvloop:
                    # uvloop 不支持 nest_asyncio，使用线程池运行
                    logger.debug(f"[_run_async_safe] Detected uvloop, using thread pool")
                    future = concurrent.futures.Future()
                    
                    def run_in_thread():
                        try:
                            # 在新线程中创建新的事件循环
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            result = new_loop.run_until_complete(coro)
                            future.set_result(result)
                            new_loop.close()
                        except Exception as e:
                            future.set_exception(e)
                    
                    thread = threading.Thread(target=run_in_thread, daemon=True)
                    thread.start()
                    thread.join(timeout=300)  # 5分钟超时
                    
                    if thread.is_alive():
                        raise TimeoutError("异步操作超时")
                    
                    return future.result()
                else:
                    # 标准 asyncio，使用 nest_asyncio
                    try:
                        import nest_asyncio
                        nest_asyncio.apply()
                        return loop.run_until_complete(coro)
                    except Exception as e:
                        logger.warning(f"[_run_async_safe] nest_asyncio failed: {e}, using thread pool")
                        # 降级到线程池
                        future = concurrent.futures.Future()
                        
                        def run_in_thread():
                            try:
                                new_loop = asyncio.new_event_loop()
                                asyncio.set_event_loop(new_loop)
                                result = new_loop.run_until_complete(coro)
                                future.set_result(result)
                                new_loop.close()
                            except Exception as e:
                                future.set_exception(e)
                        
                        thread = threading.Thread(target=run_in_thread, daemon=True)
                        thread.start()
                        thread.join(timeout=300)
                        
                        if thread.is_alive():
                            raise TimeoutError("异步操作超时")
                        
                        return future.result()
            else:
                # 循环未运行，直接使用
                return loop.run_until_complete(coro)
        except RuntimeError:
            # 完全没有事件循环（例如在 ThreadPoolExecutor 线程中），在新线程中创建并运行
            logger.debug(f"[_run_async_safe] No event loop found, creating new thread with event loop")
            future = concurrent.futures.Future()
            
            def run_in_thread():
                try:
                    # 在新线程中创建新的事件循环
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    result = new_loop.run_until_complete(coro)
                    future.set_result(result)
                    new_loop.close()
                except Exception as e:
                    future.set_exception(e)
            
            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            thread.join(timeout=300)  # 5分钟超时
            
            if thread.is_alive():
                raise TimeoutError("异步操作超时")
            
            return future.result()


@tool("优化提示词工具")
def optimize_prompt_tool(user_prompt: str) -> str:
    """
    优化用户输入的提示词，将其转化为可拍摄的镜头脚本。
    
    Args:
        user_prompt: 用户输入的原始提示词
        
    Returns:
        优化后的提示词文本
    """
    import asyncio
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    if not (OPENROUTER_BASE and OPENROUTER_KEY):
        raise RuntimeError("未配置 OpenRouter（OPENROUTER_API_BASE / OPENROUTER_API_KEY）")
    
    or_client = OpenRouterClient(
        api_base=OPENROUTER_BASE,
        api_key=OPENROUTER_KEY,
        referer=EMBED_REFERER,
        title="SaleAgent"
    )
    
    sys_prompt = "你是资深广告导演，请将用户的营销文案优化为更清晰、可拍摄的镜头脚本，包含镜头顺序、画面主体、景别、转场与结尾 CTA，时长控制在 10 秒。尽量避免有人脸出现。仅输出优化后的文本。"
    
    async def _optimize():
        return await or_client.chat_completions(
        model=PROMPT_LLM_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=512,
    )
    
    return _run_async_safe(_optimize())


import logging
logger = logging.getLogger("crewai_tools")


async def plan_storyboard_impl(goal: str, styles: List[str], total_duration: float, num_clips: int) -> str:
    """依据目标/风格/时长生成分镜脚本草案，返回 JSON 字符串。异步版本。"""
    if not (OPENROUTER_BASE and OPENROUTER_KEY):
        raise RuntimeError("未配置 OpenRouter（OPENROUTER_API_BASE / OPENROUTER_API_KEY）")
    
    or_client = OpenRouterClient(
        api_base=OPENROUTER_BASE,
        api_key=OPENROUTER_KEY,
        referer=EMBED_REFERER,
        title="SaleAgent"
    )
    
    # 使用专门的分镜模型（默认 gpt-4o-mini，结构化输出更稳定）
    model = STORYBOARD_LLM_MODEL
    # 要求输出严格的 JSON 对象（包含 storyboards 数组），提供明确格式示例
    sys_prompt = (
        "你是资深广告导演。根据用户目标与风格，将整个视频拆分为完整的可拍摄的镜头，每个分镜头最长不超过10s。\n\n"
        "【重要】时长要求：\n"
        "1. 每个镜头的时长（end_s - begin_s）必须不超过10s\n"
        "2. 如果某个镜头内容需要超过10s，应该拆分为多个镜头\n"
        "3. 镜头数量可以根据需要灵活调整，但每个镜头时长不能超过10s\n\n"
        "【重要】输出格式要求（必须严格遵守）：\n"
        "1. 严格只输出 JSON 对象，不要任何额外文字、说明、Markdown 代码块或注释\n"
        "2. JSON 结构必须为：{\"storyboards\": [{\"idx\": 1, \"desc\": \"…\", \"begin_s\": 0.0, \"end_s\": 5.0}, {\"idx\": 2, \"desc\": \"…\", \"begin_s\": 5.0, \"end_s\": 10.0}, ...]}\n"
        "3. 必须包含 begin_s 和 end_s 字段，且 end_s - begin_s <= 10.0\n"
        "4. 禁止使用其他格式，如：\n"
        "   - 禁止使用 {\"镜头1\": {...}} 格式\n"
        "   - 禁止使用 {\"内容\": \"...\", \"表现\": \"...\"} 格式\n"
        "   - 禁止使用任何中文键名（如\"内容\"、\"描述\"、\"表现\"等）\n"
        "   - 必须使用 \"idx\", \"desc\", \"begin_s\", \"end_s\" 作为键名\n"
        "5. desc 为一句中文分镜描述（20-50字），不含编号/时间/标题/Markdown 符号\n"
        "6. 不要输出 keyframes，这些由后续工具生成\n\n"
        "【正确示例】（必须完全按照此格式）：\n"
        "{\n"
        "  \"storyboards\": [\n"
        "    {\"idx\": 1, \"desc\": \"特写iPhone 17摄像头模组，金属边框反射冷冽蓝光，背景纯黑，镜头缓慢推进\"},\n"
        "    {\"idx\": 2, \"desc\": \"中景悬浮的iPhone 17完整机身，钛金属边框流转霓虹光效，手机360度旋转展示\"}\n"
        "  ]\n"
        "}\n\n"
        "【错误示例】（禁止使用）：\n"
        "❌ {\"镜头1\": {\"内容\": \"...\", \"表现\": \"...\"}}\n"
        "❌ {\"内容\": \"...\", \"描述\": \"...\"}\n"
        "❌ 任何包含中文键名的格式\n\n"
        "请严格按照正确示例的格式返回，不要使用任何其他格式。"
    )
    user_prompt = (
        f"主体目标：{goal}\n"
        f"风格：{', '.join(styles) if styles else '通用'}\n"
        f"镜头数：{num_clips}（必须严格生成恰好 {num_clips} 个镜头，不能多也不能少）\n"
        f"总时长：{total_duration}s\n\n"
        f"请严格按上述 JSON 结构返回，storyboards 数组必须包含恰好 {num_clips} 个元素，不要包含示例之外的任何文字或 Markdown。\n"
        f"【重要】必须返回有效的 JSON 格式，使用 \"idx\" 和 \"desc\" 作为键名。\n"
        f"【关键】镜头数量必须严格等于 {num_clips}，不能多也不能少。如果生成的数量不对，会被拒绝并要求重写。"
    )
    
    logger.info(f"[plan_storyboard_impl] Calling OpenRouter with model={model}, num_clips={num_clips}")
    logger.debug(f"[plan_storyboard_impl] System prompt: {sys_prompt[:500]}")
    logger.debug(f"[plan_storyboard_impl] User prompt: {user_prompt[:500]}")
    
    # 根据分镜数量动态计算 max_tokens：每个分镜约需 80 tokens（JSON格式+中文描述），基础开销 200 tokens
    calculated_max_tokens = min(3200, num_clips * 800 + 200)
    logger.info(f"[plan_storyboard_impl] Calculated max_tokens={calculated_max_tokens} for num_clips={num_clips}")
    
    try:
        # 尝试使用 JSON mode 或结构化输出（如果模型支持）
        # GPT-5-mini 支持结构化输出，可以使用 JSON Schema 强制格式
        response_format = None
        if "gpt-5" in model or "gpt-5-mini" in model:
            # 使用 JSON Schema 强制特定的数据结构
            # 注意：如果模型不支持 strict schema，可能会失败，需要降级处理
            try:
                response_format = {
                    "type": "json_object",
                    "json_schema": {
                        "name": "storyboard_schema",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {
                                "storyboards": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "idx": {
                                                "type": "integer",
                                                "description": "镜头序号，从1开始"
                                            },
                                            "desc": {
                                                "type": "string",
                                                "description": "分镜描述，20-50字的中文描述"
                                            }
                                        },
                                        "required": ["idx", "desc"],
                                        "additionalProperties": False
                                    },
                                    "minItems": num_clips,
                                    "maxItems": num_clips
                                }
                            },
                            "required": ["storyboards"],
                            "additionalProperties": False
                        }
                    }
                }
                logger.info(f"[plan_storyboard_impl] Using structured outputs with JSON schema for model {model}")
            except Exception as schema_error:
                logger.warning(f"[plan_storyboard_impl] Failed to create JSON schema, falling back to simple JSON mode: {schema_error}")
                response_format = {"type": "json_object"}
        elif "claude" in model.lower():
            # Claude 模型使用简单的 JSON mode
            response_format = {"type": "json_object"}
            logger.info(f"[plan_storyboard_impl] Using JSON mode for model {model}")
        
        outline = await or_client.chat_completions(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.4,
            max_tokens=calculated_max_tokens,
            response_format=response_format,
        )
        logger.info(f"[plan_storyboard_impl] OpenRouter returned outline (type={type(outline)}, len={len(outline) if isinstance(outline, str) else 'N/A'})")
    except OpenRouterError as e:
        # OpenRouter 特定错误（HTTP 错误、API 错误等）
        error_msg = f"OpenRouter API 错误: {str(e)}"
        logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
        # 抛出异常，让 CrewAI 知道调用失败，而不是返回空字符串
        raise RuntimeError(f"分镜生成失败：{error_msg}。请检查 OpenRouter API 配置和网络连接。")
    except httpx.TimeoutException as e:
        # 网络超时
        error_msg = f"OpenRouter API 请求超时: {str(e)}"
        logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
        raise RuntimeError(f"分镜生成失败：{error_msg}。请稍后重试。")
    except httpx.RequestError as e:
        # 网络请求错误
        error_msg = f"OpenRouter API 网络错误: {str(e)}"
        logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
        raise RuntimeError(f"分镜生成失败：{error_msg}。请检查网络连接。")
    except Exception as e:
        # 其他未知错误
        error_msg = f"分镜生成时发生未知错误: {str(e)}"
        logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
        raise RuntimeError(f"分镜生成失败：{error_msg}。")
    
    if not outline or not isinstance(outline, str) or len(outline.strip()) == 0:
        logger.warning(f"[plan_storyboard_impl] empty outline from OpenRouter (outline={repr(outline)}), trying quick retry without structured output")
        try:
            # 重试时不使用结构化输出，降低失败概率
            outline = await or_client.chat_completions(
                model=model,
                messages=[{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=calculated_max_tokens,
                response_format=None,  # 不使用结构化输出，让模型自由输出
            )
            if not outline or not isinstance(outline, str) or len(outline.strip()) == 0:
                raise RuntimeError("OpenRouter 返回空内容，即使重试后仍为空")
        except Exception as e:
            error_msg = f"重试失败: {str(e)}"
            logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
            raise RuntimeError(f"分镜生成失败：{error_msg}。")
    try:
        logger.info(f"[plan_storyboard_impl] raw_outline(len={len(outline) if isinstance(outline,str) else -1}): {outline[:1000] if isinstance(outline,str) else outline}")
    except Exception:
        pass
    
    # 解析输出为镜头描述列表：优先 JSON 对象->storyboards；其次 JSON 数组；最后清洗行文本
    import json, re
    lines: List[str] = []
    
    def _extract_lines(text: str) -> List[str]:
        """从文本中提取分镜描述列表。"""
        # 1. 代码围栏
        fenced = re.findall(r"```(?:json)?\\s*([\\s\\S]*?)```", text, flags=re.IGNORECASE)
        t = fenced[0] if fenced else text.strip()
        # 2. 尝试对象 storyboards
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and isinstance(obj.get("storyboards"), list):
                arr = obj["storyboards"]
                out: List[str] = []
                for it in arr:
                    if isinstance(it, dict) and "desc" in it:
                        desc = str(it["desc"]).strip().strip('\"').strip("'")
                        if desc:
                            out.append(desc)
                if out:
                    return out
        except Exception:
            pass
        # 2.5. 尝试处理 {"镜头1": {"内容": "...", "表现": "..."}} 格式
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                out: List[str] = []
                # 按键名排序，确保顺序正确
                sorted_keys = sorted([k for k in obj.keys() if "镜头" in str(k) or k.isdigit()], 
                                   key=lambda x: int(re.search(r'\\d+', str(x)).group()) if re.search(r'\\d+', str(x)) else 999)
                for key in sorted_keys:
                    item = obj[key]
                    if isinstance(item, dict):
                        # 优先使用 "内容"，其次 "desc"，最后 "描述"
                        desc = item.get("内容") or item.get("desc") or item.get("描述") or ""
                        if desc:
                            out.append(str(desc).strip().strip('\"').strip("'"))
                if out:
                    logger.info(f"[plan_storyboard_impl] Extracted {len(out)} descriptions from alternative format")
                    return out
        except Exception:
            pass
        # 3. 尝试数组
        try:
            l = t.find("["); r = t.rfind("]")
            if l != -1 and r != -1 and r > l:
                cand = t[l:r+1]
                arr = json.loads(cand)
                if isinstance(arr, list) and len(arr) > 0:
                    return [str(x).strip().strip('\"').strip("'") for x in arr if str(x).strip()]
        except Exception:
            pass
        # 4. 逐行兜底
        raw_lines = [ln for ln in t.splitlines() if ln.strip()]
        cleaned: List[str] = []
        for ln in raw_lines:
            s = ln.strip()
            if s in {"```", "```json", "[", "]", ",", "[,", "],"}: continue
            s = s.lstrip("#").strip().strip("* ")
            if s.endswith(","): s = s[:-1].strip()
            if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
                s = s[1:-1].strip()
            s = s.lstrip("・-• 　").strip()
            if re.match(r"^[*]*\\s*镜头\\s*\\d+", s):
                continue
            if s: cleaned.append(s)
        return cleaned
    
    def _is_valid(desc: str) -> bool:
        if not desc or len(desc) < 8: return False
        bad_tokens = ["镜头描述", "请填写", "占位", "placeholder"]
        return not any(bt in desc for bt in bad_tokens)
    
    lines = _extract_lines(outline)
    try:
        logger.info(f"[plan_storyboard_impl] parsed_lines(n={len(lines)}): {lines}")
    except Exception:
        pass
    
    # 校验：若为空或包含占位，自动重试一次，要求返回有效 JSON 对象
    needs_retry = (len(lines) < num_clips) or any(not _is_valid(s) for s in lines)
    if needs_retry:
        repair_system = (
            "你是严格的格式化助手。仅输出 JSON 对象，不要任何说明或 Markdown。\n"
            "JSON 结构：{\"storyboards\": [{\"idx\": 1, \"desc\": \"…\"}, ...]}。\n"
            "desc 必须是具体镜头画面描述，禁止出现‘镜头描述/占位/示例’等空泛内容。"
        )
        repair_user = (
            f"请为 {num_clips} 个镜头生成中文分镜描述（每句20-50字）。\n"
            f"主体目标：{goal}\n风格：{', '.join(styles) if styles else '通用'}\n总时长：{total_duration}s\n\n"
            "返回示例：{\"storyboards\": [{\"idx\":1,\"desc\":\"…\"},{\"idx\":2,\"desc\":\"…\"}]}"
        )
        try:
            repair = await or_client.chat_completions(
                model=model,
                messages=[{"role": "system", "content": repair_system}, {"role": "user", "content": repair_user}],
                temperature=0.3,
                max_tokens=calculated_max_tokens,
            )
            lines2 = _extract_lines(repair)
            if lines2:
                lines = lines2
                logger.info(f"[plan_storyboard_impl] retry_lines(n={len(lines)}): {lines}")
        except Exception as e:
            logger.warning(f"[plan_storyboard_impl] retry failed: {e}")

    # 清理空字符串和无效描述
    lines = [line.strip() for line in lines if line and line.strip() and len(line.strip()) >= 3]

    if len(lines) < num_clips:
        # 如果仍然不足，使用更具体的占位符，但标记为需要修复
        missing = num_clips - len(lines)
        logger.warning(f"[plan_storyboard_impl] Only got {len(lines)} valid descriptions, need {num_clips}, adding {missing} placeholders")
        # 使用更具体的占位符，避免被误判为有效
        for i in range(missing):
            lines.append(f"镜头{len(lines) + 1}描述待生成")
    # 不在这里截断，让审核工具检查数量并触发重写
    if len(lines) > num_clips:
        logger.warning(f"[plan_storyboard_impl] Got {len(lines)} descriptions, but only need {num_clips}, will be checked by review tool")
    
    # 计算时间分段，确保每个镜头不超过10s
    def _split_with_max_duration(total: float, max_duration: float = 10.0):
        """将总时长分割为多个镜头，每个镜头最长不超过 max_duration"""
        res = []
        t = 0.0
        idx = 1
        while t < total:
            start = round(t, 1)
            # 每个镜头最多10s，但如果剩余时间不足10s，则使用剩余时间
            duration = min(max_duration, round(total - t, 1))
            end = round(start + duration, 1)
            res.append((start, end))
            t = end
            idx += 1
        return res
    
    spans = _split_with_max_duration(total_duration, max_duration=10.0)
    # 更新 num_clips 为实际生成的镜头数（可能比用户指定的多，因为每个镜头最多10s）
    actual_num_clips = len(spans)
    if actual_num_clips != num_clips:
        logger.info(
            f"[plan_storyboard_impl] Adjusted num_clips from {num_clips} to {actual_num_clips} "
            f"to ensure each clip <= 10s (total_duration={total_duration}s)"
        )
    
    # 构建 JSON 格式的分镜脚本
    import json
    storyboards = []
    for i, (beg, end) in enumerate(spans):
        desc = lines[i] if i < len(lines) else f"镜头{i + 1}描述待生成"
        # 确保 desc 不为空且长度足够
        desc = desc.strip() if desc else f"镜头{i + 1}描述待生成"
        if not desc or len(desc) < 3:
            desc = f"镜头{i + 1}描述待生成"
        
        storyboards.append({
            "idx": i + 1,
            "desc": desc,
            "begin_s": beg,
            "end_s": end,
            "keyframes": {"in": None, "out": None}
        })
    
    return json.dumps(storyboards, ensure_ascii=False)


@tool("审核分镜脚本工具")
def review_storyboard_tool(storyboards_json: str, num_clips: int, goal: str, styles: List[str], total_duration: float = 10.0, max_retries: int = 3) -> str:
    """
    审核分镜脚本质量，确保每个镜头都有详细、具体的描述，且每个镜头时长不超过10s。
    
    Args:
        storyboards_json: JSON 格式的分镜脚本列表
        num_clips: 期望的镜头数量（仅供参考，实际数量可能因时长限制而不同）
        goal: 目标
        styles: 风格列表
        total_duration: 总时长（秒）
        max_retries: 最大重试次数
        
    Returns:
        审核通过的分镜脚本（JSON 格式）
    """
    """
    审核分镜脚本的质量，检查是否有效。
    
    如果分镜脚本无效（包含空描述、过短描述、占位符等），
    自动触发重写，最多重试 max_retries 次，直到生成有效的分镜脚本。
    
    Args:
        storyboards_json: JSON 格式的分镜脚本列表
        num_clips: 期望的镜头数量
        goal: 主体目标
        styles: 风格列表
        max_retries: 最大重试次数（默认3次）
        
    Returns:
        如果有效：返回审核通过的分镜脚本 JSON
        如果重试后仍无效：返回包含错误信息的 JSON
    """
    import json
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    # 解析并验证分镜脚本
    from typing import Tuple
    def validate_storyboards(sb_json: str) -> Tuple[bool, list, int]:
        """验证分镜脚本，返回 (是否有效, 错误列表, 有效数量)"""
        try:
            storyboards = json.loads(sb_json)
        except Exception as e:
            return False, [f"JSON 解析失败: {e}"], 0
        
        if not isinstance(storyboards, list):
            return False, ["分镜脚本必须是数组格式"], 0
        
        errors = []
        valid_count = 0
        
        # 检查每个分镜
        for i, sb in enumerate(storyboards):
            if not isinstance(sb, dict):
                errors.append(f"镜头 {i + 1}: 不是有效的对象格式")
                continue
            
            desc = str(sb.get("desc", "")).strip()
            idx = sb.get("idx", i + 1)
            begin_s = float(sb.get("begin_s", 0))
            end_s = float(sb.get("end_s", 0))
            duration = end_s - begin_s
            
            # 检查描述是否为空
            if not desc:
                errors.append(f"镜头 {idx}: 描述为空")
                continue
            
            # 检查描述是否过短（至少 8 个字符）
            if len(desc) < 8:
                errors.append(f"镜头 {idx}: 描述过短（长度: {len(desc)}），至少需要 8 个字符")
                continue
            
            # 检查是否包含占位符
            placeholder_keywords = ["镜头描述", "描述待生成", "请填写", "占位", "placeholder", "待生成"]
            if any(keyword in desc for keyword in placeholder_keywords):
                errors.append(f"镜头 {idx}: 包含占位符（'{desc[:30]}...'）")
                continue
            
            # 检查时长是否超过10s
            if duration > 10.0:
                errors.append(f"镜头 {idx}: 时长超过10s（{duration:.1f}s），每个镜头最长不超过10s")
                continue
            
            valid_count += 1
        
        # 检查数量（不再严格限制，因为镜头数量会根据时长自动调整）
        # 但确保至少有一个有效镜头
        if len(storyboards) == 0:
            errors.append("镜头数量为0，至少需要1个镜头")
        
        # 检查总时长是否匹配
        total_actual_duration = sum([float(sb.get("end_s", 0)) - float(sb.get("begin_s", 0)) for sb in storyboards])
        if abs(total_actual_duration - total_duration) > 1.0:  # 允许1秒误差
            errors.append(f"总时长不匹配：期望 {total_duration}s，实际 {total_actual_duration:.1f}s")
        
        # 必须所有镜头都有效（不再要求数量完全匹配）
        is_valid = len(errors) == 0 and valid_count == len(storyboards) and len(storyboards) > 0
        return is_valid, errors, valid_count
    
    # 首次验证
    is_valid, errors, valid_count = validate_storyboards(storyboards_json)
    
    if is_valid:
        logger.info(f"[review_storyboard_tool] Storyboard validation passed: {valid_count} valid clips")
        return storyboards_json
    
    # 如果无效，尝试自动重写
    error_summary = "; ".join(errors[:5])
    if len(errors) > 5:
        error_summary += f" ... 还有 {len(errors) - 5} 个错误"
    
    logger.warning(f"[review_storyboard_tool] Storyboard validation failed: {error_summary}, attempting rewrite...")
    
    # 自动重写逻辑
    for retry in range(max_retries):
        try:
            # 调用 plan_storyboard_impl 重新生成
            retry_result = _run_async_safe(plan_storyboard_impl(goal, styles, total_duration, num_clips))
            
            # 验证重写结果
            is_valid, new_errors, new_valid_count = validate_storyboards(retry_result)
            
            if is_valid:
                logger.info(f"[review_storyboard_tool] Rewrite successful after {retry + 1} attempt(s)")
                return retry_result
            else:
                new_error_summary = "; ".join(new_errors[:3])
                logger.warning(f"[review_storyboard_tool] Rewrite attempt {retry + 1} still invalid: {new_error_summary}")
        except Exception as e:
            logger.error(f"[review_storyboard_tool] Rewrite attempt {retry + 1} failed: {e}", exc_info=True)
    
    # 所有重试都失败，返回错误信息
    logger.error(f"[review_storyboard_tool] All {max_retries} rewrite attempts failed")
    return json.dumps({
        "valid": False,
        "errors": errors,
        "valid_count": valid_count,
        "expected_count": num_clips,
        "retry_attempts": max_retries,
        "message": f"分镜脚本审核未通过，已重试 {max_retries} 次仍无效：{error_summary}。请检查分镜生成工具或调整参数。"
    }, ensure_ascii=False)


@tool("规划分镜脚本工具")
def plan_storyboard_tool(goal: str, styles: List[str], total_duration: float, num_clips: int) -> str:
    """CrewAI Tool 封装：转调实现函数。"""
    return _run_async_safe(plan_storyboard_impl(goal, styles, total_duration, num_clips))


@tool("生成关键帧工具")
def generate_keyframe_tool(storyboards_json: str, image_control: bool = True) -> str:
    """
    为分镜脚本生成关键帧图片（首帧/尾帧）。
    
    Args:
        storyboards_json: JSON 格式的分镜脚本列表
        image_control: 是否启用图片控制
        
    Returns:
        JSON 格式的更新后的分镜脚本（包含关键帧 URL）
    """
    import json
    import asyncio
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    if not image_control:
        return storyboards_json
    
    storyboards = json.loads(storyboards_json)
    image_provider = _get_image_provider()
    
    async def generate_keyframes():
        updated_storyboards = []
        for sb in storyboards:
            desc = sb.get("desc", "")
            # 为每个分镜生成首帧和尾帧
            try:
                in_frame_url = await image_provider.generate(f"{desc}，首帧画面")
                out_frame_url = await image_provider.generate(f"{desc}，尾帧画面")
                sb["keyframes"] = {"in": in_frame_url, "out": out_frame_url}
            except Exception as e:
                logger.warning(f"[generate_keyframe_tool] Failed to generate keyframes for clip {sb.get('idx', 'unknown')}: {e}")
                # 失败时保留原有 keyframes
                sb["keyframes"] = sb.get("keyframes", {"in": None, "out": None})
            updated_storyboards.append(sb)
        return json.dumps(updated_storyboards, ensure_ascii=False)
    
    return _run_async_safe(generate_keyframes())


@tool("合并镜头为视频任务工具")
def merge_storyboards_to_video_tasks_tool(storyboards_json: str, run_id: str, total_duration: float) -> str:
    """
    将分镜脚本按时间顺序合并为10s的视频任务（为了节约成本）。
    
    规则：
    1. 按时间顺序（begin_s）处理镜头
    2. 将不足10s的镜头合并，直到累计时长达到10s
    3. 每个视频任务最多10s，如果单个镜头超过10s，则单独成为一个任务
    4. 保留 clips 数组，不合并描述，以便后续进行细节控制
    
    Args:
        storyboards_json: JSON 格式的分镜脚本列表
        run_id: 运行 ID
        total_duration: 总时长（秒）
        
    Returns:
        JSON 格式的视频任务列表（每个任务包含 clips 数组，每个任务对应一个10s的视频片段）
    """
    import json
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    try:
        storyboards_raw = json.loads(storyboards_json)
    except Exception as e:
        logger.error(f"[merge_storyboards_to_video_tasks_tool] Failed to parse JSON: {e}")
        return json.dumps([], ensure_ascii=False)
    
    # 处理不同的输入格式
    if isinstance(storyboards_raw, dict):
        storyboards = []
        import re
        sorted_keys = sorted(
            [k for k in storyboards_raw.keys() if "镜头" in str(k) or k.isdigit()], 
            key=lambda x: int(re.search(r'\d+', str(x)).group()) if re.search(r'\d+', str(x)) else 999
        )
        for idx, key in enumerate(sorted_keys, 1):
            item = storyboards_raw[key]
            if isinstance(item, dict):
                desc = item.get("内容") or item.get("desc") or item.get("描述") or ""
                storyboards.append({
                    "idx": idx,
                    "desc": str(desc).strip(),
                    "begin_s": item.get("begin_s", 0),
                    "end_s": item.get("end_s", 0),
                    "keyframes": item.get("keyframes", {"in": None, "out": None})
                })
    elif isinstance(storyboards_raw, list):
        storyboards = storyboards_raw
    else:
        logger.error(f"[merge_storyboards_to_video_tasks_tool] Invalid format: {type(storyboards_raw)}")
        return json.dumps([], ensure_ascii=False)
    
    # 按 begin_s 排序
    storyboards.sort(key=lambda x: float(x.get("begin_s", 0)))
    
    # 合并镜头为10s的视频任务
    video_tasks = []
    current_task = {
        "task_idx": 0,
        "clips": [],
        "total_duration": 0.0,
        "begin_s": 0.0,
        "end_s": 0.0
    }
    
    TARGET_DURATION = 10.0  # 每个视频任务的目标时长
    
    for sb in storyboards:
        begin_s = float(sb.get("begin_s", 0))
        end_s = float(sb.get("end_s", 0))
        duration = max(0.1, end_s - begin_s)
        desc = str(sb.get("desc", "")).strip() or str(sb.get("内容", "")).strip() or str(sb.get("描述", "")).strip()
        
        if not desc or len(desc) < 3:
            logger.warning(f"[merge_storyboards_to_video_tasks_tool] Skipping empty clip at {begin_s}-{end_s}s")
            continue
        
        # 如果单个镜头超过10s，单独成为一个任务
        if duration >= TARGET_DURATION:
            # 先保存当前任务（如果有）
            if current_task["clips"]:
                video_tasks.append({
                    "task_idx": len(video_tasks) + 1,
                    "clips": current_task["clips"].copy(),
                    "total_duration": current_task["total_duration"],
                    "begin_s": current_task["begin_s"],
                    "end_s": current_task["end_s"],
                    "keyframes": current_task["clips"][0].get("keyframes", {"in": None, "out": None})
                })
                current_task = {
                    "task_idx": 0,
                    "clips": [],
                    "total_duration": 0.0,
                    "begin_s": 0.0,
                    "end_s": 0.0
                }
            
            # 这个长镜头单独成为一个任务
            video_tasks.append({
                "task_idx": len(video_tasks) + 1,
                "clips": [sb],
                "total_duration": duration,
                "begin_s": begin_s,
                "end_s": end_s,
                "keyframes": sb.get("keyframes", {"in": None, "out": None})
            })
        else:
            # 尝试添加到当前任务
            new_total = current_task["total_duration"] + duration
            
            if new_total <= TARGET_DURATION:
                # 可以添加到当前任务
                if not current_task["clips"]:
                    current_task["begin_s"] = begin_s
                current_task["clips"].append(sb)
                current_task["total_duration"] = new_total
                current_task["end_s"] = end_s
            else:
                # 当前任务已满，先保存
                if current_task["clips"]:
                    video_tasks.append({
                        "task_idx": len(video_tasks) + 1,
                        "clips": current_task["clips"].copy(),
                        "total_duration": current_task["total_duration"],
                        "begin_s": current_task["begin_s"],
                        "end_s": current_task["end_s"],
                        "keyframes": current_task["clips"][0].get("keyframes", {"in": None, "out": None})
                    })
                
                # 开始新任务
                current_task = {
                    "task_idx": 0,
                    "clips": [sb],
                    "total_duration": duration,
                    "begin_s": begin_s,
                    "end_s": end_s
                }
    
    # 保存最后一个任务（如果有）
    if current_task["clips"]:
        video_tasks.append({
            "task_idx": len(video_tasks) + 1,
            "clips": current_task["clips"].copy(),
            "total_duration": current_task["total_duration"],
            "begin_s": current_task["begin_s"],
            "end_s": current_task["end_s"],
            "keyframes": current_task["clips"][0].get("keyframes", {"in": None, "out": None})
        })
    
    logger.info(
        f"[merge_storyboards_to_video_tasks_tool] Merged {len(storyboards)} clips into {len(video_tasks)} video tasks "
        f"(target: {total_duration}s, actual: {sum(t['total_duration'] for t in video_tasks):.1f}s)"
    )
    
    return json.dumps(video_tasks, ensure_ascii=False)


@tool("生成视频片段工具")
def generate_video_clip_tool(video_tasks_json: str, run_id: str) -> str:
    """
    为视频任务提交视频生成任务（异步模式，避免长时间阻塞）。
    
    注意：视频生成需要 3-5 分钟，此工具只负责提交任务到数据库，
    实际生成由后台任务或 webhook 完成。返回任务提交状态。
    
    Args:
        video_tasks_json: JSON 格式的视频任务列表（由合并工具生成，每个任务对应一个10s的视频片段）
        run_id: 运行 ID，用于文件命名
        
    Returns:
        JSON 格式的任务提交结果列表（包含 task_id，状态为 "pending"）
    """
    import json
    import asyncio
    import logging
    from datetime import datetime
    
    logger = logging.getLogger("crewai_tools")
    video_tasks_raw = json.loads(video_tasks_json)
    video_provider = _get_video_provider()
    
    # 处理输入格式
    if isinstance(video_tasks_raw, list):
        video_tasks = video_tasks_raw
    else:
        logger.error(f"[generate_video_clip_tool] Invalid video_tasks format: {type(video_tasks_raw)}")
        video_tasks = []
    
    async def submit_one(task: Dict[str, Any], index: int) -> Dict[str, Any]:
        """提交单个视频生成任务，带重试机制"""
        # 获取任务索引（task_idx 是视频任务的序号，不是镜头序号）
        task_idx = task.get("task_idx") or (index + 1)
        if isinstance(task_idx, str):
            try:
                task_idx = int(task_idx)
            except (ValueError, TypeError):
                task_idx = index + 1
        
        # 获取 clips 数组（保留所有镜头信息，不合并）
        clips = task.get("clips", [])
        if not clips:
            # 降级：如果没有 clips，尝试使用 desc
            desc = str(task.get("desc", "")).strip()
            if desc:
                clips = [{"desc": desc, "begin_s": task.get("begin_s", 0), "end_s": task.get("end_s", 10)}]
            else:
                error_msg = f"视频任务 {task_idx} 没有 clips 或 desc，无法生成视频"
                logger.error(f"[generate_video_clip_tool] {error_msg}")
                return {
                    "task_idx": task_idx,
                    "status": "failed",
                    "video_url": None,
                    "error": error_msg
                }
        
        # 将 clips 转换为结构化的 prompt（按时间顺序，包含每个镜头的描述和时长）
        # 格式：每个镜头一行，包含时间范围和描述
        prompt_parts = []
        for clip in clips:
            clip_desc = str(clip.get("desc", "")).strip() or str(clip.get("内容", "")).strip() or str(clip.get("描述", "")).strip()
            if not clip_desc:
                continue
            begin_s = float(clip.get("begin_s", 0))
            end_s = float(clip.get("end_s", 0))
            clip_duration = end_s - begin_s
            # 格式：时间范围 + 描述
            prompt_parts.append(f"[{begin_s:.1f}s-{end_s:.1f}s, {clip_duration:.1f}s] {clip_desc}")
        
        # 组合成完整的 prompt
        prompt = "\n".join(prompt_parts) if prompt_parts else ""
        
        # 如果没有有效的 prompt，尝试使用第一个 clip 的描述
        if not prompt or len(prompt) < 3:
            first_clip = clips[0] if clips else {}
            prompt = str(first_clip.get("desc", "")).strip() or str(first_clip.get("内容", "")).strip() or str(first_clip.get("描述", "")).strip()
        
        # 获取时长（应该是10s或更少）
        duration = max(1, min(10, int(round(task.get("total_duration", 10.0)))))
        
        # 获取关键帧（使用第一个镜头的关键帧）
        first_clip = clips[0] if clips else {}
        keyframes = first_clip.get("keyframes", task.get("keyframes", {}))
        ref_img = keyframes.get("in") if keyframes else None
        
        # 验证 prompt 是否为空或包含占位符
        if not prompt or len(prompt) < 3:
            error_msg = f"视频任务描述为空或过短（长度: {len(prompt)}），无法生成视频。原始数据: {task}"
            logger.error(f"[generate_video_clip_tool] {error_msg} for task {task_idx}")
            return {
                "task_idx": task_idx,
                "status": "failed",
                "video_url": None,
                "error": error_msg
            }
        
        # 检查是否是占位符（避免提交无效任务）
        placeholder_keywords = ["镜头描述", "描述待生成", "请填写", "占位", "placeholder"]
        if any(keyword in prompt for keyword in placeholder_keywords):
            error_msg = f"视频任务描述包含占位符（'{prompt}'），无法生成视频。"
            logger.warning(f"[generate_video_clip_tool] {error_msg} for task {task_idx}")
            return {
                "task_idx": task_idx,
                "status": "failed",
                "video_url": None,
                "error": error_msg
            }
        
        # 优先使用 Supabase 队列（如果可用）
        if _supabase_queue_available and get_supabase_queue:
            queue = get_supabase_queue()
            if queue:
                try:
                    # 添加到 Supabase 队列，由后台 worker 处理
                    task_info = await queue.add_task(
                        run_id=run_id,
                        clip_idx=task_idx,  # 使用 task_idx 而不是 clip_idx
                        prompt=prompt,
                        ref_img=ref_img,
                        duration=duration,
                        retry_count=0
                    )
                    logger.info(
                        f"[generate_video_clip_tool] Task added to Supabase queue: "
                        f"run_id={run_id}, task_idx={task_idx}, task_id={task_info.get('id')}"
                    )
                    return {
                        "task_idx": task_idx,
                        "status": "submitted",  # 返回 submitted，表示已成功提交到队列
                        "task_id": task_info.get("id"),  # Supabase task ID
                        "video_url": None,
                        "queue_type": "supabase"
                    }
                except Exception as e:
                    logger.warning(
                        f"[generate_video_clip_tool] Failed to add to Supabase queue: {e}, "
                        f"falling back to direct submission"
                    )
                    # 降级到直接提交
        
        # 降级方案：直接提交（原有逻辑）
        # 重试配置：对于队列满的情况，最多重试1次（减少重试次数，避免重复提交和成本浪费）
        max_retries = 1
        retry_delays = [10]  # 秒，增加等待时间，减少重试频率
        
        for attempt in range(max_retries + 1):
            try:
                # 提交视频生成任务（使用异步模式，避免长时间阻塞）
                # 注意：由于视频生成需要 3-5 分钟，使用异步模式可以立即返回，避免阻塞 CrewAI
                res = await video_provider.generate(prompt, ref_img or "", duration=duration, async_mode=True)
                
                if isinstance(res, dict) and res.get("pending"):
                    # 异步任务模式：返回 task_id，等待 webhook 回调或后续轮询
                    task_id_runninghub = res.get("task_id")
                    logger.info(f"[generate_video_clip_tool] Submitted async task for task {task_idx}: task_id={task_id_runninghub}, prompt_length={len(prompt)}")
                    
                    # 即使使用 direct 提交，也保存到 video_tasks 表，以便 webhook 和 worker 能处理
                    try:
                        import os
                        supabase_url = os.getenv("SUPABASE_URL")
                        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
                        if supabase_url and supabase_key:
                            from supabase import create_client
                            supabase = create_client(supabase_url, supabase_key)
                            
                            # 保存任务到 video_tasks 表
                            task_data = {
                                "run_id": run_id,
                                "clip_idx": task_idx,  # 使用 task_idx
                                "prompt": prompt,
                                "ref_img": ref_img or "",
                                "duration": duration,
                                "status": "submitted",  # 直接设为 submitted，因为已经提交到 RunningHub
                                "provider_task_id": task_id_runninghub,
                                "retry_count": 0,
                                "created_at": datetime.utcnow().isoformat(),
                                "updated_at": datetime.utcnow().isoformat()
                            }
                            
                            result = supabase.table("video_tasks").insert(task_data).execute()
                            task_id_supabase = result.data[0].get("id") if result.data else None
                            
                            if not task_id_supabase:
                                logger.error(
                                    f"[generate_video_clip_tool] Failed to save task to database: "
                                    f"run_id={run_id}, task_idx={task_idx}, runninghub_task_id={task_id_runninghub}"
                                )
                                raise Exception("Failed to save task to database")
                            
                            logger.info(
                                f"[generate_video_clip_tool] Saved direct submission to video_tasks: "
                                f"run_id={run_id}, task_idx={task_idx}, "
                                f"supabase_task_id={task_id_supabase}, runninghub_task_id={task_id_runninghub}, "
                                f"status=submitted"
                            )
                            
                            return {
                                "task_idx": task_idx,
                                "status": "submitted",  # 返回 submitted，表示已成功提交到 RunningHub
                                "task_id": task_id_supabase,  # 返回 Supabase task ID
                                "video_url": None,
                                "queue_type": "direct"  # 标记为 direct，但已保存到数据库
                            }
                    except Exception as e:
                        logger.warning(
                            f"[generate_video_clip_tool] Failed to save direct submission to database: {e}, "
                            f"falling back to in-memory tracking"
                        )
                    
                    # 降级：如果无法保存到数据库，返回 task_id（RunningHub 的 task_id）
                    return {
                        "task_idx": task_idx,
                        "status": "submitted",  # 返回 submitted，表示已成功提交到 RunningHub
                        "task_id": task_id_runninghub,  # 使用 RunningHub task_id
                        "video_url": None,
                        "queue_type": "direct"
                    }
                else:
                    # 同步模式：直接返回结果（如果 provider 支持）
                    url = res.get("video_url") if isinstance(res, dict) else str(res)
                    cdn_url = await upload_url_to_r2(url, f"{run_id}_task{task_idx}.mp4")
                    logger.info(f"[generate_video_clip_tool] Generated task {task_idx} synchronously: {cdn_url}")
                    return {
                        "task_idx": task_idx,
                        "status": "succeeded",
                        "video_url": cdn_url,
                        "queue_type": "direct"
                    }
            except Exception as e:
                error_str = str(e)
                is_queue_full = "TASK_QUEUE_MAXED" in error_str or "421" in error_str or "队列" in error_str
                is_prompt_error = "Prompt must be" in error_str or "non-empty string" in error_str or "prompt" in error_str.lower()
                
                # 如果是 prompt 错误，直接返回失败，不重试
                if is_prompt_error:
                    error_msg = f"提示词错误: {error_str}。任务描述: '{prompt[:50]}...'"
                    logger.error(f"[generate_video_clip_tool] {error_msg} for task {task_idx}")
                    return {
                        "task_idx": task_idx,
                        "status": "failed",
                        "video_url": None,
                        "error": error_msg,
                        "retry_attempts": 0  # prompt 错误不重试
                    }
                
                # 如果是队列满且还有重试次数，等待后重试
                if is_queue_full and attempt < max_retries:
                    delay = retry_delays[min(attempt, len(retry_delays) - 1)]
                    logger.warning(
                        f"[generate_video_clip_tool] Task queue full for task {task_idx}, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # 其他错误或重试次数用完，返回失败
                    logger.error(
                        f"[generate_video_clip_tool] Failed to submit task for task {task_idx}: {e}",
                        exc_info=True
                    )
                    return {
                        "task_idx": task_idx,
                        "status": "failed",
                        "video_url": None,
                        "error": error_str,
                        "retry_attempts": attempt + 1
                    }
    
    # 使用任务队列管理器统一管理所有视频生成任务，避免队列溢出
    # 但为了简化，我们直接使用 Semaphore 控制并发，并在遇到队列满时加入队列
    # 降低并发数到 1，避免队列溢出
    sem = asyncio.Semaphore(1)  # 进一步降低到 1，避免队列溢出
    
    async def run_with_sem(sb, index):
        """带信号量控制的提交，避免并发过多导致队列溢出"""
        async with sem:
            return await submit_one(sb, index)
    
    async def submit_all():
        # 串行提交，避免队列溢出（虽然慢，但更稳定）
        # 在每个任务提交后等待一小段时间，避免队列瞬间满载
        # 确保只提交 video_tasks 中的任务，避免重复提交
        results = []
        submitted_indices = set()  # 跟踪已提交的 task_idx，避免重复
        
        logger.info(f"[generate_video_clip_tool] Starting to submit {len(video_tasks)} video tasks")
        
        for idx, task in enumerate(video_tasks):
            try:
                # 获取 task_idx，确保唯一性
                task_idx = task.get("task_idx") or (idx + 1)
                if isinstance(task_idx, str):
                    try:
                        task_idx = int(task_idx)
                    except (ValueError, TypeError):
                        task_idx = idx + 1
                
                # 检查是否已经提交过这个 task_idx
                if task_idx in submitted_indices:
                    logger.warning(f"[generate_video_clip_tool] Task idx {task_idx} already submitted, skipping duplicate")
                    continue
                
                submitted_indices.add(task_idx)
                
                result = await run_with_sem(task, idx)
                results.append(result)
                
                # 如果任务成功提交（pending），等待一小段时间再提交下一个，避免队列瞬间满载
                if result.get("status") == "pending":
                    logger.info(f"[generate_video_clip_tool] Task {result.get('task_idx', idx + 1)} submitted, waiting 2s before next...")
                    await asyncio.sleep(2)  # 等待2秒，给队列一些缓冲时间
                
                # 如果队列满，等待更长时间再继续
                if result.get("status") == "failed" and "TASK_QUEUE_MAXED" in str(result.get("error", "")):
                    logger.warning(f"[generate_video_clip_tool] Queue full for task {result.get('task_idx', idx + 1)}, waiting 10s before next submission...")
                    await asyncio.sleep(10)  # 队列满时等待更长时间
            except Exception as e:
                logger.error(f"[generate_video_clip_tool] Error submitting task {idx + 1}: {e}", exc_info=True)
                task_idx = task.get("task_idx") or (idx + 1)
                if isinstance(task_idx, str):
                    try:
                        task_idx = int(task_idx)
                    except (ValueError, TypeError):
                        task_idx = idx + 1
                
                # 检查是否已经提交过
                if task_idx not in submitted_indices:
                    submitted_indices.add(task_idx)
                    results.append({
                        "task_idx": task_idx,
                        "status": "failed",
                        "video_url": None,
                        "error": str(e)
                    })
        
        logger.info(f"[generate_video_clip_tool] Submitted {len(results)} video tasks (expected {len(video_tasks)})")
        return results
    
    results = _run_async_safe(submit_all())
    
    # 将任务信息保存到数据库（如果配置了 supabase）
    try:
        import os
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if supabase_url and supabase_key:
            from supabase import create_client
            supabase = create_client(supabase_url, supabase_key)
            
            # 更新 jobs 表，记录任务状态
            task_ids = [r.get("task_id") for r in results if r.get("status") in ["pending", "submitted"] and r.get("task_id")]
            if task_ids:
                supabase.table("jobs").update({
                    "status": "processing",
                    "provider_task_id": ",".join(task_ids),  # 多个任务ID用逗号分隔
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("run_id", run_id).execute()
                logger.info(f"[generate_video_clip_tool] Saved task_ids to database: {task_ids}")
    except Exception as e:
        logger.warning(f"[generate_video_clip_tool] Failed to save to database: {e}")
    
    return json.dumps(results, ensure_ascii=False)


@tool("拼接视频工具")
def stitch_video_tool(clip_results_json: str, run_id: str) -> str:
    """
    将多个视频片段拼接为最终视频。
    
    【重要】此工具的行为：
    1. 如果视频片段状态为 "pending" 或 "submitted"，说明任务还在处理中，此时无法拼接。
    2. 工具会抛出 RuntimeError 异常，明确说明任务还在处理中。
    3. 系统会注册回调，当所有任务完成时自动触发拼接。
    4. 只有在所有视频片段状态为 "succeeded" 时，才会返回最终视频的 CDN URL。
    5. 如果工具抛出异常，调用者必须如实返回异常信息，不要自己生成或猜测 URL。
    
    Args:
        clip_results_json: JSON 格式的视频片段结果列表
        run_id: 运行 ID，用于文件命名
        
    Returns:
        最终视频的 CDN URL（仅当所有视频片段完成时）
        
    Raises:
        RuntimeError: 如果视频片段还在处理中（pending/submitted），会抛出异常，说明无法拼接。
                     调用者必须等待所有视频片段完成后再调用此工具，不要自己生成 URL。
    """
    import json
    import asyncio
    import tempfile
    import subprocess
    import httpx
    import logging
    import os
    
    logger = logging.getLogger("crewai_tools")
    clip_results = json.loads(clip_results_json)
    
    # 检查是否有 pending 或 submitted 状态的任务，如果有，尝试轮询获取结果
    pending_tasks = [r for r in clip_results if r.get("status") in ["pending", "submitted"]]
    if pending_tasks:
        task_ids = [r.get("task_id") for r in pending_tasks if r.get("task_id")]
        logger.info(f"[stitch_video_tool] Found {len(pending_tasks)} pending tasks: {task_ids}, attempting to poll for results...")
        
        async def poll_pending_task(result: dict) -> dict:
            """轮询单个 pending 任务，获取结果"""
            task_id = result.get("task_id")
            queue_type = result.get("queue_type", "direct")
            
            if not task_id:
                return result
            
            # 如果来自 Supabase 队列，使用队列的轮询方法
            if queue_type == "supabase" and _supabase_queue_available and get_supabase_queue:
                queue = get_supabase_queue()
                if queue:
                    try:
                        # 增加轮询次数：最多 120 次（10分钟）
                        max_poll_attempts = 120
                        for attempt in range(max_poll_attempts):
                            task_info = await queue.poll_task_status(task_id)
                            if task_info:
                                status = task_info.get("status")
                                if status == "succeeded":
                                    video_url = task_info.get("video_url")
                                    if video_url:
                                        logger.info(f"[stitch_video_tool] Supabase queue task {task_id} succeeded (attempt {attempt + 1}/{max_poll_attempts}), got video URL")
                                        return {
                                            "task_idx": result.get("task_idx") or result.get("idx"),
                                            "status": "succeeded",
                                            "video_url": video_url,
                                            "task_id": task_id
                                        }
                                elif status == "failed":
                                    error = task_info.get("error", "任务失败")
                                    logger.error(f"[stitch_video_tool] Supabase queue task {task_id} failed: {error}")
                                    return {
                                        "task_idx": result.get("task_idx") or result.get("idx"),
                                        "status": "failed",
                                        "video_url": None,
                                        "error": error,
                                        "task_id": task_id
                                    }
                                elif status in {"pending", "processing", "submitted"}:
                                    # 任务还在处理中，继续等待
                                    if (attempt + 1) % 12 == 0:  # 每60秒打印一次日志
                                        logger.info(f"[stitch_video_tool] Supabase queue task {task_id} still {status}, waiting... (attempt {attempt + 1}/{max_poll_attempts})")
                            await asyncio.sleep(5)
                        
                        # 超时 - 但不要返回失败，而是返回 pending
                        logger.warning(f"[stitch_video_tool] Supabase queue task {task_id} polling timeout after {max_poll_attempts} attempts (10 minutes)")
                        return {
                            "idx": result.get("idx"),
                            "status": "pending",  # 改为 pending，而不是 failed
                            "video_url": None,
                            "error": f"轮询超时（已等待10分钟），任务可能仍在处理中，task_id: {task_id}",
                            "task_id": task_id
                        }
                    except Exception as e:
                        logger.error(f"[stitch_video_tool] Error polling Supabase queue task {task_id}: {e}", exc_info=True)
                        # 降级到直接轮询 RunningHub
                        queue_type = "direct"
            
            # 直接提交的任务，使用 RunningHub 轮询
            if queue_type == "direct":
                from runninghub_client import RunningHubClient
                client = RunningHubClient()
                
                # 获取 RunningHub task_id
                # 如果 task_id 是 Supabase 的 ID，需要从数据库获取 provider_task_id
                provider_task_id = task_id
                try:
                    import os
                    supabase_url = os.getenv("SUPABASE_URL")
                    supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
                    if supabase_url and supabase_key:
                        from supabase import create_client
                        supabase = create_client(supabase_url, supabase_key)
                        
                        # 尝试从数据库获取 provider_task_id
                        task_result = supabase.table("video_tasks")\
                            .select("provider_task_id")\
                            .eq("id", task_id)\
                            .single()\
                            .execute()
                        if task_result.data and task_result.data.get("provider_task_id"):
                            provider_task_id = task_result.data.get("provider_task_id")
                            logger.debug(f"[stitch_video_tool] Found provider_task_id={provider_task_id} for Supabase task_id={task_id}")
                except Exception as e:
                    logger.debug(f"[stitch_video_tool] Could not get provider_task_id from database: {e}, using task_id directly")
                
                try:
                    # 增加轮询次数：最多 120 次（10分钟），因为视频生成需要 3-5 分钟
                    # 每个任务可能需要更长时间，所以增加等待时间
                    max_poll_attempts = 120  # 10分钟 = 120 * 5秒
                    for attempt in range(max_poll_attempts):
                        status = await client.get_status(provider_task_id)
                        if status in {"SUCCESS"}:
                            # 获取输出
                            outputs = await client.get_outputs(provider_task_id)
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
                                        # 上传到 R2
                                        task_idx = result.get("task_idx") or result.get("idx", "unknown")
                                        cdn_url = await upload_url_to_r2(url, f"{run_id}_task{task_idx}.mp4")
                                        
                                        # 更新数据库（如果任务在数据库中）
                                        try:
                                            if supabase_url and supabase_key:
                                                from datetime import datetime
                                                supabase.table("video_tasks")\
                                                    .update({
                                                        "status": "succeeded",
                                                        "video_url": cdn_url,
                                                        "updated_at": datetime.utcnow().isoformat()
                                                    })\
                                                    .eq("id", task_id)\
                                                    .execute()
                                        except Exception as e:
                                            logger.debug(f"[stitch_video_tool] Failed to update database: {e}")
                                        
                                        logger.info(f"[stitch_video_tool] Polled task {provider_task_id} succeeded (attempt {attempt + 1}/{max_poll_attempts}), got video URL")
                                        return {
                                            "task_idx": result.get("task_idx") or result.get("idx"),
                                            "status": "succeeded",
                                            "video_url": cdn_url,
                                            "task_id": task_id
                                        }
                            break
                        elif status in {"FAILED", "ERROR"}:
                            logger.error(f"[stitch_video_tool] Task {provider_task_id} failed with status: {status}")
                            
                            # 更新数据库（如果任务在数据库中）
                            try:
                                if supabase_url and supabase_key:
                                    from datetime import datetime
                                    supabase.table("video_tasks")\
                                        .update({
                                            "status": "failed",
                                            "error": f"任务失败: {status}",
                                            "updated_at": datetime.utcnow().isoformat()
                                        })\
                                        .eq("id", task_id)\
                                        .execute()
                            except Exception as e:
                                logger.debug(f"[stitch_video_tool] Failed to update database: {e}")
                            
                            return {
                                "task_idx": result.get("task_idx") or result.get("idx"),
                                "status": "failed",
                                "video_url": None,
                                "error": f"任务失败: {status}",
                                "task_id": task_id
                            }
                        elif status in {"PENDING", "RUNNING", "QUEUED"}:
                            # 任务还在处理中，继续等待
                            if (attempt + 1) % 12 == 0:  # 每60秒打印一次日志
                                logger.info(f"[stitch_video_tool] Task {provider_task_id} still {status}, waiting... (attempt {attempt + 1}/{max_poll_attempts})")
                        await asyncio.sleep(5)
                    
                    # 超时 - 但不要返回失败，而是返回 pending，让调用者知道任务还在处理
                    logger.warning(f"[stitch_video_tool] Task {task_id} polling timeout after {max_poll_attempts} attempts (10 minutes)")
                    return {
                        "task_idx": result.get("task_idx") or result.get("idx"),
                        "status": "pending",  # 改为 pending，而不是 failed
                        "video_url": None,
                        "error": f"轮询超时（已等待10分钟），任务可能仍在处理中，task_id: {task_id}",
                        "task_id": task_id
                    }
                except Exception as e:
                    logger.error(f"[stitch_video_tool] Error polling task {task_id}: {e}", exc_info=True)
                    return {
                        "task_idx": result.get("task_idx") or result.get("idx"),
                        "status": "failed",
                        "video_url": None,
                        "error": f"轮询错误: {str(e)}",
                        "task_id": task_id
                    }
            
            # 未知队列类型，返回原结果
            return result
        
        # 轮询所有 pending 任务
        async def poll_all_pending():
            tasks = [poll_pending_task(r) for r in pending_tasks]
            return await asyncio.gather(*tasks)
        
        polled_results = _run_async_safe(poll_all_pending())
        
        # 更新 clip_results
        for i, result in enumerate(clip_results):
            if result.get("status") in ["pending", "submitted"]:
                # 找到对应的轮询结果（通过 task_id 匹配）
                task_id = result.get("task_id")
                for polled in polled_results:
                    if polled.get("task_id") == task_id:
                        logger.info(
                            f"[stitch_video_tool] Updated task {task_id} status from {result.get('status')} "
                            f"to {polled.get('status')}"
                        )
                        clip_results[i] = polled
                        break
        else:
                    # 如果没有找到匹配的轮询结果，记录警告
                    logger.warning(
                        f"[stitch_video_tool] No polled result found for task_id={task_id}, "
                        f"keeping original status: {result.get('status')}"
                    )
        
        # 重新检查是否还有 pending 或 submitted 任务
        # 同时从数据库查询最新状态，确保状态同步
        still_pending = [r for r in clip_results if r.get("status") in ["pending", "submitted"]]
        
        # 如果还有 pending 任务，尝试从数据库获取最新状态
        if still_pending:
            try:
                import os
                supabase_url = os.getenv("SUPABASE_URL")
                supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
                if supabase_url and supabase_key:
                    from supabase import create_client
                    supabase = create_client(supabase_url, supabase_key)
                    
                    # 查询所有任务的最新状态
                    task_ids_to_check = [r.get("task_id") for r in still_pending if r.get("task_id")]
                    if task_ids_to_check:
                        logger.info(
                            f"[stitch_video_tool] Checking database for latest status of {len(task_ids_to_check)} tasks: {task_ids_to_check}"
                        )
                        # 查询这些任务的最新状态
                        result = supabase.table("video_tasks")\
                            .select("id, status, video_url")\
                            .in_("id", task_ids_to_check)\
                            .execute()
                        
                        if result.data:
                            # 创建 task_id -> task_info 的映射
                            task_map = {str(t.get("id")): t for t in result.data}
                            
                            # 更新 clip_results 中匹配的任务
                            updated_count = 0
                            for i, result_item in enumerate(clip_results):
                                task_id = result_item.get("task_id")
                                if task_id and str(task_id) in task_map:
                                    db_task = task_map[str(task_id)]
                                    db_status = db_task.get("status")
                                    if db_status == "succeeded":
                                        # 数据库显示任务已完成，更新 clip_results
                                        logger.info(
                                            f"[stitch_video_tool] Task {task_id} status updated from database: "
                                            f"{result_item.get('status')} -> {db_status}, video_url={db_task.get('video_url', 'N/A')[:50]}"
                                        )
                                        clip_results[i] = {
                                            "task_idx": result_item.get("task_idx") or result_item.get("idx"),
                                            "status": "succeeded",
                                            "video_url": db_task.get("video_url"),
                                            "task_id": task_id
                                        }
                                        updated_count += 1
                            
                            if updated_count > 0:
                                logger.info(
                                    f"[stitch_video_tool] Updated {updated_count} tasks from database, "
                                    f"re-checking pending status..."
                                )
            except Exception as e:
                logger.debug(f"[stitch_video_tool] Failed to check database for latest status: {e}")
        
        # 重新检查是否还有 pending 或 submitted 任务（在数据库同步后）
        still_pending = [r for r in clip_results if r.get("status") in ["pending", "submitted"]]
        if still_pending:
            task_ids = [r.get("task_id") for r in still_pending if r.get("task_id")]
            logger.info(
                f"[stitch_video_tool] Found {len(still_pending)} pending tasks after initial polling: {task_ids}. "
                f"Registering callback instead of blocking..."
            )
            
            # 使用回调机制：注册会话，当所有任务完成时自动触发拼接
            try:
                from crewai_session_manager import get_session_manager
                session_manager = get_session_manager()
                
                if session_manager:
                    # 尝试从上下文获取 session_id（如果可用）
                    # 注意：CrewAI 不直接提供 session_id，我们需要通过其他方式获取
                    # 从环境变量中获取（在 workflow_crew_run 中设置）
                    import os
                    session_id = os.getenv(f"CREWAI_SESSION_ID_{run_id}") or f"session_{run_id}"
                    
                    # 计算期望的视频任务数量（按 task_idx 计算，不是镜头数）
                    # clip_results 中的每个元素对应一个视频任务
                    total_tasks = len([r for r in clip_results if r.get("task_idx") or r.get("idx")])
                    
                    # 注册会话（异步执行，不阻塞）
                    async def register_callback():
                        await session_manager.register_session(
                            run_id=run_id,
                            session_id=session_id,
                            expected_clips=total_tasks,  # 期望的视频任务数
                            context={
                                "clip_results": clip_results,
                                "pending_task_ids": task_ids,
                                "expected_tasks": total_tasks
                            }
                        )
                        # 立即检查一次（可能任务已经完成）
                        await session_manager.check_and_trigger_stitch(run_id)
                    
                    _run_async_safe(register_callback())
                    
                    logger.info(
                        f"[stitch_video_tool] Registered callback for run_id={run_id}, "
                        f"will trigger stitch when all {total_tasks} video tasks complete"
                    )
                    
                    # 返回明确的错误信息，防止 LLM 自己生成 URL
                    # 拼接将在回调中自动完成
                    error_msg = (
                        f"❌ 视频拼接失败：视频生成任务还在处理中。\n"
                        f"当前状态：{len(still_pending)}/{total_tasks} 个视频任务仍在生成中（pending/submitted）。\n"
                        f"处理方式：已注册回调机制，当所有视频片段生成完成时，系统将自动触发拼接。\n"
                        f"请勿手动生成或猜测视频 URL，必须等待所有视频片段完成后再进行拼接。\n"
                        f"pending 任务 ID: {task_ids[:5]}{'...' if len(task_ids) > 5 else ''}"
                    )
                    logger.warning(f"[stitch_video_tool] {error_msg}")
                    raise RuntimeError(error_msg)
                else:
                    # 会话管理器不可用，降级到轮询模式
                    logger.warning(
                        f"[stitch_video_tool] Session manager not available, "
                        f"falling back to polling mode"
                    )
                    raise RuntimeError(
                        f"视频生成任务还在处理中（{len(still_pending)} 个任务 pending），"
                        f"无法拼接。请稍后再试。"
                    )
            except ImportError:
                # 会话管理器未实现，降级到轮询
                logger.warning(
                    f"[stitch_video_tool] Session manager not available, "
                    f"falling back to error response"
                )
                raise RuntimeError(
                    f"视频生成任务还在处理中（{len(still_pending)} 个任务 pending），"
                    f"无法拼接。请稍后再试。"
                )
    
    # 再次检查是否还有 pending 或 submitted 任务（防止在轮询后仍有 pending 任务）
    final_pending = [r for r in clip_results if r.get("status") in ["pending", "submitted"]]
    if final_pending:
        task_ids = [r.get("task_id") for r in final_pending if r.get("task_id")]
        logger.error(
            f"[stitch_video_tool] CRITICAL: Still have {len(final_pending)} pending tasks after polling: {task_ids}. "
            f"Cannot proceed with stitching. This should not happen if callback mechanism is working correctly."
        )
        raise RuntimeError(
            f"❌ 视频拼接失败：仍有 {len(final_pending)} 个视频任务在处理中（pending/submitted）。"
            f"无法进行拼接。请等待所有视频片段生成完成。"
            f"pending 任务 ID: {task_ids[:5]}{'...' if len(task_ids) > 5 else ''}"
        )
    
    # 按 task_idx 排序，确保顺序正确
    clip_results.sort(key=lambda x: x.get("task_idx", 0) or x.get("idx", 0) or 0)
    
    segments = [r.get("video_url") for r in clip_results if r.get("status") == "succeeded" and r.get("video_url")]
    
    if not segments:
        failed_tasks = [r for r in clip_results if r.get("status") == "failed"]
        pending_tasks = [r for r in clip_results if r.get("status") in ["pending", "submitted"]]
        if pending_tasks:
            # 如果还有 pending 任务，不应该到达这里，但为了安全起见，再次检查
            raise RuntimeError(
                f"❌ 没有可用的视频片段。仍有 {len(pending_tasks)} 个任务在处理中（pending/submitted），"
                f"无法进行拼接。请等待所有视频片段生成完成。"
            )
        elif failed_tasks:
            errors = [f"片段 {r.get('idx')}: {r.get('error', '未知错误')}" for r in failed_tasks]
            raise RuntimeError(f"❌ 没有可用的视频片段。失败的任务：{'; '.join(errors)}")
        else:
            raise RuntimeError("❌ 没有可用的视频片段。所有任务可能仍在处理中或已失败。")
    
    # 使用独立的视频拼接函数
    from video_stitcher import stitch_video_segments
    
    logger.info(
        f"[stitch_video_tool] Calling stitch_video_segments for run_id={run_id}: "
        f"{len(segments)} segments"
    )
    
    # 调用独立的拼接函数（异步执行）
    cdn_url = _run_async_safe(stitch_video_segments(segments, run_id))
    
    return cdn_url

