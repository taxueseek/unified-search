# -*- coding: utf-8 -*-
"""智能搜索路由 — TF-IDF 语义路由 + 正则硬规则 + engines_combo 渐进式多源。

v0.0.5: 双层感知路由 — 自动补充 T2/T3 引擎 + fallback_tiers 跨层降级链。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

try:
    from config import load_config, get_engines, get_domains
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from config import load_config, get_engines, get_domains

try:
    from tfidf_router import semantic_route, get_router
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        from tfidf_router import semantic_route, get_router
    except ImportError:
        semantic_route = None
        get_router = None


_RE_CHINESE = re.compile(r"[一-鿿]")
_RE_COMPARE = re.compile(
    r"\b(vs|versus)\b|(对比|比较|区别|相比|哪个好|哪家强|选哪个)", re.I)
_RE_TECH = re.compile(
    r"\b(api|python|javascript|typescript|code|react|vue|node|rust|go|"
    r"golang|docker|kubernetes|linux|git|sql|error|bug|debug|exception|"
    r"function|class|async|thread|database|algorithm|stack|memory|"
    r"programming|framework|library)\b|"
    r"(函数|方法|类|库|框架|报错|调试|编程|代码|开发|技术|源码|架构)", re.I)
_RE_QUESTION = re.compile(
    r"\b(how|what|why|when|where|which|who)\b|"
    r"(怎么|什么|为什么|如何|哪里|哪个|谁|多少|几|吗|呢|能不能|可不可以)", re.I)
_RE_DEPTH = re.compile(
    r"\b(deep|comprehensive|review|survey|research|paper|thesis)\b|"
    r"(对比分析|深度|全面|详细|深入|系统|完整|综述|研究|探究|详解|论文)", re.I)
_RE_NEWS = re.compile(
    r"\b(news|latest|recent|breaking|today)\b|"
    r"(最近|今天|最新|刚刚|突发|今日|新闻|时事|热点|更新|动态|"
    r"揭晓|出炉|宣布|发布|公布|获奖|夺冠|颁奖|开幕|闭幕|举行|"
    r"诺贝尔|奥运|世界杯|世锦赛|亚运|锦标赛|大选|选举|峰会|"
    r"20[2-9]\d(年|奥运|奖|赛))", re.I)

_ENGINE_NAMES = {
    "anysearch": "AnySearch", "tavily": "Tavily",
    "zhihu": "知乎", "eastmoney": "东方财富", "byted": "字节搜索",
    "arxiv": "arXiv", "felo": "Felo",
}

# ── 三层引擎注册表 ────────────────────────────────────────────────────────────

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "backends" / "engine_registry.yaml"
_registry_cache: list[dict[str, Any]] | None = None

# 域名 → coverage 标签映射
_DOMAIN_COVERAGE_MAP: dict[str, list[str]] = {
    "general_search": ["general"],
    "chinese_general": ["chinese", "general"],
    "stock_query": ["finance", "stock", "fund"],
    "fund_query": ["finance", "stock", "fund"],
    "financial_news": ["finance", "news"],
    "academic": ["academic"],
    "tech_deep": ["code", "tech"],
    "code_search": ["code"],
    "fact_check": ["factual", "wiki"],
    "news_realtime": ["news"],
    "zhihu_content": ["chinese", "review", "opinion"],
    "shopping": ["review", "opinion", "general"],
    # 主题路由对应的 coverage
    "finance": ["finance"],
    "news": ["news"],
    "tech": ["tech", "code"],
    "academic_topic": ["academic"],
    "general": ["general"],
}


def _load_engine_registry(force: bool = False) -> list[dict[str, Any]]:
    """加载三层引擎注册表 YAML。"""
    global _registry_cache
    if _registry_cache is not None and not force:
        return _registry_cache

    try:
        import yaml
    except ImportError:
        _registry_cache = []
        return _registry_cache

    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        _registry_cache = data.get("engines", []) if isinstance(data, dict) else []
    except Exception:
        _registry_cache = []

    return _registry_cache


def _get_tier_engines_for_domain(
    domain_name: str | None,
    tier: str,
    registry: list[dict[str, Any]] | None = None,
    limit: int = 3,
) -> list[str]:
    """获取匹配域名的指定层级引擎列表。

    Args:
        domain_name: 域名（如 "tech_deep"）
        tier: 层级（"T2" 或 "T3"）
        registry: 引擎注册表（可选，默认自动加载）
        limit: 最大返回引擎数

    Returns:
        引擎名称列表（如 ["local/stackoverflow", "local/mdn"]）
    """
    if registry is None:
        registry = _load_engine_registry()

    coverages = _DOMAIN_COVERAGE_MAP.get(domain_name or "", ["general"])
    matching: list[tuple[str, int, int]] = []  # (name, latency, priority)

    for eng in registry:
        if eng.get("tier") != tier:
            continue
        status = eng.get("status", "unknown")
        if status in ("disabled", "unavailable"):
            continue
        eng_coverage = eng.get("coverage", [])
        # 计算匹配度：覆盖标签交集数
        overlap = len(set(eng_coverage) & set(coverages))
        if overlap > 0:
            # degraded 引擎降低优先级
            priority = overlap
            if status in ("degraded", "slow"):
                priority -= 0.5
            latency = eng.get("latency_ms", 9999)
            matching.append((eng.get("name", ""), latency, priority))

    # 按优先级（覆盖度）降序，延迟升序排序
    matching.sort(key=lambda x: (-x[2], x[1]))
    return [name for name, _, _ in matching[:limit]]


def _enrich_with_tier_engines(
    engines_combo: list[str],
    domain_name: str | None,
    topic: str,
    registry: list[dict[str, Any]] | None = None,
    domain_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为 engines_combo 补充 T2/T3 引擎，返回 {engines_combo, fallback_tiers}。

    T1 引擎已在 engines_combo 中（来自 config.yaml），此函数补充 T2/T3。
    优先使用 domain_config 中的显式 t2_engines/t3_engines 配置，
    不存在时退回到 engine_registry.yaml 的自动覆盖率匹配。
    """
    if registry is None:
        registry = _load_engine_registry()
    if not registry:
        return {"engines_combo": engines_combo, "fallback_tiers": {"T2": []}}

    # 优先使用域配置中的显式设定
    t2_engines: list[str] = []
    if domain_config:
        t2_engines = list(domain_config.get("t2_engines", []) or [])

    # 若域配置无显式设定，退回到覆盖率自动匹配
    if not t2_engines:
        domain_for_lookup = domain_name or f"{topic}_topic"
        t2_engines = _get_tier_engines_for_domain(domain_for_lookup, "T2", registry, limit=5)


    # 把 T2 引擎追加到 engines_combo 后面（不抢 T1 首位）
    enriched = list(engines_combo)
    for eng in t2_engines:
        if eng not in enriched:
            enriched.append(eng)

    return {
        "engines_combo": enriched,
        "fallback_tiers": {"T2": t2_engines},
    }


