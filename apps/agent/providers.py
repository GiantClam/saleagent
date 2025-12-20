import os
import asyncio
import logging
from typing import Protocol
from dotenv import load_dotenv

# 加载 .env 文件（确保在导入时就能读取环境变量）
load_dotenv()


class ImageProvider(Protocol):
    async def generate(self, prompt: str) -> str: ...


class VideoProvider(Protocol):
    async def generate(self, prompt: str, image_url: str, duration: int = 6) -> str: ...



# MockImageProvider deleted as per request

class MockVideoProvider:
    async def generate(self, prompt: str, image_url: str, duration: int = 6, **kwargs) -> str:
        await asyncio.sleep(0.8)
        return "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"


def get_image_provider() -> ImageProvider:
    """选择图片 Provider。必须通过 PROVIDER_IMAGE 环境变量配置，否则报错。"""
    provider_name = os.getenv("PROVIDER_IMAGE")
    if not provider_name:
         raise ValueError("环境变量未配置：PROVIDER_IMAGE")
    
    provider = provider_name.lower()
    logger = logging.getLogger("workflow")

    if provider == "qwen_runninghub":
        from providers_image_scene_runninghub import SceneRunningHubImageProvider
        return SceneRunningHubImageProvider()
    elif provider == "seedream":
        from providers_image_seedream import SeedreamImageProvider
        return SeedreamImageProvider()
    elif provider == "nanobanana":
        from providers_image_nanobanana import NanoBananaImageProvider
        return NanoBananaImageProvider()
    
    raise ValueError(f"不支持的图片 Provider: {provider}")


def get_video_provider() -> VideoProvider:
    """选择视频 Provider，默认 pixverse；支持 mock；初始化失败则回退 mock。"""
    provider = (os.getenv("PROVIDER_VIDEO") or "pixverse").lower()
    logger = logging.getLogger("workflow")
    if provider == "mock":
        logger.info("[providers] Using MockVideoProvider")
        return MockVideoProvider()
    try:
        if provider == "pixverse":
            from providers_video_pixverse import PixVerseVideoProvider
            return PixVerseVideoProvider()
        elif provider == "runninghub":
            from providers_video_runninghub import RunningHubVideoProvider
            return RunningHubVideoProvider()
        elif provider == "sora2":
            from providers_video_runninghub_sora2 import RunningHubSora2VideoProvider
            return RunningHubSora2VideoProvider()
        elif provider in {"veo3.1", "hailuo"}:
            raise ValueError(f"Provider {provider} 尚未实现")
        raise ValueError(f"不支持的视频 Provider: {provider}")
    except Exception as e:
        logger.warning(f"[providers] Video provider '{provider}' init failed: {e}; fallback to mock")
        return MockVideoProvider()


