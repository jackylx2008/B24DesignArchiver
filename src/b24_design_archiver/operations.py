"""单条变更的审批表、资料复制、压缩与 PDF 合并。"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tempfile
import time
import zipfile
import uuid
from datetime import date
from pathlib import Path

from docx.enum.table import WD_ROW_HEIGHT_RULE
from docx import Document
from docx.shared import Cm, Pt

from .bluebeam_cli import combine_and_print_a4


def safe_name(value: str, limit: int = 96) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    return value[:limit].rstrip(" .") or "未命名"


def filesystem_path(path: Path) -> Path:
    """让 Windows 文件 API 能处理超过传统 MAX_PATH 的成果文件。"""
    if os.name == "nt" and len(str(path)) >= 248 and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path.resolve()))
    return path


def record_directory(record: dict, output_root: Path) -> Path:
    folder_name = str(record.get("_folder_name", "")).strip()
    if folder_name:
        return output_root / safe_name(folder_name, 160)
    raw_id = record["变更通知单编号"]
    change_id = safe_name(raw_id, 48)
    description = record.get("变更编号和说明", "")
    if description.startswith(raw_id):
        description = description[len(raw_id):].strip("（）() _-")
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
    target = folder / f"{safe_name(record['变更通知单编号'])}_设计变更审批表.docx"
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


def generate_work_order_notice(
    record: dict, config: dict, folder: Path, has_appendix: bool, content_font_pt: float | None = None,
) -> Path:
    """Fill the blank B24 design-change notice form from confirmed draft fields."""
    template = Path(config.get("template_notice") or (Path(__file__).resolve().parents[2] / "B24_设计变更通知单_模板.docx"))
    if not template.is_file():
        raise FileNotFoundError(f"找不到设计变更通知单模板：{template}")
    target = folder / f"{safe_name(record['变更通知单编号'])}_设计变更通知单.docx"
    document = Document(template)
    if not document.tables:
        raise ValueError("设计变更通知单模板没有表格")
    table = document.tables[0]

    def set_cell(cell, lines: list[str]) -> None:
        paragraph = cell.paragraphs[0]
        for run in paragraph.runs:
            run.text = ""
        run = paragraph.add_run(lines[0])
        if content_font_pt is not None:
            run.font.size = Pt(content_font_pt)
        for line in lines[1:]:
            run.add_break()
            run.add_text(line)
        if content_font_pt is not None:
            run.font.size = Pt(content_font_pt)
        for extra in cell.paragraphs[1:]:
            extra._element.getparent().remove(extra._element)

    raw_date = str(record.get("日期五", "")).strip()[:10]
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ValueError("变更日期无效，无法填写设计变更通知单") from exc
    notice_date = f"{parsed_date.year}年{parsed_date.month:02d}月{parsed_date.day:02d}日"
    content = str(record.get("变更内容三", "")).strip()
    if not content:
        raise ValueError("变更内容为空，无法填写设计变更通知单")
    ending = ["详见附图", "以下空白"] if has_appendix else ["以下空白"]
    set_cell(table.cell(0, 7), [str(record.get("变更通知单编号", "")).strip()])
    set_cell(table.cell(1, 7), [str(record.get("何种专业四", record.get("专业", ""))).strip()])
    set_cell(table.cell(2, 7), [notice_date])
    set_cell(table.cell(4, 2), [content, *ending])
    set_cell(table.cell(7, 2), [notice_date])
    # Expand the variable content row to use the printable A4 page area.
    # Retain a small reserve for the form note below the table and the footer.
    section = document.sections[0]
    body_row_index = 4
    other_rows_height = sum(
        row.height or 0 for index, row in enumerate(table.rows) if index != body_row_index
    )
    page_body_height = section.page_height - section.top_margin - section.bottom_margin
    reserved_after_table = Cm(1.8)
    target_height = page_body_height - reserved_after_table - other_rows_height
    body_row = table.rows[body_row_index]
    body_row.height = max(body_row.height or 0, target_height)
    body_row.height_rule = WD_ROW_HEIGHT_RULE.AT_LEAST
    folder.mkdir(parents=True, exist_ok=True)
    document.save(target)
    return target


def render_work_order_notice_artifacts(record: dict, config: dict, has_appendix: bool) -> tuple[bytes, bytes]:
    with tempfile.TemporaryDirectory(prefix="b24-notice-preview-") as temporary_dir:
        folder = Path(temporary_dir)
        from pypdf import PdfReader
        if os.name != "nt":
            raise RuntimeError("Word 转 PDF 目前需要 Windows 和桌面版 Microsoft Word")
        import pythoncom
        import win32com.client

        # Reduce only the variable change-content text until Word confirms that
        # the form fits on one portrait A4 sheet. Keep a readable lower bound.
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            for content_font_pt in (None, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0):
                attempt = folder / ("template-size" if content_font_pt is None else f"font-{content_font_pt:g}")
                notice_docx = generate_work_order_notice(
                    record, config, attempt, has_appendix, content_font_pt=content_font_pt,
                )
                preview_pdf = attempt / f"{notice_docx.stem}.pdf"
                _export_word_to_pdf(word, notice_docx, preview_pdf)
                reader = PdfReader(str(preview_pdf))
                if len(reader.pages) != 1:
                    continue
                page = reader.pages[0]
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                a4_width, a4_height = 595.276, 841.89
                if abs(width - a4_width) <= 4 and abs(height - a4_height) <= 4:
                    return preview_pdf.read_bytes(), notice_docx.read_bytes()
        finally:
            if word is not None:
                word.Quit()
            pythoncom.CoUninitialize()
        raise ValueError("变更内容过长，通知单无法在 7 磅以上字号下排入一张 A4 纸；请精简内容后重试")


def render_approval_preview_artifacts(record: dict, config: dict) -> tuple[bytes, bytes]:
    """Render the approval PDF and retain the exact Word file used to make it."""
    with tempfile.TemporaryDirectory(prefix="b24-approval-preview-") as temporary_dir:
        folder = Path(temporary_dir)
        approval_docx = generate_approval(record, config, folder)
        preview_pdf = folder / f"{approval_docx.stem}.pdf"
        _word_to_pdf(approval_docx, preview_pdf)
        return preview_pdf.read_bytes(), approval_docx.read_bytes()


def render_approval_preview_pdf(record: dict, config: dict) -> bytes:
    return render_approval_preview_artifacts(record, config)[0]


def remove_blank_pdf_pages(pdf_bytes: bytes) -> tuple[bytes, list[int]]:
    """Drop textless, imageless pages from the generated approval form only."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    if len(reader.pages) <= 1:
        return pdf_bytes, []
    writer = PdfWriter()
    removed: list[int] = []
    kept = 0
    for index, page in enumerate(reader.pages):
        try:
            text = (page.extract_text() or "").strip()
            has_text = bool(text)
            has_images = any(True for _ in page.images)
            compact_text = re.sub(r"\s+", "", text)
            footer_only = (
                not has_images and len(compact_text) <= 60
                and "工程变更洽商管理办法" in compact_text
            )
        except Exception:
            # If content inspection fails, preserve the page for human review.
            has_text, has_images, footer_only = True, True, False
        if index > 0 and not has_images and (not has_text or footer_only):
            removed.append(index + 1)
            continue
        writer.add_page(page)
        kept += 1
    if not kept:
        return pdf_bytes, []
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), removed


