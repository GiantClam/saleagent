import os
import asyncio
import httpx


# 参考 RunningHub 文档：工作流完整接入示例
# https://s.apifox.cn/b860476a-b4d0-4aa5-91b8-6dcaa18d6c7d/doc-7534195

class RunningHubVideoProvider:
    async def generate(self, prompt: str, image_url: str, duration: int = 6):
        api_key = os.getenv("RUNNINGHUB_API_KEY")
        workflow_id = os.getenv("RUNNINGHUB_WORKFLOW_ID")
        if not api_key or not workflow_id:
            raise RuntimeError("RunningHub 环境变量未配置：RUNNINGHUB_API_KEY、RUNNINGHUB_WORKFLOW_ID")
        use_webhook = (os.getenv("RUNNINGHUB_MODE", "poll").lower() == "webhook")

        # 1) 可选：上传参考图（若提供 image_url 是公网 URL，可直接在节点中引用）
        # 这里直接将 image_url 作为节点输入，实际可根据工作流节点定义调整

        # 2) 构造 nodeInfoList（视你的工作流而定，这里给出常见字段示例）
        node_info_list = [
            {
                "nodeId": "prompt",  # 示例：请替换为你的工作流里对应节点 ID/名称
                "fieldName": "text",
                "fieldValue": prompt,
            },
        ]
        if image_url:
            node_info_list.append({
                "nodeId": "image",
                "fieldName": "url",
                "fieldValue": image_url,
            })

        # 3) 提交任务
        async with httpx.AsyncClient(timeout=180) as client:
            submit = await client.post(
                "https://www.runninghub.cn/task/openapi/create",
                headers={"Content-Type": "application/json"},
                json={
                    "apiKey": api_key,
                    "workflowId": workflow_id,
                    "nodeInfoList": node_info_list
                },
            )
            submit.raise_for_status()
            task = submit.json().get("data", {})
            task_id = task.get("taskId") or task.get("id")
            if not task_id:
                raise RuntimeError(f"提交任务失败：{submit.text}")

            if use_webhook:
                # 直接返回 pending，由外部 webhook 完成
                return {"pending": True, "task_id": task_id}

            # 4) 轮询任务状态（fallback）
            video_url = None
            for _ in range(120):  # 最长轮询约 10 分钟（按 5s/次，120次 * 5s = 600s = 10min）
                status_resp = await client.post(
                    "https://www.runninghub.cn/task/openapi/query",
                    headers={"Content-Type": "application/json"},
                    json={
                        "apiKey": api_key,
                        "taskId": task_id
                    },
                )
                status_resp.raise_for_status()
                data = status_resp.json().get("data")
                if isinstance(data, dict) and data.get("status") in {"success", "finished", "done"}:
                    outputs = data.get("outputs") or data.get("result") or []
                    # 寻找视频文件链接
                    for item in outputs:
                        url = item.get("fileUrl") or item.get("url")
                        ftype = (item.get("fileType") or "").lower()
                        if url and ("mp4" in url or ftype in {"mp4", "video"}):
                            video_url = url
                            break
                    break
                await asyncio.sleep(5)

            if not video_url:
                raise RuntimeError("未在超时时间内获得视频结果")
            return {"video_url": video_url}


