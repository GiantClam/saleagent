import os
import asyncio
from typing import Protocol
from dotenv import load_dotenv

# 加载 .env 文件（确保在导入时就能读取环境变量）
load_dotenv()


class ImageProvider(Protocol):
    async def generate(self, prompt: str) -> str: ...


class VideoProvider(Protocol):
    async def generate(self, prompt: str, image_url: str, duration: int = 6) -> str: ...


class MockImageProvider:
    async def generate(self, prompt: str) -> str:
        await asyncio.sleep(1.0)
        return "https://picsum.photos/seed/ai-cover/1200/630"


class MockVideoProvider:
    async def generate(self, prompt: str, image_url: str, duration: int = 6) -> str:
        await asyncio.sleep(2.0)
        return "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"


def get_image_provider() -> ImageProvider:
    """选择图片 Provider，默认 qwen_runninghub，未配置时抛出错误。"""
    provider = (os.getenv("PROVIDER_IMAGE") or "qwen_runninghub").lower()
    if provider == "qwen_runninghub":
        from providers_image_qwen_runninghub import QwenRunningHubImageProvider
        # 实例化时会检查环境变量，如果未配置会抛出异常
        return QwenRunningHubImageProvider()
    elif provider == "seedream":
        from providers_image_seedream import SeedreamImageProvider
        return SeedreamImageProvider()
    elif provider == "nanobanana":
        from providers_image_nanobanana import NanoBananaImageProvider
        return NanoBananaImageProvider()
    raise ValueError(f"不支持的图片 Provider: {provider}")


def get_video_provider() -> VideoProvider:
    """选择视频 Provider，默认 pixverse，未配置时抛出错误。"""
    provider = (os.getenv("PROVIDER_VIDEO") or "pixverse").lower()
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


