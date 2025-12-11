"""
CrewAI Tools - 封装视频生成工作流的工具函数
"""
import os
from typing import Optional, List, Dict, Any, Callable
from crewai.tools import tool
from .openrouter_client import OpenRouterClient, OpenRouterError
import httpx
from .providers import get_image_provider, get_video_provider
from .r2 import upload_url_to_r2
from .runninghub_client import RunningHubClient

# 尝试导入 Supabase 队列（可选）
try:
    from .video_task_queue_supabase import get_supabase_queue
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


@tool("生成Sora2视频提示词")
def generate_sora2_prompt_tool(
    product_name: str,
    product_features: str,
    video_type: str,
    language: str,
    duration: str,
    reference_image_info: str | None = None,
    reference_image_description: str | None = None,
) -> str:
    """
    生成符合 Sora 2 技术规范的电商视频提示词，仅输出提示词文本。
    
    Args:
        product_name: 产品名称
        product_features: 产品卖点描述
        video_type: 视频类型（如奢侈品展示/运动性能/日常生活/技术创新等）
        language: 目标语言
        duration: 视频时长（10/15/25秒之一）
        reference_image_info: 参考图片信息（可选）
        reference_image_description: 参考图片描述（可选）
    
    Returns:
        直接可用于 Sora-2 的视频提示词文本
    """
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
    sys_prompt = (
        "你是一位专业的电商视频导演助手，为 Sora 2 生成直接可用的视频提示词。"
        "严格遵守：时长仅 10/15/25 秒；画幅 16:9 或 9:16；"
        "原生同步旁白（目标语言，单句不超过8字）；音效分层与音乐节奏；"
        "真实物理与摄像机运动；避免屏幕文字、复杂多物体物理、瞬间加速、过度荷兰角、过度眩光。"
        "输出只包含提示词内容，不要任何说明或额外文字。"
    )
    user_prompt = (
        "请为以下电商产品生成一个专业的 Sora-2 视频提示词：\n\n"
        f"产品名称：{product_name}\n"
        f"产品卖点：{product_features}\n"
        f"视频类型：{video_type}\n"
        f"目标语言：{language}\n"
        f"视频时长：{duration}\n"
        f"{reference_image_info or ''}\n"
        f"{reference_image_description or ''}\n\n"
        "请生成一个完整完善的、直接可用于 Sora-2 的视频提示词。注意只输出提示词内容。"
    )
    async def _gen():
        return await or_client.chat_completions(
            model=PROMPT_LLM_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1200,
        )
    return _run_async_safe(_gen())

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
    # 计算需要多少个 scene（每个 scene 10s）
    num_scenes = max(1, int(total_duration / 10.0) + (1 if total_duration % 10.0 > 0 else 0))
    
    # 要求输出严格的 JSON 对象（包含 scenes 数组），每个 scene 包含多个镜头
    # 【关键】由于 director_agent 不启用 reasoning，这里需要在 prompt 中强调格式要求
    sys_prompt = (
        "你是资深广告导演。根据用户目标与风格，将整个视频拆分为场景（scene），每个场景包含多个镜头（clip）。\n\n"
        "【重要】结构要求：\n"
        "1. 视频由多个场景（scene）组成，每个场景时长恰好为10秒\n"
        "2. 每个场景包含多个镜头（clip），镜头数量可以根据内容需要灵活调整\n"
        "3. 每个镜头的时长（end_s - begin_s）必须不超过10s\n"
        "4. 场景内的镜头时间必须连续，且场景总时长必须恰好为10s\n\n"
        "【关键】场景转场衔接要求：\n"
        "1. 每个场景的结尾画面应该自然过渡到下一个场景的开头画面\n"
        "2. 考虑场景之间的视觉连贯性，使用相似的色调、构图或元素进行衔接\n"
        "3. 在场景描述中考虑转场方式（淡入淡出、交叉溶解、硬切等），确保视觉流畅\n"
        "4. 相邻场景之间应该有逻辑关联，避免突兀的跳跃\n\n"
        "【关键】文案与语音参数要求：\n"
        "1. 每个场景必须包含完整的介绍文案（narration），文案应该覆盖整个场景的10秒时长\n"
        "2. 文案应该自然、流畅，与画面内容完美匹配，时长约30-40字（10秒旁白）\n"
        "3. 确保文案的完整性，不要截断或省略关键信息\n"
        "4. 文案应该与场景内的镜头内容同步，描述画面中正在发生的事情\n"
        "5. 文案应该具有连贯性，相邻场景的文案应该自然衔接\n\n"
        "【新增】每个场景必须包含语音参数 voice_params：emotion（happy/sad/angry/calm 等）、speed（0.5-2.0）、vol（0.5-2.0）、pitch（-12 至 12），用于旁白合成。\n\n"
        "【关键】输出格式要求（必须严格遵守，这是最重要的）：\n"
        "1. 严格只输出 JSON 对象，不要任何额外文字、说明、Markdown 代码块、注释或思考过程\n"
        "2. 不要输出任何 reasoning 或思考过程，直接输出 JSON\n"
        "3. JSON 结构必须为：{\"scenes\": [{\"scene_idx\": 1, \"narration\": \"完整的旁白文案（30-40字）\", \"voice_params\": {\"emotion\": \"calm\", \"speed\": 1.0, \"vol\": 1.0, \"pitch\": 0}, \"clips\": [{\"idx\": 1, \"desc\": \"…\", \"begin_s\": 0.0, \"end_s\": 3.0}, ...], \"begin_s\": 0.0, \"end_s\": 10.0}, ...]}\n"
        "4. 每个 scene 必须包含 scene_idx, narration, clips, begin_s, end_s 字段\n"
        "5. narration 字段：完整的旁白文案（30-40字），覆盖整个场景的10秒时长，与画面内容匹配\n"
        "6. voice_params 字段：包含 emotion/speed/vol/pitch，用于旁白合成；默认 emotion=calm, speed=1.0, vol=1.0, pitch=0\n"
        "7. 每个 clip 必须包含 idx, desc, begin_s, end_s 字段，且 end_s - begin_s <= 10.0\n"
        "8. scene 的 begin_s 和 end_s 必须恰好相差 10.0 秒\n"
        "9. scene 内的 clips 时间必须连续，且覆盖整个 scene 的时长\n"
        "10. desc 为一句中文分镜描述（20-50字），不含编号/时间/标题/Markdown 符号\n"
        "11. 不要输出 keyframes，这些由后续工具生成\n"
        "11. 【最重要】直接输出 JSON，不要任何前缀、后缀或说明文字\n\n"
        "【正确示例】（必须完全按照此格式）：\n"
        "{\n"
        "  \"scenes\": [\n"
        "    {\n"
        "      \"scene_idx\": 1,\n"
        "      \"begin_s\": 0.0,\n"
        "      \"end_s\": 10.0,\n"
        "      \"clips\": [\n"
        "        {\"idx\": 1, \"desc\": \"特写iPhone 17摄像头模组，金属边框反射冷冽蓝光，背景纯黑，镜头缓慢推进\", \"begin_s\": 0.0, \"end_s\": 3.0},\n"
        "        {\"idx\": 2, \"desc\": \"中景悬浮的iPhone 17完整机身，钛金属边框流转霓虹光效\", \"begin_s\": 3.0, \"end_s\": 7.0},\n"
        "        {\"idx\": 3, \"desc\": \"手机360度旋转展示，突出轻薄设计\", \"begin_s\": 7.0, \"end_s\": 10.0}\n"
        "      ]\n"
        "    },\n"
        "    {\n"
        "      \"scene_idx\": 2,\n"
        "      \"begin_s\": 10.0,\n"
        "      \"end_s\": 20.0,\n"
        "      \"clips\": [...]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "【错误示例】（禁止使用）：\n"
        "❌ {\"storyboards\": [...]} （旧格式，已废弃）\n"
        "❌ {\"镜头1\": {...}} 格式\n"
        "❌ 任何包含中文键名的格式\n\n"
        f"请严格按照正确示例的格式返回，生成恰好 {num_scenes} 个场景，每个场景恰好10秒。"
    )
    user_prompt = (
        f"主体目标：{goal}\n"
        f"风格：{', '.join(styles) if styles else '通用'}\n"
        f"总时长：{total_duration}秒\n"
        f"场景数：{num_scenes}（必须严格生成恰好 {num_scenes} 个场景，每个场景恰好10秒）\n\n"
        f"【重要要求】：\n"
        f"1. 每个场景必须包含完整的旁白文案（narration），约30-40字，覆盖整个10秒时长\n"
        f"2. 文案应该与画面内容完美匹配，描述场景中正在发生的事情\n"
        f"3. 相邻场景的文案应该自然衔接，确保整体叙述的连贯性\n"
        f"4. 每个场景的结尾画面应该考虑与下一个场景的衔接，确保视觉流畅\n"
        f"5. 使用相似的色调、构图或元素来连接相邻场景，避免突兀的跳跃\n\n"
        f"请严格按上述 JSON 结构返回，scenes 数组必须包含恰好 {num_scenes} 个场景。\n"
        f"每个场景必须包含 narration 字段（完整的旁白文案）、voice_params 以及多个镜头（clips）。\n"
        f"【重要】必须返回有效的 JSON 格式，使用 \"scenes\" 作为顶层数组，每个 scene 包含 \"scene_idx\", \"narration\", \"voice_params\", \"clips\", \"begin_s\", \"end_s\"。\n"
        f"【关键】场景数量必须严格等于 {num_scenes}，每个场景的时长必须恰好为10秒。"
    )
    
    logger.info(f"[plan_storyboard_impl] Calling OpenRouter with model={model}, num_clips={num_clips}")
    logger.debug(f"[plan_storyboard_impl] System prompt: {sys_prompt[:500]}")
    logger.debug(f"[plan_storyboard_impl] User prompt: {user_prompt[:500]}")
    
    # 根据分镜数量动态计算 max_tokens
    # 注意：对于推理模型（如 gpt-5-mini, gpt-5.1），即使不启用 reasoning，模型本身也会进行推理，需要更多 tokens
    # Claude 模型（如 claude-sonnet-4.5）对 JSON 支持好，不需要额外 tokens
    base_tokens = 500
    is_reasoning_model = "gpt-5" in model or "gpt-5-mini" in model or "gpt-5.1" in model
    
    if is_reasoning_model:
        # 推理模型需要更多 tokens（包括推理过程），每个 scene 约需 500-600 tokens
        tokens_per_scene = 600
        # 对于推理模型，大幅增加 max_tokens，避免被截断
        calculated_max_tokens = min(8000, num_scenes * tokens_per_scene + base_tokens)
        logger.warning(f"[plan_storyboard_impl] Using reasoning model {model}, increased max_tokens to {calculated_max_tokens} to avoid truncation")
    elif "claude" in model.lower():
        # Claude 模型对 JSON 支持很好，每个 scene 约需 200-250 tokens
        tokens_per_scene = 250
        calculated_max_tokens = min(4000, num_scenes * tokens_per_scene + base_tokens)
        logger.info(f"[plan_storyboard_impl] Using Claude model {model}, JSON format support is excellent")
    else:
        # 其他非推理模型（如 gpt-4o-mini），每个 scene 约需 200 tokens
        tokens_per_scene = 200
        calculated_max_tokens = min(4000, num_scenes * tokens_per_scene + base_tokens)
    logger.info(f"[plan_storyboard_impl] Calculated max_tokens={calculated_max_tokens} for num_scenes={num_scenes} (model={model})")
    
    try:
        # 尝试使用 JSON Schema 来强制输出指定格式（如果模型支持）
        # OpenAI GPT-4o 和 Claude Sonnet 4.5 都支持 JSON Schema
        response_format = None
        is_reasoning_model = "gpt-5" in model or "gpt-5-mini" in model or "gpt-5.1" in model
        
        if is_reasoning_model:
            # 推理模型（gpt-5-mini, gpt-5.1）即使不使用 response_format，模型也会进行推理
            # 这会导致响应很长，容易被截断，且可能返回加密的 reasoning_details
            # 建议：使用 claude-sonnet-4.5 或 gpt-4o-mini 来避免这个问题
            logger.warning(f"[plan_storyboard_impl] Using reasoning model {model} may cause truncation or format issues. Consider using claude-sonnet-4.5 or gpt-4o-mini instead.")
            # 不使用 response_format，让模型自由输出，在 prompt 中强调 JSON 格式
            response_format = None
            logger.info(f"[plan_storyboard_impl] Not using response_format for reasoning model {model} (to avoid encrypted reasoning)")
        elif "claude" in model.lower() or "gpt-4" in model or "gpt-4o" in model:
            # Claude 和 GPT-4o 模型支持 JSON Schema，使用 strict schema 强制格式
            try:
                # 定义 JSON Schema，强制输出 scenes 结构
                json_schema = {
                    "type": "object",
                    "properties": {
                        "scenes": {
                            "type": "array",
                            "description": f"场景数组，每个场景恰好10秒，必须生成恰好{num_scenes}个场景",
                            "minItems": 1,  # OpenRouter API 只支持 0 或 1，通过描述和 prompt 来要求数量
                            # 注意：OpenRouter API 不支持 maxItems，完全依赖 prompt 来限制数量
                            "items": {
                                "type": "object",
                                "properties": {
                                    "scene_idx": {
                                        "type": "integer",
                                        "description": "场景序号，从1开始"
                                    },
                                    "narration": {
                                        "type": "string",
                                        "description": "完整的旁白文案，约30-40字，覆盖整个场景的10秒时长，与画面内容匹配"
                                    },
                                    "voice_params": {
                                        "type": "object",
                                        "properties": {
                                            "emotion": {"type": "string"},
                                            "speed": {"type": "number"},
                                            "vol": {"type": "number"},
                                            "pitch": {"type": "integer"}
                                        },
                                        "required": ["emotion", "speed", "vol", "pitch"],
                                        "additionalProperties": False,
                                        "description": "旁白语音参数：emotion/speed/vol/pitch"
                                    },
                                    "begin_s": {
                                        "type": "number",
                                        "description": "场景开始时间（秒）"
                                    },
                                    "end_s": {
                                        "type": "number",
                                        "description": "场景结束时间（秒），必须恰好比 begin_s 大 10.0"
                                    },
                                    "clips": {
                                        "type": "array",
                                        "description": "镜头数组，每个场景包含至少1个镜头",
                                        "minItems": 1,  # 保持为 1，符合 OpenRouter API 要求
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "idx": {
                                                    "type": "integer",
                                                    "description": "镜头序号，从1开始"
                                                },
                                                "desc": {
                                                    "type": "string",
                                                    "description": "镜头描述，20-50字的中文描述"
                                                },
                                                "begin_s": {
                                                    "type": "number",
                                                    "description": "镜头开始时间（秒）"
                                                },
                                                "end_s": {
                                                    "type": "number",
                                                    "description": "镜头结束时间（秒），必须不超过 begin_s + 10.0"
                                                }
                                            },
                                            "required": ["idx", "desc", "begin_s", "end_s"],
                                            "additionalProperties": False
                                        }
                                    }
                                },
                                "required": ["scene_idx", "narration", "voice_params", "begin_s", "end_s", "clips"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["scenes"],
                    "additionalProperties": False
                }
                
                # 对于支持 JSON Schema 的模型，使用 strict schema
                if "claude" in model.lower():
                    # Claude 模型使用 JSON Schema（如果支持）
                    response_format = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "storyboard_schema",
                            "strict": True,  # 严格模式，强制遵循 schema
                            "schema": json_schema
                        }
                    }
                    logger.info(f"[plan_storyboard_impl] Using strict JSON Schema for Claude model {model}")
                elif "gpt-4o" in model:
                    # GPT-4o 模型支持 JSON Schema（gpt-4o-2024-08-06 及之后版本）
                    response_format = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "storyboard_schema",
                            "strict": True,  # 严格模式，强制遵循 schema
                            "schema": json_schema
                        }
                    }
                    logger.info(f"[plan_storyboard_impl] Using strict JSON Schema for GPT-4o model {model}")
                else:
                    # 其他 GPT-4 模型使用简单的 JSON mode
                    response_format = {"type": "json_object"}
                    logger.info(f"[plan_storyboard_impl] Using JSON mode for GPT-4 model {model}")
            except Exception as schema_error:
                # 如果创建 schema 失败，降级到简单的 JSON mode
                logger.warning(f"[plan_storyboard_impl] Failed to create JSON schema, falling back to simple JSON mode: {schema_error}")
                response_format = {"type": "json_object"}
        
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
        logger.warning(f"[plan_storyboard_impl] empty outline from OpenRouter (outline={repr(outline)}), trying quick retry")
        try:
            # 如果当前模型是推理模型，直接降级到 claude-sonnet-4.5 或 gpt-4o-mini
            # 推理模型即使不启用 reasoning，也会进行推理，导致响应被截断或格式问题
            is_reasoning_model = "gpt-5" in model or "gpt-5-mini" in model or "gpt-5.1" in model
            if is_reasoning_model:
                # 优先使用 claude-sonnet-4.5（JSON 支持更好），如果没有则使用 gpt-4o-mini
                fallback_model = "anthropic/claude-sonnet-4.5"
                logger.info(f"[plan_storyboard_impl] Reasoning model {model} returned empty content or was truncated, falling back to {fallback_model}")
                # 降级后重新计算 max_tokens（Claude 模型需要稍多 tokens）
                calculated_max_tokens = min(4000, num_scenes * 250 + 500)
            else:
                fallback_model = model
            
            # 简化 prompt，减少复杂度
            simplified_sys_prompt = (
                "你是资深广告导演。根据用户目标与风格，将整个视频拆分为场景（scene），每个场景包含多个镜头（clip）。\n\n"
                "输出格式：严格的 JSON 对象，结构为 {\"scenes\": [{\"scene_idx\": 1, \"clips\": [{\"idx\": 1, \"desc\": \"...\", \"begin_s\": 0.0, \"end_s\": 3.0}, ...], \"begin_s\": 0.0, \"end_s\": 10.0}, ...]}\n"
                f"请生成恰好 {num_scenes} 个场景，每个场景恰好10秒。\n"
                "【重要】只输出 JSON，不要任何额外的文字、说明或 Markdown 代码块。"
            )
            simplified_user_prompt = (
                f"目标：{goal}\n"
                f"风格：{', '.join(styles) if styles else '通用'}\n"
                f"总时长：{total_duration}秒\n"
                f"请返回 JSON 格式的分镜脚本，包含 {num_scenes} 个场景。"
            )
            
            # 使用降级模型或原模型重试
            outline = await or_client.chat_completions(
                model=fallback_model,
                messages=[
                    {"role": "system", "content": simplified_sys_prompt},
                    {"role": "user", "content": simplified_user_prompt}
                ],
                temperature=0.3,
                max_tokens=calculated_max_tokens,
                response_format={"type": "json_object"} if fallback_model != "gpt-5-mini" else None,  # 降级模型使用 JSON mode
            )
            
            if not outline or not isinstance(outline, str) or len(outline.strip()) == 0:
                raise RuntimeError(f"OpenRouter 返回空内容（模型：{fallback_model}）。可能是模型拒绝生成或 API 配置问题。")
        except OpenRouterError as e:
            # OpenRouter 特定错误（如模型拒绝）
            error_msg = f"OpenRouter 错误: {str(e)}"
            logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
            raise RuntimeError(f"分镜生成失败：{error_msg}。")
        except Exception as e:
            error_msg = f"重试失败: {str(e)}"
            logger.error(f"[plan_storyboard_impl] {error_msg}", exc_info=True)
            raise RuntimeError(f"分镜生成失败：{error_msg}。")
    try:
        logger.info(f"[plan_storyboard_impl] raw_outline(len={len(outline) if isinstance(outline,str) else -1}): {outline[:1000] if isinstance(outline,str) else outline}")
    except Exception:
        pass
    
    # 解析输出为 scene 结构：优先 JSON 对象->scenes；兼容旧格式 storyboards
    import json, re
    
    def _extract_scenes(text: str) -> dict:
        """从文本中提取 scene 结构。"""
        # 1. 代码围栏
        fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        t = fenced[0] if fenced else text.strip()
        
        # 2. 尝试新格式：scenes
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and isinstance(obj.get("scenes"), list):
                scenes = obj["scenes"]
                # 验证 scene 结构
                for scene in scenes:
                    if not isinstance(scene, dict):
                        continue
                    if "clips" not in scene or not isinstance(scene["clips"], list):
                        continue
                    # 验证每个 clip
                    for clip in scene["clips"]:
                        if not isinstance(clip, dict) or "desc" not in clip:
                            continue
                return {"scenes": scenes}
        except Exception as e:
            logger.debug(f"[plan_storyboard_impl] Failed to parse scenes format: {e}")
        
        # 3. 兼容旧格式：storyboards（转换为 scenes）
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and isinstance(obj.get("storyboards"), list):
                storyboards = obj["storyboards"]
                # 将 storyboards 转换为 scenes（每个 scene 10s）
                scenes = []
                current_scene = None
                scene_idx = 1
                clip_idx = 1
                
                for sb in storyboards:
                    if not isinstance(sb, dict) or "desc" not in sb:
                        continue
                    
                    begin_s = float(sb.get("begin_s", 0))
                    end_s = float(sb.get("end_s", begin_s + 5.0))
                    
                    # 计算应该属于哪个 scene（每10s一个scene）
                    scene_num = int(begin_s / 10.0) + 1
                    
                    if current_scene is None or current_scene["scene_idx"] != scene_num:
                        # 开始新 scene
                        if current_scene is not None:
                            scenes.append(current_scene)
                        scene_begin = (scene_num - 1) * 10.0
                        current_scene = {
                            "scene_idx": scene_num,
                            "begin_s": scene_begin,
                            "end_s": scene_begin + 10.0,
                            "clips": []
                        }
                        clip_idx = 1
                    
                    # 添加 clip 到当前 scene
                    current_scene["clips"].append({
                        "idx": clip_idx,
                        "desc": str(sb["desc"]).strip(),
                        "begin_s": begin_s,
                        "end_s": end_s
                    })
                    clip_idx += 1
                
                if current_scene is not None:
                    scenes.append(current_scene)
                
                if scenes:
                    logger.info(f"[plan_storyboard_impl] Converted {len(storyboards)} storyboards to {len(scenes)} scenes")
                    return {"scenes": scenes}
        except Exception as e:
            logger.debug(f"[plan_storyboard_impl] Failed to parse storyboards format: {e}")
        
        return None
    
    scenes_data = _extract_scenes(outline)
    # 如果没有成功解析 scenes，尝试重试
    if not scenes_data or not scenes_data.get("scenes"):
        logger.warning(f"[plan_storyboard_impl] Failed to parse scenes, retrying...")
        repair_system = (
            "你是严格的格式化助手。仅输出 JSON 对象，不要任何说明或 Markdown。\n"
            "JSON 结构：{\"scenes\": [{\"scene_idx\": 1, \"clips\": [{\"idx\": 1, \"desc\": \"…\", \"begin_s\": 0.0, \"end_s\": 3.0}, ...], \"begin_s\": 0.0, \"end_s\": 10.0}, ...]}。\n"
            "每个 scene 必须恰好10秒，包含多个 clips。desc 必须是具体镜头画面描述，禁止出现'镜头描述/占位/示例'等空泛内容。"
        )
        repair_user = (
            f"请为 {num_scenes} 个场景生成分镜脚本，每个场景恰好10秒，包含多个镜头。\n"
            f"主体目标：{goal}\n风格：{', '.join(styles) if styles else '通用'}\n总时长：{total_duration}秒\n\n"
            f"必须返回 {num_scenes} 个场景，每个场景恰好10秒。"
        )
        try:
            repair = await or_client.chat_completions(
                model=model,
                messages=[{"role": "system", "content": repair_system}, {"role": "user", "content": repair_user}],
                temperature=0.3,
                max_tokens=calculated_max_tokens,
            )
            scenes_data = _extract_scenes(repair)
            if scenes_data and scenes_data.get("scenes"):
                logger.info(f"[plan_storyboard_impl] Retry successful, got {len(scenes_data['scenes'])} scenes")
        except Exception as e:
            logger.warning(f"[plan_storyboard_impl] Retry failed: {e}")
    
    # 如果仍然没有 scenes，创建默认结构（使用 goal 和 styles 生成有意义的描述）
    if not scenes_data or not scenes_data.get("scenes"):
        logger.warning(f"[plan_storyboard_impl] Failed to parse scenes after retry, creating default structure with meaningful descriptions")
        scenes = []
        # 根据 goal 和 styles 生成场景描述
        goal_keywords = goal[:30] if goal else "视频内容"  # 取前30个字符作为关键词
        style_text = ", ".join(styles) if styles else "通用风格"
        
        for i in range(num_scenes):
            scene_begin = i * 10.0
            # 生成更有意义的描述，避免被验证逻辑过滤
            scene_desc = f"展示{goal_keywords}相关内容，采用{style_text}风格，呈现场景{i+1}的视觉画面"
            scenes.append({
                "scene_idx": i + 1,
                "begin_s": scene_begin,
                "end_s": scene_begin + 10.0,
                "clips": [
                    {
                        "idx": 1,
                        "desc": scene_desc,
                        "begin_s": scene_begin,
                        "end_s": scene_begin + 10.0
                    }
                ]
            })
        scenes_data = {"scenes": scenes}
    
    # 验证和修复 scenes
    scenes = scenes_data["scenes"]
    validated_scenes = []
    
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        scene_idx = scene.get("scene_idx", len(validated_scenes) + 1)
        scene_begin = float(scene.get("begin_s", (scene_idx - 1) * 10.0))
        scene_end = float(scene.get("end_s", scene_begin + 10.0))
        clips = scene.get("clips", [])
        
        # 确保 scene 时长恰好为10s
        if abs(scene_end - scene_begin - 10.0) > 0.1:
            scene_end = scene_begin + 10.0
        
        # 验证和修复 clips
        validated_clips = []
        clip_idx = 1
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            desc = str(clip.get("desc", "")).strip()
            # 如果描述为空或太短，跳过（但保留有意义的默认描述）
            if not desc or len(desc) < 5:
                continue
            # 过滤明显的占位符文本，但保留有意义的描述
            if any(bt in desc for bt in ["请填写", "placeholder", "待填写", "待补充"]):
                continue
            # 如果描述包含 "描述待生成" 但长度足够，可能是默认结构，尝试保留
            if "描述待生成" in desc and len(desc) < 15:
                continue
            
            clip_begin = float(clip.get("begin_s", scene_begin))
            clip_end = float(clip.get("end_s", clip_begin + 3.0))
            
            # 确保 clip 时间在 scene 范围内
            clip_begin = max(scene_begin, min(clip_begin, scene_end))
            clip_end = max(clip_begin + 0.1, min(clip_end, scene_end))
            
            validated_clips.append({
                "idx": clip_idx,
                "desc": desc,
                "begin_s": clip_begin,
                "end_s": clip_end
            })
            clip_idx += 1
        
        # 如果 scene 没有有效 clips，创建一个基于 goal 和 styles 的默认描述
        if not validated_clips:
            goal_keywords = goal[:30] if goal else "视频内容"
            style_text = ", ".join(styles) if styles else "通用风格"
            default_desc = f"展示{goal_keywords}相关内容，采用{style_text}风格，呈现场景{scene_idx}的视觉画面"
            validated_clips.append({
                "idx": 1,
                "desc": default_desc,
                "begin_s": scene_begin,
                "end_s": scene_end
            })
        
        validated_scenes.append({
            "scene_idx": scene_idx,
            "begin_s": scene_begin,
            "end_s": scene_end,
            "clips": validated_clips
        })
    
    # 确保有足够的 scenes
    while len(validated_scenes) < num_scenes:
        scene_idx = len(validated_scenes) + 1
        scene_begin = (scene_idx - 1) * 10.0
        # 生成基于 goal 和 styles 的描述
        goal_keywords = goal[:30] if goal else "视频内容"
        style_text = ", ".join(styles) if styles else "通用风格"
        default_desc = f"展示{goal_keywords}相关内容，采用{style_text}风格，呈现场景{scene_idx}的视觉画面"
        validated_scenes.append({
            "scene_idx": scene_idx,
            "begin_s": scene_begin,
            "end_s": scene_begin + 10.0,
            "clips": [{
                "idx": 1,
                "desc": default_desc,
                "begin_s": scene_begin,
                "end_s": scene_begin + 10.0
            }]
        })
    
    # 只保留需要的 scenes
    validated_scenes = validated_scenes[:num_scenes]
    
    logger.info(f"[plan_storyboard_impl] Generated {len(validated_scenes)} scenes with {sum(len(s['clips']) for s in validated_scenes)} total clips")
    
    return json.dumps({"scenes": validated_scenes}, ensure_ascii=False)


