#!/usr/bin/env python3
"""engines.py — Unified Search v2 引擎适配层

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
    """统一错误处理装饰器。所有异常返回 []，细粒度异常先于通用 Exception 匹配。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs) -> list[dict[str, Any]]:
        name = fn.__name__.replace("_engine", "").strip("_")
        try:
            return fn(*args, **kwargs)
        except subprocess.TimeoutExpired:
            logger.warning(f"引擎 {name} 超时")
        except FileNotFoundError as e:
            logger.warning(f"引擎 {name} 命令不存在: {e}")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            logger.warning(f"引擎 {name} HTTP 错误: {e}")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"引擎 {name} 解析错误: {e}")
        except Exception as e:
            logger.error(f"引擎 {name} 未预期异常: {type(e).__name__}: {e}", exc_info=True)
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
    """从嵌套 dict 按路径提取列表。data 为 None 或非 dict 时返回 []。"""
    if not isinstance(data, dict):
        return []
    obj = data
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part, [])
        else:
            return []
    return obj if isinstance(obj, list) else []


def _make_field_parser(path: str, fields: dict[str, str], url_template: str | None = None) -> Callable:
    """构造声明式 parser。url_template 支持从字段值构造 URL（如 Wikipedia）。"""
    def parser(data: Any) -> list[dict[str, Any]]:
        items = _extract_items(data, path) if isinstance(data, dict) else []
        results = []
        for item in items:
            if not isinstance(item, dict):
                continue
            r = {ok: iv[:300] if ok == "snippet" and isinstance(iv, str) else iv
                 for ok, ik in fields.items() if (iv := item.get(ik, ""))}
            # url_template 支持
            if url_template and not r.get("url"):
                try:
                    r["url"] = url_template.format(**item)
                except (KeyError, ValueError):
                    pass
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
                    }, url_template=output_map.get("url_template"))(data)
                return _parse_generic(data, spec.get("_name", ""))
        except Exception as e:
            logger.warning(f"HTTP 引擎失败: {e}")
            return []
    return _engine


# ── HTML 网页解析引擎 ─────────────────────────────────────────────────────────

