from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "lgo_config.json"


def load_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CONFIG_PATH
    with config_path.open("r", encoding="utf-8") as handle:
        return _expand_config_tokens(json.load(handle))


def project_path(*parts: str) -> Path:
    return PROJECT_ROOT.joinpath(*parts)


def _expand_config_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_config_tokens(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_config_tokens(item) for item in value]
    if isinstance(value, str):
        return value.replace("{project_root}", str(PROJECT_ROOT))
    return value
