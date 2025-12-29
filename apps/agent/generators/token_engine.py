import os
import asyncio
import httpx
import logging
from typing import Dict, Any, Optional, List
from .base import BaseGenerator

logger = logging.getLogger("workflow")

class TokenEngineGenerator(BaseGenerator):
    """
    Implementation of the Sora2 Token Engine API.
    URL: https://sora.aotiai.com/api
    """
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("SORA2_TOKEN_ENGINE_KEY")
        self.base_url = "https://sora.aotiai.com/api"
        if not self.api_key:
            logger.warning("SORA2_TOKEN_ENGINE_KEY not configured")

    def _get_headers(self):
        return {
            "X-Partner-Key": self.api_key,
            "Content-Type": "application/json"
        }

    async def generate(self, 
                       prompt: str, 
                       model: str = "sora-2", 
                       orientation: str = "landscape", 
                       size: str = "large", 
                       duration: int = 10,
                       images: Optional[List[str]] = None,
                       **kwargs) -> Dict[str, Any]:
        
        # Consolidation of image parameters
        if not images:
            img = kwargs.get("image_url") or kwargs.get("ref_img")
            if img:
                images = [img]
        
        if not self.api_key:
            return {"status": "failed", "error": "API Key missing"}

        payload = {
            "prompt": prompt,
            "model": model,
            "orientation": orientation,
            "size": size,
            "duration": duration,
        }
        if images:
            payload["images"] = images

        url = f"{self.base_url}/partner/video/generate"
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                logger.info(f"[TokenEngine] Submitting task: {model} - {prompt[:50]}...")
                resp = await client.post(url, headers=self._get_headers(), json=payload)
                data = resp.json()
                
                if resp.status_code != 200 or data.get("code") != 0:
                    error_msg = data.get("message") or f"HTTP {resp.status_code}"
                    logger.error(f"[TokenEngine] Submit failed: {error_msg}")
                    return {"status": "failed", "error": error_msg}
                
                task_id = data.get("data", {}).get("id")
                return {"status": "pending", "task_id": task_id}
                
        except Exception as e:
            logger.error(f"[TokenEngine] Exception during submit: {e}")
            return {"status": "failed", "error": str(e)}

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        url = f"{self.base_url}/partner/video/status/{task_id}"
        
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._get_headers())
                data = resp.json()
                
                if resp.status_code != 200 or data.get("code") != 0:
                    return {"status": "failed", "error": data.get("message")}
                
                res_data = data.get("data", {})
                status = res_data.get("status")
                
                if status == "completed":
                    return {
                        "status": "success",
                        "url": res_data.get("videoUrl"),
                        "progress": 100
                    }
                elif status == "failed":
                    return {
                        "status": "failed",
                        "error": res_data.get("errorMessage") or "Unknown failure"
                    }
                else:
                    return {
                        "status": "pending",
                        "progress": res_data.get("progress", 0)
                    }
                    
        except Exception as e:
            logger.error(f"[TokenEngine] Status check failed: {e}")
            return {"status": "failed", "error": str(e)}
