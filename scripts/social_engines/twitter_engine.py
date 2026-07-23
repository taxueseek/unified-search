#!/usr/bin/env python3
"""Twitter/X 搜索引擎

使用 twitter CLI (https://github.com/twitter/tw) 或 nitter 实例。
零 API Key 方案：通过 nitter RSS 或 web 抓取。
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _http_get_with_retry(url: str, headers: dict, timeout: int = 10, max_retries: int = 2):
    """带重试的 HTTP GET，尊重 429 + Retry-After。

    仅使用 stdlib，不引入第三方依赖。
    返回 (body_bytes, status_code)。失败时抛出最后一次异常。
    """
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.status
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries:
                retry_after = int(e.headers.get("Retry-After", "5"))
                time.sleep(min(retry_after, 30))
                continue
            raise
        except (urllib.error.URLError, OSError):
            if attempt < max_retries:
                time.sleep(2 ** attempt + 0.5)
                continue
            raise
    return b"", 0


def search_nitter(query: str, n: int = 5) -> list[dict]:
    """通过 nitter 公开实例搜索推文（零认证）"""
    nitter_instances = [
        "https://nitter.net",
        "https://nitter.privacydev.net",
        "https://nitter.poast.org",
    ]
    encoded_query = urllib.parse.quote(query)

    for base in nitter_instances:
        try:
            url = f"{base}/search?f=tweets&q={encoded_query}&since=2025-01-01"
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
            body, _ = _http_get_with_retry(url, headers, timeout=8, max_retries=2)
            html = body.decode("utf-8", errors="replace")
            return _parse_nitter_html(html, n)
        except Exception:
            continue
    return []


def _parse_nitter_html(html: str, n: int) -> list[dict]:
    """解析 nitter 搜索结果 HTML"""
    results = []
    # nitter 推文结构
    tweet_pattern = re.compile(
        r'<div class="tweet-content[^"]*".*?'
        r'<a class="tweet-link" href="([^"]+)".*?'
        r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL
    )
    for match in tweet_pattern.finditer(html):
        url = match.group(1)
        content = match.group(2)
        # 清理 HTML 标签
        content = re.sub(r'<[^>]+>', ' ', content).strip()
        content = re.sub(r'\s+', ' ', content)
        if len(content) > 10:
            results.append({
                "title": content[:100] + ("..." if len(content) > 100 else ""),
                "url": url if url.startswith("http") else f"https://twitter.com{url}",
                "snippet": content[:300],
                "source": "twitter",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "twitter",
                    "content_type": "tweet",
                }
            })
        if len(results) >= n:
            break
    return results


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口"""
    # 优先尝试 twitter CLI
    try:
        result = subprocess.run(
            ["tw", "search", query, "--limit", str(n), "--json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_tw_json(result.stdout, n)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # fallback: nitter
    return search_nitter(query, n)


def _parse_tw_json(raw: str, n: int) -> list[dict]:
    """解析 twitter CLI JSON 输出"""
    results = []
    for line in raw.strip().split("\n"):
        try:
            tweet = json.loads(line)
            if not isinstance(tweet, dict):
                continue
            results.append({
                "title": (tweet.get("text", "") or tweet.get("full_text", ""))[:100],
                "url": tweet.get("url", f"https://twitter.com/i/status/{tweet.get('id', '')}"),
                "snippet": tweet.get("text", "") or tweet.get("full_text", ""),
                "source": "twitter",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "twitter",
                    "content_type": "tweet",
                    "author": tweet.get("user", {}).get("screen_name", ""),
                    "likes": tweet.get("favorite_count", 0),
                    "retweets": tweet.get("retweet_count", 0),
                    "replies": tweet.get("reply_count", 0),
                }
            })
        except (json.JSONDecodeError, ValueError):
            continue
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Twitter search engine")
    parser.add_argument("action", nargs="?", default="search")
    parser.add_argument("query", nargs="?")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if not args.query:
        print("[]")
        return

    results = search(args.query, args.n)
    if args.json:
        print(json.dumps(results, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            print(f"### {i}. {r['title']}")
            print(f"- **URL**: {r['url']}")
            print(f"- {r['snippet'][:200]}")
            print()


if __name__ == "__main__":
    main()
