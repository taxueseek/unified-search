#!/usr/bin/env python3
"""
clarify.py — 意图消歧工具

核心能力：
  1. 歧义检测：识别查询中的多义词
  2. 意图分类：判断查询的真实意图
  3. 推荐路由：给出最优搜索策略
  4. 多义展开：列出所有可能含义供用户选择

用法：
  python3 clarify.py "Python 吞苹果 兼容吗"
  python3 clarify.py "苹果股价" --explain
  python3 clarify.py "Java" --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


# ── 歧义词库 ──────────────────────────────────────────────────────────────────

AMBIGUOUS_TERMS = {
    # 多义实体
    "苹果": {
        "meanings": [
            {"text": "Apple 公司（科技/股票）", "domain": "tech", "weight": 0.6},
            {"text": "苹果（水果/食品）", "domain": "general", "weight": 0.3},
            {"text": "苹果（操作系统/macOS）", "domain": "tech", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "tech": ["股价", "股票", "iPhone", "Mac", "iOS", "WWDC", "市值", "AAPL", "库克", "蒂姆"],
            "food": ["吃", "水果", "营养", "减肥", "种植", "产地", "品种"],
        },
    },
    "Python": {
        "meanings": [
            {"text": "Python 编程语言", "domain": "tech", "weight": 0.7},
            {"text": "蟒蛇（动物）", "domain": "nature", "weight": 0.2},
            {"text": "Monty Python（喜剧团体）", "domain": "entertainment", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "tech": ["代码", "编程", "函数", "库", "框架", "pip", "安装", "版本", "async", "Django", "Flask", "报错", "bug"],
            "nature": ["蛇", "爬", "动物", "宠物", "饲养", "鳞片"],
            "entertainment": ["电影", "喜剧", "英国", "动画"],
        },
    },
    "Java": {
        "meanings": [
            {"text": "Java 编程语言", "domain": "tech", "weight": 0.7},
            {"text": "印度尼西亚爪哇岛", "domain": "travel", "weight": 0.15},
            {"text": "Java 咖啡", "domain": "food", "weight": 0.15},
        ],
        "disambiguation_keywords": {
            "tech": ["编程", "代码", "JDK", "Spring", "Maven", "Gradle", "JVM", "报错", "版本"],
            "travel": ["旅游", "岛", "签证", "机票", "酒店"],
            "food": ["咖啡", "豆", "烘焙", "产地"],
        },
    },
    "Rust": {
        "meanings": [
            {"text": "Rust 编程语言", "domain": "tech", "weight": 0.7},
            {"text": "锈蚀（化学/材料）", "domain": "science", "weight": 0.2},
            {"text": "Rust 游戏", "domain": "gaming", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "tech": ["编程", "cargo", "crate", "所有权", "borrow", "编译", "代码", "性能"],
            "science": ["金属", "腐蚀", "防护", "涂层", "氧化"],
            "gaming": ["游戏", "服务器", "联机", "steam"],
        },
    },
    "茅台": {
        "meanings": [
            {"text": "贵州茅台（股票/白酒品牌）", "domain": "finance", "weight": 0.7},
            {"text": "茅台镇（地名/产区）", "domain": "travel", "weight": 0.2},
            {"text": "茅台酒（产品/品鉴）", "domain": "lifestyle", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "finance": ["股价", "股票", "市值", "财报", "营收", "利润", "分红", "600519"],
            "travel": ["镇", "旅游", "产区", "参观", "路线"],
            "lifestyle": ["口感", "品鉴", "收藏", "年份", "真假"],
        },
    },
    "Transformer": {
        "meanings": [
            {"text": "Transformer 模型（AI/深度学习）", "domain": "tech", "weight": 0.6},
            {"text": "变形金刚（玩具/电影）", "domain": "entertainment", "weight": 0.25},
            {"text": "变压器（电气设备）", "domain": "engineering", "weight": 0.15},
        ],
        "disambiguation_keywords": {
            "tech": ["attention", "BERT", "GPT", "神经网络", "NLP", "论文", "模型", "训练", "AI", "大模型"],
            "entertainment": ["电影", "玩具", "擎天柱", "大黄蜂", "动画"],
            "engineering": ["电力", "电压", "电网", "变电站"],
        },
    },
    # ── 扩展歧义词库（v2.1 新增 30+ 词）──
    "小米": {
        "meanings": [
            {"text": "小米科技（手机/IoT品牌）", "domain": "tech", "weight": 0.6},
            {"text": "小米（谷物/粮食）", "domain": "food", "weight": 0.3},
            {"text": "小米SU7（汽车）", "domain": "auto", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "tech": ["手机", "MIUI", "HyperOS", "雷军", "SU7", "市值", "港股", "1810"],
            "food": ["粮食", "作物", "种植", "产量", "小米粥"],
        },
    },
    "华为": {
        "meanings": [
            {"text": "华为技术有限公司", "domain": "tech", "weight": 0.8},
            {"text": "华为（人名/地名）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"tech": ["手机", "鸿蒙", "5G", "芯片", "Mate", "任正非"]},
    },
    "特斯拉": {
        "meanings": [
            {"text": "Tesla（电动汽车品牌）", "domain": "auto", "weight": 0.8},
            {"text": "尼古拉·特斯拉（发明家）", "domain": "science", "weight": 0.2},
        ],
        "disambiguation_keywords": {
            "auto": ["Model", "充电桩", "自动驾驶", "FSD", "马斯克"],
            "science": ["交流电", "发明", "无线电", "特斯拉线圈"],
        },
    },
    "Docker": {
        "meanings": [
            {"text": "Docker 容器化平台", "domain": "tech", "weight": 0.85},
            {"text": "码头工人", "domain": "general", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["容器", "镜像", "compose", "Kubernetes", "部署"]},
    },
    "Go": {
        "meanings": [
            {"text": "Go 编程语言", "domain": "tech", "weight": 0.7},
            {"text": "去/进行（动词）", "domain": "general", "weight": 0.3},
        ],
        "disambiguation_keywords": {"tech": ["Golang", "并发", "goroutine", "channel", "编译", "stdlib"]},
    },
    "Redis": {
        "meanings": [
            {"text": "Redis 缓存数据库", "domain": "tech", "weight": 0.9},
            {"text": "Redis（人名）", "domain": "general", "weight": 0.1},
        ],
        "disambiguation_keywords": {"tech": ["缓存", "内存", "键值", "持久化", "集群"]},
    },
    "Swift": {
        "meanings": [
            {"text": "Swift 编程语言（Apple）", "domain": "tech", "weight": 0.7},
            {"text": "Swift（金融转账系统）", "domain": "finance", "weight": 0.15},
            {"text": "Swift（歌手）", "domain": "entertainment", "weight": 0.15},
        ],
        "disambiguation_keywords": {
            "tech": ["iOS", "macOS", "Xcode", "SwiftUI", "苹果"],
            "finance": ["转账", "银行", "国际结算"],
        },
    },
    "Kotlin": {
        "meanings": [
            {"text": "Kotlin 编程语言", "domain": "tech", "weight": 0.85},
            {"text": "Kotlin（地名/岛名）", "domain": "travel", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["Android", "JVM", "JetBrains", "协程", "空安全"]},
    },
    "抖音": {
        "meanings": [
            {"text": "抖音/TikTok（短视频平台）", "domain": "tech", "weight": 0.8},
            {"text": "抖音（动词/拟声）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"tech": ["短视频", "直播", "字节跳动", "TikTok", "算法"]},
    },
    "微信": {
        "meanings": [
            {"text": "微信（通讯/社交平台）", "domain": "tech", "weight": 0.85},
            {"text": "微信（动词）", "domain": "general", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["小程序", "公众号", "支付", "腾讯", "朋友圈"]},
    },
    "GPT": {
        "meanings": [
            {"text": "GPT 系列大语言模型（OpenAI）", "domain": "tech", "weight": 0.8},
            {"text": "GPT（GUID Partition Table）", "domain": "tech", "weight": 0.2},
        ],
        "disambiguation_keywords": {"tech": ["OpenAI", "ChatGPT", "大模型", "GPT-4", "GPT-5"]},
    },
    "RAG": {
        "meanings": [
            {"text": "RAG（检索增强生成）", "domain": "tech", "weight": 0.7},
            {"text": "RAG（摇滚乐风格）", "domain": "music", "weight": 0.15},
            {"text": "Rag（抹布）", "domain": "general", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["检索", "向量", "embedding", "大模型", "知识库"]},
    },
    "芯片": {
        "meanings": [
            {"text": "半导体芯片（IC）", "domain": "tech", "weight": 0.7},
            {"text": "薯片/食品", "domain": "food", "weight": 0.2},
            {"text": "筹码（赌博）", "domain": "general", "weight": 0.1},
        ],
        "disambiguation_keywords": {
            "tech": ["半导体", "NVIDIA", "台积电", "ASML", "光刻机", "GPU"],
            "food": ["零食", "好吃", "口味", "乐事"],
        },
    },
    "蔚来": {
        "meanings": [
            {"text": "蔚来汽车（NIO）", "domain": "auto", "weight": 0.8},
            {"text": "蔚蓝/蔚然（形容词）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"auto": ["电动车", "换电", "NIO", "李斌", "ET5", "ES6"]},
    },
    "理想": {
        "meanings": [
            {"text": "理想汽车（Li Auto）", "domain": "auto", "weight": 0.6},
            {"text": "理想（概念/哲学）", "domain": "general", "weight": 0.4},
        ],
        "disambiguation_keywords": {"auto": ["增程", "L系列", "MEGA", "李想", "纯电"]},
    },
    "小鹏": {
        "meanings": [
            {"text": "小鹏汽车（XPeng）", "domain": "auto", "weight": 0.8},
            {"text": "小鹏（人名）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"auto": ["P7", "G6", "飞行汽车", "何小鹏", "智驾"]},
    },
    "Anthropic": {
        "meanings": [
            {"text": "Anthropic（AI 公司/Claude）", "domain": "tech", "weight": 0.85},
            {"text": "Anthropic（人类学的）", "domain": "science", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["Claude", "大模型", "AI安全", "Constitutional AI"]},
    },
    "OpenAI": {
        "meanings": [
            {"text": "OpenAI（AI 公司）", "domain": "tech", "weight": 0.9},
            {"text": "OpenAI（开源 AI 组织）", "domain": "tech", "weight": 0.1},
        ],
        "disambiguation_keywords": {"tech": ["GPT", "ChatGPT", "Sam Altman", "DALL-E", "Sora"]},
    },
    "Cursor": {
        "meanings": [
            {"text": "Cursor（AI 代码编辑器）", "domain": "tech", "weight": 0.7},
            {"text": "光标/鼠标指针", "domain": "general", "weight": 0.3},
        ],
        "disambiguation_keywords": {"tech": ["编辑器", "VSCode", "AI编程", "Tab补全", "Composer"]},
    },
    "Gemini": {
        "meanings": [
            {"text": "Google Gemini（AI 模型）", "domain": "tech", "weight": 0.7},
            {"text": "双子座（星座）", "domain": "general", "weight": 0.2},
            {"text": "Gemini（电影工作室）", "domain": "entertainment", "weight": 0.1},
        ],
        "disambiguation_keywords": {"tech": ["Google", "大模型", "Gemini Pro", "多模态"]},
    },
    "Claude": {
        "meanings": [
            {"text": "Claude（Anthropic AI 助手）", "domain": "tech", "weight": 0.8},
            {"text": "Claude（法语人名）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"tech": ["Anthropic", "大模型", "Claude 3", "AI助手"]},
    },
    "Notion": {
        "meanings": [
            {"text": "Notion（协作/笔记工具）", "domain": "tech", "weight": 0.85},
            {"text": "概念/观念", "domain": "general", "weight": 0.15},
        ],
        "disambiguation_keywords": {"tech": ["笔记", "协作", "数据库", "模板", "AI"]},
    },
    "飞书": {
        "meanings": [
            {"text": "飞书（字节跳动协作工具）", "domain": "tech", "weight": 0.8},
            {"text": "飞书（飞来的书信）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"tech": ["协作", "OKR", "视频会议", "文档", "多维表格"]},
    },
    "比特币": {
        "meanings": [
            {"text": "Bitcoin（加密货币）", "domain": "finance", "weight": 0.85},
            {"text": "比特币（泛指加密货币）", "domain": "finance", "weight": 0.15},
        ],
        "disambiguation_keywords": {"finance": ["BTC", "挖矿", "区块链", "交易所", "减半", "ETF"]},
    },
    "新能源": {
        "meanings": [
            {"text": "新能源汽车", "domain": "auto", "weight": 0.6},
            {"text": "新能源（太阳能/风能等）", "domain": "energy", "weight": 0.4},
        ],
        "disambiguation_keywords": {
            "auto": ["电动车", "比亚迪", "特斯拉", "充电桩", "续航"],
            "energy": ["光伏", "风电", "储能", "碳中和"],
        },
    },
    "A股": {
        "meanings": [
            {"text": "A股市场（中国股票）", "domain": "finance", "weight": 0.9},
            {"text": "A股（游戏术语）", "domain": "gaming", "weight": 0.1},
        ],
        "disambiguation_keywords": {"finance": ["上证", "深证", "创业板", "科创板", "北交所"]},
    },
    "ETF": {
        "meanings": [
            {"text": "ETF（交易所交易基金）", "domain": "finance", "weight": 0.9},
            {"text": "ETF（其他缩写）", "domain": "general", "weight": 0.1},
        ],
        "disambiguation_keywords": {"finance": ["指数基金", "净值", "场内", "LOF", "QDII"]},
    },
    "量化": {
        "meanings": [
            {"text": "量化投资/量化交易", "domain": "finance", "weight": 0.7},
            {"text": "量化（物理学/化学）", "domain": "science", "weight": 0.3},
        ],
        "disambiguation_keywords": {
            "finance": ["对冲", "因子", "Alpha", "回测", "高频", "策略"],
            "science": ["测量", "分析", "浓度", "实验"],
        },
    },
    "期权": {
        "meanings": [
            {"text": "期权（金融衍生品）", "domain": "finance", "weight": 0.85},
            {"text": "期权（选择权/选项）", "domain": "general", "weight": 0.15},
        ],
        "disambiguation_keywords": {"finance": ["认购", "认沽", "行权", "波动率", "Greeks"]},
    },
    "基金": {
        "meanings": [
            {"text": "公募/私募基金", "domain": "finance", "weight": 0.8},
            {"text": "基金（基础/根本）", "domain": "general", "weight": 0.2},
        ],
        "disambiguation_keywords": {"finance": ["净值", "回撤", "夏普", "基金经理", "ETF"]},
    },
}

# ── 品牌碰撞检测 ──────────────────────────────────────────────────────────────

BRAND_COLLISIONS = {
    "Amazon": {"domains": ["amazon.com", "aws.amazon.com"], "alt": ["亚马逊（河流/地区）"]},
    "Apple": {"domains": ["apple.com"], "alt": ["苹果（水果）"]},
    "小米": {"domains": ["xiaomi.com", "mi.com"], "alt": ["小米（谷物）"]},
    "华为": {"domains": ["huawei.com"], "alt": ["华为（人名）"]},
    "特斯拉": {"domains": ["tesla.com"], "alt": ["尼古拉·特斯拉（发明家）"]},
    "字节": {"domains": ["bytedance.com"], "alt": ["字节（计算机单位）"]},
    "快手": {"domains": ["kuaishou.com"], "alt": ["快手（手快）"]},
    "贝壳": {"domains": ["ke.com", "beike.com"], "alt": ["贝壳（海洋生物）"]},
    "飞书": {"domains": ["feishu.cn"], "alt": ["飞来的书信"]},
    "钉钉": {"domains": ["dingtalk.com"], "alt": ["钉钉子的声音"]},
    "美团": {"domains": ["meituan.com"], "alt": ["美好的团体"]},
    "京东": {"domains": ["jd.com"], "alt": ["京东（地名）"]},
    "淘宝": {"domains": ["taobao.com"], "alt": ["淘到的宝贝"]},
    "拼多多": {"domains": ["pinduoduo.com"], "alt": ["拼凑"]},
    "蔚来": {"domains": ["nio.com"], "alt": ["蔚蓝/蔚然"]},
    "理想": {"domains": ["lixiang.com"], "alt": ["理想（概念）"]},
    "小鹏": {"domains": ["xiaopeng.com"], "alt": ["小鹏（人名）"]},
    "豆瓣": {"domains": ["douban.com"], "alt": ["豆瓣酱"]},
    "雪球": {"domains": ["xueqiu.com"], "alt": ["雪球（游戏）"]},
    "知乎": {"domains": ["zhihu.com"], "alt": ["知乎（文言文）"]},
    "闲鱼": {"domains": ["goofish.com"], "alt": ["闲置的鱼"]},
    "盒马": {"domains": ["hema.com"], "alt": ["盒马（动物）"]},
}


def detect_brand_collision(query: str) -> dict | None:
    """检测查询中的品牌碰撞风险。"""
    for brand, info in BRAND_COLLISIONS.items():
        if brand in query or brand.lower() in query.lower():
            return {
                "brand": brand,
                "collision_domains": info["domains"],
                "alt_meanings": info["alt"],
                "warning": f"「{brand}」可能指向多个品牌/产品",
            }
    return None


# 意图模式
INTENT_PATTERNS = {
    "search_fact": {
        "patterns": [
            r"(?:多少|几|价格|值|身高|重量|面积|人口|GDP|增长率)",
            r"(?:what|how many|how much|when|where|who)",
            r"(?:是谁|是什么|在哪里|什么时候|多少钱)",
        ],
        "label": "事实查询",
        "weight": 1.0,
    },
    "search_opinion": {
        "patterns": [
            r"(?:怎么看|如何评价|观点|想法|建议|推荐|意见)",
            r"(?:how|why|what do you think|opinion|view)",
            r"(?:值得|好不好|怎么样|该不该|有没有必要)",
        ],
        "label": "观点/评价",
        "weight": 1.0,
    },
    "search_tech": {
        "patterns": [
            r"(?:怎么用|如何配置|安装|部署|报错|bug|error|教程|入门)",
            r"(?:how to|tutorial|guide|setup|install|configure|troubleshoot)",
            r"(?:API|SDK|框架|库|组件|插件|扩展)",
        ],
        "label": "技术操作",
        "weight": 1.0,
    },
    "search_compare": {
        "patterns": [
            r"(?:vs| versus |对比|比较|区别|差异|选择|哪个好|优缺点)",
            r"(?:A vs B|A or B|A compared to B)",
        ],
        "label": "对比分析",
        "weight": 1.0,
    },
    "search_news": {
        "patterns": [
            r"(?:最新|今天|最近|新闻|动态|进展|发生|事件)",
            r"(?:latest|recent|news|update|breaking)",
        ],
        "label": "新闻/动态",
        "weight": 1.0,
    },
    "search_deep": {
        "patterns": [
            r"(?:深度|全面|详细|系统|完整|综述|研究|分析|探讨)",
            r"(?:deep|comprehensive|review|survey|research|analysis)",
        ],
        "label": "深度研究",
        "weight": 1.0,
    },
}


# ── 核心分析 ──────────────────────────────────────────────────────────────────

def analyze_query(query: str) -> dict[str, Any]:
    """分析查询的意图和歧义。"""
    analysis = {
        "query": query,
        "language": _detect_language(query),
        "ambiguities": [],
        "intents": [],
        "entities": [],
        "recommended_strategy": "general",
        "confidence": 0.8,
    }

    # 歧义检测
    for term, info in AMBIGUOUS_TERMS.items():
        if term in query or (term.isascii() and re.search(r'\b' + re.escape(term) + r'\b', query, re.I)):
            # 检查上下文关键词
            matched_meanings = []
            for meaning in info["meanings"]:
                domain = meaning["domain"]
                keywords = info.get("disambiguation_keywords", {}).get(domain, [])
                match_count = sum(1 for kw in keywords if kw in query)
                # 强信号加分：产品型号、金融术语、学术术语
                strong_bonus = 0
                if domain == "auto" and re.search(r'(SU7|Model|ET5|ES6|P7|G6|L系列|MEGA|AION)', query, re.I):
                    strong_bonus = 0.3
                elif domain == "finance" and re.search(r'(股价|财报|市值|基金|ETF|净值|涨跌|K线|分红)', query):
                    strong_bonus = 0.2
                elif domain == "tech" and re.search(r'(论文|paper|arxiv|API|SDK|框架|库|编程|代码|编译|部署)', query, re.I):
                    strong_bonus = 0.15
                elif domain == "energy" and re.search(r'(光伏|风电|储能|碳中和|太阳能|风能)', query):
                    strong_bonus = 0.2
                if match_count > 0 or strong_bonus > 0:
                    matched_meanings.append({
                        "meaning": meaning["text"],
                        "domain": domain,
                        "context_match": match_count,
                        "base_weight": meaning["weight"],
                        "weight": meaning["weight"] + match_count * 0.1 + strong_bonus,
                    })

            if matched_meanings:
                matched_meanings.sort(key=lambda x: x["weight"], reverse=True)
                top = matched_meanings[0]
                # 置信度计算：基础分 + 关键词匹配加分 + 强信号加分
                base_conf = 0.5
                keyword_bonus = min(top["context_match"] * 0.12, 0.3)
                # strong_bonus 从 weight 中提取（weight = base_weight + keyword*0.1 + strong_bonus）
                strong_bonus = max(0, top["weight"] - top.get("base_weight", 0.5) - top["context_match"] * 0.1)
                conf = min(base_conf + keyword_bonus + strong_bonus * 0.5, 0.95)
                analysis["ambiguities"].append({
                    "term": term,
                    "possible_meanings": [m["meaning"] for m in matched_meanings],
                    "top_choice": top["meaning"],
                    "confidence": round(conf, 2),
                })
                analysis["confidence"] = min(analysis["confidence"], conf)
            else:
                # 无法消歧
                analysis["ambiguities"].append({
                    "term": term,
                    "possible_meanings": [m["text"] for m in info["meanings"]],
                    "top_choice": info["meanings"][0]["text"],
                    "confidence": info["meanings"][0]["weight"],
                })
                analysis["confidence"] = min(analysis["confidence"], 0.6)

    # 意图分类
    for intent_key, intent_info in INTENT_PATTERNS.items():
        for pattern in intent_info["patterns"]:
            if re.search(pattern, query, re.I):
                analysis["intents"].append({
                    "type": intent_key,
                    "label": intent_info["label"],
                })
                break

    # 实体提取（简单规则）
    # 英文专有名词
    eng_entities = re.findall(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b", query)
    for e in eng_entities:
        if len(e) > 2:
            analysis["entities"].append({"text": e, "type": "proper_noun"})

    # 中文专业术语
    cn_tech = re.findall(r"([\u4e00-\u9fff]{2,4}(?:引擎|框架|工具|平台|模型|算法|协议|接口|数据库))", query)
    for e in cn_tech:
        analysis["entities"].append({"text": e, "type": "tech_term"})

    # 编号/代码
    codes = re.findall(r"\b(\d{6})\b", query)
    for c in codes:
        analysis["entities"].append({"text": c, "type": "stock_code"})

    cve_codes = re.findall(r"(CVE-\d{4}-\d+)", query, re.I)
    for c in cve_codes:
        analysis["entities"].append({"text": c, "type": "cve"})

    # 品牌碰撞检测
    collision = detect_brand_collision(query)
    if collision:
        analysis["brand_collision"] = collision
        analysis["confidence"] *= 0.85

    # 推荐策略
    if analysis["ambiguities"] and analysis["ambiguities"][0]["confidence"] < 0.7:
        analysis["recommended_strategy"] = "clarify_first"
        analysis["confidence"] = 0.5
    elif any(i["type"] == "search_deep" for i in analysis["intents"]):
        analysis["recommended_strategy"] = "deep_research"
    elif any(i["type"] == "search_compare" for i in analysis["intents"]):
        analysis["recommended_strategy"] = "split_search"
    elif any(i["type"] == "search_fact" for i in analysis["intents"]):
        analysis["recommended_strategy"] = "direct_search"
    elif any(i["type"] == "search_news" for i in analysis["intents"]):
        analysis["recommended_strategy"] = "news_priority"
    else:
        analysis["recommended_strategy"] = "general"

    return analysis


def _detect_language(text: str) -> str:
    """检测主要语言。"""
    cn_count = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    en_count = sum(1 for c in text if c.isascii() and c.isalpha())
    ja_count = sum(1 for c in text if "\u3040" <= c <= "\u30ff")

    total = cn_count + en_count + ja_count
    if total == 0:
        return "unknown"

    if cn_count / total > 0.5:
        return "zh" if en_count / total < 0.3 else "zh-en"
    elif ja_count / total > 0.3:
        return "ja" if cn_count / total < 0.3 else "zh-ja"
    elif en_count / total > 0.5:
        return "en"
    else:
        return "mixed"


# ── 路由推荐 ──────────────────────────────────────────────────────────────────

def recommend_routing(analysis: dict[str, Any]) -> dict[str, Any]:
    """基于意图分析推荐搜索路由。"""
    strategy = analysis["recommended_strategy"]

    routing = {
        "strategy": strategy,
        "engines": [],
        "mode": "auto",
        "explanation": "",
    }

    if strategy == "clarify_first":
        routing["engines"] = ["clarify_first"]
        routing["explanation"] = f"检测到歧义词「{analysis['ambiguities'][0]['term']}」，建议先消歧再搜索"
        routing["mode"] = "auto"

    elif strategy == "deep_research":
        routing["engines"] = ["research"]
        routing["explanation"] = "检测到深度研究意图，建议使用深度研究工具"
        routing["mode"] = "deep"

    elif strategy == "split_search":
        routing["engines"] = ["search", "search"]  # 两次搜索
        routing["explanation"] = "检测到对比意图，建议分别搜索各对象后对比"
        routing["mode"] = "auto"

    elif strategy == "news_priority":
        routing["engines"] = ["byted", "uapi", "duckduckgo"]
        routing["explanation"] = "检测到新闻/动态意图，优先使用新闻引擎"
        routing["mode"] = "fast"

    elif strategy == "direct_search":
        routing["engines"] = ["auto"]
        routing["explanation"] = "事实查询，直接搜索即可"
        routing["mode"] = "fast"

    else:
        # 根据实体和意图推荐引擎
        entities = [e["type"] for e in analysis["entities"]]
        intents = [i["type"] for i in analysis["intents"]]

        if "stock_code" in entities or "finance" in [a.get("domain") for a in analysis.get("ambiguities", [])]:
            routing["engines"] = ["eastmoney", "anysearch"]
            routing["explanation"] = "检测到金融实体，优先使用金融引擎"
        elif "cve" in entities:
            routing["engines"] = ["anysearch"]
            routing["explanation"] = "检测到 CVE 编号，使用安全垂直域"
        elif "search_tech" in intents:
            routing["engines"] = ["github", "duckduckgo", "uapi"]
            routing["explanation"] = "检测到技术意图，优先技术源"
        elif "search_opinion" in intents:
            routing["engines"] = ["zhihu", "byted"]
            routing["explanation"] = "检测到观点/评价意图，优先知乎"
        else:
            routing["engines"] = ["auto"]
            routing["explanation"] = "无特殊意图，使用默认路由"

    return routing


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="意图消歧工具")
    parser.add_argument("query", help="搜索查询")
    parser.add_argument("--explain", action="store_true", help="详细解释")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    analysis = analyze_query(args.query)
    routing = recommend_routing(analysis)
    analysis["routing"] = routing

    if args.json:
        print(json.dumps(analysis, ensure_ascii=False, indent=2))
    else:
        print(f"\n查询分析：{analysis['query']}")
        print(f"语言：{analysis['language']} | 置信度：{analysis['confidence']:.2f}")
        print(f"推荐策略：{routing['strategy']}")
        print(f"说明：{routing['explanation']}")
        print()

        if analysis["ambiguities"]:
            print("── 歧义检测 ──")
            for a in analysis["ambiguities"]:
                print(f"  「{a['term']}」→ {a['confidence']:.0%} 置信度")
                for m in a["possible_meanings"]:
                    marker = "→" if m == a["top_choice"] else " "
                    print(f"    {marker} {m}")
            print()

        if analysis["intents"]:
            print("── 意图分类 ──")
            for i in analysis["intents"]:
                print(f"  • {i['label']}")
            print()

        if analysis["entities"]:
            print("── 实体识别 ──")
            for e in analysis["entities"]:
                print(f"  • {e['text']} ({e['type']})")
            print()

        if routing["engines"] and routing["engines"] != ["auto"]:
            print(f"── 推荐引擎 ──")
            print(f"  {', '.join(routing['engines'])}")
            print()


if __name__ == "__main__":
    main()
