import os
import asyncio
import httpx


# 通过 RunningHub 调用 Qwen 图片编辑/生成工作流
# 文档参考：工作流完整接入示例
# https://s.apifox.cn/b860476a-b4d0-4aa5-91b8-6dcaa18d6c7d/doc-7534195

class QwenRunningHubImageProvider:
    def __init__(self):
        """初始化时检查环境变量，如果未配置则抛出异常。"""
        api_key = os.getenv("RUNNINGHUB_API_KEY")
        # 优先使用 RUNNINGHUB_IMAGE_WORKFLOW_ID，如果没有则使用 RUNNINGHUB_WORKFLOW_ID
        workflow_id = os.getenv("RUNNINGHUB_IMAGE_WORKFLOW_ID") or os.getenv("RUNNINGHUB_WORKFLOW_ID")
        if not api_key:
            raise RuntimeError("RunningHub 环境变量未配置：RUNNINGHUB_API_KEY")
        if not workflow_id:
            raise RuntimeError("RunningHub 环境变量未配置：RUNNINGHUB_IMAGE_WORKFLOW_ID 或 RUNNINGHUB_WORKFLOW_ID")
        self.api_key = api_key
        self.workflow_id = workflow_id

    async def generate(self, prompt: str) -> str:
        node_info_list = [
            {"nodeId": "3", "fieldName": "text", "fieldValue": prompt}
        ]

        async with httpx.AsyncClient(timeout=120) as client:
            submit = await client.post(
                "https://www.runninghub.cn/task/openapi/create",
                headers={"Content-Type": "application/json"},
                json={
                    "apiKey": self.api_key,
                    "workflowId": self.workflow_id,
                    "nodeInfoList": node_info_list
                },
            )
            
            # 检查 HTTP 状态码
            if submit.status_code != 200:
                error_text = submit.text
                try:
                    error_json = submit.json()
                    error_code = error_json.get("code")
                    error_msg = error_json.get("msg") or error_json.get("message", "")
                    if error_code == 412 and "TOKEN_INVALID" in error_msg:
                        raise RuntimeError(
                            f"RunningHub API Key 无效或已过期。"
                            f"请检查环境变量 RUNNINGHUB_API_KEY 是否正确。"
                            f"错误详情：{error_msg}"
                        )
                    else:
                        raise RuntimeError(
                            f"提交任务失败 (HTTP {submit.status_code})：{error_msg or error_text}"
                        )
                except ValueError:
                    # 如果不是 JSON 响应
                    raise RuntimeError(
                        f"提交任务失败 (HTTP {submit.status_code})：{error_text}"
                    )
            
            submit.raise_for_status()
            submit_data = submit.json()
            
            # 检查 API 返回的业务状态码
            if submit_data.get("code") and submit_data.get("code") != 200:
                error_code = submit_data.get("code")
                error_msg = submit_data.get("msg") or submit_data.get("message", "")
                if error_code == 412 and "TOKEN_INVALID" in error_msg:
                    raise RuntimeError(
                        f"RunningHub API Key 无效或已过期。"
                        f"请检查环境变量 RUNNINGHUB_API_KEY 是否正确。"
                        f"错误详情：{error_msg}"
                    )
                else:
                    raise RuntimeError(
                        f"提交任务失败 (code: {error_code})：{error_msg}"
                    )
            
            data = submit_data.get("data", {})
            task_id = data.get("taskId") or data.get("id")
            if not task_id:
                raise RuntimeError(f"提交任务失败：未返回 taskId。响应：{submit.text}")

            image_url = None
            # 轮询任务状态：/task/openapi/status
            for _ in range(60):
                status_resp = await client.post(
                    "https://www.runninghub.cn/task/openapi/status",
                    headers={"Content-Type": "application/json"},
                    json={
                        "apiKey": self.api_key,
                        "taskId": task_id
                    },
                )

                # 检查 HTTP 状态码
                if status_resp.status_code != 200:
                    error_text = status_resp.text
                    try:
                        error_json = status_resp.json()
                        error_code = error_json.get("code")
                        error_msg = error_json.get("msg") or error_json.get("message", "")
                        if error_code == 412 and "TOKEN_INVALID" in (error_msg or ""):
                            raise RuntimeError(
                                f"RunningHub API Key 无效或已过期。"
                                f"请检查环境变量 RUNNINGHUB_API_KEY 是否正确。"
                                f"错误详情：{error_msg}"
                            )
                        else:
                            raise RuntimeError(
                                f"查询任务状态失败 (HTTP {status_resp.status_code})：{error_msg or error_text}"
                            )
                    except ValueError:
                        raise RuntimeError(
                            f"查询任务状态失败 (HTTP {status_resp.status_code})：{error_text}"
                        )

                status_data = status_resp.json()

                # 检查 API 业务码（0 为成功）
                if status_data.get("code") not in (0, None):
                    error_code = status_data.get("code")
                    error_msg = status_data.get("msg") or status_data.get("message", "")
                    raise RuntimeError(
                        f"查询任务状态失败 (code: {error_code})：{error_msg}"
                    )

                task_status = (status_data.get("data") or "").upper()
                if task_status in {"SUCCESS"}:
                    # 成功：拉取结果
                    outputs_resp = await client.post(
                        "https://www.runninghub.cn/task/openapi/outputs",
                        headers={"Content-Type": "application/json"},
                        json={
                            "apiKey": self.api_key,
                            "taskId": task_id
                        },
                    )
                    if outputs_resp.status_code != 200:
                        raise RuntimeError(
                            f"获取任务结果失败 (HTTP {outputs_resp.status_code})：{outputs_resp.text}"
                        )
                    outputs_data = outputs_resp.json()
                    if outputs_data.get("code") not in (0, None):
                        raise RuntimeError(
                            f"获取任务结果失败 (code: {outputs_data.get('code')})：{outputs_data.get('msg') or outputs_data.get('message','')}"
                        )
                    outputs = outputs_data.get("data") or []
                    for item in outputs:
                        url = item.get("fileUrl") or item.get("url")
                        ftype = (item.get("fileType") or "").lower()
                        if url and (ftype in {"png", "jpg", "jpeg"} or any(url.endswith(ext) for ext in [".png", ".jpg", ".jpeg"])):
                            image_url = url
                            break
                    break
                elif task_status in {"FAILED"}:
                    raise RuntimeError("任务失败：请检查工作流与入参")

                await asyncio.sleep(3)

            if not image_url:
                raise RuntimeError("未在超时时间内获得图片结果")
            return image_url