def append_work_order_appendices(approval_pdf: bytes, source_pdfs: list[tuple[Path, str]]) -> tuple[bytes, int]:
    """Append source attachment pages after the reviewed change approval form.

    Standalone attachment PDFs contribute every page. An issued work-order PDF
    includes its one-page form first, so only its following pages are drawings.
    """
    from pypdf import PdfReader, PdfWriter

    approval_reader = PdfReader(io.BytesIO(approval_pdf))
    approval_page_count = len(approval_reader.pages)
    writer = PdfWriter()
    for page in approval_reader.pages:
        writer.add_page(page)
    appended = 0
    for source, role in source_pdfs:
        reader = PdfReader(str(filesystem_path(source)))
        start_page = 1 if role == "issued_pdf" else 0
        for page in reader.pages[start_page:]:
            writer.add_page(page)
            appended += 1
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), approval_page_count


def pdf_page_count(pdf_bytes: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def work_order_directory_name(record: dict) -> str:
    code = str(record.get("变更通知单编号", "")).strip()
    description = str(record.get("变更编号和说明", "")).strip()
    if description.startswith(code):
        description = description[len(code):].strip("（）() _-")
    return safe_name(f"{safe_name(code, 48)}（{description}）" if description else code, 160)


def _copy_source_files(items: list[tuple[Path, str]], destination: Path) -> list[str]:
    copied: list[str] = []
    newly_copied: list[Path] = []
    origin_by_name: dict[str, tuple[Path, str]] = {}
    try:
        for source, origin in items:
            target = destination / source.name
            existing_origin = origin_by_name.get(source.name.casefold())
            if not filesystem_path(target).exists():
                _copy_file(source, target)
                newly_copied.append(target)
                origin_by_name[source.name.casefold()] = (source, origin)
                copied.append(str(target))
                continue
            if existing_origin and _same_file(existing_origin[0], source):
                continue
            if not existing_origin and _same_file(source, target):
                origin_by_name[source.name.casefold()] = (source, origin)
                continue
            # Keep colliding originals' filenames intact by placing them in a
            # subfolder named after their source contact.
            origin_folder = safe_name(origin or source.parent.name, 72)
            conflict_folder = destination / origin_folder
            conflict_folder.mkdir(parents=True, exist_ok=True)
            conflict_target = conflict_folder / source.name
            if filesystem_path(conflict_target).exists():
                if _same_file(source, conflict_target):
                    continue
                raise FileExistsError(f"同一联系单中存在同名但内容不同的附件：{source.name}")
            _copy_file(source, conflict_target)
            newly_copied.append(conflict_target)
            copied.append(str(conflict_target))
    except Exception:
        for target in newly_copied:
            target.unlink(missing_ok=True)
        raise
    return copied


def build_work_order_change_package(
    record: dict,
    config: dict,
    reviewed_pdf: bytes,
    reviewed_docx: bytes,
    source_drawings: list[tuple[Path, str]],
    output_parent: Path,
) -> tuple[Path, list[str]]:
    """Build a complete folder without changing any work-order source files."""
    for source, _ in source_drawings:
        try:
            with filesystem_path(source).open("rb") as stream:
                stream.read(1)
        except PermissionError as exc:
            code = getattr(exc, "winerror", None)
            detail = "文件被占用" if code == 32 else "访问被拒绝"
            code_label = f"Windows 错误 {code}" if code is not None else "系统拒绝读取"
            raise PermissionError(
                f"无法读取联系单原始 DWG「{source.name}」（{detail}，{code_label}）。"
                "请检查源文件的读取权限或同步状态后重试；程序没有修改原始文件。"
            ) from exc
    folder_name = str(record.get("_folder_name") or work_order_directory_name(record))
    target = output_parent / safe_name(folder_name, 160)
    if target.exists():
        raise FileExistsError(f"目标变更目录已存在，为避免覆盖已停止生成：{target}")
    output_parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".b24-package-", dir=output_parent) as temporary:
            staged = Path(temporary) / folder_name
            staged.mkdir()
            cad_folder = staged / "word&CAD"
            cad_folder.mkdir()
            named_approval_docx = cad_folder / f"{folder_name}.docx"
            named_approval_docx.write_bytes(reviewed_docx)
            _copy_source_files(source_drawings, cad_folder)
            pdf_path = staged / f"{folder_name}.pdf"
            with tempfile.NamedTemporaryFile(dir=staged, suffix=".pdf", delete=False) as handle:
                temporary_pdf = Path(handle.name)
            try:
                temporary_pdf.write_bytes(reviewed_pdf)
                temporary_pdf.replace(pdf_path)
            finally:
                temporary_pdf.unlink(missing_ok=True)
            files = sorted(path.relative_to(staged).as_posix() for path in staged.rglob("*") if path.is_file())
            staged.replace(target)
    except Exception:
        for empty_directory in (output_parent, output_parent.parent):
            try:
                empty_directory.rmdir()
            except OSError:
                pass
        raise
    return target, files


