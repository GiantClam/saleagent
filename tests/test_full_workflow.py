import os
import json
import time
import uuid
import httpx


def _server_base() -> str:
    return os.getenv("SERVER_BASE", "http://127.0.0.1:8000")


def _post(url: str, payload: dict, timeout: int = 300) -> dict:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


def _get(url: str, timeout: int = 120) -> httpx.Response:
    with httpx.Client(timeout=timeout) as client:
        r = client.get(url, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return r


def test_end_to_end_full_workflow():
    base = _server_base()
    rid = f"e2e_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    # 1) 规划分镜（需要 OpenRouter，未配置时此步骤可能失败但不影响后续）
    plan_body = {
        "goal": "为新品手机制作20秒广告，科技质感，中文旁白",
        "styles": ["科技感", "极简"],
        "total_duration": 20,
        "num_clips": 0,
        "run_id": rid,
    }
    try:
        plan_res = _post(f"{base}/workflow/plan", plan_body, timeout=180)
        # 如成功，返回 storyboards 列表
        assert "storyboards" in plan_res
    except httpx.HTTPStatusError:
        # 未配置 OpenRouter 或调用失败时，跳过分镜断言
        plan_res = {"storyboards": []}

    # 2) 关键帧图片（使用图片 Provider，未配置时回退 Mock）
    keyframes_body = {"storyboards": plan_res.get("storyboards", []), "image_control": True}
    try:
        keyframes_res = _post(f"{base}/workflow/keyframes", keyframes_body, timeout=180)
        assert "storyboards" in keyframes_res
    except httpx.HTTPStatusError:
        keyframes_res = {"storyboards": plan_res.get("storyboards", [])}

    # 3) 旁白（场景1/2）
    voice_id = os.getenv("MINIMAX_VOICE_ID", "Chinese (Mandarin)_Lyrical_Voice")
    scene1 = "第一幕：轻薄耐用机身，全天候持久续航，测试旁白与字幕有效。"
    scene2 = "第二幕：超清摄像头与夜景表现，真实细节，测试旁白与字幕有效。"
    v1 = _post(f"{base}/tools/synthesize-voice", {
        "run_id": rid,
        "scene_idx": 1,
        "narration": scene1,
        "voice_id": voice_id,
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    })
    assert "audio_url" in v1 and "subtitle_url" in v1, f"scene1 旁白失败: {json.dumps(v1, ensure_ascii=False)}"
    v2 = _post(f"{base}/tools/synthesize-voice", {
        "run_id": rid,
        "scene_idx": 2,
        "narration": scene2,
        "voice_id": voice_id,
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    })
    assert "audio_url" in v2 and "subtitle_url" in v2, f"scene2 旁白失败: {json.dumps(v2, ensure_ascii=False)}"

    # 4) 背景音乐
    bgm = _post(f"{base}/tools/synthesize-bgm", {"run_id": rid, "prompt": "现代电子氛围背景，克制不抢戏，突出科技与质感"})
    assert "bgm_url" in bgm or "audio_url" in bgm, f"BGM 生成失败: {json.dumps(bgm, ensure_ascii=False)}"

    # 5) 生成视频片段（使用固定样例或 Provider 的示例视频）
    sample_video = "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4"
    segs = [sample_video, sample_video]

    # 6) 拼接最终视频
    out_key = f"{rid}_final.mp4"
    stitch = _post(f"{base}/workflow/stitch", {"run_id": rid, "segments": segs, "output_key": out_key}, timeout=600)
    assert "final_url" in stitch, f"拼接失败: {json.dumps(stitch, ensure_ascii=False)}"
    final_url = stitch["final_url"]
    resp = _get(final_url, timeout=180)
    # 简单校验返回内容类型
    ct = resp.headers.get("Content-Type", "")
    assert "video" in ct.lower(), f"最终视频类型异常: {ct}"

