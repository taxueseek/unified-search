#!/usr/bin/env python3
"""
evidence.py — 来源可信度评估工具（wigolo evidence_score 理念移植）

核心能力：
  1. 权威性评分：基于域名白名单/黑名单 + 来源类型分级
  2. 时效性评分：基于内容时间戳 + 搜索时间
  3. 交叉验证：多个来源是否佐证同一结论
  4. 综合可信度：加权计算 + 透明分解

用法：
  python3 evidence.py --urls "https://example.com" "https://example2.com"
  python3 evidence.py --search-result '{"results": [...]}' --query "茅台股价"
  echo '{"results": [...]}' | python3 evidence.py --stdin --query "查询词"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)


# ── 权威性评分 ────────────────────────────────────────────────────────────────

# 域名权威性分级（越高越好）
AUTHORITY_TIERS = {
    # Tier 1: 官方/权威（0.95-1.0）
    "gov.cn": 1.0, "gov": 0.95, "edu.cn": 0.95, "edu": 0.9,
    "ac.cn": 0.95,  # 中国科学院
    "nature.com": 0.95, "science.org": 0.95, "ieee.org": 0.9,
    "acm.org": 0.9, "springer.com": 0.9, "elsevier.com": 0.9,
    "arxiv.org": 0.9, "pubmed.ncbi.nlm.nih.gov": 0.95,
    "scholar.google.com": 0.85, "ncbi.nlm.nih.gov": 0.95,
    "nvd.nist.gov": 0.95,  # 国家漏洞数据库
    "cve.mitre.org": 0.95,

    # Tier 2: 专业媒体/平台（0.75-0.9）
    "zhihu.com": 0.85, "github.com": 0.85, "stackoverflow.com": 0.85,
    "medium.com": 0.75, "dev.to": 0.75,
    "reuters.com": 0.9, "bloomberg.com": 0.9, "wsj.com": 0.9,
    "财新": 0.9, "caixin.com": 0.9,
    "36kr.com": 0.8, "infoq.cn": 0.8, "juejin.cn": 0.75,
    "eastmoney.com": 0.85, "xueqiu.com": 0.8,
    "docs.python.org": 0.9, "react.dev": 0.9, "nextjs.org": 0.9,

    # Tier 3: 通用可信（0.6-0.75）
    "wikipedia.org": 0.75, "baike.baidu.com": 0.7,
    "维基百科": 0.75, "百度百科": 0.7,
    "linkedin.com": 0.65, "twitter.com": 0.55, "x.com": 0.55,
    "reddit.com": 0.65, "hackernews": 0.7,

    # Tier 4: 内容农场/低质（0.2-0.4）
    "sohu.com": 0.4, "163.com": 0.45, "sina.com.cn": 0.5,
    "baijiahao.baidu.com": 0.35, "zhuanlan.zhihu.com": 0.7,
    "toutiao.com": 0.4, "weixin.qq.com": 0.5,

    # Tier 5: 已知低质（0.1-0.2）
    "content-farm": 0.1, "seo-spam": 0.1,
}

# 来源类型映射
SOURCE_TYPE_MAP = {
    "eastmoney": ("金融数据", 0.9),
    "zhihu": ("社区观点", 0.8),
    "arxiv": ("学术预印本", 0.85),
    "semantic_scholar": ("学术索引", 0.9),
    "openalex": ("学术索引", 0.85),
    "crossref": ("学术元数据", 0.9),
    "github": ("代码仓库", 0.85),
    "byted": ("中文搜索", 0.7),
    "bocha": ("中文搜索", 0.7),
    "tavily": ("AI搜索", 0.75),
    "felo": ("AI搜索", 0.7),
    "duckduckgo": ("通用搜索", 0.65),
    "wikipedia": ("百科", 0.75),
    "anysearch": ("垂直搜索", 0.75),
    "wigolo": ("本地搜索", 0.7),
    "metaso": ("AI搜索", 0.75),
}


def score_authority(url: str, source: str = "") -> dict[str, Any]:
    """评估 URL 的权威性。"""
    if not url:
        return {"score": 0.3, "reason": "无 URL", "tier": "unknown"}

    parsed = urlparse(url)
    domain = parsed.netloc.lower().strip("www.")
    path = parsed.path.lower()

    # 精确匹配域名
    best_score = 0.5
    best_reason = "通用域名"

    for pattern, score in AUTHORITY_TIERS.items():
        if domain == pattern or domain.endswith("." + pattern) or pattern in domain:
            if score > best_score:
                best_score = score
                best_reason = f"域名匹配：{pattern}"

    # 路径特征加分
    if "/docs/" in path or "/documentation/" in path:
        best_score = min(best_score + 0.05, 1.0)
        best_reason += "（文档路径）"
    if "/paper/" in path or "/arxiv/" in path or "/abs/" in path:
        best_score = min(best_score + 0.05, 1.0)
        best_reason += "（论文路径）"
    if "/issues/" in path or "/pull/" in path:
        best_score = min(best_score + 0.03, 1.0)
        best_reason += "（Issue/PR路径）"

    # 来源类型加分
    if source and source in SOURCE_TYPE_MAP:
        type_name, type_score = SOURCE_TYPE_MAP[source]
        if type_score > best_score:
            best_score = type_score
            best_reason = f"来源类型：{type_name}"

    tier = "high" if best_score >= 0.8 else "medium" if best_score >= 0.6 else "low" if best_score >= 0.4 else "very_low"

    return {
        "score": round(best_score, 2),
        "reason": best_reason,
        "tier": tier,
        "domain": domain,
    }


# ── 时效性评分 ────────────────────────────────────────────────────────────────

def score_freshness(result: dict[str, Any], query_time: float = None) -> dict[str, Any]:
    """评估结果的时效性。"""

    def _extract_year_from_url(url: str):
        """从 URL 路径提取年份。"""
        patterns = [
            r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})",
            r"/(20\d{2})[/-](\d{1,2})",
            r"/(20\d{2})/",
        ]
        for p in patterns:
            m = re.search(p, url)
            if m:
                return int(m.group(1))
        return None

    if query_time is None:
        query_time = time.time()

    snippet = result.get("snippet", "") or ""
    title = result.get("title", "") or ""
    url = result.get("url", "") or ""
    combined = f"{title} {snippet}"

    # 提取时间信息
    # 年份
    year_match = re.search(r"20(\d{2})", combined)
    if year_match:
        year = 2000 + int(year_match.group(1))
        from datetime import datetime
        age_years = datetime.now().year - year
        if age_years <= 0:
            score = 1.0
            reason = f"{year}年（今年）"
        elif age_years == 1:
            score = 0.9
            reason = f"{year}年（去年）"
        elif age_years <= 2:
            score = 0.7
            reason = f"{year}年（{age_years}年前）"
        elif age_years <= 5:
            score = 0.5
            reason = f"{year}年（{age_years}年前）"
        else:
            score = 0.3
            reason = f"{year}年（{age_years}年前，较旧）"
    else:
        # 无时间信息，尝试从 URL 提取
        url_year = _extract_year_from_url(url)
        if url_year:
            from datetime import datetime
            age_years = datetime.now().year - url_year
            if age_years <= 1:
                score = 0.8
                reason = f"URL含{url_year}年"
            elif age_years <= 3:
                score = 0.6
                reason = f"URL含{url_year}年（{age_years}年前）"
            else:
                score = 0.4
                reason = f"URL含{url_year}年（较旧）"
        else:
            score = 0.5
            reason = "无明确时间标记"

    # 时效性关键词加分
    if re.search(r"(最新|latest|recent|breaking|just|刚刚|今日|today)", combined, re.I):
        score = min(score + 0.1, 1.0)
        reason += "（含时效关键词）"

    return {
        "score": round(score, 2),
        "reason": reason,
    }


# ── 交叉验证 ──────────────────────────────────────────────────────────────────

def cross_validate(results: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """多来源交叉验证。"""
    if len(results) < 2:
        return {
            "corroboration_level": "insufficient",
            "score": 0.3,
            "detail": f"仅 {len(results)} 个结果，无法交叉验证",
            "agreement_count": 0,
            "total_sources": len(results),
        }

    # 检查 URL 重叠（不同来源指向同一页面 = 强佐证）
    urls = [r.get("url", "") for r in results if r.get("url")]
    unique_urls = set(urls)
    url_overlap = 1 - (len(unique_urls) / max(len(urls), 1))

    # 检查域名多样性
    domains = set()
    for r in results:
        url = r.get("url", "")
        if url:
            domain = urlparse(url).netloc.lower().strip("www.")
            domains.add(domain)

    # 检查标题/内容相似度（简单关键词匹配）
    query_words = set(query.lower().split())
    content_matches = 0
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        if any(w in text for w in query_words if len(w) > 1):
            content_matches += 1

    match_ratio = content_matches / max(len(results), 1)

    # 计算佐证等级
    if match_ratio >= 0.8 and len(domains) >= 3:
        level = "strong"
        score = 0.9
    elif match_ratio >= 0.6 and len(domains) >= 2:
        level = "moderate"
        score = 0.7
    elif match_ratio >= 0.4:
        level = "weak"
        score = 0.5
    else:
        level = "minimal"
        score = 0.3

    return {
        "corroboration_level": level,
        "score": round(score, 2),
        "detail": f"{content_matches}/{len(results)} 个结果与查询相关，{len(domains)} 个独立域名",
        "agreement_count": content_matches,
        "total_sources": len(results),
        "unique_domains": len(domains),
        "url_overlap": round(url_overlap, 2),
    }


# ── 综合可信度 ────────────────────────────────────────────────────────────────

def compute_credibility(results: list[dict[str, Any]], query: str) -> dict[str, Any]:
    """计算每个结果的综合可信度评分。"""
    query_time = time.time()
    scored_results = []

    for r in results:
        url = r.get("url", "")
        source = r.get("source", "")

        auth = score_authority(url, source)
        fresh = score_freshness(r, query_time)

        # 综合公式：权威性 0.5 + 时效性 0.3 + 原始分数 0.2
        original_score = r.get("score", 0.5) or 0.5
        credibility = (
            auth["score"] * 0.5 +
            fresh["score"] * 0.3 +
            original_score * 0.2
        )

        scored_results.append({
            "title": r.get("title", ""),
            "url": url,
            "source": source,
            "snippet": (r.get("snippet", "") or "")[:150],
            "credibility": {
                "final": round(credibility, 3),
                "authority": auth,
                "freshness": fresh,
                "original_score": original_score,
            },
        })

    # 交叉验证
    cross = cross_validate(results, query)

    # 排序
    scored_results.sort(key=lambda x: x["credibility"]["final"], reverse=True)

    return {
        "query": query,
        "results": scored_results,
        "cross_validation": cross,
        "summary": {
            "total": len(scored_results),
            "high_credibility": sum(1 for r in scored_results if r["credibility"]["final"] >= 0.7),
            "medium_credibility": sum(1 for r in scored_results if 0.5 <= r["credibility"]["final"] < 0.7),
            "low_credibility": sum(1 for r in scored_results if r["credibility"]["final"] < 0.5),
            "best_source": scored_results[0] if scored_results else None,
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="来源可信度评估工具")
    parser.add_argument("query", nargs="?", default="", help="搜索查询词")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读取 JSON 搜索结果")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.stdin:
        data = json.load(sys.stdin)
        results = data.get("results", [])
    else:
        # 从搜索结果文件读取
        results = []

    if not results and args.query:
        # 无输入结果时，输出提示
        print("需要提供搜索结果进行评估。用法：")
        print('  echo \'{"results": [...]}\' | python3 evidence.py --stdin --query "查询词"')
        sys.exit(1)

    report = compute_credibility(results, args.query)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n来源可信度评估：{report['query']}")
        print(f"{'='*50}")
        for r in report["results"]:
            c = r["credibility"]
            level = "🟢" if c["final"] >= 0.7 else "🟡" if c["final"] >= 0.5 else "🔴"
            print(f"{level} [{c['final']:.2f}] {r['title'][:50]}")
            print(f"   权威：{c['authority']['score']:.2f} ({c['authority']['reason']})")
            print(f"   时效：{c['freshness']['score']:.2f} ({c['freshness']['reason']})")
            print()

        cv = report["cross_validation"]
        print(f"交叉验证：{cv['corroboration_level']} (score={cv['score']:.2f})")
        print(f"  {cv['detail']}")

        s = report["summary"]
        print(f"\n总结：🟢高={s['high_credibility']} 🟡中={s['medium_credibility']} 🔴低={s['low_credibility']}")


if __name__ == "__main__":
    main()
