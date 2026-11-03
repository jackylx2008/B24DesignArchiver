"""仅监听本机的变更归档页面与操作接口。"""

from __future__ import annotations

import base64
import copy
import hashlib
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
    append_work_order_appendices, build_work_order_change_package, copy_materials, generate_approval,
    desktop_directory, merge_pdfs, pdf_page_count, record_directory, remove_blank_pdf_pages,
    render_work_order_notice_artifacts, safe_name, work_order_directory_name, zip_materials,
    zip_selected_records,
)
from .records import append_record_remark, load_records, update_progress_fields, write_record_json, write_records_json
from .progress_ai import STATES, classify_progress, local_ai_status
from .sources import SourceIndex
from .work_orders import (
    CODE_PATTERNS, STORE_NAME, candidates_for, compare_text_segments, draft_merged_change,
    load_store, load_work_order_records, next_change_number_options, proofread_change_content,
    refresh_project_work_order_json,
    recommend_matches, save_store,
)

LOGGER = logging.getLogger(__name__)
WEB_FILE = PROJECT_ROOT / "src" / "b24_design_archiver" / "web" / "index.html"
SETTINGS_FILE = PROJECT_ROOT / "local_settings.json"
CHECK_MARKS = {"√", "✓", "✔", "☑"}
DEFAULT_PARTIES = {"总包单位", "监理单位", "建设单位"}


