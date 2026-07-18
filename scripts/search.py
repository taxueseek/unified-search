#!/usr/bin/env python3
"""
search.py — Unified Search 统一搜索 CLI 主入口

职责：
  - 解析命令行参数
  - 通过 route.py 做路由决策
  - 通过 cache.py 做双层缓存（含 domain 分级 TTL）
  - 通过 engines.py 执行单个或多个引擎搜索
  - 对多引擎结果做 RRF 融合去重
  - 输出统一 JSON / 文本格式

向后兼容：
  - 保留原 search.py 的 CLI 参数与 JSON schema
  - --engine 支持 auto / anysearch / wigolo / hybrid 及 config.yaml 中任意引擎名
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

# 优先从当前目录导入，支持作为脚本直接运行
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from cache import SearchCache
from route import route_query
from engines import search as engine_search, available_engines, rerank_results
from config import get_execution_config


# ── 进度回调 ──────────────────────────────────────────────────────────────────

class Stage(str, Enum):
    """搜索执行阶段枚举，用于 on_progress 回调。"""
    START = "start"
    CACHE_HIT = "cache_hit"
    ROUTING = "routing"
    SEARCHING = "searching"
    MERGING = "merging"
    DONE = "done"
    ERROR = "error"


# ── 结果合并 ───────────────────────────────────────────────────────────────────

def rrf_merge(ranked_lists: list[list[dict[str, Any]]], k: int = 60) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion 合并多个引擎的结果列表，按 URL 去重。"""
    scores: dict[str, float] = {}
    items: dict[str, dict[str, Any]] = {}

    for results in ranked_lists:
        for i, r in enumerate(results):
            url = r.get("url", "")
            if not url:
                # 无 URL 的结果使用 title 作为临时 key 参与融合
                url = f"__title__:{r.get('title', '')}"
            scores[url] = scores.get(url, 0.0) + 1.0 / (k + i + 1)
            # 保留得分更高的版本或首次出现的字段
            if url not in items:
                items[url] = dict(r)
            else:
                # 合并 source 字段，保留更高分
                if r.get("score", 0) > items[url].get("score", 0):
                    items[url].update(r)
                if items[url].get("source") != r.get("source"):
                    sources = {items[url].get("source", ""), r.get("source", "")}
                    items[url]["source"] = "/".join(s for s in sources if s)

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [items[url] for url, _ in ranked]


