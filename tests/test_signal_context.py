"""情绪上下文 — 合并写入 / sentiment_boost 口径 / 过期回退。"""

from datetime import datetime, timedelta

import signal_context as sc


def test_update_merges_not_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # capital_flow 先写资金流
    sc.update_signal_context({"northbound_net_yi": -40.0,
                              "stock_flows": {"002156": {"main_net_yi": 1.5}}})
    # hot-money 后写涨停池——不得覆盖资金流
    sc.update_signal_context({"sector_limitups": {"半导体": 5},
                              "lianban_ladder": {"002156": {"lianban": 2, "sector": "半导体"}}})
    ctx = sc.read_signal_context()
    assert ctx["northbound_net_yi"] == -40.0
    assert ctx["sector_limitups"]["半导体"] == 5
    assert ctx["stock_flows"]["002156"]["main_net_yi"] == 1.5


def test_read_expired_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sc.update_signal_context({"sector_limitups": {"半导体": 3}})
    future = datetime.now() + timedelta(hours=30)
    assert sc.read_signal_context(max_age_hours=24, now=future) is None


def test_boost_no_ctx_is_zero():
    out = sc.sentiment_boost("002156", None)
    assert out["delta"] == 0.0 and out["notes"] == []


def test_boost_lianban_ladder_full_stack():
    ctx = {
        "lianban_ladder": {"002156": {"lianban": 3, "sector": "半导体",
                                      "seal_yi": 2.0, "first_seal": "09:32"}},
        "sector_limitups": {"半导体": 6},
        "stock_flows": {"002156": {"main_net_yi": 2.0}},
    }
    out = sc.sentiment_boost("002156", ctx)
    # 1.5(连板) + 0.5(封板资金) + 0.5(早盘封) + 1.0(板块≥5) + 0.5(主力流入)
    assert out["delta"] == 4.0
    assert out["sector"] == "半导体"
    assert any("连板梯队" in n for n in out["notes"])
    assert any("赚钱效应" in n for n in out["notes"])


def test_boost_first_board_and_cluster3():
    ctx = {"lianban_ladder": {"600584": {"lianban": 1, "sector": "封测"}},
           "sector_limitups": {"封测": 3}}
    out = sc.sentiment_boost("600584", ctx)
    assert out["delta"] == 1.3  # 0.8(首板) + 0.5(板块≥3)


def test_boost_negative_flows():
    ctx = {"northbound_net_yi": -50.0,
           "stock_flows": {"600011": {"main_net_yi": -2.0}}}
    out = sc.sentiment_boost("600011", ctx)
    assert out["delta"] == -1.0  # -0.5(主力流出) -0.5(北向流出)


def test_boost_sector_passed_externally():
    ctx = {"sector_limitups": {"半导体": 5}}
    out = sc.sentiment_boost("999999", ctx, sector="半导体")
    assert out["delta"] == 1.0