def _load_parse_maps() -> dict:
    """加载 parse_maps.yaml（声明式 CSS 选择器映射）。"""
    maps_path = Path(__file__).parent.parent / "sub-skills" / "local-search" / "parse_maps.yaml"
    if not maps_path.exists():
        return {}
    try:
        import yaml
        with open(maps_path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _detect_anti_bot(html: str) -> bool:
    """检测反爬/拦截页面。只检查关键区域，避免正文误判。"""
    if not html:
        return True
    if len(html.strip()) < 500:
        return True
    # 只在前 2000 字符（head 区域）检测反爬标记
    head_section = html[:2000].lower()
    anti_bot_head = [
        "captcha", "challenge", "cf-browser-verification",
        "access denied", "rate limit", "too many requests",
        "checking your browser", "ddos-guard", "perimeterx",
    ]
    for marker in anti_bot_head:
        if marker in head_section:
            return True
    # 如果页面有大量链接且内容充实，判定为正常结果页
    if len(html) > 50000:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            if len(soup.find_all("a")) > 20:
                return False
        except Exception:
            pass
    return False


def _build_html_engine(spec: dict[str, Any]) -> Any:
    """HTML 网页解析引擎：HTTP 抓取 + BeautifulSoup CSS 选择器解析。"""
    url_template = spec.get("url", "")
    # 注意：不设置 Accept-Encoding，让 urllib 自动处理 gzip/deflate
    # 设 Accept-Encoding: br 会导致收到 Brotli 压缩但 urllib 无法解压
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    }
    # 覆盖自定义 headers
    headers.update(spec.get("headers", {}))
    query_param = spec.get("query_param", "q")
    timeout = spec.get("timeout", 8)
    extra_params = spec.get("extra_params", {})
    engine_name = spec.get("_name", "html")
    _parse_maps_cache: dict = {}

    def _get_parse_maps() -> dict:
        if not _parse_maps_cache:
            _parse_maps_cache.update(_load_parse_maps())
        return _parse_maps_cache

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        resolved_url = _resolve(url_template, query, n)
        separator = "&" if "?" in resolved_url else "?"
        full_url = f"{resolved_url}{separator}{query_param}={up.quote(query)}"
        for k, v in extra_params.items():
            full_url += f"&{k}={up.quote(_resolve(str(v), query, n))}"
        try:
            req = urllib.request.Request(full_url, headers={k: _resolve(v, query, n) for k, v in headers.items()})
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception:
            return []
        if _detect_anti_bot(html):
            return []
        maps = _get_parse_maps()
        html_maps = maps.get("html", {})
        mapping = html_maps.get(engine_name, html_maps.get("default", {}))
        container_sel = mapping.get("container")
        title_sel = mapping.get("title", "h2 a, h3 a")
        url_sel = mapping.get("url", "a")
        snippet_sel = mapping.get("snippet")
        url_attr = mapping.get("url_attr", "href")
        default_score = mapping.get("score", 0.7)
        if not container_sel:
            return []
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            containers = soup.select(container_sel)
        except Exception:
            return []
        results = []
        for idx, item in enumerate(containers[:n * 2]):
            try:
                title_el = item.select_one(title_sel) if title_sel else None
                url_el = item.select_one(url_sel) if url_sel else None
                snippet_el = item.select_one(snippet_sel) if snippet_sel else None
                title = title_el.get_text(strip=True)[:200] if title_el else ""
                url = ""
                if url_el and url_el.has_attr(url_attr):
                    url = url_el[url_attr]
                snippet = snippet_el.get_text(strip=True)[:300] if snippet_el else ""
                if not title and not url:
                    continue
                if url and url.startswith("/"):
                    from urllib.parse import urljoin
                    url = urljoin(resolved_url, url)
                score = max(default_score - idx * 0.05, 0.1)
                results.append({"title": title, "url": url, "snippet": snippet, "score": round(score, 3), "source": engine_name})
            except Exception:
                continue
        return results[:n]
    return _engine


# ── Exa 专用引擎 ──────────────────────────────────────────────────────────────

def _build_exa_engine(spec: dict[str, Any]) -> Any:
    """Exa 语义搜索专用引擎（embedding 匹配 + 内容摘要）"""
    timeout = spec.get("timeout", 15)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, depth: str = "fast", **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        api_key = os.environ.get("EXA_API_KEY", "")
        if not api_key:
            logger.warning("EXA_API_KEY 未设置")
            return []
        url = "https://api.exa.ai/search"
        body = json.dumps({
            "query": query,
            "type": "auto",
            "numResults": min(n, 10),
            "contents": {"text": {"maxCharacters": 400}},
        }).encode("utf-8")
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                results = []
                for r in data.get("results", []):
                    results.append({
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("text", "")[:300] if r.get("text") else r.get("snippet", ""),
                        "source": "exa",
                        "score": r.get("score", 0.0),
                    })
                return results
        except Exception as e:
            logger.warning(f"Exa 引擎失败: {e}")
            return []
    return _engine


# ── 搜狗微信搜索引擎 ─────────────────────────────────────────────────────────

