#!/usr/bin/env python3
"""
route.py — Unified Search v2 三层路由决策

路由策略：
  1. 用户指定引擎 → 直接返回
  2. TF-IDF 语义路由（二元组 + boost + cost + quota）
  3. 正则硬规则匹配（config.yaml domains）
  4. 融合决策：正则 + TF-IDF 验证 → 高置信度
  5. budget 模式：过滤付费引擎

每种决策都带 reason 字符串。
"""

from __future__ import annotations

import re
import time
from typing import Any

try:
    from config import load_config, get_engines, get_domains, get_cost_factor
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines, get_domains, get_cost_factor
    from tfidf_router import semantic_route, get_router
    from quota import get_quota_manager

# 自适应学习（可选依赖）
try:
    from adaptive import get_learner
    _adaptive_learner = get_learner()
except Exception:
    _adaptive_learner = None


# ── 特征提取 ──────────────────────────────────────────────────────────────────

_RE_CHINESE = re.compile(r"[一-鿿]")
_RE_COMPARE = re.compile(r"\b(vs|versus)\b|(对比|比较|区别|相比|哪个好)", re.I)
_RE_TECH = re.compile(
    r"\b(api|python|javascript|typescript|code|react|vue|node|rust|go|"
    r"golang|docker|kubernetes|linux|git|sql|error|bug|debug|exception|"
    r"function|class|async|thread|database|algorithm|programming|framework|library)\b|"
    r"(函数|方法|类|库|框架|报错|调试|编程|代码|开发|技术|源码|架构)", re.I)
_RE_QUESTION = re.compile(
    r"\b(how|what|why|when|where|which|who)\b|"
    r"(怎么|什么|为什么|如何|哪里|哪个|谁|多少|几|吗|呢)", re.I)
_RE_DEPTH = re.compile(
    r"\b(deep|comprehensive|review|survey|research|paper|thesis)\b|"
    r"(对比分析|深度|全面|详细|深入|系统|完整|综述|研究|探究|详解|论文)", re.I)

_ENGINE_NAMES = {
    "anysearch": "AnySearch", "wigolo": "Wigolo", "tavily": "Tavily",
    "zhihu": "知乎", "eastmoney": "东方财富", "byted": "字节搜索",
    "arxiv": "arXiv", "searxng": "SearXNG", "felo": "Felo",
    "bocha": "博查", "openalex": "OpenAlex", "crossref": "Crossref",
    "github": "GitHub", "wikipedia": "Wikipedia", "metaso": "秘塔",
    "wolframalpha": "WolframAlpha", "brave": "Brave",
    "duckduckgo": "DuckDuckGo", "uapi": "UAPI", "semantic_scholar": "Semantic Scholar",
}


def extract_features(query: str) -> dict[str, Any]:
    """提取查询特征向量。"""
    total = len(query)
    chinese = len(_RE_CHINESE.findall(query))
    ratio = chinese / max(total, 1)
    return {
        "chinese_ratio": ratio,
        "english_ratio": 1.0 - ratio,
        "length": total,
        "has_compare": bool(_RE_COMPARE.search(query)),
        "has_technical": bool(_RE_TECH.search(query)),
        "has_question": bool(_RE_QUESTION.search(query)),
        "has_depth_word": bool(_RE_DEPTH.search(query)),
    }


def _feature_labels(features: dict[str, Any]) -> str:
    labels = []
    cr = features.get("chinese_ratio", 0)
    if cr > 0.6:
        labels.append("中文")
    elif cr < 0.1:
        labels.append("英文")
    for key, name in (("has_technical", "技术向"), ("has_compare", "对比分析"),
                      ("has_depth_word", "深度研究"), ("has_question", "问答型")):
        if features.get(key):
            labels.append(name)
    return " + ".join(labels) if labels else "通用查询"


# ── 域匹配 ────────────────────────────────────────────────────────────────────

