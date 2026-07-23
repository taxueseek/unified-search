# Argo v2.2 — 问题重定义与系统优化

## 1. 重新定义问题

| 层级 | 表述 |
|------|------|
| 表象需求 | 统一搜索、多引擎、要快 |
| 第一性需求 | Agent 在有限上下文里 **吸收可核验事实** 并降低幻觉 |
| 可测目标 | 提高 top-k 中「非 SERP + 含证据块 + 可抓取」比例；降低口径混用 |

旧 KPI「召回条数 / 引擎数」与目标不对齐；新 KPI：

1. `serp_rate@top8` ↓  
2. `evidence_number_rate@top8` ↑  
3. `absorbable_domain_count`（交叉验证）↑  
4. 时效误判率（历史对比年当发布年）↓  

## 2. MECE 模块

| 模块 | 负责 | 不负责 |
|------|------|--------|
| Clarify | 意图可执行 | 不评分信源 |
| Selection | 能否进候选（权威/SERP/榜单） | 不判断正文是否写得好 |
| Absorption | 证据块密度与质量 | 不单独代表「真」 |
| Freshness | 时间可决策性 | 不替代权威 |
| Consensus | 多源是否同向 | 不覆盖单源细节 |

## 3. 量化信号（工程）

| 信号 | 来源 | 权重角色 |
|------|------|----------|
| is_serp | URL 模式 | Selection 熔断 |
| authority | 域名表 + source_types_cn | Selection |
| has_numbers / comparison / definition / howto | content_signals | Absorption |
| is_qa_format | 标题/结构 | Absorption 负向 |
| publish year | 完整日期 > 近年 > URL 年；挖掉「以来」 | Freshness |
| content_domains | 非 SERP 域名集合 | Consensus |

## 4. 开发闭环

```
假设（GEO 实证）→ 改 evidence/content_signals
  → 单测 test_evidence_v22
  → A/B 同查询对比 serp_rate / number_rate
  → 文档回写 SKILL Agent 纪律
```

## 5. 已知边界

- snippet 级 absorption ≠ 全文抓取后的真吸收；高后果必须 fetch  
- 多平台 AI 引用共识（CN-GEO）未接入热路径，仅方法兼容  
- 反爬导致高权威站 absorption 失败时，应用同结论镜像，不硬撑  

## 6. 回归命令

```bash
cd ~/.claude/skills/argo
python3 -m pytest tests/test_evidence_v22.py -v
# 可选：对真实搜索 JSON 跑 evidence
argo search "2026公募基金二季报 持仓" --mode deep --json | \
  python3 scripts/evidence.py "2026公募基金二季报" --stdin --json
```
