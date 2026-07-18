---
name: local-search
description: unified-search 的 T2 子技能 — 28 个直接 HTTP 搜索引擎，零 API Key、零 token 消耗、零外部依赖
version: 2.0.0
triggers:
  - 本地搜索
  - 通用搜索
---

## Local Search v2.0.0 — T2 层本地搜索子技能

unified-search 双层架构中的 **T2 本地引擎层**，28 个直接 HTTP 引擎，零外部依赖（无 SearXNG、无 wigolo、无 Node.js）。

### 与 unified-search 的调用关系

```
unified-search（主技能 — 路由 + 融合 + 缓存 + 精排）
  ├── T1 直连 API（12 个：eastmoney, zhihu, tavily, github, arxiv ...）
  └── T2 local-search（本子技能 — 28 个零成本引擎）  ← 你在此
```

### Python 接口契约

| 函数 | 签名 | 用途 |
|------|------|------|
| `search(query, max_engines)` | `str, int → dict` | 执行搜索，返回标准化结果 |
| `get_available_engines(category)` | `str? → list[dict]` | 返回引擎列表及元数据 |
| `check_engine_health(engine_name, timeout)` | `str, int → dict` | 快速探测单个引擎是否健康 |

### 28 个引擎

| 类别 | 引擎 | 延迟 | 状态 |
|------|------|------|------|
| **通用英文**（6） | bing | 610ms | ok |
| | duckduckgo | 1013ms | ok |
| | google | 4476ms | slow |
| | mojeek | 2121ms | ok |
| | yandex | 3071ms | ok |
| | startpage | 3515ms | ok |
| **通用中文**（2） | sogou | 910ms | ok（推荐） |
| | baidu | 10680ms | degraded（CAPTCHA） |
| **学术**（5） | arxiv | 3403ms | ok |
| | pubmed | 1516ms | ok |
| | crossref | 2094ms | ok |
| | openalex | — | 新增 v2.0 |
| | dblp | — | 新增 v2.0 |
| **代码**（7） | github | 1414ms | ok |
| | stackoverflow | 1981ms | ok |
| | gitlab | 1947ms | ok |
| | npm | 2389ms | ok |
| | docker_hub | — | 新增 v2.0 |
| | pypi | — | 新增 v2.0 |
| | arch_wiki | — | 新增 v2.0 |
| **新闻**（3） | bing_news | 782ms | ok |
| | google_news | 4006ms | ok |
| | duckduckgo_news | 3392ms | ok |
| **百科**（3） | wikipedia | 747ms | ok |
| | wiktionary | 1070ms | ok |
| | wikiquote | 2231ms | ok |
| **垂直**（2） | imdb | 2637ms | ok |
| | goodreads | 489ms | ok |

### 文件结构

```
~/.grok/skills/local-search/
├── SKILL.md            # 本文件
├── search_v3.py         # 28 引擎 + 健康检查 + 元数据
└── config.json          # 配置文件
```

### 变更历史

- v2.0.0：移除 SearXNG/wigolo 依赖，新增 openalex/dblp/docker_hub/pypi/arch_wiki，Sogou 替代 Baidu 为默认中文引擎，total 23→28 引擎
- v1.0.0：初版，24 引擎
