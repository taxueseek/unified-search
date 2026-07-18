<p align="center">
  <img src="docs/assets/hero.svg" width="600" alt="Unified Search">
</p>

<h3 align="center">统一搜索引擎</h3>

<p align="center">
 你给它一个搜索查询，它自动路由到最合适的搜索引擎，融合多个结果后返回。覆盖中文/英文/学术/代码/购物/金融/新闻/百科/计算/深度研究等场景。
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#架构设计">架构</a> ·
  <a href="#引擎全景">引擎</a> ·
  <a href="#使用示例">示例</a> ·
  <a href="#安装配置">配置</a> ·
  <a href="#版本历史">更新</a>
</p>

---

## 这是什么

**一个搜索引擎，替代所有搜索引擎。**

你问「贵州茅台股价」，它自动走东财；你问「transformer attention paper」，它自动走 arXiv；你问「React vs Vue 哪个好」，它同时打知乎和字节搜索，合并去重后返回。

不是又一个搜索 API 封装。它是一套完整的搜索基础设施：

- **40 个引擎**（12 API + 28 本地零成本），自动选最优路径
- **TF-IDF 语义路由**，理解查询意图，不靠关键词死匹配
- **双层缓存**（内存 LRU + SQLite），相同查询不重复花钱
- **四层降级链**（T1 API → T2 本地 → 缓存 → 报错），永不空手而归
- **零外部依赖**，Python 标准库 + PyYAML 即可运行

```
你输入查询
    │
    ▼
┌─────────────────┐
│  TF-IDF 语义路由  │  ← 理解你想搜什么
│  + 正则硬规则     │
└────────┬────────┘
         │
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌───────┐
│ T1    │ │ T2    │
│ API   │→│ 本地  │  ← 降级链
│ 12个  │ │ 28个  │
└───┬───┘ └───┬───┘
    │         │
    └────┬────┘
         ▼
┌─────────────────┐
│  RRF 多源融合    │  ← 多引擎结果合并去重
│  + Bocha 精排    │
└────────┬────────┘
         ▼
    统一 JSON 输出
```

## 为什么需要它

| 普通做法 | Unified Search |
|---------|---------------|
| 一个 API Key 绑死一个引擎 | 40 个引擎自动切换，哪个好用哪个 |
| 中文搜用百度（CAPTCHA 拦截 10s+） | 自动走搜狗（0.91s）或字节搜索 |
| 缓存要么全有要么全无 | 金融 5 分钟 / 新闻 10 分钟 / 常青 24 小时分级 TTL |
| 引擎挂了就挂了 | 四层降级链，T1 挂了走 T2，T2 挂了读缓存 |
| 搜索结果直接拼接 | RRF 融合 + 语义精排，去重后按相关性排序 |
| 每次搜索都花 token | 28 个本地引擎零成本，缓存命中零延迟 |

## 快速开始

```bash
# 克隆
git clone https://github.com/taxueseek/unified-search.git
cd unified-search

# 安装依赖（仅 PyYAML）
pip install pyyaml

# 搜索
python3 scripts/search.py "Python asyncio"

# JSON 输出
python3 scripts/search.py "贵州茅台股价" --json

# 显示路由决策
python3 scripts/search.py "transformer attention paper" --explain
```

## 引擎全景

### T1 — 直连 API（首选层）

