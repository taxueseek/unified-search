#!/usr/bin/env python3
"""
evidence.py — 来源可信度评估（v2.2 两阶段：Selection × Absorption）

问题重定义（第一性）：
  Agent 要的不是「搜到了什么」，而是「哪些材料可被吸收进可核验答案」。
  被检索到（selection）≠ 被采用为证据（absorption）。

MECE 分解：
  A. Selection 门槛：URL 是否可引用页面 / 域名权威 / 是否 SERP·榜单污染
  B. Absorption 深度：数字·定义·对比·披露 等证据块密度（snippet 或正文）
  C. Freshness：发布年/URL 年；忽略「2015年以来」类历史对比年
  D. Consensus：多域名佐证（交叉验证）

综合：
  final = 0.40 * selection + 0.35 * absorption + 0.15 * freshness + 0.10 * original
  high-stakes 可再 × consensus 调节（见 compute_credibility）

用法：
  echo '{"results": [...]}' | python3 evidence.py "查询词" --stdin --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKENDS_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "..", "backends"))
sys.path.insert(0, SCRIPT_DIR)

try:
    from content_signals import score_evidence_density
except ImportError:  # pragma: no cover
    def score_evidence_density(text: str, title: str = "") -> dict[str, Any]:
        return {
            "has_numbers": False,
            "has_definition": False,
            "has_comparison": False,
            "has_howto": False,
            "has_disclose": False,
            "is_qa_format": False,
            "absorption_score": 0.2,
        }


# ── 权威性评分 ────────────────────────────────────────────────────────────────

AUTHORITY_TIERS = {
    # Tier 1: 官方/权威
    "gov.cn": 1.0, "gov": 0.95, "edu.cn": 0.95, "edu": 0.9,
    "ac.cn": 0.95,
    "nature.com": 0.95, "science.org": 0.95, "ieee.org": 0.9,
    "acm.org": 0.9, "springer.com": 0.9, "elsevier.com": 0.9,
    "arxiv.org": 0.9, "pubmed.ncbi.nlm.nih.gov": 0.95,
    "scholar.google.com": 0.85, "ncbi.nlm.nih.gov": 0.95,
    "nvd.nist.gov": 0.95, "cve.mitre.org": 0.95,
    "xinhuanet.com": 0.95, "people.com.cn": 0.95,

    # Tier 2: 专业媒体/平台
    "zhihu.com": 0.85, "github.com": 0.85, "stackoverflow.com": 0.85,
    "medium.com": 0.75, "dev.to": 0.75,
    "reuters.com": 0.9, "bloomberg.com": 0.9, "wsj.com": 0.9,
    "caixin.com": 0.9, "yicai.com": 0.86, "wallstreetcn.com": 0.86,
    "cls.cn": 0.88, "api3.cls.cn": 0.85, "cs.com.cn": 0.92, "stcn.com": 0.88,
    "cnr.cn": 0.9, "thepaper.cn": 0.82, "jiemian.com": 0.8,
    "36kr.com": 0.8, "infoq.cn": 0.8, "juejin.cn": 0.75,
    "eastmoney.com": 0.85, "xueqiu.com": 0.8, "10jqka.com.cn": 0.78,
    "docs.python.org": 0.9, "react.dev": 0.9, "nextjs.org": 0.9,

    # Tier 3: 通用可信
    "wikipedia.org": 0.75, "baike.baidu.com": 0.7,
    "linkedin.com": 0.65, "twitter.com": 0.45, "x.com": 0.45,
    "reddit.com": 0.55,

    # Tier 4: 内容农场/低质
    "sohu.com": 0.4, "163.com": 0.45, "sina.com.cn": 0.55,
    "k.sina.com.cn": 0.55, "baijiahao.baidu.com": 0.35,
    "zhuanlan.zhihu.com": 0.7, "toutiao.com": 0.4, "weixin.qq.com": 0.5,
    "guba.eastmoney.com": 0.35,

    # 商业榜单（高引用≠高可信）
    "maigoo.com": 0.28, "chinapp.com": 0.28, "cnpp.cn": 0.28,
}

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
    "local_search": ("本地搜索", 0.7),
    "metaso": ("AI搜索", 0.75),
    "local_baidu": ("搜索结果页聚合", 0.35),
    "local_sogou": ("搜索结果页聚合", 0.35),
    "local_bing": ("搜索结果页聚合", 0.4),
}

_CN_OVERRIDES: dict[str, Any] | None = None


def _load_cn_source_types() -> dict[str, Any]:
    global _CN_OVERRIDES
    if _CN_OVERRIDES is not None:
        return _CN_OVERRIDES
    path = os.path.join(BACKENDS_DIR, "source_types_cn.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            _CN_OVERRIDES = json.load(f)
    except Exception:
        _CN_OVERRIDES = {}
    return _CN_OVERRIDES


def _normalize_domain(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if ":" in host:
        host = host.split(":", 1)[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def is_serp_or_jump_url(url: str) -> bool:
    """是否为搜索引擎结果页 / 跳转壳（不可当信源正文）。"""
    if not url:
        return True
    low = url.lower()
    cfg = _load_cn_source_types()
    for pat in cfg.get("serp_url_patterns") or []:
        if pat.lower() in low:
            return True
    # baidu 相关搜索页
    if "baidu.com/s?" in low or "baidu.com/s?" in low.replace("&", "?"):
        return True
    if re.search(r"baidu\.com/s\?", low):
        return True
    host = _normalize_domain(url)
    # 纯搜索 host 且 path 像搜索
    try:
        path = urlparse(url).path or ""
    except Exception:
        path = ""
    if host in ("baidu.com", "www.baidu.com", "sogou.com", "www.sogou.com", "so.com"):
        if path in ("", "/", "/s", "/web") or "link" in path or path.startswith("/s"):
            return True
    return False


def score_authority(url: str, source: str = "") -> dict[str, Any]:
    """评估 URL 的权威性（Selection 门槛）。"""
    if not url:
        return {"score": 0.3, "reason": "无 URL", "tier": "unknown", "domain": "", "is_serp": True}

    domain = _normalize_domain(url)
    is_serp = is_serp_or_jump_url(url)

    if is_serp:
        return {
            "score": 0.12,
            "reason": "搜索结果页/跳转链（不可作吸收源）",
            "tier": "very_low",
            "domain": domain,
            "is_serp": True,
        }

    best_score = 0.5
    best_reason = "通用域名"
    cfg = _load_cn_source_types()

    # 精确/后缀匹配 AUTHORITY_TIERS
    for pattern, score in AUTHORITY_TIERS.items():
        if domain == pattern or domain.endswith("." + pattern):
            if score > best_score or domain == pattern or domain.endswith("." + pattern):
                # 取更具体匹配优先：更长 pattern 优先
                if domain == pattern or len(pattern) >= len(best_reason):
                    best_score = score
                    best_reason = f"域名匹配：{pattern}"

    # 重新做最长后缀匹配（修复上面逻辑）
    best_score = 0.5
    best_reason = "通用域名"
    best_len = -1
    for pattern, score in AUTHORITY_TIERS.items():
        if domain == pattern or domain.endswith("." + pattern):
            if len(pattern) > best_len:
                best_len = len(pattern)
                best_score = score
                best_reason = f"域名匹配：{pattern}"

    # JSON 覆盖
    for d, score in (cfg.get("authority_overrides") or {}).items():
        if domain == d or domain.endswith("." + d):
            if len(d) >= best_len:
                best_len = len(d)
                best_score = float(score)
                best_reason = f"中文信源表：{d}"

    demote = cfg.get("demote_domains") or {}
    for d, meta in demote.items():
        if domain == d or domain.endswith("." + d):
            best_score = float(meta.get("score", 0.3))
            best_reason = f"降权：{meta.get('reason', d)}"
            break

    social = cfg.get("social_narrative_only") or {}
    for d, score in social.items():
        if domain == d or domain.endswith("." + d):
            best_score = min(best_score, float(score))
            best_reason = f"社交/叙事源（非事实真值）：{d}"
            break

    # 路径特征
    try:
        path = urlparse(url).path.lower()
    except Exception:
        path = ""
    if "/docs/" in path or "/documentation/" in path:
        best_score = min(best_score + 0.05, 1.0)
        best_reason += "（文档路径）"
    if "/paper/" in path or "/arxiv/" in path or "/abs/" in path:
        best_score = min(best_score + 0.05, 1.0)
        best_reason += "（论文路径）"

    # 来源类型：仅当域名仍是「通用默认 0.5」时可上调；
    # 已明确高/低分的域名（白名单/降权表）不被 anysearch 等抬平
    if source and source in SOURCE_TYPE_MAP:
        type_name, type_score = SOURCE_TYPE_MAP[source]
        domain_was_default = best_reason == "通用域名"
        if (
            domain_was_default
            and type_score > best_score
            and not source.startswith("local_")
        ):
            best_score = type_score
            best_reason = f"来源类型：{type_name}"
        elif not domain_was_default:
            best_reason += f"（引擎={source}）"

    tier = (
        "high" if best_score >= 0.8
        else "medium" if best_score >= 0.6
        else "low" if best_score >= 0.4
        else "very_low"
    )

    return {
        "score": round(best_score, 2),
        "reason": best_reason,
        "tier": tier,
        "domain": domain,
        "is_serp": False,
    }


# ── 时效性评分 ────────────────────────────────────────────────────────────────

# 「2015年以来」等历史对比年 — 不当作发布年
_HIST_SINCE_RE = re.compile(
    r"(?:自|从)?(19|20)\d{2}\s*年?\s*(?:以来|起|至今|至|到现在|之前|以前|以前水平)"
)
_FULL_DATE_RE = re.compile(
    r"(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})"
)
_PUBLISH_HINT_RE = re.compile(
    r"(?:发布|披露|刊登|更新|Published|发表)[^\n]{0,12}(20\d{2})"
    r"|(20\d{2})\s*年\s*(?:[0-1]?\d\s*月)?\s*(?:[0-3]?\d\s*日)?\s*(?:电|讯|报道)"
)


def _years_from_text(text: str) -> list[int]:
    """提取候选年份，排除历史对比语境中的旧年。"""
    if not text:
        return []
    # 挖掉「2015年以来」整段，避免污染
    cleaned = _HIST_SINCE_RE.sub(" ", text)
    # 中文语境不用 \b（「2026年」左侧无词界）
    years = [int(y) for y in re.findall(r"(?<![0-9])(20\d{2})(?![0-9])", cleaned)]
    # 完整日期优先（用原文，保留结构）
    for m in _FULL_DATE_RE.finditer(text):
        years.append(int(m.group(1)))
    for m in _PUBLISH_HINT_RE.finditer(text):
        for g in m.groups():
            if g and re.fullmatch(r"20\d{2}", g):
                years.append(int(g))
    # 合理范围
    now_y = datetime.now().year
    return [y for y in years if 1990 <= y <= now_y + 1]


def score_freshness(result: dict[str, Any], query_time: float = None) -> dict[str, Any]:
    """评估结果的时效性（优先完整日期 / URL 年 / 最新合理年）。"""
    if query_time is None:
        query_time = time.time()

    snippet = result.get("snippet", "") or ""
    title = result.get("title", "") or ""
    url = result.get("url", "") or ""
    combined = f"{title} {snippet}"
    now_y = datetime.now().year

    # 1) 完整日期
    full = list(_FULL_DATE_RE.finditer(combined))
    if full:
        # 取最「新」的完整日期
        best = max(full, key=lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3))))
        year = int(best.group(1))
        age_years = now_y - year
        score, reason = _age_to_score(age_years, year, precise=True)
    else:
        years = _years_from_text(combined)
        url_year = None
        m = re.search(r"/(20\d{2})(?:/|-)", url)
        if m:
            url_year = int(m.group(1))
            years.append(url_year)

        if years:
            # 优先靠近今年的年份（发布语境），而非文中最早的历史年
            # 若存在今年或去年，取 max；否则取 max 全体
            recent = [y for y in years if y >= now_y - 1]
            year = max(recent) if recent else max(years)
            age_years = now_y - year
            score, reason = _age_to_score(age_years, year, precise=False)
            if url_year and year == url_year:
                reason += "（URL年）"
        else:
            score = 0.5
            reason = "无明确时间标记"

    if re.search(r"(最新|latest|recent|breaking|just|刚刚|今日|today|本周|本月)", combined, re.I):
        score = min(score + 0.1, 1.0)
        reason += "（含时效关键词）"

    return {"score": round(score, 2), "reason": reason}


def _age_to_score(age_years: int, year: int, precise: bool) -> tuple[float, str]:
    if age_years < 0:
        return 0.4, f"{year}年（未来年份）"
    if age_years == 0:
        return (0.9 if precise else 0.85), f"{year}年（今年）"
    if age_years == 1:
        return 0.8, f"{year}年（去年）"
    if age_years <= 2:
        return 0.65, f"{year}年（{age_years}年前）"
    if age_years <= 5:
        return 0.45, f"{year}年（{age_years}年前）"
    return 0.25, f"{year}年（{age_years}年前，较旧）"


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
            "unique_domains": 0,
            "url_overlap": 0.0,
            "content_domains": 0,
        }

    urls = [r.get("url", "") for r in results if r.get("url")]
    unique_urls = set(urls)
    url_overlap = 1 - (len(unique_urls) / max(len(urls), 1))

    domains = set()
    content_domains = set()
    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        domain = _normalize_domain(url)
        domains.add(domain)
        if not is_serp_or_jump_url(url):
            content_domains.add(domain)

    query_words = set(query.lower().split())
    content_matches = 0
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
        if any(w in text for w in query_words if len(w) > 1):
            content_matches += 1

    match_ratio = content_matches / max(len(results), 1)
    n_content = len(content_domains)

    if match_ratio >= 0.8 and n_content >= 3:
        level, score = "strong", 0.9
    elif match_ratio >= 0.6 and n_content >= 2:
        level, score = "moderate", 0.7
    elif match_ratio >= 0.4:
        level, score = "weak", 0.5
    else:
        level, score = "minimal", 0.3

    return {
        "corroboration_level": level,
        "score": round(score, 2),
        "detail": (
            f"{content_matches}/{len(results)} 与查询相关，"
            f"{n_content} 个可吸收域名（总域名 {len(domains)}）"
        ),
        "agreement_count": content_matches,
        "total_sources": len(results),
        "unique_domains": len(domains),
        "content_domains": n_content,
        "url_overlap": round(url_overlap, 2),
    }


# ── 综合可信度 ────────────────────────────────────────────────────────────────

def compute_credibility(
    results: list[dict[str, Any]],
    query: str,
    *,
    high_stakes: bool = False,
) -> dict[str, Any]:
    """两阶段综合可信度：Selection × Absorption + Freshness。"""
    query_time = time.time()
    scored_results: list[dict[str, Any]] = []

    for r in results:
        url = r.get("url", "")
        source = r.get("source", "")
        title = r.get("title", "") or ""
        snippet = r.get("snippet", "") or ""

        auth = score_authority(url, source)
        fresh = score_freshness(r, query_time)
        density = score_evidence_density(snippet, title)

        # Selection：权威为主；SERP 直接压到低分
        selection = auth["score"]
        if auth.get("is_serp"):
            selection = min(selection, 0.15)

        # Absorption：证据密度
        absorption = density["absorption_score"]

        original_score = r.get("score", 0.5) or 0.5
        # MECE 加权（量化）
        credibility = (
            selection * 0.40
            + absorption * 0.35
            + fresh["score"] * 0.15
            + float(original_score) * 0.10
        )

        scored_results.append({
            "title": title,
            "url": url,
            "source": source,
            "snippet": snippet[:180],
            "credibility": {
                "final": round(credibility, 3),
                "selection": round(selection, 3),
                "absorption": round(absorption, 3),
                "authority": auth,
                "freshness": fresh,
                "evidence_density": density,
                "original_score": original_score,
            },
        })

    cross = cross_validate(results, query)

    if high_stakes and scored_results:
        # 共识调节：强佐证略抬高 top 可吸收源；弱共识不抬
        boost = 0.0
        if cross["corroboration_level"] == "strong":
            boost = 0.05
        elif cross["corroboration_level"] == "moderate":
            boost = 0.02
        for item in scored_results:
            if not item["credibility"]["authority"].get("is_serp"):
                item["credibility"]["final"] = round(
                    min(item["credibility"]["final"] + boost, 1.0), 3
                )
            item["credibility"]["consensus_boost"] = boost

    scored_results.sort(key=lambda x: x["credibility"]["final"], reverse=True)

    return {
        "query": query,
        "framework": "selection_x_absorption_v2.2",
        "results": scored_results,
        "cross_validation": cross,
        "summary": {
            "total": len(scored_results),
            "high_credibility": sum(
                1 for r in scored_results if r["credibility"]["final"] >= 0.7
            ),
            "medium_credibility": sum(
                1 for r in scored_results if 0.5 <= r["credibility"]["final"] < 0.7
            ),
            "low_credibility": sum(
                1 for r in scored_results if r["credibility"]["final"] < 0.5
            ),
            "serp_filtered": sum(
                1 for r in scored_results
                if r["credibility"]["authority"].get("is_serp")
            ),
            "best_source": scored_results[0] if scored_results else None,
        },
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="来源可信度评估工具（Selection×Absorption）")
    parser.add_argument("query", nargs="?", default="", help="搜索查询词")
    parser.add_argument("--stdin", action="store_true", help="从 stdin 读取 JSON 搜索结果")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    parser.add_argument(
        "--high-stakes",
        action="store_true",
        help="高后果模式：启用共识微调",
    )
    args = parser.parse_args()

    if args.stdin:
        data = json.load(sys.stdin)
        results = data.get("results", [])
    else:
        results = []

    if not results and args.query:
        print("需要提供搜索结果进行评估。用法：")
        print('  echo \'{"results": [...]}\' | python3 evidence.py --stdin --query "查询词"')
        sys.exit(1)

    report = compute_credibility(
        results, args.query, high_stakes=args.high_stakes
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"\n来源可信度评估：{report['query']}  [{report['framework']}]")
        print(f"{'='*50}")
        for r in report["results"]:
            c = r["credibility"]
            level = "🟢" if c["final"] >= 0.7 else "🟡" if c["final"] >= 0.5 else "🔴"
            ed = c.get("evidence_density") or {}
            flags = []
            if ed.get("has_numbers"):
                flags.append("数")
            if ed.get("has_comparison"):
                flags.append("比")
            if ed.get("has_definition"):
                flags.append("义")
            if c["authority"].get("is_serp"):
                flags.append("SERP")
            flag_s = ",".join(flags) if flags else "-"
            print(f"{level} [{c['final']:.2f}] sel={c['selection']:.2f} abs={c['absorption']:.2f} [{flag_s}] {r['title'][:48]}")
            print(f"   权威：{c['authority']['score']:.2f} ({c['authority']['reason']})")
            print(f"   时效：{c['freshness']['score']:.2f} ({c['freshness']['reason']})")
            print()

        cv = report["cross_validation"]
        print(f"交叉验证：{cv['corroboration_level']} (score={cv['score']:.2f})")
        print(f"  {cv['detail']}")

        s = report["summary"]
        print(
            f"\n总结：🟢高={s['high_credibility']} 🟡中={s['medium_credibility']} "
            f"🔴低={s['low_credibility']} SERP={s.get('serp_filtered', 0)}"
        )


if __name__ == "__main__":
    main()
