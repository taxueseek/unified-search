#!/usr/bin/env python3
"""
fetch_v2.py — 两级抓取：HTTP 优先 + Hound 浏览器降级兜底

第一级：urllib 快速 HTTP（8s 超时）
第二级：Hound (master_fetch) stealthy 浏览器降级
  - 触发条件：HTTP 失败 / 空内容 / Cloudflare 挑战 / JS shell
  - 通过 subprocess 调用，隔离 Patchright 浏览器进程

与 fetch.py 的 fetch_page() 签名兼容，可被 research.py 直接替换。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

USER_AGENT = "argo-fetch-v2/1.0 (+local-research)"
HTTP_TIMEOUT = 8
HOUND_TIMEOUT = 30
MAX_CHARS = 8000

_CF = re.compile(
    r"checking your browser|cf-browser-verification|cf_chl_opt|"
    r"ray id|challenge-platform|please verify you are a human", re.IGNORECASE,
)
_JS = re.compile(
    r"enable javascript|javascript is required|javascript to run this app|"
    r"javascript must be enabled|you need to enable javascript", re.IGNORECASE,
)

_HOUND_WRAPPER = '''\
import asyncio, json, sys
from master_fetch.server import MasterFetchServer

async def main():
    url, max_chars = sys.argv[1], max(int(sys.argv[2]), 500)
    srv = MasterFetchServer()
    result = await srv.smart_fetch(
        url=url, max_content_chars=max_chars, timeout=30000,
        force_fetcher="stealthy", solve_cloudflare=True, cache_ttl=0,
    )
    print(result.model_dump_json())

asyncio.run(main())
'''


class ContentExtractor(HTMLParser):
    """简易 HTML 正文提取器。"""
    _skip = {"script", "style", "nav", "header", "footer", "aside", "noscript"}

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._blocks: list[tuple[float, str]] = []
        self._cur: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._skip:
            self._in_skip += 1

    def handle_endtag(self, tag):
        if tag in self._skip:
            self._in_skip = max(0, self._in_skip - 1)
        if tag in ("p", "div", "article", "section", "li", "h1", "h2", "h3", "td"):
            text = "".join(self._cur).strip()
            if len(text) > 20:
                self._blocks.append((len(text.replace(" ", "")) / max(len(text), 1), text))
            self._cur = []

    def handle_data(self, data):
        if self._in_skip == 0:
            self._cur.append(data)


def http_fetch(url: str, max_chars: int = MAX_CHARS, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    """HTTP 快速抓取。"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "pdf" in ct:
                return _r(url, ok=False, error="PDF not supported")
            charset = "utf-8"
            if "charset=" in ct:
                charset = ct.split("charset=")[-1].strip().split(";")[0]
            raw = resp.read(max(max_chars * 5, 500000))
            html = raw.decode(charset, errors="replace")

        ext = ContentExtractor()
        ext.feed(html)
        ext._blocks.sort(key=lambda x: x[0], reverse=True)
        content = "\n\n".join(t for _, t in ext._blocks[:8])
        return _r(url, content=content[:max_chars], length=len(content), ok=True, html=html)
    except Exception as e:
        return _r(url, ok=False, error=str(e)[:100])


def _needs_hound(result: dict[str, Any]) -> bool:
    """判断是否需要降级到 Hound。"""
    if not result.get("success"):
        return True
    content = result.get("content", "")
    html = result.get("html", "")
    if not content or len(content.strip()) < 50:
        return True
    if _CF.search(html) or _CF.search(content):
        return True
    if len(content) < 200 and _JS.search(html):
        return True
    return False


def _hound_script() -> str:
    """获取/缓存 Hound 子进程包装脚本路径。"""
    d = os.path.join(tempfile.gettempdir(), "argo_fetch_v2")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "hound_wrapper.py")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(_HOUND_WRAPPER)
    return p


