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
import httpx
import logging
from typing import List, Optional
from .r2 import get_r2_client
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
    
    # 创建临时目录（Windows下MoviePy/ffmpeg句柄释放存在时，使用自管理目录更稳妥）
    import shutil
    tmpdir = tempfile.mkdtemp(prefix=f"stitch_{run_id}_")
    try:
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

        # 为每个片段合成旁白并烧录字幕（如果存在），并确保每段都有统一的音频编码
        def r2_public_url(key: str) -> Optional[str]:
            base = os.getenv("R2_PUBLIC_BASE")
            if base:
                return f"{base.rstrip('/')}/{key}"
            acc = os.getenv("R2_ACCOUNT_ID")
            if acc:
                return f"https://pub-{acc}.r2.dev/{key}"
            return None
        processed_paths = []
        async def process_segment(i: int, in_path: str) -> str:
            scene_idx = i + 1
            voice_key = f"{run_id}_scene_{scene_idx}_vo.mp3"
            subs_key = f"{run_id}_scene_{scene_idx}.srt"
            voice_url = r2_public_url(voice_key)
            subs_url = r2_public_url(subs_key)
            voice_path = os.path.join(tmpdir, f"scene_{scene_idx}_vo.mp3")
            subs_path = os.path.join(tmpdir, f"scene_{scene_idx}.srt")
            has_voice = False
            has_subs = False
            # 下载语音与字幕（如果存在）
            async with httpx.AsyncClient(timeout=60) as client:
                try:
                    if voice_url:
                        r = await client.get(voice_url)
                        if r.status_code == 200 and r.content:
                            with open(voice_path, "wb") as f:
                                f.write(r.content)
                            has_voice = True
                            logger.info(f"[video_stitcher] Found narration for scene {scene_idx}")
                    if subs_url:
                        r = await client.get(subs_url)
                        if r.status_code == 200 and r.content:
                            with open(subs_path, "wb") as f:
                                f.write(r.content)
                            has_subs = True
                            logger.info(f"[video_stitcher] Found subtitles for scene {scene_idx}")
                except Exception as e:
                    logger.debug(f"[video_stitcher] Failed to fetch voice/subs for scene {scene_idx}: {e}")
            out_path = os.path.join(tmpdir, f"clip_{i}_processed.mp4")
            # 优先尝试使用 MoviePy 进行语音合成与字幕烧录，避免 FFmpeg 路径兼容问题
            try:
                from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, TextClip, concatenate_videoclips
                clip = VideoFileClip(in_path)
                try:
                    _ = float(getattr(clip, "duration", 10.0) or 10.0)
                except Exception:
                    pass
                try:
                    d = float(getattr(clip, "duration", 0.0) or 0.0)
                    if d and d > 0.06:
                        safe_end = max(0.0, d - 0.05)
                        clip = clip.subclip(0, safe_end)
                except Exception:
                    pass
                # 设置音频：优先旁白，缺失则保留原音频
                voice = None
                if has_voice:
                    voice = AudioFileClip(voice_path)
                    try:
                        clip = clip.set_audio(voice)
                    except AttributeError:
                        try:
                            clip.audio = voice
                        except Exception:
                            pass
                else:
                    try:
                        if os.getenv("FORCE_MUTE_MODEL_AUDIO", "").lower() in {"1", "true", "yes"}:
                            clip = clip.set_audio(None)
                    except Exception:
                        pass
                # 烧录字幕（如果存在）
                moviepy_subs_ok = False
                if has_subs:
                    try:
                        from moviepy.video.tools.subtitles import SubtitlesClip
                        font_env = os.getenv("SUBTITLE_FONT")
                        fontsize = int(os.getenv("SUBTITLE_FONTSIZE", "42"))
                        stroke_color = os.getenv("SUBTITLE_STROKE_COLOR", "black")
                        stroke_width = int(os.getenv("SUBTITLE_STROKE_WIDTH", "2"))
                        font_candidates = ([font_env] if font_env else [
                            "Microsoft YaHei",
                            "SimHei",
                            "Noto Sans CJK SC",
                            "Source Han Sans SC",
                            "Arial Unicode MS",
                            "Arial",
                        ])
                        sel_font = None
                        for f in font_candidates:
                            try:
                                test = TextClip(
                                    "测试",
                                    font=f,
                                    fontsize=fontsize,
                                    color="white",
                                    method="caption",
                                    size=clip.size,
                                )
                                try:
                                    test.close()
                                except Exception:
                                    pass
                                sel_font = f
                                break
                            except Exception:
                                continue
                        font = sel_font or (font_env or "Arial")
                        def make_textclip(txt):
                            return TextClip(
                                txt,
                                font=font,
                                fontsize=fontsize,
                                color="white",
                                stroke_color=stroke_color,
                                stroke_width=stroke_width,
                                method="caption",
                                size=clip.size,
                            )
                        subs = SubtitlesClip(subs_path, make_textclip)
                        clip = CompositeVideoClip([clip, subs.set_position(("center", "bottom"))])
                        moviepy_subs_ok = True
                    except Exception as e:
                        logger.warning(f"[video_stitcher] MoviePy subtitles failed for scene {scene_idx}: {e}")
                clip.write_videofile(
                    out_path,
                    codec="libx264",
                    audio_codec="aac",
                    fps=clip.fps or 30,
                    ffmpeg_params=["-vsync", "cfr", "-movflags", "+faststart"],
                    logger=None,
                )
                try:
                    if has_subs and not moviepy_subs_ok:
                        import subprocess
                        font_env = os.getenv("SUBTITLE_FONT") or "Arial Unicode MS"
                        fontsize = int(os.getenv("SUBTITLE_FONTSIZE", "42"))
                        stroke_width = int(os.getenv("SUBTITLE_STROKE_WIDTH", "2"))
                        sub_path_ff = subs_path.replace("\\", "\\\\")
                        out2_path = os.path.join(tmpdir, f"clip_{i}_subs.mp4")
                        vf = f"subtitles='{sub_path_ff}':force_style='FontName={font_env},FontSize={fontsize},Outline={stroke_width}'"
                        cmd = [
                            "ffmpeg", "-y",
                            "-i", out_path,
                            "-vf", vf,
                            "-c:v", "libx264",
                            "-c:a", "aac",
                            out2_path,
                        ]
                        try:
                            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            try:
                                import shutil
                                shutil.move(out2_path, out_path)
                            except Exception:
                                out_path = out2_path
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    clip.close()
                except Exception:
                    pass
                try:
                    if voice:
                        voice.close()
                except Exception:
                    pass
                try:
                    if 'subs' in locals() and subs:
                        subs.close()
                except Exception:
                    pass
                return out_path
            except Exception as e:
                raise RuntimeError(f"处理场景 {scene_idx} 失败: {e}")
        for i, p in enumerate(segment_paths):
            processed_paths.append(await process_segment(i, p))
        logger.info(f"[video_stitcher] Processed segments with narration/subtitles: {len(processed_paths)}")
        
        output_path = os.path.join(tmpdir, "final.mp4")
        
        used_moviepy_concat = False
        try:
            from moviepy.editor import VideoFileClip, AudioFileClip, CompositeVideoClip, concatenate_videoclips
            clips = [VideoFileClip(p) for p in processed_paths]
            # 额外安全裁剪每个片段的尾部，避免读取最后一帧时报 0 字节的告警
            safe_trim = float(os.getenv("SAFE_TRIM_SECS", "0.05") or 0.05)
            trimmed = []
            for c in clips:
                try:
                    d = float(getattr(c, "duration", 0.0) or 0.0)
                    if d and d > (safe_trim + 0.01):
                        trimmed.append(c.subclip(0, max(0.0, d - safe_trim)))
                    else:
                        trimmed.append(c)
                except Exception:
                    trimmed.append(c)
            clips = trimmed
            final = concatenate_videoclips(clips, method="compose")
            # 如果存在全局 BGM，则进行混音
            bgm_key = f"{run_id}_bgm.mp3"
            bgm_url = r2_public_url(bgm_key)
            if bgm_url:
                async with httpx.AsyncClient(timeout=60) as client:
                    rr = await client.get(bgm_url)
                    if rr.status_code == 200 and rr.content:
                        bgm_path = os.path.join(tmpdir, "bgm.mp3")
                        with open(bgm_path, "wb") as f:
                            f.write(rr.content)
                        bgm_audio = AudioFileClip(bgm_path)
                        if final.audio:
                            from moviepy.audio.AudioClip import CompositeAudioClip
                            try:
                                from moviepy.audio.fx.all import volumex as audio_volumex
                                a0 = audio_volumex(final.audio, 1.0)
                                a1 = audio_volumex(bgm_audio, 0.25)
                                mixed = CompositeAudioClip([a0, a1])
                            except Exception:
                                mixed = CompositeAudioClip([final.audio, bgm_audio])
                        else:
                            try:
                                from moviepy.audio.fx.all import volumex as audio_volumex
                                mixed = audio_volumex(bgm_audio, 0.25)
                            except Exception:
                                mixed = bgm_audio
                        try:
                            final = final.set_audio(mixed)
                        except AttributeError:
                            try:
                                final.audio = mixed
                            except Exception:
                                pass
            final.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                fps=final.fps or 30,
                ffmpeg_params=["-vsync", "cfr", "-movflags", "+faststart"],
                logger=None,
            )
            try:
                for c in clips:
                    c.close()
                final.close()
            except Exception:
                pass
            used_moviepy_concat = True
        except Exception as e:
            raise RuntimeError(f"MoviePy 拼接失败: {e}")
        
        if not os.path.exists(output_path):
            raise RuntimeError(f"输出文件不存在: {output_path}")
        
        # 获取文件大小
        file_size = os.path.getsize(output_path)
        logger.info(
            f"[video_stitcher] Stitched video file created: {output_path}, size={file_size} bytes "
            f"({file_size / 1024 / 1024:.2f} MB)"
        )
        
        

        # 上传到 R2（使用分块上传支持大文件）
        r2 = get_r2_client()
        if not r2:
            # 回退：复制到工作目录并返回 file:// URL
            fallback_out = os.path.join(os.getcwd(), f"{output_key or f'{run_id}_final.mp4'}")
            try:
                import shutil
                shutil.copyfile(output_path, fallback_out)
            except Exception:
                fallback_out = output_path
            return f"file://{fallback_out}"
        
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
                            "CacheControl": "no-cache, no-store, must-revalidate",
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
                            CacheControl="no-cache, no-store, must-revalidate",
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
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


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