def _work_order_preview_hash(fields: dict) -> str:
    values = {key: str(fields.get(key, "")).strip()
              for key in ("编号", "日期", "专业", "说明", "变更内容")}
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ArchiveState:
    def __init__(self):
        self.config = load_config()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.records: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.work_order_preview_pdfs: dict[str, bytes] = {}
        self.work_order_preview_docxs: dict[str, bytes] = {}
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
        source_work_order_path = Path(self.config.get(
            "work_order_json",
            PROJECT_ROOT / "work-order-data.json",
        )).expanduser().resolve()
        snapshot_path = self.config.get("work_order_snapshot_json")
        work_order_path = source_work_order_path
        if snapshot_path:
            snapshot_path = Path(snapshot_path).expanduser().resolve()
            try:
                sync = refresh_project_work_order_json(source_work_order_path, snapshot_path)
                work_order_path = snapshot_path
                LOGGER.info("已更新项目工作联系单 JSON：新增 %d 条、刷新 %d 条，累计 %d 条：%s",
                            sync["added"], sync["updated"], sync["total"], snapshot_path)
            except FileNotFoundError as exc:
                LOGGER.warning("酒店联系单源 JSON 不可用，尝试读取项目缓存：%s", exc)
                if snapshot_path.is_file():
                    work_order_path = snapshot_path
        source_roots = []
        if work_order_path.is_file():
            try:
                work_order_metadata = json.loads(work_order_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                LOGGER.warning("读取联系单数据中的原始目录信息失败：%s", exc)
                work_order_metadata = {}
            if isinstance(work_order_metadata, dict):
                data_root = work_order_metadata.get("data_root")
                if data_root:
                    data_root_path = Path(data_root).expanduser()
                    if not data_root_path.is_absolute():
                        data_root_path = work_order_path.parent / data_root_path
                    source_roots.append(data_root_path)
                snapshot_source_file = work_order_metadata.get("snapshot_source_file")
                if snapshot_source_file:
                    source_roots.append(Path(snapshot_source_file).parent)
        source_roots.extend((source_work_order_path.parent, work_order_path.parent))
        normalized_roots = []
        seen_roots = set()
        for root in source_roots:
            resolved_root = Path(root).expanduser().resolve()
            if resolved_root not in seen_roots:
                normalized_roots.append(resolved_root)
                seen_roots.add(resolved_root)
        self.config["work_order_source_roots"] = normalized_roots
        # Keep the singular setting for compatibility with callers from older code.
        self.config["work_order_source_root"] = normalized_roots[0] if normalized_roots else source_work_order_path.parent
        LOGGER.info("工作联系单附件原始目录候选：%s", "；".join(map(str, normalized_roots)))
        self.config["work_order_json"] = work_order_path
        try:
            self.work_orders = load_work_order_records(work_order_path)
        except FileNotFoundError as exc:
            LOGGER.warning("工作联系单数据不可用，暂不启用关联功能：%s", exc)
            self.work_orders = []
        else:
            LOGGER.info("已刷新原始工作联系单 JSON：%s（%d 条）", work_order_path, len(self.work_orders))
        self.work_order_store_path = self.output_root() / STORE_NAME
        self.work_order_store = load_store(self.work_order_store_path, work_order_path)
        self.historical_records = historical
        migrated_dates = False
        latest_change_date = self._latest_real_change_date() if historical else ""
        for draft in self.work_order_store.get("drafts", []):
            if draft.get("status") == "草稿" and not draft.get("固定真实变更日期") and latest_change_date:
                draft["固定真实变更日期"] = latest_change_date
                draft["建议日期"] = latest_change_date
                draft.setdefault("人工确认", {})["日期"] = latest_change_date
                draft.pop("PDF预览哈希", None)
                draft.pop("PDF预览页数", None)
                migrated_dates = True
        if migrated_dates:
            save_store(self.work_order_store_path, self.work_order_store)
        self.records = historical + self._published_work_order_changes()
        self._reconcile_draft_numbers()
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
                "_folder_name": str(draft.get("目录名", "")).strip(),
                "日期五": str(values.get("日期", "")).strip(), "六版本": "",
                "是否完成面单": "未填", "何种专业四": specialty, "专业": specialty,
                "确认状态": draft.get("确认状态") or "待确认",
            })
        return records

    def output_root(self) -> Path:
        return self.config["output_root"]

    def _latest_real_change_date(self) -> str:
        dates = []
        for record in self.historical_records:
            raw = str(record.get("日期五", "")).strip()[:10]
            try:
                dates.append(datetime.strptime(raw, "%Y-%m-%d").date())
            except ValueError:
                continue
        if not dates:
            raise ValueError("真实变更台账没有有效日期，无法锁定新变更日期")
        return max(dates).isoformat()

    def _source_files_for_draft(
        self, draft: dict,
    ) -> tuple[list[tuple[Path, str]], list[tuple[Path, str]], list[tuple[Path, str]]]:
        source_roots = [Path(root).resolve() for root in self.config.get("work_order_source_roots", [])]
        if not source_roots:
            source_roots = [Path(
                self.config.get("work_order_source_root") or Path(self.config["work_order_json"]).parent
            ).resolve()]
        drawings: list[tuple[Path, str]] = []
        words: list[tuple[Path, str]] = []
        appendix_pdfs: list[tuple[Path, str]] = []
        seen_drawings: set[Path] = set()
        seen_words: set[Path] = set()
        seen_pdfs: set[Path] = set()
        for record_id in draft.get("source_record_ids", []):
            item = self.work_order_by_id(record_id)
            origin = str(item.get("document_code") or item.get("record_id") or "工作联系单")
            for file_info in item.get("files", []):
                if not isinstance(file_info, dict):
                    continue
                role = str(file_info.get("role", ""))
                extension = str(file_info.get("extension", "")).lower()
                raw_path = str(file_info.get("path", "")).strip()
                if not raw_path:
                    continue
                candidate = Path(raw_path)
                candidate_paths = ([candidate] if candidate.is_absolute()
                                   else [root / candidate for root in source_roots])
                resolved_candidates = [path.resolve() for path in candidate_paths]
                allowed = [(path, root) for path in resolved_candidates for root in source_roots
                           if path.is_relative_to(root)]
                if not allowed:
                    raise ValueError(f"联系单附件路径超出原始数据目录：{file_info.get('name', raw_path)}")
                resolved = next((path for path, _ in allowed if path.is_file()), allowed[0][0])
                if not resolved.is_file():
                    if role in {"drawing_source", "source_word", "issued_pdf", "attachment_pdf"} or extension == ".dwg":
                        attempted = "；".join(str(path) for path, _ in allowed)
                        raise FileNotFoundError(
                            f"联系单附件不存在：{file_info.get('name', resolved.name)}；已检查路径：{attempted}"
                        )
                    continue
                suffix = resolved.suffix.lower()
                if role == "drawing_source" or extension == ".dwg" or suffix == ".dwg":
                    if resolved not in seen_drawings:
                        drawings.append((resolved, origin))
                        seen_drawings.add(resolved)
                if (role == "source_word" or suffix in {".doc", ".docx", ".docm", ".dot", ".dotx"}) and resolved not in seen_words:
                    words.append((resolved, origin))
                    seen_words.add(resolved)
                if role in {"issued_pdf", "attachment_pdf"} and suffix == ".pdf":
                    if resolved not in seen_pdfs:
                        appendix_pdfs.append((resolved, role if role in {"issued_pdf", "attachment_pdf"} else "attachment_pdf"))
                        seen_pdfs.add(resolved)
        return drawings, words, appendix_pdfs

    def _numbering_records(self, exclude_draft_id: str = "") -> list[dict]:
        records = list(self.records)
        for draft in self.work_order_store.get("drafts", []):
            if draft.get("status") != "草稿" or draft.get("draft_id") == exclude_draft_id:
                continue
            code = str(draft.get("建议编号") or draft.get("人工确认", {}).get("编号", "")).strip()
            if code:
                records.append({"变更通知单编号": code})
        return records

    def _reconcile_draft_numbers(self) -> None:
        """Reassign active drafts in creation order after a draft releases a number."""
        assigned = list(self.records)
        active = [draft for draft in self.work_order_store.get("drafts", [])
                  if draft.get("status") == "草稿"]
        active.sort(key=lambda draft: str(draft.get("created_at", "")))
        changed = False
        for draft in active:
            options = next_change_number_options(assigned)
            specialty = str(draft.get("人工确认", {}).get("专业")
                            or draft.get("建议专业", "")).strip()
            next_code = next((item["下一编号"] for item in options
                              if item["专业"] == specialty), "")
            if draft.get("编号选项") != options:
                draft["编号选项"] = options
                changed = True
            if next_code:
                old_code = str(draft.get("人工确认", {}).get("编号")
                               or draft.get("建议编号", "")).strip()
                if old_code != next_code or draft.get("建议编号") != next_code:
                    draft["建议编号"] = next_code
                    confirmed = draft.setdefault("人工确认", {})
                    if confirmed.get("编号"):
                        confirmed["编号"] = next_code
                    for key in ("PDF预览哈希", "PDF预览页数", "PDF预览时间", "灰度测试包"):
                        draft.pop(key, None)
                    draft_id = str(draft.get("draft_id", ""))
                    self.work_order_preview_pdfs.pop(draft_id, None)
                    self.work_order_preview_docxs.pop(draft_id, None)
                    changed = True
                assigned.append({"变更通知单编号": next_code})
        if changed:
            save_store(self.work_order_store_path, self.work_order_store)

    def _work_order_draft_code_reserved(self, code: str, exclude_draft_id: str = "") -> bool:
        return any(
            draft.get("draft_id") != exclude_draft_id
            and draft.get("status") in {"草稿", "已纳入页面"}
            and str(draft.get("人工确认", {}).get("编号") or draft.get("建议编号", "")).strip().upper() == code.strip().upper()
            for draft in self.work_order_store.get("drafts", [])
        )

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
        drafts = self.work_order_store.get("drafts", [])
        active_draft_sources = {record_id for draft in drafts if draft.get("status") in {"草稿", "已纳入页面"}
                                for record_id in draft.get("source_record_ids", [])}
        changes = [{"_id": record["_id"], "编号": record.get("变更通知单编号", ""),
                    "说明": record.get("变更编号和说明", ""), "专业": record.get("专业", ""),
                    "日期": record.get("日期五", ""), "变更内容": record.get("变更内容三", "")}
                   for record in self.records]
        changes_by_id = {item["_id"]: item for item in changes}
        records = []
        for item in self.work_orders:
            relation = relation_data.get(item["record_id"], {})
            linked_ids = relation.get("change_ids", []) if relation.get("confirmed_at") else []
            linked_changes = [changes_by_id[change_id] for change_id in linked_ids if change_id in changes_by_id]
            open_draft = next((draft for draft in drafts if draft.get("status") == "草稿"
                               and item["record_id"] in draft.get("source_record_ids", [])), None)
            published_drafts = [draft for draft in drafts if draft.get("status") == "已纳入页面"
                                and item["record_id"] in draft.get("source_record_ids", [])]
            pending_confirmation = any(draft.get("确认状态") == "待确认" for draft in published_drafts)
            for draft in published_drafts:
                values = draft.get("人工确认", {})
                code = str(values.get("编号", "")).strip()
                if code:
                    linked_changes.append({"编号": code, "说明": f"{code}（{values.get('说明', '').strip()}）"})
            corresponding_changes = list(dict.fromkeys(
                str(change.get("说明") or change.get("编号", "")).strip()
                for change in linked_changes if change.get("说明") or change.get("编号")
            ))
            records.append({
                "record_id": item["record_id"], "discipline": item.get("discipline", ""),
                "sequence_no": item.get("sequence_no", ""), "document_code": item.get("document_code", ""),
                "folder_date": item.get("folder_date", ""), "subject": item.get("subject", ""),
                "folder_path": item.get("folder_path", ""), "files": item.get("files", []),
                "需求内容": item.get("需求内容", ""), "致送单位": item.get("致送单位", ""),
                "对应的变更": "、".join(corresponding_changes),
                "是否已经转化为变更": "待确认" if pending_confirmation else "是" if corresponding_changes else "否",
                "是否需要转化为变更": self.work_order_store.get("conversion_decisions", {}).get(item["record_id"], ""),
                "是否已出变更联系单": self.work_order_store.get("issued_change_decisions", {}).get(item["record_id"], ""),
                "是否有草稿": "是" if open_draft else "否",
                "可以生成变更": bool(self.work_order_store.get("conversion_decisions", {}).get(item["record_id"]) == "是"
                                   and str(item.get("作废状态", "")).strip() != "是"
                                   and not corresponding_changes
                                   and item["record_id"] not in active_draft_sources),
                "现场是否已经完成": item.get("现场是否已经完成", ""), "作废状态": item.get("作废状态", ""),
                "status": item.get("status", ""), "status_label": item.get("status_label", ""),
                "warnings": item.get("warnings", []), "relation": relation_data.get(item["record_id"], {}),
            })
        return {
            "records": records,
            "columns": ["专业", "编号", "日期", "致送单位", "主题", "需求内容",
                        "对应的变更", "是否需要转化为变更", "是否已经转化为变更", "是否已出变更联系单", "是否有草稿"],
            "changes": changes,
            "drafts": drafts,
            "specialties": sorted({str(record.get("专业", record.get("何种专业四", ""))).strip() for record in self.records
                                    if str(record.get("专业", record.get("何种专业四", ""))).strip()} |
                                   {str(item.get("discipline", "")).strip() for item in self.work_orders
                                    if str(item.get("discipline", "")).strip()}),
            "编号选项": next_change_number_options(self._numbering_records()),
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

    def set_work_order_conversion_decision(self, work_order_id: str, decision: str) -> str:
        self.work_order_by_id(work_order_id)
        if decision not in {"", "是", "否"}:
            raise ValueError("是否需要转化为变更只能选择是、否或未填写")
        decisions = self.work_order_store.setdefault("conversion_decisions", {})
        if decision:
            decisions[work_order_id] = decision
        else:
            decisions.pop(work_order_id, None)
        save_store(self.work_order_store_path, self.work_order_store)
        return decision

    def set_work_order_issued_decision(self, work_order_id: str, decision: str) -> str:
        self.work_order_by_id(work_order_id)
        if decision not in {"", "是", "否"}:
            raise ValueError("是否已出变更联系单只能选择是、否或未填写")
        decisions = self.work_order_store.setdefault("issued_change_decisions", {})
        if decision:
            decisions[work_order_id] = decision
        else:
            decisions.pop(work_order_id, None)
        save_store(self.work_order_store_path, self.work_order_store)
        return decision

    def mark_work_order_ai_result_wrong(self, work_order_id: str) -> dict:
        item = self.work_order_by_id(work_order_id)
        if str(item.get("作废状态", "")).strip() == "是":
            raise ValueError("已作废的工作联系单不参与 AI 结果审核")
        relation = self.work_order_store.get("relations", {}).get(work_order_id)
        if not relation or not relation.get("ai_updated_at"):
            raise ValueError("该工作联系单尚无 AI 匹配结果可供审核")
        ai_updated_at = relation["ai_updated_at"]
        reviews = relation.setdefault("ai_manual_reviews", [])
        existing = next((review for review in reviews
                         if review.get("ai_updated_at") == ai_updated_at and review.get("result") == "错误"), None)
        if existing is None:
            reviews.append({
                "ai_updated_at": ai_updated_at,
                "result": "错误",
                "reviewer": "人工",
                "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            })
        save_store(self.work_order_store_path, self.work_order_store)
        return relation

    def create_work_order_draft(self, work_order_ids: list[str]) -> dict:
        unique_ids = list(dict.fromkeys(str(item) for item in work_order_ids))
        if not unique_ids:
            raise ValueError("请选择至少一条工作联系单")
        if not local_ai_status(self.config).get("available"):
            raise RuntimeError("AI未启动，无法进行工作")
        selected = [self.work_order_by_id(item) for item in unique_ids]
        if any(str(item.get("作废状态", "")).strip() == "是" for item in selected):
            raise ValueError("选择中包含已作废的工作联系单，请移除后再生成草稿")
        if any(self.work_order_store.get("conversion_decisions", {}).get(item["record_id"]) != "是"
               for item in selected):
            raise ValueError("仅能为人工标记‘需要转化为变更’的联系单生成草稿")
        already_linked = [item["record_id"] for item in selected
                          if (relation := self.work_order_store.get("relations", {}).get(item["record_id"], {})).get("confirmed_at")
                          and any(change_id in self.by_id for change_id in relation.get("change_ids", []))]
        if already_linked:
            raise ValueError("所选联系单已有已确认的对应变更，不能重复生成：" + ", ".join(already_linked))
        existing_draft_ids = {source_id for draft in self.work_order_store.get("drafts", [])
                              if draft.get("status") == "草稿" for source_id in draft.get("source_record_ids", [])}
        already_published = {source_id for draft in self.work_order_store.get("drafts", [])
                             if draft.get("status") == "已纳入页面" for source_id in draft.get("source_record_ids", [])}
        blocked = set(unique_ids) & (existing_draft_ids | already_published)
        if blocked:
            raise ValueError("所选工作联系单已有草稿或已生成变更，请打开已有草稿继续处理，避免重复生成。")
        draft = draft_merged_change(
            selected, self._numbering_records(), self.config, self._latest_real_change_date(),
        )
        self.work_order_store.setdefault("drafts", []).append(draft)
        for work_order_id in unique_ids:
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            relation.update({"review_status": "新变更草稿", "draft_id": draft["draft_id"]})
        save_store(self.work_order_store_path, self.work_order_store)
        return draft

    def render_work_order_draft_preview(self, draft_id: str, fields: dict) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", [])
                      if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") != "草稿":
            raise ValueError("只有待审核草稿可以生成 PDF 预览")
        values = {key: str(fields.get(key, "")).strip()
                  for key in ("编号", "日期", "专业", "说明", "变更内容")}
        if any(not value for value in values.values()):
            raise ValueError("请先补齐编号、日期、专业、说明和变更内容，再生成完整通知单 PDF")
        try:
            datetime.strptime(values["日期"], "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期请使用 YYYY-MM-DD 格式") from exc
        fixed_date = str(draft.get("固定真实变更日期", "")).strip()
        if not fixed_date or values["日期"] != fixed_date:
            raise ValueError(f"新变更日期固定为最后一条真实变更日期：{fixed_date or '未设置'}")
        valid_number = next((item["下一编号"] for item in draft.get("编号选项", [])
                             if item.get("专业") == values["专业"]), "")
        if not valid_number:
            valid_number = next((item["下一编号"] for item in next_change_number_options(
                self._numbering_records(exclude_draft_id=draft_id)) if item["专业"] == values["专业"]), "")
        if values["编号"] != valid_number:
            raise ValueError("编号必须按所选专业的现有编号顺延生成，请重新选择专业")
        if values["专业"] not in CODE_PATTERNS:
            raise ValueError("专业不在现有变更编号专业范围内")
        if any(record.get("变更通知单编号", "").strip().upper() == values["编号"].upper()
               for record in self.records):
            raise ValueError(f"变更编号已存在：{values['编号']}")
        if self._work_order_draft_code_reserved(values["编号"], exclude_draft_id=draft_id):
            raise ValueError("该顺延编号已被另一条草稿占用，请取消其中一条草稿后再生成 PDF")
        source_records = [self.work_order_by_id(record_id) for record_id in draft.get("source_record_ids", [])]
        recipients = list(dict.fromkeys(str(item.get("致送单位", "")).strip()
                                        for item in source_records if str(item.get("致送单位", "")).strip()))
        subjects = list(dict.fromkeys(str(item.get("subject", "")).strip()
                                      for item in source_records if str(item.get("subject", "")).strip()))
        record = {
            "_id": f"work-order-preview:{draft_id}", "_row": "",
            "审批表编号": "", "提出单位一": "、".join(recipients) or "工作联系单",
            "变更原因二": "；".join(subjects) or "工作联系单需求",
            "变更内容三": values["变更内容"],
            "变更通知单编号": values["编号"],
            "变更编号和说明": f"{values['编号']}（{values['说明']}）",
            "日期五": values["日期"], "六版本": "",
            "是否完成面单": "未填", "何种专业四": values["专业"], "专业": values["专业"],
        }
        drawing_paths, _, appendix_pdfs = self._source_files_for_draft(draft)
        notice_pdf, notice_docx = render_work_order_notice_artifacts(record, self.config, bool(drawing_paths))
        notice_pages = pdf_page_count(notice_pdf)
        pdf_bytes, _ = append_work_order_appendices(notice_pdf, appendix_pdfs)
        preview_hash = _work_order_preview_hash(values)
        if notice_pages == 1:
            draft["PDF预览哈希"] = preview_hash
            draft["PDF预览页数"] = notice_pages
            self.work_order_preview_pdfs[draft_id] = pdf_bytes
            self.work_order_preview_docxs[draft_id] = notice_docx
        else:
            draft.pop("PDF预览哈希", None)
            draft.pop("PDF预览页数", None)
            self.work_order_preview_pdfs.pop(draft_id, None)
            self.work_order_preview_docxs.pop(draft_id, None)
        draft["PDF预览时间"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_store(self.work_order_store_path, self.work_order_store)
        return {
            "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
            "preview_hash": preview_hash, "notice_page_count": notice_pages,
            "one_page": notice_pages == 1,
            "removed_blank_pages": [],
            "appended_pages": max(0, pdf_page_count(pdf_bytes) - notice_pages),
        }

    def proofread_work_order_content(self, draft_id: str, content: str) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", [])
                      if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") != "草稿":
            raise ValueError("只有待审核草稿可以进行 AI 校核")
        original = str(content or "").strip()
        revised = proofread_change_content(original, self.config)
        original_segments, revised_segments = compare_text_segments(original, revised)
        return {
            "revised_content": revised,
            "original_segments": original_segments,
            "revised_segments": revised_segments,
            "model": self.config.get("local_ai_model", ""),
        }

    def plan_work_order_gray_package(self, draft_id: str, fields: dict) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", [])
                      if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") != "草稿":
            raise ValueError("只有待审核草稿可以生成变更目录")
        values = {key: str(fields.get(key, "")).strip()
                  for key in ("编号", "日期", "专业", "说明", "变更内容")}
        preview_hash = _work_order_preview_hash(values)
        if draft.get("PDF预览哈希") != preview_hash or draft.get("PDF预览页数") != 1:
            raise ValueError("字段已变化或通知单不是一页，请重新渲染并审核 PDF")
        reviewed_pdf = self.work_order_preview_pdfs.get(draft_id)
        reviewed_docx = self.work_order_preview_docxs.get(draft_id)
        if not reviewed_pdf or not reviewed_docx:
            raise ValueError("当前 PDF 或其 Word 原件已失效，请重新渲染完整变更单")
        source_records = [self.work_order_by_id(record_id) for record_id in draft.get("source_record_ids", [])]
        recipients = list(dict.fromkeys(str(item.get("致送单位", "")).strip()
                                        for item in source_records if str(item.get("致送单位", "")).strip()))
        subjects = list(dict.fromkeys(str(item.get("subject", "")).strip()
                                      for item in source_records if str(item.get("subject", "")).strip()))
        record = {
            "_id": f"work-order-preview:{draft_id}", "_row": "", "审批表编号": "",
            "提出单位一": "、".join(recipients) or "工作联系单",
            "变更原因二": "；".join(subjects) or "工作联系单需求",
            "变更内容三": values["变更内容"], "变更通知单编号": values["编号"],
            "变更编号和说明": f"{values['编号']}（{values['说明']}）",
            "日期五": values["日期"], "六版本": "", "是否完成面单": "未填",
            "何种专业四": values["专业"], "专业": values["专业"],
        }
        folder_name = work_order_directory_name(record)
        record["_folder_name"] = folder_name
        drawings, _, _ = self._source_files_for_draft(draft)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        package_path = self.output_root() / "_灰度测试" / safe_name(draft_id, 80) / stamp / folder_name
        relative_files = [f"{folder_name}.pdf", f"word&CAD/{folder_name}.docx"]
        known_names = {f"{folder_name}.docx".casefold()}
        for source, origin in drawings:
            source_name = source.name
            relative = f"word&CAD/{source_name}"
            if source_name.casefold() in known_names:
                relative = f"word&CAD/{safe_name(origin or source.parent.name, 72)}/{source_name}"
            known_names.add(source_name.casefold())
            relative_files.append(relative)
        return {"路径": str(package_path), "文件": relative_files, "字段哈希": preview_hash,
                "生成时间": datetime.now().astimezone().isoformat(timespec="seconds")}

    def create_work_order_gray_package(self, draft_id: str, fields: dict, planned_path: str = "") -> dict:
        plan = self.plan_work_order_gray_package(draft_id, fields)
        draft = next(item for item in self.work_order_store.get("drafts", [])
                     if item.get("draft_id") == draft_id)
        package_path = Path(planned_path).resolve() if planned_path else Path(plan["路径"]).resolve()
        gray_root = (self.output_root() / "_灰度测试").resolve()
        if not package_path.is_relative_to(gray_root) or package_path.name != Path(plan["路径"]).name:
            raise ValueError("变更目录目标路径无效，请重新生成")
        if package_path.exists():
            raise FileExistsError(f"临时变更目录已存在，为避免覆盖已停止生成：{package_path}")
        source_records = [self.work_order_by_id(record_id) for record_id in draft.get("source_record_ids", [])]
        recipients = list(dict.fromkeys(str(item.get("致送单位", "")).strip()
                                        for item in source_records if str(item.get("致送单位", "")).strip()))
        subjects = list(dict.fromkeys(str(item.get("subject", "")).strip()
                                      for item in source_records if str(item.get("subject", "")).strip()))
        values = {key: str(fields.get(key, "")).strip()
                  for key in ("编号", "日期", "专业", "说明", "变更内容")}
        record = {
            "_id": f"work-order-preview:{draft_id}", "_row": "", "审批表编号": "",
            "提出单位一": "、".join(recipients) or "工作联系单",
            "变更原因二": "；".join(subjects) or "工作联系单需求",
            "变更内容三": values["变更内容"], "变更通知单编号": values["编号"],
            "变更编号和说明": f"{values['编号']}（{values['说明']}）",
            "日期五": values["日期"], "六版本": "", "是否完成面单": "未填",
            "何种专业四": values["专业"], "专业": values["专业"], "_folder_name": package_path.name,
        }
        drawings, _, _ = self._source_files_for_draft(draft)
        reviewed_pdf = self.work_order_preview_pdfs[draft_id]
        reviewed_docx = self.work_order_preview_docxs[draft_id]
        package_path, files = build_work_order_change_package(
            record, self.config, reviewed_pdf, reviewed_docx, drawings, package_path.parent,
        )
        package = {
            "路径": str(package_path), "文件": files, "字段哈希": plan["字段哈希"],
            "格式版本": 2,
            "生成时间": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        draft["灰度测试包"] = package
        save_store(self.work_order_store_path, self.work_order_store)
        return {"package": package}

    def publish_work_order_draft(self, draft_id: str, fields: dict, reviewed_all_fields: bool = False,
                                 reviewed_pdf_hash: str = "", reviewed_package: bool = False) -> dict:
        draft = next((item for item in self.work_order_store.get("drafts", []) if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到新变更草稿")
        if draft.get("status") == "已纳入页面":
            raise ValueError("该草稿已经纳入主页面")
        if reviewed_all_fields is not True:
            raise ValueError("请先人工审核变更单全部字段并勾选确认")
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
        fixed_date = str(draft.get("固定真实变更日期", "")).strip()
        if not fixed_date or confirmed["日期"] != fixed_date:
            raise ValueError(f"新变更日期固定为最后一条真实变更日期：{fixed_date or '未设置'}")
        if draft.get("PDF预览页数") != 1:
            raise ValueError("通知单必须完整排在一页内，请缩短变更内容后重新渲染并审核 PDF")
        preview_hash = _work_order_preview_hash(confirmed)
        if draft.get("PDF预览哈希") != preview_hash or reviewed_pdf_hash != preview_hash:
            raise ValueError("变更单字段已变化或尚未生成对应 PDF，请刷新 PDF 并重新审核整张变更单")
        package_review = draft.get("灰度测试包", {})
        if (reviewed_package is not True or package_review.get("字段哈希") != preview_hash
                or package_review.get("格式版本") != 2):
            raise ValueError("请重新生成变更目录，并人工核对文件清单")
        gray_path = Path(str(package_review.get("路径", ""))).resolve()
        if not gray_path.is_dir():
            raise ValueError("临时变更目录不存在，请重新生成并核对文件清单")
        reviewed_pdf = self.work_order_preview_pdfs.get(draft_id)
        reviewed_docx = self.work_order_preview_docxs.get(draft_id)
        if not reviewed_pdf or not reviewed_docx:
            raise ValueError("当前 PDF 或其 Word 原件已失效，请重新渲染并审核整张变更单")
        valid_number = next((item["下一编号"] for item in draft.get("编号选项", [])
                             if item.get("专业") == confirmed["专业"]), "")
        if not valid_number:
            valid_number = next((item["下一编号"] for item in next_change_number_options(
                self._numbering_records(exclude_draft_id=draft_id)) if item["专业"] == confirmed["专业"]), "")
        if confirmed["编号"] != valid_number:
            raise ValueError("变更编号必须使用所选专业现有编号顺延生成的编号")
        if self._work_order_draft_code_reserved(confirmed["编号"], exclude_draft_id=draft_id):
            raise ValueError("该顺延编号已被另一条草稿占用，请处理重复草稿后再写入变更页")
        record = {
            "变更通知单编号": confirmed["编号"],
            "变更编号和说明": f"{confirmed['编号']}（{confirmed['说明']}）",
        }
        record.update({
            "变更编号和说明": f"{confirmed['编号']}（{confirmed['说明']}）",
            "日期五": confirmed["日期"], "变更通知单编号": confirmed["编号"],
            "何种专业四": confirmed["专业"], "变更内容三": confirmed["变更内容"],
            "提出单位一": "工作联系单归并", "变更原因二": "工作联系单需求归并",
        })
        folder_name = work_order_directory_name(record)
        record["_folder_name"] = folder_name
        drawing_paths, _, _ = self._source_files_for_draft(draft)
        change_folder, package_files = build_work_order_change_package(
            record, self.config, reviewed_pdf, reviewed_docx, drawing_paths, self.output_root(),
        )
        pdf_path = change_folder / f"{folder_name}.pdf"
        copied_dwgs = [str(change_folder / item) for item in package_files
                       if Path(item).suffix.lower() == ".dwg"]

        draft["人工确认"] = confirmed
        draft["建议编号"] = confirmed["编号"]
        draft["建议专业"] = confirmed["专业"]
        draft["status"] = "已纳入页面"
        draft["确认状态"] = "待确认"
        draft["目录名"] = folder_name
        draft["输出PDF"] = str(pdf_path)
        draft["输出目录文件"] = package_files
        draft["复制DWG"] = copied_dwgs
        draft["confirmed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        for work_order_id in draft.get("source_record_ids", []):
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            linked_ids = list(dict.fromkeys([*relation.get("change_ids", []), f"work-order:{draft_id}"]))
            relation.update({"review_status": "已形成新变更", "draft_id": draft_id,
                             "change_ids": linked_ids})
        try:
            save_store(self.work_order_store_path, self.work_order_store)
        except Exception:
            import shutil
            shutil.rmtree(change_folder, ignore_errors=True)
            raise
        self.work_order_preview_pdfs.pop(draft_id, None)
        self.work_order_preview_docxs.pop(draft_id, None)
        self.reload()
        return {"draft": draft, "records_count": len(self.records), "pdf_path": str(pdf_path),
                "copied_dwgs": draft["复制DWG"]}

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
        fixed_date = str(draft.get("固定真实变更日期", "")).strip()
        if fixed_date and confirmed.get("日期") not in {"", fixed_date}:
            raise ValueError(f"新变更日期固定为最后一条真实变更日期：{fixed_date}")
        if fixed_date:
            confirmed["日期"] = fixed_date
        draft["人工确认"] = confirmed
        draft.pop("PDF预览哈希", None)
        draft.pop("PDF预览时间", None)
        draft["草稿更新时间"] = datetime.now().astimezone().isoformat(timespec="seconds")
        save_store(self.work_order_store_path, self.work_order_store)
        self.work_order_preview_pdfs.pop(draft_id, None)
        self.work_order_preview_docxs.pop(draft_id, None)
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
        self.work_order_preview_pdfs.pop(draft_id, None)
        self.work_order_preview_docxs.pop(draft_id, None)
        self._reconcile_draft_numbers()
        return draft

    def rollback_published_work_order_change(self, draft_id: str) -> dict:
        """Return a pending published change to its work-order draft."""
        import shutil

        draft = next((item for item in self.work_order_store.get("drafts", []) if item.get("draft_id") == draft_id), None)
        if draft is None:
            raise KeyError("找不到工作联系单变更")
        if draft.get("status") != "已纳入页面" or draft.get("确认状态") != "待确认":
            raise ValueError("只有待确认的工作联系单变更可以回退")
        folder = Path(str(draft.get("输出PDF", ""))).resolve().parent if draft.get("输出PDF") else None
        if folder and folder.exists():
            root = self.output_root().resolve()
            if folder.parent != root:
                raise ValueError("变更目录不在 B24 输出目录下，拒绝回退")
            shutil.rmtree(folder)
        draft["status"] = "草稿"
        draft.pop("确认状态", None)
        for key in ("目录名", "输出目录", "输出PDF", "输出目录文件", "复制DWG", "confirmed_at"):
            draft.pop(key, None)
        for work_order_id in draft.get("source_record_ids", []):
            relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
            relation["change_ids"] = [value for value in relation.get("change_ids", []) if value != f"work-order:{draft_id}"]
            relation.pop("draft_id", None)
            relation["review_status"] = "待人工审核"
        save_store(self.work_order_store_path, self.work_order_store)
        self._reconcile_draft_numbers()
        self.reload()
        return draft

    def delete_work_order_drafts_for_records(self, record_ids: list[str]) -> dict:
        selected_ids = set(record_ids)
        if not selected_ids:
            raise ValueError("请选择至少一条工作联系单")
        known_ids = {item["record_id"] for item in self.work_orders}
        unknown = selected_ids - known_ids
        if unknown:
            raise KeyError(f"找不到工作联系单条目：{', '.join(sorted(unknown))}")

        drafts = self.work_order_store.get("drafts", [])
        matched = [draft for draft in drafts if draft.get("status") == "草稿"
                   and selected_ids.intersection(draft.get("source_record_ids", []))]
        if not matched:
            raise ValueError("所选工作联系单没有可删除的草稿")

        store_snapshot = copy.deepcopy(self.work_order_store)
        matched_ids = {draft.get("draft_id") for draft in matched}
        self.work_order_store["drafts"] = [draft for draft in drafts if draft.get("draft_id") not in matched_ids]
        released_records = []
        for draft in matched:
            draft_id = str(draft.get("draft_id", ""))
            for work_order_id in draft.get("source_record_ids", []):
                relation = self.work_order_store.setdefault("relations", {}).setdefault(work_order_id, {})
                if relation.get("draft_id") == draft_id:
                    relation.pop("draft_id", None)
                    relation["review_status"] = "待人工审核"
                    released_records.append(work_order_id)
        try:
            save_store(self.work_order_store_path, self.work_order_store)
        except Exception:
            self.work_order_store = store_snapshot
            raise
        for draft_id in matched_ids:
            self.work_order_preview_pdfs.pop(str(draft_id), None)
            self.work_order_preview_docxs.pop(str(draft_id), None)
        self._reconcile_draft_numbers()
        return {"deleted_count": len(matched), "deleted_draft_ids": sorted(matched_ids),
                "released_record_ids": sorted(set(released_records))}

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
                    elif self.path == "/api/work-order-conversion-decision":
                        decision = state.set_work_order_conversion_decision(
                            str(body.get("record_id", "")), str(body.get("decision", "")))
                        self._json({"decision": decision})
                    elif self.path == "/api/work-order-issued-decision":
                        decision = state.set_work_order_issued_decision(
                            str(body.get("record_id", "")), str(body.get("decision", "")))
                        self._json({"decision": decision})
                    elif self.path == "/api/work-order-ai-review":
                        relation = state.mark_work_order_ai_result_wrong(str(body.get("record_id", "")))
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
                    elif self.path == "/api/work-order-draft-preview":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("新变更预览字段格式无效")
                        self._json(state.render_work_order_draft_preview(str(body.get("draft_id", "")), fields))
                    elif self.path == "/api/work-order-draft-gray-package":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("变更目录字段格式无效")
                        self._json(state.create_work_order_gray_package(
                            str(body.get("draft_id", "")), fields, str(body.get("planned_path", ""))))
                    elif self.path == "/api/work-order-draft-gray-plan":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("变更目录清单字段格式无效")
                        self._json(state.plan_work_order_gray_package(
                            str(body.get("draft_id", "")), fields))
                    elif self.path == "/api/work-order-proofread":
                        self._json(state.proofread_work_order_content(
                            str(body.get("draft_id", "")), str(body.get("content", ""))))
                    elif self.path == "/api/work-order-draft-cancel":
                        self._json({"draft": state.cancel_work_order_draft(str(body.get("draft_id", "")))})
                    elif self.path == "/api/work-order-rollback":
                        self._json({"draft": state.rollback_published_work_order_change(str(body.get("draft_id", "")))})
                    elif self.path == "/api/work-order-draft-delete":
                        record_ids = body.get("record_ids")
                        if not isinstance(record_ids, list) or not all(isinstance(item, str) for item in record_ids):
                            raise ValueError("工作联系单编号格式无效")
                        self._json(state.delete_work_order_drafts_for_records(record_ids))
                    elif self.path == "/api/work-order-publish":
                        fields = body.get("fields")
                        if not isinstance(fields, dict):
                            raise ValueError("新变更字段格式无效")
                        self._json(state.publish_work_order_draft(
                            str(body.get("draft_id", "")), fields, body.get("reviewed_all_fields") is True,
                            str(body.get("reviewed_pdf_hash", "")), body.get("reviewed_package") is True))
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
                        records = []
                        for item in ids:
                            record = state.get_record(str(item))
                            records.append((record, state.folder(record)))
                        destination = desktop_directory() / "1111"
                        files = zip_selected_records(records, destination)
                        self._json({"files": [str(path) for path in files], "count": len(files),
                                    "destination": str(destination)})
                    else:
                        self._json({"error": "接口不存在"}, 404)
            except Exception as exc:
                LOGGER.exception("写入接口失败")
                self._bad(exc)

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler)
