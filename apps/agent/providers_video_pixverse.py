import os
import httpx


class PixVerseVideoProvider:
    async def generate(self, prompt: str, image_url: str, duration: int = 6) -> str:
        api_key = os.getenv("PIXVERSE_API_KEY")
        if not api_key:
            raise RuntimeError("PIXVERSE_API_KEY 未配置")
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                "https://api.pixverse.ai/v1/video",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"prompt": prompt, "image_url": image_url, "duration": duration},
            )
            r.raise_for_status()
            data = r.json()
            # 根据早前示例取值
            return data.get("data", {}).get("video_url") or data.get("video_url")


