"""从 input 的本地 Excel 台账预览或写入处理情况。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from b24_design_archiver.config import load_config  # noqa: E402
from b24_design_archiver.processing_status import apply_processing_status, read_processing_status  # noqa: E402
from b24_design_archiver.records import load_records  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", nargs="+", help="只处理指定变更通知单编号")
    parser.add_argument("--all", action="store_true", help="选择全部 B24 变更")
    parser.add_argument("--apply", action="store_true", help="写入总表 JSON；默认只预览")
    args = parser.parse_args()
    if args.all == bool(args.ids):
        parser.error("请指定 --ids 或 --all（二选一）")
    workbooks = list((ROOT / "input").glob("*.xlsx"))
    if len(workbooks) != 1:
        parser.error("input 目录必须恰好包含一个 .xlsx 文件")
    config = load_config()
    records = load_records(config["workbook"])
    numbers = {record["变更通知单编号"].upper() for record in records} if args.all else {number.strip().upper() for number in args.ids}
    output_root = config["output_root"]
    settings = ROOT / "local_settings.json"
    if settings.is_file():
        saved = json.loads(settings.read_text(encoding="utf-8"))
        if saved.get("output_root"):
            output_root = Path(saved["output_root"]).expanduser().resolve()
    if args.apply:
        result = apply_processing_status(workbooks[0], output_root, numbers)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        found = read_processing_status(workbooks[0], numbers)
        print(json.dumps({number: found.get(number, []) for number in sorted(numbers)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
