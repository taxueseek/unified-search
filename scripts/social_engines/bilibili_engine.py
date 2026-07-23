#!/usr/bin/env python3
"""B站搜索引擎

使用 B站公开搜索 API（无需登录可搜索视频）。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request


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


def search(query: str, n: int = 5) -> list[dict]:
    """通过 B站搜索 API 搜索"""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://api.bilibili.com/x/web-interface/search/all/v2"
        f"?keyword={encoded}&page=1&pagesize={n}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://www.bilibili.com"
    }
    try:
        body, _ = _http_get_with_retry(url, headers, timeout=10, max_retries=2)
        data = json.loads(body.decode("utf-8"))
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