def _filter_by_tier(
    engines_combo: list[str],
    tier: str,
    registry: list[dict[str, Any]] | None = None,
) -> list[str]:
    """按层级过滤引擎列表。

    Args:
        engines_combo: 引擎列表
        tier: "api"（仅 T1）/ "local"（优先 T2）/ "all"（全部）
        registry: 引擎注册表

    Returns:
        过滤后的引擎列表
    """
    if tier == "all":
        return engines_combo

    if registry is None:
        registry = _load_engine_registry()

    # 构建引擎名 → 层级映射
    tier_map: dict[str, str] = {}
    for eng in registry:
        tier_map[eng.get("name", "")] = eng.get("tier", "")

    if tier == "api":
        # 仅保留 T1 引擎
        return [e for e in engines_combo if tier_map.get(e, "T1") == "T1"]
    elif tier == "local":
        # T2 优先，T1 保留（因为 T2 通常没有 T1 完整）
        # 策略：T2 引擎放到前面，T1 引擎向后移
        t2 = [e for e in engines_combo if tier_map.get(e) == "T2"]
        t1 = [e for e in engines_combo if tier_map.get(e, "T1") == "T1"]
        return t2 + t1

    return engines_combo


def extract_features(query: str) -> dict[str, Any]:
    """提取查询特征向量。"""
    total = len(query)
    chinese = len(_RE_CHINESE.findall(query))
    ratio = chinese / max(total, 1)
    return {
        "chinese_ratio": ratio,
        "english_ratio": 1.0 - ratio,
        "length": total,
        "word_count": len(query.split()),
        "has_compare": bool(_RE_COMPARE.search(query)),
        "has_technical": bool(_RE_TECH.search(query)),
        "has_question": bool(_RE_QUESTION.search(query)),
        "has_depth_word": bool(_RE_DEPTH.search(query)),
        "has_news_word": bool(_RE_NEWS.search(query)),
    }


