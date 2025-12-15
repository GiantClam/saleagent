import os
from typing import List, Dict, Any, Optional
import httpx
import logging
import json

logger = logging.getLogger(__name__)


class OpenRouterError(RuntimeError):
    pass


class OpenRouterClient:
    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        referer: Optional[str] = None,
        title: str = "SaleAgent",
    ) -> None:
        # 优先读取 OPENROUTER_*，兼容旧变量
        self.api_base = (
            api_base
            or os.getenv("OPENROUTER_API_BASE")
            or os.getenv("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("LLM_API_KEY")
        )
        if not self.api_key:
            raise OpenRouterError("缺少 OPENROUTER_API_KEY（或兼容的 LLM_API_KEY）")
        self.referer = referer or os.getenv("EMBEDDING_REFERER") or os.getenv("SITE_URL") or "https://saleagent.app"
        # 代理支持：优先使用 OPENROUTER_PROXY，其次 HTTP_PROXY/HTTPS_PROXY
        proxy = os.getenv("OPENROUTER_PROXY")
        http_proxy = os.getenv("OPENROUTER_HTTP_PROXY") or os.getenv("HTTP_PROXY")
        https_proxy = os.getenv("OPENROUTER_HTTPS_PROXY") or os.getenv("HTTPS_PROXY")
        if proxy:
            self.proxies: Optional[dict] = {"http://": proxy, "https://": proxy}
        elif http_proxy or https_proxy:
            self.proxies = {}
            if http_proxy:
                self.proxies["http://"] = http_proxy
            if https_proxy:
                self.proxies["https://"] = https_proxy
        else:
            self.proxies = None
        self.title = title

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if "openrouter.ai" in self.api_base:
            headers["HTTP-Referer"] = self.referer
            headers["X-Title"] = self.title
        return headers

    async def chat_completions(self, model: str, messages: List[Dict[str, Any]], temperature: float = 0.7, max_tokens: int = 512, response_format: Optional[Dict[str, Any]] = None) -> str:
        async with httpx.AsyncClient(timeout=60, proxies=self.proxies) as client:
            request_payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            # 如果支持 response_format（如 OpenAI 的 JSON mode），添加它
            if response_format:
                request_payload["response_format"] = response_format
            logger.info(f"[OpenRouterClient] Request: POST {self.api_base}/chat/completions")
            logger.info(f"[OpenRouterClient] Model: {model}, Temperature: {temperature}, Max tokens: {max_tokens}")
            logger.debug(f"[OpenRouterClient] Request payload: {json.dumps(request_payload, ensure_ascii=False, indent=2)}")
            
            r = await client.post(
                f"{self.api_base}/chat/completions",
                headers=self._headers(),
                json=request_payload,
            )
            
            logger.info(f"[OpenRouterClient] Response status: {r.status_code}")
            
            if r.status_code != 200:
                error_text = r.text[:1000]  # 限制长度
                logger.error(f"[OpenRouterClient] HTTP {r.status_code}: {error_text}")
                raise OpenRouterError(f"HTTP {r.status_code}: {error_text}")
            
            try:
                data = r.json()
            except Exception as e:
                raw_text = r.text[:2000]  # 限制长度
                logger.error(f"[OpenRouterClient] Failed to parse JSON response: {e}")
                logger.error(f"[OpenRouterClient] Raw response text (first 2000 chars): {raw_text}")
                raise OpenRouterError(f"无法解析 JSON 响应: {e}")
            
            # 打印完整的响应内容（用于调试）
            try:
                response_str = json.dumps(data, ensure_ascii=False, indent=2)
                logger.info(f"[OpenRouterClient] Full response data: {response_str[:2000]}")  # 限制长度
            except Exception:
                logger.warning(f"[OpenRouterClient] Failed to serialize response for logging")
            
            # 尽可能兼容多种返回结构
            try:
                choice = (data.get("choices") or [None])[0] or {}
                logger.info(f"[OpenRouterClient] Parsed choice keys: {list(choice.keys()) if isinstance(choice, dict) else 'not a dict'}")
                
                # openai-style
                msg = choice.get("message") or {}
                content = msg.get("content")
                
                # 检查是否有拒绝信息
                refusal = msg.get("refusal")
                if refusal:
                    logger.warning(f"[OpenRouterClient] Model refused to generate content: {refusal}")
                
                logger.info(f"[OpenRouterClient] Content type: {type(content)}, value preview: {str(content)[:200] if content else 'None'}")
                
                if isinstance(content, list):
                    # Some providers return content as a list of parts
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    content = "".join(text_parts).strip()
                    logger.debug(f"[OpenRouterClient] Content from list: {content[:200]}")
                
                if isinstance(content, str) and content.strip():
                    logger.info(f"[OpenRouterClient] Returning content (len={len(content)}): {content[:200]}")
                    return content.strip()
                
                # 检查 reasoning 字段（某些模型可能将内容放在这里）
                reasoning = msg.get("reasoning")
                if isinstance(reasoning, str) and reasoning.strip():
                    logger.info(f"[OpenRouterClient] Found content in reasoning field (len={len(reasoning)}): {reasoning[:200]}")
                    return reasoning.strip()
                
                # 检查 reasoning_details（某些模型使用加密的 reasoning）
                reasoning_details = msg.get("reasoning_details")
                if isinstance(reasoning_details, list) and len(reasoning_details) > 0:
                    # 尝试从 reasoning_details 中提取内容
                    for detail in reasoning_details:
                        if isinstance(detail, dict):
                            # 如果是加密的 reasoning，无法直接使用，但可以记录
                            if detail.get("type") == "reasoning.encrypted":
                                logger.warning(f"[OpenRouterClient] Found encrypted reasoning, cannot extract content directly")
                            # 如果有其他可用的文本字段
                            elif detail.get("text"):
                                text = detail.get("text")
                                if isinstance(text, str) and text.strip():
                                    logger.info(f"[OpenRouterClient] Found content in reasoning_details (len={len(text)}): {text[:200]}")
                                    return text.strip()
                
                # fallback: text field
                text_field = choice.get("text")
                if isinstance(text_field, str) and text_field.strip():
                    logger.info(f"[OpenRouterClient] Returning text field (len={len(text_field)}): {text_field[:200]}")
                    return text_field.strip()
                
                # fallback: top-level output_text (some routers)
                output_text = data.get("output_text")
                if isinstance(output_text, str) and output_text.strip():
                    logger.info(f"[OpenRouterClient] Returning output_text (len={len(output_text)}): {output_text[:200]}")
                    return output_text.strip()
                
                # 如果所有解析都失败，打印详细的警告信息
                logger.warning(f"[OpenRouterClient] No valid content found in response. Full data structure:")
                logger.warning(f"[OpenRouterClient] data.keys(): {list(data.keys()) if isinstance(data, dict) else 'not a dict'}")
                logger.warning(f"[OpenRouterClient] data.get('choices'): {data.get('choices')}")
                logger.warning(f"[OpenRouterClient] choice keys: {list(choice.keys()) if isinstance(choice, dict) else 'not a dict'}")
                logger.warning(f"[OpenRouterClient] msg keys: {list(msg.keys()) if isinstance(msg, dict) else 'not a dict'}")
                logger.warning(f"[OpenRouterClient] msg.get('content'): {repr(msg.get('content'))}")
                logger.warning(f"[OpenRouterClient] msg.get('refusal'): {repr(msg.get('refusal'))}")
                logger.warning(f"[OpenRouterClient] msg.get('reasoning'): {repr(msg.get('reasoning'))}")
                logger.warning(f"[OpenRouterClient] msg.get('reasoning_details'): {repr(msg.get('reasoning_details'))}")
                
                # 如果模型拒绝生成，抛出更明确的错误
                if refusal:
                    raise OpenRouterError(f"模型拒绝生成内容: {refusal}")
                
                # 如果 finish_reason 是 "length"，说明内容被截断了，但可能有一些内容
                finish_reason = choice.get("finish_reason")
                if finish_reason == "length":
                    logger.warning(f"[OpenRouterClient] Response was truncated (finish_reason=length), but no content found")
                
                return ""
            except Exception as e:
                # 返回原始数据以便上层记录日志
                logger.error(f"[OpenRouterClient] Exception while parsing response: {e}")
                logger.error(f"[OpenRouterClient] Full response data: {json.dumps(data, ensure_ascii=False, default=str)[:2000]}")
                raise OpenRouterError(f"无效响应：{data}")

    async def chat(
        self,
        *,
        model: str,
        system: Optional[str] = None,
        messages: List[Dict[str, Any]] = [],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}] + msgs
        request_payload: Dict[str, Any] = {
            "model": model,
            "messages": msgs,
        }
        if temperature is not None:
            request_payload["temperature"] = float(temperature)
        if max_tokens is not None:
            request_payload["max_tokens"] = int(max_tokens)
        if response_format:
            request_payload["response_format"] = response_format
        async with httpx.AsyncClient(timeout=60, proxies=self.proxies) as client:
            r = await client.post(
                f"{self.api_base}/chat/completions",
                headers=self._headers(),
                json=request_payload,
            )
            if r.status_code != 200:
                raise OpenRouterError(f"HTTP {r.status_code}: {r.text}")
            return r.json()

    def pick_content(self, resp: Dict[str, Any]) -> Optional[str]:
        try:
            choices = resp.get("choices") or []
            msg = (choices[0] or {}).get("message", {}) if choices else {}
            content = msg.get("content")
            if isinstance(content, list):
                return "".join([str(x.get("text") or "") for x in content])
            return content
        except Exception:
            return None