| 引擎 | 覆盖 | 延迟 | 成本 | 状态 | 说明 |
|------|------|------|------|------|------|
| **anysearch** | 通用/技术 | 2s | 免费 | ✅ | 通用搜索主力，本地 CLI |
| **zhihu** | 中文/评测 | 1.5s | API | ✅ | 知乎搜索，中文观点/评测 |
| **eastmoney** | 金融/股票/基金 | 1.2s | 免费 | ✅ | 东方财富，金融数据首选 |
| **byted** | 通用/新闻/中文 | 1.5s | API | ✅ | 字节搜索 API |
| **tavily** | 通用 | 3s | API | ✅ | Tavily AI 搜索 |
| **github** | 代码 | 2s | API | ✅ | GitHub 代码搜索 |
| **arxiv** | 学术 | 3.5s | 免费 | ✅ | arXiv 论文搜索 |
| semantic_scholar | 学术 | 3s | 免费 | ⏸️ | Semantic Scholar |
| openalex | 学术 | 4s | 免费 | ⏸️ | OpenAlex 开放学术 |
| crossref | 学术 | 3s | 免费 | ⏸️ | Crossref DOI |
| bocha | 中文/通用 | 1.5s | API | ⏸️ | 博查搜索 |
| brave | 通用 | 1.5s | API | ⏸️ | Brave Search |
| metaso | 中文/通用 | 3s | API | ⏸️ | 秘塔搜索 |
| felo | 通用/技术 | 4s | API | ⏸️ | Felo AI 搜索 |
| uapi | 通用 | 2s | API | ⏸️ | UAPI 聚合搜索 |
| duckduckgo | 通用 | 1.5s | 免费 | ⏸️ | DuckDuckGo |
| wolframalpha | 事实/数学 | 2s | API | ⏸️ | WolframAlpha |
| wikipedia | 百科 | 1s | 免费 | ⏸️ | Wikipedia |

### T2 — 本地引擎（零成本层）

| 引擎 | 覆盖 | 延迟 | 说明 |
|------|------|------|------|
| **bing** | 通用 | 610ms | Bing 网页搜索 — 最佳通用 |
| **sogou** | 中文 | 910ms | 搜狗搜索 — 最佳中文（替代百度） |
| duckduckgo | 通用 | 1013ms | DuckDuckGo 搜索 |
| mojeek | 通用 | 2121ms | Mojeek 独立搜索 |
| yandex | 通用 | 3071ms | Yandex 搜索 |
| startpage | 通用 | 3515ms | Startpage 隐私搜索 |
| wikipedia | 百科 | 747ms | Wikipedia 百科 |
| arxiv | 学术 | 3403ms | arXiv 论文 |
| pubmed | 医学 | 1516ms | PubMed 生物医学 |
| crossref | 学术 | 2094ms | Crossref DOI |
| bing_news | 新闻 | 782ms | Bing 新闻 |
| google_news | 新闻 | 4006ms | Google News |
| github | 代码 | 1414ms | GitHub 仓库 |
| stackoverflow | 代码 | 1981ms | Stack Overflow |
| gitlab | 代码 | 1947ms | GitLab 项目 |
| npm | 代码 | 2389ms | npm 包搜索 |
| imdb | 影视 | 2637ms | IMDb 电影 |
| goodreads | 图书 | 489ms | Goodreads 图书 |

## 路由决策

查询进来后，系统做三层判断：

```
查询 → 特征提取 → 三路并行判断
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
    正则硬规则    TF-IDF 语义    主题分类
    (13 个域)    (余弦相似度)   (5 个主题)
         │           │           │
         └─────┬─────┘           │
               ▼                 │
          融合决策 ←─────────────┘
               │
               ▼
         引擎组合 + 搜索深度
```

### 13 个领域路由

| 域 | 触发词 | 首选引擎 | 典型场景 |
|----|--------|---------|---------|
| `stock_query` | 股价/涨跌/K线/大盘 | eastmoney | 贵州茅台股价 |
| `fund_query` | 基金/ETF/基金经理 | eastmoney | 基金净值排行 |
| `financial_news` | 研报/财报/宏观 | byted | 美联储加息 |
| `zhihu_content` | 知乎/怎么看待/推荐 | zhihu | 笔记本推荐 |
| `academic` | 论文/paper/arXiv | arxiv | transformer attention |
| `tech_deep` | research/综述/深度 | semantic_scholar | 深度学习综述 |
| `code_search` | github/代码/函数 | github | Python asyncio |
| `fact_check` | what is/是什么/天气 | duckduckgo | 北京天气 |
| `news_realtime` | 最新/今天/突发 | byted | 诺贝尔奖 |
| `shopping` | 评测/推荐/值得买 | zhihu | 笔记本电脑推荐 |
| `chinese_general` | 中文通用 | bocha | 中文通用搜索 |
| `general_search` | 兜底 | byted | 其他查询 |

### 搜索深度自动选择

