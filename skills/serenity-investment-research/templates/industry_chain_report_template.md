# 《{industry_name} 产业链投研报告》

> 本报告仅用于研究和信息整理，不构成任何投资建议。  
> 研究日期：{date}  
> 研究深度：{research_depth}  
> 投资周期假设：{time_horizon}

## 1. 一句话结论

说明产业链处于哪个商业化阶段，真正的 chokepoint 在哪里，当前市场是否已充分定价。

## 2. 产业阶段判断

| 阶段 | 当前判断 | 证据 | 可信度 |
|---|---|---|---|
| 技术成熟度 | {view} | {evidence} | {grade} |
| 商业化 | {view} | {evidence} | {grade} |
| 量产爬坡 | {view} | {evidence} | {grade} |
| 盈利能力 | {view} | {evidence} | {grade} |

## 3. 产业链地图

```text
{value_chain_map}
```

## 4. 产业链层级排序（Chokepoint 排名）

层级拆细，不许混桶（算力芯片、EDA/IP、存储互连、设备、材料、测试封装、光链路、
PCB/CCL、电源散热分开）。每层给出稀缺度论证：供应商数量 / 认证周期 / 扩产难度 /
专用设备与 know-how / 预付款与产能预订。

| 排名 | 层级/节点 | 稀缺度论证（谁是真扩产约束） | 证据 | 反证 | 拥挤度 |
|---:|---|---|---|---|---|
| 1 | {node} | {scarcity_reason} | {evidence} | {bear} | {crowding} |

## 5. 公司排序（标的池）

每个最终候选回答五问：卡住哪个环节 / 链上位置 / 为什么排这里 / 证据 / 什么情况推翻。

| 公司 | 市场/代码 | 层级/节点 | 卡住哪个环节 | 为什么排这里 | 证据(ID) | 什么情况推翻 | 主要风险 |
|---|---|---|---|---|---|---|---|
| {company} | {ticker} | {node} | {constraint} | {rank_reason} | {evidence_ids} | {invalidation} | {risk} |

## 6. 被降级的热门方向

至少点名一个市场热门但排序靠后的方向，并解释原因（强制反共识检查）。

| 热门方向 | 市场共识 | 为什么降级 | 什么情况恢复 |
|---|---|---|---|
| {direction} | {consensus} | {downgrade_reason} | {restore_condition} |

## 7. 催化剂日历

| 时间 | 事件 | 影响节点 | 验证标准 | 失败信号 |
|---|---|---|---|---|
| {date} | {event} | {node} | {standard} | {failure} |

## 8. What Would Prove This Theme Is Overhyped

| 反证 | 触发信号 | 监控来源 | 影响 |
|---|---|---|---|
| {bear} | {trigger} | {source} | {impact} |

## 9. 红旗清单

ledger 中全部 `red_flag` 条目逐条披露；无红旗时写明「本次扫描未命中红旗清单」。

| 红旗 | 涉及公司/层级 | 证据(ID) | 对评分的影响 |
|---|---|---|---|
| {red_flag} | {target} | {evidence_id} | {score_impact} |

## 10. Serenity 评分

| 维度 | 权重 | 分数 | 依据 |
|---|---:|---:|---|
| 行业空间 | 15% | {score} | {reason} |
| 商业模式 | 20% | {score} | {reason} |
| 竞争格局 | 15% | {score} | {reason} |
| 财务质量 | 15% | {score} | {reason} |
| 估值赔率 | 20% | {score} | {reason} |
| 风险控制 | 15% | {score} | {reason} |

## 11. 跟踪清单与优先研究名单

| 指标 | 更新频率 | 阈值 | 来源 |
|---|---|---|---|
| {metric} | {frequency} | {threshold} | {source} |

优先研究名单：{priority_research_list}

## 12. 资料来源

| 来源 | 日期 | 链接/文件 | 可信度 | 支持结论 |
|---|---|---|---|---|
