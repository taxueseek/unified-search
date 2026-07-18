#!/usr/bin/env python3
"""
config.py — Unified Search 配置加载器

职责：
  - 从项目根目录的 config.yaml 加载统一配置
  - 支持热加载（按 mtime 缓存）
  - 使用 PyYAML 解析（缺失时给出明确安装提示）
  - 将 ~ 展开为实际用户目录
  - 提供类型化访问接口（引擎列表、域规则、缓存/执行/输出配置）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── 路径 ──────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ── 默认配置（config.yaml 缺失/损坏时的兜底）──────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "engines": {
        "anysearch": {
            "enabled": True,
            "type": "http",
            "method": "POST",
            "url": "https://api.anysearch.com/mcp",
            "headers": {
                "Content-Type": "application/json",
                "X-Anysearch-Client": "unified-search/1.0.0",
            },
            "body": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search",
                    "arguments": {
                        "query": "{query}",
                        "max_results": "{n}",
                    },
                },
            },
            "format": "jsonrpc",
            "timeout": 15,
            "_api_key_header": "Authorization",
            "_api_key_env": "ANYSEARCH_API_KEY",
            "_api_key_prefix": "Bearer ",
        },
        "eastmoney": {
            "enabled": True,
            "type": "http",
            "method": "POST",
            "url": "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search",
            "headers": {"Content-Type": "application/json"},
            "body": {"query": "{query}"},
            "format": "json",
            "timeout": 10,
            "_api_key_header": "apikey",
            "_api_key_env": "EASTMONEY_APIKEY",
        },
        "tavily": {
            "enabled": False,
            "type": "http",
            "module": "tavily",
            "client_class": "TavilyClient",
            "search_method": "search",
            "env_var": "TAVILY_API_KEY",
        },
    },
    "domains": [
        {
            "name": "general_search",
            "desc": "通用搜索（兜底）",
            "patterns": [],
            "primary": "anysearch",
            "fallback": "wigolo",
            "parallel": True,
        },
    ],
    "cache": {
        "enabled": True,
        "db_path": "~/.cache/unified-search/cache.db",
        "ttl": 3600,
        "max_size_mb": 200,
    },
    "execution": {
        "default_timeout": 8,
        "parallel_timeout": 6,
        "max_parallel_engines": 3,
        "retry_count": 0,
    },
    "output": {
        "format": "auto",
        "include_scores": True,
        "include_routing_decision": True,
    },
}


# ── YAML 加载 ─────────────────────────────────────────────────────────────────

def _require_yaml():
    """导入 PyYAML；不可用时抛出清晰错误。"""
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError as e:
        raise ImportError(
            "缺少 PyYAML，请安装：pip install pyyaml"
        ) from e


def _load_yaml(text: str) -> dict[str, Any]:
    """用 PyYAML 解析 YAML 文本，返回 dict。"""
    yaml = _require_yaml()
    parsed = yaml.safe_load(text)
    if isinstance(parsed, dict):
        return parsed
    return {}


# ── 配置加载与缓存 ─────────────────────────────────────────────────────────────

_config_cache: dict[str, Any] | None = None
_config_mtime: float = 0.0
_config_load_error: str | None = None


def _expand_value(value: Any) -> Any:
    """递归展开字符串中的 ~ 为用户目录。"""
    if isinstance(value, str):
        return os.path.expanduser(value)
    if isinstance(value, list):
        return [_expand_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand_value(v) for k, v in value.items()}
    return value


def _validate_engine_paths(config: dict[str, Any]) -> dict[str, Any]:
    """验证引擎 CLI 路径，不存在则标记为 disabled。

    对 type=cli 的引擎，检查 cmd 列表最后一个元素（脚本路径）是否存在。
    若最后一个元素不是脚本路径，或 cmd[0] 是 npx/node 则跳过检查。
    若不存在，自动设置 enabled=False 并记录日志。
    """
    import logging as _logging
    _log = _logging.getLogger("unified_search.config")

    for name, spec in config.get("engines", {}).items():
        if not isinstance(spec, dict):
            continue
        if spec.get("type") != "cli":
            continue
        cmd = spec.get("cmd", [])
        if not cmd:
            continue
        # npx/node 命令跳过路径检查（命令本身由 npx 运行时解析）
        if cmd[0] in ("npx", "node"):
            continue
        cmd_path_str = cmd[-1]
        if cmd_path_str.startswith("--"):
            continue
        cmd_path = Path(cmd_path_str).expanduser()
        if not cmd_path.exists():
            spec["enabled"] = False
            _log.warning(f"引擎 {name} 路径不存在，已自动禁用: {cmd_path}")
    return config


def load_config(force: bool = False) -> dict[str, Any]:
    """加载配置，支持热加载。

    Args:
        force: 强制重新加载，忽略 mtime 缓存。

    Returns:
        配置字典；加载失败时返回 DEFAULT_CONFIG 的深拷贝。
    """
    global _config_cache, _config_mtime, _config_load_error

    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        if _config_cache is None:
            _config_cache = _validate_engine_paths(
                _expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))
            )
        return _config_cache

    if not force and _config_cache is not None and mtime == _config_mtime:
        return _config_cache

    try:
        text = CONFIG_PATH.read_text(encoding="utf-8")
        parsed = _load_yaml(text)
        if parsed:
            _config_cache = _validate_engine_paths(_expand_value(parsed))
            _config_mtime = mtime
            _config_load_error = None
        else:
            if _config_cache is None:
                _config_cache = _validate_engine_paths(
                    _expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))
                )
    except ImportError as e:
        # PyYAML 缺失：向上抛出清晰错误，不静默回退
        _config_load_error = str(e)
        raise
    except Exception as e:
        _config_load_error = str(e)
        if _config_cache is None:
            _config_cache = _validate_engine_paths(
                _expand_value(json.loads(json.dumps(DEFAULT_CONFIG)))
            )

    return _config_cache


def last_load_error() -> str | None:
    """返回最近一次加载 config.yaml 的错误信息（若有）。"""
    return _config_load_error


# ── 类型化访问接口 ─────────────────────────────────────────────────────────────

def get_engines(config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """返回启用的引擎配置字典，键为引擎名。"""
    cfg = config if config is not None else load_config()
    engines = cfg.get("engines", {})
    return {name: spec for name, spec in engines.items() if spec.get("enabled", True)}


def get_domains(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """返回按优先级排序的域规则列表。"""
    cfg = config if config is not None else load_config()
    return cfg.get("domains", [])


def get_cache_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回缓存配置。"""
    cfg = config if config is not None else load_config()
    return cfg.get("cache", DEFAULT_CONFIG["cache"])


