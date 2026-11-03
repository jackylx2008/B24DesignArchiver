"""Work-order reconciliation data and local-AI helpers."""

from __future__ import annotations

import difflib
import json
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


STORE_NAME = "工作联系单变更关联.json"
CODE_PATTERNS = {
    "给排水": r"05-03-C2-\d{3,}", "暖通": r"06-00-C2-\d{3,}",
    "精装给排水": r"JZ-05-03-C2-\d{3,}", "精装暖通": r"JZ-06-00-C2-\d{3,}",
    "热力": r"(?:R20|RL20)-03-Z-\d{2,}", "燃气": r"251614GC-01GS-\d+",
    "泳池设备": r"泳池变更-\d+",
}
NUMBERING_RULES = (
    ("给排水", re.compile(r"^(05-03-C2-)(\d+)$", re.IGNORECASE)),
    ("暖通", re.compile(r"^(06-00-C2-)(\d+)$", re.IGNORECASE)),
    ("精装给排水", re.compile(r"^(JZ-05-03-C2-)(\d+)$", re.IGNORECASE)),
    ("精装暖通", re.compile(r"^(JZ-06-00-C2-)(\d+)$", re.IGNORECASE)),
    ("热力", re.compile(r"^(R20|RL20)(-03-Z-)(\d+)$", re.IGNORECASE)),
    ("燃气", re.compile(r"^(251614GC-01GS-)(\d+)$", re.IGNORECASE)),
    ("泳池设备", re.compile(r"^(泳池变更-)(\d+)$", re.IGNORECASE)),
)


def load_work_order_records(path: Path) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到工作联系单 JSON：{path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise ValueError("工作联系单 JSON 缺少 records 数组")
    result = []
    seen = set()
    for item in records:
        if not isinstance(item, dict) or not str(item.get("record_id", "")).strip():
            continue
        record_id = str(item["record_id"]).strip()
        if record_id in seen:
            raise ValueError(f"工作联系单 JSON 中 record_id 重复：{record_id}")
        seen.add(record_id)
        result.append({**item, "record_id": record_id})
    return result


def refresh_project_work_order_json(source_path: Path, project_path: Path) -> dict[str, int]:
    """Merge the latest hotel work-order dataset into this project's local JSON.

    Existing IDs are refreshed from the canonical source, new IDs are appended,
    and records absent from a later source scan are retained in the project copy.
    """
    source_data = json.loads(source_path.read_text(encoding="utf-8-sig"))
    if not isinstance(source_data, dict) or not isinstance(source_data.get("records"), list):
        raise ValueError(f"酒店工作联系单 JSON 结构无效：{source_path}")
    previous_data = {}
    if project_path.is_file():
        previous_data = json.loads(project_path.read_text(encoding="utf-8-sig"))
        if not isinstance(previous_data, dict) or not isinstance(previous_data.get("records"), list):
            raise ValueError(f"项目工作联系单 JSON 结构无效：{project_path}")
    previous_records = previous_data.get("records", [])
    merged_by_id: dict[str, dict] = {}
    for item in previous_records:
        if isinstance(item, dict) and str(item.get("record_id", "")).strip():
            merged_by_id[str(item["record_id"]).strip()] = item
    added = updated = 0
    for item in source_data["records"]:
        if not isinstance(item, dict) or not str(item.get("record_id", "")).strip():
            continue
        record_id = str(item["record_id"]).strip()
        previous = merged_by_id.get(record_id)
        if previous is None:
            merged_by_id[record_id] = item
            added += 1
        else:
            refreshed = {**previous, **item}
            if refreshed != previous:
                updated += 1
            merged_by_id[record_id] = refreshed
    payload = {**source_data, "records": list(merged_by_id.values()),
               "snapshot_source_file": str(source_path.resolve()), "snapshot_updated_at": _now()}
    project_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=project_path.parent,
                                     suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(project_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"added": added, "updated": updated, "total": len(merged_by_id)}


def new_store(source_path: Path) -> dict:
    return {
        "schema_version": 1,
        "source_file": str(source_path),
        "updated_at": _now(),
        "relations": {},
        "drafts": [],
        "conversion_decisions": {},
        "issued_change_decisions": {},
    }


def load_store(path: Path, source_path: Path) -> dict:
    if not path.is_file():
        return new_store(source_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"工作联系单关联 JSON 版本无效：{path}")
    if not isinstance(data.get("relations"), dict) or not isinstance(data.get("drafts"), list):
        raise ValueError(f"工作联系单关联 JSON 结构无效：{path}")
    data.setdefault("source_file", str(source_path))
    data.setdefault("conversion_decisions", {})
    data.setdefault("issued_change_decisions", {})
    return data


def save_store(path: Path, store: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    store["updated_at"] = _now()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def candidates_for(work_order: dict, changes: list[dict], limit: int = 12) -> list[dict]:
    source_text = _normalize(" ".join(str(work_order.get(key, "")) for key in ("subject", "需求内容", "discipline")))
    ranked = []
    for change in changes:
        change_text = _normalize(" ".join(str(change.get(key, "")) for key in ("变更编号和说明", "变更内容三", "变更内容", "何种专业四", "专业")))
        if not source_text or not change_text:
            continue
        ratio = difflib.SequenceMatcher(None, source_text[:3000], change_text[:3000], autojunk=False).ratio()
        source_bigrams, change_bigrams = _bigrams(source_text), _bigrams(change_text)
        union = source_bigrams | change_bigrams
        jaccard = len(source_bigrams & change_bigrams) / len(union) if union else 0.0
        score = 0.48 * ratio + 0.52 * jaccard
        if _same_discipline(work_order.get("discipline", ""), change.get("专业", change.get("何种专业四", ""))):
            score += 0.07
        ranked.append((score, change))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"id": str(item.get("_id", "")), "编号": _change_code(item),
         "说明": str(item.get("变更编号和说明", "")),
         "专业": str(item.get("专业", item.get("何种专业四", ""))),
         "日期": str(item.get("日期五", item.get("日期", ""))),
         "变更内容": str(item.get("变更内容三", item.get("变更内容", "")))[:1800],
         "预筛分": round(score, 4)}
        for score, item in ranked[:limit]
    ]


