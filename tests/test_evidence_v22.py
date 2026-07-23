#!/usr/bin/env python3
"""Argo evidence v2.2 单元测试：Selection×Absorption、SERP 降权、时效、证据密度。"""
from __future__ import annotations

import os
import sys
import unittest

SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPT_DIR))

from content_signals import score_evidence_density, compute_content_quality  # noqa: E402
from evidence import (  # noqa: E402
    compute_credibility,
    is_serp_or_jump_url,
    score_authority,
    score_freshness,
)


class TestSerpDemotion(unittest.TestCase):
    def test_baidu_search_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url(
                "https://www.baidu.com/s?wd=%E5%85%AC%E5%8B%9F%E5%9F%BA%E9%87%91"
            )
        )

    def test_baidu_link_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url(
                "http://www.baidu.com/link?url=abc123xyz"
            )
        )

    def test_sogou_link_is_serp(self):
        self.assertTrue(
            is_serp_or_jump_url("https://www.sogou.com/link?url=xxx")
        )

    def test_real_article_not_serp(self):
        self.assertFalse(
            is_serp_or_jump_url(
                "https://finance.eastmoney.com/a/202607213815797914.html"
            )
        )

    def test_serp_authority_very_low(self):
        auth = score_authority("https://www.baidu.com/s?wd=test")
        self.assertTrue(auth["is_serp"])
        self.assertLessEqual(auth["score"], 0.15)


class TestEvidenceDensity(unittest.TestCase):
    def test_numbers_and_compare_boost(self):
        text = "截至2026年二季度末，电子行业持仓占比升至约43%，通信约17%，环比大增。"
        d = score_evidence_density(text, "公募持仓")
        self.assertTrue(d["has_numbers"])
        self.assertTrue(d["has_comparison"])
        self.assertGreaterEqual(d["absorption_score"], 0.45)

    def test_qa_title_penalty(self):
        weak = score_evidence_density("随便聊聊", "基金怎么样？")
        strong = score_evidence_density(
            "根据披露，股票市值8.84万亿，较一季度增加9.6%。",
            "二季报股票持仓",
        )
        self.assertTrue(weak["is_qa_format"])
        self.assertGreater(strong["absorption_score"], weak["absorption_score"])

    def test_quality_includes_evidence_fields(self):
        q = compute_content_quality(
            "这是一篇关于定义的长文。" * 20
            + "所谓GEO是指生成式引擎优化。步骤如下：第一、第二。对比A与B，增长15%。",
            "GEO 定义与对比",
        )
        self.assertIn("has_numbers", q)
        self.assertIn("absorption_score", q)
        self.assertTrue(q["content_ok"] or q["word_count"] > 0)


class TestFreshness(unittest.TestCase):
    def test_ignore_historical_since_year(self):
        """「2015年以来」不应把时效打成2015旧闻。"""
        r = {
            "title": "公募极致抱团",
            "snippet": "电子+通信合计近60%，创2015年以来新高。2026年二季度末主动偏股基金加仓。",
            "url": "https://wallstreetcn.com/articles/3777647",
        }
        f = score_freshness(r)
        # 应识别到 2026 而非 2015
        self.assertIn("2026", f["reason"])
        self.assertGreaterEqual(f["score"], 0.7)

    def test_full_date_preferred(self):
        r = {
            "title": "基金持仓",
            "snippet": "发布于2026年07月21日，电子持仓占比上升。",
            "url": "https://example.com/a",
        }
        f = score_freshness(r)
        self.assertIn("2026", f["reason"])
        self.assertGreaterEqual(f["score"], 0.8)


class TestTwoStageCredibility(unittest.TestCase):
    def test_evidence_page_ranks_above_serp(self):
        results = [
            {
                "title": "相关搜索公募基金持仓",
                "url": "https://www.baidu.com/s?wd=公募",
                "snippet": "",
                "score": 0.9,
                "source": "local_baidu",
            },
            {
                "title": "中际旭创取代宁德时代登顶",
                "url": "https://finance.eastmoney.com/a/202607213815797914.html",
                "snippet": "截至2026年二季度末，公募股票持仓8.84万亿，较一季度增加9.60%。",
                "score": 0.5,
                "source": "anysearch",
            },
        ]
        report = compute_credibility(results, "2026公募基金二季报持仓")
        self.assertEqual(report["framework"], "selection_x_absorption_v2.2")
        best = report["results"][0]
        self.assertIn("eastmoney", best["url"])
        self.assertGreaterEqual(best["credibility"]["final"], 0.5)
        # SERP 应在后面或显著更低
        serp = next(r for r in report["results"] if "baidu.com/s" in r["url"])
        self.assertLess(serp["credibility"]["final"], best["credibility"]["final"])
        self.assertTrue(serp["credibility"]["authority"]["is_serp"])

    def test_commercial_ranking_demoted(self):
        auth = score_authority("https://www.maigoo.com/brand/list.html")
        self.assertLessEqual(auth["score"], 0.35)

    def test_cs_com_cn_high(self):
        auth = score_authority(
            "https://www.cs.com.cn/tzjj/01/2026/07/22/detail_2026072210026163.html"
        )
        self.assertGreaterEqual(auth["score"], 0.85)


if __name__ == "__main__":
    unittest.main()