def _feature_labels(features: dict[str, Any]) -> str:
    labels = []
    cr = features.get("chinese_ratio", 0)
    if cr > 0.6:
        labels.append("中文")
    elif cr < 0.1:
        labels.append("英文")
    for key, name in (
        ("has_technical", "技术向"),
        ("has_compare", "对比分析"),
        ("has_depth_word", "深度研究"),
        ("has_news_word", "新闻实时"),
        ("has_question", "问答型"),
    ):
        if features.get(key):
            labels.append(name)
    return " + ".join(labels) if labels else "通用查询"


# ── Tavily-inspired: 搜索深度自动选择 ─────────────────────────────────────────

def auto_select_depth(query: str, features: dict[str, Any]) -> str:
    """基于查询特征自动选择搜索深度。

    Tavily 的设计哲学：
    - ultra-fast: 事实/数字查询（what/who/when/多少）
    - fast: 教程/指南（how to/教程）
    - balanced: 对比/评测（vs/对比/哪个好）
    - deep: 深度研究（research/综述/深度分析）
    """
    cfg = load_config()
    depth_rules = cfg.get("execution", {}).get("depth_rules", {})

    # 按优先级匹配
    for depth in ["ultra_fast", "fast", "balanced", "deep"]:
        rules = depth_rules.get(depth, {})
        patterns = rules.get("patterns", [])
        for pattern in patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return depth

    # 基于特征的默认选择
    if features.get("has_news_word"):
        return "ultra_fast"
    if features.get("has_question"):
        return "fast"
    if features.get("has_compare"):
        return "balanced"
    if features.get("has_depth_word"):
        return "deep"

    return "fast"  # 默认 fast


# ── Tavily-inspired: 主题检测与路由 ─────────────────────────────────────────

def detect_topic(query: str, features: dict[str, Any]) -> str:
    """检测查询主题，用于主题感知路由。

    Tavily 的主题分类：
    - general: 通用搜索
    - news: 新闻搜索（更强调时效性）
    - finance: 金融搜索（更强调数据准确性）
    - tech: 技术搜索
    - academic: 学术搜索
    """
    # 金融关键词
    finance_patterns = r"(股价|行情|涨跌|基金|股票|ETF|财报|研报|投资|理财|银行|货币|利率|GDP|CPI|PMI)"
    if re.search(finance_patterns, query, re.IGNORECASE):
        return "finance"

    # 新闻关键词
    news_patterns = r"(最新|新闻|今天|突发|breaking|latest|news)"
    if features.get("has_news_word") or re.search(news_patterns, query, re.IGNORECASE):
        return "news"

    # 技术关键词
    if features.get("has_technical"):
        return "tech"

    # 学术关键词
    academic_patterns = r"(论文|paper|arXiv|preprint|DOI|引用|citation|学术|research|study)"
    if re.search(academic_patterns, query, re.IGNORECASE):
        return "academic"

    return "general"


def get_topic_config(topic: str) -> dict[str, Any]:
    """获取主题相关的配置（引擎组合、深度、领域过滤）。"""
    cfg = load_config()
    topic_routing = cfg.get("execution", {}).get("topic_routing", {})
    return topic_routing.get(topic, topic_routing.get("general", {}))


def get_domain_filter(topic: str) -> dict[str, list[str]]:
    """获取领域过滤配置。"""
    cfg = load_config()
    domain_filter = cfg.get("execution", {}).get("domain_filter", {})
    trusted = domain_filter.get("trusted_domains", {}).get(topic, [])
    excluded = domain_filter.get("exclude_domains", [])
    return {"include": trusted, "exclude": excluded}


