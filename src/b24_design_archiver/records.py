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


def _write_json(data: object, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, suffix=".json", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
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


def write_record_json(record: dict[str, str], output_root: Path) -> dict:
    """为一条变更保存元数据，资料直接列在其成果目录下。"""
    folder = record_directory(record, output_root)
    folder.mkdir(parents=True, exist_ok=True)
    item = {EXPORT_NAMES.get(key, key): value for key, value in record.items()
            if key not in {"审批表编号", "何种专业四"}}
    copied = sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*")
                    if path != folder / "record.json" and filesystem_path(path).is_file())
    item["已复制资料"] = bool(copied)
    item["资料文件"] = copied
    _write_json(item, folder / "record.json")
    return item


def write_records_json(records: list[dict[str, str]], output_root: Path) -> Path:
    """更新每条变更的 record.json 和输出根目录的汇总 JSON。"""
    destination = output_root / "B24变更条目.json"
    previous = {}
    if destination.is_file():
        for item in json.loads(destination.read_text(encoding="utf-8")):
            previous[item["_id"]] = {
                key: item[key] for key in ("处理情况", "人工备注", "进展AI判定", "进展状态") if key in item
            }
    exported = [write_record_json(record, output_root) for record in records]
    for item in exported:
        if item["_id"] in previous:
            old = previous[item["_id"]]
            status = old.get("处理情况")
            if isinstance(status, list):
                # Migrate the earlier row-list format in the aggregate JSON.
                from .processing_status import summarize_processing_status
                status = summarize_processing_status(
                    status, is_jz=item["变更通知单编号"].startswith("JZ-")
                )
            if status is not None:
                item["处理情况"] = status
            item["人工备注"] = old.get("人工备注", [])
            for key in ("进展AI判定", "进展状态"):
                if key in old:
                    item[key] = old[key]
        else:
            item["处理情况"] = {
                "蓝图下发情况": {name: "×" for name in ("总包单位", "监理单位", "建设单位")},
                "Excel来源记录": [],
            }
            item["人工备注"] = []
    return _write_json(exported, destination)


def append_record_remark(destination: Path, record_id: str, text: str) -> list[dict[str, str]]:
    """把人工备注作为追加记录写入根目录汇总 JSON。"""
    from datetime import datetime

    records = json.loads(destination.read_text(encoding="utf-8"))
    record = next((item for item in records if item.get("_id") == record_id), None)
    if record is None:
        raise KeyError(f"汇总 JSON 中找不到记录：{record_id}")
    remarks = record.setdefault("人工备注", [])
    if not isinstance(remarks, list):
        raise ValueError("该记录的人工备注格式无效")
    remarks.append({"内容": text, "保存时间": datetime.now().astimezone().isoformat(timespec="seconds")})
    _write_json(records, destination)
    return remarks


def update_progress_fields(destination: Path, record_id: str, **fields: object) -> dict:
    """Persist AI assessment or user-selected progress status in the aggregate JSON."""
    records = json.loads(destination.read_text(encoding="utf-8"))
    record = next((item for item in records if item.get("_id") == record_id), None)
    if record is None:
        raise KeyError(f"汇总 JSON 中找不到记录：{record_id}")
    record.update(fields)
    _write_json(records, destination)
    return {key: record[key] for key in fields}
