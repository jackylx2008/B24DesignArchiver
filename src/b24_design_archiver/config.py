"""读取本机路径配置。"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
VARIABLE = re.compile(r"\$\{([A-Z_]+)\}")
LOGGER = logging.getLogger(__name__)


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
    env_file = PROJECT_ROOT / "common.env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() and not name.lstrip().startswith("#"):
                os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))
    app["local_ai_base_url"] = os.environ.get("LLAMACPP_BASE_URL", "http://127.0.0.1:8080/v1").strip()
    app["local_ai_model"] = os.environ.get("LLAMACPP_MODEL", "").strip()
    app["local_ai_api_key"] = os.environ.get("LLAMACPP_API_KEY", "").strip()
    app["local_ai_timeout"] = int(os.environ.get("LLAMACPP_TIMEOUT_SEC", "180"))
    for key in ("workbook", "template_before", "template_after", "output_root"):
        app[key] = _expand(app[key])
    sources = [_expand(value) for value in app["source_directories"]]
    for raw in app.get("legacy_config_files", []):
        path = _expand(raw)
        if not path.is_file():
            LOGGER.warning("旧项目资料配置不存在：%s", path)
            continue
        with path.open(encoding="utf-8") as stream:
            old = yaml.safe_load(stream) or {}
        for key in ("base_directories", "pdf_directories", "docx_directories"):
            for entry in old.get(key, []):
                source = Path(entry).expanduser()
                if not source.is_absolute():
                    source = path.parent / source
                source = source.resolve()
                if source.is_dir():
                    sources.append(source)
                else:
                    LOGGER.warning("旧项目资料目录不存在，已跳过：%s", source)
    app["source_directories"] = list(dict.fromkeys(sources))
    return app