async def refine_storyboard_from_scene_descriptions(scene_texts: List[str], styles: List[str], total_duration: float) -> str:
    """依据每个 scene 的文字描述，生成符合规范的 scene 结构 storyboard（每个 scene 恰好 10s）。"""
    if not (OPENROUTER_BASE and OPENROUTER_KEY):
        raise RuntimeError("未配置 OpenRouter（OPENROUTER_API_BASE / OPENROUTER_API_KEY）")
    or_client = OpenRouterClient(
        api_base=OPENROUTER_BASE,
        api_key=OPENROUTER_KEY,
        referer=EMBED_REFERER,
        title="SaleAgent"
    )
    num_scenes = max(1, len(scene_texts))
    sys_prompt = (
        "你是资深广告导演。基于用户已产出的每个场景的文字描述，生成规范的分镜脚本：\n"
        "- 返回 JSON 对象，包含 \"scenes\" 数组，长度与输入场景数一致\n"
        "- 每个 scene 恰好 10s，包含完整 narration（约30-40字，覆盖全时长）与多个 clips\n"
        "- clips 列表中每个镜头包含 idx, desc, begin_s, end_s，镜头时间连续且总时长恰好 10s\n"
        "- narration 要与 clips 描述内容一致、自然衔接\n"
        "- 保持风格：" + ", ".join(styles or []) + "\n"
        "- 输出严格 JSON：{\"scenes\": [{\"scene_idx\": 1, \"narration\": \"...\", \"clips\": [...], \"begin_s\": 0.0, \"end_s\": 10.0}, ...]}\n"
    )
    user_prompt = {
        "scene_texts": scene_texts,
        "requirements": {
            "num_scenes": num_scenes,
            "per_scene_duration": 10.0,
            "total_duration": float(total_duration or num_scenes * 10.0),
        }
    }
    resp = await or_client.chat(
        model=STORYBOARD_LLM_MODEL,
        system=sys_prompt,
        messages=[{"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)}],
        temperature=0.3,
    )
    content = or_client.pick_content(resp) or "{}"
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and obj.get("scenes"):
            return json.dumps(obj, ensure_ascii=False)
    except Exception:
        pass
    # 回退：为每个文本生成基本结构
    scenes = []
    for i, t in enumerate(scene_texts, start=1):
        scenes.append({
            "scene_idx": i,
            "narration": (str(t)[:40] or "旁白"),
            "clips": [
                {"idx": 1, "desc": str(t)[:80] or "镜头描述", "begin_s": 0.0, "end_s": 5.0},
                {"idx": 2, "desc": "场景延续", "begin_s": 5.0, "end_s": 10.0},
            ],
            "begin_s": 0.0,
            "end_s": 10.0,
        })
    return json.dumps({"scenes": scenes}, ensure_ascii=False)

def review_storyboard_impl(storyboards_json: str, num_clips: int, goal: str, styles: List[str], total_duration: float = 10.0, max_retries: int = 3) -> str:
    """
    审核分镜脚本的内部实现函数（可直接调用）。
    
    审核分镜脚本的质量，检查是否有效。
    
    如果分镜脚本无效（包含空描述、过短描述、占位符等），
    自动触发重写，最多重试 max_retries 次，直到生成有效的分镜脚本。
    
    Args:
        storyboards_json: JSON 格式的分镜脚本列表
        num_clips: 期望的镜头数量
        goal: 主体目标
        styles: 风格列表
        total_duration: 总时长（秒）
        max_retries: 最大重试次数（默认3次）
        
    Returns:
        如果有效：返回审核通过的分镜脚本 JSON
        如果重试后仍无效：返回包含错误信息的 JSON
    """
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
    
    # 解析并验证分镜脚本（支持 scene 结构）
    from typing import Tuple
    def validate_storyboards(sb_json: str) -> Tuple[bool, list, int]:
        """验证分镜脚本（scene 结构），返回 (是否有效, 错误列表, 有效数量)"""
        try:
            storyboards_data = json.loads(sb_json)
        except Exception as e:
            return False, [f"JSON 解析失败: {e}"], 0
        
        # 支持 scene 结构
        scenes = []
        if isinstance(storyboards_data, dict) and "scenes" in storyboards_data:
            scenes = storyboards_data["scenes"]
        elif isinstance(storyboards_data, list):
            # 兼容旧格式：如果是列表，假设是 scenes
            scenes = storyboards_data
        else:
            return False, ["分镜脚本必须是包含 scenes 的对象格式或 scenes 数组"], 0
        
        if not isinstance(scenes, list) or len(scenes) == 0:
            return False, ["scenes 数组为空，至少需要1个场景"], 0
        
        errors = []
        valid_scene_count = 0
        total_clips = 0
        
        # 检查每个 scene
        for scene_idx, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                errors.append(f"场景 {scene_idx + 1}: 不是有效的对象格式")
                continue
            
            scene_num = scene.get("scene_idx", scene_idx + 1)
            scene_begin = float(scene.get("begin_s", (scene_num - 1) * 10.0))
            scene_end = float(scene.get("end_s", scene_begin + 10.0))
            scene_duration = scene_end - scene_begin
            clips = scene.get("clips", [])
            
            # 检查 scene 时长是否恰好为10s
            if abs(scene_duration - 10.0) > 0.1:
                errors.append(f"场景 {scene_num}: 时长不是10s（{scene_duration:.1f}s），每个场景必须恰好10秒")
                continue
            
            # 检查 clips
            if not isinstance(clips, list) or len(clips) == 0:
                errors.append(f"场景 {scene_num}: 没有镜头（clips）")
                continue
            
            scene_valid = True
            for clip_idx, clip in enumerate(clips):
                if not isinstance(clip, dict):
                    errors.append(f"场景 {scene_num} 镜头 {clip_idx + 1}: 不是有效的对象格式")
                    scene_valid = False
                    continue
                
                desc = str(clip.get("desc", "")).strip()
                clip_num = clip.get("idx", clip_idx + 1)
                clip_begin = float(clip.get("begin_s", scene_begin))
                clip_end = float(clip.get("end_s", clip_begin + 3.0))
                clip_duration = clip_end - clip_begin
                
                # 检查描述是否为空
                if not desc:
                    errors.append(f"场景 {scene_num} 镜头 {clip_num}: 描述为空")
                    scene_valid = False
                    continue
                
                # 检查描述是否过短（至少 8 个字符）
                if len(desc) < 8:
                    errors.append(f"场景 {scene_num} 镜头 {clip_num}: 描述过短（长度: {len(desc)}），至少需要 8 个字符")
                    scene_valid = False
                    continue
                
                # 检查是否包含占位符
                placeholder_keywords = ["镜头描述", "描述待生成", "请填写", "占位", "placeholder", "待生成"]
                if any(keyword in desc for keyword in placeholder_keywords):
                    errors.append(f"场景 {scene_num} 镜头 {clip_num}: 包含占位符（'{desc[:30]}...'）")
                    scene_valid = False
                    continue
                
                # 检查时长是否超过10s
                if clip_duration > 10.0:
                    errors.append(f"场景 {scene_num} 镜头 {clip_num}: 时长超过10s（{clip_duration:.1f}s），每个镜头最长不超过10s")
                    scene_valid = False
                    continue
                
                # 检查 clip 时间是否在 scene 范围内
                if clip_begin < scene_begin or clip_end > scene_end:
                    errors.append(f"场景 {scene_num} 镜头 {clip_num}: 时间超出场景范围（{clip_begin:.1f}s-{clip_end:.1f}s 不在 {scene_begin:.1f}s-{scene_end:.1f}s 内）")
                    scene_valid = False
                    continue
                
                total_clips += 1
            
            if scene_valid:
                valid_scene_count += 1
        
        # 检查总时长是否匹配
        expected_scenes = max(1, int(total_duration / 10.0) + (1 if total_duration % 10.0 > 0 else 0))
        if len(scenes) != expected_scenes:
            errors.append(f"场景数量不匹配：期望 {expected_scenes} 个场景，实际 {len(scenes)} 个")
        
        # 必须所有 scene 都有效，且至少有一个有效镜头
        is_valid = len(errors) == 0 and valid_scene_count == len(scenes) and total_clips > 0
        return is_valid, errors, total_clips
    
    # 首次验证
    is_valid, errors, valid_count = validate_storyboards(storyboards_json)
    
    if is_valid:
        logger.info(f"[review_storyboard_impl] Storyboard validation passed: {valid_count} valid clips")
        return storyboards_json
    
    # 如果无效，尝试自动重写
    error_summary = "; ".join(errors[:5])
    if len(errors) > 5:
        error_summary += f" ... 还有 {len(errors) - 5} 个错误"
    
    logger.warning(f"[review_storyboard_impl] Storyboard validation failed: {error_summary}, attempting rewrite...")
    
    # 自动重写逻辑
    for retry in range(max_retries):
        try:
            # 调用 plan_storyboard_impl 重新生成
            retry_result = _run_async_safe(plan_storyboard_impl(goal, styles, total_duration, num_clips))
            
            # 验证重写结果
            is_valid, new_errors, new_valid_count = validate_storyboards(retry_result)
            
            if is_valid:
                logger.info(f"[review_storyboard_impl] Rewrite successful after {retry + 1} attempt(s)")
                return retry_result
            else:
                new_error_summary = "; ".join(new_errors[:3])
                logger.warning(f"[review_storyboard_impl] Rewrite attempt {retry + 1} still invalid: {new_error_summary}")
        except Exception as e:
            logger.error(f"[review_storyboard_impl] Rewrite attempt {retry + 1} failed: {e}", exc_info=True)
    
    # 所有重试都失败，返回错误信息
    logger.error(f"[review_storyboard_impl] All {max_retries} rewrite attempts failed")
    return json.dumps({
        "valid": False,
        "errors": errors,
        "valid_count": valid_count,
        "expected_count": num_clips,
        "retry_attempts": max_retries,
        "message": f"分镜脚本审核未通过，已重试 {max_retries} 次仍无效：{error_summary}。请检查分镜生成工具或调整参数。"
    }, ensure_ascii=False)


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
    return review_storyboard_impl(storyboards_json, num_clips, goal, styles, total_duration, max_retries)


@tool("规划分镜脚本工具")
def plan_storyboard_tool(goal: str, styles: List[str], total_duration: float, num_clips: int) -> str:
    """CrewAI Tool 封装：转调实现函数。"""
    return _run_async_safe(plan_storyboard_impl(goal, styles, total_duration, num_clips))


@tool("生成关键帧工具")
def generate_keyframe_tool(storyboards_json: str, image_control: bool = True) -> str:
    """
    为分镜脚本生成关键帧图片（首帧/尾帧）或为 scene 生成预览图片。
    
    Args:
        storyboards_json: JSON 格式的分镜脚本（支持 scene 结构或旧格式）
        image_control: 是否启用图片控制
        
    Returns:
        JSON 格式的更新后的分镜脚本（包含关键帧 URL 或 scene 图片 URL）
    """
    import json
    import asyncio
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    # 即使 image_control=False，如果是 scene 结构，也生成图片（用于前端展示）
    data = json.loads(storyboards_json)
    
    # 检查是否是 scene 结构
    is_scene_structure = isinstance(data, dict) and "scenes" in data
    
    # 如果是 scene 结构，总是生成图片（用于前端展示）
    if not image_control and not is_scene_structure:
        return storyboards_json
    
    image_provider = _get_image_provider()
    
    async def generate_keyframes():
        # 检查是否是 scene 结构
        if isinstance(data, dict) and "scenes" in data:
            # 新格式：scene 结构，为每个 scene 生成一张预览图片
            scenes = data["scenes"]
            updated_scenes = []
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
                    image_prompt = f"{scene_desc}，视频场景画面，无人脸、无真人，无人物形象"
                    image_url = await image_provider.generate(image_prompt)
                    scene["image_url"] = image_url
                    logger.info(f"[generate_keyframe_tool] Generated image for scene {scene_idx}: {image_url}")
                except Exception as e:
                    logger.warning(f"[generate_keyframe_tool] Failed to generate image for scene {scene_idx}: {e}")
                    scene["image_url"] = None
                updated_scenes.append(scene)
            return json.dumps({"scenes": updated_scenes}, ensure_ascii=False)
        elif isinstance(data, list):
            # 旧格式：storyboards 列表，为每个分镜生成首帧和尾帧
            updated_storyboards = []
            for sb in data:
                desc = sb.get("desc", "")
                # 为每个分镜生成首帧和尾帧
                # 【重要】避免生成带有人脸的图片，因为 sora2 不支持使用真人图片作为参考
                try:
                    in_prompt = f"{desc}，首帧画面，无人物、无人脸、无真人，避免出现任何人物形象"
                    out_prompt = f"{desc}，尾帧画面，无人物、无人脸、无真人，避免出现任何人物形象"
                    in_frame_url = await image_provider.generate(in_prompt)
                    out_frame_url = await image_provider.generate(out_prompt)
                    sb["keyframes"] = {"in": in_frame_url, "out": out_frame_url}
                except Exception as e:
                    logger.warning(f"[generate_keyframe_tool] Failed to generate keyframes for clip {sb.get('idx', 'unknown')}: {e}")
                    # 失败时保留原有 keyframes
                    sb["keyframes"] = sb.get("keyframes", {"in": None, "out": None})
                updated_storyboards.append(sb)
            return json.dumps(updated_storyboards, ensure_ascii=False)
        else:
            logger.warning(f"[generate_keyframe_tool] Unknown format, returning original")
            return storyboards_json
    
    return _run_async_safe(generate_keyframes())


def merge_storyboards_to_video_tasks_impl(storyboards_json: str, run_id: str, total_duration: float) -> str:
    """
    将分镜脚本（scene 结构）转换为视频任务的内部实现函数（可直接调用）。
    
    规则：
    1. 每个 scene 恰好10秒，直接作为一个视频任务
    2. 使用 scene 的描述结构（scene 内所有 clips 的描述）
    3. 每个视频任务对应一个 scene
    
    Args:
        storyboards_json: JSON 格式的分镜脚本（包含 scenes 数组）
        run_id: 运行 ID
        total_duration: 总时长（秒）
        
    Returns:
        JSON 格式的视频任务列表（每个任务对应一个 scene，每个 scene 恰好10s）
    """
    """
    将分镜脚本（scene 结构）转换为视频任务。
    
    规则：
    1. 每个 scene 恰好10秒，直接作为一个视频任务
    2. 使用 scene 的描述结构（scene 内所有 clips 的描述）
    3. 每个视频任务对应一个 scene
    
    Args:
        storyboards_json: JSON 格式的分镜脚本（包含 scenes 数组）
        run_id: 运行 ID
        total_duration: 总时长（秒）
        
    Returns:
        JSON 格式的视频任务列表（每个任务对应一个 scene，每个 scene 恰好10s）
    """
    import json
    import logging
    
    logger = logging.getLogger("crewai_tools")
    
    try:
        storyboards_raw = json.loads(storyboards_json)
    except Exception as e:
        logger.error(f"[merge_storyboards_to_video_tasks_impl] Failed to parse JSON: {e}")
        return json.dumps([], ensure_ascii=False)
    
    # 处理 scene 结构
    scenes = []
    if isinstance(storyboards_raw, dict) and "scenes" in storyboards_raw:
        scenes = storyboards_raw["scenes"]
    elif isinstance(storyboards_raw, list):
        # 兼容旧格式：如果是列表，假设是 scenes
        scenes = storyboards_raw
    else:
        logger.error(f"[merge_storyboards_to_video_tasks_impl] Invalid format: expected scenes structure, got {type(storyboards_raw)}")
        return json.dumps([], ensure_ascii=False)
    
    # 按 scene_idx 排序
    scenes.sort(key=lambda x: int(x.get("scene_idx", 0)))
    
    # 每个 scene 转换为一个视频任务
    video_tasks = []
    for scene in scenes:
        scene_idx = scene.get("scene_idx", len(video_tasks) + 1)
        scene_begin = float(scene.get("begin_s", (scene_idx - 1) * 10.0))
        scene_end = float(scene.get("end_s", scene_begin + 10.0))
        clips = scene.get("clips", [])
        
        # 确保 scene 时长恰好为10s
        if abs(scene_end - scene_begin - 10.0) > 0.1:
            scene_end = scene_begin + 10.0
        
        # 构建 scene 的描述：合并所有 clips 的描述
        clip_descriptions = []
        for clip in clips:
            if isinstance(clip, dict) and clip.get("desc"):
                desc = str(clip.get("desc", "")).strip()
                if desc and len(desc) >= 3:
                    clip_descriptions.append(desc)
        
        # 如果没有有效的描述，使用默认描述
        if not clip_descriptions:
            clip_descriptions = [f"场景{scene_idx}视频内容"]
        
        # 合并描述（用分号连接）
        scene_desc = "；".join(clip_descriptions)
        
        # 获取关键帧（使用第一个 clip 的关键帧）
        keyframes = {"in": None, "out": None}
        if clips and isinstance(clips[0], dict):
            keyframes = clips[0].get("keyframes", keyframes)
        
        # 获取 scene 的图片（优先使用 scene 的 image_url）
        scene_image_url = scene.get("image_url")
        
        # 获取 scene 的旁白文案（narration）
        scene_narration = scene.get("narration", "").strip()
        
        # 如果 scene 有 image_url，将其设置为 keyframes 的 "in"
        if scene_image_url:
            keyframes["in"] = scene_image_url
            logger.info(f"[merge_storyboards_to_video_tasks_impl] Using scene image_url for scene {scene_idx}: {scene_image_url}")
        
        video_tasks.append({
            "task_idx": scene_idx,  # 使用 scene_idx 作为 task_idx
            "scene_idx": scene_idx,
            "desc": scene_desc,
            "narration": scene_narration,  # 保存完整的旁白文案
            "voice_params": scene.get("voice_params", {}),
            "clips": clips,  # 保留所有 clips 信息
            "total_duration": 10.0,  # 每个 scene 恰好10s
            "begin_s": scene_begin,
            "end_s": scene_end,
            "keyframes": keyframes,
            "image_url": scene_image_url  # 也单独保存 scene 的 image_url，方便后续使用
        })
    
    logger.info(
        f"[merge_storyboards_to_video_tasks_impl] Converted {len(scenes)} scenes into {len(video_tasks)} video tasks "
        f"(target: {total_duration}s, actual: {sum(t['total_duration'] for t in video_tasks):.1f}s)"
    )
    
    return json.dumps(video_tasks, ensure_ascii=False)


@tool("合并镜头为视频任务工具")
def merge_storyboards_to_video_tasks_tool(storyboards_json: str, run_id: str, total_duration: float) -> str:
    """
    将分镜脚本（scene 结构）转换为视频任务。
    
    规则：
    1. 每个 scene 恰好10秒，直接作为一个视频任务
    2. 使用 scene 的描述结构（scene 内所有 clips 的描述）
    3. 每个视频任务对应一个 scene
    
    Args:
        storyboards_json: JSON 格式的分镜脚本（包含 scenes 数组）
        run_id: 运行 ID
        total_duration: 总时长（秒）
        
    Returns:
        JSON 格式的视频任务列表（每个任务对应一个 scene，每个 scene 恰好10s）
    """
    return merge_storyboards_to_video_tasks_impl(storyboards_json, run_id, total_duration)


async def generate_video_clip_impl(video_tasks_json: str, run_id: str) -> str:
    """
    为视频任务提交视频生成任务的内部实现函数（可直接调用，异步版本）。
    
    注意：视频生成需要 3-5 分钟，此工具只负责提交任务到数据库，
    实际生成由后台任务或 webhook 完成。返回任务提交状态。
    
    Args:
        video_tasks_json: JSON 格式的视频任务列表（由合并工具生成，每个任务对应一个10s的视频片段）
        run_id: 运行 ID，用于文件命名
        
    Returns:
        JSON 格式的任务提交结果列表（包含 task_id，状态为 "pending"）
    """
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
    
    # 记录视频提供商信息
    try:
        video_provider = _get_video_provider()
        provider_type = type(video_provider).__name__
        logger.info(
            f"[generate_video_clip_impl] Video provider initialized: "
            f"type={provider_type}, "
            f"video_tasks_count={len(video_tasks_raw) if isinstance(video_tasks_raw, list) else 0}, "
            f"run_id={run_id}"
        )
    except Exception as e:
        logger.error(f"[generate_video_clip_impl] Failed to get video provider: {e}", exc_info=True)
        raise
    
    # 处理输入格式
    if isinstance(video_tasks_raw, list):
        video_tasks = video_tasks_raw
    else:
        logger.error(f"[generate_video_clip_impl] Invalid video_tasks format: {type(video_tasks_raw)}")
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
        
        # 获取前一个 scene 的信息（用于转场衔接）
        prev_scene_info = None
        if index > 0 and task_idx > 1:
            prev_task = video_tasks[index - 1] if index - 1 < len(video_tasks) else None
            if prev_task:
                prev_clips = prev_task.get("clips", [])
                if prev_clips and isinstance(prev_clips, list) and len(prev_clips) > 0:
                    # 获取前一个 scene 的最后一个 clip 的描述
                    last_clip = prev_clips[-1] if isinstance(prev_clips[-1], dict) else {}
                    prev_clip_desc = last_clip.get("desc", "").strip()
                    prev_scene_desc = prev_task.get("desc", "").strip()
                    if prev_clip_desc or prev_scene_desc:
                        prev_scene_info = {
                            "desc": prev_scene_desc or prev_clip_desc,
                            "last_clip": prev_clip_desc
                        }
        
        # 优先使用 scene 的描述（desc），这是合并了所有 clips 的描述
        scene_desc = task.get("desc", "").strip()
        clips = task.get("clips", [])
        
        if scene_desc and len(scene_desc) >= 3:
            # 使用 scene 的描述（已经在 merge_storyboards_to_video_tasks_tool 中合并了所有 clips）
            base_prompt = scene_desc
        elif clips and isinstance(clips, list) and len(clips) > 0:
            # 如果没有 scene 描述，合并所有 clips 的描述（用分号连接）
            clip_descriptions = []
            for clip in clips:
                if isinstance(clip, dict):
                    clip_desc = str(clip.get("desc", "")).strip()
                    if clip_desc and len(clip_desc) >= 3:
                        clip_descriptions.append(clip_desc)
            base_prompt = "；".join(clip_descriptions) if clip_descriptions else f"场景{task_idx}视频内容"
        else:
            # 降级：如果都没有，使用默认描述
            base_prompt = f"场景{task_idx}视频内容"
            logger.warning(f"[generate_video_clip_impl] Task {task_idx} has no desc or clips, using default prompt")
        
        # 构建增强的 prompt，包含转场信息
        if prev_scene_info and task_idx > 1:
            # 添加前一个 scene 的信息，确保视觉连贯性
            transition_note = f"【转场衔接】前一个场景的结尾画面：{prev_scene_info.get('last_clip', prev_scene_info.get('desc', ''))}。"
            transition_note += f"当前场景应该从相似的视觉元素、色调或构图自然过渡，确保视觉流畅，避免突兀的跳跃。"
            prompt = f"{transition_note} {base_prompt}"
            logger.info(f"[generate_video_clip_impl] Added transition context for task {task_idx}: {prev_scene_info.get('last_clip', 'N/A')[:50]}")
        else:
            prompt = base_prompt
        
        # 获取时长（每个 scene 恰好10s）
        duration = max(1, min(10, int(round(task.get("total_duration", 10.0)))))
        
        # 获取参考图（优先使用 scene 的 image_url，其次使用 keyframes 的 "in"）
        ref_img = None
        
        # 优先使用 scene 的 image_url
        scene_image_url = task.get("image_url")
        if scene_image_url:
            ref_img = scene_image_url
            logger.info(f"[generate_video_clip_impl] Using scene image_url as ref_img for task {task_idx}: {ref_img}")
        else:
            # 降级：使用 keyframes 的 "in"
            keyframes = task.get("keyframes", {})
            if clips and isinstance(clips, list) and len(clips) > 0:
                first_clip = clips[0] if isinstance(clips[0], dict) else {}
                keyframes = first_clip.get("keyframes", keyframes)
            ref_img = keyframes.get("in") if keyframes else None
            if ref_img:
                logger.info(f"[generate_video_clip_impl] Using keyframes.in as ref_img for task {task_idx}: {ref_img}")
            else:
                logger.warning(f"[generate_video_clip_impl] No ref_img found for task {task_idx} (no scene image_url or keyframes.in)")
        
        # 验证 prompt 是否为空或包含占位符
        if not prompt or len(prompt) < 3:
            error_msg = f"视频任务描述为空或过短（长度: {len(prompt)}），无法生成视频。原始数据: {task}"
            logger.error(f"[generate_video_clip_impl] {error_msg} for task {task_idx}")
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
            logger.warning(f"[generate_video_clip_impl] {error_msg} for task {task_idx}")
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
                        f"[generate_video_clip_impl] Task added to Supabase queue: "
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
                        f"[generate_video_clip_impl] Failed to add to Supabase queue: {e}, "
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
                logger.info(
                    f"[generate_video_clip_impl] Submitting video generation task: "
                    f"task_idx={task_idx}, "
                    f"prompt_length={len(prompt)}, "
                    f"has_ref_img={bool(ref_img)}, "
                    f"duration={duration}, "
                    f"provider_type={type(video_provider).__name__}"
                )
                res = await video_provider.generate(prompt, ref_img or "", duration=duration, async_mode=True)
                logger.info(
                    f"[generate_video_clip_impl] Video provider response: "
                    f"task_idx={task_idx}, "
                    f"result_type={type(res)}, "
                    f"result={res}"
                )
                
                if isinstance(res, dict) and res.get("pending"):
                    # 异步任务模式：返回 task_id，等待 webhook 回调或后续轮询
                    task_id_runninghub = res.get("task_id")
                    logger.info(f"[generate_video_clip_impl] Submitted async task for task {task_idx}: task_id={task_id_runninghub}, prompt_length={len(prompt)}")
                    
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
                                    f"[generate_video_clip_impl] Failed to save task to database: "
                                    f"run_id={run_id}, task_idx={task_idx}, runninghub_task_id={task_id_runninghub}"
                                )
                                raise Exception("Failed to save task to database")
                            
                            logger.info(
                                f"[generate_video_clip_impl] Saved direct submission to video_tasks: "
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
                            f"[generate_video_clip_impl] Failed to save direct submission to database: {e}, "
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
                    logger.info(f"[generate_video_clip_impl] Generated task {task_idx} synchronously: {cdn_url}")
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
                    logger.error(f"[generate_video_clip_impl] {error_msg} for task {task_idx}")
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
                        f"[generate_video_clip_impl] Task queue full for task {task_idx}, "
                        f"retrying in {delay}s (attempt {attempt + 1}/{max_retries + 1})"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    # 其他错误或重试次数用完，返回失败
                    logger.error(
                        f"[generate_video_clip_impl] Failed to submit task for task {task_idx}: {e}",
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
        
        logger.info(f"[generate_video_clip_impl] Starting to submit {len(video_tasks)} video tasks")
        
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
                    logger.warning(f"[generate_video_clip_impl] Task idx {task_idx} already submitted, skipping duplicate")
                    continue
                
                submitted_indices.add(task_idx)
                
                result = await run_with_sem(task, idx)
                results.append(result)
                
                # 如果任务成功提交（pending），等待一小段时间再提交下一个，避免队列瞬间满载
                if result.get("status") == "pending":
                    logger.info(f"[generate_video_clip_impl] Task {result.get('task_idx', idx + 1)} submitted, waiting 2s before next...")
                    await asyncio.sleep(2)  # 等待2秒，给队列一些缓冲时间
                
                # 如果队列满，等待更长时间再继续
                if result.get("status") == "failed" and "TASK_QUEUE_MAXED" in str(result.get("error", "")):
                    logger.warning(f"[generate_video_clip_impl] Queue full for task {result.get('task_idx', idx + 1)}, waiting 10s before next submission...")
                    await asyncio.sleep(10)  # 队列满时等待更长时间
            except Exception as e:
                logger.error(f"[generate_video_clip_impl] Error submitting task {idx + 1}: {e}", exc_info=True)
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
        
        logger.info(f"[generate_video_clip_impl] Submitted {len(results)} video tasks (expected {len(video_tasks)})")
        return results
    
    results = await submit_all()
    
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
                logger.info(f"[generate_video_clip_impl] Saved task_ids to database: {task_ids}")
    except Exception as e:
        logger.warning(f"[generate_video_clip_impl] Failed to save to database: {e}")
    
    return json.dumps(results, ensure_ascii=False)


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
    return _run_async_safe(generate_video_clip_impl(video_tasks_json, run_id))


@tool("拼接视频工具")
def stitch_video_tool(clip_results_json: str, run_id: str) -> str:
    """
    将多个视频片段拼接为最终视频。
    
    【重要】此工具的行为：
    1. 首先检查 crew_sessions 表，如果 status 为 "completed" 且有 result，直接返回 result 字段的 URL。
    2. 如果视频片段状态为 "pending" 或 "submitted"，说明任务还在处理中，此时无法拼接。
    3. 工具会抛出 RuntimeError 异常，明确说明任务还在处理中。
    4. 系统会注册回调，当所有任务完成时自动触发拼接。
    5. 只有在所有视频片段状态为 "succeeded" 时，才会返回最终视频的 CDN URL。
    6. 如果工具抛出异常，调用者必须如实返回异常信息，不要自己生成或猜测 URL。
    
    Args:
        clip_results_json: JSON 格式的视频片段结果列表
        run_id: 运行 ID，用于文件命名
        
    Returns:
        最终视频的 CDN URL（仅当所有视频片段完成时，或从 crew_sessions 表获取）
        
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
    
    # 首先检查 crew_sessions 表，如果已经完成，直接返回 result
    try:
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        if supabase_url and supabase_key:
            from supabase import create_client
            supabase = create_client(supabase_url, supabase_key)
            
            session_result = supabase.table("crew_sessions")\
                .select("status, result")\
                .eq("run_id", run_id)\
                .execute()
            
            if session_result.data and len(session_result.data) > 0:
                session = session_result.data[0]
                status = session.get("status", "")
                result = session.get("result", "")
                
                # 如果状态是 completed 且有 result，直接返回
                if status == "completed" and result:
                    logger.info(
                        f"[stitch_video_tool] Found completed session in crew_sessions: "
                        f"run_id={run_id}, result={result[:100]}"
                    )
                    return result
                
                # 如果状态是 stitching，说明正在拼接，等待完成
                if status == "stitching":
                    logger.info(
                        f"[stitch_video_tool] Session is stitching, waiting for completion: run_id={run_id}"
                    )
                    # 等待一段时间后再次检查
                    import time
                    for _ in range(60):  # 最多等待 5 分钟（60 * 5秒）
                        time.sleep(5)
                        session_result = supabase.table("crew_sessions")\
                            .select("status, result")\
                            .eq("run_id", run_id)\
                            .execute()
                        if session_result.data and len(session_result.data) > 0:
                            session = session_result.data[0]
                            if session.get("status") == "completed" and session.get("result"):
                                logger.info(
                                    f"[stitch_video_tool] Stitch completed, got result: {session.get('result')[:100]}"
                                )
                                return session.get("result")
    except Exception as e:
        logger.debug(f"[stitch_video_tool] Failed to check crew_sessions: {e}")
        # 继续执行，不阻塞
    
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
                from .runninghub_client import RunningHubClient
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


@tool("旁白合成工具")
def synthesize_voice_tool(scene_idx: int, narration: str, voice_id: str, emotion: str, speed: float, vol: float, pitch: int, run_id: str) -> str:
    """
    根据旁白文本与语音参数生成语音（MP3）与字幕（SRT），并上传到 R2。
    返回包含音频与字幕 URL 的 JSON。
    """
    return _run_async_safe(synthesize_voice_impl(scene_idx, narration, voice_id, emotion, speed, vol, pitch, run_id))


async def synthesize_voice_impl(scene_idx: int, narration: str, voice_id: str, emotion: str, speed: float, vol: float, pitch: int, run_id: str) -> str:
    import os, httpx, json, logging, re
    from .r2 import get_r2_client
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 Minimax API（MINIMAX_API_KEY）")
    logger = logging.getLogger("crewai_tools.synthesize_voice")
    audio_bytes: bytes = b""
    subtitle_bytes: bytes = b""
    meaningful = re.sub(r"\s+", "", str(narration or ""))
    if len(meaningful) < 20:
        raise RuntimeError("旁白文本长度不足20字")
    logger.info(
        f"[synthesize_voice_tool] Calling Minimax API for scene_idx={scene_idx}: "
        f"voice_id={voice_id}, emotion={emotion}, speed={speed}, vol={vol}, pitch={pitch}, text={narration}..."
    )
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            url = "https://api.minimaxi.com/v1/t2a_v2"
            speech_model = os.getenv("MINIMAX_SPEECH_MODEL", "speech-2.6-turbo")
            payload = {
                "model": speech_model,
                "text": narration,
                "voice_setting": {
                    "voice_id": voice_id,
                    "speed": float(speed),
                    "vol": float(vol),
                    "pitch": int(pitch),
                    "emotion": emotion,
                },
                "audio_setting": {
                    "sample_rate": 32000,
                    "bitrate": 128000,
                    "format": "mp3",
                    "channel": 2,
                },
                "stream": False,
                # 如果服务支持 URL 输出，尝试设置（不影响兼容）
                "output_format": "url",
                # 使能字幕输出，由服务端生成字幕并返回 URL（若支持）
                "subtitle_enable": True,
            }
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
            try:
                br = data.get("base_resp") or {}
                sc = (br.get("status_code") or data.get("status_code"))
                sm = (br.get("status_msg") or data.get("status_msg"))
                if sc or sm:
                    logger.info(f"[synthesize_voice_tool] base_resp: code={sc}, msg={sm}")
            except Exception:
                pass
            audio_url = (data.get("audio_url") or (data.get("data") or {}).get("audio_url") or
                         ((data.get("audio_file") or {}).get("url")))
            audio_any = (data.get("audio") or (data.get("data") or {}).get("audio"))
            subtitle_url = (
                data.get("subtitle_url") or
                (data.get("data") or {}).get("subtitle_url") or
                ((data.get("subtitle_file") or {}).get("url")) or
                (data.get("subtitle_file") if isinstance(data.get("subtitle_file"), str) else None) or
                (((data.get("data") or {}).get("subtitle_file")) if isinstance((data.get("data") or {}).get("subtitle_file"), str) else None)
            )
            if audio_url:
                ra = await client.get(audio_url)
                ra.raise_for_status()
                audio_bytes = ra.content
            elif audio_any:
                s = str(audio_any).strip()
                if s.startswith("http://") or s.startswith("https://"):
                    ra = await client.get(s)
                    ra.raise_for_status()
                    audio_bytes = ra.content
                else:
                    try:
                        audio_bytes = bytes.fromhex(s)
                    except Exception:
                        import base64
                        try:
                            audio_bytes = base64.b64decode(s)
                        except Exception:
                            raise RuntimeError("同步 TTS 返回的 audio 非 URL/HEX/Base64")
            else:
                raise RuntimeError("同步 TTS 未返回音频内容")
            
            def json_to_srt(json_text: str) -> str:
                try:
                    arr = json.loads(json_text)
                    if isinstance(arr, dict):
                        arr = arr.get("subtitles") or arr.get("segments") or arr.get("list") or []
                    lines = []
                    idx = 1
                    def fmt(t: float) -> str:
                        h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60); ms = int(round((t - int(t)) * 1000))
                        return f"{h:02}:{m:02}:{s:02},{ms:03}"
                    for item in arr if isinstance(arr, list) else []:
                        if not isinstance(item, (dict,)):
                            continue
                        st = item.get("begin_time") or item.get("start") or item.get("begin") or item.get("from")
                        et = item.get("end_time") or item.get("end") or item.get("to")
                        txt = item.get("text") or item.get("content") or item.get("sentence") or item.get("word") or ""
                        try:
                            stf = float(st)
                            etf = float(et)
                        except Exception:
                            continue
                        if txt is None:
                            txt = ""
                        # 过滤仅由标点/空白组成的片段（避免生成看似“空白”的字幕条目）
                        import re
                        cleaned = re.sub(r"[\s:;，。；、！!？?－—~·…·，]+", "", str(txt))
                        if not cleaned:
                            continue
                        lines.append(f"{idx}\n{fmt(stf)} --> {fmt(etf)}\n{str(txt)}\n")
                        idx += 1
                    if lines:
                        return "\n".join(lines)
                except Exception:
                    pass
                return ""
            if subtitle_url:
                rs = await client.get(subtitle_url)
                if rs.status_code == 200 and rs.content:
                    try:
                        srt = json_to_srt(rs.text)
                    except Exception:
                        srt = ""
                    if srt:
                        subtitle_bytes = srt.encode("utf-8")
                    else:
                        subtitle_bytes = rs.content
            
    except Exception as e:
        logger.error(f"[synthesize_voice] 同步合成失败: {e}", exc_info=True)
        audio_bytes = b""
    # 兜底：如果音频为空或过小，判定为失败，避免上传无效音频
    if not audio_bytes or len(audio_bytes) < 10000:
        raise RuntimeError("同步 TTS 返回的音频为空或过小")
    def make_srt(text: str, total_s: float = 10.0, spd: float = 1.0) -> str:
        import re
        parts = [p.strip() for p in re.split(r"[，。；、：:,!?.]", text) if p.strip()]
        if not parts:
            parts = [text.strip()] if text.strip() else ["旁白"]
        parts = parts[:4]
        weights = [max(1.0, len(p) / max(0.5, spd)) for p in parts]
        total_w = sum(weights)
        durations = [total_s * (w / total_w) for w in weights]
        min_seg = 1.2
        need = sum(max(0.0, min_seg - d) for d in durations)
        if need > 0 and total_s > 0:
            extra_pool = sum(max(0.0, d - min_seg) for d in durations)
            if extra_pool > 0:
                factor = need / extra_pool
                durations = [max(min_seg, d - max(0.0, d - min_seg) * factor) for d in durations]
            else:
                n = len(durations)
                durations = [total_s / n for _ in durations]
        sum_d = sum(durations)
        if sum_d > 0:
            durations = [d * (total_s / sum_d) for d in durations]
        def fmt(t: float) -> str:
            h = int(t // 3600); m = int((t % 3600) // 60); s = int(t % 60); ms = int(round((t - int(t)) * 1000))
            return f"{h:02}:{m:02}:{s:02},{ms:03}"
        lines = []
        cur = 0.0
        for i, (p, d) in enumerate(zip(parts, durations), start=1):
            start = cur
            end = min(total_s, cur + d)
            lines.append(f"{i}\n{fmt(start)} --> {fmt(end)}\n{p}\n")
            cur = end
        return "\n".join(lines)
    if not audio_bytes or len(audio_bytes) == 0:
        raise RuntimeError("Minimax 未返回有效音频，直连/回退均失败")
    if subtitle_bytes:
        try:
            raw = subtitle_bytes.decode("utf-8", errors="ignore")
            if raw.strip().startswith("[") or raw.strip().startswith("{"):
                srt_conv = json_to_srt(raw)
                srt_text = srt_conv if srt_conv else make_srt(narration, 10.0, float(speed))
            else:
                srt_text = raw
            # 如果转成的 SRT 仍然缺少有效文本，回退按旁白生成
            import re
            if not re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", srt_text or ""):
                srt_text = make_srt(narration, 10.0, float(speed))
        except Exception:
            srt_text = make_srt(narration, 10.0, float(speed))
    else:
        srt_text = make_srt(narration, 10.0, float(speed))
    r2 = get_r2_client()
    if not r2:
        raise RuntimeError("R2 未配置")
    bucket = os.getenv("R2_BUCKET", "video")
    audio_key = f"{run_id}_scene_{scene_idx}_vo.mp3"
    subs_key = f"{run_id}_scene_{scene_idx}.srt"
    # 统一 SRT 行尾为 CRLF，并加 UTF-8 BOM，避免浏览器/播放器误判编码
    srt_norm = "\r\n".join(srt_text.replace("\r\n", "\n").splitlines())
    srt_bytes = b"\xEF\xBB\xBF" + srt_norm.encode("utf-8")
    r2.put_object(Bucket=bucket, Key=audio_key, Body=audio_bytes, ContentType="audio/mpeg", CacheControl="no-cache, no-store, must-revalidate")
    r2.put_object(Bucket=bucket, Key=subs_key, Body=srt_bytes, ContentType="application/x-subrip; charset=utf-8", CacheControl="no-cache, no-store, must-revalidate")
    public_base = os.getenv("R2_PUBLIC_BASE")
    account_id = os.getenv("R2_ACCOUNT_ID")
    def puburl(key: str) -> str:
        if public_base:
            return f"{public_base.rstrip('/')}/{key}"
        if account_id:
            return f"https://pub-{account_id}.r2.dev/{key}"
        raise RuntimeError("R2_PUBLIC_BASE 或 R2_ACCOUNT_ID 未配置")
    return json.dumps({"audio_url": puburl(audio_key), "subtitle_url": puburl(subs_key)}, ensure_ascii=False)


@tool("背景音乐合成工具")
def synthesize_bgm_tool(bgm_prompt: str, run_id: str) -> str:
    """
    根据提示生成背景音乐（MP3），并上传到 R2。返回包含 BGM URL 的 JSON。
    """
    return _run_async_safe(synthesize_bgm_impl(bgm_prompt, run_id))


async def synthesize_bgm_impl(bgm_prompt: str, run_id: str) -> str:
    import os, httpx, json, logging
    from .r2 import get_r2_client
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("未配置 Minimax API（MINIMAX_API_KEY）")
    logger = logging.getLogger("crewai_tools.synthesize_bgm")
    audio_bytes: bytes = b""
    try:
        async with httpx.AsyncClient(timeout=600) as client:
            url = "https://api.minimaxi.com/v1/music_generation"
            lyrics_hint = (
                "[Intro]\n纯器乐，无人声。氛围柔和，铺底延迟与暖垫。\n"
                "[Verse]\n纯器乐，无人声。节奏克制，突出轻微律动与质感。\n"
                "[Chorus]\n纯器乐，无人声。能量略微提升，柔和主旋律淡入。\n"
                "[Bridge]\n纯器乐，无人声。纹理变化，加入细微琶音与过渡。\n"
                "[Outro]\n纯器乐，无人声。逐步收束，元素淡出，尾部渐隐。\n"
            )
            payload = {
                "model": "music-2.0",
                "prompt": bgm_prompt,
                # 初始直接请求 URL 输出；若服务端不支持，后续回退去掉此参数
                "output_format": "url",
                "stream": False,
                "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
                # 结构化“器乐-only”歌词，满足参数约束但不引入人声
                "lyrics": lyrics_hint,
            }
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 404:
                url = "https://api.minimaxi.chat/v1/music_generation"
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
            if resp.status_code >= 400:
                txt = resp.text[:300]
                lyrics_hint = "[Intro]\nInstrumental background, no vocals.\n[Verse]\nInstrumental background, no vocals.\n[Chorus]\nInstrumental background, no vocals.\n[Outro]\nInstrumental background, no vocals."
                payload_fallback = {"model": "music-2.0", "prompt": bgm_prompt, "stream": False, "lyrics": lyrics_hint}
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload_fallback,
                )
            resp.raise_for_status()
            jd = resp.json()
            data = jd.get("data") or {}
            # 兼容返回：data.audio_url 或 data.audio（可能是 URL 或 HEX）
            audio_url = jd.get("audio_url") or data.get("audio_url") or ((jd.get("audio_file") or {}).get("url"))
            audio_any = jd.get("audio") or data.get("audio")
            if audio_url:
                r = await client.get(audio_url)
                r.raise_for_status()
                audio_bytes = r.content
            elif audio_any:
                s = str(audio_any).strip()
                if s.startswith("http://") or s.startswith("https://"):
                    r = await client.get(s)
                    r.raise_for_status()
                    audio_bytes = r.content
                else:
                    try:
                        audio_bytes = bytes.fromhex(s)
                    except Exception:
                        import base64
                        try:
                            audio_bytes = base64.b64decode(s)
                        except Exception:
                            raise RuntimeError("音乐生成返回的 audio 非 URL/HEX/Base64")
            else:
                raise RuntimeError("Minimax 音乐生成未返回音频（audio_url/audio）")
    except Exception as e:
        import tempfile, subprocess
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.close()
        cmd = [
            "ffmpeg", "-f", "lavfi", "-t", "30", "-i", "sine=frequency=440:sample_rate=32000",
            "-filter:a", "volume=0.2",
            "-c:a", "libmp3lame", "-b:a", "128k", "-y", tmp.name
        ]
        subprocess.run(cmd, capture_output=True)
        with open(tmp.name, "rb") as f:
            audio_bytes = f.read()
        logger.warning(f"[synthesize_bgm] music-2.0 合成失败，回退 bytes={len(audio_bytes)}: {e}")
    r2 = get_r2_client()
    if not r2:
        raise RuntimeError("R2 未配置")
    bucket = os.getenv("R2_BUCKET", "video")
    bgm_key = f"{run_id}_bgm.mp3"
    r2.put_object(Bucket=bucket, Key=bgm_key, Body=audio_bytes, ContentType="audio/mpeg", CacheControl="no-cache, no-store, must-revalidate")
    public_base = os.getenv("R2_PUBLIC_BASE")
    account_id = os.getenv("R2_ACCOUNT_ID")
    if public_base:
        bgm_url = f"{public_base.rstrip('/')}/{bgm_key}"
    elif account_id:
        bgm_url = f"https://pub-{account_id}.r2.dev/{bgm_key}"
    else:
        raise RuntimeError("R2_PUBLIC_BASE 或 R2_ACCOUNT_ID 未配置")
    return json.dumps({"bgm_url": bgm_url}, ensure_ascii=False)

