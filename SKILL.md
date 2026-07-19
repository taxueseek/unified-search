---
name: argo
description: Argo 阿尔戈 — 6工具MCP服务：argo_search（47引擎搜索）+ argo_research（深度研究）+ argo_evidence（可信度评估）+ argo_clarify（意图消歧）+ argo_crawl（站点爬取）+ argo_extract（结构化提取）。TF-IDF路由 + 成本感知 + RRF融合 + Bocha reranker。
version: 2.5.0
triggers:
  - 搜索
  - 查一下
  - 搜一下
  - search for
  - look up
engines:
  - local_search
  - anysearch
  - wigolo
  - tavily
  - zhihu
  - eastmoney
  - byted
  - arxiv
  - searxng
  - duckduckgo
  - uapi
  - semantic_scholar
  - felo
  - bocha
  - openalex
  - crossref
  - github
  - wikipedia
  - metaso
  - wolframalpha
  - brave
---

## Argo v2.5.0

统一搜索入口 v2，替代零散的搜索命令。核心设计：

- **TF-IDF 语义路由**：二元组 + boost_keywords + boost_combos，< 5ms 延迟
- **成本感知评分**：free=1.0 / low=0.7 / paid=0.3 三档 cost_factor
- **预算模式**：fast / auto / deep / budget 四档配额追踪 + 自动降级
- **渐进式多源**：engines_combo 先快后全 + 并行模式
- **AnySearch 垂直域**：19 个垂直域结构化搜索，匿名兜底
- **双层缓存**：L1 LRU + L2 SQLite + gzip，分级 TTL
- **RRF 融合**：多引擎 Reciprocal Rank Fusion 去重合并
- **Bocha Reranker**：语义精排后处理
- **自适应学习**：success × latency × cost 三维评分，SQLite 持久化

### 用法

```bash
# 自动路由（推荐）
python3 scripts/search.py "查询词"

# JSON 输出（供 Agent 消费）
python3 scripts/search.py "查询词" --json

# 解释路由决策（含 TF-IDF 分数）
python3 scripts/search.py "查询词" --explain

# 强制引擎
python3 scripts/search.py "查询词" --engine anysearch
python3 scripts/search.py "查询词" --engine byted
python3 scripts/search.py "查询词" --engine arxiv
python3 scripts/search.py "查询词" --engine eastmoney
python3 scripts/search.py "查询词" --engine zhihu
python3 scripts/search.py "查询词" --engine bocha

# 本地零成本优先（local_search 聚合）
python3 scripts/search.py "查询词" --local-first
python3 scripts/search.py "查询词" --local-first --mode fast

# 预算模式
python3 scripts/search.py "查询词" --mode fast     # 免费优先，自动前置 local_search
python3 scripts/search.py "查询词" --mode auto     # 成本感知（默认）
python3 scripts/search.py "查询词" --mode deep     # 质量优先
python3 scripts/search.py "查询词" --mode budget   # 配额控制

# 跳过缓存 / 搜索深度 / 列出引擎 / 配额状态
python3 scripts/search.py "查询词" --no-cache
python3 scripts/search.py "查询词" --depth fast|balanced|deep
python3 scripts/search.py --list-engines
python3 scripts/quota.py stats

# AnySearch 垂直域搜索
python3 scripts/search.py "AAPL" --domain finance --sub_domain finance.us_stock

# TF-IDF 路由测试
python3 scripts/tfidf_router.py "查询词"

# ── 深度研究（问题分解→多源采集→综合报告）──
python3 scripts/research.py "CRISPR脱靶效应AI预测方法综述"
python3 scripts/research.py "CVE-2024-6387 生产环境影响" --depth deep --json
python3 scripts/research.py "React vs Vue 2025 生产环境对比" --sub-queries 5

# ── 来源可信度评估 ──
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json

# ── 意图消歧 ──
python3 scripts/clarify.py "Python 吞苹果 兼容吗" --explain
python3 scripts/clarify.py "苹果股价" --json
```

### 引擎全景（22 个引擎 + Reranker）

| 引擎 | cost_tier | 特点 | 延迟 |
|------|-----------|------|------|
| anysearch | free | 垂直领域通用 | ~2.7s |
| zhihu | free | 知乎观点 | ~700ms |
| eastmoney | free | 金融数据 | ~400ms |
| arxiv | free | 学术论文 | ~1s |
| wigolo | free | 本地语义搜索 | ~800ms |
| duckduckgo | free | 快速事实 | ~500ms |
| uapi | free | 中文网页 | ~800ms |
| semantic_scholar | free | 学术+引用 | ~1s |
| openalex | free | 2.5亿+论文 | ~2s |
| crossref | free | DOI/引用元数据 | ~2s |
| github | free | 代码搜索 | ~1s |
| wikipedia | free | 百科事实 | ~500ms |
| searxng | free | 聚合搜索 | ~1s |
| wolframalpha | free | 计算知识 | ~2s |
| bocha | low | 中文网页(AI友好) | ~1s |
| metaso | low | 中文AI搜索 | ~2s |
| byted | low | 字节搜索 | ~1s |
| brave | low | 隐私搜索 | ~1s |
| tavily | paid | 国际搜索 | ~2s |
| felo | paid | AI综合答案 | ~3s |
| **Bocha Reranker** | low | 语义精排（后处理） | ~500ms |

### 三大增强工具（v2.0 新增）

| 工具 | 功能 | 适用场景 | Token 开销 |
|------|------|---------|-----------|
| `research` | 问题分解→多源并行采集→综合报告+引用+知识缺口 | 学术综述、事实核查、竞品分析、技术选型 | ~700/次 |
| `evidence` | 权威性+时效性+交叉验证的综合可信度评分 | 高后果决策、学术引用、新闻真伪 | ~300/次 |
| `clarify` | 歧义检测+意图分类+推荐路由策略 | 歧义查询、意图不明确、多语言混合 | ~200/次 |

