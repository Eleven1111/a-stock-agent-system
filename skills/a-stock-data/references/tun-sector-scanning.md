# TUN 模式下板块扫描工作流

当 ClashX TUN 模式阻断了 East Money 的 `push2.eastmoney.com` 端点时，
`ak.stock_board_industry_name_em()` 等板块/行业数据 API 不可用。
以下是在此限制下进行板块筛选的可靠工作流。

## 核心原则

当无法动态获取板块成分股时，手工维护**目标板块的硬编码股票列表**，
用腾讯 API 取实时行情 + stock-analyst 做技术面筛选。

## 步骤

### Step 1: 确定板块候选股票

用已知知识列出目标板块的核心标的。
例如互联网电商板块的 6 个子分类、25 只股票。
来源：过往分析、券商研报、行业分类常识。

### Step 2: 腾讯 API 批量取行情

要点（已踩过的坑）：

1. **设置 NO_PROXY**
   ```python
   import os
   os.environ["NO_PROXY"] = ".gtimg.cn"
   ```

2. **股票代码在 parts[2]，不是 parts[0]**
   腾讯 API 返回格式：`v_sz300059="51~东方财富~300059~18.91~..."`，
   split("~") 后 parts[0]=市场代码(51), parts[2]=股票代码(300059)

3. **分批查询**，每批最多 10~20 只股票（实测单次可查 25 只）：
   ```python
   for i in range(0, len(codes), 10):
       batch = codes[i:i+10]
       url = f"http://qt.gtimg.cn/q={','.join(batch)}"
   ```

4. **字段提取速查：**
   | 索引 | 字段 | 处理 |
   |------|------|------|
   | val[2] | 股票代码 | 6位数字 |
   | val[1] | 名称 | |
   | val[3] | 现价 | 直接使用 |
   | val[32] | 涨跌幅% | |
   | val[37] | 成交额(万) | `/10000` = 亿元 |
   | val[38] | 换手率% | |
   | val[39] | 动态PE | 空=亏损/暂无 |
   | val[45] | 总市值(亿) | 直接float，已是亿单位 |

### Step 3: 技术面筛选

对候选列表中的质优标的（PE合理+市值适中），
逐只跑 `stock-analyst` 做技术分析：

```bash
bash -c 'source ~/.bashrc && ~/.hermes/hermes-agent/venv/bin/python3 \
  ~/.hermes/skills/stock-analyst/analyst.py analyze 603613 国联股份'
```

关注：RSI超卖/KDJ超卖/布林带位置/均线排列。

### Step 4: 合成结论

按 S/A/B/C 分级 + 具体买入价/止损价/目标价/仓位建议输出。

## 注意事项

- 该工作流**不适用于需要全市场动态选股的场景**
- 硬编码列表需要**定期维护更新**（建议每月审视一次）
- 如果 TUN 模式临时关闭，仍可走东财 API 获取完整板块数据
- 腾讯 API 数据是**前一交易日收盘数据**，盘中查询也是昨收
