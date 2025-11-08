import os
import httpx
import boto3


def get_r2_client():
    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv("R2_ACCESS_KEY")
    secret_key = os.getenv("R2_SECRET_KEY")
    if not (account_id and access_key and secret_key):
        return None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


async def upload_url_to_r2(url: str, key: str, bucket: str = None) -> str:
    bucket = bucket or os.getenv("R2_BUCKET", "video")
    r2 = get_r2_client()
    if not r2:
        # 未配置 R2，直接返回源地址
        return url
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        r.raise_for_status()
        r2.put_object(Bucket=bucket, Key=key, Body=r.content, ContentType="video/mp4")
    # 公网访问域
    account_id = os.getenv("R2_ACCOUNT_ID")
    return f"https://pub-{account_id}.r2.dev/{key}"


