import os
import json
import time
import uuid
import httpx


def _server_base() -> str:
    return os.getenv("SERVER_BASE", "http://127.0.0.1:8000")


def _post(url: str, payload: dict) -> dict:
    with httpx.Client(timeout=180) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


def _get_bytes(url: str) -> bytes:
    with httpx.Client(timeout=180) as client:
        r = client.get(url, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return r.content


def test_voice_short_text_returns_error():
    base = _server_base()
    rid = f"t_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    body = {
        "run_id": rid,
        "scene_idx": 1,
        "narration": "太短了",  # 小于20字，应触发长度校验
        "voice_id": os.getenv("MINIMAX_VOICE_ID", "Chinese (Mandarin)_Lyrical_Voice"),
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    }
    data = _post(f"{base}/tools/synthesize-voice", body)
    assert "error" in data, f"期望返回错误，但得到: {json.dumps(data, ensure_ascii=False)}"
    assert "长度" in data["error"], f"错误信息中应包含长度提示，实际: {data['error']}"


def test_voice_succeeds_and_has_srt_or_reports_issue():
    base = _server_base()
    rid = f"t_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    narration = "这是场景一的旁白测试文本，长度超过二十个字，验证音频和字幕。"
    body = {
        "run_id": rid,
        "scene_idx": 1,
        "narration": narration,
        "voice_id": os.getenv("MINIMAX_VOICE_ID", "Chinese (Mandarin)_Lyrical_Voice"),
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    }
    data = _post(f"{base}/tools/synthesize-voice", body)
    if "error" in data:
        # 直接暴露当前旁白工具问题（例如：接口不返回音频、音色权限、或阈值过严）
        raise AssertionError(
            f"旁白合成失败: {data['error']} — 请检查 MINIMAX_API_KEY/MINIMAX_VOICE_ID、TTS 回退策略与服务可用性"
        )
    au = data.get("audio_url")
    su = data.get("subtitle_url")
    assert isinstance(au, str) and au, f"缺少 audio_url: {json.dumps(data, ensure_ascii=False)}"
    assert isinstance(su, str) and su, f"缺少 subtitle_url: {json.dumps(data, ensure_ascii=False)}"
    audio_bytes = _get_bytes(au)
    assert len(audio_bytes) >= 5000, f"音频过小({len(audio_bytes)}B)，疑似服务端未返回有效音频"
    sub_bytes = _get_bytes(su)
    srt_text = sub_bytes.decode("utf-8", errors="ignore")
    assert any(ch.isalnum() for ch in srt_text) or "-->" in srt_text, "字幕内容疑似无效"

