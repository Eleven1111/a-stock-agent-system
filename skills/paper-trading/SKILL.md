---
name: paper-trading
description: 研究专用模拟交易账户，严格执行开盘推荐后 Chanlun 二次门控。
---

# 模拟交易

本模块只积累研究数据，不连接券商，不发送真实订单，也不影响正式推荐排序。

不可变入场顺序：

```text
09:35 开盘推荐通过 -> 开盘确认通过 -> Chanlun 看多结构通过 -> 成交检查 -> 模拟买入
```

Chanlun 只能筛选已有推荐，不能产生候选、改变排名或提高推荐分。模拟账户初始资金
10 万元，使用独立 `paper_portfolio.json` 投影；规范事件仍追加到统一
`signal_ledger.jsonl` 的 `paper.*` 命名空间。

运行入口：

```bash
python skills/paper-trading/scripts/paper_trading_runner.py --phase open --json
python skills/paper-trading/scripts/paper_trading_runner.py --phase monitor --json
python skills/paper-trading/scripts/paper_trading_runner.py --phase close --json
```
