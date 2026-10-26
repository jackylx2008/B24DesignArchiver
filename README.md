# B24 设计变更归档

运行 `python design-archiver.py` 会打开小型控制窗口。点击“打开变更台账”后，浏览器打开本机 HTML 页面；关闭控制窗口即停止服务。终端模式可运行 `python design-archiver.py --no-ui`，按 `Ctrl+C` 停止。

首次运行安装依赖：`python -m pip install -r requirements.txt`。复制 `config.example.yaml` 为 `config.yaml`，填写本机 B24 Excel 台账、按 2025-10-01 切换的两个审批表模板和资料源目录。`config.yaml` 包含业务路径，仅留在本机，不同步到 GitHub。PDF 流程需要 Windows 桌面版 Microsoft Word、Bluebeam Revu 20 eXtreme 和系统的“Microsoft Print to PDF”打印机。程序默认查找 Revu 20 标准安装目录；特殊安装路径可通过 `BLUEBEAM_SCRIPT_ENGINE` 指向 `ScriptEngine.exe`。默认成果位于本项目 `output/B24`，可在页面修改；本机选择保存在不入库的 `local_settings.json`。也可通过 `CLOUDSTATION_ROOT` 或 `CLOUDSTATION_ROOT_WINDOWS` 覆盖群晖同步根路径。

页面显示台账中所有填写了“变更通知单编号”的 B24 记录。每条记录可查看匹配的资料目录、PDF 和 DOCX，再逐项生成审批表、复制完整资料目录和 PDF/DOCX 文件、压缩原资料目录、将审批表 DOCX 转为 PDF 后使用 Bluebeam ScriptEngine 合并 PDF，并按竖向 A4、自动旋转、仅缩小的设置打印 A4 版。可勾选多条批量操作，或将已生成的多个独立成果目录打成总 ZIP。

每条变更使用输出根目录下的独立文件夹。单条合并可勾选要合并的 PDF；批量合并遇到多个候选 PDF 时会报告该条编号，留待人工选择。已有审批表和 ZIP 不自动覆盖；同名但内容不同的复制文件会追加序号；已有合并 PDF 需人工处理旧文件后才能重做。页面只在本机 `127.0.0.1` 运行。资料源目录不存在时会在预览中提示。

合并输出保留原 PDF 页面；A4 版通过 Revu 的 `PrintToFile` 调用“Microsoft Print to PDF”。本机 Bluebeam PDF 打印机在此命令下只生成 PostScript，因此不能直接作为 PDF 输出端。打印版可能不保留原 PDF 的可搜索文字和交互对象，需要检索时使用同目录的未重排合并版。

测试：`python -m unittest discover -s tests -v`。测试仅使用临时台账和临时资料，不写入真实业务目录。
