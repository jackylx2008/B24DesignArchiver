"""把 B24 台账转换为逐条变更记录。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from .operations import filesystem_path, record_directory

REQUIRED = (
    "审批表编号", "提出单位一", "变更原因二", "变更内容三", "变更通知单编号",
    "变更编号和说明", "日期五", "六版本", "是否完成面单", "何种专业四",
)

EXPORT_NAMES = {
    "提出单位一": "提出单位",
    "变更原因二": "变更原因",
    "变更内容三": "变更内容",
    "日期五": "日期",
    "六版本": "版本",
}


def specialty_for(change_id: str) -> str:
    code = change_id.upper()
    if code.startswith("JZ-05"):
        return "精装给排水"
    if code.startswith("JZ-06"):
        return "精装暖通"
    if code.startswith("05"):
        return "给排水"
    if code.startswith("06"):
        return "暖通"
    if code.startswith(("R20", "RL20")):
        return "热力"
    if code.startswith("251614GC"):
        return "燃气"
    if change_id.startswith("泳池变更"):
        return "泳池设备"
    return ""


def _value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def load_records(workbook: Path) -> list[dict[str, str]]:
    if not workbook.is_file():
        raise FileNotFoundError(f"找不到 B24 台账：{workbook}")
    book = load_workbook(workbook, read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        rows = sheet.values
        header = [_value(cell) for cell in next(rows)]
        missing = set(REQUIRED) - set(header)
        if missing:
            raise ValueError(f"台账缺少列：{', '.join(sorted(missing))}")
        records = []
        seen = set()
        for row_number, cells in enumerate(rows, start=2):
            item = {name: _value(cells[index]) if index < len(cells) else "" for index, name in enumerate(header)}
            change_id = item["变更通知单编号"]
            if not change_id:
                continue
            if change_id in seen:
                raise ValueError(f"台账第 {row_number} 行变更编号重复：{change_id}")
            seen.add(change_id)
            item["_row"] = str(row_number)
            item["_id"] = hashlib.sha256(change_id.encode("utf-8")).hexdigest()
            item["专业"] = specialty_for(change_id)
            records.append(item)
        return records
    finally:
        book.close()


def write_records_json(records: list[dict[str, str]], output_root: Path) -> Path:
    """保存使用清晰字段名的条目 JSON，不导出台账模板标记列。"""
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / "B24变更条目.json"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_root, suffix=".json", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            exported = []
            for record in records:
                item = {EXPORT_NAMES.get(key, key): value for key, value in record.items()
                        if key not in {"审批表编号", "何种专业四"}}
                folder = record_directory(record, output_root)
                copied = []
                for name in ("原始资料目录", "资料文件"):
                    material_root = folder / name
                    if material_root.is_dir():
                        copied.extend(path.relative_to(output_root).as_posix() for path in material_root.rglob("*") if filesystem_path(path).is_file())
                item["已复制资料"] = bool(copied)
                item["资料文件"] = sorted(copied)
                exported.append(item)
            json.dump(exported, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        for attempt in range(10):
            try:
                temporary.replace(destination)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
    return destination