def deduplicate_by_url(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """简单 URL 去重，保留第一次出现。"""
    seen: set[str] = set()
    out = []
    for r in results:
        url = r.get("url", "")
        key = url if url else f"title:{r.get('title', '')}"
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# ── 执行层 ─────────────────────────────────────────────────────────────────────

def execute_search(query: str, decision: dict[str, Any], max_results: int,
                   timeout: int, depth: str, cache: SearchCache, skip_cache: bool,
                   on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                   tier: str = "all") -> dict[str, Any]:
    """执行搜索：先查缓存，未命中则按决策执行引擎并写缓存。

    v3.1: 支持三层感知路由 + 跨层降级链 + fallback_chain 记录。
    """
    domain = decision.get("domain") or "general"
    engine_label = decision.get("engine", "auto")
    # 优先使用 engines_combo（渐进式多源），降级到 engines
    engines_combo = decision.get("engines_combo", decision.get("engines", [engine_label]))
    engines = engines_combo  # 向后兼容
    parallel = decision.get("parallel", False) and len(engines) > 1

    if on_progress:
        on_progress(Stage.START, {"query": query})

    # 缓存键使用实际引擎列表的排序字符串，避免不同引擎结果混用
    cache_engine_key = "+".join(sorted(engines)) if len(engines) > 1 else engines[0]

    if on_progress:
        on_progress(Stage.ROUTING, {"domain": domain, "engine": engine_label, "engines": engines})

    if not skip_cache:
        hit = cache.get(query, cache_engine_key, max_results, domain=domain)
        if hit:
            if on_progress:
                on_progress(Stage.CACHE_HIT, {"cache_level": hit.get("_cache_level", "L?")})
            # TF-IDF 全0分时省略字段，避免传输无效数据
            tfidf_scores = decision.get("tfidf_scores", [])
            if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
                tfidf_scores = []

            return {
                "query": query,
                "engine": engine_label,
                "engines": engines,
                "engines_combo": engines_combo,
                "cached": True,
                "cache_level": hit.get("_cache_level", "L?"),
                "domain": domain,
                "elapsed_ms": 0,
                "tier": tier,
                "fallback_chain": [],
                "tfidf_scores": tfidf_scores,
                "results": hit.get("results", []),
                "count": len(hit.get("results", [])),
            }

    if on_progress:
        on_progress(Stage.SEARCHING, {"engines": engines})

    t0 = time.time()
    raw_results: dict[str, list[dict[str, Any]]] = {}
    fallback_chain: list[dict[str, Any]] = []

    # 重试配置
    exec_cfg = get_execution_config()
    retry_count = exec_cfg.get("retry_count", 0)

    fallback_tiers = decision.get("fallback_tiers", {"T2": []})

    def _call_engine(eng: str, call_depth: str) -> list[dict[str, Any]]:
        """调用引擎；local/* → local-search 模块，其余走 engines.py 注册表。"""
        # T2 本地引擎：前缀 "local/" 剥离后调用 search_v3
        if eng.startswith("local/"):
            local_engine = eng[len("local/"):]
            try:
                import importlib.util
                # 从 config 读取 local-search 路径，默认为项目内 local-search/search_v3.py
                cfg = get_execution_config()
                local_search_path = cfg.get(
                    "local_search_path",
                    str(Path(__file__).resolve().parent.parent / "local-search" / "search_v3.py")
                )
                spec = importlib.util.spec_from_file_location(
                    "search_v3",
                    local_search_path
                )
                if spec and spec.loader:
                    local_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(local_mod)
                    result = local_mod.search(query, max_engines=min(max_results, 5))
                    return normalize_local_results(result.get("results", []), local_engine)
            except Exception:
                return []
            return []

        # T1 API 引擎：走 engines.py 注册表
        return engine_search(
            query, eng, n=max_results, timeout=timeout, depth=call_depth,
        )

    def _exec_engine(eng: str, retries: int = retry_count) -> list[dict[str, Any]]:
        """执行单个引擎，支持重试 + 深度降级。"""
        res: list[dict[str, Any]] = []
        for attempt in range(retries + 1):
            res = _call_engine(eng, depth)
            if res and any("error" not in (r or {}) for r in res):
                return res
            if attempt < retries:
                if on_progress:
                    on_progress(Stage.SEARCHING, {"engines": [eng], "retry": attempt + 1})
        # 降级尝试：改为 balanced depth 再试一次
        if depth != "balanced" and (not res or all("error" in (r or {}) for r in res)):
            res = _call_engine(eng, "balanced")
        return res

    def _result_has_valid(res: list[dict[str, Any]]) -> bool:
        """检查结果列表是否有有效条目。"""
        return bool(res and any("error" not in (r or {}) for r in res))

    def _exec_engine_list(
        eng_list: list[str],
        level: int,
        label: str,
        parallel_exec: bool = False,
    ) -> dict[str, list[dict[str, Any]]]:
        """执行一组引擎，记录降级链。

        Args:
            eng_list: 引擎名列表
            level: 降级层级（0=T1, 1=T2, 2=T3）
            label: 层级标签
            parallel_exec: 是否并行执行

        Returns:
            {引擎名: 结果列表} 字典
        """
        results: dict[str, list[dict[str, Any]]] = {}

        if not eng_list:
            fallback_chain.append({
                "level": level, "label": label,
                "engines": [], "result": "empty_pool",
            })
            return results

        if parallel_exec and len(eng_list) > 1:
            with ThreadPoolExecutor(max_workers=min(len(eng_list), 3)) as ex:
                futures = {ex.submit(_exec_engine, eng): eng for eng in eng_list}
                for fut in as_completed(futures, timeout=timeout + 2):
                    eng = futures[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = [{"error": str(e), "source": eng}]
                    results[eng] = res
                    if not _result_has_valid(res):
                        fallback_chain.append({
                            "level": level, "label": label,
                            "engine": eng, "result": _result_label(res),
                        })
                    else:
                        fallback_chain.append({
                            "level": level, "label": label,
                            "engine": eng, "result": "ok",
                        })
                for fut in futures:
                    if not fut.done():
                        fut.cancel()
        else:
            for eng in eng_list:
                res = _exec_engine(eng)
                results[eng] = res
                fallback_chain.append({
                    "level": level, "label": label,
                    "engine": eng, "result": _result_label(res),
                })
                if _result_has_valid(res):
                    break

        return results

    def _result_label(res: list[dict[str, Any]]) -> str:
        """推断结果标签。"""
        if not res:
            return "empty"
        if all("error" in (r or {}) for r in res):
            return "error"
        if any("error" not in (r or {}) for r in res):
            return "ok"
        return "partial"

    # ── 层级执行：T1 → T2 → T3 ──────────────────────────────────────────

    # 从 engines_combo 中分离 T1 和 T2 引擎
    registry_tier_map: dict[str, str] = {}
    try:
        import yaml
        from pathlib import Path
        rp = Path(__file__).resolve().parent.parent / "backends" / "engine_registry.yaml"
        with open(rp, "r") as f:
            reg_data = yaml.safe_load(f)
        for e in reg_data.get("engines", []):
            registry_tier_map[e.get("name", "")] = e.get("tier", "")
    except Exception:
        pass

    t1_engines = [e for e in engines if registry_tier_map.get(e, "T1") == "T1"]
    t2_engines = [e for e in engines if registry_tier_map.get(e) == "T2"]

    # 第0层：T1 引擎执行
    raw_results = _exec_engine_list(t1_engines, 0, "T1", parallel_exec=parallel)

    t1_valid = [res for res in raw_results.values() if _result_has_valid(res)]
    t1_all_failed = len(t1_valid) == 0 and len(t1_engines) > 0

    # 第1层：T1 全部失败 → 降级到 T2
    if t1_all_failed and t2_engines:
        if on_progress:
            on_progress(Stage.SEARCHING, {"engines": t2_engines, "fallback_level": 1})
        t2_raw = _exec_engine_list(t2_engines, 1, "T2", parallel_exec=True)
        raw_results.update(t2_raw)

    elapsed = int((time.time() - t0) * 1000)

    # 合并结果：逐条过滤含 error 的条目（非整组丢弃）
    filtered_results: dict[str, list[dict[str, Any]]] = {}
    for eng, res in raw_results.items():
        filtered = [r for r in res if isinstance(r, dict) and "error" not in r and not (isinstance(r, dict) and r.get("title") or r.get("url")) == False]
        filtered = [r for r in filtered if r.get("title") or r.get("url")]
        if filtered:
            filtered_results[eng] = filtered
    valid_lists = list(filtered_results.values())

    if len(valid_lists) > 1:
        merged = rrf_merge(valid_lists)[:max_results]
    elif valid_lists:
        merged = deduplicate_by_url(valid_lists[0])[:max_results]
    else:
        merged = []

    # Bocha Reranker 精排：多引擎融合后用语义模型二次排序
    if merged and len(merged) > 1:
        merged = rerank_results(query, merged, top_n=max_results)

    # 质量排序：按 score 降序
    if merged:
        merged.sort(key=lambda r: abs(r.get("score", 0) or 0), reverse=True)
        merged = merged[:max_results]

    if on_progress:
        on_progress(Stage.MERGING, {"count": len(merged)})

    result_payload = {
        "results": merged,
        "engines_used": list(raw_results.keys()),
        "domain": domain,
    }

    # 写缓存
    if not skip_cache:
        # 对耗时查询自动延长缓存 TTL（昂贵查询更值得缓存久一点）
        effective_ttl = None
        if elapsed > 2000:  # >2s 的查询
            # 耗时越长缓存越久：2s→2x TTL, 5s→4x TTL, 10s→8x TTL
            multiplier = min(2 ** (elapsed // 2000), 8)
            base_ttl = cache.resolve_ttl(domain)
            effective_ttl = base_ttl * multiplier
        cache.set(query, cache_engine_key, max_results, result_payload,
                  domain=domain, ttl=effective_ttl)

    if on_progress:
        on_progress(Stage.DONE, {"count": len(merged), "elapsed_ms": elapsed})

    # TF-IDF 全0分时省略字段，避免传输无效数据
    tfidf_scores = decision.get("tfidf_scores", [])
    if tfidf_scores and all(s.get("score", 0) == 0 for s in tfidf_scores):
        tfidf_scores = []

    # 收集错误：含引擎级 error + 全失败时 fallback_chain 摘要
    errors = _collect_errors(raw_results)
    if not errors and not merged and fallback_chain:
        failed = [f"{fc.get('engine','?')}:{fc.get('result','?')}"
                   for fc in fallback_chain if fc.get('result', 'ok') != 'ok']
        if failed:
            errors = [f"all_engines_failed: {', '.join(failed)}"]

    return {
        "query": query,
        "engine": engine_label,
        "engines": engines,
        "engines_combo": engines_combo,
        "cached": False,
        "domain": domain,
        "elapsed_ms": elapsed,
        "tier": tier,
        "fallback_chain": fallback_chain,
        "tfidf_scores": tfidf_scores,
        "results": merged,
        "count": len(merged),
        "engines_used": list(raw_results.keys()),
        "errors": errors,
    }


def _collect_errors(raw_results: dict[str, list[dict[str, Any]]]) -> list[str]:
    """收集引擎返回的错误信息。"""
    errors = []
    for eng, res in raw_results.items():
        for r in res:
            if isinstance(r, dict) and "error" in r:
                errors.append(f"{eng}: {r['error']}")
    return errors


def normalize_local_results(results: list[dict[str, Any]], source_prefix: str) -> list[dict[str, Any]]:
    """标准化 local-search 返回的结果为统一 SearchResult 格式。"""
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "snippet": r.get("snippet", "")[:300],
            "source": f"local/{source_prefix}",
            "score": r.get("score", 0.0),
        })
    return out


# ── 统一便捷入口 ──────────────────────────────────────────────────────────────

def super_search(query: str, engine: str = "auto", n: int = 5, explain: bool = False,
                 on_progress: Optional[Callable[[Stage, dict[str, Any]], None]] = None,
                 skip_cache: bool = False, timeout: int = 10,
                 depth: str = "fast", tier: str = "all") -> dict[str, Any]:
    """统一搜索便捷入口。

    Args:
        query: 搜索关键词
        engine: 引擎名或 "auto" 自动路由
        n: 最大结果数
        explain: 是否打印路由决策到 stderr
        on_progress: 进度回调 fn(stage, data)
        skip_cache: 是否跳过缓存
        timeout: 超时秒数
        depth: 搜索深度
        tier: 引擎层级（"api"/"local"/"all"）

    Returns:
        dict: {query, engine, engines, cached, domain, elapsed_ms, results, count, ...}
    """
    cache = SearchCache()
    decision = route_query(query, engine_override=engine, tier=tier)

    if explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])}",
            file=sys.stderr,
        )

    return execute_search(
        query=query,
        decision=decision,
        max_results=n,
        timeout=timeout,
        depth=depth,
        cache=cache,
        skip_cache=skip_cache,
        on_progress=on_progress,
        tier=tier,
    )


