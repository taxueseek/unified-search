#!/usr/bin/env python3
"""
query_rewriter.py — 查询改写引擎（v2.5 新增）

原理：
  1. 复用 clarify.py 的歧义词库 + 品牌碰撞检测
  2. 高置信度消歧时，向查询追加领域关键词
  3. 混合语言查询时，提取英文部分作为备选改写
  4. 返回 (original, rewritten, confidence, reason)

用法：
  from query_rewriter import rewrite_query
  result = rewrite_query("苹果股价")
  # → rewritten="苹果股价 Apple 公司 AAPL 股票行情", confidence=0.85
"""

from __future__ import annotations

import re
from typing import Any


# ── 消歧改写映射 ──────────────────────────────────────────────────────────────
# 格式: (歧义词, 领域) → 追加的关键词
# 当歧义词被高置信度消歧后，将这些关键词追加到查询中

REWRITE_MAP: dict[tuple[str, str], str] = {
    # 科技公司/产品
    ("苹果", "finance"): "Apple 公司 AAPL 股票行情",
    ("苹果", "tech"): "Apple 公司 iPhone Mac iOS",
    ("Python", "tech"): "Python 编程语言 pip 库 框架",
    ("Java", "tech"): "Java 编程语言 JDK Spring JVM",
    ("Rust", "tech"): "Rust 编程语言 cargo crate 所有权",
    ("小米", "tech"): "小米科技 手机 MIUI HyperOS",
    ("小米", "auto"): "小米汽车 SU7",
    ("华为", "tech"): "华为技术 鸿蒙 5G 芯片 Mate",
    ("特斯拉", "auto"): "Tesla 电动汽车 Model FSD",
    ("蔚来", "auto"): "蔚来汽车 NIO 换电 ET5 ES6",
    ("理想", "auto"): "理想汽车 Li Auto 增程 L系列 MEGA",
    ("小鹏", "auto"): "小鹏汽车 XPeng P7 G6 智驾",
    ("芯片", "tech"): "半导体芯片 IC GPU 光刻机",
    ("Transformer", "tech"): "Transformer 模型 AI 深度学习 attention NLP",
    ("GPT", "tech"): "GPT 大语言模型 OpenAI ChatGPT GPT-4",
    ("RAG", "tech"): "RAG 检索增强生成 向量 embedding 知识库",
    ("Gemini", "tech"): "Google Gemini AI 大模型 多模态",
    ("Claude", "tech"): "Claude Anthropic AI 助手 大模型",
    ("Docker", "tech"): "Docker 容器化 镜像 compose Kubernetes 部署",
    ("Go", "tech"): "Go 编程语言 Golang 并发 goroutine channel",
    ("Redis", "tech"): "Redis 缓存数据库 内存 键值 持久化",
    ("Swift", "tech"): "Swift 编程语言 iOS macOS SwiftUI",
    ("Kotlin", "tech"): "Kotlin 编程语言 Android JVM JetBrains",
    ("Cursor", "tech"): "Cursor AI 代码编辑器 VSCode Composer",
    ("Notion", "tech"): "Notion 协作工具 笔记 数据库 模板",
    ("飞书", "tech"): "飞书 字节跳动 协作 OKR 文档",
    ("抖音", "tech"): "抖音 TikTok 短视频 直播 字节跳动",
    ("微信", "tech"): "微信 WeChat 小程序 公众号 支付 腾讯",
    ("Anthropic", "tech"): "Anthropic AI 公司 Claude 大模型",
    ("OpenAI", "tech"): "OpenAI AI 公司 GPT ChatGPT",

    # 金融
    ("茅台", "finance"): "贵州茅台 600519 白酒 股票行情",
    ("比特币", "finance"): "Bitcoin BTC 加密货币 区块链 挖矿",
    ("A股", "finance"): "A股市场 上证 深证 创业板 科创板",
    ("ETF", "finance"): "ETF 交易所交易基金 指数基金 净值",
    ("量化", "finance"): "量化投资 量化交易 对冲 因子 Alpha 回测",
    ("期权", "finance"): "期权 金融衍生品 认购 认沽 行权 波动率",
    ("基金", "finance"): "公募基金 私募基金 净值 回撤 基金经理",
    ("新能源", "auto"): "新能源汽车 电动车 充电桩 续航",
    ("新能源", "energy"): "新能源 光伏 风电 储能 碳中和",
    ("Swift", "finance"): "Swift 国际结算 银行 转账",

    # 学术
    ("Python", "nature"): "Python 蟒蛇 爬行动物 宠物",
    ("特斯拉", "science"): "尼古拉·特斯拉 交流电 发明 无线电",
}

# 品牌碰撞 → 自动追加的域名限定词
BRAND_REWRITE: dict[str, str] = {
    "Apple": "Apple Inc 公司",
    "Amazon": "Amazon 电商 AWS",
    "小米": "小米科技 公司",
    "华为": "华为技术 公司",
    "特斯拉": "Tesla 公司",
    "字节": "字节跳动 ByteDance",
}


# ── 辅助函数 ──────────────────────────────────────────────────────────────────

_RE_CHINESE = re.compile(r"[\u4e00-\u9fff]")
_RE_ENGLISH_WORD = re.compile(r"\b[a-zA-Z]{2,}\b")


def _has_chinese(text: str) -> bool:
    return bool(_RE_CHINESE.search(text))


def _has_english(text: str) -> bool:
    return bool(_RE_ENGLISH_WORD.search(text))


def _extract_english_terms(text: str) -> list[str]:
    """提取英文术语（用于混合语言查询的拆分改写）。"""
    return [w.lower() for w in _RE_ENGLISH_WORD.findall(text) if len(w) > 2]


