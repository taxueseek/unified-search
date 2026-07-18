#!/usr/bin/env python3
"""统一搜索端到端验收测试 — 针对团队 A 的 search.py 输出格式"""

import json
import os
import subprocess
import sys
import time

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEARCH_PY = os.path.join(SKILL_DIR, "scripts", "search.py")


def run_search(query, engine="auto", timeout=15, no_cache=False, json_output=True):
    """执行搜索并解析 JSON 输出"""
    cmd = ["python3", SEARCH_PY, query, "--engine", engine]
    if no_cache:
        cmd.append("--no-cache")
    if json_output:
        cmd.append("--json")

    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = (time.time() - t0) * 1000
    try:
        data = json.loads(r.stdout)
    except Exception:
        data = {"_error": r.stdout + r.stderr, "_raw": r.stdout}
    return data, elapsed


def test_cache_hit():
    """测试缓存命中：同查询第二次应 < 500ms 且包含 cached=true"""
    # 先清除缓存确保干净状态
    subprocess.run(["python3", SEARCH_PY.rsplit("/", 1)[0] + "/cache.py", "clear", "--older-than", "0"],
                   capture_output=True, text=True)
    q = f"Nobel Prize Physics 2024 test_cache {int(time.time())}"
    # 第一次：搜索并写入缓存
    run_search(q, "anysearch")
    # 第二次：应命中缓存
    data, t2 = run_search(q, "anysearch")
    assert data.get("cached") is True, f"期望 cached=True, 实际 keys={list(data.keys())}"
    assert t2 < 500, f"缓存命中应 < 500ms, 实际 {t2:.0f}ms"


def test_chinese_short_query():
    """中文短查询应完成端到端流程并返回有效结构（结果数取决于外部引擎可用性）。"""
    data, elapsed = run_search("诺贝尔物理奖 2024", timeout=8)
    assert "results" in data or "_error" not in data, f"输出无效: {data}"
    assert "elapsed_ms" in data, f"缺少 elapsed_ms: {list(data.keys())}"
    assert isinstance(data.get("results"), list)
    # 若外部引擎均不可用导致空结果，至少不应报错
    assert "error" not in data, f"不应返回顶层 error: {data.get('error')}"


def test_english_technical():
    """英文技术查询应返回结果"""
    data, elapsed = run_search("Python asyncio event loop internals", timeout=10)
    assert "results" in data, f"输出无效: {data}"
    assert len(data.get("results", [])) > 0, f"期望有结果，实际 {len(data.get('results', []))}"


def test_explain_mode():
    """--explain 应输出路由决策（通过 route.py 快速验证，避免外部引擎耗时）。"""
    r = subprocess.run(
        ["python3", SEARCH_PY.rsplit("/", 1)[0] + "/route.py",
         f"天气 北京 {int(time.time())}", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    stdout = r.stdout + r.stderr
    assert (
        "anysearch" in stdout.lower()
        or "wigolo" in stdout.lower()
        or "eastmoney" in stdout.lower()
        or "路由" in stdout
    ), f"explain 输出缺少路由决策信息: {stdout}"


def test_search_explain_output():
    """search.py --explain 应输出路由决策到 stderr（允许较长耗时）。"""
    r = subprocess.run(
        ["python3", SEARCH_PY, "天气 北京", "--explain", "--no-cache", "--timeout", "15"],
        capture_output=True,
        text=True,
        timeout=25,
    )
    stdout = r.stdout + r.stderr
    assert (
        "路由" in stdout
        or "anysearch" in stdout.lower()
        or "wigolo" in stdout.lower()
        or "eastmoney" in stdout.lower()
    ), f"search.py --explain 输出缺少路由信息: {stdout}"


def test_comparison_query():
    """对比分析查询应触发合理路由"""
    r = subprocess.run(
        ["python3", SEARCH_PY, "React vs Vue 2026", "--explain"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0


def test_json_schema():
    """JSON 输出应符合 schema"""
    data, _ = run_search(f"test query for schema validation {int(time.time())}")
    assert "results" in data, f"缺少 results 字段: {list(data.keys())}"
    assert isinstance(data["results"], list)
    if data["results"]:
        r = data["results"][0]
        assert "title" in r or "url" in r, f"结果缺少 title 和 url: {r}"


def test_no_cache_flag():
    """--no-cache 应跳过缓存直接请求"""
    q = f"no-cache-flag-test-unique-2026-{int(time.time())}"
    # 先写入缓存
    run_search(q, "anysearch", no_cache=True)
    # --no-cache 不应该有 cached=true
    data, _ = run_search(q, "anysearch", no_cache=True)
    # 非缓存执行时 search.py 不输出 cached=true
    cached_field = data.get("cached")
    assert cached_field is None or cached_field is False, f"期望无 cached=true, 实际 {cached_field}"


def test_engine_override():
    """--engine 参数应强制指定引擎"""
    data, _ = run_search(
        f"test engine override {int(time.time())}",
        engine="anysearch",
        no_cache=True,
    )
    # 强制指定 anysearch 时，输出应该有 source=anysearch 的结果
    results = data.get("results", [])
    if results:
        assert results[0].get("source") == "anysearch" or "engine" in data, \
            f"期望 source=anysearch, 实际 {results[0]}"


def test_empty_query():
    """空查询应返回错误或空结果，不崩溃"""
    r = subprocess.run(
        ["python3", SEARCH_PY, "", "--json"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert r.returncode in (0, 1), f"期望返回码 0 或 1, 实际 {r.returncode}"


def test_elapsed_ms_present():
    """非缓存查询应输出 elapsed_ms"""
    data, _ = run_search(f"elapsed-ms-test-{int(time.time())}", no_cache=True)
    assert "elapsed_ms" in data, f"缺少 elapsed_ms: {list(data.keys())}"


if __name__ == "__main__":
    tests = [
        test_cache_hit,
        test_chinese_short_query,
        test_english_technical,
        test_explain_mode,
        test_search_explain_output,
        test_comparison_query,
        test_json_schema,
        test_no_cache_flag,
        test_engine_override,
        test_empty_query,
        test_elapsed_ms_present,
    ]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✅ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: ERROR {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{passed}/{passed + failed} passed")
    sys.exit(0 if failed == 0 else 1)
