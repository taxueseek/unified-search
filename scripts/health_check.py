#!/usr/bin/env python3
"""
health_check.py — 引擎健康检查

职责：
  - 遍历三层引擎注册表所有引擎
  - 标记可用/降级/不可用
  - 按层级筛选可用引擎
  - 输出健康报告供路由决策使用

调用方式：
  - 内部：from health_check import health_check_all, get_available_engines_by_tier
  - CLI：python3 health_check.py [--tier T1] [--json]
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# 添加 scripts 目录到 import 路径
SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "backends" / "engine_registry.yaml"

# 本地搜索模块路径（优雅降级，缺失时不阻断）
# 默认路径为项目内 local-search/ 子目录，可通过环境变量 LOCAL_SEARCH_PATH 覆盖
_LOCAL_SEARCH_V3_PATH = Path(os.environ.get(
    "LOCAL_SEARCH_PATH",
    str(Path(__file__).resolve().parent.parent / "local-search" / "search_v3.py")
))
_SEARXNG_BRIDGE_PATH = Path(os.environ.get(
    "SEARXNG_BRIDGE_PATH",
    str(Path(__file__).resolve().parent.parent / "local-search" / "searxng_bridge.py")
))


def _load_registry() -> list[dict[str, Any]]:
    """加载引擎注册表 YAML。"""
    if not REGISTRY_PATH.exists():
        return []

    if yaml is None:
        # 无 PyYAML 时尝试简易解析（仅处理基本结构）
        return _parse_yaml_light()

    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return []

    if not isinstance(data, dict):
        return []

    return data.get("engines", [])


def _parse_yaml_light() -> list[dict[str, Any]]:
    """简易 YAML 解析器回退 — 仅提取 engines 列表。"""
    try:
        text = REGISTRY_PATH.read_text(encoding="utf-8")
        # 查找 engines: 段落后面的条目
        # 这是一个非常基础的解析，PyYAML 不可用时才使用
        return []
    except Exception:
        return []


def _check_t1_engine(name: str, timeout: float = 5) -> dict[str, Any]:
    """检查 T1 引擎健康状态。

    通过 engines.py 的 search() 函数快速探测。
    """
    result = {
        "engine": name,
        "tier": "T1",
        "available": False,
        "latency_ms": 0,
        "error": None,
        "status": "unavailable",
    }

    try:
        from engines import search, get_registry
        registry = get_registry()
        if name not in registry:
            result["error"] = f"引擎 {name} 未在 engines.py 注册表中"
            result["status"] = "unavailable"
            return result

        fn = registry[name]
        t0 = time.time()
        # 快速探测：只请求 1 条结果
        try:
            results = fn("test", n=1, _timeout=timeout)
        except TypeError:
            results = fn("test", n=1)

        elapsed = round((time.time() - t0) * 1000)
        result["latency_ms"] = elapsed

        if results and isinstance(results, list) and len(results) > 0:
            result["available"] = True
            result["status"] = "ok"
            result["sample"] = results[0]
        else:
            result["error"] = "返回空结果"
            result["status"] = "degraded"

    except ImportError:
        result["error"] = "engines.py 不可导入"
        result["status"] = "unavailable"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["status"] = "unavailable"

    return result


def _check_t2_engine(name: str, timeout: float = 5) -> dict[str, Any]:
    """检查 T2 引擎健康状态。

    通过 search_v3.check_engine_health() 探测。
    引擎名去掉 local/ 前缀后传给 check_engine_health。
    """
    local_name = name.replace("local/", "") if name.startswith("local/") else name

    result = {
        "engine": name,
        "tier": "T2",
        "available": False,
        "latency_ms": 0,
        "error": None,
        "status": "unavailable",
    }

    try:
        sys.path.insert(0, str(_LOCAL_SEARCH_V3_PATH.parent))
        from search_v3 import check_engine_health
        health = check_engine_health(local_name, timeout=timeout)
        result["available"] = health.get("available", False)
        result["latency_ms"] = health.get("latency_ms", 0)
        result["error"] = health.get("error")
        result["status"] = health.get("status", "unavailable")
        if health.get("sample"):
            result["sample"] = health["sample"]
    except ImportError:
        result["error"] = "search_v3.py 不可导入"
        result["status"] = "unavailable"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["status"] = "unavailable"

    return result


def _check_t3_engine(name: str, timeout: float = 5) -> dict[str, Any]:
    """检查 T3 引擎健康状态。

    通过 searxng_bridge 检查 SearXNG 是否可达，然后检查引擎是否在列表中。
    """
    result = {
        "engine": name,
        "tier": "T3",
        "available": False,
        "latency_ms": 0,
        "error": None,
        "status": "unavailable",
    }

    try:
        sys.path.insert(0, str(_SEARXNG_BRIDGE_PATH.parent))
        from searxng_bridge import fetch_searxng_engine_list

        t0 = time.time()
        engine_data = fetch_searxng_engine_list(timeout=timeout)
        elapsed = round((time.time() - t0) * 1000)
        result["latency_ms"] = elapsed

        if engine_data.get("error"):
            result["error"] = engine_data["error"]
            result["status"] = "unavailable"
            return result

        # 检查指定引擎是否在垂直可用列表中
        searxng_engine_name = name.replace("searxng/", "")
        available_verticals = engine_data.get("available_vertical", [])
        available_names = {e.get("name", "").lower() for e in available_verticals}

        if searxng_engine_name.lower() in available_names or \
           searxng_engine_name.lower().replace("_", " ") in available_names:
            result["available"] = True
            result["status"] = "ok"
        else:
            # 也检查是否在 category_map 中
            cmap = engine_data.get("category_map", {})
            found = False
            for cat_engines in cmap.values():
                if searxng_engine_name.lower() in [e.lower() for e in cat_engines]:
                    found = True
                    break
            if found:
                result["available"] = True
                result["status"] = "ok"
            else:
                result["error"] = f"引擎 {searxng_engine_name} 不在 SearXNG 可用垂直列表中"
                result["status"] = "degraded"

    except ImportError:
        result["error"] = "searxng_bridge.py 不可导入"
        result["status"] = "unavailable"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["status"] = "unavailable"

    return result


# 健康检查映射
_CHECKERS = {
    "T1": _check_t1_engine,
    "T2": _check_t2_engine,
    "T3": _check_t3_engine,
}


def health_check_all(
    timeout: float = 5,
    tiers: list[str] | None = None,
    fast: bool = False,
) -> dict[str, Any]:
    """遍历注册表所有引擎，标记可用/降级/不可用。

    Args:
        timeout: 单引擎探测超时（秒）
        tiers: 要检查的层级列表（None = 全部）
        fast: 快速模式 — 仅检查推荐引擎

    Returns:
        {
            "ok": [...],
            "degraded": [...],
            "unavailable": [...],
            "by_tier": {"T1": [...], "T2": [...], "T3": [...]},
            "summary": {"total": N, "ok_count": N, "degraded_count": N, ...},
            "elapsed_ms": N,
        }
    """
    t0_total = time.time()

    registry = _load_registry()
    if not registry:
        return {
            "ok": [], "degraded": [], "unavailable": [],
            "by_tier": {}, "summary": {"total": 0, "ok_count": 0,
                                       "degraded_count": 0, "unavailable_count": 0},
            "elapsed_ms": 0, "error": "引擎注册表为空或不可读",
        }

    target_tiers = set(tiers) if tiers else {"T1", "T2", "T3"}

    ok_list: list[dict] = []
    degraded_list: list[dict] = []
    unavailable_list: list[dict] = []
    by_tier: dict[str, list[dict]] = {"T1": [], "T2": [], "T3": []}

    for engine in registry:
        name = engine.get("name", "")
        tier = engine.get("tier", "")
        status = engine.get("status", "unknown")

        if tier not in target_tiers:
            continue

        # 快速模式：仅检查推荐引擎 + 状态为 ok 的引擎
        if fast:
            is_recommended = engine.get("recommended", False)
            is_ok = status == "ok"
            if not (is_recommended or is_ok):
                engine["_checked"] = False
                engine["_skip_reason"] = "非推荐引擎（快速模式）"
                continue

        # 如果注册表已标记为 disabled，直接跳过
        if status == "disabled":
            engine["_checked"] = False
            engine["_skip_reason"] = "已禁用"
            continue

        # 对于 degraded 状态的引擎（如 Baidu），不做探测直接使用注册表状态
        if status == "degraded":
            check_result = {
                "engine": name,
                "tier": tier,
                "available": False,
                "latency_ms": engine.get("latency_ms", 0),
                "error": engine.get("note", "已降级"),
                "status": "degraded",
                "_checked": True,
                "_from_registry": True,
            }
            degraded_list.append(check_result)
            by_tier.setdefault(tier, []).append(check_result)
            continue

        # 执行健康检查
        checker = _CHECKERS.get(tier)
        if checker:
            check_result = checker(name, timeout=timeout)
            check_result["_checked"] = True
        else:
            check_result = {
                "engine": name,
                "tier": tier,
                "available": False,
                "latency_ms": 0,
                "error": f"未知层级: {tier}",
                "status": "unavailable",
                "_checked": True,
            }

        # 分类
        st = check_result.get("status", "unavailable")
        if st == "ok":
            ok_list.append(check_result)
        elif st in ("degraded", "slow"):
            degraded_list.append(check_result)
        else:
            unavailable_list.append(check_result)

        by_tier.setdefault(tier, []).append(check_result)

    elapsed = round((time.time() - t0_total) * 1000)

    return {
        "ok": ok_list,
        "degraded": degraded_list,
        "unavailable": unavailable_list,
        "by_tier": by_tier,
        "summary": {
            "total": len(ok_list) + len(degraded_list) + len(unavailable_list),
            "ok_count": len(ok_list),
            "degraded_count": len(degraded_list),
            "unavailable_count": len(unavailable_list),
        },
        "elapsed_ms": elapsed,
    }


def get_available_engines_by_tier(
    tier: str,
    include_degraded: bool = False,
) -> list[str]:
    """按层级筛选可用引擎名称列表。

    Args:
        tier: 层级名（"T1" / "T2" / "T3"）
        include_degraded: 是否包含降级但仍可用的引擎

    Returns:
        引擎名称列表（仅为 ok 状态，若 include_degraded 则包含 degraded）
    """
    registry = _load_registry()
    names: list[str] = []

    for engine in registry:
        if engine.get("tier") != tier:
            continue
        status = engine.get("status", "unknown")
        if status == "ok":
            names.append(engine.get("name", ""))
        elif include_degraded and status in ("degraded", "slow"):
            names.append(engine.get("name", ""))

    return names


# ── CLI 调试用 ─────────────────────────────────────────────────────────────────
def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="引擎健康检查")
    parser.add_argument("--tier", "-t", choices=["T1", "T2", "T3"],
                        help="仅检查指定层级")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--fast", action="store_true", help="快速模式（仅检查推荐引擎）")
    parser.add_argument("--list", action="store_true", help="列出注册表所有引擎（不探测）")
    args = parser.parse_args()

    if args.list:
        registry = _load_registry()
        if args.tier:
            registry = [e for e in registry if e.get("tier") == args.tier]
        for e in registry:
            print(f"[{e.get('tier', '?')}] {e.get('name', '?')} "
                  f"({e.get('status', '?')}) — {e.get('desc', '')}")
        return

    tiers = [args.tier] if args.tier else None
    result = health_check_all(timeout=5, tiers=tiers, fast=args.fast)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"健康检查完成 — 耗时 {result['elapsed_ms']}ms")
        summary = result["summary"]
        print(f"  可用: {summary['ok_count']}  降级: {summary['degraded_count']}  "
              f"不可用: {summary['unavailable_count']}  总计: {summary['total']}")
        print()
        if result["ok"]:
            print("✅ 可用:")
            for e in result["ok"]:
                print(f"  [{e['tier']}] {e['engine']} ({e['latency_ms']}ms)")
        if result["degraded"]:
            print("⚠️  降级:")
            for e in result["degraded"]:
                print(f"  [{e['tier']}] {e['engine']} — {e.get('error', '')}")
        if result["unavailable"]:
            print("❌ 不可用:")
            for e in result["unavailable"]:
                print(f"  [{e['tier']}] {e['engine']} — {e.get('error', '')}")


if __name__ == "__main__":
    _cli()
