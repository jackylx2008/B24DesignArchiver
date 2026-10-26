# Project Skills

## 通用前缀标记

为保证这份 `COMMON_PROJECT_SKILLS.md` 可复用，统一使用通用前缀标记 `PROJECT_PREFIX`。

使用方式约定：

- Python 包路径使用 `<project_prefix>` 形式，例如 `src/<project_prefix>/flows/`
- Python 入口脚本放在项目根目录下，不使用 `<project_prefix>` 包路径，例如 `example.py`
- 包内导入路径使用 `<project_prefix>` 形式，例如 `from <project_prefix>.flows.example_flow import run`
- 项目根目录基础设施直接按根模块导入，例如 `from logging_config import get_logger`

这份文档中出现的前缀示例，都应理解为“项目名替换位”，而不是某个固定项目名。

补充约定：

- 文档中的项目名、目录名、模块名、配置名、命令名、路径名和示例文件名应使用示意名称或通用占位名称。
- 配置示例不得使用真实项目名称、真实仓库名称、真实业务名称、真实机器路径或真实环境信息。
- 如需展示示例，应优先使用 `demo`、`sample`、`example`、`test`、`PROJECT_PREFIX` 等通用写法。

## 目标

在这个项目里，优先保持“基础能力模块 + 场景编排层 + 独立入口脚本”三层结构：

- 基础能力模块负责单一职责、可复用的处理能力。
- 场景编排层负责围绕某类需求组织处理步骤。
- 独立入口脚本负责对应不同需求的启动、配置接入和结果输出。

后续新增功能时，应优先沿着这条结构扩展，而不是把业务逻辑直接堆进某个入口文件里。

## 项目结构

项目采用 `src` 布局承载包代码，入口脚本放在项目根目录。推荐结构如下：

- `logging_config.py`
- `config.yaml`
- `common.env.example`
- `example.py`
- `src/<project_prefix>/modules/`
- `src/<project_prefix>/flows/`
- `docs/`
- `tests/`
- `logs/`

建议保留的基础模块类型包括：

- `logging_config.py`
  - 必须存在于项目根目录
  - 提供统一日志初始化和 logger 获取能力
- `config_loader.py`
  - 位于 `src/<project_prefix>/`
  - 负责读取 `config.yaml`
  - 支持 `${ENV_VAR:-default}` 形式的环境变量覆盖
  - 支持从 `common.env` 注入本地环境变量
- `context.py`
  - 提供统一上下文对象
  - 作为入口层与编排层共享配置、路径和运行信息的统一入口

目录职责约定：

- `modules/`
  - 放通用处理能力、公共工具、通用适配逻辑
- `flows/`
  - 放面向某类业务目标的编排逻辑
- `docs/`
  - 独立存放项目文档
  - 项目根目录只保留初始入口文档 `README.md`；除 `README.md` 外的架构、配置、部署、开发和使用说明等文档统一放入 `docs/`
- `tests/`
  - 统一存放测试代码，包括单元测试、集成测试及测试辅助代码
  - 不要将测试代码混放在 `src/`、入口脚本目录或业务模块中
- `logs/`
  - 统一存放运行日志
  - 日志目录固定使用复数形式 `logs/`，不得使用 `log/`
- 项目根目录下的独立 `.py` 文件
  - 放不同需求对应的独立启动脚本
  - 一个入口脚本对应一个明确工作流
  - 不再采用 CLI 子命令统一分发的方式


## 模块约定

模块层只负责可复用能力，不负责完整需求的入口组织。

约定：

- 新增基础能力时，优先放到 `modules`。
- 模块应尽量保持单一职责、低耦合、可复用。
- 模块不应直接绑定某一个具体入口或某一个具体需求。
- 模块可以是处理器、适配器、读写器、转换器、校验器、帮助函数集合等任意合适形态，不强制限定命名风格。
- 不要在模块里直接决定完整执行路径。
- 路径处理优先使用 `pathlib.Path`。
- 模块输出应尽量结构化，避免把不稳定格式直接暴露给上层。
- 模块接收已经解析的路径和明确参数，不直接读取 `config.yaml` 或 `common.env`。

## 编排层约定

编排层负责组织步骤，不负责承载底层通用实现细节。

约定：

