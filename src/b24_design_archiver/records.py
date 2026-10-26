"""把 B24 台账转换为逐条变更记录。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

REQUIRED = (
    "审批表编号", "提出单位一", "变更原因二", "变更内容三", "变更通知单编号",
    "变更编号和说明", "日期五", "六版本", "是否完成面单", "何种专业四",
)


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
            item["_id"] = change_id
            records.append(item)
        return records
    finally:
        book.close()
