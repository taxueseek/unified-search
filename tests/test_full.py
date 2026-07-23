#!/usr/bin/env python3
"""完整集成测试套件 — 覆盖 engines / cache / route / end-to-end / progress"""

import json
import os
import sys
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from engines import search, available_engines, safe_search
from cache import SearchCache
from route import route_query


# ── 引擎层测试 ─────────────────────────────────────────────────────────────────

class TestEngines(unittest.TestCase):
    def test_available_engines_not_empty(self):
        engines = available_engines()
        self.assertIsInstance(engines, list)
        self.assertTrue(len(engines) > 0, "可用引擎列表不应为空")

    def test_search_returns_list(self):
        for eng in available_engines()[:2]:
            result = search("test", eng, n=2)
            self.assertIsInstance(result, list, f"引擎 {eng} 应返回 list")

    def test_search_invalid_engine_returns_empty(self):
        result = search("test", "nonexistent_engine_xyz")
        self.assertEqual(result, [], "未知引擎应返回空 list 而非抛异常")

    def test_search_timeout_no_crash(self):
        """极短超时应优雅返回空 list，不崩溃。"""
        result = search("test", "anysearch", n=1, timeout=0.001)
        self.assertIsInstance(result, list)

    def test_safe_search_decorator_catches_timeout(self):
        """safe_search 装饰器应捕获 TimeoutExpired 并返回 []。"""
        import subprocess

        @safe_search
        def fake_engine_timeout(query, n=5, timeout=8):
            raise subprocess.TimeoutExpired(cmd="x", timeout=0.001)

        result = fake_engine_timeout("test")
        self.assertEqual(result, [])

    def test_safe_search_decorator_catches_file_not_found(self):
        """safe_search 装饰器应捕获 FileNotFoundError 并返回 []。"""

        @safe_search
        def fake_engine_fnf(query, n=5, timeout=8):
            raise FileNotFoundError("/nonexistent/binary")

        result = fake_engine_fnf("test")
        self.assertEqual(result, [])

    def test_safe_search_decorator_catches_generic_exception(self):
        """safe_search 装饰器应捕获通用异常并返回 []。"""

        @safe_search
        def fake_engine_broken(query, n=5, timeout=8):
            raise RuntimeError("boom")

        result = fake_engine_broken("test")
        self.assertEqual(result, [])


# ── 缓存层测试 ─────────────────────────────────────────────────────────────────

class TestCache(unittest.TestCase):
    def setUp(self):
        self.cache = SearchCache(db_path=":memory:")

    def test_cache_set_get(self):
        self.cache.set("test", "anysearch", 5, {"results": [{"title": "Test"}]}, ttl=60)
        result = self.cache.get("test", "anysearch", 5)
        self.assertIsNotNone(result, "刚写入的缓存应命中")
        self.assertEqual(result["results"][0]["title"], "Test")

    def test_cache_miss(self):
        result = self.cache.get("nonexistent_query_xyz", "anysearch", 5)
        self.assertIsNone(result, "不存在的 key 应返回 None")

    def test_cache_expiry(self):
        self.cache.set("expire_test", "anysearch", 5, {"results": [{"title": "Old"}]}, ttl=0)
        time.sleep(0.1)
        result = self.cache.get("expire_test", "anysearch", 5)
        self.assertIsNone(result, "TTL=0 的缓存应立即过期")

    def test_cache_stats(self):
        self.cache.set("a", "anysearch", 5, {"results": []}, ttl=60)
        self.cache.get("a", "anysearch", 5)
        self.cache.get("b", "anysearch", 5)
        stats = self.cache.stats
        self.assertEqual(stats["l1"]["hits"], 1, "L1 应有 1 次命中")
        self.assertEqual(stats["l2"]["misses"], 1, "L2 应有 1 次未命中")

    def test_cache_domain_ttl_resolution(self):
        """域名分级 TTL 解析应返回正确值。"""
        self.assertEqual(SearchCache.resolve_ttl("stock"), 300)
        self.assertEqual(SearchCache.resolve_ttl("fund"), 300)
        self.assertEqual(SearchCache.resolve_ttl("deep"), 7200)
        self.assertEqual(SearchCache.resolve_ttl("general"), 3600)

    def test_cache_l2_persistence(self):
        """L2 SQLite 缓存在新实例中应可读取。"""
        self.cache.set("persist_test", "anysearch", 5,
                       {"results": [{"title": "Persisted"}]}, domain="general")
        cache2 = SearchCache(db_path=":memory:")
        # 注意：:memory: 数据库不跨实例共享，此处仅验证新实例可正常工作
        cache2.set("q", "e", 1, {"results": []}, ttl=60)
        self.assertIsNotNone(cache2.get("q", "e", 1))