# ── 主改写函数 ─────────────────────────────────────────────────────────────────

def rewrite_query(query: str, min_confidence: float = 0.7) -> dict[str, Any]:
    """改写查询，追加领域关键词以提升搜索质量。

    Args:
        query: 原始查询
        min_confidence: 消歧置信度阈值，低于此值不做改写

    Returns:
        {
            "original": str,           # 原始查询
            "rewritten": str | None,   # 改写后的查询（无改写时为 None）
            "confidence": float,       # 改写置信度 (0.0-1.0)
            "reason": str,             # 改写原因
            "type": str,               # 改写类型: disambiguate / split / direct
        }
    """
    # 延迟导入避免循环依赖
    from clarify import AMBIGUOUS_TERMS, BRAND_COLLISIONS

    rewritten_parts: list[str] = []
    reasons: list[str] = []
    total_confidence = 0.0
    match_count = 0

    # ── 策略 1：歧义消解改写 ──
    query_lower = query.lower()
    for term, info in AMBIGUOUS_TERMS.items():
        # 在查询中定位歧义词（大小写不敏感）
        term_lower = term.lower()
        if term_lower not in query_lower and not (
            term.isascii()
            and re.search(r"\b" + re.escape(term) + r"\b", query, re.I)
        ):
            continue

        best_domain = None
        best_conf = 0.0
        best_weight = 0.0

        for meaning in info["meanings"]:
            domain = meaning["domain"]
            keywords = info.get("disambiguation_keywords", {}).get(domain, [])
            # 大小写不敏感匹配
            match_count_kw = sum(
                1 for kw in keywords if kw.lower() in query_lower
            )

            # 跨域强信号：金融/汽车/学术术语对任何领域都有效
            strong_bonus = 0.0
            is_auto_signal = False
            if re.search(
                r"(SU7|Model\s*[SY3X]|ET5|ES6|ES8|P7|G6|G9|L\d|MEGA|AION|X9|FSD|充电桩|续航|换电)",
                query, re.I
            ):
                is_auto_signal = True

            if re.search(
                r"(股价|财报|市值|基金|ETF|净值|涨跌|K线|分红|营收|利润|ROE|研报)",
                query
            ):
                strong_bonus = 0.25
            elif is_auto_signal:
                strong_bonus = 0.35
            elif re.search(
                r"(论文|paper|arxiv|API|SDK|框架|库|编程|代码|编译|部署|开源|repo)",
                query, re.I
            ):
                strong_bonus = 0.2
            elif domain == "energy" and re.search(
                r"(光伏|风电|储能|碳中和|太阳能|风能)", query
            ):
                strong_bonus = 0.25

            # 汽车信号：自动优先 auto 域
            if is_auto_signal and domain == "auto":
                strong_bonus = max(strong_bonus, 0.6)
                match_count_kw += 5  # 虚拟关键词匹配，确保 auto 域胜出

            weight = meaning["weight"] + match_count_kw * 0.12 + strong_bonus
            # 强信号对置信度的贡献更大（0.7 vs 关键词 0.12）
            conf = min(
                0.5 + match_count_kw * 0.22 + strong_bonus * 0.7, 0.95
            )

            if conf > best_conf or (conf == best_conf and weight > best_weight):
                best_domain = domain
                best_conf = conf
                best_weight = weight

        if best_domain and best_conf >= min_confidence:
            rewrite_key = (term, best_domain)
            append = REWRITE_MAP.get(rewrite_key)
            if append:
                rewritten_parts.append(append)
                reasons.append(f"「{term}」→ {best_domain} 领域")
                total_confidence += best_conf
                match_count += 1

    # ── 策略 2：混合语言拆分 ──
    if _has_chinese(query) and _has_english(query):
        eng_terms = _extract_english_terms(query)
        technical = any(
            t
            in {
                "api", "sdk", "http", "json", "xml", "css", "html", "sql",
                "npm", "pip", "git", "ssh", "rest", "graphql", "grpc",
                "react", "vue", "node", "python", "java", "rust", "go",
                "docker", "kubernetes", "linux", "nginx", "redis",
                "async", "await", "thread", "process", "async",
            }
            for t in eng_terms
        )
        if technical and match_count == 0:
            appended = " ".join(eng_terms)
            rewritten_parts.append(appended)
            reasons.append(f"混合语言拆分：{appended}")
            if not total_confidence:
                total_confidence = 0.6
            match_count += 1

    # ── 构建结果 ──
    if not rewritten_parts:
        return {
            "original": query,
            "rewritten": None,
            "confidence": 0.0,
            "reason": "无需改写",
            "type": "direct",
        }

    avg_confidence = round(total_confidence / match_count, 2)
    rewritten = query + " " + " ".join(rewritten_parts)

    return {
        "original": query,
        "rewritten": rewritten.strip(),
        "confidence": avg_confidence,
        "reason": "；".join(reasons),
        "type": "disambiguate",
    }


# ── CLI 测试 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json
    import sys

    tests = sys.argv[1:] if len(sys.argv) > 1 else [
        "苹果股价",
        "Python 吞苹果 兼容吗",
        "小米 su7 价格",
        "特斯拉 FSD 最新进展",
        "芯片 光刻机 ASML",
        "茅台财报 2025",
        "Transformer attention 机制论文",
        "量化 基金 回测 因子",
        "新能源 光伏 储能",
        "Rust 编程 入门",
    ]

    for q in tests:
        result = rewrite_query(q)
        if result["rewritten"]:
            print(f"原始：{q}")
            print(f"改写：{result['rewritten']}")
            print(f"置信度：{result['confidence']} | {result['reason']}")
        else:
            print(f"原始：{q} → {result['reason']}")
        print()
