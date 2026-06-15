# portfolio_manager.py API 参考

## 数据文件

| 文件 | 常量 | 用途 |
|------|------|------|
| `data/portfolio.json` | `PORTFOLIO_FILE` | 持仓状态：cash / positions[] / total_cost / cash_reconciled |
| `data/cash_flow.json` | `CASHFLOW_FILE` | 流水：每笔 buy/sell/deposit/withdraw 记录 |
| `data/trade_history.json` | `HISTORY_FILE` | 已清仓交易：用于凯利公式胜率统计 |

## Python API

所有写操作线程安全（`threading.RLock` + 原子写 via `os.replace`）。

### 持仓操作

```python
pm.add_position(code, name, cost, shares)
# → 开仓/加仓。自动从 cash 扣除 cost×shares。
# → 余额不足返回 {"error": "现金不足: ..."}
# → 加仓时按加权平均更新 cost

pm.close_position(code, sell_price)
# → 清仓。自动将 proceeds 加入 cash。
# → 写入 trade_history.json + cash_flow.json

pm.ensure_portfolio()
# → 加载持仓。对老版本执行一次性现金对账（cash -= total_cost）。
# → 写入 cash_reconciled 标记，幂等。
```

### 现金操作

```python
pm.deposit(amount)
# → 入金，amount 必须 > 0

pm.withdraw(amount)
# → 出金，余额不足返回 {"error": "..."}
```

### 查询

```python
pm.load_portfolio()   # 直接读取文件（不加对账逻辑）
pm.load_cashflow()    # 读取现金流记录
pm.load_history()     # 读取已清仓历史
```

## 线程安全设计

```
add_position()
  → with _portfolio_lock:           # RLock — 可重入
       pf = load_portfolio()         # 内部也拿 _portfolio_lock（安全，RLock）
       pf["cash"] -= total
       save_portfolio(pf)
       save_cashflow(...)
```

**关键坑位：** 曾经用 `threading.Lock()`，`add_position()` 持锁后 `load_portfolio()` 内部再次 `acquire` → 死锁。**必须用 `RLock`**。

## 老文件对账

旧版 `portfolio.json` 中 `cash` = 总本金（未扣除持仓成本），不是可用现金。

```python
def ensure_portfolio():
    pf = load_portfolio()
    if not pf.get("cash_reconciled"):
        # 老文件：cash 是总本金 → 减去持仓成本 = 实际可用现金
        if pf["total_cost"] > 0 and pf["cash"] >= pf["total_cost"]:
            pf["cash"] = pf["cash"] - pf["total_cost"]
        pf["cash_reconciled"] = True
        save_portfolio(pf)
    return pf
```

对账仅执行一次，之后 `cash_reconciled=True` 阻止重复扣减。

## CLI 接口

```bash
# 开仓（不支持 --shares 时默认 1000 股？实际需传 --shares）
python3 portfolio_manager.py --add 600519 贵州茅台 150.00 --shares 100

# 清仓
python3 portfolio_manager.py --close 600519 155.00

# 风控检查（默认行为）
python3 portfolio_manager.py --check
python3 portfolio_manager.py --check --json
```

注意：CLI 模式下 `--add` 不走 `add_position()` 内的现金扣减逻辑（直接调函数，会正常扣减）。但 `elif args.check or True` 无条件触发检查是一个已知问题——如果是 `--add` 后也会执行 `--check`，不会出错但多余。
