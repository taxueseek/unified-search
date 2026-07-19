#!/usr/bin/env python3
"""
search.py — Unified Search v2 CLI 主入口 & 执行编排

职责：
  - 解析命令行参数
  - 通过 route.py 做路由决策（含预算模式）
  - 通过 cache.py 做双层缓存
  - 通过 engines.py 执行引擎搜索
  - RRF 融合 + Bocha Reranker 精排
  - 通过 adaptive.py 记录引擎表现
  - 输出统一 JSON / 文本格式
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from typing import Any, Callable, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache
from route import route_query
from engines import search as engine_search, available_engines
from config import get_execution_config, get_cost_factor


# ── 进度阶段 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"


# ── RRF 融合 ───────────────────────────────────────────────────────────────────

def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多引擎结果。"""
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for results in ranked_lists:
        for i, r in enumerate(results):
            url = r.get("url", "") or f"__title__:{r.get('title', '')}"
            scores[url] = scores.get(url, 0.0) + 1.0 / (k + i + 1)
            if url not in items:
                items[url] = dict(r)
            else:
                if r.get("score", 0) > items[url].get("score", 0):
                    items[url].update(r)
                sources = {items[url].get("source", ""), r.get("source", "")}
                items[url]["source"] = "/".join(s for s in sources if s)

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [items[url] for url, _ in ranked]