def hound_fetch(url: str, max_chars: int = MAX_CHARS, timeout: int = HOUND_TIMEOUT) -> dict[str, Any]:
    """通过 subprocess 调用 Hound stealthy 浏览器抓取。"""
    try:
        proc = subprocess.run(
            [sys.executable, _hound_script(), url, str(max_chars)],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            return _r(url, ok=False, error=f"Hound error: {(proc.stderr or '').strip()[:200]}",
                      method="hound_browser")
        data = json.loads(proc.stdout.strip())
        cl = data.get("content", [])
        text = "\n".join(cl) if isinstance(cl, list) else str(cl)
        ok = data.get("error", "") == "" and data.get("status", 0) < 400 and bool(text.strip())
        return _r(url, content=text[:max_chars], length=len(text), ok=ok,
                  error=data.get("error") or None, method="hound_browser",
                  content_ok=data.get("content_ok", False),
                  page_type=data.get("page_type", "unknown"),
                  source_type=data.get("source_type", "unknown"),
                  is_official=data.get("is_official", False),
                  is_stale=data.get("is_stale", False),
                  content_age_days=data.get("content_age_days", -1),
                  quality_score=data.get("quality_score", 0.0),
                  title=(data.get("metadata") or {}).get("title", ""))
    except subprocess.TimeoutExpired:
        return _r(url, ok=False, error=f"Hound timeout after {timeout}s", method="hound_browser")
    except (json.JSONDecodeError, Exception) as e:
        return _r(url, ok=False, error=f"Hound unavailable: {str(e)[:100]}", method="hound_browser")


def _r(url: str, content: str = "", length: int | None = None,
       ok: bool = False, error: str | None = None, method: str = "http",
       html: str | None = None, content_ok: bool | None = None,
       page_type: str = "unknown", source_type: str = "unknown",
       is_official: bool = False, is_stale: bool = False,
       content_age_days: int = -1, quality_score: float | None = None,
       title: str = "") -> dict[str, Any]:
    """构建 Argo 统一输出 schema。"""
    if length is None:
        length = len(content)
    if content_ok is None:
        content_ok = ok and length > 50 and not error
    if quality_score is None:
        quality_score = 0.0 if not ok else (0.85 if length > 2000 else 0.6 if length > 500 else 0.3)

    result: dict[str, Any] = {
        "url": url, "content": content, "title": title, "length": length,
        "success": ok, "error": error, "content_ok": content_ok,
        "page_type": page_type, "source_type": source_type,
        "is_official": is_official, "is_stale": is_stale,
        "content_age_days": content_age_days, "fetch_method": method,
        "quality_score": quality_score,
    }
    if html is not None:
        result["html"] = html
    return result


def _classify(url: str) -> tuple[str, bool]:
    """快速域名分类（HTTP 模式轻量补充）。"""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return "unknown", False
    if host.endswith(".gov") or ".gov." in host:
        return "gov", True
    if host.endswith(".edu"):
        return "edu", True
    if "github.com" in host or host.endswith(".github.io"):
        return "github", True
    if host.startswith("docs.") or host.startswith("developer."):
        return "docs-site", True
    if "stackoverflow" in host or "stackexchange" in host:
        return "qa", False
    if any(m in host for m in ("forum", "community", "discourse")):
        return "forum", False
    return "unknown", False


def fetch_v2(url: str, max_chars: int = MAX_CHARS,
             timeout: int = HTTP_TIMEOUT, use_hound_fallback: bool = True) -> dict[str, Any]:
    """两级抓取主函数：HTTP 优先 + Hound 降级。"""
    result = http_fetch(url, max_chars=max_chars, timeout=timeout)

    if result["success"] and result["source_type"] == "unknown":
        st, official = _classify(url)
        result["source_type"] = st
        result["is_official"] = official

    if use_hound_fallback and _needs_hound(result):
        hr = hound_fetch(url, max_chars=max_chars)
        if hr["success"] or not result["success"]:
            if hr["success"] and not result["success"]:
                hr["http_fallback"] = True
            return hr
    return result


def fetch_page(url: str, max_chars: int = 3000,
               timeout: int = 8, raw: bool = False) -> dict[str, Any]:
    """与 fetch.py 的 fetch_page() 签名兼容，支持透明替换。"""
    result = fetch_v2(url, max_chars=max_chars, timeout=timeout)
    out = {"url": result["url"], "content": result["content"],
           "length": result["length"], "success": result["success"],
           "error": result.get("error", "")}
    if raw:
        # raw 模式需要 HTML，单独请求（Hound 不返回 HTML）
        out["html"] = http_fetch(url, max_chars=max_chars, timeout=timeout).get("html", "")
    return out


def fetch_pages_parallel(urls: list[str], max_chars: int = 3000,
                         timeout: int = 8, max_workers: int = 3) -> list[dict[str, Any]]:
    """并行抓取多个 URL（兼容 fetch.py）。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = []
    with ThreadPoolExecutor(max_workers=min(len(urls), max_workers)) as ex:
        futs = {ex.submit(fetch_page, u, max_chars, timeout): u for u in urls}
        for fut in as_completed(futs, timeout=timeout + 3):
            try:
                results.append(fut.result())
            except Exception:
                results.append({"url": futs[fut], "content": "", "length": 0,
                                "success": False, "error": "parallel fetch failed"})
    return results


def main():
    import argparse
    p = argparse.ArgumentParser(description="Argo fetch v2 — HTTP + Hound 浏览器降级")
    p.add_argument("url")
    p.add_argument("--max-chars", type=int, default=MAX_CHARS)
    p.add_argument("--timeout", type=int, default=HTTP_TIMEOUT)
    p.add_argument("--no-hound", action="store_true")
    args = p.parse_args()

    r = fetch_v2(args.url, max_chars=args.max_chars, timeout=args.timeout,
                 use_hound_fallback=not args.no_hound)
    out = {k: r[k] for k in ("success", "length", "fetch_method", "content_ok",
                             "quality_score", "page_type", "source_type", "url")}
    if r.get("error"):
        out["error"] = r["error"]
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("--- CONTENT ---")
    print(r["content"][:2000])


if __name__ == "__main__":
    main()
