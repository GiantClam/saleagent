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


from generators.token_engine import TokenEngineGenerator
from generators.comfyui import ComfyUIGenerator
from generators.fallback import MultiGenerator
import json

def _load_workflow_config():
    config_path = os.path.join(os.path.dirname(__file__), "configs", "workflows.json")
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            return json.load(f)
    return {}

def get_video_provider() -> VideoProvider:
    """
    Returns a MultiGenerator configured with prioritized video providers.
    """
    provider_env = (os.getenv("PROVIDER_VIDEO") or "runninghub").lower()
    
    if provider_env == "mock":
        return MockVideoProvider()
        
    config = _load_workflow_config()
    generators = []
    
    # Primary: RunningHub
    rh_key = os.getenv("RUNNINGHUB_API_KEY")
    rh_workflow = config.get("runninghub", {}).get("workflows", {}).get("sora2")
    if rh_key and rh_workflow:
        generators.append(ComfyUIGenerator(
            "RunningHub", rh_key, rh_workflow["workflow_id"], 
            rh_workflow["nodes"], config["runninghub"]["base_url"]
        ))
        
    # Backup 1: Liblib.art
    ll_key = os.getenv("LIBLIB_API_KEY")
    ll_workflow = config.get("liblib", {}).get("workflows", {}).get("standard")
    if ll_key and ll_workflow:
        generators.append(ComfyUIGenerator(
            "Liblib", ll_key, ll_workflow["workflow_id"], 
            ll_workflow["nodes"], config["liblib"]["base_url"]
        ))
        
    # Backup 2: Token Engine (Sora2)
    te_key = os.getenv("SORA2_TOKEN_ENGINE_KEY")
    if te_key:
        generators.append(TokenEngineGenerator(te_key))
        
    if not generators:
        logger.warning("[providers] No video generators configured, using Mock")
        return MockVideoProvider()
        
    return MultiGenerator(generators)


