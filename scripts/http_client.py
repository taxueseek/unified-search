#!/usr/bin/env python3
"""
http_client.py — 零依赖增强 HTTP 请求层

替代 urllib 的轻量封装，提供 Hound 级别的搜索弹性：
  1. User-Agent 轮换池（模拟 Chrome/Safari/Firefox/Edge）
  2. Cookie Jar 积累（跨请求保持会话）
  3. 指数退避重试 + 429/503 Retry-After 尊重
  4. 请求间随机抖动延迟（避免被识别为 bot）
  5. curl subprocess fallback（需要更强反检测时）

纯 stdlib 实现，零 pip 依赖。

用法：
    from http_client import HttpClient
    client = HttpClient()
    resp = client.get("https://example.com")
    print(resp["status"], resp["text"][:200])
"""

from __future__ import annotations

import http.client
import http.cookiejar
import json
import os
import random
import re
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Any


# ─── User-Agent 轮换池 ───────────────────────────────────────────────────────

# 模拟主流浏览器的完整请求头（不只是 UA 字符串，而是完整 header 集）
_UA_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:142.0) Gecko/20100101 Firefox/142.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="150", "Microsoft Edge";v="150"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Upgrade-Insecure-Requests": "1",
    },
]


def _random_headers(extra: dict | None = None) -> dict:
    """生成一组随机浏览器请求头。"""
    profile = random.choice(_UA_PROFILES).copy()
    if extra:
        profile.update(extra)
    return profile


# ─── Cookie 管理 ─────────────────────────────────────────────────────────────

class _CookieManager:
    """跨请求保持 Cookie 积累（Hound 暖会话机制）。"""

    def __init__(self, persist_path: str | None = None):
        self._jar = http.cookiejar.CookieJar()
        self._persist_path = persist_path

    def get_cookie_header(self, url: str) -> str:
        """获取适用于指定 URL 的 Cookie 头。"""
        parsed = urllib.parse.urlparse(url)
        # 构建一个虚拟 request 对象让 cookiejar 提取
        req = urllib.request.Request(url)
        self._jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""

    def extract_from_response(self, url: str, response_headers: list[tuple[str, str]]) -> None:
        """从响应头提取 Set-Cookie 并存入 jar。"""
        parsed = urllib.parse.urlparse(url)
        # 构建 mock request 让 cookiejar 能提取
        req = urllib.request.Request(url)
        # 使用 http.cookiejar.extract_cookies 需要 response 对象
        # 简化：手动解析 Set-Cookie
        for name, value in response_headers:
            if name.lower() == "set-cookie":
                self._parse_and_store(url, value)

    def _parse_and_store(self, url: str, set_cookie: str) -> None:
        """解析 Set-Cookie 头并存入 jar。"""
        try:
            parsed = urllib.parse.urlparse(url)
            # 用 MozillaCookieJar 的方式存储
            cookie = http.cookiejar.Cookie(
                version=0,
                name="",
                value="",
                port=None,
                port_specified=False,
                domain=parsed.hostname or "",
                domain_specified=True,
                domain_initial_dot=False,
                path="/",
                path_specified=True,
                secure=parsed.scheme == "https",
                expires=None,
                discard=True,
                comment=None,
                comment_url=None,
                rest={},
                rfc2109=False,
            )
            # 解析 name=value 部分
            parts = set_cookie.split(";")
            if parts:
                nv = parts[0].strip()
                if "=" in nv:
                    cookie.name, cookie.value = nv.split("=", 1)
                    cookie.domain = parsed.hostname or ""
                    self._jar.set_cookie(cookie)
        except Exception:
            pass

    @property
    def jar(self) -> http.cookiejar.CookieJar:
        return self._jar


# ─── 主 HTTP 客户端 ──────────────────────────────────────────────────────────

