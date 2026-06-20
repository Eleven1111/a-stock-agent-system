"""mootdx 深历史数据源 — 纯函数单测 + reconstruct（fake client，不触网）。

涨停判定阈值经真实数据校准：探针对 40 只主板个股 ×400 日线命中 240 次，
实际涨幅全部落在 10.0%-10.11%，公式 round(prev*(1+pct), 2) 与交易所口径一致。
"""

import importlib.util
from pathlib import Path

import pandas as pd

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "mootdx_source.py"
SPEC = importlib.util.spec_from_file_location("mootdx_source", SCRIPT)
ms = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ms)


def _df(rows):
    """构造 mootdx 风格 bars DataFrame（datetime/open/close/high/low/vol）。"""
    return pd.DataFrame([
        {"datetime": f"{d} 15:00", "open": o, "close": c,
         "high": max(o, c), "low": min(o, c), "vol": 1000.0}
        for d, o, c in rows
    ])


class _FakeClient:
    """单页 mootdx client 替身：start>0 即视为翻到尽头。"""

    def __init__(self, bars_by_code):
        self._bars = bars_by_code
        self.bar_calls = 0

    def bars(self, symbol, frequency, offset, start):
        self.bar_calls += 1
        if start > 0:
            return None
        return self._bars.get(str(symbol))

    def stocks(self, market):  # pragma: no cover - reconstruct 用 universe 注入
        return None


# --------------------------------------------------------------------------- #
# 纯函数
# --------------------------------------------------------------------------- #
def test_is_a_stock_filters_non_equity():
    for code in ("600519", "601318", "603019", "688981", "000001", "002594", "300750"):
        assert ms.is_a_stock(code), code
    for code in ("999999", "395001", "150018", "110030", "510300"):
        assert not ms.is_a_stock(code), code


def test_limit_ratio_by_board():
    assert ms.limit_ratio("600519") == 0.10
    assert ms.limit_ratio("300750") == 0.20
    assert ms.limit_ratio("688981") == 0.20
    assert ms.limit_ratio("830799") == 0.30
    assert ms.limit_ratio("600519", "ST长生") == 0.05      # ST 优先级最高
    assert ms.limit_ratio("300750", "*ST天龙") == 0.05


def test_limit_cap_rounds_to_cent():
    assert ms.limit_cap(10.00, "600000") == 11.00
    assert ms.limit_cap(13.69, "002006") == 15.06         # 13.69*1.1=15.059 → 15.06
    assert ms.limit_cap(20.00, "300001") == 24.00         # 创业板 20%
    assert ms.limit_cap(10.00, "600000", "ST股") == 10.50  # ST 5%


def test_to_kline_aligns_tencent_schema():
    kl = ms.to_kline(_df([("2024-06-03", 9.9, 11.0)]))
    assert kl == [{"date": "2024-06-03", "open": 9.9, "close": 11.0,
                   "high": 11.0, "low": 9.9, "volume": 1000.0}]
    assert ms.to_kline(None) == []
    assert ms.to_kline(pd.DataFrame()) == []


def test_detect_limitups_mainboard_streak_and_break():
    kline = [
        {"date": "2024-06-02", "close": 10.0},   # 基准（prev）
        {"date": "2024-06-03", "close": 11.0},   # 涨停 lianban=1
        {"date": "2024-06-04", "close": 12.1},   # 涨停 lianban=2
        {"date": "2024-06-05", "close": 12.0},   # 断板
        {"date": "2024-06-06", "close": 13.2},   # 12.0→13.2 涨停 lianban=1
    ]
    hits = ms.detect_limitups(kline, "002001")
    assert [(h["date"], h["lianban"]) for h in hits] == [
        ("2024-06-03", 1), ("2024-06-04", 2), ("2024-06-06", 1)]


def test_detect_limitups_chinext_20pct():
    # 创业板需涨 20%：10→12 命中；10→11.5（涨 15%）不到线
    assert len(ms.detect_limitups(
        [{"date": "d0", "close": 10.0}, {"date": "d1", "close": 12.0}], "300001")) == 1
    assert len(ms.detect_limitups(
        [{"date": "d0", "close": 10.0}, {"date": "d1", "close": 11.5}], "300001")) == 0


def test_detect_limitups_st_5pct():
    kline = [{"date": "d0", "close": 10.0},
             {"date": "d1", "close": 10.5},    # ST 涨停
             {"date": "d2", "close": 10.9}]    # 10.5*1.05=11.025，10.9<11.025 不算
    hits = ms.detect_limitups(kline, "600000", "ST股")
    assert [(h["date"], h["lianban"]) for h in hits] == [("d1", 1)]


def test_detect_limitups_guards_zero_prev():
    kline = [{"date": "d0", "close": 0.0}, {"date": "d1", "close": 5.0}]
    assert ms.detect_limitups(kline, "600000") == []


def test_standardize_event_matches_map_zt_row_shape():
    ev = ms.standardize_event("2001", "测试股", {"date": "2024-06-03", "lianban": 2})
    assert ev == {
        "code": "002001", "name": "测试股", "date": "2024-06-03",
        "first_seal": None, "lianban": 2, "seal_amount": None,
        "float_mktcap": None, "sector": None, "is_st": False,
    }
    assert ms.standardize_event("600000", "*ST退", {"date": "d", "lianban": 1})["is_st"]


# --------------------------------------------------------------------------- #
# reconstruct（fake client）
# --------------------------------------------------------------------------- #
def test_fetch_daily_stops_when_start_covered():
    client = _FakeClient({"600000": _df([("2024-06-02", 10, 10), ("2024-06-03", 10, 11)])})
    kl = ms.fetch_daily("600000", "2024-06-02", client=client, max_pages=4)
    assert [b["date"] for b in kl] == ["2024-06-02", "2024-06-03"]
    assert client.bar_calls == 1   # 首页已覆盖 start_date，不再翻页


def test_reconstruct_filters_window_and_standardizes():
    bars = _df([
        ("2024-05-28", 10.0, 10.0),
        ("2024-05-29", 10.0, 11.0),   # 涨停但在区间外
        ("2024-06-03", 11.0, 12.1),   # 区间内 涨停 lianban 累加
        ("2024-06-04", 12.1, 13.31),  # 区间内 涨停
        ("2024-06-05", 13.31, 13.0),  # 断板
    ])
    client = _FakeClient({"002001": bars})
    events = ms.reconstruct_limitup_events(
        "2024-06-01", "2024-06-30",
        client=client, universe=[{"code": "002001", "name": "测试股"}])
    dates = [e["date"] for e in events]
    assert dates == ["2024-06-03", "2024-06-04"]   # 05-29 被区间过滤
    assert all(e["first_seal"] is None and e["sector"] is None for e in events)
    assert events[0]["code"] == "002001"
