"""Safety configuration helpers.

The safety layer intentionally reads small YAML files at use time. These files
are operator-facing policy knobs, so avoiding long-lived caches keeps local
changes effective without restarting the web service.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


SAFETY_CONFIG_DIR = Path("config/safety")


def read_safety_yaml(filename: str) -> dict[str, Any]:
    """Read a safety YAML file from config/safety.

    Missing or invalid files are treated as empty config. Invalid YAML is not
    silently ignored by PyYAML itself, but non-mapping top-level documents are.
    """
    path = SAFETY_CONFIG_DIR / filename
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def read_safety_list(filename: str, keys: tuple[str, ...]) -> list[Any]:
    """Read a list-like value from a safety config file.

    The loader accepts a few keys so the YAML remains human friendly:
    ``patterns:``, ``rules:``, or ``commands:`` depending on the file.
    """
    data = read_safety_yaml(filename)
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
    return []
