import os
import asyncio
import httpx
import logging
import json
from typing import Dict, Any, Optional, List
from .base import BaseGenerator

logger = logging.getLogger("workflow")

class ComfyUIGenerator(BaseGenerator):
    """
    Standard generator for platforms using ComfyUI Open API (e.g., RunningHub, Liblib.art).
    """
    def __init__(self, 
                 platform_name: str,
                 api_key: str,
                 workflow_id: str,
                 node_mappings: Dict[str, Any],
                 base_url: str):
        self.platform_name = platform_name
        self.api_key = api_key
        self.workflow_id = workflow_id
        self.node_mappings = node_mappings
        self.base_url = base_url

    async def generate(self, **kwargs) -> Dict[str, Any]:
        """
        Translates generic parameters (prompt, image_url) into ComfyUI nodeInfoList.
        """
        node_info_list = []
        
        # Map prompt
        if "prompt" in kwargs and "prompt" in self.node_mappings:
            mapping = self.node_mappings["prompt"]
            node_info_list.append({
                "nodeId": mapping["nodeId"],
                "fieldName": mapping["fieldName"],
                "fieldValue": kwargs["prompt"]
            })
            
        # Map image
        if ("image_url" in kwargs or "image" in kwargs) and "image" in self.node_mappings:
            mapping = self.node_mappings["image"]
            url = kwargs.get("image_url") or kwargs.get("image")
            node_info_list.append({
                "nodeId": mapping["nodeId"],
                "fieldName": mapping["fieldName"],
                "fieldValue": url
            })

        # Platform specific submission logic (simplified to RunningHub style for now)
        # In a real scenario, base_url would point to the platform's creation endpoint.
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                logger.info(f"[{self.platform_name}] Submitting task to {self.base_url}")
                payload = {
                    "apiKey": self.api_key,
                    "workflowId": self.workflow_id,
                    "nodeInfoList": node_info_list
                }
                
                resp = await client.post(f"{self.base_url}/task/openapi/create", json=payload)
                data = resp.json()
                
                if resp.status_code != 200 or data.get("code") != 0:
                    return {"status": "failed", "error": data.get("msg") or f"HTTP {resp.status_code}"}
                
                task_id = data.get("data", {}).get("taskId") or data.get("data", {}).get("id")
                return {"status": "pending", "task_id": task_id}
                
        except Exception as e:
            logger.error(f"[{self.platform_name}] Submit exception: {e}")
            return {"status": "failed", "error": str(e)}

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        # Implementation depends on platform, but mostly similar to RH
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                payload = {"apiKey": self.api_key, "taskId": task_id}
                resp = await client.post(f"{self.base_url}/task/openapi/status", json=payload)
                data = resp.json()
                
                status = str(data.get("data") or "").upper()
                if status == "SUCCESS":
                    # Fetch outputs
                    res_resp = await client.post(f"{self.base_url}/task/openapi/outputs", json=payload)
                    res_data = res_resp.json()
                    outputs = res_data.get("data") or []
                    
                    # Search for video/image URL
                    url = None
                    for item in outputs:
                        url = item.get("fileUrl") or item.get("url")
                        if url: break # Return the first found URL
                    
                    return {"status": "success", "url": url}
                elif status in ("FAILED", "ERROR"):
                    return {"status": "failed", "error": "Platform reported failure"}
                else:
                    return {"status": "pending"}
        except Exception as e:
            return {"status": "failed", "error": str(e)}
