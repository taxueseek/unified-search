#!/usr/bin/env python3
"""小红书搜索引擎

使用 xhs-cli (xiaohongshu-cli) 工具。
需要先通过 `xhs login` 登录获取 cookies。
"""

import json
import subprocess


def search(query: str, n: int = 5) -> list[dict]:
    """主搜索入口"""
    try:
        result = subprocess.run(
            ["xhs", "search", query],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_xhs_output(result.stdout, n)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return []


def _parse_xhs_output(raw: str, n: int) -> list[dict]:
    """解析 xhs-cli 输出"""
    results = []
    try:
        data = json.loads(raw)
        items = data.get("items", data.get("data", []))
        if isinstance(items, dict):
            items = items.get("items", [])
        for item in items[:n]:
            if not isinstance(item, dict):
                continue
            note_card = item.get("note_card", item)
            title = note_card.get("display_title", note_card.get("title", ""))
            desc = note_card.get("desc", "")
            interact = note_card.get("interact_info", {})
            results.append({
                "title": title[:100] + ("..." if len(title) > 100 else ""),
                "url": f"https://www.xiaohongshu.com/explore/{item.get('id', note_card.get('note_id', ''))}",
                "snippet": (desc or title)[:300],
                "source": "xiaohongshu",
                "score": max(1.0 - len(results) * 0.1, 0.1),
                "social_meta": {
                    "platform": "xiaohongshu",
                    "content_type": "note",
                    "author": note_card.get("user", {}).get("nickname", ""),
                    "likes": interact.get("liked_count", 0),
                    "comments": interact.get("comment_count", 0),
                    "collects": interact.get("collected_count", 0),
                    "type": note_card.get("type", "normal"),  # normal / video
                }
            })
    except (json.JSONDecodeError, ValueError):
        # 尝试文本解析
        pass
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Xiaohongshu search engine")
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