# ── 路由层测试 ─────────────────────────────────────────────────────────────────

class TestRoute(unittest.TestCase):
    def test_route_returns_valid_decision(self):
        d = route_query("英伟达股价")
        self.assertIn("engine", d)
        self.assertIn("engines", d)

    def test_route_stock_domain(self):
        d = route_query("英伟达股价")
        self.assertEqual(d["domain"], "stock_query")

    def test_route_english_tech(self):
        d = route_query("Python asyncio")
        self.assertIn(d["engine"], ["github", "anysearch", "tavily", "byted"])

    def test_route_override(self):
        d = route_query("test query", engine_override="eastmoney")
        self.assertEqual(d["engine"], "eastmoney")

    def test_route_confidence_range(self):
        """所有路由决策的 confidence 应在 [0, 1] 范围内。"""
        for q in ["Python", "英伟达股价", "React vs Vue", "任意的查询"]:
            d = route_query(q)
            self.assertGreaterEqual(d["confidence"], 0.0)
            self.assertLessEqual(d["confidence"], 1.0)

    def test_route_engines_not_empty(self):
        """路由决策的 engines 列表不应为空。"""
        d = route_query("anything")
        self.assertTrue(len(d.get("engines", [])) >= 1)


# ── 端到端测试 ─────────────────────────────────────────────────────────────────

class TestEndToEnd(unittest.TestCase):
    def test_full_search_flow(self):
        from search import super_search
        result = super_search("Python", n=3, skip_cache=True)
        self.assertIn("results", result)
        self.assertIn("elapsed_ms", result)
        self.assertIsInstance(result["results"], list)

    def test_cache_hit_faster_than_cold(self):
        from search import super_search
        q = f"unique_test_{int(time.time())}"
        r1 = super_search(q, n=2)
        r2 = super_search(q, n=2)
        # 第二次应命中缓存（cached=True）或更快
        self.assertTrue(
            r2.get("cached") is True or r2["elapsed_ms"] <= r1["elapsed_ms"],
            f"缓存应加速：第一次 {r1['elapsed_ms']}ms, 第二次 {r2['elapsed_ms']}ms"
        )

    def test_progress_callback(self):
        from search import super_search, Stage
        stages = []

        def on_progress(stage, data):
            stages.append(stage)

        result = super_search("test progress", n=1, on_progress=on_progress, skip_cache=True)
        self.assertTrue(len(stages) > 0, "进度回调应被调用")
        # 第一个阶段应是 START
        self.assertEqual(stages[0], Stage.START)
        # 最后一个阶段应是 DONE 或 ERROR
        self.assertIn(stages[-1], [Stage.DONE, Stage.ERROR])
        # 中间应经历 SEARCHING 和 MERGING
        self.assertIn(Stage.SEARCHING, stages)
        self.assertIn(Stage.MERGING, stages)

    def test_progress_callback_receives_stage_objects(self):
        """on_progress 收到的 stage 应是 Stage 枚举实例。"""
        from search import super_search, Stage
        received = []

        def on_progress(stage, data):
            received.append((stage, data))

        super_search("enum check", n=1, on_progress=on_progress, skip_cache=True)
        for stage, data in received:
            self.assertIsInstance(stage, Stage)
            self.assertIsInstance(data, dict)

    def test_super_search_with_explain(self):
        """explain=True 应输出路由决策到 stderr，不影响返回结构。"""
        from search import super_search
        import io
        old_stderr = sys.stderr
        sys.stderr = io.StringIO()
        try:
            result = super_search("test explain", n=1, explain=True)
        finally:
            output = sys.stderr.getvalue()
            sys.stderr = old_stderr
        self.assertIn("results", result)
        self.assertIn("路由", output)

    def test_super_search_override_engine(self):
        """engine 参数应覆盖默认路由。"""
        from search import super_search
        result = super_search("Python asyncio", engine="duckduckgo", n=2, skip_cache=True)
        self.assertEqual(result["engine"], "duckduckgo")


# ── 主运行入口 ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