- 编排层用于表达“为实现某一类目标，需要按什么顺序组合哪些能力”。
- 编排层应优先复用 `modules` 中已有能力，而不是重复实现底层细节。
- 编排层命名和公开入口应在同一项目内保持一致。
- 编排层通过统一上下文获取全局配置、场景配置和项目路径。
- 编排层使用统一日志模块获取 logger，不自行添加 handler。
- 编排结果应使用项目内统一的结构化形式，便于入口层汇总和确定退出状态。
- 如果一个新场景只是旧场景的扩展，优先复用已有编排逻辑或其依赖的基础模块。
- 编排层应重点表达步骤组织、阶段边界、异常传递和结果汇总，而不是沉入底层实现。

## 入口脚本约定

入口层采用“项目根目录下不同 Python 文件对应不同工作流”的方式组织，不使用 CLI 子命令统一分发。

当前入口位于：

- 项目根目录

组织原则：

- 一个入口脚本对应一个明确工作流或一类明确需求。
- 入口脚本名称应尽量直接反映用途。
- 各入口之间可共享同一套基础模块、上下文对象和日志能力。
- 公共逻辑应下沉到 `flows` 或 `modules`，不要在多个入口脚本里重复复制。

推荐示例：

- `scan.py`：承接扫描类工作流
- `report.py`：承接报告类工作流
- `export.py`：承接导出类工作流

约定：

- 入口脚本只负责：
  - 解析入口参数
  - 加载配置
  - 初始化日志
  - 创建上下文
  - 调用对应编排逻辑
  - 输出结果并处理退出状态
- 不要把核心业务逻辑直接写进入口脚本。
- 新增需求时，优先在项目根目录新增独立入口文件，而不是继续堆叠分支判断。
- 若多个入口共享相同启动动作，可抽出公共入口辅助模块，但不要重新退回到“大一统 CLI 分发”。

### 入口文件头 docstring

项目根目录下的 Python 入口文件，必须在文件头部写模块级 docstring。

要求：

- docstring 必须位于 `from __future__ import annotations` 之前。
- 使用中文说明。
- 同一项目的入口文件应使用一致的 docstring 风格。
- 不要只写一句简单描述；应写成入口工具说明。
- 如果入口文件无参数，也应说明固定读取哪些配置文件。
- 如果入口文件带 CLI 参数，应说明必填参数、可选参数和示例命令。
- 如果入口文件有输出文件、写入目标文件或控制台输出，应说明输出位置或输出内容。
- 如果运行逻辑依赖本地 `.env`、`config.yaml` 或其他配置文件，应说明这些配置文件的职责。

推荐结构：

```python
"""示例工具

用途：
  说明该入口脚本解决什么问题、处理什么数据、调用哪个类型的工作流。

配置文件：
  说明默认读取哪些配置文件，以及各配置文件负责什么。

必填参数：
  --name   说明参数用途

可选参数：
  --config-file   说明默认值和用途

示例：
  python example.py --name demo

输出：
  说明输出文件位置、目标文件写入方式或控制台输出内容。
"""
```

入口参数帮助应优先复用文件头 docstring，例如：

```python
parser = argparse.ArgumentParser(
    description=__doc__,
    formatter_class=argparse.RawDescriptionHelpFormatter,
)
```

## 配置约定

### `config.yaml`

应用配置入口统一为项目根目录下的 `config.yaml`。

通用结构可参考：

- `app`
  - `log_level`
  - `input_path`
  - `output_dir`
- `flows.example`
  - 当前场景需要的输入、输出和处理选项

约定：

- 全局运行项放在 `app` 节点。
- 特定场景或编排逻辑的配置放在 `flows.<name>` 或其他统一约定的命名空间下。
- 默认值写在 YAML 中。
- 本地差异优先通过环境变量覆盖，而不是写死在源码里。
- 配置结构应服务于“可复用、可扩展、可替换”，不要围绕某一个入口文件临时拼接字段。
- 配置加载、环境变量展开和相对路径解析应集中处理，避免分散在入口、编排或基础模块中。
- 编排层只读取与自身场景对应的配置；基础模块不读取业务配置。

补充约定：

- 配置示例使用的名称必须为示意名称，不得使用真实名称。
- 示例中的目录、文件名、数据源名称、服务地址、环境变量值等，都应使用占位内容。
- 示例中的路径优先写成相对路径、虚拟路径或通用路径，不要写真实本机路径。
- 示例中的项目标识、仓库标识、业务标识均应脱敏处理。

### `common.env`

本地环境文件不入库，仓库中只保留 `common.env.example`。

当前示例变量可包括：