def get_execution_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回执行层配置。"""
    cfg = config if config is not None else load_config()
    return cfg.get("execution", DEFAULT_CONFIG["execution"])


def get_output_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回输出配置。"""
    cfg = config if config is not None else load_config()
    return cfg.get("output", DEFAULT_CONFIG["output"])


# ── CLI 调试用 ─────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="Unified Search 配置查看器")
    parser.add_argument("--path", action="store_true", help="显示配置文件路径")
    parser.add_argument("--engines", action="store_true", help="显示引擎配置")
    parser.add_argument("--domains", action="store_true", help="显示域规则")
    parser.add_argument("--check", action="store_true", help="检查并报告解析问题")
    args = parser.parse_args()

    if args.path:
        print(CONFIG_PATH)
        return

    cfg = load_config(force=True)

    if args.engines:
        print(json.dumps(get_engines(cfg), ensure_ascii=False, indent=2))
    elif args.domains:
        print(json.dumps(get_domains(cfg), ensure_ascii=False, indent=2))
    elif args.check:
        err = last_load_error()
        print(json.dumps({
            "path": str(CONFIG_PATH),
            "ok": err is None,
            "error": err,
            "engines": list(get_engines(cfg).keys()),
            "domains": [d.get("name") for d in get_domains(cfg)],
        }, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
