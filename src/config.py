# -*- coding: utf-8 -*-
"""配置加载。

所有模块统一从这里拿配置，避免各处硬编码路径和阈值。
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"


def _resolve(path: str | Path) -> Path:
    """相对路径一律相对项目根目录解析，这样从任何工作目录运行都不会错。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


@functools.lru_cache(maxsize=8)
def load_yaml(path: str | Path) -> dict[str, Any]:
    full = _resolve(path)
    if not full.exists():
        raise FileNotFoundError(f"配置文件不存在: {full}")
    with open(full, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@functools.lru_cache(maxsize=1)
def load_config(path: str | Path = DEFAULT_CONFIG) -> dict[str, Any]:
    """加载主配置，并把 paths 下的相对路径展开成绝对路径。"""
    cfg = dict(load_yaml(path))
    paths = dict(cfg.get("paths", {}))
    for key, value in paths.items():
        paths[key] = str(_resolve(value))
    cfg["paths"] = paths
    return cfg


def get(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """按 "matcher.slot_hit_threshold" 这种点分路径取值。"""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
