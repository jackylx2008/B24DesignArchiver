"""仅监听本机的变更归档页面与操作接口。"""

from __future__ import annotations

import json
import logging
import os
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
from .records import load_records, write_records_json
from .sources import SourceIndex

LOGGER = logging.getLogger(__name__)
WEB_FILE = PROJECT_ROOT / "src" / "b24_design_archiver" / "web" / "index.html"
SETTINGS_FILE = PROJECT_ROOT / "local_settings.json"


class ArchiveState:
    def __init__(self):
        self.config = load_config()
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.records: list[dict] = []
        self.by_id: dict[str, dict] = {}
        self.sync_status = {"running": False, "completed": 0, "total": 0, "copied": 0, "errors": []}
        if SETTINGS_FILE.is_file():
            try:
                saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if saved.get("output_root"):
                    self.config["output_root"] = Path(saved["output_root"]).expanduser().resolve()
            except (OSError, ValueError) as exc:
                LOGGER.warning("读取本机输出设置失败：%s", exc)
        self.reload()

    def reload(self) -> None:
        records = load_records(self.config["workbook"])
        self.records_json = write_records_json(records, self.output_root())
        self.records = records
        self.by_id = {record["_id"]: record for record in self.records}
        self.sources = SourceIndex(self.config["source_directories"], [record["变更通知单编号"] for record in self.records])

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
        records_json = write_records_json(self.records, path)
        SETTINGS_FILE.write_text(json.dumps({"output_root": str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config["output_root"] = path
        self.records_json = records_json
        return str(path)

    def get_record(self, record_id: str) -> dict:
        if record_id not in self.by_id:
            raise KeyError(f"找不到条目：{record_id}")
        return self.by_id[record_id]

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
                        self.records_json = write_records_json(records, output_root)
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
                        records = [{**record, "_output_exists": state.folder(record).is_dir()} for record in state.records]
                        self._json({"records": records, "workbook": str(state.config["workbook"]), "output_root": str(state.output_root()), "records_json": str(state.records_json)})
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
