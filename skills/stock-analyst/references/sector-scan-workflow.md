# 全市场扫描 + 深层选股工作流

基于本会话开发的三因子板块分析方法。

## 工作流

### 第1步：全量板块扫描

```bash
cd ~/.hermes/skills/stock-analyst
~/.hermes/hermes-agent/venv/bin/python3 scripts/sector_scan.py
```

⚠️ **必须遍历所有行业板块，不能只用预设板块**。sector_scan.py 用 AkShare 的 stock_zt_pool_em() 获取近7日所有涨停板，按行业聚合，输出热度评分。

热度分公式：涨停数/5*0.4 + 出现天数/7*10*0.3 + 最高连板数*0.3

### 第2步：板块轮动追踪

```bash
~/.hermes/hermes-agent/venv/bin/python3 ~/.hermes/skills/hot-money-tactics/scripts/analyze.py --rotation
```

识别：持续热点 / 新冒头板块 / 退潮板块 / 轮动方向。

### 第3步：重点板块个股深度分析

```bash
# 预设板块批量扫描（快速筛选）
~/.hermes/hermes-agent/venv/bin/python3 analyst.py screen 半导体

# 单股深度分析（获取完整技术指标）
~/.hermes/hermes-agent/venv/bin/python3 analyst.py analyze 002156 通富微电
```

### 第4步：三因子交叉判断

| 维度 | 数据来源 | 权重 |
|------|---------|------|
| 涨停热度 | sector_scan.py 输出 | 判断短期资金方向 |
| 技术面 | analyst.py analyze 输出 | 判断中期位置（超买/超卖/中性） |
| 宏观催化 | 新闻/政策/季节性分析 | 判断未来3个月驱动力 |

### 第5步：输出格式

必须包含：
- 标的名称和代码
- 当前价格和技术面摘要
- 买入区间（给出具体价格范围）
- 止损位
- 目标位（至少两个目标）
- 持有周期
- 仓位建议
- 推荐理由（一句话核心逻辑）

分级体系：
- S级：现在就能建仓，确定性最高
- A级：等回调或等催化剂
- B级：观察期
- C级：回避

## 注意事项

- 涨停板数据用 stock_zt_pool_em() 
- 实时行情用腾讯 qt.gtimg.cn（已内置降级链）
- 历史K线：腾讯 ifzq → 新浪 → BaoStock（已内置降级链）
- **资金流向：** `akshare stock_individual_fund_flow()` 现在可用（需 `.env` 配置 `NO_PROXY` 绕过 Clash）
- sector_scan.py 动态计算最近 7 个交易日，无需手工改日期