def recommend_matches(work_order: dict, candidates: list[dict], config: dict) -> list[dict]:
    if not candidates:
        return []
    aliases = {f"C{index:03d}": item for index, item in enumerate(candidates, start=1)}
    prompt_candidates = [{key: value for key, value in item.items() if key != "id"} | {"id": alias}
                         for alias, item in aliases.items()]
    payload = {
        "work_order": {
            "编号": work_order.get("document_code", ""), "日期": work_order.get("folder_date", ""),
            "专业": work_order.get("discipline", ""), "主题": work_order.get("subject", ""),
            "需求内容": str(work_order.get("需求内容", ""))[:5000],
        },
        "candidates": prompt_candidates,
    }
    prompt = (
        "你是工程设计变更来源追溯助手。判断工作联系单需求是否直接来源于候选设计变更内容。"
        "只有候选内容表达相同工程对象和实质相同的设计调整时才推荐；仅专业相同、关键词相同或主题相关不足以认定。"
        "允许一个联系单对应多条变更。不要推断不存在的事实。只返回 JSON："
        '{"matches":[{"id":"候选id","confidence":0.0,"reason":"简短理由","evidence":"两边对应内容要点"}]}。'
        "没有可靠候选时 matches 返回空数组。confidence 范围 0 到 1，只推荐 confidence 不低于 0.6 的候选。"
    )
    result = _chat_json(config, prompt, json.dumps(payload, ensure_ascii=False), max_tokens=1400)
    matches = []
    for item in result.get("matches", []) if isinstance(result, dict) else []:
        if not isinstance(item, dict) or str(item.get("id", "")) not in aliases:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            continue
        if confidence < 0.6:
            continue
        candidate = aliases[str(item["id"])]
        matches.append({
            "change_id": str(candidate["id"]), "编号": candidate["编号"],
            "说明": candidate["说明"], "专业": candidate["专业"],
            "变更内容": candidate["变更内容"][:800],
            "confidence": round(confidence, 3), "reason": str(item.get("reason", ""))[:240],
            "evidence": str(item.get("evidence", ""))[:400],
        })
    matches.sort(key=lambda item: item["confidence"], reverse=True)
    return matches[:8]


