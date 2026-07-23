#!/usr/bin/env python3
"""search_v3.py — local-search 主入口 v3

职责：
  - 解析命令行参数（兼容 unified-search CLI 调用）
  - 通过 smart_router 选择本地引擎组合
  - 通过 health_check 过滤不可用引擎（TTL 5min）
  - 并行抓取，解析 HTML/RSS/JSON/XML
  - 复用 unified-search/scripts/cache.py 的 L1/L2 缓存
  - 输出 strict unified-search schema
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# 将 unified-search/scripts 加入路径，以复用 cache.py
SKILL_DIR = Path(__file__).resolve().parent
UNIFIED_SCRIPT_DIR = SKILL_DIR.parent.parent / "scripts"
# 确保当前目录优先于 scripts 目录，避免 health_check 等同名模块冲突
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))
if UNIFIED_SCRIPT_DIR.exists() and str(UNIFIED_SCRIPT_DIR) not in sys.path:
    sys.path.insert(1, str(UNIFIED_SCRIPT_DIR))

try:
    from cache import SearchCache
except ImportError:
    SearchCache = None  # type: ignore

from engine_registry import EngineRegistry, get_registry
from health_check import get_available_engines
from smart_router import route_query

logger = logging.getLogger("local_search.search_v3")
if not logger.handlers:
    logger.setLevel(logging.WARNING)
    logger.addHandler(logging.StreamHandler(sys.stderr))

CONFIG_PATH = SKILL_DIR / "config.yaml"
PARSE_MAPS_PATH = SKILL_DIR / "parse_maps.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning(f"加载 YAML 失败 {path}: {e}")
        return {}


@functools.lru_cache(maxsize=1)
def _load_config() -> dict[str, Any]:
    return _load_yaml(CONFIG_PATH)


@functools.lru_cache(maxsize=1)
def _load_parse_maps() -> dict[str, Any]:
    return _load_yaml(PARSE_MAPS_PATH)


_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _resolve(template: str | list[str], query: str, n: int, **extra: Any) -> str | list[str]:
    if isinstance(template, list):
        return [_resolve(item, query, n, **extra) for item in template]  # type: ignore
    s = str(template).replace("{query}", query).replace("{n}", str(n))
    for k, v in extra.items():
        s = s.replace(f"{{{k}}}", str(v))
    return s


def _fetch(url: str, method: str = "GET", data: bytes | None = None,
           headers: dict[str, str] | None = None, timeout: float = 8,
           user_agent: str = "") -> str:
    req_headers = dict(_HEADERS)
    if headers:
        req_headers.update(headers)
    if user_agent:
        req_headers["User-Agent"] = user_agent
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if raw.startswith(b"\x1f\x8b"):
                import gzip
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        logger.warning(f"HTTP {e.code} for {url}")
    except urllib.error.URLError as e:
        logger.warning(f"URL error for {url}: {e.reason}")
    except Exception as e:
        logger.warning(f"Fetch error for {url}: {e}")
    return ""


def _build_url(spec: dict[str, Any], query: str, n: int) -> str:
    url = _resolve(spec["url"], query, n)
    qp = spec.get("query_param", "q")
    extra = spec.get("extra_params", {})
    params = {qp: query}
    for k, v in extra.items():
        params[k] = _resolve(str(v), query, n)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{urllib.parse.urlencode(params)}"


# ── HTML 解析 ──────────────────────────────────────────────────────────────────

def _select_with_bs4(soup: Any, selector: str):
    try:
        return soup.select(selector)
    except Exception:
        return []


def _select_first_with_bs4(soup: Any, selector: str) -> Any:
    items = _select_with_bs4(soup, selector)
    return items[0] if items else None


def _parse_html(engine_name: str, html: str, spec: dict[str, Any],
                maps: dict[str, Any]) -> list[dict[str, Any]]:
    html_maps = maps.get("html", {})
    mapping = html_maps.get(engine_name, html_maps.get("default", {}))
    container_sel = mapping.get("container")
    title_sel = mapping.get("title")
    url_sel = mapping.get("url")
    snippet_sel = mapping.get("snippet")
    url_attr = mapping.get("url_attr", "href")
    default_score = mapping.get("score", 0.5)

    if not container_sel:
        return []

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        containers = _select_with_bs4(soup, container_sel)
    except Exception:
        return []

    results: list[dict[str, Any]] = []
    base = spec.get("_base", spec.get("url", ""))
    for idx, item in enumerate(containers):
        try:
            title_el = _select_first_with_bs4(item, title_sel) if title_sel else None
            url_el = _select_first_with_bs4(item, url_sel) if url_sel else None
            snippet_el = _select_first_with_bs4(item, snippet_sel) if snippet_sel else None

            title = title_el.get_text(strip=True)[:200] if title_el else ""
            url = ""
            if url_el and url_el.has_attr(url_attr):
                url = url_el[url_attr]
            snippet = snippet_el.get_text(strip=True)[:300] if snippet_el else ""

            if not title and not url:
                continue

            if url and url.startswith("/"):
                url = urllib.parse.urljoin(base, url)

            score = max(default_score - idx * 0.05, 0.1)
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "source": engine_name,
            })
        except Exception:
            continue
    return results


# ── XML / RSS 解析 ─────────────────────────────────────────────────────────────

def _parse_xml(engine_name: str, text: str, maps: dict[str, Any],
               is_rss: bool = False) -> list[dict[str, Any]]:
    if is_rss:
        mapping = maps.get("rss", {}).get("default", {})
    else:
        mapping = maps.get("xml", {}).get(engine_name, {})

    entry_path = mapping.get("entry_path") or mapping.get("item_path", ".//item")
    title_tag = mapping.get("title", "title")
    url_tag = mapping.get("url", "link")
    snippet_tag = mapping.get("snippet", "description")
    namespaces = mapping.get("namespaces", {})

    results: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return results

    if entry_path.startswith(".//{"):
        entries = root.findall(entry_path, namespaces)
    else:
        entries = root.findall(entry_path)

    for idx, entry in enumerate(entries):
        try:
            title = url = snippet = ""
            if title_tag.startswith("atom:"):
                tag = title_tag.split(":")[1]
                ns = namespaces.get("atom")
                node = entry.find(f"{{{ns}}}{tag}") if ns else None
                title = (node.text or "").strip() if node is not None else ""
            else:
                title = (entry.findtext(title_tag, default="")).strip()

            if url_tag.startswith("atom:"):
                tag = url_tag.split(":")[1]
                ns = namespaces.get("atom")
                node = entry.find(f"{{{ns}}}{tag}") if ns else None
                url = (node.text or "").strip() if node is not None else ""
            else:
                url = (entry.findtext(url_tag, default="")).strip()

            if snippet_tag.startswith("atom:"):
                tag = snippet_tag.split(":")[1]
                ns = namespaces.get("atom")
                node = entry.find(f"{{{ns}}}{tag}") if ns else None
                snippet = (node.text or "").strip() if node is not None else ""
            else:
                snippet = (entry.findtext(snippet_tag, default="")).strip()

            title = re.sub(r"\s+", " ", title)[:200]
            snippet = re.sub(r"\s+", " ", snippet)[:300]
            score = max(0.7 - idx * 0.05, 0.1)
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet,
                "score": round(score, 3),
                "source": engine_name,
            })
        except Exception:
            continue
    return results


# ── JSON 解析 ──────────────────────────────────────────────────────────────────

def _get_path(data: Any, path: str) -> Any:
    if path == ".":
        return data
    obj = data
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _format_url(template: str | None, item: dict[str, Any], default: str = "") -> str:
    if not template:
        return default
    try:
        return template.format(**item)
    except (KeyError, IndexError):
        return default


def _parse_json(engine_name: str, text: str, maps: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = maps.get("json", {}).get(engine_name, {})
    items_path = mapping.get("items", ".")
    title_key = mapping.get("title")
    url_key = mapping.get("url")
    snippet_key = mapping.get("snippet")
    url_template = mapping.get("url_template")

    results: list[dict[str, Any]] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return results

    items = _get_path(data, items_path)
    if not isinstance(items, list):
        return results

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            if engine_name == "local_pubmed" and isinstance(item, str):
                item = {"pmid": item}
            else:
                continue

        title = url = snippet = ""
        if title_key:
            raw = _get_path(item, title_key)
            if isinstance(raw, list):
                raw = raw[0] if raw else ""
            title = str(raw or "")[:200]
        if url_key:
            raw = _get_path(item, url_key)
            url = str(raw or "")[:500]
        elif url_template:
            url = _format_url(url_template, item)
        if snippet_key:
            raw = _get_path(item, snippet_key)
            snippet = str(raw or "")[:300]

        title = re.sub(r"<[^>]+>", " ", title)
        title = re.sub(r"\s+", " ", title).strip()
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()

        score = max(0.7 - idx * 0.05, 0.1)
        results.append({
            "title": title,
            "url": url,
            "snippet": snippet,
            "score": round(score, 3),
            "source": engine_name,
        })
    return results


# ── 单个引擎执行 ─────────────────────────────────────────────────────────────────

def _search_one(engine_name: str, query: str, n: int = 5,
                timeout: float | None = None) -> tuple[list[dict[str, Any]], str]:
    cfg = _load_config()
    maps = _load_parse_maps()
    settings = cfg.get("settings", {})
    engines = cfg.get("engines", {})
    spec = engines.get(engine_name, {})
    if not spec:
        return [], f"未找到引擎配置: {engine_name}"
    if not spec.get("enabled", True):
        return [], f"引擎已禁用: {engine_name}"

    to = timeout or spec.get("timeout") or settings.get("default_timeout", 8)
    user_agent = settings.get("user_agent", "")
    fmt = spec.get("format", "html")
    method = spec.get("method", "GET")
    headers = spec.get("headers", {})

    url = _build_url(spec, query, n)
    spec["_base"] = spec.get("url", "")

    t0 = time.time()
    try:
        text = _fetch(url, method=method, headers=headers, timeout=to, user_agent=user_agent)
    except Exception as e:
        return [], f"{engine_name} 请求异常: {e}"
    elapsed = round((time.time() - t0) * 1000, 2)

    if not text:
        return [], f"{engine_name} 返回空内容"

    if fmt == "html":
        results = _parse_html(engine_name, text, spec, maps)
    elif fmt == "xml":
        results = _parse_xml(engine_name, text, maps, is_rss=False)
    elif fmt == "rss":
        results = _parse_xml(engine_name, text, maps, is_rss=True)
    elif fmt == "json":
        results = _parse_json(engine_name, text, maps)
    else:
        results = []

    for r in results:
        r["_engine"] = engine_name
        r["_elapsed"] = elapsed
    return results[:n], ""


# ── 缓存 key ───────────────────────────────────────────────────────────────────

def _cache_key(engines: list[str]) -> str:
    return "local_search+" + "+".join(sorted(engines)) if engines else "local_search"


def _cache_domain(domain: str | None) -> str:
    return domain or "local_general"


# ── 批量执行 ─────────────────────────────────────────────────────────────────────

def search_engines(
    query: str,
    engines: list[str] | None = None,
    n: int = 5,
    timeout: float | None = None,
    max_parallel: int = 5,
    skip_cache: bool = False,
    registry: EngineRegistry | None = None,
    mode: str = "fast",
) -> dict[str, Any]:
    """local-search 主入口：批量调用本地引擎，返回 unified-search schema。"""
    reg = registry or get_registry()
    cfg = _load_config()
    settings = cfg.get("settings", {})
    max_parallel = max_parallel or settings.get("max_parallel_engines", 5)

    # 自动路由
    if not engines:
        decision = route_query(query, registry=reg, max_engines=3, require_available=False)
        engines = decision["engines"]
        domain = decision.get("domain")
    else:
        domain = None

    # 健康过滤（fast/budget 模式下更严格，只检查实际要用的引擎）
    if mode in ("fast", "budget"):
        try:
            available = set(get_available_engines(registry=reg, engine_names=engines))
            engines = [e for e in engines if e in available]
        except Exception as e:
            logger.warning(f"可用性检查失败: {e}")

    if not engines:
        # 全部不可用，回退到启用的引擎
        engines = reg.list_engines(enabled_only=True)[:3]

    # 缓存读取
    cache = SearchCache() if SearchCache is not None else None
    cache_key = _cache_key(engines)
    cache_domain = _cache_domain(domain)
    if not skip_cache and cache is not None:
        hit = cache.get(query, cache_key, n, domain=cache_domain)
        if hit:
            return {
                "query": query,
                "engine": engines[0] if engines else "local_search",
                "engines": engines,
                "engines_combo": engines,
                "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": cache_domain,
                "elapsed_ms": 0,
                "tfidf_scores": [],
                "results": hit.get("results", []),
                "count": len(hit.get("results", [])),
                "engines_used": engines,
                "errors": [],
                "mode": mode,
            }

    t0_all = time.time()
    all_results: list[dict[str, Any]] = []
    engines_used: list[str] = []
    errors: list[str] = []

    def _task(name: str) -> tuple[str, list[dict[str, Any]], str]:
        res, err = _search_one(name, query, n=n, timeout=timeout)
        return name, res, err

    with ThreadPoolExecutor(max_workers=min(len(engines), max_parallel)) as ex:
        futures = {ex.submit(_task, name): name for name in engines}
        for fut in as_completed(futures, timeout=timeout or 30):
            name = futures[fut]
            try:
                _, res, err = fut.result()
                if res:
                    all_results.extend(res)
                    engines_used.append(name)
                if err:
                    errors.append(err)
            except Exception as e:
                errors.append(f"{name}: {e}")

    all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
    elapsed = int((time.time() - t0_all) * 1000)
    final_results = all_results[: n * len(engines)] if engines else all_results[:n]

    payload = {
        "results": final_results,
        "engines_used": engines_used,
    }

    # 写缓存
    if not skip_cache and cache is not None:
        cache.set(query, cache_key, n, payload, domain=cache_domain)

    return {
        "query": query,
        "engine": engines[0] if engines else "local_search",
        "engines": engines,
        "engines_combo": engines,
        "cached": False,
        "cache_level": None,
        "domain": cache_domain,
        "elapsed_ms": elapsed,
        "tfidf_scores": [],
        "results": final_results,
        "count": len(final_results),
        "engines_used": engines_used,
        "errors": errors,
        "mode": mode,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _parse_engine_list(value: str) -> list[str]:
    return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]


def main():
    parser = argparse.ArgumentParser(description="local-search v3 子技能入口")
    parser.add_argument("query", nargs="?", help="搜索关键词")
    parser.add_argument("--engine", "-e", default="", help="引擎名，多个用逗号分隔")
    parser.add_argument("--n", type=int, default=5, help="每引擎结果数")
    parser.add_argument("--timeout", "-t", type=float, default=None, help="超时秒数")
    parser.add_argument("--max-parallel", type=int, default=5)
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存")
    parser.add_argument("--mode", default="fast", choices=["fast", "auto", "deep", "budget"],
                        help="unified-search 模式透传")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if not args.query:
        parser.error("必须提供搜索关键词")

    engines = _parse_engine_list(args.engine) if args.engine else None
    result = search_engines(
        args.query,
        engines=engines,
        n=args.n,
        timeout=args.timeout,
        max_parallel=args.max_parallel,
        skip_cache=args.no_cache,
        mode=args.mode,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
