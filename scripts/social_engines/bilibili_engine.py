#!/usr/bin/env python3
"""B站搜索引擎

使用 B站公开搜索 API（无需登录可搜索视频）。
"""

import json
import urllib.parse
import urllib.request


def search(query: str, n: int = 5) -> list[dict]:
    """通过 B站搜索 API 搜索"""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://api.bilibili.com/x/web-interface/search/all/v2"
        f"?keyword={encoded}&page=1&pagesize={n}"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com"
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return _parse_bilibili_response(data, n)
    except Exception:
        return []


def _parse_bilibili_response(data: dict, n: int) -> list[dict]:
    """解析 B站 API 响应"""
    results = []
    data_data = data.get("data", {})
    for result_type in data_data.get("result", []):
        if result_type.get("result_type") != "video":
            continue
        for item in result_type.get("data", [])[:n]:
            title = item.get("title", "")
            # 清理 HTML 标签
            import re
            title = re.sub(r'<[^>]+>', '', title)
            results.append({
                "title": title[:100] + ("..." if len(title) > 100 else ""),
                "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
                "snippet": (item.get("description", "") or title)[:300],
                "source": "bilibili",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "bilibili",
                    "content_type": "video",
                    "author": item.get("author", ""),
                    "duration": item.get("duration", ""),
                    "play_count": item.get("play", 0),
                    "danmu_count": item.get("video_review", 0),
                    "like_count": item.get("like", 0),
                    "bvid": item.get("bvid", ""),
                }
            })
            if len(results) >= n:
                break
        if len(results) >= n:
            break
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Bilibili search engine")
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
