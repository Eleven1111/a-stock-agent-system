"""打板回测数据层 — 纯函数单测（kline_lookup / assemble_events，不触网）"""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_data.py"
SPEC = importlib.util.spec_from_file_location("daban_bt_data", SCRIPT)
dat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dat)


def _kline():
    return [
        {"date": "2026-06-02", "open": 9.5, "close": 9.8},
        {"date": "2026-06-03", "open": 9.9, "close": 10.0},   # T
        {"date": "2026-06-04", "open": 10.2, "close": 10.6},  # T+1
    ]


def test_market_prefix():
    assert dat.market_prefix("600255") == "sh"
    assert dat.market_prefix("002156") == "sz"
    assert dat.market_prefix("255") == "sz"  # zfill 后 000255


def test_norm_date():
    assert dat._norm_date("20260603") == "2026-06-03"
    assert dat._norm_date("2026-06-03") == "2026-06-03"


def test_kline_lookup_returns_t_and_next():
    looked = dat.kline_lookup(_kline(), "20260603")
    assert looked == (10.0, 10.2, 10.6)   # t_close, t1_open, t1_close


def test_kline_lookup_last_bar_has_no_next():
    assert dat.kline_lookup(_kline(), "2026-06-04") is None


def test_kline_lookup_missing_date():
    assert dat.kline_lookup(_kline(), "2026-05-01") is None


def test_assemble_events_joins_and_counts_drops():
    raw = [
        {"code": "600255", "name": "鑫科材料", "date": "20260603", "first_seal": "092500",
         "lianban": 2, "seal_amount": 3.8e8, "float_mktcap": 7.9e9, "所属行业": "金属",
         "sector": "金属新材", "is_st": False},
        {"code": "600256", "name": "无K线票", "date": "20260603", "first_seal": "100000"},
        {"code": "600255", "name": "鑫科材料", "date": "20260604", "first_seal": "092500"},  # 末日无次日
    ]
    kline_by_code = {"600255": _kline()}
    events, dropped = dat.assemble_events(raw, kline_by_code)
    assert len(events) == 1
    e = events[0]
    assert e["t_close"] == 10.0 and e["t1_open"] == 10.2 and e["t1_close"] == 10.6
    assert e["first_seal"] == "092500" and e["sector"] == "金属新材"
    assert dropped["no_kline"] == 1      # 600256 无 K 线
    assert dropped["no_next_day"] == 1   # 600255@06-04 末日无次日


def test_assess_coverage_flags_degraded_sample():
    # 请求 2 年(~520 交易日)，实际只 2 天 → 必须告警
    raw = [{"date": "20260601"}, {"date": "20260602"}]
    cov = dat.assess_coverage(raw, "20240603", "20260602")
    assert cov["covered_trading_days"] == 2
    assert cov["coverage_ratio"] < 0.1
    assert cov["warning"] is not None and "覆盖严重不足" in cov["warning"]


def test_assess_coverage_full_sample_no_warning():
    # 构造覆盖率 >=0.8 的样本（请求约 7 交易日，给 8 个不同日期）
    raw = [{"date": f"2026-06-{d:02d}"} for d in range(1, 11)]
    cov = dat.assess_coverage(raw, "20260601", "20260610")
    assert cov["warning"] is None


def test_map_zt_row_marks_st():
    row = {"代码": "600256", "名称": "ST康美", "首次封板时间": "093000",
           "连板数": 1, "封板资金": 1.0e8, "流通市值": 5.0e9, "所属行业": "医药"}
    ev = dat._map_zt_row(row, "20260603")
    assert ev["is_st"] is True
    assert ev["code"] == "600256" and ev["sector"] == "医药"
