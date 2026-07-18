#!/usr/bin/env python3
"""性能基准测试 — Unified Search v2"""

import json
import os
import subprocess
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_PY = f"{SKILL_DIR}/scripts/search.py"

QUERIES = [
    {"query": "诺贝尔物理奖 2024", "engine": "auto", "expected_domain": "news_realtime"},
    {"query": "Python asyncio event loop", "engine": "auto", "expected_domain": None},
    {"query": "对比分析 React Vue Svelte", "engine": "auto", "expected_domain": None},
    {"query": "巴黎奥运会金牌榜 2024", "engine": "auto", "expected_domain": "news_realtime"},
    {"query": "Rust vs Go 2026 performance", "engine": "auto", "expected_domain": None},
    {"query": "贵州茅台股价", "engine": "auto", "expected_domain": "stock_query"},
    {"query": "基金净值排行", "engine": "auto", "expected_domain": "fund_query"},
]

REPEAT = 3


def run_once(query: str, engine: str) -> dict:
    """执行单次搜索，返回耗时与结果数。"""
    t0 = time.time()
    try:
        r = subprocess.run(
            ["python3", SEARCH_PY, query, "--engine", engine, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        elapsed = (time.time() - t0) * 1000
        ok = r.returncode == 0
        try:
            data = json.loads(r.stdout)
            cached = data.get("cached", False)
            result_count = len(data.get("results", []))
            domain = data.get("domain")
            engines_used = data.get("engines_used", [])
        except Exception:
            cached = False
            result_count = 0
            domain = None
            engines_used = []
    except subprocess.TimeoutExpired:
        elapsed = (time.time() - t0) * 1000
        ok = False
        cached = False
        result_count = 0
        domain = None
        engines_used = []

    return {
        "elapsed_ms": round(elapsed, 1),
        "ok": ok,
        "cached": cached,
        "result_count": result_count,
        "domain": domain,
        "engines_used": engines_used,
    }


def benchmark():
    print(f"{'=' * 70}")
    print("统一搜索性能基准测试")
    print(f"{'=' * 70}")
    print(f"每个查询跑 {REPEAT} 轮\n")

    results = []
    for q in QUERIES:
        key = f"{q['query']} ({q['engine']})"
        print(f"▶ {key}")
        if q.get("expected_domain"):
            print(f"  期望域: {q['expected_domain']}")
        times = []
        for i in range(REPEAT):
            r = run_once(q["query"], q["engine"])
            times.append(r)
            status = "✅" if r["ok"] else "❌"
            cached = " [cached]" if r["cached"] else ""
            domain = f" [{r['domain']}]" if r["domain"] else ""
            engines = f" engines={r['engines_used']}" if r["engines_used"] else ""
            print(f"  run {i + 1}: {status} {r['elapsed_ms']:.0f}ms{cached}{domain}{engines} ({r['result_count']} results)")
            results.append({"query": q["query"], "engine": q["engine"], **r})

        avg = sum(t["elapsed_ms"] for t in times) / len(times)
        print(f"  ── avg={avg:.0f}ms\n")

    # 汇总统计
    print(f"{'=' * 70}")
    print("汇总统计")
    print(f"{'=' * 70}")

    by_query = {}
    for r in results:
        key = f"{r['query']} ({r['engine']})"
        by_query.setdefault(key, []).append(r["elapsed_ms"])

    print(f"{'查询':<40} {'avg':>8} {'min':>8} {'max':>8}")
    print("-" * 70)
    for key, times in by_query.items():
        avg = sum(times) / len(times)
        print(f"{key:<40} {avg:>7.0f}ms {min(times):>7.0f}ms {max(times):>7.0f}ms")

    # 缓存命中率统计
    total = len(results)
    cached_count = sum(1 for r in results if r.get("cached"))
    ok_count = sum(1 for r in results if r["ok"])
    print(f"\n成功率: {ok_count}/{total}")
    if total > 0:
        print(f"缓存命中率: {cached_count}/{total} ({cached_count / total * 100:.1f}%)")

    # 域路由正确性
    print(f"\n{'=' * 70}")
    print("域路由检查")
    print(f"{'=' * 70}")
    route_ok = 0
    for q in QUERIES:
        if not q.get("expected_domain"):
            continue
        sample = next((r for r in results if r["query"] == q["query"]), None)
        if sample and sample.get("domain") == q["expected_domain"]:
            route_ok += 1
            print(f"✅ {q['query']}: {q['expected_domain']}")
        else:
            actual = sample.get("domain") if sample else "N/A"
            print(f"⚠️  {q['query']}: 期望 {q['expected_domain']}, 实际 {actual}")

    # SLA 检查
    print(f"\n{'=' * 70}")
    print("SLA 检查")
    print(f"{'=' * 70}")
    sla_failures = 0
    for r in results:
        engine = r["engine"]
        sla_limit = 3000 if engine == "anysearch" else 8000 if engine in ("wigolo", "tavily") else 10000
        if r["elapsed_ms"] > sla_limit and not r.get("cached"):
            print(f"⚠️  SLA FAIL: {r['query']} ({engine}) {r['elapsed_ms']:.0f}ms > {sla_limit}ms")
            sla_failures += 1
    if sla_failures == 0:
        print("✅ 所有非缓存查询满足 SLA")
    else:
        print(f"⚠️  {sla_failures} 次查询超出 SLA")


if __name__ == "__main__":
    benchmark()