def _domain_reason(features: dict[str, Any], engine: str, domain: dict[str, Any],
                    tfidf_best: str | None = None) -> str:
    labels = _feature_labels(features)
    tfidf_info = f" [TF-IDF→{tfidf_best}]" if tfidf_best else ""
    return (
        f"{labels}{tfidf_info} → 命中域 [{domain.get('name', '?')}] → "
        f"{_ENGINE_NAMES.get(engine, engine)}"
    )


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
    """按 config.yaml domains 顺序匹配；无命中返回 catch-all（patterns 为空）。"""
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


def _get_engines_combo(domain: dict[str, Any], enabled: set[str]) -> list[str]:
    """从域配置获取 engines_combo，过滤掉不可用的引擎。"""
    combo = domain.get("engines_combo", [])
    if combo:
        return [e for e in combo if e in enabled]

    # 降级：用 primary + fallback
    primary = domain.get("primary", "anysearch")
    fallback = domain.get("fallback")
    engines = [primary]
    if fallback and fallback != primary:
        engines.append(fallback)
    return [e for e in engines if e in enabled]


def route_query(query: str, engine_override: str = "auto",
                config: dict[str, Any] | None = None,
                tier: str = "all") -> dict[str, Any]:
    """路由决策主函数，返回 engine / engines / engines_combo / reason / domain 等字段。

    Args:
        query: 搜索查询词
        engine_override: 强制指定引擎（"auto" 为自动路由）
        config: 配置字典（可选）
        tier: 引擎层级过滤（"api" / "local" / "all"）

    Returns:
        路由决策字典，新增字段：
        - fallback_tiers: {"T2": [...], "T3": [...]}  跨层降级备选
        - tier: 实际使用的层级
    """
    start = time.perf_counter()
    _effective_tier = tier  # 闭包 _done() 使用

    def _done(**kw: Any) -> dict[str, Any]:
        base = {
            "scores": {},
            "elapsed_ms": (time.perf_counter() - start) * 1000,
            "tier": _effective_tier,
            "fallback_tiers": {"T2": []},
        }
        base.update(kw)
        return base

    if engine_override != "auto":
        wp = None
        return _done(
            engine=engine_override, engines=[engine_override],
            engines_combo=[engine_override],
            reason=f"用户指定: {engine_override}", confidence=1.0,
            features={}, domain=None, parallel=False,
        )

    features = extract_features(query)
    cfg = config if config is not None else load_config()
    enabled = set(get_engines(cfg).keys())

    # 加载三层引擎注册表（用于 T2/T3 补充）
    registry = _load_engine_registry()

    # Tavily-inspired: 主题检测与搜索深度自动选择
    topic = detect_topic(query, features)
    auto_depth = auto_select_depth(query, features)
    topic_config = get_topic_config(topic)
    domain_filter = get_domain_filter(topic)

    # 1. TF-IDF 语义路由（新增）
    tfidf_best = None
    tfidf_scores = []
    if semantic_route is not None:
        try:
            tfidf_scores = semantic_route(query, top_k=3)
            if tfidf_scores:
                tfidf_best = tfidf_scores[0][0]
        except Exception:
            pass

    # 2. 正则硬规则匹配（保留）
    domain = match_domain(query, get_domains(cfg))

    # 3. 融合决策
    if domain:
        engines_combo = _get_engines_combo(domain, enabled)
        domain_name = domain.get("name")

        # TF-IDF 验证：如果 TF-IDF 也指向同一方向，置信度更高
        if tfidf_best and tfidf_best in engines_combo:
            confidence = 0.95
        elif tfidf_best and tfidf_best != engines_combo[0]:
            # TF-IDF 与正则不一致，但正则更可信（硬规则），降低置信度
            confidence = 0.8
        else:
            confidence = 0.9

        # ── 升级1：三层感知引擎补充 + tier 过滤 ──────────────────
        enriched = _enrich_with_tier_engines(engines_combo, domain_name, topic, registry, domain_config=domain)
        engines_combo = enriched["engines_combo"]
        fallback_tiers = enriched["fallback_tiers"]
        engines_combo = _filter_by_tier(engines_combo, tier, registry)
        if not engines_combo:
            engines_combo = _get_engines_combo(domain, enabled)
            _effective_tier = "all"
            fallback_tiers = {"T2": []}

        parallel = bool(domain.get("parallel", False)) or len(engines_combo) > 2

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            engines_fallback=[e for e in enabled if e not in engines_combo],
            reason=_domain_reason(features, engines_combo[0], domain, tfidf_best),
            confidence=confidence, features=features,
            domain=domain_name, parallel=parallel,
            tfidf_scores=[{"engine": n, "score": s} for n, s in tfidf_scores],
            fallback_tiers=fallback_tiers,
            # Tavily-inspired: 主题和深度信息
            topic=topic, auto_depth=auto_depth,
            domain_filter=domain_filter,
        )

    # 4. 正则未命中，用 TF-IDF 结果
    if tfidf_best and tfidf_best in enabled:
        engines_combo = [tfidf_best, "anysearch"]
        # 确保 anysearch 在列表中
        if "anysearch" in enabled and "anysearch" not in engines_combo:
            engines_combo.append("anysearch")
        engines_combo = [e for e in engines_combo if e in enabled]
        # 补充 T2/T3 引擎 + tier 过滤
        enriched = _enrich_with_tier_engines(engines_combo, None, topic, registry)
        engines_combo = enriched["engines_combo"]
        fallback_tiers = enriched["fallback_tiers"]
        engines_combo = _filter_by_tier(engines_combo, tier, registry)
        if not engines_combo:
            engines_combo = [tfidf_best, "anysearch"]
            engines_combo = [e for e in engines_combo if e in enabled]
            _effective_tier = "all"
            fallback_tiers = {"T2": []}

        return _done(
            engine=engines_combo[0],
            engines=engines_combo,
            engines_combo=engines_combo,
            engines_fallback=[e for e in enabled if e not in engines_combo],
            reason=f"TF-IDF 语义路由 → {_ENGINE_NAMES.get(tfidf_best, tfidf_best)}",
            confidence=0.85, features=features,
            domain=None, parallel=len(engines_combo) > 1,
            tfidf_scores=[{"engine": n, "score": s} for n, s in tfidf_scores],
            fallback_tiers=fallback_tiers,
            topic=topic, auto_depth=auto_depth,
            domain_filter=domain_filter,
        )

    # 5. 兜底
    fallback_combo = ["anysearch", "byted"]
    fallback_combo = [e for e in fallback_combo if e in enabled]
    if not fallback_combo:
        fallback_combo = sorted(enabled)[:2] if enabled else ["anysearch"]

    # 补充 T2/T3 引擎 + tier 过滤
    enriched = _enrich_with_tier_engines(fallback_combo, None, topic, registry)
    fallback_combo = enriched["engines_combo"]
    fallback_tiers = enriched["fallback_tiers"]
    fallback_combo = _filter_by_tier(fallback_combo, tier, registry)
    if not fallback_combo:
        fallback_combo = ["anysearch", "byted"]
        fallback_combo = [e for e in fallback_combo if e in enabled]
        _effective_tier = "all"
        fallback_tiers = {"T2": []}

    return _done(
        engine=fallback_combo[0],
        engines=fallback_combo,
        engines_combo=fallback_combo,
        engines_fallback=[],
        reason=f"无匹配域，回退 {fallback_combo[0]}",
        confidence=0.3, features=features, domain=None, parallel=False,
        tfidf_scores=[{"engine": n, "score": s} for n, s in tfidf_scores],
        fallback_tiers=fallback_tiers,
        topic=topic, auto_depth=auto_depth,
        domain_filter=domain_filter,
    )


def _cli() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="统一搜索路由器 v3.0")
    parser.add_argument("query", help="查询词")
    parser.add_argument("--engine", default="auto", help="强制指定引擎")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()
    decision = route_query(args.query, engine_override=args.engine)
    if args.json:
        print(json.dumps(decision, ensure_ascii=False, indent=2))
    else:
        print(f"引擎: {decision['engine']}")
        print(f"引擎组合: {decision.get('engines_combo', decision['engines'])}")
        print(f"原因: {decision['reason']}")
        print(f"置信度: {decision['confidence']:.2f}")
        if decision.get("domain"):
            print(f"域: {decision['domain']}")
        if decision.get("elapsed_ms") is not None:
            print(f"耗时: {decision['elapsed_ms']:.3f} ms")


if __name__ == "__main__":
    _cli()
