"""B24 设计变更归档启动器。

用途：
  读取 B24 台账与路径配置，启动仅供本机访问的 HTML 页面，并自动在浏览器中打开。
  控制窗口可查看读取状态、重新打开页面；关闭窗口后本地服务停止。

配置文件：
  config.yaml 指定台账、模板、资料源目录与默认输出目录；如已有本机
  local_settings.json，则沿用其中保存的输出目录。

可选参数：
  --no-ui  不显示控制窗口，直接打开浏览器；适合终端启动。

示例：
  python design-archiver.py

输出：
  每条变更的成果放入 output/B24 下的独立目录；日志保存在 logs/。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import subprocess
import threading
import webbrowser
from pathlib import Path

from logging_config import configure_utf8_stdio, setup_logger

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from b24_design_archiver.server import ArchiveState, create_server  # noqa: E402


def _chrome_path() -> Path | None:
    """Return a local Chrome executable on Windows, if installed."""
    if sys.platform != "win32":
        return None
    candidates = [shutil.which("chrome.exe"), shutil.which("chrome")]
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(str(Path(base) / "Google/Chrome/Application/chrome.exe"))
    return next((Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


def _chrome_windows() -> set[int]:
    """Find visible Chrome windows currently showing this app's page title."""
    if sys.platform != "win32":
        return set()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    found: set[int] = set()

    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if "B24 设计变更归档" in title.value:
            found.add(int(hwnd))
        return True

    user32.EnumWindows(enum_callback(collect), 0)
    return found


def _close_chrome_window(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


def _move_chrome_window_to_primary(hwnd: int) -> None:
    """Move a launched Chrome window to the Windows primary monitor and maximize it."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [(name, wintypes.LONG) for name in ("left", "top", "right", "bottom")]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(RECT), callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    primary: list[RECT] = []

    def collect(monitor: int, _hdc: int, _rect: object, _lparam: int) -> bool:
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)) and info.dwFlags & 1:  # MONITORINFOF_PRIMARY
            primary.append(info.rcWork)
            return False
        return True

    user32.EnumDisplayMonitors(None, None, callback_type(collect), 0)
    if not primary:
        return
    work = primary[0]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetWindowPos(hwnd, None, work.left, work.top, work.right - work.left, work.bottom - work.top, 0x0014)
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-ui", action="store_true", help="直接打开浏览器，不显示控制窗口")
    args = parser.parse_args()
    configure_utf8_stdio()
    logger = setup_logger()
    state = ArchiveState()
    server = create_server(state)
    url = f"http://127.0.0.1:{server.server_port}/"
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    logger.info("B24 设计变更页面：%s；共 %d 条记录", url, len(state.records))

    try:
        if args.no_ui:
            webbrowser.open(url)
            try:
                worker.join()
            except KeyboardInterrupt:
                pass
        else:
            import tkinter as tk
            from tkinter import ttk

            window = tk.Tk()
            chrome = _chrome_path()
            opened_chrome_windows: set[int] = set()
            chrome_launch_baselines: list[set[int]] = []

            def open_page() -> None:
                if chrome:
                    before = _chrome_windows()
                    chrome_launch_baselines.append(before)
                    try:
                        subprocess.Popen(
                            [str(chrome), "--new-window", url],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    except OSError as exc:
                        logger.warning("启动 Chrome 失败，将使用默认浏览器：%s", exc)
                        webbrowser.open_new(url)
                        return

                    attempts = 0

                    def find_opened_window() -> None:
                        nonlocal attempts
                        newly_opened = _chrome_windows() - before
                        opened_chrome_windows.update(newly_opened)
                        for hwnd in newly_opened:
                            _move_chrome_window_to_primary(hwnd)
                        if not newly_opened and attempts < 40 and window.winfo_exists():
                            attempts += 1
                            window.after(250, find_opened_window)

                    window.after(250, find_opened_window)
                else:
                    webbrowser.open_new(url)

            def close_control() -> None:
                # Catch a Chrome window that appeared just before the user closed the GUI.
                current_windows = _chrome_windows()
                for baseline in chrome_launch_baselines:
                    opened_chrome_windows.update(current_windows - baseline)
                for hwnd in opened_chrome_windows:
                    _close_chrome_window(hwnd)
                window.destroy()

            window.title("B24 设计变更归档")
            window.geometry("400x190")
            window.resizable(False, False)
            frame = ttk.Frame(window, padding=20)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=f"已读取 {len(state.records)} 条 B24 变更", font=("Microsoft YaHei UI", 13)).pack(pady=(4, 16))
            ttk.Button(frame, text="打开变更台账", command=open_page).pack(fill="x", pady=4)
            ttk.Label(frame, text="关闭此窗口将关闭本次打开的台账页面").pack(pady=10)
            # 等 Tk 窗口进入事件循环后再打开浏览器，确保桌面窗口与页面同时出现。
            window.protocol("WM_DELETE_WINDOW", close_control)
            window.after(250, open_page)
            window.mainloop()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
