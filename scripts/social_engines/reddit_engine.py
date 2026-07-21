#!/usr/bin/env python3
"""Reddit 搜索引擎

使用 Reddit JSON API（无需认证即可搜索公开内容）。
也可通过 rdt CLI 或 praw 扩展。
"""

import json
import subprocess
import urllib.parse
import urllib.request


def search_reddit_api(query: str, n: int = 5) -> list[dict]:
    """通过 Reddit JSON API 搜索"""
    encoded = urllib.parse.quote(query)
    url = f"https://www.reddit.com/search.json?q={encoded}&limit={n}&sort=relevance"
    req = urllib.request.Request(url, headers={
        "User-Agent": "argo-search/1.0 (by taxueseek)"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return _parse_reddit_response(data, n)
    except Exception:
        return []


def _parse_reddit_response(data: dict, n: int) -> list[dict]:
    """解析 Reddit API 响应"""
    results = []
    for child in data.get("data", {}).get("children", [])[:n]:
        post = child.get("data", {})
        if not post:
            continue
        title = post.get("title", "")
        selftext = post.get("selftext", "")
        results.append({
            "title": title[:100] + ("..." if len(title) > 100 else ""),
            "url": f"https://reddit.com{post.get('permalink', '')}",
            "snippet": (selftext or title)[:300],
            "source": "reddit",
            "score": max(1.0 - len(results) * 0.1, 0.1),
            "social_meta": {
                "platform": "reddit",
                "content_type": "post",
                "subreddit": post.get("subreddit", ""),
                "author": post.get("author", ""),
                "upvotes": post.get("ups", 0),
                "comments": post.get("num_comments", 0),
                "awards": post.get("total_awards_received", 0),
            }
        })
    return results


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口"""
    # 优先尝试 rdt CLI
    try:
        result = subprocess.run(
            ["rdt", "search", query, "--limit", str(n), "--json"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_rdt_json(result.stdout, n)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # fallback: Reddit JSON API
    return search_reddit_api(query, n)


def _parse_rdt_json(raw: str, n: int) -> list[dict]:
    """解析 rdt CLI JSON 输出"""
    results = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("results", data.get("posts", []))
        else:
            items = []
        for post in items[:n]:
            if not isinstance(post, dict):
                continue
            title = post.get("title", "")
            results.append({
                "title": title,
                "url": post.get("url", post.get("permalink", "")),
                "snippet": (post.get("selftext", "") or title)[:300],
                "source": "reddit",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "reddit",
                    "content_type": "post",
                    "subreddit": post.get("subreddit", ""),
                    "author": post.get("author", ""),
                    "upvotes": post.get("ups", post.get("upvotes", 0)),
                    "comments": post.get("num_comments", 0),
                }
            })
    except (json.JSONDecodeError, ValueError):
        pass
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Reddit search engine")
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
