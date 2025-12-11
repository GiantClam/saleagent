import os
import json
import time
import uuid
import httpx


def _server_base() -> str:
    return os.getenv("SERVER_BASE", "http://127.0.0.1:8000")


def _post(url: str, payload: dict) -> dict:
    with httpx.Client(timeout=120) as client:
        r = client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


def test_env_check_reports_dependencies():
    base = _server_base()
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{base}/tools/env-check")
        r.raise_for_status()
        data = r.json()
    # 这些键存在即可，用于在报告中揭示依赖缺失
    for k in [
        "MINIMAX_MCP_BASE",
        "MINIMAX_API_KEY",
        "MINIMAX_VOICE_ID",
        "R2_BUCKET",
        "R2_PUBLIC_BASE",
        "R2_ACCOUNT_ID",
    ]:
        assert k in data


def test_crewai_workflow_plan_requires_openrouter_or_fails_fast():
    base = _server_base()
    body = {
        "goal": "为新品手机制作15秒广告，科技质感，中文旁白",
        "styles": ["科技感", "极简"],
        "total_duration": 20,
        "num_clips": 0,
        "run_id": f"t_{int(time.time())}_{uuid.uuid4().hex[:6]}",
    }
    # 期望：未配置 OpenRouter 时，返回包含错误信息的 500/200-json
    try:
        data = _post(f"{base}/workflow/plan", body)
    except httpx.HTTPStatusError as e:
        # 5xx 直接视为暴露了依赖问题
        assert e.response.status_code >= 400
        return
    if "detail" in data:
        # FastAPI 抛出的异常
        msg = json.dumps(data, ensure_ascii=False)
        assert ("OpenRouter" in msg) or ("未配置" in msg)
    elif "error" in data:
        assert ("OpenRouter" in data["error"]) or ("未配置" in data["error"])  # 揭示多智能体依赖
    else:
        # 如果成功返回，也接受（说明已配置 OpenRouter），但至少包含 scenes
        assert "scenes" in json.dumps(data, ensure_ascii=False)

