import os
import json
import tempfile
import httpx
from dotenv import load_dotenv
import argparse
import sys

def hex_prefix(b: bytes) -> str:
    h = [f"{x:02X}" for x in b[:16]]
    return " ".join(h)

def save_and_info(name: str, content: bytes, outdir: str | None = None):
    d = outdir or tempfile.mkdtemp(prefix="minimax_smoke_")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as f:
        f.write(content)
    print(json.dumps({"saved": p, "size": len(content), "prefix": hex_prefix(content)}))
    return p

def validate_and_exit(audio_bytes: bytes, sub_bytes: bytes, min_audio: int, min_sub: int):
    ok_audio = len(audio_bytes) >= max(0, min_audio)
    ok_sub = len(sub_bytes) >= max(0, min_sub)
    print(json.dumps({
        "ok_audio": ok_audio,
        "ok_subtitle": ok_sub,
        "audio_len": len(audio_bytes),
        "subtitle_len": len(sub_bytes)
    }))
    if not ok_audio or not ok_sub:
        sys.exit(1)

def call_sync_tts(api_key: str, gid: str | None, text: str, voice_id: str):
    bases = [
        "https://api.minimaxi.com/v1/t2a_v2",
        "https://api-bj.minimaxi.com/v1/t2a_v2",
        "https://api.minimaxi.chat/v1/t2a_v2",
    ]

    speech_model = os.getenv("MINIMAX_SPEECH_MODEL")
    payload = {
        "model": speech_model,
        "text": text,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
            "emotion": "calm",
        },
        "audio_setting": {
            "sample_rate": 32000,
            "bitrate": 128000,
            "format": "mp3",
            "channel": 2,
        },
        "stream": False,
        "output_format": "url",
        "subtitle_enable": True,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=180) as client:
        resp = None
        used = None
        for base in bases:
            url = base if ("minimaxi.chat" not in base or not gid) else f"{base}?GroupId={gid}"
            r = client.post(url, headers=headers, json=payload)
            if r.status_code in (200, 201):
                resp = r
                used = url
                break
        if resp is None:
            raise RuntimeError("sync tts failed")
        print(json.dumps({"sync_tts_url": used, "status": resp.status_code}))
        print(resp.text[:300])
        data = resp.json()
        au = data.get("audio_url") or (data.get("data") or {}).get("audio_url") or ((data.get("audio_file") or {}).get("url"))
        ah = data.get("audio") or (data.get("data") or {}).get("audio")
        su = (
            data.get("subtitle_url") or
            (data.get("data") or {}).get("subtitle_url") or
            ((data.get("subtitle_file") or {}).get("url")) or
            (data.get("subtitle_file") if isinstance(data.get("subtitle_file"), str) else None) or
            (((data.get("data") or {}).get("subtitle_file")) if isinstance((data.get("data") or {}).get("subtitle_file"), str) else None)
        )
        st = data.get("subtitle") or (data.get("data") or {}).get("subtitle")
        audio_bytes = b""
        if au:
            ra = client.get(au)
            ra.raise_for_status()
            audio_bytes = ra.content
        elif ah:
            s = ah.strip()
            if s.startswith("http://") or s.startswith("https://"):
                ra = client.get(s)
                ra.raise_for_status()
                audio_bytes = ra.content
            else:
                try:
                    audio_bytes = bytes.fromhex(s)
                except Exception:
                    import base64
                    try:
                        audio_bytes = base64.b64decode(s)
                    except Exception:
                        raise RuntimeError("audio field not url/hex/base64")
        else:
            raise RuntimeError("no audio content returned")
        sub_bytes = b""
        if su:
            rs = client.get(su)
            if rs.status_code == 200 and rs.content:
                sub_bytes = rs.content
        elif st:
            sub_bytes = st.encode("utf-8", errors="ignore")
        return audio_bytes, sub_bytes

def call_music(api_key: str, prompt: str):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"prompt": prompt, "output_format": "url"}
    with httpx.Client(timeout=180) as client:
        url = "https://api.minimaxi.com/v1/music_generation"
        lyrics_hint = (
            "[Intro]\n纯器乐，无人声。氛围柔和，铺底延迟与暖垫。\n"
            "[Verse]\n纯器乐，无人声。节奏克制，突出轻微律动与质感。\n"
            "[Chorus]\n纯器乐，无人声。能量略微提升，柔和主旋律淡入。\n"
            "[Bridge]\n纯器乐，无人声。纹理变化，加入细微琶音与过渡。\n"
            "[Outro]\n纯器乐，无人声。逐步收束，元素淡出，尾部渐隐。\n"
        )
        payload = {"model": "music-2.0", "prompt": prompt, "output_format": "url", "stream": False,
                   "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
                   "lyrics": lyrics_hint}
        r = client.post(url, headers=headers, json=payload)
        if r.status_code == 404:
            url = "https://api.minimaxi.chat/v1/music_generation"
            r = client.post(url, headers=headers, json=payload)
        print(r.text[:300])
        if r.status_code >= 400:
            print(json.dumps({"music_status": r.status_code, "music_url": url}))
            print(r.text[:300])
            # 回退移除 output_format
            lyrics_hint = (
                "[Intro]\nInstrumental background, no vocals. Repeat motifs and pads for ambience.\n"
                "[Verse]\nInstrumental background, no vocals. Keep rhythm steady and minimal.\n"
                "[Chorus]\nInstrumental background, no vocals. Slight energy rise with soft leads.\n"
                "[Bridge]\nInstrumental background, no vocals. Texture change with subtle arps.\n"
                "[Outro]\nInstrumental background, no vocals. Fade elements gracefully.\n"
            )
            p2 = {"prompt": prompt, "model": "music-2.0", "stream": False, "lyrics": lyrics_hint}
            r = client.post(url, headers=headers, json=p2)
            print(r.text[:300])
            r.raise_for_status()
        data = r.json()
        au = data.get("audio_url") or (data.get("data") or {}).get("audio_url") or ((data.get("audio_file") or {}).get("url"))
        if not au:
            au = data.get("audio") or (data.get("data") or {}).get("audio")
        if not au:
            raise RuntimeError("music_generation no audio or audio_url")
        rr = client.get(au)
        rr.raise_for_status()
        return rr.content

def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="这是一个旁白测试，用于验证音频与字幕是否非空。")
    parser.add_argument("--voice_id", default=os.getenv("MINIMAX_VOICE_ID") or "Chinese (Mandarin)_Lyrical_Voice")
    parser.add_argument("--gid", default=os.getenv("MINIMAX_GROUP_ID"))
    parser.add_argument("--min_audio_bytes", type=int, default=10000)
    parser.add_argument("--min_subtitle_bytes", type=int, default=10)
    parser.add_argument("--outdir", default=None)
    parser.add_argument("--prompt", default="现代简约电子氛围，克制不抢戏，商业广告背景，音量适中")
    args = parser.parse_args()

    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY missing")

    audio_bytes, sub_bytes = call_sync_tts(api_key, args.gid, args.text, args.voice_id)
    print(json.dumps({"voice_audio": len(audio_bytes), "subtitle": len(sub_bytes)}))
    save_and_info("voice.mp3", audio_bytes, args.outdir)
    if sub_bytes:
        save_and_info("subtitle.srt", sub_bytes, args.outdir)
    validate_and_exit(audio_bytes, sub_bytes, args.min_audio_bytes, args.min_subtitle_bytes)

    music_bytes = call_music(api_key, args.prompt)
    print(json.dumps({"bgm_audio": len(music_bytes)}))
    save_and_info("bgm.mp3", music_bytes, args.outdir)

if __name__ == "__main__":
    main()