| 深度 | 触发条件 | 说明 |
|------|---------|------|
| `ultra_fast` | what/who/when/多少 | 事实/数字查询 |
| `fast` | tutorial/how to/教程 | 教程/指南 |
| `balanced` | vs/对比/评测 | 对比/评测 |
| `deep` | research/综述/深度分析 | 深度研究 |

## 使用示例

### 金融搜索

```bash
$ python3 scripts/search.py "贵州茅台股价" --explain

[路由] 中文 + 金融向 → 命中域 [stock_query] → 东方财富
=== 5 results (1200ms via eastmoney)
  [0.95] 贵州茅台(600519)股票价格_行情_走势图
    https://quote.eastmoney.com/sh600519.html
  [0.87] 贵州茅台最新行情分析
    ...
```

### 学术搜索

```bash
$ python3 scripts/search.py "transformer attention mechanism paper" --json

{
  "query": "transformer attention mechanism paper",
  "engine": "arxiv",
  "engines": ["arxiv", "semantic_scholar", "openalex"],
  "domain": "academic",
  "elapsed_ms": 3400,
  "results": [
    {
      "title": "Attention Is All You Need",
      "url": "https://arxiv.org/abs/1706.03762",
      "snippet": "We propose a new simple network architecture, the Transformer...",
      "score": 0.95,
      "source": "arxiv"
    }
  ]
}
```

### 多引擎融合

```bash
$ python3 scripts/search.py "React vs Vue 2026" --tier api

# 同时查询知乎 + 字节搜索，RRF 融合去重
=== 8 results (2100ms via zhihu+byted)
  [0.92] React vs Vue: 全面对比 2026
    https://...
  [0.88] Vue 3 vs React 19 性能对比
    ...
```

## 作为库使用

```python
from scripts.search import super_search

# 自动路由
result = super_search("Python asyncio", n=5)
print(result["results"])

# 指定引擎
result = super_search("黄金价格", engine="eastmoney", n=3)

# 跳过缓存
result = super_search("最新新闻", skip_cache=True)

# 进度回调
def on_progress(stage, data):
    print(f"[{stage.value}] {data}")

result = super_search("深度学习", on_progress=on_progress)
```

## 安装配置

### 环境要求

- Python 3.10+
- PyYAML（`pip install pyyaml`）
- 无需 Node.js、SearXNG 或其他外部服务

### 配置 API Key（可选）

以下引擎需要 API Key，不配置则自动跳过：

```bash
# .env 或系统环境变量
export TAVILY_API_KEY="tvly-xxx"         # Tavily AI 搜索
export BOCHA_API_KEY="xxx"               # 博查搜索
export BRAVE_API_KEY="xxx"               # Brave Search
export METASO_API_KEY="xxx"              # 秘塔搜索
export FELO_API_KEY="xxx"               # Felo AI 搜索
export ZHIHU_ACCESS_SECRET="xxx"         # 知乎搜索
export GITHUB_TOKEN="ghp_xxx"           # GitHub 代码搜索（可选，提高限频）
export WOLFRAM_APPID="xxx"              # WolframAlpha
export WEB_SEARCH_API_KEY="xxx"         # 字节搜索
```

> **零配置可用**：不配任何 Key，18+ 个免费引擎（anysearch + 28 个 T2 本地引擎）即可工作。

### API Key 配置（可选）

`anysearch` 和 `eastmoney` 引擎支持 API Key 提升配额。不配置则使用匿名访问（限频较低）。

```bash
export ANYSEARCH_API_KEY="as_sk_xxx"    # AnySearch API Key（可选）
export EASTMONEY_APIKEY="xxx"           # 东方财富 API Key（可选）
```

### 缓存配置

```yaml
cache:
  db_path: ~/.cache/unified-search/cache.db  # 缓存数据库路径
  enabled: true
  max_size_mb: 500                           # 最大缓存 500MB
  ttl: 3600                                  # 默认 TTL 1 小时
```

分级 TTL（自动生效）：