- `LOG_LEVEL`
- `INPUT_PATH`
- `OUTPUT_DIR`
- `CLOUDSTATION_ROOT`
- `CLOUDSTATION_ROOT_WINDOWS`
- `CLOUDSTATION_ROOT_MACOS`
- `CLOUDSTATION_ROOT_LINUX`
- 工作流覆盖变量，例如 `EXAMPLE_INPUT_PATH`、`EXAMPLE_OUTPUT_DIR`

约定：

- 机器相关、路径相关、本地调试相关配置放在 `common.env`。
- 绝对路径只应出现在本地环境文件里，不应扩散到源码里。
- `common.env.example` 中的值仅作格式示意，不应包含真实路径、真实账号、真实服务地址或真实项目标识。
- `common.env` 不覆盖进程中已经存在的环境变量，便于命令行或运行环境显式指定更高优先级值。

### CloudStation 根目录

跨 Windows、macOS 或 Linux 同步开发时，群晖同步根目录统一抽象为 `CLOUDSTATION_ROOT`。

约定：

- 每个项目都必须配置 CloudStation 根目录，用于在不同操作系统上访问或处理同步范围内的数据。
- Windows 系统的 CloudStation 根目录固定为 `D:\CloudStation`。
- macOS 系统的 CloudStation 根目录固定为 `~/SynologyDrive/`。
- 源码和 `config.yaml` 中不要直接写死上述路径或其他本机绝对路径，应统一通过 CloudStation 根目录配置引用。
- 路径字段中使用 `${CLOUDSTATION_ROOT}` 作为标记符，例如 `${CLOUDSTATION_ROOT}/Python/Project/<project_name>/data/input.xlsx`。
- 本地环境文件中可以同时保留不同平台的根目录变量，由运行时代码根据当前系统选择。
- 若显式提供 `CLOUDSTATION_ROOT`，它优先于平台变量。
- 平台变量命名统一为：
  - `CLOUDSTATION_ROOT_WINDOWS`
  - `CLOUDSTATION_ROOT_MACOS`
  - `CLOUDSTATION_ROOT_LINUX`

`common.env` 或 `.env` 示例：

```dotenv
CLOUDSTATION_ROOT_WINDOWS=D:\CloudStation
CLOUDSTATION_ROOT_MACOS=~/SynologyDrive/
CLOUDSTATION_ROOT_LINUX=~/CloudStation
```

`config.yaml` 示例：

```yaml
local_excel:
  path: "${CLOUDSTATION_ROOT}/Python/Project/<project_name>/data/local.xlsx"
  output_path: ""
```

实现要求：

- 项目应在统一配置加载或路径解析入口中解析 `${CLOUDSTATION_ROOT}`。
- Windows 下选择 `CLOUDSTATION_ROOT_WINDOWS`。
- macOS 下选择 `CLOUDSTATION_ROOT_MACOS`。
- Linux 下选择 `CLOUDSTATION_ROOT_LINUX`。
- `~` 应通过 `Path(...).expanduser()` 或等价方式展开。
- 模块内部接收解析后的路径，避免在业务逻辑中判断操作系统。

## 日志约定

项目必须在项目根目录下提供 `logging_config.py`，并统一通过该文件初始化日志。
`logging_config.py` 不放入包目录、`src/` 目录或入口脚本旁的子目录，避免不同入口脚本各自维护日志配置。

当前公开接口可包括：

- `setup_logger(...)`
- `get_logger(name)`

统一行为要求：

- 同时输出到控制台和滚动文件
- 日志文件统一保存到项目根目录下的 `logs/`
- 不得创建或使用 `log/` 作为日志目录
- 日志文件名基于当前入口脚本名
- 单文件 10 MB
- 保留 5 份备份

约定：

- 在入口脚本调用编排逻辑前完成日志初始化。
- 编排层和需要记录日志的基础模块统一通过 `get_logger(__name__)` 获取 logger。
- 除非有明确理由，不要在其他模块里重复配置 handler 或 root logger。
- 日志策略应保持项目内一致，不因单个入口脚本而单独割裂。

推荐模式：

```python
from logging_config import get_logger, setup_logger

setup_logger(log_level="INFO")
logger = get_logger(__name__)
```

## 测试约定

测试代码统一位于项目根目录的 `tests/`。

约定：

- 新增或修改基础模块时，应在 `tests/` 增加对应单元测试。
- 编排或入口行为变复杂时，应补充工作流级或入口级测试。
- 测试命令和静态检查命令应在 `README.md` 中明确。
- 测试不得依赖或修改真实业务数据目录，应使用临时目录和最小化样本。

