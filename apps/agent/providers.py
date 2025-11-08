import os
import asyncio
from typing import Protocol


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
    """选择图片 Provider，默认 qwen_runninghub，未配置时回退 Mock。"""
    provider = (os.getenv("PROVIDER_IMAGE") or "qwen_runninghub").lower()
    if provider == "qwen_runninghub":
        try:
            from .providers_image_qwen_runninghub import QwenRunningHubImageProvider
            return QwenRunningHubImageProvider()
        except Exception:
            return MockImageProvider()
    elif provider == "seedream":
        try:
            from .providers_image_seedream import SeedreamImageProvider
            return SeedreamImageProvider()
        except Exception:
            return MockImageProvider()
    elif provider == "nanobanana":
        try:
            from .providers_image_nanobanana import NanoBananaImageProvider
            return NanoBananaImageProvider()
        except Exception:
            return MockImageProvider()
    return MockImageProvider()


def get_video_provider() -> VideoProvider:
    """选择视频 Provider，默认 pixverse，未配置时回退 Mock。"""
    provider = (os.getenv("PROVIDER_VIDEO") or "pixverse").lower()
    if provider == "pixverse":
        try:
            from .providers_video_pixverse import PixVerseVideoProvider
            return PixVerseVideoProvider()
        except Exception:
            return MockVideoProvider()
    elif provider == "runninghub":
        try:
            from .providers_video_runninghub import RunningHubVideoProvider
            return RunningHubVideoProvider()
        except Exception:
            return MockVideoProvider()
    elif provider in {"sora2", "veo3.1", "hailuo"}:
        # 预留占位，未来实现
        return MockVideoProvider()
    return MockVideoProvider()


