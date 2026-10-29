"""Use the configured local OpenAI-compatible model to classify progress notes."""

from __future__ import annotations

import json
import ipaddress
import urllib.error
import urllib.request
from urllib.parse import urlsplit


STATES = {"已完成", "未完成", "进行中"}


def local_ai_status(config: dict) -> dict:
    """Check that the configured local service responds and exposes a model."""
    base = str(config.get("local_ai_base_url", "")).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or not _is_local_address(parsed.hostname):
        return {"available": False, "model": config.get("local_ai_model", ""), "message": "本地 AI 地址配置无效"}
    root = f"{parsed.scheme}://{parsed.netloc}"
    api = root + (parsed.path or "/v1")
    headers = {"Authorization": f"Bearer {config['local_ai_api_key']}"} if config.get("local_ai_api_key") else {}
    try:
        health = urllib.request.Request(f"{root}/health", headers=headers)
        with urllib.request.urlopen(health, timeout=3) as response:
            if response.status >= 400:
                raise RuntimeError("health check failed")
        models_request = urllib.request.Request(f"{api}/models", headers=headers)
        with urllib.request.urlopen(models_request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [str(item.get("id", "")) for item in payload.get("data", []) if isinstance(item, dict) and item.get("id")]
        if not models:
            raise RuntimeError("未检测到已加载模型")
        configured = str(config.get("local_ai_model", ""))
        return {"available": True, "model": models[0], "configured_model": configured, "message": f"运行正常：{models[0]}"}
    except Exception as exc:
        return {"available": False, "model": config.get("local_ai_model", ""), "message": f"未连接：{exc}"}


def classify_progress(notes: list[dict], config: dict) -> dict:
    """Return a concise progress classification based on the accumulated notes."""
    text = "\n".join(str(note.get("内容", "")) for note in notes if isinstance(note, dict)).strip()
    if not text:
        raise ValueError("没有可供 AI 判断的进展备注")
    base = str(config.get("local_ai_base_url", "")).rstrip("/")
    parsed_url = urlsplit(base)
    if parsed_url.scheme != "http" or not _is_local_address(parsed_url.hostname):
        raise ValueError("本地 AI 地址必须是回环或私网地址")
    payload = {
        "model": config.get("local_ai_model") or "",
        "temperature": 0.1,
        "max_tokens": 300,
        "stream": False,
        "messages": [
            {"role": "system", "content": (
                "根据工程设计变更的进展备注判断当前状态。只返回 JSON 对象，字段为 status 和 summary。"
                "status 只能是 已完成、未完成、进行中。已明确全部完成/闭环/验收通过时用已完成；"
                "尚未开始、待处理、未下发、未完成或明确受阻且没有实质推进时用未完成；"
                "已经开始、正在协调/施工/整改/审批/等待反馈或部分完成时用进行中。"
                "summary 用不超过 60 个汉字概括最新进展，不要添加备注中没有的信息。"
            )},
            {"role": "user", "content": f"请判断以下按时间追加的进展备注：\n{text}"},
        ],
    }
    request = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(
            {"Authorization": f"Bearer {config['local_ai_api_key']}"}
            if config.get("local_ai_api_key") else {}
        )},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=int(config.get("local_ai_timeout", 180))) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"本地 AI 分类失败：{exc}") from exc
    content = result["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    content = str(content).strip().removeprefix("```json").removesuffix("```").strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError("本地 AI 未返回有效 JSON") from exc
    status = str(parsed.get("status", "")).strip()
    summary = str(parsed.get("summary", "")).strip()[:60]
    if status not in STATES:
        raise RuntimeError("本地 AI 返回了未知进展状态")
    return {"状态": status, "摘要": summary}


def _is_local_address(host: str | None) -> bool:
    if host in {"localhost", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_private or address.is_loopback
    except ValueError:
        return False
