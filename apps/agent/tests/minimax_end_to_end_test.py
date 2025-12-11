import os
import json
import time
import uuid
import re
import httpx
import argparse

def gen_run_id(prefix: str = "test") -> str:
    return f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:6]}"

def post_json(url: str, data: dict) -> dict:
    with httpx.Client(timeout=180) as client:
        r = client.post(url, json=data)
        r.raise_for_status()
        return r.json()

def get_bytes(url: str) -> tuple[bytes, str]:
    with httpx.Client(timeout=180) as client:
        r = client.get(url, headers={"Cache-Control": "no-cache"})
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "")

def hex_prefix(b: bytes, n: int = 16) -> str:
    return " ".join(f"{x:02X}" for x in b[:n])

def is_nontrivial_srt(s: str) -> bool:
    if not s or len(s) < 10:
        return False
    if re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", s):
        return True
    return False

def run_test(server_base: str = "http://127.0.0.1:8000") -> dict:
    run_id = gen_run_id("minimax_end2end")
    voice_id = os.getenv("MINIMAX_VOICE_ID") or "Chinese (Mandarin)_Lyrical_Voice"
    narration = "这是一次端到端校验，用于验证旁白音频与字幕是否有效。"
    body = {
        "run_id": run_id,
        "scene_idx": 1,
        "narration": narration,
        "voice_id": voice_id,
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    }
    res = post_json(f"{server_base}/tools/synthesize-voice", body)
    au = res.get("audio_url")
    su = res.get("subtitle_url")
    audio_bytes = b""
    sub_bytes = b""
    audio_ct = ""
    sub_ct = ""
    if au:
        audio_bytes, audio_ct = get_bytes(au)
    if su:
        sub_bytes, sub_ct = get_bytes(su)
    audio_ok = len(audio_bytes) >= 10000 and (audio_bytes.startswith(b"ID3") or audio_bytes.startswith(b"\xFF"))
    srt_text = sub_bytes.decode("utf-8", errors="ignore") if sub_bytes else ""
    srt_ok = is_nontrivial_srt(srt_text)
    return {
        "run_id": run_id,
        "audio_url": au,
        "subtitle_url": su,
        "audio_len": len(audio_bytes),
        "audio_ct": audio_ct,
        "audio_prefix": hex_prefix(audio_bytes),
        "subtitle_len": len(sub_bytes),
        "subtitle_ct": sub_ct,
        "subtitle_preview": srt_text[:80],
        "audio_ok": audio_ok,
        "subtitle_ok": srt_ok,
    }

def run_full(server_base: str, run_id: str, scene1: str, scene2: str, voice_id: str, segments: list[str]) -> dict:
    def post_json(url: str, data: dict) -> dict:
        with httpx.Client(timeout=300) as client:
            r = client.post(url, json=data)
            r.raise_for_status()
            return r.json()
    # synth voice 1
    r1 = post_json(f"{server_base}/tools/synthesize-voice", {
        "run_id": run_id,
        "scene_idx": 1,
        "narration": scene1,
        "voice_id": voice_id,
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    })
    # synth voice 2
    r2 = post_json(f"{server_base}/tools/synthesize-voice", {
        "run_id": run_id,
        "scene_idx": 2,
        "narration": scene2,
        "voice_id": voice_id,
        "emotion": "calm",
        "speed": 1.0,
        "vol": 1.0,
        "pitch": 0,
    })
    # bgm
    bgm_prompt = "写实但富有创意的现代电子氛围背景，克制不抢戏，突出科技与质感；节奏自然衔接，适配20秒广告。"
    rb = post_json(f"{server_base}/tools/synthesize-bgm", {"run_id": run_id, "prompt": bgm_prompt})
    # stitch
    out_key = f"{run_id}_final.mp4"
    rs = post_json(f"{server_base}/workflow/stitch", {"run_id": run_id, "segments": segments, "output_key": out_key})
    return {
        "run_id": run_id,
        "scene1": r1,
        "scene2": r2,
        "bgm": rb,
        "stitch": rs,
    }

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    ap.add_argument("--run_id", default=None)
    ap.add_argument("--scene1", default="第一幕：写实呈现轻薄耐用机身，握持更轻；全天候持久，伴你一天。")
    ap.add_argument("--scene2", default="第二幕：5000万超清摄像头，夜景细节清晰；更可拍摄月亮，创意镜头自然衔接。")
    ap.add_argument("--voice_id", default=os.getenv("MINIMAX_VOICE_ID") or "Chinese (Mandarin)_Lyrical_Voice")
    ap.add_argument("--segments", default="")
    args = ap.parse_args()
    if args.mode == "smoke":
        result = run_test(args.server)
        print(json.dumps(result, ensure_ascii=False))
        if not (result.get("audio_ok") and result.get("subtitle_ok")):
            raise SystemExit(1)
    else:
        rid = args.run_id or gen_run_id("iphone17_ads")
        segs = [s for s in (args.segments.split(",") if args.segments else []) if s]
        result = run_full(args.server, rid, args.scene1, args.scene2, args.voice_id, segs)
        print(json.dumps(result, ensure_ascii=False))