## Git 推送约定

项目需要同步到远端仓库时，优先按下面顺序处理。

同步 Git 前必须维护 `README.md`：

- 如果仓库没有 `README.md`，先创建。
- 如果入口脚本、配置方式、运行命令、输出目录或测试方式变化，必须同步更新 `README.md`。
- `README.md` 应进入版本库，用于说明项目用途、运行方式、配置方式和 Git 同步注意事项。

`docs/COMMON_PROJECT_SKILLS.md` 是项目文档的一部分，应由 Git 跟踪：

- 不要在 `.gitignore` 中排除该文件。
- 项目结构、配置、入口、日志或测试约定发生变化时，应同步更新并提交该文件。
- 文档中不得写入真实业务数据、凭据或仅适用于某台机器的私有信息。

### 基础检查

先确认当前目录是否已经是 Git 仓库、当前分支、工作区状态和远端地址：

```powershell
git status --short --branch
git branch --show-current
git remote -v
```

如果当前目录还不是 Git 仓库，可初始化 `main` 分支并添加远端：

```powershell
git init -b main
git remote add origin <remote_url>
```

提交前先确认 `.gitignore` 已排除运行产物、本地环境文件和日志目录，例如：

- `output/`
- `logs/`
- `*.env`
- `common.env`

### 提交和常规推送

常规提交流程：

```powershell
git add -A
git status --short
git commit -m "Initial project commit"
```

如果远端使用 HTTPS 且网络、认证均正常，可直接推送：

```powershell
git remote set-url origin https://github.com/<owner>/<repo>.git
git push -u origin main
```

如果 HTTPS 方式出现 `Failed to connect to github.com port 443`、连接超时或 Git 自身网络栈异常，优先改用 SSH。

### SSH 推送

确认 SSH key 可用：

```powershell
ssh -T -o BatchMode=yes git@github.com
```

如果返回已成功认证的信息，即可使用 SSH remote：

```powershell
git remote set-url origin git@github.com:<owner>/<repo>.git
git push -u origin main
```

如果本机 `~/.ssh/config` 已配置 GitHub 走 `ssh.github.com:443`，`git@github.com:<owner>/<repo>.git` 仍可保持为项目 remote，具体端口由 SSH 配置接管。

示例 SSH 配置：

```text
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

### SSH 443 备用推送

当需要绕过项目 remote 配置，或显式验证 `ssh.github.com:443` 通道时，可使用 `GIT_SSH_COMMAND`：

```powershell
$env:GIT_SSH_COMMAND = "ssh -F none -p 443 -i `"$env:USERPROFILE\.ssh\id_ed25519`" -o IdentitiesOnly=yes"
git ls-remote ssh://git@ssh.github.com/<owner>/<repo>.git refs/heads/main
git push ssh://git@ssh.github.com/<owner>/<repo>.git main:refs/heads/main
```

说明：

- `<owner>` 和 `<repo>` 必须替换为实际仓库归属和仓库名。
- 示例中使用 `id_ed25519`，如本机使用其他 key，应替换为对应私钥路径。
- `Everything up-to-date` 表示本地提交已经和远端一致，不需要重复推送。
- 推送完成后用 `git status --short --branch` 和 `git ls-remote origin refs/heads/main` 验证本地与远端提交是否一致。

## 默认工作方式

如果后续继续在这个项目上开发，默认按下面的顺序推进：

1. 先确认新增内容属于基础模块、编排层还是入口层。
2. 先补配置项，再写业务逻辑。
3. 先复用现有 logger 和 context，不要新开一套接线方式。
4. 让编排层负责组织步骤，让基础模块负责提供能力。
5. 不同需求优先使用项目根目录下的独立入口脚本承接。
6. 运行产物写入 `output/`，日志写入 `logs/`。

## 适用场景

这份技能文档适用于：

- 新增处理能力
- 扩展新的编排逻辑
- 增加项目根目录下新的独立入口脚本
- 增加配置项、日志项和输出方式
- 保持项目结构可扩展、可复用
- 控制示例内容的通用性与脱敏性

## 不建议的做法

- 在入口脚本中直接堆完整业务逻辑
- 在编排层中重复实现底层处理细节
- 在基础模块中混入完整流程编排逻辑
- 在源码里写死本机绝对路径
- 为每个模块单独维护一套日志配置
- 在示例配置中使用真实项目名称、真实路径、真实仓库名称或真实业务标识
- 把运行产物、日志文件或真实 `common.env` 提交到版本库
