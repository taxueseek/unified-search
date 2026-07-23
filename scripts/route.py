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

# 引擎注册中心（子引擎可见性）
try:
    from argo_engine_registry import get_registry as _get_registry
except Exception:
    _get_registry = None


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
    "anysearch": "AnySearch", "tavily": "Tavily",
    "zhihu": "知乎", "eastmoney": "东方财富", "byted": "字节搜索",
    "arxiv": "arXiv", "searxng": "SearXNG", "felo": "Felo",
    "bocha": "博查", "openalex": "OpenAlex", "crossref": "Crossref",
    "github": "GitHub", "wikipedia": "Wikipedia", "metaso": "秘塔",
    "wolframalpha": "WolframAlpha", "brave": "Brave",
    "duckduckgo": "DuckDuckGo", "uapi": "UAPI", "semantic_scholar": "Semantic Scholar",
    "exa": "Exa 语义搜索", "wechat_sogou": "搜狗微信搜索",
    "hackernews": "Hacker News", "stackoverflow": "Stack Overflow",
    "google_scholar": "Google Scholar", "v2ex": "V2EX",
    "ths_hot": "同花顺热点", "cls_telegraph": "财联社电报", "em_global_news": "东财全球资讯",
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


def _expand_local_search(engine_list: list[str], features: dict | None = None) -> list[str]:
    """将 local_search 扩展为具体的子引擎（基于查询特征）。"""
    if "local_search" not in engine_list:
        return engine_list
    if _get_registry is None:
        return engine_list

    registry = _get_registry()
    sub_engines = registry.list_local_engines(available_only=False)
    if not sub_engines:
        return engine_list

    selected = _select_sub_engines(sub_engines, features)
    result = [e for e in engine_list if e != "local_search"]
    for eng in selected:
        if eng not in result:
            result.append(eng)
    return result[:4]


def _add_language_engines(engine_list: list[str], features: dict | None = None) -> list[str]:
    """为已路由的查询添加语言相关的本地引擎（补充源）。

    当 TF-IDF 已选中主引擎后，根据查询语言特征追加本地子引擎，
    实现多源融合（网页引擎 + 本地零成本引擎）。
    """
    if _get_registry is None:
        return engine_list
    if not features:
        return engine_list

    chinese_ratio = features.get("chinese_ratio", 0)

    # 已包含 local_ 引擎则跳过
    if any(e.startswith("local_") for e in engine_list):
        return engine_list

    registry = _get_registry()
    sub_engines = registry.list_local_engines(available_only=False)
    if not sub_engines:
        return engine_list

    selected: list[str] = []
    # 只要含中文字符就追加中文引擎（阈值 0.1 覆盖中英混合查询）
    if chinese_ratio > 0.1:
        selected = [e for e in ["local_baidu", "local_sogou", "local_bing"] if e in sub_engines]
    elif features.get("has_depth_word"):
        selected = [e for e in ["local_arxiv", "local_semantic_scholar"] if e in sub_engines]

    result = list(engine_list)
    for eng in selected[:2]:  # 最多追加 2 个
        if eng not in result:
            result.append(eng)
    return result


def _select_sub_engines(sub_engines: list[str], features: dict | None = None) -> list[str]:
    """根据查询特征选择子引擎。"""
    if not features:
        return [e for e in ["local_bing", "local_duckduckgo", "local_mojeek"] if e in sub_engines]

    chinese_ratio = features.get("chinese_ratio", 0)
    if chinese_ratio > 0.1:
        return [e for e in ["local_baidu", "local_sogou", "local_bing"] if e in sub_engines]
    elif features.get("has_technical"):
        return [e for e in ["local_github", "local_stackoverflow", "local_bing"] if e in sub_engines]
    elif features.get("has_depth_word"):
        return [e for e in ["local_arxiv", "local_semantic_scholar", "local_bing"] if e in sub_engines]
    else:
        return [e for e in ["local_bing", "local_duckduckgo", "local_mojeek"] if e in sub_engines]


