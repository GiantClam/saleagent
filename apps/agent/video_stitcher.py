"""
视频拼接模块：提供独立的视频拼接函数，可直接从代码层面调用

功能：
1. 下载视频片段
2. 使用 FFmpeg 拼接
3. 上传到 R2
4. 返回最终视频的 CDN URL
"""

import os
import asyncio
import tempfile
import subprocess
import httpx
import logging
from typing import List, Optional
from r2 import get_r2_client
import boto3
from boto3.s3.transfer import TransferConfig

logger = logging.getLogger("video_stitcher")


async def stitch_video_segments(
    segment_urls: List[str],
    run_id: str,
    output_key: Optional[str] = None
) -> str:
    """
    拼接视频片段为最终视频
    
    Args:
        segment_urls: 视频片段 URL 列表（按顺序）
        run_id: 运行 ID，用于文件命名
        output_key: 可选的输出文件 key（默认: {run_id}_final.mp4）
        
    Returns:
        最终视频的 CDN URL
        
    Raises:
        RuntimeError: 如果拼接失败或配置错误
    """
    if not segment_urls:
        raise ValueError("视频片段列表不能为空")
    
    logger.info(
        f"[video_stitcher] Starting stitch for run_id={run_id}: "
        f"{len(segment_urls)} video segments"
    )
    
    # 下载所有视频片段
    async def download_segment(url: str, path: str):
        """下载单个视频片段"""
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
            logger.debug(f"[video_stitcher] Downloaded segment: {url} -> {path}")
    
    # 创建临时目录
    with tempfile.TemporaryDirectory() as tmpdir:
        # 下载所有片段
        async def download_all():
            tasks = [
                download_segment(url, os.path.join(tmpdir, f"clip_{i}.mp4"))
                for i, url in enumerate(segment_urls)
            ]
            await asyncio.gather(*tasks)
            return [os.path.join(tmpdir, f"clip_{i}.mp4") for i in range(len(segment_urls))]
        
        segment_paths = await download_all()
        logger.info(f"[video_stitcher] Downloaded {len(segment_paths)} segments")
        
        # 创建 concat 文件
        concat_file = os.path.join(tmpdir, "concat.txt")
        with open(concat_file, "w") as f:
            for path in segment_paths:
                f.write(f"file '{path}'\n")
        
        # 使用 ffmpeg 拼接
        output_path = os.path.join(tmpdir, "final.mp4")
        cmd = [
            "ffmpeg", "-f", "concat", "-safe", "0", "-i", concat_file,
            "-c", "copy", "-movflags", "+faststart", "-y", output_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg 拼接失败: {result.stderr}")
        
        # 验证输出文件是否存在
        if not os.path.exists(output_path):
            raise RuntimeError(f"FFmpeg 拼接完成但输出文件不存在: {output_path}")
        
        # 获取文件大小
        file_size = os.path.getsize(output_path)
        logger.info(
            f"[video_stitcher] Stitched video file created: {output_path}, size={file_size} bytes "
            f"({file_size / 1024 / 1024:.2f} MB)"
        )
        
        # 上传到 R2（使用分块上传支持大文件）
        r2 = get_r2_client()
        if not r2:
            raise RuntimeError("R2 未配置")
        
        bucket = os.getenv("R2_BUCKET", "video")
        key = output_key or f"{run_id}_final.mp4"
        
        logger.info(
            f"[video_stitcher] Uploading final video to R2: bucket={bucket}, key={key}, size={file_size} bytes"
        )
        
        # 构建公网访问 URL（优先使用 R2_PUBLIC_BASE）
        r2_public_base = os.getenv("R2_PUBLIC_BASE")
        
        # 如果 R2_PUBLIC_BASE 包含 example.com，记录错误并抛出异常
        if r2_public_base and "example.com" in r2_public_base.lower():
            logger.error(
                f"[video_stitcher] CRITICAL: R2_PUBLIC_BASE environment variable contains 'example.com': {r2_public_base}. "
                f"This is likely a configuration error. Please check your .env file or environment variables."
            )
            raise RuntimeError(
                f"R2_PUBLIC_BASE environment variable is set to an invalid value: {r2_public_base}. "
                f"Please set it to a valid CDN domain (e.g., https://s.aimarketingsite.com)"
            )
        
        # 上传文件（支持大文件分块上传）
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                if file_size > 50 * 1024 * 1024:  # 50MB
                    # 使用 upload_file 进行分块上传（自动处理 multipart）
                    logger.info(
                        f"[video_stitcher] Using multipart upload for large file (attempt {attempt + 1}/{max_retries})"
                    )
                    r2.upload_file(
                        output_path,
                        bucket,
                        key,
                        ExtraArgs={
                            "ContentType": "video/mp4",
                            "Metadata": {
                                "run_id": run_id,
                                "file_size": str(file_size)
                            }
                        },
                        Config=TransferConfig(
                            multipart_threshold=50 * 1024 * 1024,  # 50MB 以上使用分块
                            max_concurrency=4,
                            multipart_chunksize=10 * 1024 * 1024,  # 每块 10MB
                            use_threads=True
                        )
                    )
                else:
                    # 小文件直接使用 put_object
                    with open(output_path, "rb") as f:
                        r2.put_object(
                            Bucket=bucket,
                            Key=key,
                            Body=f.read(),
                            ContentType="video/mp4",
                            Metadata={
                                "run_id": run_id,
                                "file_size": str(file_size)
                            }
                        )
                
                logger.info(
                    f"[video_stitcher] Successfully uploaded final video to R2: bucket={bucket}, key={key}"
                )
                break  # 上传成功，退出重试循环
                
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[video_stitcher] Upload attempt {attempt + 1} failed: {e}. "
                        f"Retrying in {retry_delay} seconds..."
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 2  # 指数退避
                else:
                    # 最后一次尝试也失败
                    logger.error(
                        f"[video_stitcher] Failed to upload final video after {max_retries} attempts: {e}",
                        exc_info=True
                    )
                    raise RuntimeError(f"上传到 R2 失败（已重试 {max_retries} 次）: {e}")
        
        # 生成最终 CDN URL
        if not r2_public_base:
            account_id = os.getenv("R2_ACCOUNT_ID")
            if account_id:
                # 降级：使用默认的 R2 公网域名
                final_video_url = f"https://pub-{account_id}.r2.dev/{key}"
                logger.warning(
                    f"[video_stitcher] R2_PUBLIC_BASE not set, using default R2 domain: {final_video_url}"
                )
            else:
                raise RuntimeError("R2_PUBLIC_BASE 或 R2_ACCOUNT_ID 未配置")
        else:
            # 使用 R2_PUBLIC_BASE（Cloudflare CDN 域名）
            r2_public_base_clean = r2_public_base.rstrip('/')
            final_video_url = f"{r2_public_base_clean}/{key}"
            logger.info(
                f"[video_stitcher] Generated final video URL using R2_PUBLIC_BASE: {final_video_url} "
                f"(base={r2_public_base_clean}, key={key})"
            )
        
        logger.info(
            f"[video_stitcher] Stitch completed for run_id={run_id}: {final_video_url}"
        )
        
        return final_video_url


def stitch_video_segments_sync(
    segment_urls: List[str],
    run_id: str,
    output_key: Optional[str] = None
) -> str:
    """
    拼接视频片段为最终视频（同步版本）
    
    这是一个同步包装函数，内部调用异步版本。
    适用于需要在同步上下文中调用的场景。
    
    Args:
        segment_urls: 视频片段 URL 列表（按顺序）
        run_id: 运行 ID，用于文件命名
        output_key: 可选的输出文件 key（默认: {run_id}_final.mp4）
        
    Returns:
        最终视频的 CDN URL
        
    Raises:
        RuntimeError: 如果拼接失败或配置错误
    """
    try:
        # 尝试获取当前事件循环
        loop = asyncio.get_running_loop()
        # 如果已经有运行中的事件循环，使用 nest_asyncio 或线程池
        import nest_asyncio
        nest_asyncio.apply()
        return asyncio.run(stitch_video_segments(segment_urls, run_id, output_key))
    except RuntimeError:
        # 没有运行中的事件循环，直接使用 asyncio.run
        return asyncio.run(stitch_video_segments(segment_urls, run_id, output_key))
    except ImportError:
        # nest_asyncio 不可用，使用线程池
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(
                asyncio.run,
                stitch_video_segments(segment_urls, run_id, output_key)
            )
            return future.result()

