"""使用临时台账和资料验证逐条归档流程。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document
from openpyxl import Workbook
from pypdf import PdfReader
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from b24_design_archiver.bluebeam_cli import combine_and_print_a4, find_script_engine
from b24_design_archiver.server import ArchiveState, create_server


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        workbook = self.root / "台账.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.append([
            "审批表编号", "提出单位一", "变更原因二", "变更内容三", "变更通知单编号",
            "变更编号和说明", "日期五", "六版本", "是否完成面单", "何种专业四",
        ])
        sheet.append(["BG001", "设计部", "图纸完善", "调整水泵参数", "05-03-C2-001", "05-03-C2-001（水泵）", "2025-09-30", "施工图", "是", "给排水"])
        sheet.append(["BG002", "设计部", "图纸完善", "修改管线", "05-03-C2-002", "05-03-C2-002（管线）", "2025-10-02", "施工图", "否", "给排水"])
        sheet.append([None] * 10)
        book.save(workbook)
        before = self.root / "前.docx"
        after = self.root / "后.docx"
        for path in (before, after):
            doc = Document()
            doc.add_paragraph("变更通知单编号 日期五")
            table = doc.add_table(rows=1, cols=1)
            table.cell(0, 0).text = "变更内容三"
            doc.save(path)
        source = self.root / "source"
        target = source / "05-03-C2-001（水泵）"
        target.mkdir(parents=True)
        (target / "05-03-C2-001说明.docx").write_bytes(b"sample docx")
        pdf = target / "05-03-C2-001图纸.pdf"
        draw = canvas.Canvas(str(pdf), pagesize=(900, 500))
        draw.drawString(30, 30, "drawing")
        draw.save()
        (source / "05-03-C2-0010误匹配.pdf").write_bytes(b"ignored")
        self.config = {
            "workbook": workbook, "template_before": before, "template_after": after,
            "template_change_date": "2025-10-01", "output_root": self.root / "output",
            "source_directories": [source],
        }
        with patch("b24_design_archiver.server.load_config", return_value=self.config), patch("b24_design_archiver.server.SETTINGS_FILE", self.root / "settings.json"):
            self.state = ArchiveState()

    def test_record_preview_and_actions(self) -> None:
        self.assertEqual(len(self.state.records), 2)
        preview = self.state.preview("05-03-C2-001")
        self.assertEqual(len(preview["directories"]), 1)
        self.assertEqual(len(preview["pdfs"]), 1)
        self.assertIn("水泵", preview["output_folder"])
        result = self.state.action("05-03-C2-001", "generate")
        generated = Path(result["files"][0])
        doc = Document(generated)
        self.assertIn("调整水泵参数", doc.tables[0].cell(0, 0).text)
        self.assertIn("2025.09.30", doc.paragraphs[0].text)
        copy_result = self.state.action("05-03-C2-001", "copy")
        self.assertEqual(len(copy_result["files"]), 3)
        self.assertTrue(Path(copy_result["files"][0]).is_dir())
        self.assertEqual(self.state.action("05-03-C2-001", "copy")["files"], [])
        archive_result = self.state.action("05-03-C2-001", "zip")
        with zipfile.ZipFile(archive_result["files"][0]) as archive:
            self.assertEqual(len(archive.namelist()), 2)
        self.assertEqual(self.state.action("05-03-C2-001", "zip")["files"], archive_result["files"])
        second = self.state.action("05-03-C2-002", "generate")
        self.assertNotEqual(Path(second["files"][0]).parent, generated.parent)

    def test_http(self) -> None:
        server = create_server(self.state)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/api/records") as response:
                data = json.load(response)
            self.assertEqual(len(data["records"]), 2)
            request = urllib.request.Request(base + "/api/action", data=json.dumps({"id": "05-03-C2-001", "action": "generate"}).encode(), headers={"Content-Type": "application/json", "X-Session-Token": self.state.token})
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(len(result["files"]), 1)
        finally:
            server.shutdown()
            server.server_close()

    @unittest.skipUnless(os.name == "nt", "Bluebeam CLI 仅支持 Windows")
    def test_bluebeam_cli_merge_and_a4(self) -> None:
        try:
            find_script_engine()
        except FileNotFoundError as exc:
            self.skipTest(str(exc))
        source = Path(self.state.preview("05-03-C2-001")["pdfs"][0])
        approval = self.root / "approval.pdf"
        draw = canvas.Canvas(str(approval), pagesize=(595, 842))
        draw.drawString(30, 30, "approval")
        draw.save()
        merged = self.root / "merged.pdf"
        a4 = self.root / "a4.pdf"
        combine_and_print_a4([approval, source], merged, a4)
        merged_pages = PdfReader(merged).pages
        self.assertEqual(len(merged_pages), 2)
        self.assertIn("approval", merged_pages[0].extract_text())
        pages = PdfReader(a4).pages
        self.assertEqual(len(pages), 2)
        for page in pages:
            self.assertAlmostEqual(float(page.mediabox.width), 595.3, delta=2)
            self.assertAlmostEqual(float(page.mediabox.height), 841.9, delta=2)

    def test_multiple_pdfs_require_selection_before_writing(self) -> None:
        folder = self.root / "source" / "05-03-C2-001（水泵）"
        (folder / "05-03-C2-001附件.pdf").write_bytes(b"second candidate")
        self.state.sources.invalidate()
        with self.assertRaisesRegex(ValueError, "选择要合并的文件"):
            self.state.action("05-03-C2-001", "merge")
        self.assertFalse(self.state.folder(self.state.get_record("05-03-C2-001")).exists())
        with self.assertRaisesRegex(ValueError, "请选择当前变更"):
            self.state.action("05-03-C2-001", "merge", ["outside.pdf"])


if __name__ == "__main__":
    unittest.main()