def _same_file(first: Path, second: Path) -> bool:
    if filesystem_path(first).stat().st_size != filesystem_path(second).stat().st_size:
        return False

    def digest(path: Path) -> bytes:
        hasher = hashlib.sha256()
        with filesystem_path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.digest()
    return digest(first) == digest(second)


def _unique_target(target: Path, source: Path) -> Path | None:
    if not filesystem_path(target).exists():
        return target
    if _same_file(source, target):
        return None
    for number in range(2, 1000):
        option = target.with_name(f"{target.stem}_{number}{target.suffix}")
        if not filesystem_path(option).exists():
            return option
        if _same_file(source, option):
            return None
    raise ValueError(f"同名文件过多：{target.name}")


def _copy_file(source: Path, target: Path) -> None:
    """同目录暂存后发布，避免同步软件扫描时留下半写入文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=target.suffix, delete=False) as handle:
        staged = Path(handle.name)
    try:
        shutil.copy2(source, staged)
        for attempt in range(5):
            try:
                filesystem_path(staged).replace(filesystem_path(target))
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        staged.unlink(missing_ok=True)


def copy_materials(preview: dict, folder: Path) -> list[str]:
    copied = []
    directories = [Path(name) for name in preview["directories"]]
    for source in directories:
        for path in source.rglob("*"):
            if path.is_dir():
                (folder / path.relative_to(source)).mkdir(parents=True, exist_ok=True)
            elif path.is_file():
                destination = _unique_target(folder / path.relative_to(source), path)
                if destination is not None:
                    _copy_file(path, destination)
                    copied.append(str(destination))
    sources = sorted(set(preview["pdfs"] + preview["docxs"]))
    for name in sources:
        source = Path(name)
        if any(directory == source or directory in source.parents for directory in directories):
            continue
        target = _unique_target(folder / safe_name(source.name, 180), source)
        if target is not None:
            _copy_file(source, target)
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


def desktop_directory() -> Path:
    """Return the user's actual Desktop path, including redirected Windows desktops."""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                desktop, _ = winreg.QueryValueEx(key, "Desktop")
            expanded = Path(os.path.expandvars(desktop)).expanduser()
            if expanded.is_absolute():
                return expanded
        except (OSError, ImportError):
            pass
        try:
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID.from_buffer_copy(uuid.UUID("B4BFCC3A-DB2C-424C-B029-7FE99A87C641").bytes_le)
            path = wintypes.LPWSTR()
            get_folder = ctypes.windll.shell32.SHGetKnownFolderPath
            get_folder.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                   ctypes.POINTER(wintypes.LPWSTR)]
            get_folder.restype = wintypes.HRESULT
            result = get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(path))
            if result == 0 and path:
                try:
                    return Path(path.value)
                finally:
                    free_memory = ctypes.windll.ole32.CoTaskMemFree
                    free_memory.argtypes = [ctypes.c_void_p]
                    free_memory(path)
        except Exception:
            pass
    return Path.home() / "Desktop"


