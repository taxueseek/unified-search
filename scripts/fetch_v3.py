#!/usr/bin/env python3
"""
fetch_v3.py — 三级抓取架构（零外部依赖，纯 stdlib + 系统 Chrome）

吸收 Hound 的页面交互能力，但不引入 Playwright/Patchright 依赖：
  第一级：增强 HTTP（UA 轮换 + Cookie 积累 + 重试弹性）
  第二级：Chrome CDP 驱动（页面交互/JS 渲染/CF 绕过）
  第三级：内容质量评估（content_ok/page_type/quality_score）

对比 fetch_v2：
- fetch_v2: urllib + Hound subprocess（需要 master_fetch 包）
- fetch_v3: http_client(stdlib) + chrome_cdp(stdlib+系统Chrome) → 完全自主

用法：
    from fetch_v3 import fetch_v3, fetch_page_v3
    result = fetch_v3("https://example.com", actions=[{"click": "#btn"}])
    # 兼容旧接口
    result = fetch_page_v3("https://example.com", max_chars=3000)
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
import traceback
import urllib.parse
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

# 确保能导入同目录模块
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


# ─── 内容提取器（复用 fetch.py 的逻辑，增强版）──────────────────────────────

_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"}

class ContentExtractor(HTMLParser):
    """从 HTML 提取正文文本（基于文本密度排序）。"""

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._blocks: list[tuple[float, str]] = []
        self._current: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._in_skip += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._in_skip = max(0, self._in_skip - 1)
        if tag == "title":
            self._in_title = False
        if tag in ("p", "div", "article", "section", "li", "h1", "h2", "h3", "h4", "td", "blockquote"):
            text = "".join(self._current).strip()
            if len(text) > 20:
                density = len(text.replace(" ", "")) / max(len(text), 1)
                self._blocks.append((density, text))
            self._current = []

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_skip == 0:
            self._current.append(data)


def extract_content(html: str, max_chars: int = 8000) -> tuple[str, str]:
    """从 HTML 提取正文和标题。"""
    ext = ContentExtractor()
    try:
        ext.feed(html)
    except Exception:
        pass
    ext._blocks.sort(key=lambda x: x[0], reverse=True)
    content = "\n\n".join(text for _, text in ext._blocks[:10])
    return content[:max_chars], ext.title.strip()


# ─── 降级检测 ────────────────────────────────────────────────────────────────

_CF_MARKERS = re.compile(
    r"checking your browser|cf-browser-verification|cf_chl_opt|ray id|"
    r"challenge-platform|please verify you are a human|cloudflare",
    re.IGNORECASE,
)

_JS_MARKERS = re.compile(
    r"enable javascript|javascript is required|javascript to run this app|"
    r"you need to enable javascript|requires javascript",
    re.IGNORECASE,
)

_EMPTY_SHELL = re.compile(
    r"^[\s\n]*<html[^>]*>[\s\n]*<head>.*?</head>[\s\n]*<body>[\s\n]*</body>[\s\n]*</html>[\s\n]*$",
    re.IGNORECASE | re.DOTALL,
)


def _needs_browser(result: dict) -> bool:
    """判断是否需要升级到浏览器抓取。"""
    if not result.get("success"):
        return True
    content = result.get("content", "")
    html = result.get("html", "")
    # 内容过少
    if not content or len(content.strip()) < 100:
        return True
    # 空壳 HTML
    if html and _EMPTY_SHELL.search(html):
        return True
    # CF 挑战
    if _CF_MARKERS.search(html) or _CF_MARKERS.search(content):
        return True
    # JS 要求
    if len(content) < 300 and _JS_MARKERS.search(html):
        return True
    return False


# ─── 第一级：增强 HTTP ───────────────────────────────────────────────────────

def _http_fetch(url: str, max_chars: int = 8000, timeout: float = 8.0) -> dict:
    """使用 http_client（UA 轮换 + Cookie 积累）抓取。"""
    try:
        from http_client import HttpClient
        client = HttpClient(timeout=timeout, max_retries=1, jitter=False)
        resp = client.get(url)
    except ImportError:
        # fallback 到 urllib
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "argo-fetch-v3/1.0 (+local-research)",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", errors="replace")
                return _make_result(url, text, max_chars, "http")
        except Exception as e:
            return _make_result(url, "", 0, "http", ok=False, error=str(e)[:100])

    if resp.get("status", 0) >= 400:
        return _make_result(url, "", 0, "http", ok=False,
                            error=f"HTTP {resp.get('status')}")
    if not resp.get("text"):
        return _make_result(url, "", 0, "http", ok=False, error="empty response")

    return _make_result(url, resp["text"], max_chars, "http")


def _make_result(url: str, html: str, max_chars: int,
                 method: str, ok: bool = True, error: str | None = None,
                 title: str = "") -> dict:
    """构建统一输出格式。"""
    content, extracted_title = extract_content(html, max_chars) if ok and html else ("", "")
    if not title:
        title = extracted_title
    return {
        "url": url,
        "content": content,
        "html": html[:max_chars * 2] if html else "",
        "title": title,
        "length": len(content),
        "success": ok,
        "error": error,
        "fetch_method": method,
    }


# ─── 第二级：Chrome CDP 浏览器 ───────────────────────────────────────────────

def _browser_fetch(url: str, max_chars: int = 8000, timeout: float = 15.0,
                   actions: list[dict] | None = None) -> dict:
    """使用 Chrome CDP 驱动抓取（支持页面交互）。"""
    try:
        from chrome_cdp import ChromeCDP
    except ImportError:
        return _make_result(url, "", 0, "browser", ok=False,
                            error="chrome_cdp not available")

    try:
        cdp = ChromeCDP(auto_start=True)
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"Chrome failed to start: {str(e)[:100]}")

    try:
        # 导航
        cdp.navigate(url, wait_until="networkidle")

        # 执行页面交互序列（Hound actions 等价能力）
        if actions:
            cdp.execute_actions(actions)

        # 提取内容
        html = cdp.get_html()
        text = cdp.get_text()
        title = cdp.get_title()

        return {
            "url": url,
            "content": text[:max_chars] if text else "",
            "html": html[:max_chars * 2] if html else "",
            "title": title or "",
            "length": len(text) if text else 0,
            "success": bool(text),
            "error": None if text else "empty content",
            "fetch_method": "chrome_cdp",
        }
    except Exception as e:
        return _make_result(url, "", 0, "browser", ok=False,
                            error=f"CDP error: {str(e)[:100]}")
    finally:
        try:
            cdp.stop()
        except Exception:
            pass


# ─── 第三级：质量评估 ─────────────────────────────────────────────────────────

def _assess_quality(result: dict) -> dict:
    """计算内容质量信号（内联 content_signals 的核心逻辑）。"""
    url = result.get("url", "")
    content = result.get("content", "")
    html = result.get("html", "")

    # source_type + is_official
    source_type, is_official = _classify_domain(url)

    # page_type
    page_type = _detect_page_type(html, content)

    # quality_score
    quality_score = _compute_quality(content, html)

    # content_ok
    content_ok = quality_score > 0.25 and len(content) > 80

    # is_stale (简化：无日期信息时保守判定)
    is_stale = False
    content_age_days = -1

    result.update({
        "content_ok": content_ok,
        "page_type": page_type,
        "source_type": source_type,
        "is_official": is_official,
        "is_stale": is_stale,
        "content_age_days": content_age_days,
        "quality_score": quality_score,
    })
    return result


def _classify_domain(url: str) -> tuple[str, bool]:
    """快速域名分类。"""
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return "unknown", False

    if host.endswith(".gov") or ".gov." in host:
        return "gov", True
    if host.endswith(".edu") or host.endswith(".ac.uk"):
        return "edu", True
    if "github.com" in host or host.endswith(".github.io"):
        return "github", True
    if host.startswith("docs.") or host.startswith("developer."):
        return "docs-site", True
    if "stackoverflow" in host or "stackexchange" in host:
        return "qa", False
    if any(m in host for m in ("forum", "community", "discourse")):
        return "forum", False
    if host in ("reddit.com", "www.reddit.com", "old.reddit.com"):
        return "forum", False
    if any(host == d or host.endswith("." + d) for d in (
        "nytimes.com", "bbc.com", "reuters.com", "theguardian.com",
        "bloomberg.com", "techcrunch.com", "theverge.com",
    )):
        return "news", False
    return "unknown", False


def _detect_page_type(html: str, content: str) -> str:
    """检测页面结构类型。"""
    if not html:
        return "unknown"
    # 重定向
    if re.search(r'<meta[^>]*http-equiv=["\']refresh["\']', html, re.I):
        return "redirect"
    # paywall
    if re.search(r'subscribe to continue|paywall|premium content', html, re.I):
        return "paywall"
    # forum
    if re.search(r'phpbb|discourse|class="forum|id="forum', html, re.I):
        return "forum"
    # qa
    if re.search(r'stackoverflow|class="question|data-answerid', html, re.I):
        return "qa"
    # docs
    if re.search(r'mkdocs|docusaurus|readthedocs|sphinx-document|md-nav', html, re.I):
        return "docs"
    # list/index (many links, little text)
    link_count = len(re.findall(r'<a\s+href=', html))
    text_len = len(content)
    if link_count > 20 and text_len < 500:
        return "list"
    # article
    if text_len > 200:
        return "article"
    return "unknown"


def _compute_quality(content: str, html: str) -> float:
    """计算质量评分（0-1）。"""
    if not content:
        return 0.0
    word_count = len(content.split())
    text_density = len(content.replace(" ", "").replace("\n", "")) / max(len(content), 1)
    has_structure = bool(re.search(r'[.!?。！？].{10,}[.!?。！？]', content))

    score = min(1.0, (
        0.4 * min(word_count / 500, 1.0) +
        0.3 * text_density +
        0.2 * (1.0 if has_structure else 0.0) +
        0.1 * (1.0 if len(content) > 1000 else 0.0)
    ))
    return round(score, 2)


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def _optimize_url(url: str) -> str:
    """URL 优化：Reddit 重写、追踪参数清理等。

    - reddit.com → old.reddit.com（7× 更小、无 JS 渲染要求）
    - 清理常见追踪参数（utm_source, fbclid, gclid 等）
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # Reddit 优化
    if host in ("reddit.com", "www.reddit.com", "new.reddit.com"):
        # 重写为 old.reddit（纯 HTML，无需 JS，体积更小）
        url = url.replace("://www.reddit.com", "://old.reddit.com")
        url = url.replace("://reddit.com", "://old.reddit.com")
        url = url.replace("://new.reddit.com", "://old.reddit.com")

    # 清理追踪参数
    tracking_params = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                       "utm_content", "fbclid", "gclid", "ref", "ref_src"}
    qs = urllib.parse.parse_qs(parsed.query)
    filtered = {k: v for k, v in qs.items() if k.lower() not in tracking_params}
    if len(filtered) < len(qs):
        new_qs = urllib.parse.urlencode(filtered, doseq=True)
        parsed = parsed._replace(query=new_qs)
        url = urllib.parse.urlunparse(parsed)

    return url