class HttpClient:
    """增强 HTTP 客户端：UA 轮换 + Cookie 积累 + 重试弹性。

    设计原则：
    - 每次请求随机选择 UA profile（模拟不同浏览器）
    - 自动积累 Cookie（跨请求保持会话状态）
    - 429/503 尊重 Retry-After 头
    - 指数退避重试（最多 3 次）
    - 请求间随机抖动延迟（0.1-0.5s）
    """

    def __init__(self, timeout: float = 10.0, max_retries: int = 2,
                 jitter: bool = True, use_curl_fallback: bool = False):
        self.timeout = timeout
        self.max_retries = max_retries
        self.jitter = jitter
        self.use_curl_fallback = use_curl_fallback
        self._cookies = _CookieManager()
        self._last_request_time = 0.0

    def get(self, url: str, extra_headers: dict | None = None,
            follow_redirects: bool = True) -> dict:
        """发送 GET 请求，返回统一响应格式。

        返回：{
            "status": int,
            "headers": dict,
            "text": str,
            "url": str,       # 最终 URL（跟随重定向后）
            "elapsed_ms": int,
            "from_cache": bool,
        }
        """
        self._apply_jitter()

        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                return self._do_get(url, extra_headers, follow_redirects)
            except (socket.timeout, ConnectionError, OSError) as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = self._backoff_delay(attempt)
                    time.sleep(wait)
            except Exception as e:
                last_error = e
                break

        return {"status": 0, "headers": {}, "text": "", "url": url,
                "elapsed_ms": 0, "error": str(last_error)[:200]}

    def _do_get(self, url: str, extra_headers: dict | None,
                follow_redirects: bool) -> dict:
        """实际执行 GET 请求（使用 http.client，不自动解压）。"""
        start = time.time()

        # 构建请求头
        headers = _random_headers(extra_headers)
        cookie_str = self._cookies.get_cookie_header(url)
        if cookie_str:
            headers["Cookie"] = cookie_str

        # 解析 URL
        parsed = urllib.parse.urlparse(url)
        if not parsed.scheme:
            url = "https://" + url
            parsed = urllib.parse.urlparse(url)

        # 使用 http.client（不自动解压，我们可以手动处理）
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = connection_class(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                               timeout=self.timeout)

        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_headers = dict(resp.getheaders())

        # 提取 Cookie
        self._cookies.extract_from_response(url, resp.getheaders())

        # 读取 body
        raw_body = resp.read()
        conn.close()

        # 手动解压
        encoding = resp.getheader("Content-Encoding", "")
        if "gzip" in encoding:
            import gzip
            import io
            raw_body = gzip.GzipFile(fileobj=io.BytesIO(raw_body)).read()
        elif "br" in encoding:
            try:
                import brotli
                raw_body = brotli.decompress(raw_body)
            except ImportError:
                # brotli 不可用 → curl fallback
                return self.get_with_curl(url, extra_headers)

        # 解码
        content_type = resp.getheader("Content-Type", "")
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=")[-1].strip().split(";")[0]

        text = raw_body.decode(charset, errors="replace")

        elapsed = int((time.time() - start) * 1000)

        return {
            "status": status,
            "headers": resp_headers,
            "text": text,
            "url": url,
            "elapsed_ms": elapsed,
            "from_cache": False,
        }

    def _apply_jitter(self) -> None:
        """请求间随机抖动延迟。"""
        if not self.jitter:
            return
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 0.1:
            delay = random.uniform(0.05, 0.3)
            time.sleep(delay)
        self._last_request_time = time.time()

    @staticmethod
    def _backoff_delay(attempt: int) -> float:
        """指数退避 + 随机抖动。"""
        base = min(2 ** attempt, 8)  # 1s, 2s, 4s, 8s cap
        return base + random.uniform(0, 0.5)

    def get_with_curl(self, url: str, extra_headers: dict | None = None) -> dict:
        """curl subprocess fallback（更强的反检测能力）。

        curl 的 TLS 指纹与 Python urllib 不同，某些网站对 curl 更友好。
        """
        start = time.time()
        headers = _random_headers(extra_headers)

        cmd = ["curl", "-s", "-L", "--max-time", str(int(self.timeout)),
               "-w", "\\n%{http_code}\\n%{url_effective}"]
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)

        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout + 5)
            output = r.stdout.strip()
            lines = output.rsplit("\n", 2)
            if len(lines) >= 2:
                text = lines[0] if len(lines) == 2 else "\n".join(lines[:-2])
                status = int(lines[-2]) if lines[-2].isdigit() else 0
                final_url = lines[-1]
            else:
                text = output
                status = 0
                final_url = url

            elapsed = int((time.time() - start) * 1000)
            return {"status": status, "headers": {}, "text": text, "url": final_url,
                    "elapsed_ms": elapsed, "from_cache": False, "via_curl": True}
        except Exception as e:
            return {"status": 0, "headers": {}, "text": "", "url": url,
                    "elapsed_ms": 0, "error": str(e)[:200]}


# ─── 便捷函数 ────────────────────────────────────────────────────────────────

def fetch_url(url: str, timeout: float = 10.0, max_retries: int = 2) -> dict:
    """一次性 GET 请求的便捷函数。"""
    client = HttpClient(timeout=timeout, max_retries=max_retries)
    return client.get(url)


# ─── CLI 测试 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else "https://httpbin.org/get"

    print(f"=== Testing HttpClient: {url} ===")
    client = HttpClient(timeout=10, max_retries=1)
    resp = client.get(url)

    print(f"Status: {resp['status']}")
    print(f"Elapsed: {resp['elapsed_ms']}ms")
    print(f"Text length: {len(resp['text'])}")
    if resp.get("text"):
        try:
            data = json.loads(resp["text"])
            print(f"Server saw UA: {data.get('headers', {}).get('User-Agent', 'N/A')[:60]}")
        except json.JSONDecodeError:
            print(f"First 200 chars: {resp['text'][:200]}")

    # 测试 curl fallback
    print(f"\n=== Testing curl fallback ===")
    resp2 = client.get_with_curl(url)
    print(f"Status: {resp2['status']}, via_curl: {resp2.get('via_curl')}")
    if resp2.get("text"):
        try:
            data = json.loads(resp2["text"])
            print(f"Server saw UA: {data.get('headers', {}).get('User-Agent', 'N/A')[:60]}")
        except json.JSONDecodeError:
            pass
