"""仅监听本机的变更归档页面与操作接口。"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .config import PROJECT_ROOT, load_config
from .operations import (
    copy_materials, generate_approval, merge_pdfs, record_directory,
    zip_materials, zip_selected_folders,
)
from .records import append_record_remark, load_records, update_progress_fields, write_record_json, write_records_json
from .progress_ai import STATES, classify_progress, local_ai_status
from .sources import SourceIndex
from .work_orders import (
    CODE_PATTERNS, STORE_NAME, candidates_for, draft_merged_change, load_store, load_work_order_records,
    recommend_matches, save_store,
)

LOGGER = logging.getLogger(__name__)
WEB_FILE = PROJECT_ROOT / "src" / "b24_design_archiver" / "web" / "index.html"
SETTINGS_FILE = PROJECT_ROOT / "local_settings.json"
CHECK_MARKS = {"√", "✓", "✔", "☑"}
DEFAULT_PARTIES = {"总包单位", "监理单位", "建设单位"}


class ArchiveState:
    def __init__(self):
        self.config = load_config()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.records: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.sync_status = {"running": False, "completed": 0, "total": 0, "copied": 0, "errors": []}
        self.work_order_match_status = {"running": False, "completed": 0, "total": 0, "errors": []}
        if SETTINGS_FILE.is_file():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if saved.get("output_root"):
                    self.config["output_root"] = Path(saved["output_root"]).expanduser().resolve()
            except (OSError, ValueError) as exc:
                LOGGER.warning("读取本机输出设置失败：%s", exc)
        self.reload()

    def reload(self) -> None:
        historical = load_records(self.config["workbook"])
        work_order_path = Path(self.config.get(
            "work_order_json",
            PROJECT_ROOT / "work-order-data.json",
        )).expanduser().resolve()
        self.config["work_order_json"] = work_order_path
        try:
            self.work_orders = load_work_order_records(work_order_path)
        except FileNotFoundError as exc:
            LOGGER.warning("工作联系单数据不可用，暂不启用关联功能：%s", exc)
            self.work_orders = []
        self.work_order_store_path = self.output_root() / STORE_NAME
        self.work_order_store = load_store(self.work_order_store_path, work_order_path)
        self.historical_records = historical
        self.records = historical + self._published_work_order_changes()
        self.records_json = write_records_json(self.records, self.output_root())
        self.by_id = {record["_id"]: record for record in self.records}
        self.sources = SourceIndex(self.config["source_directories"], [record["变更通知单编号"] for record in self.records])

    def _published_work_order_changes(self) -> list[dict]:
        records = []
        used_codes = {str(record.get("变更通知单编号", "")).strip().upper() for record in self.historical_records}
        for draft in self.work_order_store.get("drafts", []):
            if draft.get("status") != "已纳入页面":
                continue
            values = draft.get("人工确认", {})
            code = str(values.get("编号", "")).strip()
            if not code or code.upper() in used_codes:
                LOGGER.warning("忽略编号为空或重复的新变更草稿：%s", draft.get("draft_id", ""))
                continue
            used_codes.add(code.upper())
            title = str(values.get("说明", "")).strip()
            specialty = str(values.get("专业", "")).strip()
            records.append({
                "_id": f"work-order:{draft['draft_id']}", "_row": "",
                "审批表编号": "", "提出单位一": "工作联系单归并", "变更原因二": "工作联系单需求归并",
                "变更内容三": str(values.get("变更内容", "")).strip(),
                "变更通知单编号": code,
                "变更编号和说明": f"{code}（{title}）" if title else code,
                "日期五": str(values.get("日期", "")).strip(), "六版本": "",
                "是否完成面单": "未填", "何种专业四": specialty, "专业": specialty,
            })
        return records

    def output_root(self) -> Path:
        return self.config["output_root"]

    def set_output_root(self, raw: str) -> str:
        if not raw.strip():
            raise ValueError("输出目录不能为空")
        path = Path(raw).expanduser().resolve()
        if not path.is_absolute() or str(path) == path.anchor:
            raise ValueError("请选择具体的输出文件夹")
        for source in self.config["source_directories"]:
            if path == source or source in path.parents or path in source.parents:
                raise ValueError("输出目录不能与资料源目录重叠")
        if path == self.config["workbook"].parent:
            raise ValueError("输出目录不能是台账所在目录")
        target_store = path / STORE_NAME
        if target_store.is_file() and target_store != self.work_order_store_path:
            raise ValueError("新输出目录已存在工作联系单关联 JSON；请先处理该文件，避免覆盖已有关联数据")
        records_json = write_records_json(self.records, path)
        if self.work_order_store_path.is_file() and self.work_order_store_path != target_store:
            save_store(target_store, self.work_order_store)
        SETTINGS_FILE.write_text(json.dumps({"output_root": str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config["output_root"] = path
        self.work_order_store_path = target_store
        self.records_json = records_json
        return str(path)

    def get_record(self, record_id: str) -> dict:
        if record_id not in self.by_id:
            raise KeyError(f"找不到条目：{record_id}")
        return self.by_id[record_id]

    def records_for_page(self) -> list[dict]:
        aggregate = json.loads(self.records_json.read_text(encoding="utf-8"))
        annotations = {
            record["_id"]: {key: record[key] for key in ("处理情况", "人工备注", "进展AI判定", "进展状态", "进展状态来源") if key in record}
            for record in aggregate
        }
        return [
            {**record, **annotations.get(record["_id"], {}),
             "_work_order_sources": self._work_order_sources_for_change(record["_id"]),
             "_output_exists": self.folder(record).is_dir()}
            for record in self.records
        ]

    def _work_order_sources_for_change(self, change_id: str) -> list[dict]:
        linked_ids = set()
        for record_id, relation in self.work_order_store.get("relations", {}).items():
            if change_id in relation.get("change_ids", []):
                linked_ids.add(record_id)
        for draft in self.work_order_store.get("drafts", []):
            if draft.get("status") == "已纳入页面" and f"work-order:{draft.get('draft_id')}" == change_id:
                linked_ids.update(draft.get("source_record_ids", []))
        by_work_id = {item["record_id"]: item for item in self.work_orders}
        return [{"record_id": item["record_id"], "编号": item.get("document_code", ""),
                 "主题": item.get("subject", ""), "专业": item.get("discipline", "")}
                for key in linked_ids if (item := by_work_id.get(key))]

    def work_order_payload(self) -> dict:
        relation_data = self.work_order_store.get("relations", {})
        records = []
        for item in self.work_orders:
            records.append({
                "record_id": item["record_id"], "document_code": item.get("document_code", ""),
                "sequence_no": item.get("sequence_no", ""), "folder_date": item.get("folder_date", ""),
                "folder_path": item.get("folder_path", ""),
                "subject": item.get("subject", ""), "discipline": item.get("discipline", ""),
                "需求内容": item.get("需求内容", ""), "status": item.get("status", ""),
                "是否需要变更": item.get("是否需要变更", ""), "现场是否已经完成": item.get("现场是否已经完成", ""),
                "作废状态": item.get("作废状态", ""), "relation": relation_data.get(item["record_id"], {}),
            })
        drafts = self.work_order_store.get("drafts", [])
        return {
            "records": records,
            "changes": [{"_id": record["_id"], "编号": record.get("变更通知单编号", ""),
                         "说明": record.get("变更编号和说明", ""), "专业": record.get("专业", ""),
                         "日期": record.get("日期五", ""), "变更内容": record.get("变更内容三", "")}
                        for record in self.records],
            "drafts": drafts,
            "specialties": sorted({str(record.get("专业", record.get("何种专业四", ""))).strip() for record in self.records
                                    if str(record.get("专业", record.get("何种专业四", ""))).strip()} |
                                   {str(item.get("discipline", "")).strip() for item in self.work_orders
                                    if str(item.get("discipline", "")).strip()}),
            "store_file": str(self.work_order_store_path),
        }

    def recommend_work_order_links(self, work_order_id: str) -> dict:
        item = self.work_order_by_id(work_order_id)
        if str(item.get("作废状态", "")).strip() == "是":
            raise ValueError("已作废的工作联系单不能参与 AI 匹配")
        candidates = candidates_for(item, self.records)
        matches = recommend_matches(item, candidates, self.config)
        relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
        status = relation.get("review_status") if relation.get("change_ids") else "待人工审核"
        relation.update({"review_status": status, "ai_candidates": matches, "ai_updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        save_store(self.work_order_store_path, self.work_order_store)
        return {"record_id": work_order_id, "matches": matches, "relation": relation}

    def start_work_order_match_batch(self) -> dict:
        if self.work_order_match_status["running"]:
            return dict(self.work_order_match_status)
        relations = self.work_order_store.get("relations", {})
        pending = [item["record_id"] for item in self.work_orders
                   if str(item.get("作废状态", "")).strip() != "是"
                   and not relations.get(item["record_id"], {}).get("ai_updated_at")
                   and not relations.get(item["record_id"], {}).get("confirmed_at")
                   and not any(item["record_id"] in draft.get("source_record_ids", [])
                               for draft in self.work_order_store.get("drafts", []))]
        self.work_order_match_status = {"running": True, "completed": 0, "total": len(pending), "errors": []}

        def run() -> None:
            for work_order_id in pending:
                try:
                    item = self.work_order_by_id(work_order_id)
                    candidates = candidates_for(item, self.records)
                    matches = recommend_matches(item, candidates, self.config)
                    with self.lock:
                        relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
                        status = relation.get("review_status") if relation.get("change_ids") else "待人工审核"
                        relation.update({"review_status": status, "ai_candidates": matches,
                                         "ai_updated_at": datetime.now().astimezone().isoformat(timespec="seconds")})
                        save_store(self.work_order_store_path, self.work_order_store)
                except Exception as exc:
                    LOGGER.exception("AI 工作联系单匹配失败：%s", work_order_id)
                    with self.lock:
                        self.work_order_match_status["errors"].append({"record_id": work_order_id, "error": str(exc)})
                finally:
                    with self.lock:
                        self.work_order_match_status["completed"] += 1
            with self.lock:
                self.work_order_match_status["running"] = False

        threading.Thread(target=run, name="work-order-ai-match", daemon=True).start()
        return dict(self.work_order_match_status)

    def work_order_by_id(self, work_order_id: str) -> dict:
        item = next((record for record in self.work_orders if record["record_id"] == work_order_id), None)
        if item is None:
            raise KeyError(f"找不到工作联系单条目：{work_order_id}")
        return item

    def set_work_order_links(self, work_order_id: str, change_ids: list[str]) -> dict:
        item = self.work_order_by_id(work_order_id)
        if str(item.get("作废状态", "")).strip() == "是":
            raise ValueError("已作废的工作联系单不能关联变更")
        if not isinstance(change_ids, list) or not change_ids:
            raise ValueError("请选择至少一条要关联的变更")
        if not all(change_id in self.by_id for change_id in change_ids):
            raise ValueError("关联的变更中包含当前台账不存在的条目")
        relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
        has_open_draft = any(draft.get("draft_id") == relation.get("draft_id") and draft.get("status") == "草稿"
                             for draft in self.work_order_store.get("drafts", []))
        has_published_draft = any(draft.get("draft_id") == relation.get("draft_id") and draft.get("status") == "已纳入页面"
                                  for draft in self.work_order_store.get("drafts", []))
        status = "新变更草稿" if has_open_draft else "已形成新变更" if has_published_draft else "已关联变更"
        relation.update({"review_status": status, "change_ids": list(dict.fromkeys(change_ids)),
                         "confirmed_at": datetime.now().astimezone().isoformat(timespec="seconds")})
        save_store(self.work_order_store_path, self.work_order_store)
        return relation

    def create_work_order_draft(self, work_order_ids: list[str]) -> dict:
        unique_ids = list(dict.fromkeys(str(item) for item in work_order_ids))
        if not unique_ids:
            raise ValueError("请选择至少一条工作联系单")
        selected = [self.work_order_by_id(item) for item in unique_ids]
        if any(str(item.get("作废状态", "")).strip() == "是" for item in selected):
            raise ValueError("选择中包含已作废的工作联系单，请移除后再生成草稿")
        existing_draft_ids = {source_id for draft in self.work_order_store.get("drafts", [])
                              if draft.get("status") == "草稿" for source_id in draft.get("source_record_ids", [])}
        already_published = {source_id for draft in self.work_order_store.get("drafts", [])
                             if draft.get("status") == "已纳入页面" for source_id in draft.get("source_record_ids", [])}
        blocked = set(unique_ids) & (existing_draft_ids | already_published)
        if blocked:
            raise ValueError(f"以下联系单已在新变更草稿中，需先处理原草稿：{', '.join(sorted(blocked))}")
        draft = draft_merged_change(selected, self.records, self.config)
        self.work_order_store.setdefault("drafts", []).append(draft)
        for work_order_id in unique_ids:
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            relation.update({"review_status": "新变更草稿", "draft_id": draft["draft_id"]})
        save_store(self.work_order_store_path, self.work_order_store)
        return draft

    def publish_work_order_draft(self, draft_id: str, fields: dict) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", []) if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") == "已纳入页面":
            raise ValueError("该草稿已经纳入主页面")
        confirmed = {
            "编号": str(fields.get("编号", "")).strip(),
            "日期": str(fields.get("日期", "")).strip(),
            "专业": str(fields.get("专业", "")).strip(),
            "说明": str(fields.get("说明", "")).strip(),
            "变更内容": str(fields.get("变更内容", "")).strip(),
        }
        if any(not value for value in confirmed.values()):
            raise ValueError("请填写编号、日期、专业、说明和变更内容")
        if not confirmed["专业"] or len(confirmed["专业"]) > 40:
            raise ValueError("请填写有效专业")
        pattern = CODE_PATTERNS.get(confirmed["专业"])
        if pattern and not re.fullmatch(pattern, confirmed["编号"], re.IGNORECASE):
            raise ValueError("变更编号格式与所选专业不匹配，请按现有编号规则填写")
        if any(record.get("变更通知单编号", "").strip().upper() == confirmed["编号"].upper() for record in self.records):
            raise ValueError(f"变更编号已存在：{confirmed['编号']}")
        if len(confirmed["变更内容"]) > 6000:
            raise ValueError("变更内容不能超过 6000 个字符")
        date_value = confirmed["日期"]
        try:
            confirmed["日期"] = datetime.fromisoformat(date_value).strftime("%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期请使用 YYYY-MM-DD 格式") from exc
        draft["人工确认"] = confirmed
        draft["status"] = "已纳入页面"
        draft["confirmed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        for work_order_id in draft.get("source_record_ids", []):
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            linked_ids = list(dict.fromkeys([*relation.get("change_ids", []), f"work-order:{draft_id}"]))
            relation.update({"review_status": "已形成新变更", "draft_id": draft_id,
                             "change_ids": linked_ids})
        save_store(self.work_order_store_path, self.work_order_store)
        self.reload()
        return {"draft": draft, "records_count": len(self.records)}

    def update_work_order_draft(self, draft_id: str, fields: dict) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", []) if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") == "已纳入页面":
            raise ValueError("已纳入页面的变更不能作为草稿修改")
        allowed = {"编号", "日期", "专业", "说明", "变更内容"}
        confirmed = dict(draft.get("人工确认", {}))
        for key, value in fields.items():
            if key in allowed:
                confirmed[key] = str(value).strip()
        draft["人工确认"] = confirmed
        draft["草稿更新时间"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_store(self.work_order_store_path, self.work_order_store)
        return draft

    def cancel_work_order_draft(self, draft_id: str) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", []) if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") != "草稿":
            raise ValueError("只有尚未纳入页面的草稿可以取消")
        draft["status"] = "已取消"
        draft["cancelled_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        for work_order_id in draft.get("source_record_ids", []):
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            if relation.get("draft_id") == draft_id:
                relation.pop("draft_id", None)
                relation["review_status"] = "待人工审核"
        save_store(self.work_order_store_path, self.work_order_store)
        return draft

    def add_remark(self, record_id: str, text: str) -> list[dict[str, str]]:
        if record_id not in self.by_id:
            raise KeyError(f"找不到条目：{record_id}")
        note = text.strip()
        if not note:
            raise ValueError("备注内容不能为空")
        if len(note) > 2000:
            raise ValueError("备注不能超过 2000 个字符")
        aggregate = json.loads(self.records_json.read_text(encoding="utf-8"))
        saved = next((item for item in aggregate if item.get("_id") == record_id), None)
        if saved is None:
            raise KeyError(f"汇总 JSON 中找不到记录：{record_id}")
        processing = saved.get("处理情况", {})
        sources = processing.get("Excel来源记录", [])
        names = set(DEFAULT_PARTIES)
        for source in sources:
            names.update((source.get("蓝图下发情况") or {}).keys())
        values = []
        if sources:
            for source in sources:
                party_status = source.get("蓝图下发情况") or {}
                values.extend(str(party_status.get(name, "×")).strip() for name in names)
        else:
            values.extend(str(value).strip() for value in processing.get("蓝图下发情况", {}).values())
        if values and all(value in CHECK_MARKS for value in values):
            raise ValueError("该变更已完成，不能添加备注")
        return append_record_remark(self.records_json, record_id, note)

    def classify_record_progress(self, record_id: str) -> dict:
        if record_id not in self.by_id:
            raise KeyError(f"找不到条目：{record_id}")
        data = json.loads(self.records_json.read_text(encoding="utf-8"))
        saved = next((item for item in data if item.get("_id") == record_id), None)
        notes = saved.get("人工备注", []) if saved else []
        assessment = classify_progress(notes, self.config)
        assessment["更新时间"] = datetime.now().astimezone().isoformat(timespec="seconds")
        update_progress_fields(self.records_json, record_id, 进展AI判定=assessment,
                               进展状态=assessment["状态"], 进展状态来源="AI")
        return assessment

    def classify_pending_progress(self) -> dict:
        aggregate = json.loads(self.records_json.read_text(encoding="utf-8"))
        pending = []
        for item in aggregate:
            notes = item.get("人工备注", [])
            if not notes:
                continue
            assessed_at = item.get("进展AI判定", {}).get("更新时间", "")
            if not assessed_at:
                pending.append(item)
                continue
            try:
                assessed_time = datetime.fromisoformat(assessed_at)
                has_new_note = any(
                    datetime.fromisoformat(note.get("保存时间", "")) > assessed_time
                    for note in notes if isinstance(note, dict) and note.get("保存时间")
                )
            except ValueError:
                has_new_note = True
            if has_new_note:
                pending.append(item)
        updated, errors = [], []
        for item in pending:
            code = item.get("变更通知单编号", item.get("_id", ""))
            try:
                self.classify_record_progress(item["_id"])
                updated.append(code)
            except Exception as exc:
                errors.append({"编号": code, "错误": str(exc)})
        return {"total": len(pending), "updated": updated, "errors": errors}

    def set_progress_status(self, record_id: str, status: str) -> dict:
        if record_id not in self.by_id:
            raise KeyError(f"找不到条目：{record_id}")
        if status not in STATES and status != "作废":
            raise ValueError("进展状态无效")
        data = json.loads(self.records_json.read_text(encoding="utf-8"))
        saved = next((item for item in data if item.get("_id") == record_id), None)
        if not saved:
            raise KeyError(f"汇总 JSON 中找不到记录：{record_id}")
        can_mark_void = status == "作废"
        can_restore_void = saved.get("进展状态") == "作废" and saved.get("进展状态来源") == "人工"
        can_adjust_ongoing = saved.get("进展AI判定", {}).get("状态") == "进行中"
        if not (can_mark_void or can_restore_void or can_adjust_ongoing):
            raise ValueError("当前记录仅支持人工标注作废")
        return update_progress_fields(self.records_json, record_id, 进展状态=status, 进展状态来源="人工")

    def folder(self, record: dict) -> Path:
        result = record_directory(record, self.output_root()).resolve()
        if self.output_root().resolve() not in result.parents:
            raise ValueError("输出路径越界")
        return result

    def preview(self, record_id: str) -> dict:
        record = self.get_record(record_id)
        sources = self.sources.for_record(record["变更通知单编号"])
        sources["output_folder"] = str(self.folder(record))
        return sources

    def action(self, record_id: str, action: str, selected_pdfs: list[str] | None = None) -> dict:
        record = self.get_record(record_id)
        folder = self.folder(record)
        if action not in {"generate", "copy", "zip", "merge", "all"}:
            raise ValueError(f"未知操作：{action}")
        preview = self.preview(record_id) if action != "generate" else None
        if action in {"zip", "all"} and not preview["directories"]:
            raise ValueError("未匹配到资料目录，不能生成目录 ZIP")
        if action in {"merge", "all"}:
            available = set(preview["pdfs"])
            if selected_pdfs is None:
                if len(available) > 1:
                    raise ValueError(f"找到 {len(available)} 份 PDF，请打开该条目并选择要合并的文件")
                selected_pdfs = list(available)
            if not selected_pdfs or len(selected_pdfs) != len(set(selected_pdfs)) or not set(selected_pdfs).issubset(available):
                raise ValueError("请选择当前变更匹配到的 PDF")
        results: list[str] = []
        if action in {"generate", "merge", "all"}:
            approval = generate_approval(record, self.config, folder)
            results.append(str(approval))
        if action in {"copy", "all"}:
            results.extend(copy_materials(preview, folder))
            self.records_json = write_records_json(self.records, self.output_root())
        if action in {"zip", "all"}:
            results.extend(zip_materials(preview, folder))
        if action in {"merge", "all"}:
            results.extend(merge_pdfs(approval, {**preview, "pdfs": selected_pdfs}, folder))
        return {"folder": str(folder), "files": results}

    def start_material_sync(self) -> dict:
        if self.sync_status["running"]:
            return dict(self.sync_status)
        self.sources.build()
        records = list(self.records)
        sources = self.sources
        output_root = self.output_root()
        self.sync_status = {"running": True, "completed": 0, "total": len(records), "copied": 0, "errors": []}

        def run() -> None:
            for record in records:
                code = record["变更通知单编号"]
                try:
                    preview = sources.for_record(code)
                    folder = record_directory(record, output_root)
                    copied = copy_materials(preview, folder)
                    with self.lock:
                        self.sync_status["copied"] += len(copied)
                        write_record_json(record, output_root)
                except Exception as exc:
                    LOGGER.exception("同步资料失败：%s", code)
                    with self.lock:
                        self.sync_status["errors"].append(f"{code}: {exc}")
                finally:
                    with self.lock:
                        self.sync_status["completed"] += 1
            with self.lock:
                try:
                    self.records_json = write_records_json(records, output_root)
                except Exception as exc:
                    LOGGER.exception("最终 JSON 同步失败")
                    self.sync_status["errors"].append(f"JSON: {exc}")
                finally:
                    self.sync_status["running"] = False

        threading.Thread(target=run, name="b24-material-sync").start()
        return dict(self.sync_status)


def create_server(state: ArchiveState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            LOGGER.info("HTTP %s", fmt % args)

        def _json(self, data: object, status: int = 200) -> None:
            raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _bad(self, exc: Exception) -> None:
            LOGGER.warning("操作失败：%s", exc)
            status = 404 if isinstance(exc, KeyError) else 400
            self._json({"error": str(exc)}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                raise ValueError("请求内容过大")
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                raw = WEB_FILE.read_text(encoding="utf-8").replace("__SESSION_TOKEN__", state.token).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            try:
                if parsed.path == "/api/records":
                    with state.lock:
                        records = state.records_for_page()
                        self._json({"records": records, "workbook": str(state.config["workbook"]), "output_root": str(state.output_root()), "records_json": str(state.records_json)})
                elif parsed.path == "/api/work-orders":
                    with state.lock:
                        self._json(state.work_order_payload())
                elif parsed.path == "/api/work-order-match-status":
                    with state.lock:
                        self._json(dict(state.work_order_match_status))
                elif parsed.path == "/api/local-ai-status":
                    self._json(local_ai_status(state.config))
                elif parsed.path == "/api/sync-status":
                    with state.lock:
                        self._json(dict(state.sync_status))
                elif parsed.path.startswith("/api/preview/"):
                    from urllib.parse import unquote
                    with state.lock:
                        self._json(state.preview(unquote(parsed.path.removeprefix("/api/preview/"))))
                else:
                    self._json({"error": "页面不存在"}, 404)
            except Exception as exc:
                LOGGER.exception("读取接口失败")
                self._bad(exc)

        def do_POST(self) -> None:
            if self.headers.get("X-Session-Token") != state.token:
                self._json({"error": "无效的本机会话"}, 403)
                return
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{self.headers['Host']}":
                self._json({"error": "来源页面不匹配"}, 403)
                return
            try:
                body = self._body()
                with state.lock:
                    if self.path == "/api/reload":
                        state.reload()
                        self._json({"count": len(state.records)})
                    elif self.path == "/api/rescan":
                        state.sources.invalidate()
                        self._json({"ok": True})
                    elif self.path == "/api/sync-materials":
                        self._json(state.start_material_sync())
                    elif self.path == "/api/output":
                        self._json({"output_root": state.set_output_root(str(body.get("path", "")))})
                    elif self.path == "/api/action":
                        selected = body.get("selected_pdfs")
                        if selected is not None and (not isinstance(selected, list) or not all(isinstance(item, str) for item in selected)):
                            raise ValueError("PDF 选择格式无效")
                        self._json(state.action(str(body.get("id", "")), str(body.get("action", "")), selected))
                    elif self.path == "/api/remark":
                        remarks = state.add_remark(str(body.get("id", "")), str(body.get("text", "")))
                        self._json({"人工备注": remarks})
                    elif self.path == "/api/batch-remark":
                        ids = body.get("ids")
                        text = str(body.get("text", "")).strip()
                        if not isinstance(ids, list) or not ids:
                            raise ValueError("请至少选择一条变更")
                        if not all(isinstance(item, str) for item in ids):
                            raise ValueError("变更编号格式无效")
                        if not text:
                            raise ValueError("请输入统一进展备注")
                        if len(text) > 2000:
                            raise ValueError("备注不能超过 2000 个字符")
                        results = []
                        for record_id in dict.fromkeys(ids):
                            try:
                                remarks = state.add_remark(record_id, text)
                                results.append({"id": record_id, "ok": True, "remarks": remarks})
                            except Exception as exc:
                                results.append({"id": record_id, "ok": False, "error": str(exc)})
                        self._json({"results": results})
                    elif self.path == "/api/progress-ai":
                        assessment = state.classify_record_progress(str(body.get("id", "")))
                        self._json({"进展AI判定": assessment, "进展状态": assessment["状态"], "进展状态来源": "AI"})
                    elif self.path == "/api/progress-ai-batch":
                        self._json(state.classify_pending_progress())
                    elif self.path == "/api/progress-status":
                        updated = state.set_progress_status(str(body.get("id", "")), str(body.get("status", "")))
                        self._json(updated)
                    elif self.path == "/api/work-order-match":
                        self._json(state.recommend_work_order_links(str(body.get("record_id", ""))))
                    elif self.path == "/api/work-order-match-batch":
                        self._json(state.start_work_order_match_batch())
                    elif self.path == "/api/work-order-links":
                        ids = body.get("change_ids")
                        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                            raise ValueError("关联变更编号格式无效")
                        relation = state.set_work_order_links(str(body.get("record_id", "")), ids)
                        self._json({"relation": relation})
                    elif self.path == "/api/work-order-draft":
                        ids = body.get("work_order_ids")
                        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
                            raise ValueError("联系单编号格式无效")
                        self._json({"draft": state.create_work_order_draft(ids)})
                    elif self.path == "/api/work-order-draft-update":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("新变更草稿字段格式无效")
                        self._json({"draft": state.update_work_order_draft(str(body.get("draft_id", "")), fields)})
                    elif self.path == "/api/work-order-draft-cancel":
                        self._json({"draft": state.cancel_work_order_draft(str(body.get("draft_id", "")))})
                    elif self.path == "/api/work-order-publish":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("新变更字段格式无效")
                        self._json(state.publish_work_order_draft(str(body.get("draft_id", "")), fields))
                    elif self.path == "/api/open":
                        record = state.get_record(str(body.get("id", "")))
                        folder = state.folder(record)
                        folder.mkdir(parents=True, exist_ok=True)
                        if os.name == "nt":
                            os.startfile(folder)
                        else:
                            subprocess.Popen(["open" if os.uname().sysname == "Darwin" else "xdg-open", str(folder)])
                        self._json({"folder": str(folder)})
                    elif self.path == "/api/batch-zip":
                        ids = body.get("ids")
                        if not isinstance(ids, list) or not ids:
                            raise ValueError("请先选择至少一条变更")
                        folders = [state.folder(state.get_record(str(item))) for item in ids]
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        destination = state.output_root() / f"B24已选变更_{stamp}.zip"
                        self._json({"file": str(zip_selected_folders(folders, destination))})
                    else:
                        self._json({"error": "接口不存在"}, 404)
            except Exception as exc:
                LOGGER.exception("写入接口失败")
                self._bad(exc)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
