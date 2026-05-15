"""LLM 调用层：stepfun → DeepSeek → qwen 三级 fallback。

设计原则（plan §LLM Fallback 链）：
- 每个 provider 是一个 dict 配置，统一 OpenAI-compatible chat completions 接口
- 主调超时 30s 或非 200 → fallback 下一个
- 全部失败 → 抛 LLMAllFailed
- PROBE_LLM_MOCK=1 时返回离线 mock JSON（便于本地烟测）
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("probe.llm")

TIMEOUT_SEC = 30


class LLMAllFailed(Exception):
    """三个 provider 全部失败。"""


def _providers() -> list[dict]:
    """从环境变量加载 provider 列表，顺序即 fallback 顺序。"""
    return [
        {
            "name": "stepfun",
            "base_url": os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.com/v1/chat/completions"),
            "api_key": os.environ.get("STEPFUN_API_KEY", ""),
            "model": os.environ.get("STEPFUN_MODEL", "step-3.6"),
        },
        {
            "name": "deepseek",
            "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1/chat/completions"),
            "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        },
        {
            "name": "qwen",
            "base_url": os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"),
            "api_key": os.environ.get("QWEN_API_KEY", ""),
            "model": os.environ.get("QWEN_MODEL", "qwen-plus"),
        },
    ]


def _call_one(provider: dict, prompt: str) -> str:
    """单次 OpenAI-compatible chat 调用，返回纯文本。"""
    body = json.dumps({
        "model": provider["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    req = urllib.request.Request(
        provider["base_url"],
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider['api_key']}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        if resp.status != 200:
            raise RuntimeError(f"http {resp.status}")
        payload = json.loads(resp.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def _mock_response(prompt: str) -> tuple[str, str]:
    """离线 mock：根据 prompt 是否含敷衍内容回不同 depth_score。"""
    txt = prompt
    if "挺好的" in txt or "不错" in txt:
        depth = 1
        flags = ["no_specifics"]
        stuck = "unknown"
    elif "按钮" in txt or "页面" in txt or "上传" in txt:
        depth = 4
        flags = []
        stuck = "按钮/上传流程的反馈第二步"
    else:
        depth = 3
        flags = []
        stuck = "首屏意图理解阶段"

    return "mock/offline", json.dumps({
        "depth_score": depth,
        "depth_rationale": f"mock 评分 depth={depth}",
        "stuck_step": stuck,
        "stuck_confidence": 0.6,
        "followup_questions": ["mock 追问 1", "mock 追问 2"],
        "risk_flags": flags,
    }, ensure_ascii=False)


def call_llm(prompt: str) -> tuple[str, str]:
    """三级 fallback 调用。

    返回 ``(model_used, raw_text)``。``model_used`` 形如 ``"stepfun/step-3.6"``，
    即 ``"<provider>/<model>"``，便于事后审计区分同 provider 不同模型。
    失败抛 ``LLMAllFailed``。
    """
    if os.environ.get("PROBE_LLM_MOCK", "") == "1":
        return _mock_response(prompt)

    errors = []
    for provider in _providers():
        if not provider["api_key"]:
            errors.append(f"{provider['name']}: missing api key")
            continue
        try:
            text = _call_one(provider, prompt)
            return f"{provider['name']}/{provider['model']}", text
        except Exception as exc:  # noqa: BLE001
            # 任何 provider 级异常都视为单点失败：urllib URLError/HTTPError、
            # http.client.HTTPException、socket OSError、json 解析、畸形 payload
            # 的 IndexError/TypeError 都应继续 fallback。BaseException（如
            # KeyboardInterrupt、SystemExit）不在 Exception 范畴，仍会透传。
            errors.append(f"{provider['name']}: {exc}")
            log.warning("LLM %s failed: %s", provider["name"], exc)
            continue

    raise LLMAllFailed("; ".join(errors))
