import os
import httpx


# 通过 RunningHub 调用 Qwen 图片编辑/生成工作流
# 文档参考：工作流完整接入示例
# https://s.apifox.cn/b860476a-b4d0-4aa5-91b8-6dcaa18d6c7d/doc-7534195

class QwenRunningHubImageProvider:
    async def generate(self, prompt: str) -> str:
        api_key = os.getenv("RUNNINGHUB_API_KEY")
        workflow_id = os.getenv("RUNNINGHUB_IMAGE_WORKFLOW_ID", os.getenv("RUNNINGHUB_WORKFLOW_ID"))
        if not api_key or not workflow_id:
            raise RuntimeError("RunningHub 环境变量未配置：RUNNINGHUB_API_KEY、RUNNINGHUB_IMAGE_WORKFLOW_ID")

        node_info_list = [
            {"nodeId": "prompt", "fieldName": "text", "fieldValue": prompt}
        ]

        async with httpx.AsyncClient(timeout=120) as client:
            submit = await client.post(
                "https://www.runninghub.cn/api/open/v1/workflow/submitTask",
                headers={"Content-Type": "application/json", "apiKey": api_key},
                json={"workflowId": workflow_id, "nodeInfoList": node_info_list},
            )
            submit.raise_for_status()
            data = submit.json().get("data", {})
            task_id = data.get("taskId") or data.get("id")
            if not task_id:
                raise RuntimeError(f"提交任务失败：{submit.text}")

            image_url = None
            for _ in range(60):
                status_resp = await client.post(
                    "https://www.runninghub.cn/api/open/v1/workflow/queryTaskOutputs",
                    headers={"Content-Type": "application/json", "apiKey": api_key},
                    json={"taskId": task_id},
                )
                status_resp.raise_for_status()
                sdata = status_resp.json().get("data")
                if isinstance(sdata, dict) and sdata.get("status") in {"success", "finished", "done"}:
                    outputs = sdata.get("outputs") or []
                    for item in outputs:
                        url = item.get("fileUrl") or item.get("url")
                        ftype = (item.get("fileType") or "").lower()
                        if url and (ftype in {"png", "jpg", "jpeg"} or any(url.endswith(ext) for ext in [".png", ".jpg", ".jpeg"])):
                            image_url = url
                            break
                    break
                await httpx.AsyncClient().aclose()
                await httpx.sleep(3)

            if not image_url:
                raise RuntimeError("未在超时时间内获得图片结果")
            return image_url