def _get_engines_combo(domain: dict[str, Any], enabled: set[str], mode: str = "auto",
                       features: dict | None = None) -> list[str]:
    """从域配置获取 engines_combo，过滤不可用/付费（budget 模式）。
    自动将 local_search 扩展为子引擎（消灭黑盒）。"""
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

    # 🔑 关键改动：将 local_search 扩展为子引擎
    if "local_search" in filtered:
        filtered = _expand_local_search(filtered, features)

    # fast/budget 模式过滤付费引擎
    if mode in ("fast", "budget"):
        quota_mgr = get_quota_manager()
        filtered = [e for e in filtered if quota_mgr.is_available(e, mode=mode)]

    # fast/budget 模式优先前置零成本子引擎
    if mode in ("fast", "budget"):
        free_locals = [e for e in filtered if e.startswith("local_")]
        others = [e for e in filtered if not e.startswith("local_")]
        filtered = free_locals + others

    # fast 模式：只保留免费引擎，最多 2 个
    if mode == "fast":
        from config import get_cost_factor
        filtered = [e for e in filtered if get_cost_factor(e) >= 0.85]
        filtered = filtered[:2]

    # 自适应学习过滤（保留主引擎不被过滤）
    if _adaptive_learner is not None and len(filtered) > 1:
        original = filtered[:]
        primary = domain.get("primary")
        filtered = [e for e in filtered if e == primary or _adaptive_learner.get_score(e) >= 0.3]
        if not filtered:
            filtered = original

    # 健康检查过滤
    try:
        from health_check import is_available as _hc_available
        healthy = []
        for e in filtered:
            if e.startswith("local_"):
                if _hc_available(e):
                    healthy.append(e)
            else:
                healthy.append(e)
        if healthy:
            filtered = healthy
    except ImportError:
        try:
            from health_probe import get_engine_status
            healthy = [e for e in filtered if get_engine_status(e).get("available", True)]
            if healthy:
                filtered = healthy
        except ImportError:
            pass

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
    except ImportError:
        pass  # tfidf_router 模块不可用
    except Exception as e:
        import logging
        logging.getLogger("unified_search.route").debug(f"TF-IDF 路由跳过: {type(e).__name__}")

    # 预算模式过滤可用引擎
    quota_mgr = get_quota_manager()
    if mode in ("fast", "budget"):
        enabled = {e for e in enabled if quota_mgr.is_available(e, mode=mode)}

    # 正则硬规则匹配
    domain = match_domain(query, get_domains(cfg))

    if domain:
        engines_combo = _get_engines_combo(domain, enabled, mode, features)
        # 🔑 为中文/学术查询追加本地引擎
        engines_combo = _add_language_engines(engines_combo, features)
        if not engines_combo:
            # 域内引擎全被过滤，回退
            engines_combo = [e for e in ["local_search", "anysearch", "duckduckgo"] if e in enabled]
            if not engines_combo:
                engines_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
            # 扩展 local_search → 子引擎
            engines_combo = _expand_local_search(engines_combo, features)

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
        # 🔑 展开 local_search → 子引擎
        engines_combo = _expand_local_search(engines_combo, features)
        # 🔑 为中文/学术查询追加本地引擎
        engines_combo = _add_language_engines(engines_combo, features)

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            reason=f"TF-IDF 语义路由 → {_ENGINE_NAMES.get(engines_combo[0], engines_combo[0])} (正则未命中)",
            confidence=0.85, features=features, domain=None,
            parallel=len(engines_combo) > 1,
            tfidf_scores=[{"engine": n, "score": s} for n, s, _ in tfidf_scores],
            mode=mode,
        )

    # 兜底
    fallback_combo = [e for e in ["local_search", "anysearch", "duckduckgo"] if e in enabled]
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]
    fallback_combo = _expand_local_search(fallback_combo, features)

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