def _compile_domain_patterns(domains: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compiled = []
    for idx, domain in enumerate(domains):
        patterns = domain.get("patterns", [])
        if isinstance(patterns, str):
            patterns = []
        regexes = []
        for p in patterns:
            try:
                regexes.append(re.compile(p))
            except re.error:
                continue
        compiled.append({**domain, "_idx": idx, "_compiled": regexes})
    return compiled


def match_domain(query: str, domains: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """按 config.yaml domains 顺序匹配；无命中返回 catch-all。"""
    if domains is None:
        domains = get_domains()
    compiled = _compile_domain_patterns(domains)
    catch_all: dict[str, Any] | None = None
    for domain in compiled:
        if not domain.get("patterns", []):
            catch_all = domain
            continue
        for regex in domain["_compiled"]:
            if regex.search(query):
                return domain
    return catch_all


def _get_engines_combo(domain: dict[str, Any], enabled: set[str], mode: str = "auto") -> list[str]:
    """从域配置获取 engines_combo，过滤不可用/付费（budget 模式）。"""
    combo = domain.get("engines_combo", [])
    if combo:
        filtered = [e for e in combo if e in enabled]
    else:
        primary = domain.get("primary", "anysearch")
        fallback = domain.get("fallback")
        engines = [primary]
        if fallback and fallback != primary:
            engines.append(fallback)
        filtered = [e for e in engines if e in enabled]

    # budget/fast 模式过滤付费引擎
    if mode in ("fast", "budget"):
        quota_mgr = get_quota_manager()
        filtered = [e for e in filtered if quota_mgr.is_available(e, mode=mode)]

    # fast/budget 模式优先前置 local_search（零成本兜底）
    if mode in ("fast", "budget") and "local_search" in enabled and "local_search" not in filtered:
        filtered.insert(0, "local_search")

    # fast 模式：只保留免费/本地引擎，最多 2 个，确保速度和零成本
    if mode == "fast":
        from config import get_cost_factor
        filtered = [e for e in filtered if get_cost_factor(e) >= 0.85]
        if "local_search" in filtered:
            filtered.remove("local_search")
            filtered.insert(0, "local_search")
        filtered = filtered[:2]

    # 自适应学习过滤：排除近期表现差的引擎（成功率 < 30% 或综合评分 < 0.3）
    if _adaptive_learner is not None and len(filtered) > 1:
        original = filtered[:]
        filtered = [e for e in filtered if _adaptive_learner.get_score(e) >= 0.3]
        if not filtered:
            filtered = original
        elif len(filtered) < len(original) and "local_search" in enabled and "local_search" not in filtered:
            filtered.append("local_search")

    # 健康探针过滤：排除已知不可达的 HTTP 引擎
    try:
        from health_probe import get_engine_status
        healthy = [e for e in filtered if get_engine_status(e).get("available", True)]
        if healthy:
            filtered = healthy
    except Exception:
        pass  # 健康探针模块不可用时跳过

    return filtered


# ── 路由主函数 ─────────────────────────────────────────────────────────────────

def route_query(query: str, engine_override: str = "auto",
                mode: str = "auto") -> dict[str, Any]:
    """路由决策主函数。

    Args:
        query: 查询词
        engine_override: 用户指定引擎
        mode: 预算模式 (fast/auto/deep/budget)

    Returns:
        dict: {engine, engines, engines_combo, reason, confidence, domain, ...}
    """
    start = time.perf_counter()

    def _done(**kw: Any) -> dict[str, Any]:
        base = {"elapsed_ms": round((time.perf_counter() - start) * 1000, 3)}
        base.update(kw)
        return base

    if engine_override != "auto":
        return _done(
            engine=engine_override, engines=[engine_override],
            engines_combo=[engine_override],
            reason=f"用户指定: {engine_override}", confidence=1.0,
            features={}, domain=None, parallel=False, mode=mode,
        )

    features = extract_features(query)
    cfg = load_config()
    enabled = set(get_engines(cfg).keys())

    # TF-IDF 语义路由
    tfidf_best = None
    tfidf_scores = []
    try:
        tfidf_scores = semantic_route(query, top_k=3)
        if tfidf_scores:
            tfidf_best = tfidf_scores[0][0]
    except Exception:
        pass

    # 预算模式过滤可用引擎
    quota_mgr = get_quota_manager()
    if mode in ("fast", "budget"):
        enabled = {e for e in enabled if quota_mgr.is_available(e, mode=mode)}

    # 正则硬规则匹配
    domain = match_domain(query, get_domains(cfg))

    if domain:
        engines_combo = _get_engines_combo(domain, enabled, mode)
        if not engines_combo:
            # 域内引擎全被过滤，回退
            engines_combo = [e for e in ["local_search", "anysearch", "duckduckgo"] if e in enabled]
            if not engines_combo:
                engines_combo = sorted(enabled)[:2] if enabled else ["anysearch"]

        # TF-IDF 验证 + catch-all 修复
        tfidf_best_score = tfidf_scores[0][1] if tfidf_scores else 0.0
        is_catch_all = not domain.get("patterns", [])  # 无模式 = 兜底域

        if tfidf_best and tfidf_best in engines_combo:
            confidence = 0.95
        elif tfidf_best and tfidf_best != engines_combo[0]:
            confidence = 0.8
            # catch-all 域 + TF-IDF 高置信度推荐 → 注入推荐引擎到首位
            if is_catch_all and tfidf_best_score > 0.15 and tfidf_best in enabled:
                engines_combo = [tfidf_best] + [e for e in engines_combo if e != tfidf_best]
                confidence = 0.85
        else:
            confidence = 0.9
            # catch-all 域 + TF-IDF 推荐但不在 combo 中 → 前置
            if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in enabled:
                engines_combo.insert(0, tfidf_best)
                confidence = 0.8

        parallel = bool(domain.get("parallel", False)) or len(engines_combo) > 2
        # fast 模式强制串行，先 local_search 成功即避免额外 HTTP 开销
        if mode == "fast":
            parallel = False

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            engines_fallback=[e for e in enabled if e not in engines_combo],
            reason=(
                f"{_feature_labels(features)} → 命中域 [{domain.get('name', '?')}]"
                + (f" [TF-IDF→{tfidf_best}]" if tfidf_best else "")
                + (f" [TF-IDF覆写catch-all]" if is_catch_all and tfidf_best and tfidf_best_score > 0.15 and tfidf_best in engines_combo else "")
                + f" → {_ENGINE_NAMES.get(engines_combo[0], engines_combo[0])}"
            ),
            confidence=confidence, features=features,
            domain=domain.get("name"), parallel=parallel,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode,
        )

    # 正则未命中，用 TF-IDF 结果
    if tfidf_best and tfidf_best in enabled:
        engines_combo = [tfidf_best]
        if "anysearch" in enabled and "anysearch" not in engines_combo:
            engines_combo.append("anysearch")
        engines_combo = [e for e in engines_combo if e in enabled]

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            reason=f"TF-IDF 语义路由 → {_ENGINE_NAMES.get(tfidf_best, tfidf_best)} (正则未命中)",
            confidence=0.85, features=features, domain=None,
            parallel=len(engines_combo) > 1,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode,
        )

    # 兜底
    fallback_combo = [e for e in ["local_search", "anysearch", "duckduckgo"] if e in enabled]
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]

    return _done(
        engine=fallback_combo[0],
        engines=fallback_combo,
        engines_combo=fallback_combo,
        engines_fallback=[],
        reason=f"无匹配域，回退 {_ENGINE_NAMES.get(fallback_combo[0], fallback_combo[0])}",
        confidence=0.3, features=features, domain=None, parallel=False,
        tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
        mode=mode,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _cli():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Unified Search v2 路由器")
    parser.add_argument("query")
    parser.add_argument("--engine", default="auto")
    parser.add_argument("--mode", default="auto", choices=["fast", "auto", "deep", "budget"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    decision = route_query(args.query, engine_override=args.engine, mode=args.mode)
    if args.json:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        print(f"引擎: {decision['engine']}")
        print(f"组合: {decision.get('engines_combo', decision['engines'])}")
        print(f"原因: {decision['reason']}")
        print(f"置信度: {decision['confidence']:.2f}")
        print(f"耗时: {decision.get('elapsed_ms', 0):.3f} ms")


if __name__ == "__main__":
    _cli()
