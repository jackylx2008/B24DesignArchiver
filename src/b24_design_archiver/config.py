"""读取本机路径配置。"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
VARIABLE = re.compile(r"\$\{([A-Z_]+)\}")


def _cloudstation_root() -> str:
    explicit = os.environ.get("CLOUDSTATION_ROOT")
    if explicit:
        return explicit
    platform_key = (
        "CLOUDSTATION_ROOT_WINDOWS" if sys.platform == "win32" else
        "CLOUDSTATION_ROOT_MACOS" if sys.platform == "darwin" else
        "CLOUDSTATION_ROOT_LINUX"
    )
    default = "D:/CloudStation" if sys.platform == "win32" else "~/SynologyDrive"
    return os.environ.get(platform_key, default)


def _expand(value: str) -> Path:
    variables = {"PROJECT_ROOT": str(PROJECT_ROOT), "CLOUDSTATION_ROOT": _cloudstation_root()}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise ValueError(f"未知路径变量: {key}")
        return variables[key]

    return Path(VARIABLE.sub(replace, value)).expanduser().resolve()


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise FileNotFoundError("缺少 config.yaml；请复制 config.example.yaml 并填写本机资料路径")
    with CONFIG_PATH.open(encoding="utf-8") as stream:
        app = yaml.safe_load(stream)["app"]
    for key in ("workbook", "template_before", "template_after", "output_root"):
        app[key] = _expand(app[key])
    app["source_directories"] = [_expand(value) for value in app["source_directories"]]
    return app