def zip_selected_records(records: list[tuple[dict, Path]], destination: Path) -> list[Path]:
    """Write one ZIP per selected change into destination, named by code and description."""
    if not records:
        raise ValueError("请先选择至少一条变更")
    missing = [str(folder) for _, folder in records if not folder.is_dir()]
    if missing:
        raise ValueError("以下变更没有成果目录，未能打包：" + "；".join(missing))
    destination.mkdir(parents=True, exist_ok=True)
    outputs = []
    used_names = set()
    for record, folder in records:
        code = str(record.get("变更通知单编号", "")).strip()
        label = str(record.get("变更编号和说明", "")).strip() or code
        name = safe_name(label, 180)
        if name in used_names:
            name = safe_name(f"{name}_{code}", 180)
        used_names.add(name)
        output = destination / f"{name}.zip"
        zip_directory(folder, output)
        outputs.append(output)
    return outputs


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
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        _export_word_to_pdf(word, source, destination)
    finally:
        if word is not None:
            word.Quit()
        pythoncom.CoUninitialize()


def _export_word_to_pdf(word, source: Path, destination: Path) -> None:
    document = None
    try:
        document = word.Documents.Open(str(source.resolve()), ReadOnly=True)
        document.ExportAsFixedFormat(str(destination.resolve()), 17)
    finally:
        if document is not None:
            document.Close(False)


def merge_pdfs(approval_docx: Path, preview: dict, folder: Path) -> list[str]:
    pdfs = [Path(name) for name in preview["pdfs"]]
    if not pdfs:
        raise ValueError("未找到对应的原始 PDF，无法合并")
    output = folder / f"{safe_name(approval_docx.stem)}_合并.pdf"
    a4 = folder / f"{safe_name(approval_docx.stem)}_合并_A4.pdf"
    if output.exists() or a4.exists():
        raise FileExistsError("合并 PDF 已存在；为避免覆盖，请先在输出目录中处理旧成果")
    with tempfile.TemporaryDirectory(prefix="b24-word-") as temporary_dir:
        temporary = Path(temporary_dir)
        approval_pdf = temporary / "approval.pdf"
        _word_to_pdf(approval_docx, approval_pdf)
        merged = temporary / "merged.pdf"
        normalized = temporary / "a4.pdf"
        combine_and_print_a4([approval_pdf, *pdfs], merged, normalized)
        staged_outputs: list[tuple[Path, Path]] = []
        published: list[Path] = []
        try:
            for source, destination in ((merged, output), (normalized, a4)):
                with tempfile.NamedTemporaryFile(dir=folder, suffix=".pdf", delete=False) as handle:
                    staged = Path(handle.name)
                staged_outputs.append((staged, destination))
                shutil.copy2(source, staged)
            for staged, destination in staged_outputs:
                staged.rename(destination)
                published.append(destination)
        except OSError:
            for destination in published:
                destination.unlink(missing_ok=True)
            raise
        finally:
            for staged, _ in staged_outputs:
                staged.unlink(missing_ok=True)
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