def _build_wechat_sogou_engine(spec: dict[str, Any]) -> Any:
    """搜狗微信搜索引擎（weixin.sogou.com）

    抓取搜狗微信搜索结果页，提取公众号文章标题、链接、摘要、公众号名。
    无需登录，无需 API key，纯 HTML 解析。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://weixin.sogou.com/weixin?type=2&query={up.quote(query)}&ie=utf8"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            li_pattern = re.compile(
                r'<li\s+id="sogou_vr_11002601_box_\d+"[^>]*>(.*?)</li>', re.DOTALL
            )
            for li in li_pattern.findall(html)[:n]:
                title_match = re.search(
                    r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', li, re.DOTALL
                )
                if not title_match:
                    continue
                href = title_match.group(1).replace("&amp;", "&")
                title = re.sub(r"<[^>]+>", "", title_match.group(2)).strip()
                title = title.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                summary_match = re.search(
                    r'<p[^>]*class="txt-info"[^>]*>(.*?)</p>', li, re.DOTALL
                )
                summary = re.sub(r"<[^>]+>", "", summary_match.group(1)).strip() if summary_match else ""
                summary = summary.replace("<!--red_beg-->", "").replace("<!--red_end-->", "")

                account_match = re.search(
                    r'<span[^>]*class="all-time-y2"[^>]*>(.*?)</span>', li, re.DOTALL
                )
                account = re.sub(r"<[^>]+>", "", account_match.group(1)).strip() if account_match else ""

                results.append({
                    "title": title[:80],
                    "url": "https://weixin.sogou.com" + href if href.startswith("/") else href,
                    "snippet": summary[:200],
                    "account": account,
                    "source": "wechat_sogou",
                })
            return results
        except Exception as e:
            logger.warning(f"搜狗微信搜索失败: {e}")
            return []
    return _engine


# ── Hacker News 搜索引擎 ──────────────────────────────────────────────────────

def _build_hackernews_engine(spec: dict[str, Any]) -> Any:
    """Hacker News 搜索（Algolia API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://hn.algolia.com/api/v1/search?query={up.quote(query)}&tags=story&hitsPerPage={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            results = []
            for h in data.get("hits", []):
                results.append({
                    "title": h.get("title", ""),
                    "url": h.get("url", f"https://news.ycombinator.com/item?id={h.get('objectID', '')}"),
                    "snippet": f"score: {h.get('points', 0)} | comments: {h.get('num_comments', 0)} | by: {h.get('author', '')}",
                    "source": "hackernews",
                })
            return results
        except Exception as e:
            logger.warning(f"HackerNews 引擎失败: {e}")
            return []
    return _engine


# ── Stack Overflow 搜索引擎 ───────────────────────────────────────────────────

