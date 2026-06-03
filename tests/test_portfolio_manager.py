"""资金/仓位管理 — 迁移对账 + 并发安全 + 输入校验测试（纯逻辑，不触网）"""

import threading

import portfolio_manager as pm


def _wire(tmp_path, monkeypatch, initial=None):
    """把模块级文件常量指向临时目录，可选写入初始持仓。"""
    monkeypatch.setattr(pm, "PORTFOLIO_FILE", str(tmp_path / "portfolio.json"))
    monkeypatch.setattr(pm, "CASHFLOW_FILE", str(tmp_path / "cash_flow.json"))
    monkeypatch.setattr(pm, "HISTORY_FILE", str(tmp_path / "trade_history.json"))
    if initial is not None:
        pm.save_portfolio(initial)


# ========== CRITICAL A：老文件一次性现金对账 ==========

def test_legacy_cash_reconciled_once(tmp_path, monkeypatch):
    """旧版 cash 是未扣减的总本金 → 首次加载按 本金-持仓成本 重算，且幂等。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000,
        "positions": [{"code": "600011", "name": "华能", "cost": 9.1, "shares": 8000}],
        "total_cost": 72800,
    })  # 注意：无 cash_reconciled 标记 → 视为老文件

    pf = pm.ensure_portfolio()
    assert pf["cash"] == 27200, "可用现金应 = 100000 - 72800"
    assert pf["cash_reconciled"] is True

    # 幂等：再次加载不得二次扣减
    pf2 = pm.ensure_portfolio()
    assert pf2["cash"] == 27200


def test_new_file_not_double_reconciled(tmp_path, monkeypatch):
    """已带 cash_reconciled 的新文件不得被再次对账。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 30000, "positions": [], "total_cost": 70000, "cash_reconciled": True,
    })
    pf = pm.ensure_portfolio()
    assert pf["cash"] == 30000


# ========== CRITICAL B：portfolio.json 读改写并发安全 ==========

def test_concurrent_add_no_loss(tmp_path, monkeypatch):
    """10 个并发开仓不得丢持仓、现金扣减必须精确、账实一致。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })

    n = 10
    threads = [
        threading.Thread(target=pm.add_position, args=(f"60{i:04d}", f"股{i}", 10.0, 1000))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    pf = pm.load_portfolio()
    assert len(pf["positions"]) == n, f"并发开仓丢持仓: 期望{n} 实际{len(pf['positions'])}"
    assert pf["cash"] == 1000000 - n * 10000, "现金扣减不精确（丢更新）"

    buys = [c for c in pm.load_cashflow() if c["action"] == "buy"]
    assert len(buys) == n
    assert pf["cash"] == 1000000 - sum(c["amount"] for c in buys), "账本与余额不一致"


def test_concurrent_deposit_no_loss(tmp_path, monkeypatch):
    """20 个并发入金，最终现金必须等于全部入金之和。"""
    _wire(tmp_path, monkeypatch, initial={
        "cash": 0, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    n = 20
    threads = [threading.Thread(target=pm.deposit, args=(1000,)) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert pm.load_portfolio()["cash"] == n * 1000


# ========== 交易语义 ==========

def test_add_then_close_cash_roundtrip(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    pm.add_position("600011", "华能", 9.0, 2000)          # -18000
    assert pm.load_portfolio()["cash"] == 82000
    r = pm.close_position("600011", 10.0)                  # +20000
    assert r["ok"] and r["pnl"] == 2000
    assert pm.load_portfolio()["cash"] == 102000
    assert pm.load_portfolio()["positions"] == []


def test_add_weighted_average_cost(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 100000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    pm.add_position("600011", "华能", 10.0, 1000)   # 10000
    r = pm.add_position("600011", "华能", 12.0, 1000)  # 12000 → 均价11
    assert r["action"] == "加仓"
    assert r["cost"] == 11.0 and r["shares"] == 2000


# ========== 输入校验 / 余额不足 ==========

def test_withdraw_insufficient_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 5000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    r = pm.withdraw(9999)
    assert "error" in r and "ok" not in r
    assert pm.load_portfolio()["cash"] == 5000, "拒绝的出金不得改动余额"


def test_add_insufficient_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    r = pm.add_position("600011", "华能", 100.0, 1000)  # 需要 100000
    assert "error" in r
    assert pm.load_portfolio()["positions"] == []


def test_non_positive_inputs_rejected(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch, initial={
        "cash": 1000, "positions": [], "total_cost": 0, "cash_reconciled": True,
    })
    assert "error" in pm.deposit(0)
    assert "error" in pm.deposit(-100)
    assert "error" in pm.withdraw(-100)
    assert "error" in pm.add_position("600011", "华能", -1.0, 1000)
    assert "error" in pm.add_position("600011", "华能", 1.0, 0)
    assert pm.load_portfolio()["cash"] == 1000, "全部非法输入应为 no-op"
