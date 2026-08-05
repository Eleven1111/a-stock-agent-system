---
name: announcement-radar
description: >-
  Whole-market A-share announcement recall. It fetches every CNINFO filing for
  a disclosure day, classifies it into 8/43 event categories with a progress
  stage, and splits the result into three recall buckets (divergence, unmatched,
  extreme) for agent deep-reading. It is a triage layer, not a scorer: it emits
  no ranked list and no trading advice.
version: 1.0.0
author: Luna
metadata:
  hermes:
    tags: [A股, 公告, 召回, 事件驱动, 巨潮]
    category: finance
---

# 公告召回雷达

把每天 ~1450 条 A 股公告压成 ~165 条 / ~115 家的可深读集合，交给 Agent 判断。

方法源自中国银河证券《公告贝叶斯评分系统与公告筛选》（2026-07-31），
但**本仓库只取其分类与评分作为分流依据，不取其选股结论**——原因见下节。

## 运行边界

- **不产出 Top-N 名单，不产出买卖建议。** 分数只用于三桶阈值判定。
- **只读标题，不读正文。** 定期报告（年报/半年报/季报/业绩预告）按报告口径
  不设先验、不参与召回；要分析财报走财报那条路。
- 抓取层在 `skills/common/cninfo_client.py`，与 `announcement_risk`
  共用巨潮客户端；全市场扫描计入独立的 `cninfo_bulk` 限流桶，
  不与逐股风险扫描抢配额。

## 为什么不排序（2026-08-04 实测结论）

对 2026-07-31 / 08-01 双日全量实测，原 skill 的「每日选 Bayes NSS 前 20 只」不可用：

- **Top-N 没有唯一解**：08-01 去重后 501 只个股，Top20 的分数门槛是 7 分，
  而并列 7 分的有 **45 只**。取哪 20 只取决于巨潮接口返回顺序，不是分数。
  分数是整数，1296 条有效评分只落在 16 个取值上，分辨率不足以排序。
- **双评分实质是单评分**：anchored 模式下 `bayes == nss` 的占比为 68.0%（07-31）
  / 69.1%（08-01）。内置 `bayes_params.json` 是从报告公开结果反推的锚定值，
  不是原始 MCMC 后验；重训练需要 pymc（本仓库未装）。
- **92% 命中率掩盖了漏网结构**：未命中的 8% 里是「核电项目核准」「德国 GMP 证书」
  「一致行动协议」这类高价值非常规事件——常规公告好命中，有 alpha 的漏。

所以这里的产物是**分流结果**：哪些公告值得人/Agent 再看一眼，以及为什么。

## 三桶召回

| 桶 | 规则 | 07-31 | 08-01 | 意图 |
|---|---|---|---|---|
| **分歧** | `\|bayes − nss\| ≥ 3` | 83 | 98 | 规则分与历史统计位置不一致处才有信息量 |
| **漏网** | 分类未命中 + 标题含强事件词 | 18 | 18 | 捞回关键词词典的结构性盲区 |
| **极值** | `\|nss\| ≥ 6`，排除回购/股权激励 | 64 | 63 | 先验强信号；两类常态噪音每日 ~105 条，剔除 |
| | **合并去重** | **160 / 127 家** | **169 / 105 家** | 压缩比 ~11% |

阈值在 `assets/recall_rules.json`，改阈值必须复跑这两日并更新 `expected_yield`。
`limits` 里的规模护栏会在召回条数越界时**抛错拒绝交付**——阈值或上游数据变了
比交付一份规模失控的结果更需要被看见。

## 运行

```bash
A_STOCK_STATE_HOME=/Users/na/.a-stock-agent-cc PYTHONPATH=skills/common \
  .venv/bin/python skills/announcement-radar/scripts/radar.py --date today --emit-brief
```

耗时 6~14 分钟（2026-08-04 两次实测：5:59 与约 13 分钟）：约 50 次接口请求
× `http_client` 2.5s/源 节流，实际耗时受网络波动影响很大。

产物：

- `$A_STOCK_STATE_HOME/cron/output/announcement-radar/<date>.json` —— 全量召回集，
  每条只留白名单字段（无正文、无 PDF）
- stdout lite 简报 —— 桶计数 + 每桶前 5 条，受 `max_output_chars: 2400` 约束

**全量 artifact 不进上下文。** 需要细节时按路径读文件。

## 第四步：你要做的深读

程序给的是「哪些值得看」，不是「看出了什么」。对召回集里的条目：

1. **先看漏网桶**——它们没有分数，全靠你判断，且信息价值通常最高
2. **再看分歧桶**——问「为什么规则分和历史统计不一致」，这是结构性线索
3. 需要正文时调 `announcement_risk.extract_pdf_text(url)`（依赖 pypdf，已是主依赖）。
   要区分「没抽到正文」和「压根没抽」时改调
   `extract_pdf_text_with_status(url) -> (text, status)`，status ∈
   `ok` / `empty` / `no_url` / `fetch_failed` / `parse_failed` / `pdf_backend_missing`；
   `fetch_announcements()` 返回的每行也带同名 `text_status` 字段。
   拿到 `pdf_backend_missing` 说明环境装漏了 pypdf（`pip install -c constraints.txt -e .`），
   **不要**当成「这份公告没正文」继续往下解读。

输出结构化解读时**严格区分原文事实与推断**：事项阶段（别把「拟建设」读成「已达产」）、
关键数字（未披露就写「未披露」，不估算）、业务与财务影响（量不出来就写「无法量化」）、
具体风险、后续跟踪节点、原文链接。

## 已知边界

- **行业效应很弱**：报告表 15 显示科技/消费/周期的 Bayes 均值差异只在 0.1 量级，
  别指望行业维度提供多少增量。
- **行业名有两套口径**：`industry_map.load_cached()` 返回的是东财板块名与国民经济
  行业分类混合的 127 个名称，不是申万一级。`assets/industry_map.json` 里
  `sw_to_group`（报告口径）与 `em_to_group`（兜底）双表查找，覆盖缓存全部 127 个名称。
  新增名称会静默落入「其他」，靠 `stats.industry_mapped_rate` 暴露。
- **行业缓存本身覆盖不全**：2026-08-05 实测识别率 68.7%，缺口不在映射表
  （缓存内行业名 100% 认识），而是缓存只有 4053 只个股、A 股约 5400 只，
  缺的集中在创业板/科创板；上游 `industry-map-refresh` 作业当前仍在失败。
- **关键词分类会漏**：命中率 < 85% 时会告警，去补 `assets/taxonomy.json`。
  漏网桶的强事件词表同样需要随样本迭代。
- **策略会失效**：报告自承 2026 年以来收益转负（-0.11%），归因为市场风格切换。
  这也是本仓库只用它做召回、不用它做决策的原因之一。
- 结论基于历史价格与统计规律，不构成投资建议。

## 目录

```
assets/
  taxonomy.json          43 二级分类的关键词、排除词、NSS 先验、极性反转规则
  stage_rules.json       5 阶段识别规则与优先级
  industry_map.json      申万一级 → 科技/消费/周期/其他
  bayes_params.json      锚定模式贝叶斯参数（非原始后验）
  recall_rules.json      三桶阈值与规模护栏（已在 07-31/08-01 定标）
scripts/
  classify.py            分类 + 阶段（纯计算）
  score.py               NSS + Bayes 双评分（纯计算）
  recall.py              三桶召回 + lite 投影 + 护栏（纯计算）
  radar.py               编排入口
references/
  methodology.md         方法论、公式推导、参数来源、已知局限
```