def draft_merged_change(work_orders: list[dict], changes: list[dict], config: dict,
                        fixed_date: str) -> dict:
    if not work_orders:
        raise ValueError("请至少选择一条未形成变更的联系单")
    selected = [{
        "record_id": item["record_id"], "联系单编号": item.get("document_code", ""),
        "日期": item.get("folder_date", ""), "专业": item.get("discipline", ""),
        "主题": item.get("subject", ""), "需求内容": str(item.get("需求内容", ""))[:5000],
    } for item in work_orders]
    numbering_options = next_change_number_options(changes)
    if not numbering_options:
        raise ValueError("现有变更记录中没有可顺延的编号，无法自动生成新编号")
    specialties = [item["专业"] for item in numbering_options]
    number_by_specialty = {item["专业"]: item["下一编号"] for item in numbering_options}
    prompt = (
        "你是工程设计变更整理助手。仅根据用户手动选中的工作联系单，起草一条设计变更并润色语句，使其准确、清晰、规范。"
        "不得添加选中联系单中没有的技术信息；润色不得改变技术含义。发现选中内容互不相关时仍需说明并给出谨慎草稿。"
        f"专业只能从现有变更编号对应的专业中选择：{', '.join(specialties)}。"
        f"日期固定为最后一条真实变更的日期 {fixed_date}，不得按联系单日期、需求日期或当前日期修改。"
        "变更内容需要归纳、缩写选中联系单的要求，只保留设计变更必须明确的技术做法、范围、材料和施工要求，"
        "不得逐字完整复制联系单；控制篇幅，使审批表变更内容与其余字段能够完整排在一张 A4 页面内。"
        "表达简洁但不得遗漏关键技术参数和边界条件。"
        "不要生成或修改编号，编号由程序根据相应专业的现有最后编号顺延生成。"
        "只返回 JSON，字段：title、content、specialty、date、reason。"
    )
    result = _chat_json(config, prompt, json.dumps({
        "work_orders": selected,
        "existing_numbers_by_specialty": numbering_options,
    }, ensure_ascii=False), max_tokens=1800)
    specialty = str(result.get("specialty", "")).strip()
    if specialty not in set(specialties):
        source_specialties = [str(item.get("discipline", "")).strip() for item in work_orders]
        specialty = next((option for source in source_specialties for option in specialties
                          if _same_discipline(source, option)), specialties[0])
    date_value = fixed_date
    code = number_by_specialty[specialty]
    title = str(result.get("title", "")).strip()[:160]
    content = str(result.get("content", "")).strip()[:6000]
    if not content:
        raise RuntimeError("本地 AI 未生成合并后的变更内容")
    return {
        "draft_id": uuid.uuid4().hex,
        "status": "草稿",
        "source_record_ids": [item["record_id"] for item in work_orders],
        "建议编号": code,
        "编号选项": numbering_options,
        "建议日期": date_value,
        "固定真实变更日期": fixed_date,
        "建议专业": specialty,
        "建议说明": title,
        "建议变更内容": content,
        "AI理由": str(result.get("reason", ""))[:600],
        "人工确认": {"编号": "", "日期": "", "专业": "", "说明": "", "变更内容": ""},
        "created_at": _now(),
    }


def proofread_change_content(content: str, config: dict) -> str:
    original = str(content or "").strip()
    if not original:
        raise ValueError("变更内容不能为空")
    if len(original) > 6000:
        raise ValueError("变更内容不能超过 6000 个字符")
    result = _chat_json(
        config,
        "你是工程设计变更通知单的中文校核编辑。请校核并润色用户提供的变更内容，纠正错别字、标点、病句和不规范表达，"
        "使语句准确、简洁、清楚，保留原有条款/序号结构。不得改变技术含义，不得增加、推测或删减技术事实、数字、"
        "计量单位、材料、设备型号、位置、范围、责任主体或施工要求。当前文本用于一页设计变更审批表，尽量维持或缩短篇幅，"
        "不可为了压缩而遗漏必要技术要求。只返回 JSON 对象，字段 revised_content 为完整校核稿。",
        json.dumps({"变更内容": original}, ensure_ascii=False),
        max_tokens=3000,
    )
    revised = str(result.get("revised_content", "")).strip()
    if not revised:
        raise RuntimeError("本地 AI 没有返回校核后的变更内容")
    if len(revised) > 6000:
        raise RuntimeError("本地 AI 返回内容超过 6000 个字符")
    return revised


