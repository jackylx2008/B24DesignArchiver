"""单条变更的审批表、资料复制、压缩与 PDF 合并。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path

from docx import Document


def safe_name(value: str, limit: int = 96) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:limit].rstrip(" .") or "未命名"


def record_directory(record: dict, output_root: Path) -> Path:
    change_id = safe_name(record["_id"], 48)
    description = record.get("变更编号和说明", "")
    if description.startswith(record["_id"]):
        description = description[len(record["_id"]):].strip("（）() _-")
    return output_root / safe_name(f"{change_id}_{description}" if description else change_id)


def _template(config: dict, record: dict) -> Path:
    raw_date = record["日期五"]
    if not raw_date:
        raise ValueError("台账日期为空，无法选择审批表模板")
    chosen = "template_after" if date.fromisoformat(raw_date[:10]) >= date.fromisoformat(config["template_change_date"]) else "template_before"
    template = config[chosen]
    if not template.is_file():
        raise FileNotFoundError(f"找不到模板：{template}")
    return template


def _replace_runs(paragraph, values: dict[str, str]) -> None:
    for run in paragraph.runs:
        content = run.text
        for key, replacement in values.items():
            content = content.replace(key, replacement)
        run.text = content


def generate_approval(record: dict, config: dict, folder: Path) -> Path:
    template = _template(config, record)
    target = folder / f"{safe_name(record['_id'])}_设计变更审批表.docx"
    if target.exists():
        return target
    values = {key: value for key, value in record.items() if not key.startswith("_")}
    values["日期五"] = values["日期五"].replace("-", ".")
    values["#####"] = record["变更通知单编号"]
    document = Document(template)
    for paragraph in document.paragraphs:
        _replace_runs(paragraph, values)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    _replace_runs(paragraph, values)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=folder, suffix=".docx", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        document.save(temporary)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _same_file(first: Path, second: Path) -> bool:
    if first.stat().st_size != second.stat().st_size:
        return False

    def digest(path: Path) -> bytes:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.digest()
    return digest(first) == digest(second)


def _unique_target(target: Path, source: Path) -> Path | None:
    if not target.exists():
        return target
    if _same_file(source, target):
        return None
    for number in range(2, 1000):
        option = target.with_name(f"{target.stem}_{number}{target.suffix}")
        if not option.exists():
            return option
        if _same_file(source, option):
            return None
    raise ValueError(f"同名文件过多：{target.name}")


def copy_materials(preview: dict, folder: Path) -> list[str]:
    target_dir = folder / "资料文件"
    copied = []
    for name in preview["directories"]:
        source = Path(name)
        source_hash = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
        target = folder / "原始资料目录" / f"{safe_name(source.name)}_{source_hash}"
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary_dir:
            staged = Path(temporary_dir) / target.name
            shutil.copytree(source, staged)
            staged.replace(target)
        copied.append(str(target))
    sources = sorted(set(preview["pdfs"] + preview["docxs"]))
    for name in sources:
        source = Path(name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_target(target_dir / safe_name(source.name, 180), source)
        if target is not None:
            shutil.copy2(source, target)
            copied.append(str(target))
    return copied


def zip_directory(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(source.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(source))
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("生成的 ZIP 校验失败")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def zip_materials(preview: dict, folder: Path) -> list[str]:
    results = []
    for source_name in preview["directories"]:
        source = Path(source_name)
        source_hash = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
        target = folder / "资料压缩包" / f"{safe_name(source.name)}_{source_hash}.zip"
        if target.exists():
            results.append(str(target))
            continue
        zip_directory(source, target)
        results.append(str(target))
    return results


def _word_to_pdf(source: Path, destination: Path) -> None:
    if os.name != "nt":
        raise RuntimeError("Word 转 PDF 目前需要 Windows 和桌面版 Microsoft Word")
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        document = word.Documents.Open(str(source.resolve()), ReadOnly=True)
        document.ExportAsFixedFormat(str(destination.resolve()), 17)
    finally:
        if document is not None:
            document.Close(False)
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()


def _normalize_a4(source: Path, destination: Path) -> None:
    from pdfrw import PdfReader as RwReader
    from pdfrw.buildxobj import pagexobj
    from pdfrw.toreportlab import makerl
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    width, height = A4
    margin = 10 * 72 / 25.4
    draw = canvas.Canvas(str(destination), pagesize=A4)
    for page in RwReader(str(source)).pages:
        obj = pagexobj(page)
        src_width = float(obj.BBox[2] - obj.BBox[0])
        src_height = float(obj.BBox[3] - obj.BBox[1])
        rotate = src_width > src_height
        content_width = src_height if rotate else src_width
        content_height = src_width if rotate else src_height
        scale = min(1.0, (width - 2 * margin) / content_width, (height - 2 * margin) / content_height)
        x = (width - content_width * scale) / 2
        y = (height - content_height * scale) / 2
        draw.saveState()
        if rotate:
            draw.translate(x + content_width * scale, y)
            draw.rotate(90)
        else:
            draw.translate(x, y)
        draw.scale(scale, scale)
        draw.doForm(makerl(draw, obj))
        draw.restoreState()
        draw.showPage()
    draw.save()


def merge_pdfs(approval_docx: Path, preview: dict, folder: Path) -> list[str]:
    from pypdf import PdfReader, PdfWriter

    pdfs = [Path(name) for name in preview["pdfs"]]
    if not pdfs:
        raise ValueError("未找到对应的原始 PDF，无法合并")
    output = folder / f"{safe_name(approval_docx.stem)}_合并.pdf"
    a4 = folder / f"{safe_name(approval_docx.stem)}_合并_A4.pdf"
    if output.exists() or a4.exists():
        raise FileExistsError("合并 PDF 已存在；为避免覆盖，请先在输出目录中处理旧成果")
    with tempfile.TemporaryDirectory(dir=folder) as temporary_dir:
        temporary = Path(temporary_dir)
        approval_pdf = temporary / "approval.pdf"
        _word_to_pdf(approval_docx, approval_pdf)
        writer = PdfWriter()
        for pdf in [approval_pdf, *pdfs]:
            reader = PdfReader(str(pdf))
            if not reader.pages:
                raise ValueError(f"PDF 没有页面：{pdf}")
            for page in reader.pages:
                writer.add_page(page)
        merged = temporary / "merged.pdf"
        with merged.open("wb") as stream:
            writer.write(stream)
        normalized = temporary / "a4.pdf"
        _normalize_a4(merged, normalized)
        shutil.copy2(merged, output)
        shutil.copy2(normalized, a4)
    return [str(output), str(a4)]


def zip_selected_folders(folders: list[Path], destination: Path) -> Path:
    if destination.exists():
        raise FileExistsError(f"总 ZIP 已存在：{destination}")
    if not any(folder.is_dir() for folder in folders):
        raise ValueError("所选变更尚无成果目录")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in folders:
            if not folder.is_dir():
                continue
            for file in sorted(folder.rglob("*")):
                if file.is_file():
                    archive.write(file, Path(folder.name) / file.relative_to(folder))
    return destination
