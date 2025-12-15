import os
import httpx
import boto3
from urllib.parse import urlparse
from botocore.config import Config


def get_r2_client():
    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv("R2_ACCESS_KEY")
    secret_key = os.getenv("R2_SECRET_KEY")
    if not (account_id and access_key and secret_key):
        return None
    
    # 配置超时和重试，支持大文件上传
    config = Config(
        connect_timeout=60,
        read_timeout=300,  # 5 分钟读取超时
        retries={
            'max_attempts': 3,
            'mode': 'adaptive'
        },
        max_pool_connections=50
    )
    
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=config
    )


async def upload_url_to_r2(url: str, key: str, bucket: str = None) -> str:
    bucket = bucket or os.getenv("R2_BUCKET", "video")
    r2 = get_r2_client()
    if not r2:
        # 未配置 R2，直接返回源地址
        return url
    content: bytes
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        async with httpx.AsyncClient() as client:
            r = await client.get(url)
            r.raise_for_status()
            content = r.content
    elif parsed.scheme == "file":
        # 读取本地文件
        path = parsed.path
        with open(path, "rb") as f:
            content = f.read()
    else:
        # 当作本地路径处理
        try:
            with open(url, "rb") as f:
                content = f.read()
        except Exception:
            # 最后回退为直接返回源地址
            return url
    r2.put_object(Bucket=bucket, Key=key, Body=content, ContentType="video/mp4")
    # 公网访问域：优先 R2_PUBLIC_BASE
    public_base = os.getenv("R2_PUBLIC_BASE")
    if public_base:
        return f"{public_base.rstrip('/')}/{key}"
    account_id = os.getenv("R2_ACCOUNT_ID")
    return f"https://pub-{account_id}.r2.dev/{key}"


def presign_put_url(key: str, bucket: str = None, content_type: str = "application/octet-stream", expires: int = 3600) -> dict:
    """
    生成 Cloudflare R2 的预签名 PUT URL，用于浏览器直传大文件。
    返回 {"upload_url", "key", "bucket", "headers", "public_url"}
    """
    bucket = bucket or os.getenv("R2_BUCKET", "video")
    r2 = get_r2_client()
    if not r2:
        raise RuntimeError("R2 is not configured")
    url = r2.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=expires,
    )
    public_base = os.getenv("R2_PUBLIC_BASE")
    if public_base:
        public_url = f"{public_base.rstrip('/')}/{key}"
    else:
        account_id = os.getenv("R2_ACCOUNT_ID")
        public_url = f"https://pub-{account_id}.r2.dev/{key}"
    return {
        "upload_url": url,
        "key": key,
        "bucket": bucket,
        "headers": {"Content-Type": content_type},
        "public_url": public_url,
    }


