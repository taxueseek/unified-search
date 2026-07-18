#!/usr/bin/env python3
"""统一搜索单元测试 — 覆盖 config / route / engines / cache"""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from config import load_config, get_engines, get_domains
from route import extract_features, match_domain, route_query
from cache import SearchCache, DOMAIN_TIER_MAP, CACHE_TIERS
from engines import get_registry, available_engines


class TestConfig(unittest.TestCase):
    def test_load_config_returns_dict(self):
        cfg = load_config(force=True)
        self.assertIn("engines", cfg)
        self.assertIn("domains", cfg)
        self.assertIn("cache", cfg)

    def test_engines_are_enabled(self):
        engines = get_engines()
        # 核心引擎必须可用：anysearch, zhihu, tavily, byted, arxiv, eastmoney
        self.assertTrue(len(engines) >= 3)
        self.assertIn("anysearch", engines)

    def test_domains_have_required_fields(self):
        domains = get_domains()
        for d in domains:
            self.assertIn("name", d)
            self.assertIn("primary", d)
            self.assertIn("patterns", d)

    def test_tilde_expanded(self):
        cfg = load_config(force=True)
        engines = cfg.get("engines", {})
        for spec in engines.values():
            if "cmd" in spec and isinstance(spec["cmd"], list):
                for item in spec["cmd"]:
                    self.assertFalse(item.startswith("~"))


class TestRoute(unittest.TestCase):
    def test_user_override(self):
        d = route_query("anything", engine_override="wigolo")
        self.assertEqual(d["engine"], "wigolo")
        self.assertEqual(d["confidence"], 1.0)

    def test_stock_domain(self):
        d = route_query("英伟达股价")
        self.assertEqual(d["engine"], "eastmoney")
        self.assertIn("eastmoney", d["engines"])
        self.assertEqual(d["domain"], "stock_query")
        self.assertGreaterEqual(d["confidence"], 0.9)

    def test_fund_domain(self):
        d = route_query("基金净值")
        self.assertEqual(d["engine"], "eastmoney")
        self.assertEqual(d["domain"], "fund_query")

    def test_technical_english(self):
        d = route_query("Python asyncio internals")
        # 命中 code_search（asyncio 命中代码模式）
        self.assertIn(d["engine"], ["github", "anysearch", "tavily", "byted"])
        self.assertIn(d["domain"], ["code_search", "general_search"])

    def test_general_catch_all(self):
        d = route_query("random general query")
        self.assertEqual(d["domain"], "general_search")
        self.assertTrue(len(d.get("engines", [])) >= 1)

    def test_features_extracted(self):
        f = extract_features("React vs Vue 哪个好")
        self.assertTrue(f["has_compare"])
        self.assertGreater(f["chinese_ratio"], 0)

    def test_match_domain_stock(self):
        d = match_domain("贵州茅台股价")
        self.assertIsNotNone(d)
        self.assertEqual(d.get("name"), "stock_query")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_cache.db")
        self.cache = SearchCache(db_path=self.db_path)
        self.cache.clear(older_than_hours=0)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_l1_hit(self):
        self.cache.set("q1", "anysearch", 5, {"results": [{"title": "x"}]}, domain="general")
        hit = self.cache.get("q1", "anysearch", 5, domain="general")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_cache_level"), "L1")

    def test_l2_persist(self):
        self.cache.set("q2", "wigolo", 3, {"results": [{"title": "y"}]}, domain="general")
        # 创建新实例，强制读 L2
        cache2 = SearchCache(db_path=self.db_path)
        hit = cache2.get("q2", "wigolo", 3, domain="general")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_cache_level"), "L2")

    def test_domain_ttl(self):
        self.assertEqual(SearchCache.resolve_ttl("stock_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("fund_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("financial_news"), 600)
        self.assertEqual(SearchCache.resolve_ttl("news_realtime"), 900)
        self.assertEqual(SearchCache.resolve_ttl("tech_deep"), 7200)
        self.assertEqual(SearchCache.resolve_ttl("general_search"), 3600)
        self.assertEqual(SearchCache.resolve_ttl("general"), 3600)
        self.assertEqual(SearchCache.resolve_ttl("unknown_domain"), 3600)

    def test_ttl_expiration(self):
        self.cache.set("q3", "anysearch", 5, {"results": [{"title": "z"}]},
                       domain="general", ttl=0)
        time.sleep(0.05)
        hit = self.cache.get("q3", "anysearch", 5, domain="general")
        self.assertIsNone(hit)

    def test_tier_stats(self):
        self.cache.set("q4", "anysearch", 5, {"results": []}, domain="stock_query")
        stats = self.cache.stats
        self.assertIn("l1", stats)
        self.assertIn("l2", stats)
        self.assertIn("tiers", stats)


class TestEngines(unittest.TestCase):
    def test_registry_has_anysearch(self):
        registry = get_registry()
        self.assertIn("anysearch", registry)

    def test_available_engines(self):
        engines = available_engines()
        self.assertIn("anysearch", engines)
        self.assertIn("zhihu", engines)
        self.assertIn("tavily", engines)


class TestEndToEnd(unittest.TestCase):
    def run_search(self, query, engine="auto", timeout=10, no_cache=False):
        cmd = ["python3", str(SCRIPT_DIR / "search.py"), query,
               "--engine", engine, "--json"]
        if no_cache:
            cmd.append("--no-cache")
        r = __import__("subprocess").run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(r.stdout) if r.stdout else {}

    def test_routes_stock_to_eastmoney(self):
        data = self.run_search("贵州茅台股价", no_cache=True)
        self.assertEqual(data.get("engine"), "eastmoney")
        self.assertGreaterEqual(data.get("count", 0), 0)

    def test_json_schema(self):
        data = self.run_search(f"schema-test-{time.time()}")
        self.assertIn("results", data)
        self.assertIn("elapsed_ms", data)

    def test_engine_override(self):
        data = self.run_search("Python asyncio", engine="arxiv", no_cache=True)
        self.assertEqual(data.get("engine"), "arxiv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
