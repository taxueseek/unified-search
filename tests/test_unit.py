#!/usr/bin/env python3
"""Unified Search v2 单元测试 — config / route / engines / cache / tfidf / quota / adaptive"""

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
LOCAL_SEARCH_DIR = SKILL_DIR / "sub-skills" / "local-search"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(LOCAL_SEARCH_DIR))

from config import load_config, get_engines, get_domains, get_cost_factor, get_cost_tiers
from route import extract_features, match_domain, route_query
from cache import SearchCache, DOMAIN_TIER_MAP, CACHE_TIERS
from engines import get_registry, available_engines, search as engine_search
from tfidf_router import semantic_route, SemanticRouter
from quota import QuotaManager
from adaptive import AdaptiveLearner

from engine_registry import EngineRegistry, get_engine, list_engines, list_categories, update_availability
from health_check import apply_threshold, _detect_anti_bot
from smart_router import extract_features as local_extract_features, route_query as local_route_query


class TestConfig(unittest.TestCase):
    def test_load_config(self):
        cfg = load_config(force=True)
        self.assertIn("engines", cfg)
        self.assertIn("domains", cfg)

    def test_engines_enabled(self):
        engines = get_engines()
        self.assertTrue(len(engines) >= 3)
        self.assertIn("anysearch", engines)

    def test_cost_factor(self):
        # anysearch 是 free
        self.assertEqual(get_cost_factor("anysearch"), 1.0)
        # 未知引擎默认 1.0
        self.assertEqual(get_cost_factor("nonexistent"), 1.0)

    def test_cost_tiers(self):
        tiers = get_cost_tiers()
        self.assertIn("free", tiers)
        self.assertIn("anysearch", tiers.get("free", []))

    def test_tilde_expanded(self):
        cfg = load_config(force=True)
        for spec in cfg.get("engines", {}).values():
            if "cmd" in spec and isinstance(spec["cmd"], list):
                for item in spec["cmd"]:
                    self.assertFalse(item.startswith("~"))


class TestRoute(unittest.TestCase):
    def test_user_override(self):
        d = route_query("anything", engine_override="duckduckgo")
        self.assertEqual(d["engine"], "duckduckgo")
        self.assertEqual(d["confidence"], 1.0)

    def test_stock_domain(self):
        d = route_query("英伟达股价")
        self.assertEqual(d["engine"], "eastmoney")
        self.assertIn("eastmoney", d["engines"])
        self.assertEqual(d["domain"], "stock_query")

    def test_fund_domain(self):
        d = route_query("基金净值")
        self.assertEqual(d["engine"], "eastmoney")
        self.assertEqual(d["domain"], "fund_query")

    def test_technical_english(self):
        d = route_query("Python asyncio internals")
        # TF-IDF 可将技术英文问句路由到代码/问答本地引擎，不再强制 general_search
        tech_engines = {
            "anysearch", "byted", "duckduckgo", "github",
            "local_stackoverflow", "local_github", "local_bing",
        }
        self.assertTrue(
            d["engine"] in tech_engines or any(e in tech_engines for e in d.get("engines", [])),
            f"unexpected tech route: {d.get('engine')} / {d.get('engines')}",
        )
        self.assertIn(d.get("domain"), ("general_search", None, "tech_deep", "local_code"))

    def test_mode_budget_filters_paid(self):
        d = route_query("latest AI news", mode="budget")
        # budget 模式不应包含付费引擎
        for eng in d.get("engines", []):
            self.assertNotEqual(get_cost_factor(eng), 0.3)

    def test_features_extracted(self):
        f = extract_features("React vs Vue 哪个好")
        self.assertTrue(f["has_compare"])
        self.assertGreater(f["chinese_ratio"], 0)

    def test_has_reason(self):
        d = route_query("贵州茅台股价")
        self.assertIn("reason", d)
        self.assertTrue(len(d["reason"]) > 0)


