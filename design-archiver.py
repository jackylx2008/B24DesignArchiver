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
import sys
import threading
import webbrowser
from pathlib import Path

from logging_config import configure_utf8_stdio, setup_logger

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from b24_design_archiver.server import ArchiveState, create_server  # noqa: E402


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
            window.title("B24 设计变更归档")
            window.geometry("400x190")
            window.resizable(False, False)
            frame = ttk.Frame(window, padding=20)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=f"已读取 {len(state.records)} 条 B24 变更", font=("Microsoft YaHei UI", 13)).pack(pady=(4, 16))
            ttk.Button(frame, text="打开变更台账", command=lambda: webbrowser.open(url)).pack(fill="x", pady=4)
            ttk.Label(frame, text="关闭此窗口将停止本地服务").pack(pady=10)
            # 等 Tk 窗口进入事件循环后再打开浏览器，确保桌面窗口与页面同时出现。
            window.after(250, lambda: webbrowser.open(url))
            window.mainloop()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
