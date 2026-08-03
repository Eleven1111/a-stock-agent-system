"""capital-flow 扫描规模上限 — 防作业超时，且截断必须留痕。

背景：个股资金流每只需一次东财往返（本机实测 1.3~3.5s，生产机曾记录 ~10s），
而 cron manifest 里 capital-flow 的 run.timeout_seconds=300。全量标的（曾达 171 只）
必然超时，超时会让整个作业无产出——连本可跑完的那部分也拿不到。

本文件锁定两件事：① 截断确实发生且持仓优先；② 截断不静默（产物字段 + 报告文本）。
"""

import importlib.util
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def load_cfm():
    spec = importlib.util.spec_from_file_location(
        "capital_flow_monitor_cap_test",
        os.path.join(ROOT, "skills", "stock-triage", "scripts", "capital_flow_monitor.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _targets(n_portfolio, n_monitor):
    return (
        [{"code": f"60{i:04d}", "name": f"持仓{i}", "source": "portfolio"}
         for i in range(n_portfolio)]
        + [{"code": f"00{i:04d}", "name": f"监控{i}", "source": "monitor"}
           for i in range(n_monitor)]
    )


def test_caps_at_max_stock_targets(monkeypatch):
    cfm = load_cfm()
    monkeypatch.setattr(cfm.runtime_targets, "load_stock_targets",
                        lambda: _targets(5, 166))  # 171 只，复刻生产机规模

    picked = cfm.load_runtime_stocks()

    assert len(picked) == cfm.MAX_STOCK_TARGETS == 20


def test_portfolio_holdings_come_first(monkeypatch):
    """持仓必须全部入选，不能被监控标的挤掉。"""
    cfm = load_cfm()
    monkeypatch.setattr(cfm.runtime_targets, "load_stock_targets",
                        lambda: _targets(3, 100))

    names = [name for _, _, name in cfm.load_runtime_stocks()]

    assert names[:3] == ["持仓0", "持仓1", "持仓2"]
    assert len(names) == 20


def test_no_truncation_when_targets_fit(monkeypatch):
    """标的数未超上限时不得截断，也不该报截断。"""
    cfm = load_cfm()
    monkeypatch.setattr(cfm.runtime_targets, "load_stock_targets",
                        lambda: _targets(2, 3))

    assert len(cfm.load_runtime_stocks()) == 5


def test_inputs_not_mutated(monkeypatch):
    """排序不得就地改动 runtime_targets 返回的列表。"""
    cfm = load_cfm()
    original = _targets(2, 3)
    snapshot = [dict(t) for t in original]
    monkeypatch.setattr(cfm.runtime_targets, "load_stock_targets", lambda: original)

    cfm.prioritized_stock_targets()

    assert original == snapshot


def test_report_surfaces_truncation():
    """截断必须出现在报告文本里——静默少扫等于假装全覆盖。"""
    cfm = load_cfm()
    data = {
        "timestamp": "2026-08-03T10:30:00",
        "northbound": {},
        "stocks": [],
        "sectors": [],
        "targets_total": 171,
        "targets_scanned": 20,
        "targets_truncated": 151,
    }

    report = cfm.format_report(data)

    assert "20/171" in report
    assert "151" in report


def test_report_silent_when_nothing_truncated():
    cfm = load_cfm()
    data = {
        "timestamp": "2026-08-03T10:30:00",
        "northbound": {},
        "stocks": [],
        "sectors": [],
        "targets_total": 8,
        "targets_scanned": 8,
        "targets_truncated": 0,
    }

    assert "未覆盖" not in cfm.format_report(data)
