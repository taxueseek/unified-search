#!/usr/bin/env python3
"""engines.py — Unified Search v2 引擎适配层（精简版，< 400 行）

配置驱动 + 声明式 output_map 字段提取 + 通用 parser 兜底。
支持 cli / http(GET/POST) 类型，所有异常吞没返回 []。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    from config import load_config, get_engines
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines

logger = logging.getLogger("unified_search.engines")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))


def safe_search(fn: Callable) -> Callable:
    """统一错误处理装饰器 — 所有异常返回 []。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
        name = fn.__name__.replace("_engine", "").strip("_")
        try:
            return fn(*args, **kwargs)
        except subprocess.TimeoutExpired:
            logger.warning(f"引擎 {name} 超时")
        except FileNotFoundError as e:
            logger.warning(f"引擎 {name} 命令不存在: {e}")
        except Exception as e:
            logger.error(f"引擎 {name} 异常: {type(e).__name__}: {e}", exc_info=True)
        return []
    return wrapper


def _run(cmd: list[str], timeout: float = 8, engine_name: str = "?") -> str:
    """执行命令，超时/异常不抛。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
        tail = (r.stderr or "").strip()[:200]
        logger.warning(f"引擎 {engine_name} 失败 (rc={r.returncode}): {tail}")
        return r.stdout if r.stdout.strip() else ""
    except subprocess.TimeoutExpired:
        logger.warning(f"引擎 {engine_name} 超时 (>{timeout}s)")
    except FileNotFoundError as e:
        logger.error(f"引擎 {engine_name} CLI 缺失: {e}")
    except Exception as e:
        logger.error(f"引擎 {engine_name} 异常: {type(e).__name__}: {e}")
    return ""


def _resolve(template: list[str] | str, query: str, n: int, **extra: Any) -> list[str] | str:
    """替换模板占位符。"""
    if isinstance(template, list):
        return [_resolve(item, query, n, **extra) for item in template]
    s = template.replace("{query}", query).replace("{n}", str(n))
    s = s.replace("{TIMESTAMP}", str(int(time.time())))
    for key, val in extra.items():
        s = s.replace(f"{{{key}}}", str(val))
    if s.startswith("~"):
        s = str(Path.home() / s[1:])
    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", lambda m: os.environ.get(m.group(1), m.group(0)), s)


def _extract_items(data: Any, path: str) -> list[dict]:
    """从嵌套 dict 按路径提取列表。"""
    obj = data
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part, [])
        else:
            return []
    return obj if isinstance(obj, list) else []


def _make_field_parser(path: str, fields: dict[str, str]) -> Callable:
    """构造声明式 parser。"""
    def parser(data: Any) -> list[dict[str, Any]]:
        items = _extract_items(data, path) if isinstance(data, dict) else []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            r = {ok: iv[:300] if ok == "snippet" and isinstance(iv, str) else iv
                 for ok, ik in fields.items() if (iv := item.get(ik, ""))}
            if r.get("title") or r.get("url"):
                results.append(r)
        return results[:10]
    return parser


def _build_cli_engine(spec: dict[str, Any]) -> Any:
    cmd_template = spec.get("cmd", [])
    search_args = spec.get("search_args", [])
    env_overrides = spec.get("env", {})

    @safe_search
    def _engine(query: str, n: int = 5, timeout: float = 8, mode: str = "fast", **kwargs) -> list[dict[str, Any]]:
        cmd = _resolve(cmd_template, query, n, mode=mode)
        args = _resolve(search_args, query, n, mode=mode)
        if not cmd:
            return []
        env = os.environ.copy()
        env.update(env_overrides)
        return _parse_text_output(_run(cmd + args, timeout=timeout, engine_name=spec.get("_name", "cli")),
                                  spec.get("_name", "cli"))
    return _engine


def _build_http_engine(spec: dict[str, Any]) -> Any:
    """统一 HTTP 引擎构造（GET/POST）。"""
    url_template = spec.get("url", "")
    headers = spec.get("headers", {"Content-Type": "application/json"})
    query_param = spec.get("query_param", "q")
    fmt = spec.get("format", "")
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    output_map = spec.get("output_map", {})
    is_get = spec.get("method", "GET") == "GET"
    body_template = spec.get("body", {})

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        import urllib.parse as up

        if is_get:
            resolved_url = _resolve(url_template, query, n)
            separator = "&" if "?" in resolved_url else "?"
            full_url = f"{resolved_url}{separator}{query_param}={up.quote(query)}"
            if fmt:
                full_url += f"&format={fmt}"
            for k, v in extra_params.items():
                full_url += f"&{k}={up.quote(_resolve(str(v), query, n))}"
            req = urllib.request.Request(full_url, headers={k: _resolve(v, query, n) for k, v in headers.items()})
        else:
            body: dict[str, Any] = {}
            for k, v in body_template.items():
                resolved = _resolve(str(v), query, n)
                if k == "search_depth":
                    body[k] = depth
                elif resolved.lower() == "true":
                    body[k] = True
                elif resolved.lower() == "false":
                    body[k] = False
                else:
                    try:
                        body[k] = int(resolved)
                    except ValueError:
                        try:
                            body[k] = float(resolved)
                        except ValueError:
                            body[k] = resolved
            req = urllib.request.Request(url_template, data=json.dumps(body).encode("utf-8"),
                                         headers={k: _resolve(v, query, n) for k, v in headers.items()})

        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                raw = resp.read().decode("utf-8")
                if fmt == "xml":
                    return _parse_xml(raw, spec.get("_name", ""))
                data = json.loads(raw)
                if output_map:
                    return _make_field_parser(output_map.get("items", ""), {
                        "title": output_map.get("item_title", "title"),
                        "url": output_map.get("item_url", "url"),
                        "snippet": output_map.get("item_summary", "snippet"),
                        "source": output_map.get("item_source", "source"),
                    })(data)
                return _parse_generic(data, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"HTTP 引擎失败: {e}")
            return []
    return _engine


_BUILDERS = {"cli": _build_cli_engine, "http": _build_http_engine}


# ── 通用解析器 ─────────────────────────────────────────────────────────────────

def _parse_text_output(text: str, engine_name: str) -> list[dict[str, Any]]:
    """通用 CLI 文本解析：优先 JSON，其次结构化文本。"""
    try:
        data = json.loads(text.strip())
        if isinstance(data, list):
            return [{"title": i.get("title", ""), "url": i.get("url", ""),
                     "snippet": i.get("snippet", i.get("content", ""))[:300],
                     "source": engine_name} for i in data if isinstance(i, dict)]
        if isinstance(data, dict):
            items = data.get("results", data.get("items", data.get("data", [])))
            if isinstance(items, list):
                return [{"title": i.get("title", ""), "url": i.get("url", ""),
                         "snippet": i.get("snippet", i.get("content", ""))[:300],
                         "source": engine_name} for i in items if isinstance(i, dict)]
    except (json.JSONDecodeError, ValueError):
        pass

    results, cur = [], {}
    seen_url = False
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith("### "):
            if cur:
                results.append(cur)
            cur = {"title": re.sub(r'^\d+\.\s*', '', s[4:].strip()), "source": engine_name,
                   "score": max(1.0 - len(results) * 0.1, 0.1)}
            seen_url = False
        elif s.startswith("- **URL**: ") and cur:
            cur["url"] = s[11:].strip()
            seen_url = True
        elif s.startswith("- ") and not s.startswith("- **") and seen_url and cur:
            cur["snippet"] = " ".join(s[2:].strip().split())[:300]
            seen_url = False
    if cur:
        results.append(cur)
    return results[:10]


def _parse_xml(text: str, engine_name: str) -> list[dict[str, Any]]:
    """解析 Atom XML（arXiv 等）。"""
    import xml.etree.ElementTree as ET
    results = []
    try:
        root = ET.fromstring(text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall(".//atom:entry", ns) or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for entry in entries:
            title = entry.findtext("atom:title", "", ns).strip().replace("\n", " ")[:200]
            summary = entry.findtext("atom:summary", "", ns).strip().replace("\n", " ")[:300]
            entry_id = entry.findtext("atom:id", "", ns)
            url = entry_id
            for link in entry.findall("atom:link", ns):
                if link.get("title") == "pdf":
                    url = link.get("href", url)
                    break
            if title:
                results.append({"title": title, "url": url, "snippet": summary, "source": engine_name})
    except ET.ParseError:
        pass
    return results


def _parse_generic(data: dict[str, Any], engine_name: str = "?") -> list[dict[str, Any]]:
    """通用 JSON 解析：自动探测常见字段。"""
    items = None
    for key in ["results", "items", "data", "works", "search"]:
        if "." in key:
            parts = key.split(".")
            obj = data
            for p in parts:
                obj = obj.get(p, {}) if isinstance(obj, dict) else {}
            if isinstance(obj, list):
                items = obj
                break
        elif isinstance(data, dict) and key in data and isinstance(data[key], list):
            items = data[key]
            break

    if items is None and isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                for key in ["results", "items", "value", "search"]:
                    if key in v and isinstance(v[key], list):
                        items = v[key]
                        break
            if items:
                break

    if not items or not isinstance(items, list):
        return []

    results = []
    for i in items:
        if not isinstance(i, dict):
            continue
        title = i.get("title", "")
        if isinstance(title, list):
            title = title[0] if title else ""
        url = i.get("url", i.get("URL", i.get("html_url", "")))
        snippet = (i.get("snippet", i.get("content", i.get("summary", i.get("description", "")))))[:300]
        score = i.get("score", i.get("relevance_score", 0.5))
        results.append({"title": str(title)[:200], "url": str(url),
                        "snippet": str(snippet), "score": score, "source": engine_name})
    return results[:10]


# ── 引擎注册表 ─────────────────────────────────────────────────────────────────

_engine_registry: dict[str, Any] = {}
_engine_registry_loaded = False


def _load_registry():
    global _engine_registry, _engine_registry_loaded
    if _engine_registry_loaded:
        return
    cfg = load_config()
    engines = get_engines(cfg)
    registry = {}
    for name, spec in engines.items():
        spec = dict(spec)
        spec["_name"] = name
        builder = _BUILDERS.get(spec.get("type", "cli"))
        if builder:
            registry[name] = builder(spec)
        else:
            logger.warning(f"未知引擎类型: {spec.get('type')} (引擎 {name})")
    _engine_registry = registry
    _engine_registry_loaded = True


def get_registry() -> dict[str, Any]:
    _load_registry()
    return _engine_registry


def available_engines() -> list[str]:
    return sorted(get_registry().keys())


def search(query: str, engine: str, n: int = 5, timeout: float = 8, depth: str = "fast", mode: str = "fast") -> list[dict[str, Any]]:
    """统一引擎调用入口；失败返回空 list，不抛异常。"""
    registry = get_registry()
    fn = registry.get(engine)
    if not fn:
        logger.warning(f"未知引擎: {engine}")
        return []
    t0 = time.time()
    try:
        results = fn(query, n, timeout, depth=depth, mode=mode)
    except TypeError:
        try:
            results = fn(query, n, timeout)
        except Exception as e:
            logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
            results = []
    except Exception as e:
        logger.error(f"引擎 {engine} 异常: {type(e).__name__}: {e}")
        results = []
    elapsed = time.time() - t0
    if results and isinstance(results, list):
        for r in results:
            if isinstance(r, dict) and "error" not in r:
                r["_engine"] = engine
                r["_elapsed"] = round(elapsed, 3)
    return results if isinstance(results, list) else []


def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="引擎适配层调试")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="anysearch")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--timeout", "-t", type=float, default=8)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return
    if not args.query:
        parser.error("必须提供 query")
    print(json.dumps(search(args.query, args.engine, args.n, args.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
