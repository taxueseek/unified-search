#!/usr/bin/env python3
"""
search_types.py — 统一类型系统

定义 SearchResult 数据类和 normalize_result 统一转换，
将所有引擎的原始输出统一为结构化格式。

字段规范：
  - title: 结果标题（必填，截断到 200 字符）
  - url: 结果链接（可选）
  - snippet: 摘要/片段（截断到 300 字符）
  - score: 相关性评分（默认 0.5）
  - source: 引擎来源
  - metadata: 元数据字典
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class SearchResult:
    """统一的搜索结果数据类。"""
    title: str
    url: str = ""
    snippet: str = ""
    score: float = 0.5
    source: str = ""
    metadata: dict = field(default_factory=dict)
    social_meta: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为字典，省略空值/默认值字段。"""
        d: dict[str, Any] = {"title": self.title}
        if self.url:
            d["url"] = self.url
        if self.snippet:
            d["snippet"] = self.snippet[:300]
        if self.score != 0.5:
            d["score"] = round(self.score, 3)
        if self.source:
            d["source"] = self.source
        if self.metadata:
            d["metadata"] = self.metadata
        if self.social_meta:
            d["social_meta"] = self.social_meta
        return d


def normalize_result(item: dict, engine: str, default_score: float = 0.5) -> SearchResult:
    """将任意引擎输出统一为 SearchResult。

    处理字段名混乱（score/relevance_score/snippet/content/summary 混用）。
    """
    snippet = (
        item.get("snippet")
        or item.get("content")
        or item.get("summary")
        or item.get("description", "")
    )
    raw_score = item.get("score", item.get("relevance_score", default_score))
    try:
        score = float(raw_score) if isinstance(raw_score, (int, float, str)) else default_score
    except (ValueError, TypeError):
        score = default_score

    return SearchResult(
        title=item.get("title", "")[:200],
        url=item.get("url", ""),
        snippet=snippet[:300] if snippet else "",
        score=score,
        source=item.get("source", engine),
        metadata=item.get("metadata", {}),
        social_meta=item.get("social_meta"),
    )
