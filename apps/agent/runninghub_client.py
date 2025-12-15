import os
import asyncio
import httpx
from typing import Any, Dict, List, Optional, Tuple


class RunningHubError(RuntimeError):
    pass


def _assert_ok(resp: httpx.Response) -> Dict[str, Any]:
    if resp.status_code != 200:
        raise RunningHubError(f"HTTP {resp.status_code}: {resp.text}")
    data = resp.json()
    # 按文档：code == 0 表示成功
    code = data.get("code")
    if code not in (0, None):
        msg = data.get("msg") or data.get("message", "")
        raise RunningHubError(f"API code={code}: {msg}")
    return data


class RunningHubClient:
    def __init__(self, api_key: Optional[str] = None, *, max_retries: int = 3, backoff_base: float = 0.5, timeout: Optional[httpx.Timeout] = None) -> None:
        key = api_key or os.getenv("RUNNINGHUB_API_KEY")
        if not key:
            raise RunningHubError("缺少 RUNNINGHUB_API_KEY")
        self.api_key = key
        # 不在初始化时创建客户端，避免事件循环绑定问题
        self._client: Optional[httpx.AsyncClient] = None
        self._client_loop_id: Optional[int] = None
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.1, float(backoff_base))
        # 增加超时时间，避免 ConnectTimeout
        self.timeout = timeout or httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 httpx 客户端，确保绑定到当前事件循环"""
        try:
            current_loop = asyncio.get_running_loop()
            current_loop_id = id(current_loop)
        except RuntimeError:
            # 没有运行中的事件循环，创建新的
            current_loop_id = None
        
        # 如果客户端不存在，或者绑定到了不同的事件循环，创建新的
        if self._client is None or self._client_loop_id != current_loop_id:
            # 关闭旧的客户端
            if self._client is not None:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            
            # 创建新的客户端
            self._client = httpx.AsyncClient(timeout=self.timeout)
            self._client_loop_id = current_loop_id
        
        return self._client

    async def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        c = await self._get_client()
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                # 使用实例的超时配置
                resp = await c.post(
                    url, 
                    headers={"Content-Type": "application/json"}, 
                    json=payload
                )
                return _assert_ok(resp)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, httpx.ConnectTimeout) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                await asyncio.sleep(self.backoff_base * (2 ** attempt))
            except Exception:
                raise
        raise RunningHubError(f"请求失败：{type(last_err).__name__}: {last_err}")

    async def _post_files(self, url: str, files: Dict[str, Any]) -> Dict[str, Any]:
        c = await self._get_client()
        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await c.post(url, files=files)
                return _assert_ok(resp)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException, httpx.ConnectTimeout) as e:
                last_err = e
                if attempt >= self.max_retries:
                    break
                # 指数退避，但最大等待时间不超过 30 秒
                wait_time = min(self.backoff_base * (2 ** attempt), 30.0)
                await asyncio.sleep(wait_time)
            except Exception:
                raise
        raise RunningHubError(f"上传失败：{type(last_err).__name__}: {last_err}")

    async def create_task(self, workflow_id: str, node_info_list: List[Dict[str, Any]]) -> str:
        import logging
        logger = logging.getLogger("crewai_tools")
        
        logger.info(
            f"[RunningHubClient] Creating task via API: "
            f"url=https://www.runninghub.cn/task/openapi/create, "
            f"workflow_id={workflow_id}, "
            f"node_info_list_count={len(node_info_list)}, "
            f"api_key_length={len(self.api_key) if self.api_key else 0}"
        )
        
        payload = {
            "apiKey": self.api_key, 
            "workflowId": workflow_id, 
            "nodeInfoList": node_info_list
        }
        logger.info(f"[RunningHubClient] Request payload: {payload}")
        
        try:
            data = await self._post_json(
                "https://www.runninghub.cn/task/openapi/create",
                payload,
            )
            logger.info(f"[RunningHubClient] API response received: {data}")
            
            d = data.get("data") or {}
            task_id = d.get("taskId") or d.get("id")
            if not task_id:
                logger.error(f"[RunningHubClient] Missing taskId in response: {data}")
                raise RunningHubError(f"创建任务返回缺少 taskId：{data}")
            
            logger.info(f"[RunningHubClient] Task created successfully: task_id={task_id}")
            return str(task_id)
        except Exception as e:
            logger.error(
                f"[RunningHubClient] Failed to create task: {e}, "
                f"workflow_id={workflow_id}, "
                f"node_info_list={node_info_list}",
                exc_info=True
            )
            raise

    async def get_status(self, task_id: str) -> str:
        data = await self._post_json(
            "https://www.runninghub.cn/task/openapi/status",
            {"apiKey": self.api_key, "taskId": task_id},
        )
        # data: "QUEUED","RUNNING","FAILED","SUCCESS";
        status = (data.get("data") or "").upper()
        
        return status

    async def get_outputs(self, task_id: str) -> List[Dict[str, Any]]:
        data = await self._post_json(
            "https://www.runninghub.cn/task/openapi/outputs",
            {"apiKey": self.api_key, "taskId": task_id},
        )
        outputs = data.get("data") or []
        if not isinstance(outputs, list):
            raise RunningHubError("outputs 不是数组")
        return outputs

    async def upload_bytes(self, content: bytes, filename: str, file_type: str = "input") -> str:
        # multipart form
        form = {
            "apiKey": (None, self.api_key),
            "fileType": (None, file_type),
            "file": (filename, content),
        }
        data = await self._post_files("https://www.runninghub.cn/task/openapi/upload", form)
        d = data.get("data") or {}
        file_name = d.get("fileName")
        if not file_name:
            raise RunningHubError(f"上传返回缺少 fileName：{data}")
        return file_name

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


