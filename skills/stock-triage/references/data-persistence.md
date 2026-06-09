# 持仓数据持久化机制与风险说明

## 为什么对话上下文不可靠

A 股 Agent 系统的持仓/资金/交易数据绝不能只靠对话上下文或记忆保存。

数据丢失的根因链（2026-06-03 实测）：
1. 用户告知持仓 → Agent 记住但未写入文件
2. 上下文压缩触发——73 条消息被移除且未做摘要
3. 下一轮 Agent 读 `portfolio.json` 见空仓
4. 用户发现数据丢了，暴怒

### 技术细节
- `state.db`（~/.hermes/state.db, 217MB）是 Hermes 真实 session DB
- `hermes-sessions.db`（~/.hermes/data/）**不是** Hermes 文件——全代码库 0 引用
- 上下文压缩不创建 parent session 拆分，被移除消息永久消失
- session_search 无法找回被压缩丢弃的消息

## 三文件持久化系统

| 文件 | 路径 | 内容 |
|------|------|------|
| portfolio.json | `$HERMES_HOME/skills/stock-triage/data/portfolio.json` | 当前持仓 |
| trade_history.json | 同目录 | 已清仓交易 |
| cash_flow.json | 同目录 | 资金流水 |

## 并发安全（两次犯错教训）

| 函数 | 场景 |
|------|------|
| `mutate_json(path, mutator, default)` | 任意读-改-写事务 |
| `update_json_list(path, item, dedup_key)` | 追加到列表 |

已犯错误：performance_tracker 40 并发丢记录（PR #2）、portfolio_manager 10 并发只剩 4 笔（PR #4）。

## 老文件迁移
v1.0 不扣现金，cash=总本金。_normalize()：可用现金 = 本金 − 持仓成本。打 cash_reconciled 标记。

## 输入校验
- 金额/股数拒绝零和负数
- CLI 判参用 is not None（不用 if args.x: 会吞 0）