# ── 输出格式化 ─────────────────────────────────────────────────────────────────

def format_text_output(results: dict[str, Any]) -> str:
    """格式化为人类可读文本。"""
    lines = []
    count = results.get("count", 0)
    elapsed = results.get("elapsed_ms", 0)
    engine = results.get("engine", "?")
    cached = results.get("cached", False)
    cache_level = results.get("cache_level", "")
    domain = results.get("domain", "")

    header = f"=== {count} results ({elapsed}ms via {engine})"
    if cached:
        header += f" [CACHE {cache_level}]"
    elif domain:
        header += f" [domain:{domain}]"
    lines.append(header)

    errors = results.get("errors", [])
    if errors:
        for err in errors[:3]:
            lines.append(f"  [ERROR] {err}")

    for r in results.get("results", []):
        score = r.get("score", r.get("relevance_score", 0))
        title = r.get("title", "?")[:80]
        url = r.get("url", "")
        source = r.get("source", "")
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
        description="Unified Search — 统一搜索 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  python3 search.py "python async"
  python3 search.py "python async" -e wigolo -n 10 --depth deep
  python3 search.py "python async" --explain --json
  python3 search.py "python async" --no-cache -t 15

发现命令：
  python3 search.py --list-engines           # 列出所有引擎详情
  python3 search.py --find-engines "学术论文"  # 语义匹配引擎
  python3 search.py --describe-engine arxiv  # 查看引擎详情
  python3 search.py --status                 # 健康检查
        """,
    )
    parser.add_argument("query", nargs="*", help="搜索关键词")
    parser.add_argument(
        "--engine", "-e",
        default="auto",
        help="搜索引擎（默认 auto；可用 --list-engines 查看）",
    )
    parser.add_argument(
        "--max-results", "-n",
        type=int, default=5,
        help="最大结果数（默认 5）",
    )
    parser.add_argument(
        "--depth", "-d",
        default="fast",
        choices=["ultra-fast", "fast", "balanced", "deep"],
        help="搜索深度（默认 fast，部分引擎支持）",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="禁用缓存",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="显示路由决策",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="JSON 输出",
    )
    parser.add_argument(
        "--timeout", "-t",
        type=int, default=10,
        help="超时秒数（默认 10）",
    )
    parser.add_argument(
        "--list-engines",
        action="store_true",
        help="列出可用引擎并退出",
    )
    parser.add_argument(
        "--find-engines",
        metavar="QUERY",
        help="根据查询语义匹配可用引擎",
    )
    parser.add_argument(
        "--describe-engine",
        metavar="ENGINE",
        help="查看单个引擎详情",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="健康检查 + 可用性诊断",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="打印进度阶段到 stderr（调试用）",
    )
    parser.add_argument(
        "--tier",
        default="all",
        choices=["api", "local", "all"],
        help="引擎层级（默认 all；api=仅T1, local=优先T2, all=全开）",
    )

    args = parser.parse_args()

    # ── 发现命令 ──────────────────────────────────────────────────────────────
    if args.list_engines:
        from engines import list_engines_detailed
        print(json.dumps(list_engines_detailed(), ensure_ascii=False, indent=2))
        return

    if args.find_engines:
        from engines import find_engines
        print(json.dumps(find_engines(args.find_engines), ensure_ascii=False, indent=2))
        return

    if args.describe_engine:
        from engines import describe_engine
        print(json.dumps(describe_engine(args.describe_engine), ensure_ascii=False, indent=2))
        return

    if args.status:
        from engines import engine_status
        print(json.dumps(engine_status(), ensure_ascii=False, indent=2))
        return

    if not args.query:
        parser.error("必须提供搜索关键词")

    query = " ".join(args.query)
    cache = SearchCache()

    # 路由决策
    decision = route_query(query, engine_override=args.engine, tier=args.tier)

    if args.explain:
        combo = decision.get('engines_combo', decision.get('engines', []))
        print(
            f"[路由] {decision['reason']} → engine={decision['engine']} "
            f"combo={combo} domain={decision.get('domain')} "
            f"tfidf={decision.get('tfidf_scores', [])}",
            file=sys.stderr,
        )

    # 进度回调（CLI --progress 模式）
    on_progress = None
    if args.progress:
        def on_progress(stage: Stage, data: dict[str, Any]):
            print(f"[progress] {stage.value} {data}", file=sys.stderr)

    # 执行搜索
    results = execute_search(
        query=query,
        decision=decision,
        max_results=args.max_results,
        timeout=args.timeout,
        depth=args.depth,
        cache=cache,
        skip_cache=args.no_cache,
        on_progress=on_progress,
        tier=args.tier,
    )

    # 输出
    if args.json_output:
        # 移除内部字段，保持 schema 干净
        public = {k: v for k, v in results.items() if not k.startswith("_")}
        print(json.dumps(public, ensure_ascii=False, indent=2))
    else:
        print(format_text_output(results))


if __name__ == "__main__":
    main()
