#!/usr/bin/env python3
"""
智能路由器 — 从 250 个引擎中选择最佳组合
"""

import re
from typing import List, Dict, Any

# 引擎分层配置
ENGINE_LAYERS = {
    # T1 核心引擎（<1s，覆盖 80% 场景）
    "t1_core": {
        "bing": {"weight": 0.4, "latency": 600, "coverage": "general"},
        "duckduckgo": {"weight": 0.3, "latency": 800, "coverage": "general"},
        "wikipedia": {"weight": 0.3, "latency": 500, "coverage": "definition"},
    },
    # T2 扩展引擎（<3s，覆盖 20% 场景）
    "t2_extended": {
        "baidu": {"weight": 0.5, "latency": 1000, "coverage": "chinese"},
        "arxiv": {"weight": 0.6, "latency": 2000, "coverage": "academic"},
        "bing_news": {"weight": 0.5, "latency": 1500, "coverage": "news"},
        "github": {"weight": 0.4, "latency": 1200, "coverage": "code"},
        "stackoverflow": {"weight": 0.4, "latency": 1400, "coverage": "code"},
        "mdn": {"weight": 0.5, "latency": 1000, "coverage": "code"},
        "google_scholar": {"weight": 0.5, "latency": 2500, "coverage": "academic"},
        "brave": {"weight": 0.3, "latency": 900, "coverage": "general"},
        "mojeek": {"weight": 0.2, "latency": 1200, "coverage": "general"},
    },
    # T3 索引引擎（<5s，深度研究）
    "t3_index": {
        "semantic_scholar": {"weight": 0.5, "latency": 3000, "coverage": "academic"},
        "pubmed": {"weight": 0.4, "latency": 2500, "coverage": "medical"},
        "crossref": {"weight": 0.4, "latency": 2800, "coverage": "academic"},
    }
}

# 查询意图识别规则
INTENT_RULES = [
    # 定义类查询
    (r"什么是|是什么|定义|含义|意思", "definition", ["wikipedia", "duckduckgo"]),
    # 教程类查询
    (r"怎么|如何|教程|入门|学习", "tutorial", ["stackoverflow", "mdn", "github"]),
    # 学术类查询
    (r"论文|研究|学术|arxiv|paper|research", "academic", ["arxiv", "semantic_scholar"]),
    # 新闻类查询
    (r"新闻|最新|今天|latest|news|today", "news", ["bing_news", "google_news"]),
    # 代码类查询
    (r"代码|api|函数|error|bug|code", "code", ["github", "stackoverflow", "mdn"]),
    # 中文类查询
    (r"[\u4e00-\u9fff]", "chinese", ["baidu", "bing"]),
    # 通用查询
    (r".*", "general", ["bing", "duckduckgo", "wikipedia"]),
]

def detect_intent(query: str) -> tuple[str, List[str]]:
    """检测查询意图"""
    for pattern, intent, engines in INTENT_RULES:
        if re.search(pattern, query, re.IGNORECASE):
            return intent, engines
    return "general", ["bing", "duckduckgo", "wikipedia"]

def select_engines(query: str, max_engines: int = 3) -> List[str]:
    """选择最佳引擎组合"""
    intent, default_engines = detect_intent(query)
    
    # 根据意图选择引擎
    engines = []
    for engine in default_engines:
        if len(engines) < max_engines:
            engines.append(engine)
    
    return engines

def get_engine_config(query: str) -> Dict[str, Any]:
    """获取引擎配置"""
    intent, engines = detect_intent(query)
    
    # 根据意图选择层级
    if intent in ["definition", "general"]:
        layer = "t1_core"
        timeout = 1000
    elif intent in ["tutorial", "code", "chinese"]:
        layer = "t2_extended"
        timeout = 3000
    elif intent in ["academic", "news"]:
        layer = "t2_extended"
        timeout = 5000
    else:
        layer = "t1_core"
        timeout = 1000
    
    return {
        "intent": intent,
        "layer": layer,
        "engines": engines,
        "timeout": timeout,
        "max_parallel": len(engines)
    }

if __name__ == "__main__":
    import sys
    import json
    
    if len(sys.argv) < 2:
        print("用法: python3 smart_router.py <查询>")
        sys.exit(1)
    
    query = " ".join(sys.argv[1:])
    config = get_engine_config(query)
    print(json.dumps(config, ensure_ascii=False, indent=2))
