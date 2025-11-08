# 占位：火山 Seedream 接入（需相应 API 端点与鉴权）
import asyncio


class SeedreamImageProvider:
    async def generate(self, prompt: str) -> str:
        # TODO: 替换为火山引擎 Seedream 实际调用
        await asyncio.sleep(1.0)
        return "https://picsum.photos/seed/seedream/1200/630"


