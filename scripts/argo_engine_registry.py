#!/usr/bin/env python3
"""engine_registry.py — Argo 统一引擎注册中心

合并主路径引擎 + local-search 子引擎，使 route.py 可以直接路由到
local_bing、local_baidu 等子引擎，消除 local_search 黑盒。

向后兼容：local_search 仍作为 fallback alias 保留。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("argo.engine_registry")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler())

ARGO_DIR = Path(__file__).resolve().parent.parent
LOCAL_SEARCH_DIR = ARGO_DIR / "sub-skills" / "local-search"
LOCAL_SEARCH_CONFIG = LOCAL_SEARCH_DIR / "config.yaml"
HEALTH_STATE_PATH = Path(os.path.expanduser("~/.cache/unified-search")) / "argo_engine_health.json"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class EngineRegistry:
    """统一引擎注册表：主引擎 + local-search 子引擎。"""

    def __init__(self):
        self._health: dict = {}
        self._load_health()

    def _load_health(self):
        if HEALTH_STATE_PATH.exists():
            try:
                self._health = json.loads(HEALTH_STATE_PATH.read_text())
            except Exception:
                self._health = {}

    def _save_health(self):
        try:
            HEALTH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            HEALTH_STATE_PATH.write_text(json.dumps(self._health, ensure_ascii=False, indent=2))
        except Exception:
            pass

    def get_local_search_engines(self) -> dict[str, dict]:
        """获取 local-search 子引擎配置（带 local_ 前缀）。"""
        cfg = _load_yaml(LOCAL_SEARCH_CONFIG)
        engines = cfg.get("engines", {})
        result = {}
        for name, spec in engines.items():
            merged = dict(spec)
            merged["name"] = name
            merged["_source"] = "local-search"
            health = self._health.get(name, {})
            merged["available"] = self._is_available(spec, health)
            merged["consecutive_failures"] = health.get("consecutive_failures", 0)
            result[name] = merged
        return result

    def _is_available(self, spec: dict, health: dict) -> bool:
        if not spec.get("enabled", True):
            return False
        if not health:
            return True
        return bool(health.get("available", True))

    def list_local_engines(self, available_only: bool = False) -> list[str]:
        """列出 local-search 子引擎名。"""
        result = []
        for name, spec in self.get_local_search_engines().items():
            if available_only and not spec.get("available", True):
                continue
            result.append(name)
        return result

    def update_health(self, name: str, available: bool, **extra):
        """更新引擎健康状态并持久化。"""
        record = self._health.setdefault(name, {})
        record["last_checked"] = time.time()
        record["available"] = available
        if available:
            record["consecutive_failures"] = 0
            record["last_ok"] = time.time()
        else:
            record["consecutive_failures"] = record.get("consecutive_failures", 0) + 1
        for k, v in extra.items():
            record[k] = v
        self._save_health()

    def is_available(self, name: str) -> bool:
        health = self._health.get(name, {})
        return bool(health.get("available", True))


# 单例
_registry: EngineRegistry | None = None


def get_registry() -> EngineRegistry:
    global _registry
    if _registry is None:
        _registry = EngineRegistry()
    return _registry
