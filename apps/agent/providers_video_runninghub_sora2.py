import asyncio
import os
import logging
from typing import Optional
import httpx

from runninghub_client import RunningHubClient, RunningHubError

logger = logging.getLogger("crewai_tools")


class RunningHubSora2VideoProvider:
    def __init__(self) -> None:
        self.workflow_id = os.getenv("RUNNINGHUB_SORA2_WORKFLOW_ID", "1985261217524629506")
        if not self.workflow_id:
            raise RuntimeError("缺少 RUNNINGHUB_SORA2_WORKFLOW_ID 或 workflow id")
        self.client = RunningHubClient()

    async def _maybe_upload_image(self, image_url: Optional[str]) -> Optional[str]:
        if not image_url:
            return None
        # 如果是 http(s) 外链，RunningHub 可直接使用；否则尝试下载再上传
        if image_url.startswith("http://") or image_url.startswith("https://"):
            return image_url
        # 尝试把本地路径下载为 bytes 并上传
        # 在常见使用里前端会传公网 URL，这里作为兜底逻辑
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                resp = await c.get(image_url)
                if resp.status_code == 200:
                    content = resp.content
                    file_name = image_url.split("/")[-1] or "image.png"
                    stored = await self.client.upload_bytes(content, file_name, file_type="input")
                    return stored  # 返回 RunningHub 内部 fileName
        except Exception:
            pass
        # 回退为原始传入
        return image_url

    async def generate(self, prompt: str, image_url: Optional[str], duration: int = 10, async_mode: bool = False) -> dict:
        """
        生成视频
        
        Args:
            prompt: 提示词
            image_url: 参考图片 URL
            duration: 视频时长（秒）
            async_mode: 是否异步模式。如果为 True，创建任务后立即返回 pending 状态，不轮询结果
        
        Returns:
            如果 async_mode=False: 返回 {"video_url": "...", "task_id": "..."}
            如果 async_mode=True: 返回 {"pending": True, "task_id": "..."}
        """
        # 验证 prompt 不能为空
        prompt = str(prompt).strip() if prompt else ""
        if not prompt or len(prompt) < 3:
            raise RunningHubError(f"Prompt must be a non-empty string (got: '{prompt[:50]}...', length: {len(prompt)})")
        
        # 组装 nodeInfoList（根据用户说明）
        # node id=40，fieldName=image -> 使用上传后的 fileName 或外链 URL
        # node id=41，fieldName=prompt
        image_ref = await self._maybe_upload_image(image_url)
        node_info_list = []
        if image_ref:
            node_info_list.append({"nodeId": "40", "fieldName": "image", "fieldValue": image_ref})
        node_info_list.append({"nodeId": "41", "fieldName": "prompt", "fieldValue": prompt})
        # 目前该工作流固定 10s，后续可根据 duration 切换不同 workflow 或增加时长节点
        _ = duration

        # 创建任务
        logger.info(
            f"[RunningHubSora2VideoProvider] Creating task: "
            f"workflow_id={self.workflow_id}, "
            f"prompt_length={len(prompt)}, "
            f"has_image={bool(image_ref)}, "
            f"duration={duration}, "
            f"async_mode={async_mode}, "
            f"node_info_list={node_info_list}"
        )
        task_id = await self.client.create_task(self.workflow_id, node_info_list)
        logger.info(
            f"[RunningHubSora2VideoProvider] Task created successfully: "
            f"task_id={task_id}, workflow_id={self.workflow_id}"
        )
        
        # 如果异步模式，立即返回 pending 状态
        if async_mode:
            return {"pending": True, "task_id": task_id}

        # 同步模式：轮询状态，成功后获取 outputs 中的视频链接
        video_url: Optional[str] = None
        for _ in range(120):  # 最长轮询约 10 分钟（5s/次，120次 * 5s = 600s = 10min）
            status = await self.client.get_status(task_id)
            if status in {"SUCCESS"}:
                outputs = await self.client.get_outputs(task_id)
                for item in outputs:
                    # 增强视频直链解析：兼容多种字段名
                    url = (
                        item.get("fileUrl") 
                        or item.get("url") 
                        or item.get("ossUrl") 
                        or item.get("downloadUrl")
                        or (item.get("value") if isinstance(item.get("value"), str) else None)
                    )
                    ftype = (item.get("fileType") or item.get("type") or "").lower()
                    # 检查是否为视频：URL 包含 mp4 或 fileType 为 mp4/video
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
                    break
            if status in {"FAILED", "ERROR"}:
                raise RunningHubError("Sora2 任务失败，请检查工作流与入参")
            await asyncio.sleep(5)

        if not video_url:
            raise RunningHubError("未在超时时间内获得视频结果")

        return {"video_url": video_url, "task_id": task_id}


