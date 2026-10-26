"""调用本机 Revu 20 ScriptEngine 合并 PDF 并打印成竖向 A4。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError


PRINTER_NAME = "Microsoft Print to PDF"
SCRIPT_TIMEOUT_SECONDS = 120
PRINT_TIMEOUT_SECONDS = 45
A4_WIDTH = 595.3
A4_HEIGHT = 841.9


def find_script_engine() -> Path:
    """优先使用本机覆盖路径，再查找标准的 Revu 20 安装位置。"""
    override = os.environ.get("BLUEBEAM_SCRIPT_ENGINE")
    if override:
        path = Path(override).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"BLUEBEAM_SCRIPT_ENGINE 指向的文件不存在：{path}")
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(variable)
        if base:
            path = Path(base) / "Bluebeam Software" / "Bluebeam Revu" / "20" / "Revu" / "ScriptEngine.exe"
            if path.is_file():
                return path
    raise FileNotFoundError("找不到 Bluebeam Revu 20 的 ScriptEngine.exe")


def _require_printer() -> None:
    if os.name != "nt":
        raise RuntimeError("Bluebeam CLI 合并和 A4 输出仅支持 Windows")
    import win32print

    names = {printer[2] for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL)}
    if PRINTER_NAME not in names:
        raise RuntimeError(f"找不到 PDF 打印机：{PRINTER_NAME}")


def _page_count(path: Path) -> int:
    pages = len(PdfReader(str(path)).pages)
    if not pages:
        raise ValueError(f"PDF 没有页面：{path}")
    return pages


def _execute(engine: Path, commands: list[str]) -> None:
    # -i 的响应以 Ready 开始，每条命令应返回 0；进程退出码本身不足以判定脚本成功。
    result = subprocess.run(
        [str(engine), "-i"],
        input="\n".join([*commands, "exit", ""]),
        text=True,
        capture_output=True,
        timeout=SCRIPT_TIMEOUT_SECONDS,
        check=False,
    )
    responses = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or responses[:1] != ["Ready"] or responses[1:] != ["0"] * len(commands):
        detail = " | ".join(responses[1:]) or result.stderr.strip() or f"退出码 {result.returncode}"
        raise RuntimeError(f"Bluebeam 脚本失败：{detail}")


def _wait_for_a4(path: Path, expected_pages: int) -> None:
    deadline = time.monotonic() + PRINT_TIMEOUT_SECONDS
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 1000:
            try:
                reader = PdfReader(str(path))
                if len(reader.pages) != expected_pages:
                    raise ValueError("A4 输出页数与合并文件不一致")
                for index, page in enumerate(reader.pages, start=1):
                    width = float(page.mediabox.width)
                    height = float(page.mediabox.height)
                    if abs(width - A4_WIDTH) > 2 or abs(height - A4_HEIGHT) > 2:
                        raise ValueError(f"第 {index} 页不是竖向 A4：{width:.1f} × {height:.1f} pt")
                return
            except (OSError, ValueError, PdfReadError) as exc:
                last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"等待 Bluebeam A4 输出超时：{last_error or path}")


def combine_and_print_a4(sources: list[Path], merged_output: Path, a4_output: Path) -> None:
    """用 Revu ScriptEngine 的 Combine 和 PrintToFile 生成两份 PDF。"""
    engine = find_script_engine()
    _require_printer()
    if len(sources) < 2:
        raise ValueError("合并至少需要审批表 PDF 与一份原始 PDF")
    expected_pages = sum(_page_count(path) for path in sources)

    # Bluebeam 脚本通过控制台接收路径；临时英文路径可避开中文文件名的编码差异。
    with tempfile.TemporaryDirectory(prefix="b24-bluebeam-") as staging_dir:
        staging = Path(staging_dir)
        inputs = []
        for index, source in enumerate(sources, start=1):
            staged = staging / f"input_{index:03d}.pdf"
            shutil.copy2(source, staged)
            inputs.append(staged)
        merged = staging / "merged.pdf"
        printed = staging / "a4.pdf"
        parameters = ", ".join(f'"{path.as_posix()}"' for path in inputs)
        commands = [
            f"Combine({parameters})",
            f'Save("{merged.as_posix()}")',
            f'Open("{merged.as_posix()}")',
            # 本机 20.2.70 的 PrintToFile 实际把输出文件放在最后一个参数。
            # Bluebeam PDF 打印机会写出 PostScript；Microsoft PDF 打印机直接得到 PDF。
            f'PrintToFile("{PRINTER_NAME}", "A4", false, "-1", 1, 2, 1, false, 1, "{printed.as_posix()}")',
        ]
        _execute(engine, commands)
        if not merged.is_file() or _page_count(merged) != expected_pages:
            raise RuntimeError("Bluebeam 合并输出的页数不正确")
        _wait_for_a4(printed, expected_pages)
        shutil.copy2(merged, merged_output)
        shutil.copy2(printed, a4_output)