def _build_stackoverflow_engine(spec: dict[str, Any]) -> Any:
    """Stack Overflow 搜索（Stack Exchange API）"""
    timeout = spec.get("timeout", 8)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance&q={up.quote(query)}&site=stackoverflow&pagesize={min(n, 10)}"
        headers = {"User-Agent": "argo-search/1.0", "Accept-Encoding": "gzip"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                import gzip
                raw = resp.read()
                try:
                    data = json.loads(gzip.decompress(raw))
                except Exception:
                    data = json.loads(raw)
            results = []
            for item in data.get("items", []):
                tags = ", ".join(item.get("tags", [])[:3])
                results.append({
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "snippet": f"score: {item.get('score', 0)} | answers: {item.get('answer_count', 0)} | tags: {tags}",
                    "source": "stackoverflow",
                })
            return results
        except Exception as e:
            logger.warning(f"StackOverflow 引擎失败: {e}")
            return []
    return _engine


# ── Google Scholar 搜索引擎 ───────────────────────────────────────────────────

def _build_google_scholar_engine(spec: dict[str, Any]) -> Any:
    """Google Scholar 搜索（HTTP 页面解析）"""
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://scholar.google.com/scholar?q={up.quote(query)}&hl=en&as_sdt=0%2C5&num={min(n, 10)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<h3[^>]*class="[^"]*gs_rt[^"]*"[^>]*>(.*?)</h3>', html, re.DOTALL)
            snippets = re.findall(r'<div[^>]*class="[^"]*gs_rs[^"]*"[^>]*>(.*?)</div>', html, re.DOTALL)
            for i, t in enumerate(titles[:n]):
                title = re.sub(r'<[^>]+>', '', t).strip()
                snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip() if i < len(snippets) else ""
                if title:
                    results.append({
                        "title": title[:100],
                        "url": f"https://scholar.google.com/scholar?q={up.quote(title[:50])}",
                        "snippet": snippet[:200],
                        "source": "google_scholar",
                    })
            return results
        except Exception as e:
            logger.warning(f"Google Scholar 引擎失败: {e}")
            return []
    return _engine


# ── V2EX 搜索引擎 ─────────────────────────────────────────────────────────────

def _build_v2ex_engine(spec: dict[str, Any]) -> Any:
    """V2EX 社区搜索"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        to = _timeout or timeout
        url = f"https://www.v2ex.com/search?q={up.quote(query)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                html = resp.read().decode("utf-8")
            results = []
            titles = re.findall(r'<span[^>]*class="[^"]*item_title[^"]*"[^>]*>(.*?)</span>', html, re.DOTALL)
            for t in titles[:n]:
                title = re.sub(r'<[^>]+>', '', t).strip()
                if title:
                    results.append({
                        "title": title[:80],
                        "url": f"https://www.v2ex.com/search?q={up.quote(query)}",
                        "snippet": "V2EX 社区讨论",
                        "source": "v2ex",
                    })
            return results
        except Exception as e:
            logger.warning(f"V2EX 引擎失败: {e}")
            return []
    return _engine


# ── 同花顺热点引擎 ─────────────────────────────────────────────────────────────

def _build_ths_hot_engine(spec: dict[str, Any]) -> Any:
    """同花顺当日强势股 + 题材归因（独家能力）

    不只告诉你"哪些走强"，还告诉你"为什么走强"——同花顺编辑部人工运营的题材标签。
    """
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        to = _timeout or timeout
        from datetime import date as _date
        trade_date = _date.today().strftime("%Y-%m-%d")

        url = (
            f"http://zx.10jqka.com.cn/event/api/getharden/"
            f"date/{trade_date}/orderby/date/orderway/desc/charset/GBK/"
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "Chrome/117.0.0.0 Safari/537.36"
            )
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read())
            if data.get("errocode", 0) != 0:
                return []
            rows = data.get("data") or []
            results = []
            for r in rows[:n]:
                results.append({
                    "title": f"{r.get('name', '')}({r.get('code', '')}) +{r.get('zhangfu', 0)}%",
                    "url": f"https://quote.eastmoney.com/{r.get('code', '')}.html",
                    "snippet": f"题材: {r.get('reason', '未知')} | 换手{r.get('huanshou', 0)}% | 成交额{r.get('chengjiaoe', 0)/1e8:.1f}亿",
                    "source": "ths_hot",
                })
            return results
        except Exception as e:
            logger.warning(f"同花顺热点引擎失败: {e}")
            return []
    return _engine


# ── 财联社电报引擎 ─────────────────────────────────────────────────────────────

def _build_cls_telegraph_engine(spec: dict[str, Any]) -> Any:
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import hashlib
        from datetime import datetime
        to = _timeout or timeout
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
                  "last_time": "", "refresh_type": "1", "rn": str(n)}
        qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("roll_data", []) or []:
                ts = item.get("ctime")
                t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                title = item.get("title", "") or item.get("brief", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("content", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://www.cls.cn/",
                    "snippet": f"{t} | {(item.get('content', '') or item.get('brief', ''))[:150]}",
                    "source": "cls_telegraph",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"财联社电报引擎失败: {e}")
            return []
    return _engine


# ── 东财全球资讯引擎 ─────────────────────────────────────────────────────────

def _build_em_global_news_engine(spec: dict[str, Any]) -> Any:
    """东财全球财经资讯（7×24 滚动）"""
    timeout = spec.get("timeout", 10)

    @safe_search
    def _engine(query: str, n: int = 10, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import uuid
        to = _timeout or timeout
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724",
            "fastColumn": "102", "sortEnd": "",
            "pageSize": str(n * 2),  # 多拉一些用于过滤
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            req = urllib.request.Request(url + "?" + "&".join(f"{k}={v}" for k, v in params.items()), headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", []):
                title = item.get("title", "")
                # 关键词过滤
                if query and query.strip():
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + item.get("summary", "")).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {(item.get('summary', '') or '')[:150]}",
                    "source": "em_global_news",
                })
            return results[:n]
        except Exception as e:
            logger.warning(f"东财全球资讯引擎失败: {e}")
            return []
    return _engine



# ── 东财财经搜索引擎 ─────────────────────────────────────────────────────────

def _build_eastmoney_engine(spec: dict[str, Any]) -> Any:
    """东财经搜搜索（纯 HTTP API，零外部依赖）

    支持：
    - 个股新闻搜索（按股票代码或关键词）
    - 东财全球资讯（7×24 财经快讯）
    """
    timeout = spec.get("timeout", 12)

    @safe_search
    def _engine(query: str, n: int = 5, _timeout: float | None = None, **kwargs) -> list[dict[str, Any]]:
        import urllib.parse as up
        import re
        to = _timeout or timeout

        # 判断是股票代码（6位数字）还是关键词
        is_stock_code = re.match(r'^\d{6}$', query.strip())

        if is_stock_code:
            # 按股票代码搜新闻
            return _eastmoney_stock_news(query.strip(), n, to)
        else:
            # 按关键词搜全球资讯
            return _eastmoney_keyword_news(query, n, to)

    def _eastmoney_stock_news(code: str, n: int, to: float) -> list[dict[str, Any]]:
        """按股票代码搜新闻"""
        import json as _json
        import urllib.parse as up
        cb = "jQuery_news"
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        inner_params = _json.dumps({
            "uid": "", "keyword": code, "type": ["cmsArticleWebOld"],
            "client": "web", "clientType": "web", "clientVersion": "curr",
            "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default",
                      "pageIndex": 1, "pageSize": n, "preTag": "", "postTag": ""}},
        }, separators=(',', ':'))
        params = {"cb": cb, "param": inner_params}
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://so.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={up.quote(str(v))}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                text = resp.read().decode("utf-8")
            json_str = text[text.index("(") + 1:text.rindex(")")]
            d = _json.loads(json_str)
            articles = d.get("result", {}).get("cmsArticleWebOld", []) or []
            results = []
            for a in articles[:n]:
                results.append({
                    "title": re.sub(r'<[^>]+>', '', a.get("title", ""))[:80],
                    "url": a.get("url", ""),
                    "snippet": re.sub(r'<[^>]+>', '', a.get("content", ""))[:200],
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财个股新闻搜索失败: {e}")
            return []

    def _eastmoney_keyword_news(query: str, n: int, to: float) -> list[dict[str, Any]]:
        """按关键词搜全球资讯"""
        import uuid
        url = "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
        params = {
            "client": "web", "biz": "web_724", "fastColumn": "102",
            "sortEnd": "", "pageSize": str(n * 2),
            "req_trace": str(uuid.uuid4()),
        }
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://kuaixun.eastmoney.com/"}
        try:
            full_url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
            req = urllib.request.Request(full_url, headers=headers)
            with urllib.request.urlopen(req, timeout=to) as resp:
                d = json.loads(resp.read())
            results = []
            for item in d.get("data", {}).get("fastNewsList", [])[:n]:
                title = item.get("title", "")
                summary = (item.get("summary", "") or "")[:200]
                if query:
                    keywords = query.strip().split()
                    if not any(kw.lower() in (title + summary).lower() for kw in keywords):
                        continue
                results.append({
                    "title": title[:80],
                    "url": "https://kuaixun.eastmoney.com/",
                    "snippet": f"{item.get('showTime', '')} | {summary}",
                    "source": "eastmoney",
                })
            return results
        except Exception as e:
            logger.warning(f"东财资讯搜索失败: {e}")
            return []

    return _engine


_BUILDERS = {"cli": _build_cli_engine, "http": _build_http_engine, "html": _build_html_engine, "exa": _build_exa_engine, "wechat_sogou": _build_wechat_sogou_engine, "hackernews": _build_hackernews_engine, "stackoverflow": _build_stackoverflow_engine, "google_scholar": _build_google_scholar_engine, "v2ex": _build_v2ex_engine, "ths_hot": _build_ths_hot_engine, "cls_telegraph": _build_cls_telegraph_engine, "em_global_news": _build_em_global_news_engine, "eastmoney": _build_eastmoney_engine}

# ── 通用解析器 ─────────────────────────────────────────────────────────────────

def _parse_text_output(text: str, engine_name: str) -> list[dict[str, Any]]:
    """通用 CLI 文本解析：优先 JSON，其次结构化文本。"""
    if not text or not text.strip():
        return []
    text = text.strip()
    try:
        data = json.loads(text)
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
