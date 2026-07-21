#!/usr/bin/env python3
"""
research.py — 深度研究工具（wigolo research 理念移植 + 社交舆情模式）

核心能力：
  1. 问题分解：将复杂查询拆分为 3-5 个子查询
  2. 多源采集：对每个子查询并行执行搜索
  3. 综合报告：合并去重 + 来源标注 + 知识缺口识别
  4. 引用追踪：每个结论可追溯到具体搜索结果
  5. 社交舆情：跨平台 UGC 情绪倾向 + 高频讨论点

用法：
  python3 research.py "CRISPR-Cas9 脱靶效应的 AI 预测方法综述"
  python3 research.py "CVE-2024-6387 生产环境影响评估" --depth deep
  python3 research.py "台积电财报分歧分析" --sub-queries 5
  python3 research.py "iPhone 16 用户评价" --mode social-sentiment --platforms xiaohongshu,reddit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from search import super_search, rrf_merge, deduplicate_by_url


# ── 交叉引用检测 ──────────────────────────────────────────────────────────────

def detect_cross_references(results: list[dict[str, Any]], min_sources: int = 2,
                            min_ngram_len: int = 3) -> list[dict[str, Any]]:
    """检测多个来源的交叉引用（n-gram 重叠）。

    如果同一 n-gram 出现在 ≥min_sources 个不同域名的结果中，
    标记为「潜在佐证」。
    """
    import re
    from urllib.parse import urlparse

    # 提取所有 snippet 的 n-gram
    ngram_sources: dict[str, set] = {}  # ngram -> set of (url, title)
    for r in results:
        url = r.get("url", "")
        domain = urlparse(url).netloc.lower().strip("www.") if url else "unknown"
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        # 简单分词（中英文混合）
        # 英文按空格分
        en_tokens = re.findall(r"[a-zA-Z]+", text.lower())
        # 中文按字符 bigram/trigram
        cn_chars = re.findall(r"[\u4e00-\u9fff]+", text)
        cn_tokens = []
        for seg in cn_chars:
            for i in range(len(seg) - min_ngram_len + 1):
                cn_tokens.append(seg[i:i + min_ngram_len])

        all_tokens = en_tokens + cn_tokens
        for n in range(min_ngram_len, min(min_ngram_len + 1, len(all_tokens) + 1)):
            for i in range(len(all_tokens) - n + 1):
                ngram = " ".join(all_tokens[i:i + n])
                if len(ngram) >= 4:  # 过滤太短的 ngram
                    ngram_sources.setdefault(ngram, set()).add(domain)

    # 找出被多个来源佐证的 n-gram
    cross_refs = []
    for ngram, domains in ngram_sources.items():
        if len(domains) >= min_sources:
            cross_refs.append({
                "ngram": ngram,
                "source_count": len(domains),
                "domains": sorted(domains),
            })

    # 按来源数排序，取 top 10
    cross_refs.sort(key=lambda x: x["source_count"], reverse=True)
    return cross_refs[:10]


# ── 问题分解 ──────────────────────────────────────────────────────────────────

def decompose_query(query: str, num_sub: int = 4) -> list[dict[str, str]]:
    """将复杂查询分解为子查询。

    策略：基于关键词特征自动分解，不依赖 LLM。
    """
    sub_queries = []

    # 策略 1：中英文混合 → 分语言搜索
    has_chinese = any("\u4e00" <= c <= "\u9fff" for c in query)
    has_english = any(c.isascii() and c.isalpha() for c in query)

    if has_chinese and has_english:
        # 提取英文核心词
        eng_words = " ".join(w for w in query.split() if w.isascii() and len(w) > 2)
        if eng_words:
            sub_queries.append({
                "query": eng_words,
                "intent": "英文核心概念搜索",
                "strategy": "english_focused"
            })

    # 策略 2：包含年份/时间 → 补充时效性搜索
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        year = year_match.group()
        sub_queries.append({
            "query": f"{query} {year} latest update",
            "intent": f"{year}年最新进展",
            "strategy": "temporal"
        })

    # 策略 3：包含对比词 → 分别搜索各对象
    compare_match = re.search(r"(?:vs| versus |对比|比较|和|与|及)", query, re.I)
    if compare_match:
        parts = re.split(r"(?:vs| versus |对比|比较|和|与|及)", query, flags=re.I)
        for part in parts[:2]:
            part = part.strip()
            if part and len(part) > 2:
                sub_queries.append({
                    "query": part,
                    "intent": f"独立搜索：{part[:20]}",
                    "strategy": "split_compare"
                })

    # 策略 4：包含「如何/怎么/why」→ 补充教程/方案搜索
    how_match = re.search(r"(?:如何|怎么|how|why|为什么|最佳实践|best practice)", query, re.I)
    if how_match:
        sub_queries.append({
            "query": f"{query} tutorial guide best practices",
            "intent": "教程/最佳实践",
            "strategy": "tutorial"
        })

    # 策略 5：包含「问题/bug/错误」→ 补充社区讨论搜索
    bug_match = re.search(r"(?:bug|error|问题|报错|故障|issue|panic|crash|exception)", query, re.I)
    if bug_match:
        sub_queries.append({
            "query": f"{query} solution fix workaround community",
            "intent": "社区解决方案",
            "strategy": "community_fix"
        })

    # 策略 6：包含「论文/学术」→ 补充学术搜索
    academic_match = re.search(r"(?:论文|paper|arxiv|学术|综述|review|survey|研究)", query, re.I)
    if academic_match:
        sub_queries.append({
            "query": f"{query} arxiv semantic scholar 2024 2025",
            "intent": "学术文献补充",
            "strategy": "academic"
        })

    # 策略 7：包含「安全/CVE」→ 补充安全源
    security_match = re.search(r"(?:CVE|漏洞|vulnerability|security|exploit|PoC)", query, re.I)
    if security_match:
        sub_queries.append({
            "query": f"{query} NVD exploit PoC advisory",
            "intent": "安全数据源补充",
            "strategy": "security"
        })

    # 策略 8：包含「金融/股票/财报」→ 补充金融源
    finance_match = re.search(r"(?:股价|财报|基金|股票|行情|金融|financial|earnings|stock)", query, re.I)
    if finance_match:
        sub_queries.append({
            "query": f"{query} 东方财富 雪球 研报",
            "intent": "金融数据补充",
            "strategy": "finance"
        })

    # 确保至少有原始查询
    if not sub_queries:
        sub_queries.append({
            "query": query,
            "intent": "原始查询",
            "strategy": "direct"
        })

    # 补充通用搜索
    if len(sub_queries) < num_sub:
        sub_queries.append({
            "query": query,
            "intent": "综合搜索",
            "strategy": "general"
        })

    return _deduplicate_sub_queries(sub_queries[:num_sub])


def _deduplicate_sub_queries(sub_queries: list[dict[str, str]]) -> list[dict[str, str]]:
    """基于 Jaccard 相似度去重子查询。"""
    import re as _re
    def _tokens(q: str) -> set:
        return set(_re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', q.lower()))
    unique = []
    seen_tokens = []
    for sq in sub_queries:
        tokens = _tokens(sq["query"])
        is_dup = False
        for prev in seen_tokens:
            jaccard = len(tokens & prev) / max(len(tokens | prev), 1)
            if jaccard > 0.6:
                is_dup = True
                break
        if not is_dup:
            unique.append(sq)
            seen_tokens.append(tokens)
    return unique


# ── 多源采集 ──────────────────────────────────────────────────────────────────

def collect_sources(sub_queries: list[dict[str, str]], max_results: int = 5,
                    timeout: int = 15, depth: str = "balanced",
                    mode: str = "auto") -> dict[str, Any]:
    """对每个子查询并行执行搜索，返回聚合结果。"""
    all_results = []
    engines_used = set()
    sub_results = []
    t0 = time.time()

    def _search_one(sq: dict[str, str]) -> dict[str, Any]:
        result = super_search(
            sq["query"], n=max_results, timeout=timeout,
            depth=depth, mode=mode, skip_cache=False
        )
        return {
            "sub_query": sq["query"],
            "intent": sq["intent"],
            "strategy": sq["strategy"],
            "results": result.get("results", []),
            "engines_used": result.get("engines_used", []),
            "elapsed_ms": result.get("elapsed_ms", 0),
        }

    with ThreadPoolExecutor(max_workers=min(len(sub_queries), 4)) as ex:
        futures = {ex.submit(_search_one, sq): sq for sq in sub_queries}
        all_futures = list(futures.keys())
        try:
            for fut in as_completed(futures, timeout=timeout * 2 + 5):
                try:
                    sr = fut.result()
                    sub_results.append(sr)
                    all_results.extend(sr["results"])
                    engines_used.update(sr["engines_used"])
                except Exception as e:
                    sq = futures[fut]
                    sub_results.append({
                        "sub_query": sq["query"],
                        "intent": sq["intent"],
                        "strategy": sq["strategy"],
                        "results": [],
                        "engines_used": [],
                        "error": str(e),
                        "elapsed_ms": 0,
                    })
        except Exception:
            # 超时后收集已完成的 futures
            for fut in all_futures:
                if fut.done() and not fut.cancelled():
                    try:
                        sr = fut.result()
                        if sr not in sub_results:
                            sub_results.append(sr)
                            all_results.extend(sr["results"])
                            engines_used.update(sr["engines_used"])
                    except Exception:
                        pass

    # RRF 融合
    result_lists = [sr["results"] for sr in sub_results if sr["results"]]
    if len(result_lists) > 1:
        merged = rrf_merge(result_lists)
    elif result_lists:
        merged = deduplicate_by_url(result_lists[0])
    else:
        merged = []

    elapsed = int((time.time() - t0) * 1000)

    return {
        "merged_results": merged[:max_results * 3],
        "sub_results": sub_results,
        "engines_used": sorted(engines_used),
        "total_results": len(merged),
        "elapsed_ms": elapsed,
    }


# ── 知识缺口识别 ──────────────────────────────────────────────────────────────

def identify_gaps(sub_results: list[dict[str, Any]], query: str) -> list[str]:
    """识别搜索结果中的知识缺口。"""
    gaps = []

    # 检查是否有子查询完全失败
    for sr in sub_results:
        if not sr["results"]:
            gaps.append(f"子查询「{sr['intent']}」无结果：{sr['sub_query'][:40]}")

    # 检查是否有子查询结果过少
    for sr in sub_results:
        if sr["results"] and len(sr["results"]) < 2:
            gaps.append(f"子查询「{sr['intent']}」结果稀少（仅 {len(sr['results'])} 条）")

    # 检查来源多样性
    all_sources = set()
    for sr in sub_results:
        for r in sr["results"]:
            src = r.get("source", "")
            if src:
                all_sources.add(src)
    if len(all_sources) < 3:
        gaps.append(f"来源多样性不足：仅 {len(all_sources)} 个引擎有结果（{', '.join(all_sources)}）")

    # 检查时间覆盖
    import re
    year_match = re.search(r"20\d{2}", query)
    if year_match:
        target_year = year_match.group()
        has_recent = False
        for sr in sub_results:
            for r in sr["results"]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")
                if target_year in title or target_year in snippet:
                    has_recent = True
                    break
        if not has_recent:
            gaps.append(f"未找到 {target_year} 年的直接相关内容")

    return gaps


# ── 综合报告 ──────────────────────────────────────────────────────────────────

def synthesize_report(query: str, collection: dict[str, Any],
                      gaps: list[str]) -> dict[str, Any]:
    """生成综合研究报告。"""
    merged = collection["merged_results"]
    sub_results = collection["sub_results"]

    # 按子查询分组的关键发现
    key_findings = []
    for sr in sub_results:
        if sr["results"]:
            best = sr["results"][0]
            key_findings.append({
                "aspect": sr["intent"],
                "strategy": sr["strategy"],
                "top_result": {
                    "title": best.get("title", ""),
                    "url": best.get("url", ""),
                    "snippet": (best.get("snippet", "") or "")[:200],
                    "score": best.get("score", 0),
                    "source": best.get("source", ""),
                },
                "result_count": len(sr["results"]),
            })

    # 来源统计
    source_counts = {}
    for r in merged:
        src = r.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    # 引用列表
    citations = []
    for i, r in enumerate(merged[:15]):
        citations.append({
            "id": f"[{i+1}]",
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "source": r.get("source", ""),
            "score": r.get("score", 0),
        })

    # 交叉引用检测
    all_results = []
    for sr in sub_results:
        all_results.extend(sr["results"])
    cross_refs = detect_cross_references(all_results)

    return {
        "query": query,
        "key_findings": key_findings,
        "total_sources": collection["total_results"],
        "engines_used": collection["engines_used"],
        "source_distribution": source_counts,
        "citations": citations,
        "cross_references": cross_refs,
        "gaps": gaps,
        "elapsed_ms": collection["elapsed_ms"],
        "sub_query_count": len(sub_results),
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

def deep_research(query: str, num_sub_queries: int = 4, max_results: int = 5,
                  timeout: int = 15, depth: str = "balanced",
                  mode: str = "auto") -> dict[str, Any]:
    """执行深度研究。"""
    # 0. 查询改写
    rewrite_result = None
    try:
        from query_rewriter import rewrite_query as do_rewrite
        rewrite_result = do_rewrite(query)
        if rewrite_result["rewritten"] and rewrite_result["confidence"] >= 0.7:
            query = rewrite_result["rewritten"]
    except Exception:
        pass

    # 1. 问题分解
    sub_queries = decompose_query(query, num_sub_queries)

    # 2. 多源采集
    collection = collect_sources(sub_queries, max_results, timeout, depth, mode)

    # 3. 知识缺口
    gaps = identify_gaps(collection["sub_results"], query)

    # 4. 综合报告
    report = synthesize_report(query, collection, gaps)

    if rewrite_result and rewrite_result["rewritten"]:
        report["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }

    return report


# ── 社交舆情研究 ─────────────────────────────────────────────────────────────

def social_sentiment_research(query: str, platforms: list[str] | None = None,
                              max_results: int = 5) -> dict[str, Any]:
    """社交舆情研究：跨平台 UGC 情绪与讨论分析"""
    if platforms is None:
        platforms = ["twitter", "reddit", "xiaohongshu"]

    from search import super_search

    platform_results: dict[str, list] = {}
    all_results: list[dict] = []
    engines_used: set[str] = set()
    t0 = time.time()

    for platform in platforms:
        try:
            result = super_search(query, n=max_results, engines=[platform], mode="fast")
            results = result.get("results", [])
            platform_results[platform] = results
            all_results.extend(results)
            engines_used.update(result.get("engines_used", []))
        except Exception:
            platform_results[platform] = []

    # 互动数据聚合
    engagement_totals = {"likes": 0, "comments": 0, "shares": 0, "views": 0}
    top_topics: dict[str, int] = {}

    for r in all_results:
        meta = r.get("social_meta", {})
        if isinstance(meta, dict):
            engagement_totals["likes"] += meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            engagement_totals["comments"] += meta.get("comments", meta.get("num_comments", 0))
            engagement_totals["shares"] += meta.get("shares", meta.get("retweets", meta.get("reposts_count", 0)))
            engagement_totals["views"] += meta.get("views", meta.get("play_count", 0))
        # 简单话题提取
        title = r.get("title", "")
        for word in title.split():
            if len(word) > 2:
                top_topics[word] = top_topics.get(word, 0) + 1

    top_topics_sorted = sorted(top_topics.items(), key=lambda x: x[1], reverse=True)[:10]
    elapsed = int((time.time() - t0) * 1000)

    return {
        "query": query,
        "mode": "social-sentiment",
        "platforms": platforms,
        "total_posts": len(all_results),
        "platform_breakdown": {p: len(r) for p, r in platform_results.items()},
        "engagement_totals": engagement_totals,
        "top_topics": [{"topic": t, "mentions": c} for t, c in top_topics_sorted],
        "cross_platform_posts": [
            {
                "platform": r.get("source", ""),
                "title": r.get("title", "")[:100],
                "url": r.get("url", ""),
                "snippet": r.get("snippet", "")[:200],
                "social_meta": r.get("social_meta", {}),
            }
            for r in all_results[:15]
        ],
        "engines_used": sorted(engines_used),
        "elapsed_ms": elapsed,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="深度研究工具")
    parser.add_argument("query", help="研究查询")
    parser.add_argument("--sub-queries", type=int, default=4, help="子查询数量")
    parser.add_argument("-n", "--max-results", type=int, default=5, help="每个子查询最大结果数")
    parser.add_argument("--timeout", type=int, default=15, help="超时秒数")
    parser.add_argument("--depth", choices=["fast", "balanced", "deep"], default="balanced")
    parser.add_argument("--mode", choices=["fast", "auto", "deep", "budget", "social-sentiment"], default="auto",
                        help="研究模式：fast/auto/deep/budget/social-sentiment")
    parser.add_argument("--platforms", type=str, default=None,
                        help="社交平台列表（仅 social-sentiment 模式），逗号分隔")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    # 社交舆情模式
    if args.mode == "social-sentiment":
        platforms = [p.strip() for p in args.platforms.split(",")] if args.platforms else None
        report = social_sentiment_research(args.query, platforms, args.max_results)
    else:
        report = deep_research(
            args.query, args.sub_queries, args.max_results,
            args.timeout, args.depth, args.mode
        )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if report.get("mode") == "social-sentiment":
            _print_social_report(report)
        else:
            _print_deep_report(report)


def _print_social_report(report: dict):
    """打印社交舆情报告"""
    print(f"\n{'='*60}")
    print(f"社交舆情分析：{report['query']}")
    print(f"{'='*60}")
    print(f"平台：{', '.join(report['platforms'])} | 引擎：{', '.join(report['engines_used'])}")
    print(f"抓取帖子：{report['total_posts']} | 耗时：{report['elapsed_ms']}ms")
    print()
    print("── 平台分布 ──")
    for platform, count in report["platform_breakdown"].items():
        print(f"  {platform}: {count} 条")
    print()
    print("── 互动数据汇总 ──")
    eng = report["engagement_totals"]
    print(f"  点赞/投票：{eng['likes']:,} | 评论：{eng['comments']:,} | 转发：{eng['shares']:,} | 观看：{eng['views']:,}")
    print()
    print("── 高频讨论话题 ──")
    for topic in report["top_topics"]:
        print(f"  「{topic['topic']}」 ({topic['mentions']} 次)")
    print()
    print("── 代表性内容 ──")
    for post in report["cross_platform_posts"][:5]:
        meta = post.get("social_meta", {})
        engagement = ""
        if isinstance(meta, dict):
            likes = meta.get("likes", meta.get("upvotes", meta.get("attitudes_count", 0)))
            comments = meta.get("comments", meta.get("num_comments", 0))
            engagement = f" | 👍{likes} 💬{comments}"
        print(f"  [{post['platform']}] {post['title'][:60]}{engagement}")
        print(f"    {post['url']}")
        print()


def _print_deep_report(report: dict):
    """打印深度研究报告"""


if __name__ == "__main__":
    main()
