# B24 设计变更归档

[本次更新说明](CHANGELOG.md)

运行 `python design-archiver.py` 会打开小型控制窗口。点击“打开变更台账”后，浏览器打开本机 HTML 页面；关闭控制窗口即停止服务。终端模式可运行 `python design-archiver.py --no-ui`，按 `Ctrl+C` 停止。

首次运行安装依赖：`python -m pip install -r requirements.txt`。复制 `config.example.yaml` 为 `config.yaml`，填写本机 B24 Excel 台账、按 2025-10-01 切换的两个审批表模板和资料源目录。`config.yaml` 包含业务路径，仅留在本机，不同步到 GitHub。PDF 流程需要 Windows 桌面版 Microsoft Word、Bluebeam Revu 20 eXtreme 和系统的“Microsoft Print to PDF”打印机。程序默认查找 Revu 20 标准安装目录；特殊安装路径可通过 `BLUEBEAM_SCRIPT_ENGINE` 指向 `ScriptEngine.exe`。默认成果位于本项目 `output/B24`，页面不显示路径设置；如本机已有 `local_settings.json`，继续使用其中保存的目录。也可通过 `CLOUDSTATION_ROOT` 或 `CLOUDSTATION_ROOT_WINDOWS` 覆盖群晖同步根路径。

页面显示台账中所有填写了“变更通知单编号”的 B24 记录。每条记录可查看匹配的资料目录、PDF 和 DOCX，再逐项生成审批表、复制完整资料目录和 PDF/DOCX 文件、压缩原资料目录、将审批表 DOCX 转为 PDF 后使用 Bluebeam ScriptEngine 合并 PDF，并按竖向 A4、自动旋转、仅缩小的设置打印 A4 版。可勾选多条批量操作，或将已生成的多个独立成果目录打成总 ZIP。

表格中的变更通知单编号是链接，点击会在文件管理器中打开该条成果目录；目录尚不存在时会先创建。

页面的操作栏与表格标题行在滚动条目时保持可见，日期保持单行。页面根据“变更通知单编号”的前缀填写并筛选专业：`05` 给排水、`06` 暖通、`JZ-05` 精装给排水、`JZ-06` 精装暖通、`R20`/`RL20` 热力、`251614GC` 燃气、“泳池变更”泳池设备。原始台账字段仍供审批表模板替换使用。

启动、重读台账或复制资料时，程序会把当前全部条目保存为输出根目录下的 UTF-8 文件 `B24变更条目.json`。JSON 将台账表头中的中文数字标记去掉，例如“变更内容三”导出为“变更内容”；“审批表编号”和空的“何种专业四”不导出。每条 `_id` 是由变更通知单编号生成的稳定 SHA-256 hash；页面显示、资料匹配和成果文件名仍使用变更通知单编号。每条 JSON 记录的“已复制资料”和“资料文件”反映输出目录中实际保存的资料。

`config.yaml` 中的 `legacy_config_files` 可引用 `AutoZipFolder/path_config_B24.yaml` 和 `CopyZipDesktop/path_config_B24.yaml`，程序会合并这两个项目配置中的资料、PDF 和 DOCX 来源并去重。页面点击“同步已有资料”会将匹配的文件夹及 PDF/DOCX 复制到每条变更的成果目录，自动更新 JSON；再次同步会跳过已复制且内容相同的文件。不存在的来源目录不会阻止其余资料复制。

示例配置中的 `legacy_config_files` 可按本机路径调整；这两个旧项目没有安装时也可以删掉该配置项，只使用 `source_directories`。更新程序后需重启本地服务，浏览器刷新页面即可使用新界面。若其他脚本读取旧 JSON，请注意 `_id` 已改为 hash，原编号仍在“变更通知单编号”字段。

每条变更使用输出根目录下的独立文件夹。单条合并可勾选要合并的 PDF；批量合并遇到多个候选 PDF 时会报告该条编号，留待人工选择。已有审批表和 ZIP 不自动覆盖；同名但内容不同的复制文件会追加序号；已有合并 PDF 需人工处理旧文件后才能重做。页面只在本机 `127.0.0.1` 运行。资料源目录不存在时会在预览中提示。

合并输出保留原 PDF 页面；A4 版通过 Revu 的 `PrintToFile` 调用“Microsoft Print to PDF”。本机 Bluebeam PDF 打印机在此命令下只生成 PostScript，因此不能直接作为 PDF 输出端。打印版可能不保留原 PDF 的可搜索文字和交互对象，需要检索时使用同目录的未重排合并版。

测试：`python -m unittest discover -s tests -v`。测试仅使用临时台账和临时资料，不写入真实业务目录。