| 域 | TTL | 说明 |
|----|-----|------|
| 金融（股票/基金） | 5 分钟 | 数据变化快 |
| 新闻 | 10 分钟 | 时效性要求高 |
| 实时事件 | 15 分钟 | 突发/赛事 |
| 通用搜索 | 1 小时 | 默认 |
| 深度研究 | 2 小时 | 论文/综述 |
| 常青内容 | 24 小时 | 百科/定义 |

## 文件结构

```
unified-search/
├── README.md                           # 本文档
├── LICENSE                             # MIT License
├── .gitignore
├── config.yaml                         # 域路由 + 引擎注册 + 缓存/执行配置
├── backends/
│   ├── engine_registry.yaml            # 三层引擎注册表（T1+T2 元数据）
│   ├── domain_profiles.json            # TF-IDF 领域文档（语义路由用）
│   └── quota_profiles.json             # 配额/成本配置
├── scripts/
│   ├── search.py                       # CLI 主入口
│   ├── route.py                        # 双层路由 + 降级链
│   ├── engines.py                      # 统一引擎执行器（18+ 解析器）
│   ├── config.py                       # 配置加载器（热加载）
│   ├── cache.py                        # 双层缓存（L1 LRU + L2 SQLite）
│   ├── tfidf_router.py                 # TF-IDF 语义路由引擎
│   ├── health_check.py                 # 引擎健康检测
│   ├── quota.py                        # 配额管理
│   ├── search_types.py                 # 结果标准化类型
│   └── benchmark.py                    # 性能基准测试
├── local-search/                       # T2 本地引擎层（28 个零成本引擎）
│   ├── search_v3.py                    # 搜索执行 + 健康检查
│   └── config.json                     # 本地引擎配置
├── tests/
│   ├── test_unit.py                    # 单元测试
│   ├── test_integration.py             # 端到端验收
│   ├── test_full.py                    # 完整集成测试
│   └── test_new_engines.py             # 新引擎集成测试
└── docs/
    └── architecture.md                 # 架构文档
```

## 设计哲学

1. **零依赖优先。** Python 标准库 + PyYAML 即可运行。不引入 requests、httpx、flask 等第三方库。
2. **降级不报错。** 任何引擎失败都不影响整体。T1 挂了走 T2，T2 挂了读缓存，缓存没有返回空结果 + 明确错误信息。
3. **配置驱动。** 新增引擎只需在 `engine_registry.yaml` 加一行 + `config.yaml` 加路由规则，不改代码。
4. **缓存分级。** 金融数据 5 分钟过期，常青内容 24 小时。不一刀切。
5. **路由透明。** `--explain` 输出完整路由决策链，方便调试和优化。

## CLI 参数

```
用法: python3 scripts/search.py [选项] 查询词

选项:
  --engine, -e       搜索引擎（默认 auto）
  --max-results, -n  最大结果数（默认 5）
  --depth, -d        搜索深度（ultra-fast/fast/balanced/deep）
  --tier             引擎层级（api/local/all）
  --no-cache         禁用缓存
  --explain          显示路由决策
  --json             JSON 输出
  --timeout, -t      超时秒数（默认 10）
  --list-engines     列出可用引擎
  --progress         打印进度阶段（调试用）
```

## 适用平台

作为 Python 脚本，任何支持命令行调用的环境都能用：

- **AI Agent 集成**：Claude Code / Grok Build / Codex 等，作为搜索后端
- **脚本调用**：Shell / Python / Node.js 子进程
- **Web 服务**：包装为 Flask/FastAPI 接口
- **CI/CD**：自动化测试中的搜索验证

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| **v1.0.0** | 2026-07 | 首次公开发布。40 引擎（12 T1 + 28 T2），TF-IDF 语义路由，双层缓存，四层降级链，Bocha Reranker 精排 |

## 贡献

欢迎提交 Issue 和 Pull Request。

1. Fork 本仓库
2. 创建特性分支（`git checkout -b feature/amazing-feature`）
3. 提交更改（`git commit -m 'Add amazing feature'`）
4. 推送到分支（`git push origin feature/amazing-feature`）
5. 创建 Pull Request

## License

MIT License © 2026 [taxueseek](https://github.com/taxueseek)

---

> 好的搜索引擎不是让你搜得更多，是让你搜得更准。输入查询，它替你选路。