def compare_text_segments(original: str, revised: str) -> tuple[list[dict], list[dict]]:
    """Return parallel text segments; changed text is highlighted on each side."""
    original_segments: list[dict] = []
    revised_segments: list[dict] = []

    def append(segments: list[dict], text: str, changed: bool) -> None:
        if not text:
            return
        if segments and segments[-1]["changed"] == changed:
            segments[-1]["text"] += text
        else:
            segments.append({"text": text, "changed": changed})

    matcher = difflib.SequenceMatcher(None, original, revised, autojunk=False)
    for tag, original_start, original_end, revised_start, revised_end in matcher.get_opcodes():
        changed = tag != "equal"
        append(original_segments, original[original_start:original_end], changed)
        append(revised_segments, revised[revised_start:revised_end], changed)
    return original_segments, revised_segments


def next_change_number_options(changes: list[dict]) -> list[dict]:
    """Return one next sequential code per profession found in existing change codes."""
    latest: dict[str, tuple[int, int, str, int]] = {}
    for index, item in enumerate(changes):
        code = _change_code(item).strip()
        for specialty, pattern in NUMBERING_RULES:
            match = pattern.fullmatch(code)
            if not match:
                continue
            if specialty == "热力":
                prefix = match.group(1) + match.group(2)
                serial = int(match.group(3))
                width = len(match.group(3))
            else:
                prefix = match.group(1)
                serial = int(match.group(2))
                width = len(match.group(2))
            previous = latest.get(specialty)
            if previous is None or (serial, index) >= (previous[0], previous[1]):
                latest[specialty] = (serial, index, prefix, width)
            break
    result = []
    for specialty, _ in NUMBERING_RULES:
        if specialty not in latest:
            continue
        serial, _, prefix, width = latest[specialty]
        result.append({"专业": specialty, "下一编号": f"{prefix}{str(serial + 1).zfill(max(3, width))}"})
    return result


def _chat_json(config: dict, system_prompt: str, user_prompt: str, max_tokens: int) -> dict:
    base = str(config.get("local_ai_base_url", "")).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or not _is_private(parsed.hostname):
        raise ValueError("本地 AI 地址必须是回环或私网地址")
    payload = {
        "model": config.get("local_ai_model") or "", "temperature": 0.1,
        "max_tokens": max_tokens, "stream": False,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
    }
    headers = {"Content-Type": "application/json"}
    if config.get("local_ai_api_key"):
        headers["Authorization"] = f"Bearer {config['local_ai_api_key']}"
    request = urllib.request.Request(f"{base}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=int(config.get("local_ai_timeout", 180))) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"本地 AI 分析失败：{exc}") from exc
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    text = str(content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("本地 AI 未返回有效 JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("本地 AI 返回的数据格式无效")
    return result


def _change_code(record: dict) -> str:
    return str(record.get("变更通知单编号", "")).strip()


def _normalize(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE).lower()


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _same_discipline(source: str, target: str) -> bool:
    aliases = {"暖通空调": "暖通", "消防水": "给排水", "泳池水": "泳池设备", "泳池机房土建": "泳池设备"}
    left, right = aliases.get(source, source), aliases.get(target, target)
    return bool(left and right and (left == right or left in right or right in left))


def _normalize_date(value: str) -> str:
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", value)
    if not match:
        return ""
    year, month, day = map(int, match.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _is_private(host: str | None) -> bool:
    import ipaddress

    if host in {"localhost", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(host or "")
        return address.is_private or address.is_loopback
    except ValueError:
        return False


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