def fetch_v3(url: str, max_chars: int = 8000, timeout: float = 8.0,
             use_browser_fallback: bool = True,
             actions: list[dict] | None = None,
             force_browser: bool = False) -> dict:
    """三级抓取主函数。

    第一级：增强 HTTP（UA 轮换 + Cookie 积累）
    第二级：Chrome CDP 浏览器（自动降级或 actions 触发）
    第三级：质量评估（content_ok/page_type/quality_score）

    Args:
        url: 目标 URL
        max_chars: 最大返回字符数
        timeout: HTTP 超时（浏览器模式固定 15s）
        use_browser_fallback: HTTP 失败时自动升级浏览器
        actions: 页面交互序列（设置则强制使用浏览器）
        force_browser: 强制使用浏览器

    Returns:
        统一 schema 的 dict（含 content_ok/page_type/quality_score）
    """
    # URL 优化（Reddit 重写、追踪参数清理）
    url = _optimize_url(url)

    # 有 actions → 强制浏览器模式
    if actions:
        force_browser = True

    if force_browser:
        result = _browser_fetch(url, max_chars, timeout=15.0, actions=actions)
    else:
        # 第一级：HTTP
        result = _http_fetch(url, max_chars, timeout)

        # 第二级：浏览器降级
        if use_browser_fallback and _needs_browser(result):
            browser_result = _browser_fetch(url, max_chars, timeout=15.0)
            if browser_result.get("success") or not result.get("success"):
                browser_result["http_fallback"] = True
                result = browser_result

    # 第三级：质量评估
    result = _assess_quality(result)

    return result


