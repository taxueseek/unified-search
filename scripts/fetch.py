#!/usr/bin/env python3
"""
fetch.py — 轻量页面抓取（纯标准库）

从 URL 抓取页面并提取正文文本，用于 research 深度研究。
"""

from __future__ import annotations

import re
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse


class ContentExtractor(HTMLParser):
    """从 HTML 中提取正文文本。"""

    _skip_tags = {"script", "style", "nav", "header", "footer", "aside", "noscript"}

    def __init__(self):
        super().__init__()
        self._in_skip = 0
        self._blocks: list[tuple[float, str]] = []
        self._current_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._in_skip += 1

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._in_skip = max(0, self._in_skip - 1)
        if tag in ("p", "div", "article", "section", "li", "h1", "h2", "h3", "td"):
            text = "".join(self._current_text).strip()
            if len(text) > 20:
                # 文本密度 = 非空字符 / 总字符
                density = len(text.replace(" ", "")) / max(len(text), 1)
                self._blocks.append((density, text))
            self._current_text = []

    def handle_data(self, data):
        if self._in_skip == 0:
            self._current_text.append(data)


def fetch_page(url: str, max_chars: int = 3000, timeout: int = 8,
              raw: bool = False) -> dict[str, Any]:
    """抓取页面并提取正文。

    Args:
        url: 目标 URL
        max_chars: 最大返回字符数
        timeout: 超时秒数
        raw: True 时返回原始 HTML（用于 extract/crawl_sitemap 等需要 HTML 的场景）
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "unified-search/2.5 (+local-research)",
            "Accept": "text/html,application/xhtml+xml,application/xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" in content_type:
                return {"url": url, "content": "", "html": "", "length": 0, "success": False, "error": "PDF not supported"}
            raw_bytes = resp.read(max(max_chars * 5, 500000))
            # 编码检测
            charset = "utf-8"
            if "charset=" in content_type:
                charset = content_type.split("charset=")[-1].strip().split(";")[0]
            html = raw_bytes.decode(charset, errors="replace")

        # 正文提取
        extractor = ContentExtractor()
        extractor.feed(html)
        extractor._blocks.sort(key=lambda x: x[0], reverse=True)
        content = "\n\n".join(text for _, text in extractor._blocks[:8])

        result = {
            "url": url,
            "content": content[:max_chars],
            "length": len(content),
            "success": True,
        }
        if raw:
            result["html"] = html[:max(max_chars * 2, 100000)]
        return result
    except Exception as e:
        return {"url": url, "content": "", "html": "", "length": 0, "success": False, "error": str(e)[:100]}


def fetch_pages_parallel(urls: list[str], max_chars: int = 3000,
                         timeout: int = 8, max_workers: int = 3) -> list[dict[str, Any]]:
    """并行抓取多个页面。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=min(len(urls), max_workers)) as ex:
        futures = {ex.submit(fetch_page, url, max_chars, timeout): url for url in urls}
        for fut in as_completed(futures, timeout=timeout + 3):
            try:
                results.append(fut.result())
            except Exception:
                results.append({"url": futures[fut], "content": "", "length": 0, "success": False})
    return results


# CLI 测试
if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://docs.python.org/3/"
    result = fetch_page(url, max_chars=500)
    print(f"成功: {result['success']}, 长度: {result['length']}")
    if result["success"]:
        print(result["content"][:300])
    else:
        print(f"错误: {result.get('error', 'unknown')}")
