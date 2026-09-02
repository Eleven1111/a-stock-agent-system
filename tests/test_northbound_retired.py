"""北向日频净额已停发 —— 采集端必须 fail-closed，且不得被南向顶替（F008）。

这些断言守的是一条静默替换：``stock_hsgt_fund_flow_summary_em`` 同时返回北向和
南向四行，北向两行的净额自 2024 年披露调整后恒为 0，而南向两行带着真实的大额
``资金净流入``。按行位置取值（原实现的 ``reversed(rows)``）会拿到南向数字并贴上
北向标签，一路进到个股情绪扣分与退出信号。

正向对照是本文件的关键：只断言「返回空」的守卫，用 ``return {}`` 就能全绿，等于
把「永远不给数」伪装成「守得住」。所以每条 unavailable 断言都配一条同结构的
非零北向输入，要求解析器照常给出数值。
"""

from __future__ import annotations

import market_adapters as ma


def _summary_rows(sh_net: float, sz_net: float, south_inflow: float = 420.0):
    """一天的 HSGT 汇总四行：北向两行 + 南向两行，字段名与上游一致。"""
    return [
        {
            "交易日": "2026-09-02",
            "类型": "沪港通",
            "板块": "沪股通",
            "资金方向": "北向",
            "成交净买额": sh_net,
            "资金净流入": 0.0,
        },
        {
            "交易日": "2026-09-02",
            "类型": "沪港通",
            "板块": "港股通(沪)",
            "资金方向": "南向",
            "成交净买额": 54.06,
            "资金净流入": south_inflow,
        },
        {
            "交易日": "2026-09-02",
            "类型": "深港通",
            "板块": "深股通",
            "资金方向": "北向",
            "成交净买额": sz_net,
            "资金净流入": 0.0,
        },
        {
            "交易日": "2026-09-02",
            "类型": "深港通",
            "板块": "港股通(深)",
            "资金方向": "南向",
            "成交净买额": -14.65,
            "资金净流入": south_inflow,
        },
    ]


def test_all_zero_northbound_rows_report_unavailable():
    """2026-09-02 实测形态：北向两行全 0 —— 这是停发占位，不是「平盘」。"""
    assert ma.northbound_net_from_rows(_summary_rows(0.0, 0.0)) == {}


def test_southbound_inflow_never_becomes_northbound():
    """南向 420 亿在场时仍必须判不可用；退回按行取值会让这条变红。"""
    rows = _summary_rows(0.0, 0.0, south_inflow=420.0)
    assert ma.northbound_net_from_rows(rows) == {}

    big_south = ma.northbound_net_from_rows(_summary_rows(0.0, 0.0, south_inflow=-999.0))
    assert big_south == {}


def test_real_northbound_rows_are_still_parsed():
    """正向对照：口径若恢复，两条北向净买额求和照常给数。"""
    parsed = ma.northbound_net_from_rows(_summary_rows(12.5, -3.4))
    assert parsed == {"date": "2026-09-02", "net_flow_yi": 9.1}


def test_northbound_rows_in_yuan_are_normalised_to_yi():
    parsed = ma.northbound_net_from_rows(
        [
            {
                "交易日": "2026-09-02",
                "板块": "沪股通",
                "资金方向": "北向",
                "成交净买额": 1_250_000_000.0,
            }
        ]
    )
    assert parsed == {"date": "2026-09-02", "net_flow_yi": 12.5}


def test_kamt_payload_is_read_per_leg_not_from_a_missing_klines_key():
    """kamt.kline 的响应按 hk2sh/hk2sz 分腿，根本没有 ``klines`` 键。"""
    payload = {
        "data": {
            "hk2sh": ["2026-09-02,0.00,5200000.00,273757367.45"],
            "hk2sz": ["2026-09-02,0.00,5200000.00,251431468.29"],
            "sh2hk": ["2026-09-02,4200000.00,0.00,2278115453.44"],
            "sz2hk": ["2026-09-02,4200000.00,0.00,2278115453.44"],
        }
    }
    assert ma.kamt_northbound_net(payload) == {}

    live = {
        "data": {
            "hk2sh": ["2026-09-02,80000.00,5200000.00,273757367.45"],
            "hk2sz": ["2026-09-02,45000.00,5200000.00,251431468.29"],
            "sh2hk": ["2026-09-02,4200000.00,0.00,2278115453.44"],
        }
    }
    assert ma.kamt_northbound_net(live) == {"date": "2026-09-02", "net_flow_yi": 12.5}


def test_fetch_falls_closed_and_never_replays_csv_history(monkeypatch, tmp_path):
    """采集失败时不得用历史 CSV 复活 —— 那些行里存的正是被误标的南向值。"""
    monkeypatch.setattr(ma, "_cache_get", lambda *_a, **_k: None)
    monkeypatch.setattr(ma, "_cache_set", lambda *_a, **_k: None)
    monkeypatch.setattr(ma, "_northbound_csv_path", lambda: str(tmp_path / "northbound_history.csv"))

    def _all_providers_empty(_endpoint, _providers, empty):
        return empty

    monkeypatch.setattr(ma, "_fallback_chain", _all_providers_empty)

    with open(tmp_path / "northbound_history.csv", "w", encoding="utf-8") as handle:
        handle.write("date,net_flow_yi,provider,recorded_at\n")
        handle.write("2026-09-01,420.0,akshare,2026-09-01T15:30:00\n")

    assert ma.fetch_northbound_flow() == {}


def test_northbound_history_replay_helper_is_gone():
    """复活路径必须是删掉的，而不是留着等人再调用一次。"""
    assert not hasattr(ma, "_load_northbound_history")