class TestTfidfRouter(unittest.TestCase):
    def test_route_returns_scores(self):
        scores = semantic_route("英伟达财报", top_k=3)
        self.assertTrue(len(scores) >= 1)
        # 确认返回 (engine, score, reason) 三元组
        for item in scores:
            self.assertEqual(len(item), 3)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], float)
            self.assertIsInstance(item[2], str)

    def test_academic_route(self):
        scores = semantic_route("transformer attention mechanism paper", top_k=3)
        engines = [s[0] for s in scores]
        self.assertTrue(any(e in engines for e in ["arxiv", "semantic_scholar", "openalex"]))

    def test_finance_route(self):
        scores = semantic_query = semantic_route("基金净值 股票行情", top_k=3)
        self.assertTrue(scores[0][1] > 0)

    def test_should_parallel(self):
        router = SemanticRouter()
        # 深度研究型查询应触发并行
        self.assertTrue(router.should_parallel("深度分析 AI 格局", []))
        # 简单查询不触发
        self.assertFalse(router.should_parallel("天气", [("duckduckgo", 0.5, "")]))


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
        self.cache.set("q2", "duckduckgo", 3, {"results": [{"title": "y"}]}, domain="general")
        cache2 = SearchCache(db_path=self.db_path)
        hit = cache2.get("q2", "duckduckgo", 3, domain="general")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("_cache_level"), "L2")

    def test_domain_ttl(self):
        self.assertEqual(SearchCache.resolve_ttl("stock_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("fund_query"), 300)
        self.assertEqual(SearchCache.resolve_ttl("tech_deep"), 7200)
        self.assertEqual(SearchCache.resolve_ttl("general_search"), 3600)
        self.assertEqual(SearchCache.resolve_ttl("unknown"), 3600)

    def test_ttl_expiry(self):
        self.cache.set("q3", "anysearch", 5, {"results": [{"title": "z"}]},
                       domain="general", ttl=0)
        time.sleep(0.05)
        self.assertIsNone(self.cache.get("q3", "anysearch", 5, domain="general"))


class TestEngines(unittest.TestCase):
    def test_registry_has_anysearch(self):
        registry = get_registry()
        self.assertIn("anysearch", registry)

    def test_available_engines(self):
        engines = available_engines()
        self.assertIn("anysearch", engines)

    def test_search_returns_list(self):
        # 模拟调用（不实际发请求，测试接口）
        result = engine_search("test nonexistent engine xyz", "nonexistent", n=1)
        self.assertIsInstance(result, list)


class TestQuota(unittest.TestCase):
    def test_quota_manager_init(self):
        mgr = QuotaManager()
        self.assertIsNotNone(mgr)

    def test_record_and_remaining(self):
        mgr = QuotaManager()
        mgr.record("test_engine", success=True)
        # 无限配额引擎返回 1.0
        self.assertEqual(mgr.get_remaining_ratio("test_engine"), 1.0)


class TestAdaptive(unittest.TestCase):
    def test_learner_init(self):
        learner = AdaptiveLearner()
        self.assertIsNotNone(learner)

    def test_record_and_score(self):
        learner = AdaptiveLearner()
        learner.record("test_engine", success=True, latency_ms=500, cost=0.0)
        score = learner.get_score("test_engine")
        self.assertIsInstance(score, float)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


class TestLocalSearchRegistry(unittest.TestCase):
    def test_registry_loads_engines(self):
        reg = EngineRegistry()
        engines = reg.list_engines()
        self.assertIn("local_arxiv", engines)
        self.assertIn("local_wikipedia", engines)

    def test_list_by_category(self):
        reg = EngineRegistry()
        self.assertIn("local_arxiv", reg.list_engines(category="academic"))
        self.assertIn("local_github", reg.list_engines(category="code"))
        self.assertIn("local_baidu", reg.list_engines(category="chinese"))

    def test_get_engine_has_fields(self):
        reg = EngineRegistry()
        eng = reg.get_engine("local_arxiv")
        self.assertIsNotNone(eng)
        self.assertIn("type", eng)
        self.assertIn("available", eng)

    def test_update_availability(self):
        reg = EngineRegistry()
        reg.update_availability("local_test_engine", False, fail_reason="unit_test")
        self.assertFalse(reg.is_available("local_test_engine"))
        reg.update_availability("local_test_engine", True)
        self.assertTrue(reg.is_available("local_test_engine"))


class TestLocalSearchHealthCheck(unittest.TestCase):
    def test_detect_anti_bot(self):
        self.assertIsNotNone(_detect_anti_bot("please complete the captcha", 200))
        self.assertEqual(_detect_anti_bot("", 429), "rate_limited")
        self.assertIsNone(_detect_anti_bot("normal result page", 200))

    def test_apply_threshold_success_recover(self):
        prev = {"available": False, "consecutive_failures": 2}
        report = {"available": True, "status": 200, "latency_ms": 500}
        self.assertTrue(apply_threshold(report, prev))

    def test_apply_threshold_two_failures(self):
        prev = {"available": True, "consecutive_failures": 1}
        report = {"available": False, "status": 503, "latency_ms": 500}
        self.assertFalse(apply_threshold(report, prev))

    def test_apply_threshold_slow_marks_unavailable(self):
        prev = {"available": True, "consecutive_failures": 0}
        report = {"available": False, "status": 200, "latency_ms": 9000}
        self.assertFalse(apply_threshold(report, prev))


class TestLocalSearchSmartRouter(unittest.TestCase):
    def test_route_academic(self):
        decision = local_route_query("transformer attention paper")
        self.assertIn("local_arxiv", decision["engines"])
        self.assertEqual(decision["domain"], "academic")

    def test_route_code(self):
        decision = local_route_query("python list comprehension stackoverflow")
        self.assertIn("local_stackoverflow", decision["engines"])

    def test_route_reference(self):
        decision = local_route_query("what is the capital of France wikipedia")
        self.assertIn("local_wikipedia", decision["engines"])

    def test_preferred_engines_override(self):
        decision = local_route_query("anything", preferred_engines=["local_github"], require_available=False)
        self.assertEqual(decision["engines"], ["local_github"])


class TestLocalSearchConfigIntegration(unittest.TestCase):
    def test_local_search_in_cost_tiers(self):
        tiers = get_cost_tiers()
        self.assertIn("local_search", tiers.get("free", []))
        self.assertIn("local_arxiv", tiers.get("free", []))

    def test_local_search_engine_config(self):
        engines = get_engines()
        self.assertIn("local_search", engines)
        self.assertEqual(engines["local_search"].get("type"), "cli")


if __name__ == "__main__":
    unittest.main(verbosity=2)