def deduplicate_by_url(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """URL 去重。"""
    seen: set[str] = set()
    out = []
    for r in results:
        key = r.get("url", "") or f"title:{r.get('title', '')}"
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


# ── Bocha Reranker ──────────────────────────────────────────────────────────────

def rerank_results(query: str, results: list[dict[str, Any]],
                   top_n: int = 10, timeout: float = 5) -> list[dict[str, Any]]:
    """使用博查语义排序模型对搜索结果二次精排。"""
    if not results or len(results) <= 1:
        return results

    api_key = os.environ.get("BOCHA_API_KEY", "")
    if not api_key:
        return results

    documents = []
    for r in results:
        doc_text = f"{r.get('title', '')} {r.get('snippet', '')}".strip()
        documents.append(doc_text or "empty")

    import urllib.request
    payload = json.dumps({
        "model": "gte-rerank", "query": query,
        "documents": documents[:50],
        "top_n": min(top_n, len(documents)),
        "return_documents": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.bocha.cn/v1/rerank", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rerank_results_list = data.get("data", {}).get("results", [])
            if not rerank_results_list:
                return results
            scored = []
            for rr in rerank_results_list:
                idx = rr.get("index", -1)
                score = rr.get("relevance_score", 0)
                if 0 <= idx < len(results):
                    item = dict(results[idx])
                    orig_score = item.get("score", 0) or 0
                    item["score"] = round(score * 0.7 + orig_score * 0.3, 4)
                    scored.append(item)
            if scored:
                scored.sort(key=lambda x: x.get("score", 0), reverse=True)
                return scored[:top_n]
    except Exception:
        pass
    return results


# ── 执行层 ─────────────────────────────────────────────────────────────────────

def execute_search(query: str, decision: dict[str, Any], max_results: int,
                   timeout: int, depth: str, cache: SearchCache, skip_cache: bool,
                   mode: str = "auto",
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None) -> dict[str, Any]:
    """执行搜索：缓存 → 引擎 → 融合 → 精排 → 写缓存。"""
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    engines = engines_combo
    parallel = decision.get("parallel", False) and len(engines) > 1

    if on_progress:
        on_progress(Stage.START, {"query": query})

    cache_engine_key = "+".join(sorted(engines)) if len(engines) > 1 else engines[0]

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    # 缓存命中
    if not skip_cache:
        hit = cache.get(query, cache_engine_key, max_results, domain=domain)
        if hit:
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []
            return {
                "query": query, "engine": engine_label, "engines": engines,
                "engines_combo": engines_combo, "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain, "elapsed_ms": 0,
                "tfidf_scores": tfidf_scores,
                "results": hit.get("results", []),
                "count": len(hit.get("results", [])),
                "mode": mode,
            }

    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    t0 = time.time()
    raw_results: dict[str, list[dict[str, Any]]] = {}

    exec_cfg = get_execution_config()
    retry_count = exec_cfg.get("retry_count", 0)

    def _exec_engine(eng: str, retries: int = retry_count) -> list[dict[str, Any]]:
        for attempt in range(retries + 1):
            res = engine_search(query, eng, n=max_results, timeout=timeout, depth=depth, mode=mode)
            if res and any("error" not in r for r in res):
                return res
        if depth != "balanced":
            return engine_search(query, eng, n=max_results, timeout=timeout, depth="balanced", mode=mode)
        return res if res else []

    if parallel:
        with ThreadPoolExecutor(max_workers=min(len(engines), 3)) as ex:
            futures = {ex.submit(_exec_engine, eng): eng for eng in engines}
            try:
                for fut in as_completed(futures, timeout=timeout + 2):
                    eng = futures[fut]
                    try:
                        raw_results[eng] = fut.result()
                    except Exception as e:
                        raw_results[eng] = [{"error": str(e), "source": eng}]
            except TimeoutError:
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
                        eng = futures[fut]
                        raw_results[eng] = [{"error": "timeout", "source": eng}]
            for fut in futures:
                if not fut.done():
                    fut.cancel()
    else:
        for eng in engines:
            res = _exec_engine(eng)
            raw_results[eng] = res
            if res and any("error" not in r for r in res):
                break

    elapsed = int((time.time() - t0) * 1000)

    # 融合
    valid_lists = [res for res in raw_results.values()
                   if res and not any("error" in r for r in res)]
    if len(valid_lists) > 1:
        merged = rrf_merge(valid_lists)[:max_results]
    elif valid_lists:
        merged = deduplicate_by_url(valid_lists[0])[:max_results]
    else:
        merged = []

    # Reranker 精排
    if merged and len(merged) > 1:
        merged = rerank_results(query, merged, top_n=max_results)

    # 按 score 排序
    if merged:
        merged.sort(key=lambda r: abs(r.get("score", 0) or 0), reverse=True)
        merged = merged[:max_results]

    # 内嵌可信度评分（快速版本：authority + freshness，不含 cross_validation）
    if merged:
        try:
            from evidence import score_authority, score_freshness
            for r in merged:
                url = r.get("url", "")
                source = r.get("source", "")
                auth = score_authority(url, source)
                fresh = score_freshness(r)
                r["authority"] = auth["score"]
                r["authority_tier"] = auth["tier"]
                r["freshness"] = fresh["score"]
        except Exception:
            pass  # evidence 模块不可用时跳过

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    result_payload = {
        "results": merged, "engines_used": list(raw_results.keys()), "domain": domain,
    }

    # 写缓存
    if not skip_cache:
        effective_ttl = None
        if elapsed > 2000:
            multiplier = min(2 ** (elapsed // 2000), 8)
            base_ttl = cache.resolve_ttl(domain)
            effective_ttl = base_ttl * multiplier
        cache.set(query, cache_engine_key, max_results, result_payload,
                  domain=domain, ttl=effective_ttl)

    # 记录自适应学习数据
    try:
        from adaptive import get_learner
        learner = get_learner()
        for eng, res in raw_results.items():
            success = bool(res and any("error" not in r for r in res))
            latency = elapsed / max(len(raw_results), 1)
            cost = get_cost_factor(eng)
            learner.record(eng, success=success, latency_ms=latency, cost=0.0 if cost >= 0.85 else 0.001)
    except Exception:
        pass

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    return {
        "query": query, "engine": engine_label, "engines": engines,
        "engines_combo": engines_combo, "cached": False,
        "domain": domain, "elapsed_ms": elapsed,
        "tfidf_scores": tfidf_scores, "results": merged,
        "count": len(merged), "engines_used": list(raw_results.keys()),
        "errors": _collect_errors(raw_results), "mode": mode,
    }


def _collect_errors(raw_results: dict[str, list[dict[str, Any]]]) -> list[str]:
    errors = []
    for eng, res in raw_results.items():
        for r in res:
            if isinstance(r, dict) and "error" in r:
                errors.append(f"{eng}: {r['error']}")
    return errors


# ── 统一入口 ──────────────────────────────────────────────────────────────────

def super_search(query: str, engine: str = "auto", n: int = 5, explain: bool = False,
                 skip_cache: bool = False, timeout: int = 10,
                 depth: str = "fast", mode: str = "auto", local_first: bool = False,
                 rewrite: bool = True, cache: Any = None) -> dict[str, Any]:
    """统一搜索便捷入口。

    Args:
        query: 搜索查询词
        engine: 指定引擎（默认 auto）
        n: 最大结果数
        explain: 是否输出路由解释
        skip_cache: 是否跳过缓存
        timeout: 超时
        depth: 搜索深度
        mode: 预算模式
        local_first: 强制本地优先
        rewrite: 是否自动改写查询（默认 True）
    """
    cache = cache if cache is not None else SearchCache()
    rewrite_result = None

    # 查询改写：追加领域关键词提升搜索质量
    if rewrite:
        try:
            from query_rewriter import rewrite_query as do_rewrite
            rewrite_result = do_rewrite(query)
            if rewrite_result["rewritten"] and rewrite_result["confidence"] >= 0.7:
                query = rewrite_result["rewritten"]
        except Exception:
            pass  # 改写失败不影响搜索

    if local_first:
        decision = route_query(query, engine_override="local_search", mode=mode)
    else:
        decision = route_query(query, engine_override=engine, mode=mode)
    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={mode}",
            file=sys.stderr,
        )
    result = execute_search(query=query, decision=decision, max_results=n,
                          timeout=timeout, depth=depth, cache=cache,
                          skip_cache=skip_cache, mode=mode)
    if rewrite_result and rewrite_result["rewritten"]:
        result["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }
    return result


# ── 输出格式化 ─────────────────────────────────────────────────────────────────

def format_text_output(results: dict[str, Any]) -> str:
    lines = []
    count = results.get("count", 0)
    elapsed = results.get("elapsed_ms", 0)
    engine = results.get("engine", "?")
    cached = results.get("cached", False)
    cache_level = results.get("cache_level", "")
    domain = results.get("domain", "")
    mode = results.get("mode", "auto")

    header = f"=== {count} results ({elapsed}ms via {engine})"
    if cached:
        header += f" [CACHE {cache_level}]"
    elif domain:
        header += f" [domain:{domain}]"
    if mode != "auto":
        header += f" [mode:{mode}]"
    lines.append(header)

    for err in results.get("errors", [])[:3]:
        lines.append(f"  [ERROR] {err}")

    for r in results.get("results", []):
        score = r.get("score", 0)
        title = r.get("title", "?")[:80]
        url = r.get("url", "")
        prefix = f"[{score:.2f}]" if score else "[?]"
        lines.append(f"  {prefix} {title}")
        if url:
            lines.append(f"    {url}")
        snippet = r.get("snippet", "")
        if snippet:
            lines.append(f"    {snippet[:120]}")

    return "\n".join(lines)


# ── CLI 主入口 ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Unified Search v2 — 统一搜索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 search.py "python async"
  python3 search.py "英伟达财报" --explain --json
  python3 search.py "基金推荐" --mode fast
  python3 search.py "AAPL" --engine anysearch --domain finance --sub_domain finance.us_stock
        """,
    )
    parser.add_argument("query", nargs="?")
    parser.add_argument("--engine", "-e", default="auto")
    parser.add_argument("--max-results", "-n", type=int, default=5)
    parser.add_argument("--depth", "-d", default="fast",
                        choices=["fast", "balanced", "deep"])
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--timeout", "-t", type=int, default=10)
    parser.add_argument("--list-engines", action="store_true")
    parser.add_argument("--mode", default="auto",
                        choices=["fast", "auto", "deep", "budget"],
                        help="预算模式: fast=免费优先, auto=成本感知, deep=质量优先, budget=配额控制")
    parser.add_argument("--local-first", action="store_true",
                        help="强制优先使用 local_search 零成本聚合引擎")
    parser.add_argument("--domain", default="", help="AnySearch 垂直域")
    parser.add_argument("--sub_domain", default="", help="AnySearch 子域")
    parser.add_argument("--progress", action="store_true")

    args = parser.parse_args()

    if args.list_engines:
        print(json.dumps(available_engines(), ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    cache = SearchCache()
    if args.local_first:
        decision = route_query(args.query, engine_override="local_search", mode=args.mode)
    else:
        decision = route_query(args.query, engine_override=args.engine, mode=args.mode)

    # 查询改写
    rewrite_result = None
    try:
        from query_rewriter import rewrite_query as do_rewrite
        rewrite_result = do_rewrite(args.query)
        if rewrite_result["rewritten"] and rewrite_result["confidence"] >= 0.7:
            search_query = rewrite_result["rewritten"]
        else:
            search_query = args.query
    except Exception:
        search_query = args.query

    if args.explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])} mode={args.mode}",
            file=sys.stderr,
        )
        if rewrite_result and rewrite_result["rewritten"]:
            print(
                f"[改写] {rewrite_result['original']} → {rewrite_result['rewritten']} "
                f"({rewrite_result['confidence']:.0%})",
                file=sys.stderr,
            )

    on_progress = None
    if args.progress:
        def on_progress(stage: Stage, data: dict[str, Any]):
            print(f"[progress] {stage.value} {data}", file=sys.stderr)

    results = execute_search(
        query=search_query, decision=decision, max_results=args.max_results,
        timeout=args.timeout, depth=args.depth, cache=cache,
        skip_cache=args.no_cache, mode=args.mode, on_progress=on_progress,
    )

    if rewrite_result and rewrite_result["rewritten"]:
        results["rewritten_query"] = {
            "original": rewrite_result["original"],
            "rewritten": rewrite_result["rewritten"],
            "confidence": rewrite_result["confidence"],
            "reason": rewrite_result["reason"],
        }

    if args.json_output:
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(format_text_output(results))


if __name__ == "__main__":
    main()
