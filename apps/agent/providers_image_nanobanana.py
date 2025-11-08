# 占位：Vertex AI NanoBanana 图像模型接入（通常需 Google 认证）
import asyncio


class NanoBananaImageProvider:
    async def generate(self, prompt: str) -> str:
        # TODO: 替换为 Vertex AI 实际推理调用
        await asyncio.sleep(1.0)
        return "https://picsum.photos/seed/nanobanana/1200/630"