def fetch_page_v3(url: str, max_chars: int = 3000,
                  timeout: int = 8, raw: bool = False) -> dict:
    """兼容 fetch.py 的 fetch_page() 签名，支持透明替换。"""
    result = fetch_v3(url, max_chars=max_chars, timeout=float(timeout))
    out = {
        "url": result["url"],
        "content": result["content"],
        "length": result["length"],
        "success": result["success"],
        "error": result.get("error", ""),
    }
    if raw:
        out["html"] = result.get("html", "")
    return out


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Argo fetch v3 — 三级抓取（零依赖）")
    p.add_argument("url", help="目标 URL")
    p.add_argument("--max-chars", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--browser", action="store_true", help="强制使用浏览器")
    p.add_argument("--no-fallback", action="store_true", help="禁用浏览器降级")
    p.add_argument("--actions", type=str, help="页面交互 JSON（如 '[{\"click\":\"#btn\"}]'）")
    args = p.parse_args()

    actions = None
    if args.actions:
        actions = json.loads(args.actions)

    r = fetch_v3(args.url, max_chars=args.max_chars, timeout=args.timeout,
                 force_browser=args.browser,
                 use_browser_fallback=not args.no_fallback,
                 actions=actions)

    # 输出摘要
    summary = {k: r[k] for k in ("success", "fetch_method", "content_ok",
                                  "quality_score", "page_type", "source_type",
                                  "is_official", "length", "url")}
    if r.get("error"):
        summary["error"] = r["error"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if r.get("title"):
        print(f"\nTitle: {r['title']}")
    print(f"\n--- CONTENT ({r['length']} chars) ---")
    print(r.get("content", "")[:2000])