#### research — 深度研究

```bash
# 自动分解问题，多源采集
python3 scripts/research.py "你的复杂查询"

# 控制子查询数量和搜索深度
python3 scripts/research.py "查询" --sub-queries 5 --depth deep

# JSON 输出供 Agent 消费
python3 scripts/research.py "查询" --json
```

输出包含：`key_findings`（按子查询分组的关键发现）、`citations`（引用列表）、`gaps`（知识缺口）、`source_distribution`（来源统计）。

#### evidence — 可信度评估

```bash
# 对搜索结果进行可信度评分
echo '{"results": [...]}' | python3 scripts/evidence.py "查询词" --stdin --json
```

输出包含：每个结果的 `credibility.final`（综合分）、`authority`（权威性分解）、`freshness`（时效性分解）、`cross_validation`（交叉验证等级）。

#### clarify — 意图消歧

```bash
# 分析查询歧义和意图
python3 scripts/clarify.py "有歧义的查询" --explain --json
```

输出包含：`ambiguities`（歧义词+可能含义+置信度）、`intents`（意图分类）、`recommended_strategy`（推荐策略：clarify_first/deep_research/split_search/direct_search）。

### MCP 服务

六个工具同时暴露为 MCP server（JSON-RPC over stdio），可被 Grok/Claude 等客户端直接调用：

```bash
# 启动 MCP 服务
python3 scripts/mcp_server.py

# 本地测试
python3 scripts/mcp_server.py --test
```

MCP 工具名：`argo_search`、`argo_research`、`argo_evidence`、`argo_clarify`、`argo_crawl`、`argo_extract`。

### 成本感知路由公式

```
score = quality × cost_factor

cost_factor:
  free  = 1.0   (anysearch/zhihu/eastmoney/arxiv/wigolo/duckduckgo/uapi/...)
  low   = 0.7   (bocha/metaso/byted/brave)
  paid  = 0.3   (tavily/felo)
```

### 预算模式

| 模式 | 说明 | 触发条件 |
|------|------|---------|
| fast | 免费引擎优先，禁用付费 | 简单查询 |
| auto | 成本感知评分（默认） | 普通查询 |
| deep | 质量优先，忽略成本 | 深度研究 |
| budget | 配额控制，用完降级 | 配额紧张 |

### Local Search 子技能与 SearXNG 替代策略

local-search 是 argo 内置的「零成本聚合后端」，不依赖独立的 SearXNG 服务：

- **25 个本地引擎，20 个默认启用**：覆盖 web_general、chinese、academic、news、code、reference、vertical 七大类，
  统一通过 HTML/RSS/JSON/XML 解析公开页面。
- **引擎注册表**（`sub-skills/local-search/engine_registry.py`）：唯一真源，加载
  `config.yaml` + `parse_maps.yaml`；新增引擎只需改 YAML。
- **健康探针**（`sub-skills/local-search/health_check.py`）：canary 查询 + 反爬/拦截检测，
  状态缓存 5 分钟；连续 2 次失败或单次 >8s 标记 unavailable，成功 1 次恢复。
  fast/budget 模式下只检查实际要用的引擎，避免全量探针拖慢响应。
- **智能路由**（`sub-skills/local-search/smart_router.py`）：根据查询特征自动选择最优本地引擎组合。
- **统一 schema**：输出与 argo 完全一致，直接参与 RRF 融合与 Bocha reranker。

**SearXNG 替代说明**：当 SearXNG 未启用或不可用时，argo 在 `fast`/`budget` 模式
下会自动将 `local_search` 加入 `engines_combo` 首位，实现同等零成本聚合效果，无需运行
SearXNG 实例。强制使用本地聚合：

```bash
python3 scripts/search.py "查询词" --local-first
```

### 文件结构

```
argo-v2/
├── SKILL.md              # 本文件 — 技能注册文档
├── config.yaml           # 引擎配置 & 路由规则
├── backends/
│   ├── domain_profiles.json   # TF-IDF 领域文档
│   └── quota_profiles.json    # 配额配置
├── scripts/
│   ├── config.py         # 配置加载器
│   ├── search.py         # CLI 入口 & 执行编排
│   ├── route.py          # 三层路由决策
│   ├── engines.py        # 引擎适配层
│   ├── cache.py          # 双层缓存
│   ├── adaptive.py       # 自适应学习
│   ├── tfidf_router.py   # TF-IDF 语义路由
│   ├── quota.py          # 配额管理
│   ├── search_types.py   # 统一类型系统
│   ├── research.py       # [新] 深度研究工具
│   ├── evidence.py       # [新] 可信度评估工具
│   ├── clarify.py        # [新] 意图消歧工具
│   ├── crawl.py          # [新] 站点爬取工具
│   ├── extract.py        # [新] 结构化提取工具
│   ├── fetch.py          # [新] 页面抓取工具
│   └── mcp_server.py     # [新] MCP 服务层（6 工具）
├── sub-skills/
│   └── local-search/     # 本地引擎子技能
└── tests/
```

### 输出 JSON Schema

```json
{
  "query": "string",
  "engine": "string",
  "engines": ["string"],
  "engines_combo": ["string"],
  "cached": false,
  "cache_level": "L1 | L2",
  "domain": "string | null",
  "elapsed_ms": 0,
  "tfidf_scores": [{"engine": "string", "score": 0.0}],
  "results": [
    {
      "title": "string",
      "url": "string",
      "snippet": "string",
      "score": 0.0,
      "source": "string"
    }
  ],
  "count": 0,
  "engines_used": ["string"],
  "errors": ["string"]
}
```
