import os
import asyncio
import httpx
import logging


# 通过 RunningHub 调用 Qwen 图片编辑/生成工作流
# 文档参考：工作流完整接入示例
# https://s.apifox.cn/b860476a-b4d0-4aa5-91b8-6dcaa18d6c7d/doc-7534195

class SceneRunningHubImageProvider:
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
        import logging
        logger = logging.getLogger("workflow")
        node_info_list = [
            {"nodeId": "3", "fieldName": "text", "fieldValue": prompt}
        ]

        async with httpx.AsyncClient(timeout=120) as client:
            logger.info(
                f"[RunningHub] image.generate request: workflow_id={self.workflow_id}, node_info_list={node_info_list}"
            )
            submit = await client.post(
                "https://www.runninghub.cn/task/openapi/create",
                headers={"Content-Type": "application/json", "Host": "www.runninghub.cn"},
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
            logger.info(f"[RunningHub] image.generate task created: task_id={task_id}")

            image_url = None
            # 轮询任务状态：/task/openapi/status
            # 轮询任务状态
            for _ in range(60):
                await asyncio.sleep(3)
                
                try:
                    task_status = await client.get_status(task_id)
                    logger.info(f"[RunningHub] image.generate status: {task_status}")
                except Exception as e:
                    logger.warning(f"[RunningHub] get_status failed: {e}")
                    continue

                if task_status == "SUCCESS":
                    # 成功：拉取结果
                    logger.info(f"[RunningHub] Task {task_id} SUCCESS, fetching outputs...")
                    outputs_resp = await client.post(
                        "https://www.runninghub.cn/task/openapi/outputs",
                        headers={"Content-Type": "application/json"},
                        json={
                            "apiKey": self.api_key,
                            "taskId": task_id
                        },
                    )
                    
                    if outputs_resp.status_code != 200:
                        raise RuntimeError(f"获取任务结果失败 (HTTP {outputs_resp.status_code})：{outputs_resp.text}")
                        
                    outputs_data = outputs_resp.json()
                    if outputs_data.get("code") not in (0, None):
                        raise RuntimeError(f"获取任务结果失败 (code: {outputs_data.get('code')})：{outputs_data.get('msg') or outputs_data.get('message','')}")
                        
                    outputs = outputs_data.get("data") or []
                    for item in outputs:
                        url = item.get("fileUrl") or item.get("url")
                        ftype = (item.get("fileType") or "").lower()
                        if url and (ftype in {"png", "jpg", "jpeg"} or any(url.endswith(ext) for ext in [".png", ".jpg", ".jpeg"])):
                            image_url = url
                            break
                    break

                elif task_status == "FAILED":
                    try:
                        query_resp = await client.post(
                            "https://www.runninghub.cn/task/openapi/query",
                            headers={"Content-Type": "application/json"},
                            json={
                                "apiKey": self.api_key,
                                "taskId": task_id
                            },
                        )
                        jr = query_resp.json() if query_resp.status_code == 200 else {"status": query_resp.status_code, "text": query_resp.text}
                        logger.warning(f"[RunningHub] image.generate FAILED detail: {jr}")
                    except Exception:
                        pass
                    raise RuntimeError("任务失败：请检查工作流与入参")
            
            if not image_url:
                raise RuntimeError("未在超时时间内获得图片结果")
            return image_url

    async def generate_scene(self, image_url: str, text: str, timeout_minutes: int = 8) -> dict:
        """基于用户输入图片与场景文字，调用 RunningHub 工作流生成分镜头图片与描述。

        返回 {"image_url": <图片URL>, "desc_text": <文字描述>}。
        """
        from runninghub_client import RunningHubClient, RunningHubError
        import httpx
        client = RunningHubClient(api_key=os.getenv("RUNNINGHUB_API_KEY"))
        # 直接使用当前 provider 的 workflow_id（来自 RUNNINGHUB_IMAGE_WORKFLOW_ID）
        workflow_id = self.workflow_id
        # 固定追加提示词
        fixed_hint = (
            f"根据参考图生成产品的广告片分镜 6格分镜图 影视大片感。"
            f"要求：图片中的商品宽高比例、瓶子形状、外观及细节请务必保持不变。"
            f"画面没有字幕、没有中文，如果画面中出现中文字那么中文字的书写要准确无误，人物特征要保持一致性，人物清晰且没有崩坏"
        )
        if "6格分镜图" not in text:
            text = f"{fixed_hint}。{text}。"

        # 处理参考图：RunningHub 支持两类输入
        # 1) 平台内部相对路径（如 'api/xxxx.jpg'）——直接作为节点值传入
        # 2) 公网 http(s) URL ——尽量下载并上传为 RunningHub 内部 fileName，提升兼容性
        async def _maybe_upload_image(url: str) -> str:
            u = str(url or "").strip()
            if not u:
                return u
            
            if u.startswith("http://") or u.startswith("https://"):
                try:
                    async with httpx.AsyncClient(timeout=60) as c:
                        resp = await c.get(u)
                        if resp.status_code == 200 and resp.content:
                            file_name = (u.split("/")[-1] or "image.png")
                            try:
                                stored = await client.upload_bytes(resp.content, file_name, file_type="input")
                                
                                logging.getLogger("workflow").info(f"[RunningHub] Uploaded external image: {stored}")
                                return stored
                            except Exception as e:
                                logging.getLogger("workflow").warning(f"[RunningHub] upload_bytes failed, fallback to original URL: {e}")
                except Exception as e:
                    logging.getLogger("workflow").warning(f"[RunningHub] download image failed, fallback to original URL: {e}")
            return u

        image_ref = await _maybe_upload_image(image_url)
        # 提交节点：image -> nodeId=21，text -> nodeId=3
        node_info_list = [
            {"nodeId": "21", "fieldName": "image", "fieldValue": image_ref},
            {"nodeId": "3", "fieldName": "text", "fieldValue": text},
        ]
        logging.getLogger("workflow").info(
            f"[RunningHub] generate_scene request: workflow_id={workflow_id}, image_ref={image_ref}, text={text}, node_info_list={node_info_list}"
        )
        task_id = await client.create_task(workflow_id, node_info_list)
        # 轮询状态，最长 timeout_minutes 分钟
        max_iters = int((timeout_minutes * 60) / 5)
        img_url = None
        desc_text = None
        for _ in range(max_iters):
            st = await client.get_status(task_id)
            if st == "SUCCESS":
                outs = await client.get_outputs(task_id)
                for it in outs:
                    # Search for a valid HTTP URL across potential keys
                    candidates = [
                        it.get("fileUrl"),
                        it.get("value") if isinstance(it.get("value"), str) else None
                    ]
                    
                    url = None
                    for cand in candidates:
                        if cand and isinstance(cand, str):
                            cand_s = cand.strip()
                            if cand_s.lower().startswith(("http://", "https://")):
                                url = cand_s
                                break
                    
                    if not url:
                        # No valid URL found in this item
                        continue

                    ul = str(url).lower()


                    if any(ul.endswith(ext) for ext in [".png", ".jpg", ".jpeg"]):
                        img_url = url
                    elif any(ul.endswith(ext) for ext in [".json", ".txt"]):
                        try:
                            async with httpx.AsyncClient(timeout=60) as hc:
                                rr = await hc.get(url)
                                if rr.status_code == 200 and rr.content:
                                    try:
                                        desc_text = rr.content.decode("utf-8", errors="ignore")
                                    except Exception:
                                        desc_text = rr.text
                        except Exception:
                            pass
                break
            if st in {"FAILED", "ERROR"}:
                try:
                    await _log_rh_task_detail(task_id, "FAILED")
                except Exception:
                    pass
                raise RunningHubError("场景任务失败，请检查工作流与入参")
            await asyncio.sleep(5)
        logging.getLogger("workflow").info(
            f"[RunningHub] generate_scene result: image_url={img_url}, has_desc_text={bool(desc_text)}"
        )
        if not img_url:
            try:
                await _log_rh_task_detail(task_id, "TIMEOUT")
            except Exception:
                pass
        return {"image_url": img_url, "desc_text": desc_text}

async def _log_rh_task_detail(task_id: str, tag: str) -> None:
    async with httpx.AsyncClient(timeout=60) as hc:
        # Try finding the right endpoint/payload for query
        # Some versions of API might expect int taskId
        payload = {"apiKey": os.getenv("RUNNINGHUB_API_KEY"), "taskId": task_id}
        
        try:
            qr = await hc.post(
                "https://www.runninghub.cn/task/openapi/query",
                headers={"Content-Type": "application/json"},
                json=payload,
            )
            if qr.status_code == 200:
                data = qr.json()
                # code 404 means task not found in query (maybe too fresh or wrong endpoint)
                if data.get("code") == 404:
                     logging.getLogger("workflow").warning(f"[RunningHub] Task {task_id} not found in query endpoint.")
                else:
                     logging.getLogger("workflow").warning(f"[RunningHub] scene.generate {tag} detail: {data}")
            else:
                 logging.getLogger("workflow").warning(f"[RunningHub] Failed to query task detail (HTTP {qr.status_code})")
        except Exception as e:
            logging.getLogger("workflow").warning(f"[RunningHub] Query exception: {e}")


