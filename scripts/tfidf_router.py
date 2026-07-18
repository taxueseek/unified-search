#!/usr/bin/env python3
"""
TF-IDF 语义路由引擎 — 替代纯正则匹配，提供更灵活的查询理解。

原理：
  1. 加载各引擎的领域文档，构建语料库
  2. 对每个引擎计算 TF-IDF 向量（代表该领域的"语义中心"）
  3. 新查询来了，向量化后与各引擎算余弦相似度
  4. 结合配额状态打分，输出最优引擎 + 备选列表

所有计算纯本地，零 API 开销，典型延迟 <5ms。

适配 unified-search 架构。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional

# ── 路径 ──────────────────────────────────────────────
SKILL_DIR = Path(__file__).parent.parent
BACKENDS_DIR = SKILL_DIR / "backends"
DOMAIN_PROFILES_PATH = BACKENDS_DIR / "domain_profiles.json"

# ── 分词 ──────────────────────────────────────────────
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """简单分词：中文单字 + 英文单词，统一小写。"""
    tokens = []
    for match in _TOKEN_PATTERN.finditer(text.lower()):
        token = match.group()
        if len(token) == 1 and '\u4e00' <= token <= '\u9fff':
            tokens.append(token)
        elif len(token) > 1:
            tokens.append(token)
    return tokens


def tokenize_with_bigrams(text: str) -> list[str]:
    """分词 + 二元组，捕捉短语信息。"""
    unigrams = tokenize(text)
    bigrams = []
    for i in range(len(unigrams) - 1):
        bigrams.append(f"{unigrams[i]}_{unigrams[i+1]}")
    return unigrams + bigrams


# ── TF-IDF 计算 ────────────────────────────────────────
class TfidfVectorizer:
    """轻量 TF-IDF 向量化器，不需要 sklearn。"""

    def __init__(self):
        self.idf: dict[str, float] = {}
        self.vocab: set[str] = set()

    def fit(self, documents: list[str]) -> None:
        """从文档集合计算 IDF。"""
        df: Counter = Counter()
        total = len(documents)

        for doc in documents:
            tokens = set(tokenize_with_bigrams(doc))
            self.vocab.update(tokens)
            for token in tokens:
                df[token] += 1

        # IDF = log(N / df) + 1（平滑）
        for token, count in df.items():
            self.idf[token] = math.log((total + 1) / (count + 1)) + 1

    def transform(self, text: str) -> dict[str, float]:
        """把文本转为 TF-IDF 向量（稀疏表示）。"""
        tokens = tokenize_with_bigrams(text)
        tf = Counter(tokens)
        total = len(tokens) if tokens else 1

        vec = {}
        for token, count in tf.items():
            if token in self.idf:
                vec[token] = (count / total) * self.idf[token]
        return vec


# ── 余弦相似度 ────────────────────────────────────────
def cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    """两个稀疏向量的余弦相似度。"""
    if not vec_a or not vec_b:
        return 0.0

    common = set(vec_a.keys()) & set(vec_b.keys())
    dot = sum(vec_a[k] * vec_b[k] for k in common)

    norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
    norm_b = math.sqrt(sum(v * v for v in vec_b.values()))

    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── 配额感知 ──────────────────────────────────────────
def _load_quota_state() -> dict:
    """加载配额状态文件。"""
    quota_path = Path.home() / ".cache" / "unified-search" / "quota.json"
    if not quota_path.exists():
        return {}
    try:
        return json.loads(quota_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _quota_ratio(engine: str, quota_state: dict) -> float:
    """计算配额剩余比例。返回 0.0-1.0，无记录时默认 1.0。"""
    info = quota_state.get(engine)
    if not info:
        return 1.0
    limit = info.get("limit", 100)
    used = info.get("used", 0)
    remaining = max(0, limit - used)
    return remaining / limit


# ── 主路由引擎 ────────────────────────────────────────
class SemanticRouter:
    """TF-IDF 语义路由引擎。"""

    def __init__(self):
        self.vectorizer = TfidfVectorizer()
        self.engine_names: list[str] = []
        self.engine_vectors: dict[str, dict[str, float]] = {}
        self.boost_keywords: dict[str, dict[str, float]] = {}
        self.boost_combos: dict[str, dict[str, float]] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """懒加载领域文档，只加载一次。"""
        if self._loaded:
            return

        if not DOMAIN_PROFILES_PATH.exists():
            self._loaded = True
            return

        try:
            profiles = json.loads(DOMAIN_PROFILES_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            self._loaded = True
            return

        # 构建语料库：每个引擎一个"伪文档"（所有领域文档拼接）
        corpus = []
        for name, profile in profiles.items():
            if name.startswith("_"):
                continue
            self.engine_names.append(name)
            self.boost_keywords[name] = profile.get("boost_keywords", {})
            self.boost_combos[name] = profile.get("boost_combos", {})
            combined = " ".join(profile.get("documents", []))
            corpus.append(combined)

        # 训练 TF-IDF
        if corpus:
            self.vectorizer.fit(corpus)

            # 为每个引擎计算代表向量
            for i, name in enumerate(self.engine_names):
                self.engine_vectors[name] = self.vectorizer.transform(corpus[i])

        self._loaded = True

    def route(self, query: str, top_k: int = 3,
              quota_aware: bool = True) -> list[tuple[str, float]]:
        """
        路由查询到最匹配的引擎。

        返回: [(engine_name, score), ...] 按分数降序。
        """
        self._ensure_loaded()

        if not self.engine_names:
            return [("anysearch", 0.0)]

        # 向量化查询
        query_vec = self.vectorizer.transform(query)

        # 计算与每个引擎的余弦相似度
        scores = []
        quota_state = _load_quota_state() if quota_aware else {}

        for name in self.engine_names:
            sim = cosine_similarity(query_vec, self.engine_vectors[name])

            # boost 加权：累加制，多个关键词命中叠加效果
            boost = 1.0
            for kw, weight in self.boost_keywords.get(name, {}).items():
                if kw.lower() in query.lower():
                    boost += weight - 1.0

            # 组合关键词加成：多个词同时出现时额外加分
            for combo, bonus in self.boost_combos.get(name, {}).items():
                combo_words = combo.split()
                if all(w.lower() in query.lower() for w in combo_words):
                    boost += bonus

            final_score = sim * boost

            # 配额感知惩罚
            if quota_aware and quota_state:
                qr = _quota_ratio(name, quota_state)
                if qr < 0.2:
                    final_score *= qr * 2
                elif qr < 0.5:
                    final_score *= 0.5 + qr

            scores.append((name, round(final_score, 4)))

        # 按分数降序
        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    def should_parallel(self, query: str, scores: list[tuple[str, float]]) -> bool:
        """判断是否需要多路并行搜索。"""
        research_signals = ["分析", "研究", "格局", "趋势", "深度", "全面", "详细",
                            "research", "analysis", "comprehensive", "deep",
                            "对比", "评测", "推荐", "review", "comparison"]
        if any(kw in query.lower() for kw in research_signals):
            return True

        if not scores or scores[0][1] < 0.15:
            return True

        if len(scores) >= 2 and scores[0][1] > 0:
            gap = (scores[0][1] - scores[1][1]) / scores[0][1]
            if gap < 0.2:
                return True

        return False


# ── 模块级单例 ────────────────────────────────────────
_router: Optional[SemanticRouter] = None


def get_router() -> SemanticRouter:
    global _router
    if _router is None:
        _router = SemanticRouter()
    return _router


def semantic_route(query: str, top_k: int = 3) -> list[tuple[str, float]]:
    """便捷函数：语义路由。"""
    return get_router().route(query, top_k=top_k)


def semantic_route_auto(query: str) -> str:
    """便捷函数：返回最优引擎名。"""
    scores = semantic_route(query, top_k=1)
    return scores[0][0] if scores else "anysearch"


# ── CLI 测试 ──────────────────────────────────────────
if __name__ == "__main__":
    import sys

    test_queries = sys.argv[1:] if len(sys.argv) > 1 else [
        "英伟达最新财报",
        "Python 异步编程最佳实践",
        "2026年AI芯片行业竞争格局",
        "美联储加息对股市影响",
        "笔记本电脑推荐 2026",
        "小米汽车销量",
        "latest AI research papers",
        "transformer attention mechanism paper",
        "北京旅游攻略",
        "基金定投策略",
    ]

    router = get_router()
    print(f"{'查询':<35} {'最优引擎':<12} {'分数':<8} {'Top-3':<30} {'并行?'}")
    print("-" * 100)

    for q in test_queries:
        scores = router.route(q, top_k=3)
        best = scores[0] if scores[0] else ("?", 0)
        top3 = ", ".join(f"{n}:{s:.3f}" for n, s in scores[:3])
        parallel = router.should_parallel(q, scores)
        print(f"{q:<35} {best[0]:<12} {best[1]:<8.4f} {top3:<30} {'✓' if parallel else '✗'}")
