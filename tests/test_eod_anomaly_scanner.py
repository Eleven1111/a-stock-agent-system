"""尾盘异动扫描器 — 纯函数测试（不触网）"""

import eod_anomaly_scanner as eas


def _spot_row(code="600001", name="正常票", price=10.0, amount=2e8, market_cap=6e9, pe=20.0):
    return {
        "代码": code, "名称": name, "最新价": price, "成交额": amount,
        "总市值": market_cap, "市盈率-动态": pe,
    }


# ======================== screen_universe ========================

def test_screen_universe_keeps_row_passing_all_filters():
    rows = eas.screen_universe([_spot_row()])
    assert len(rows) == 1
    assert rows[0]["code"] == "600001"


def test_screen_universe_excludes_st_and_delisting_risk_names():
    rows = eas.screen_universe([_spot_row(name="ST正常"), _spot_row(name="退市股份")])
    assert rows == []


def test_screen_universe_excludes_small_cap_and_thin_liquidity():
    small_cap = eas.screen_universe([_spot_row(market_cap=1e9)])
    thin_amount = eas.screen_universe([_spot_row(amount=5e7)])
    assert small_cap == []
    assert thin_amount == []


def test_screen_universe_excludes_pe_out_of_range():
    negative_pe = eas.screen_universe([_spot_row(pe=-5.0)])
    extreme_pe = eas.screen_universe([_spot_row(pe=150.0)])
    missing_pe = eas.screen_universe([_spot_row(pe=None)])
    assert negative_pe == []
    assert extreme_pe == []
    assert missing_pe == []


def test_screen_universe_allows_source_level_absence_of_optional_fields():
    row = _spot_row()
    row.pop("总市值")
    row.pop("市盈率-动态")

    rows = eas.screen_universe([row])

    assert len(rows) == 1
    assert rows[0]["market_cap"] is None
    assert rows[0]["pe"] is None


def test_screen_universe_skips_malformed_rows_without_crashing():
    assert eas.screen_universe([{"代码": "600001", "名称": "坏数据", "最新价": "N/A"}]) == []


# ======================== compute_tail_anomaly ========================

def _minute_rows(baseline_volume, tail_volume_total, before_price, close_price, last_time="1500"):
    return [
        {"time": "0930", "price": before_price, "cum_volume": 0.0},
        {"time": "1429", "price": before_price, "cum_volume": baseline_volume},
        {"time": last_time, "price": close_price, "cum_volume": baseline_volume + tail_volume_total},
    ]


def test_compute_tail_anomaly_computes_ratio_and_change():
    # baseline=700000 手 -> 每30分钟均量=100000；尾盘450000手 -> 量比4.5x
    rows = _minute_rows(baseline_volume=700000, tail_volume_total=450000, before_price=10.0, close_price=10.3)
    signal = eas.compute_tail_anomaly(rows)
    assert signal["tail_volume_ratio"] == 4.5
    assert signal["tail_price_change_pct"] == 3.0
    assert signal["close_price"] == 10.3


def test_compute_tail_anomaly_returns_none_when_day_incomplete():
    # 盘中运行：最后一条时间早于15:00，数据不完整
    rows = [{"time": "0930", "price": 10.0, "cum_volume": 0.0}, {"time": "1345", "price": 10.1, "cum_volume": 5000.0}]
    assert eas.compute_tail_anomaly(rows) is None


def test_compute_tail_anomaly_returns_none_without_pre_tail_data():
    rows = [{"time": "1500", "price": 10.0, "cum_volume": 1000.0}]
    assert eas.compute_tail_anomaly(rows) is None


def test_compute_tail_anomaly_returns_none_for_empty_input():
    assert eas.compute_tail_anomaly([]) is None


# ======================== covers_market_close / close_coverage_sufficient ========================

def test_covers_market_close_tracks_whether_data_reached_the_close():
    assert eas.covers_market_close([{"time": "1500", "price": 10.0, "cum_volume": 1.0}]) is True
    assert eas.covers_market_close([{"time": "1459", "price": 10.0, "cum_volume": 1.0}]) is False
    assert eas.covers_market_close([]) is False
    assert eas.covers_market_close([{"price": 10.0}]) is False


def test_close_coverage_sufficient_rejects_a_run_that_never_reached_the_close():
    """盘中运行：抓到了全市场数据，但没有一只到收盘 —— 整轮无效。"""
    assert eas.close_coverage_sufficient({"fetched": 3000, "closed": 0}) is False


def test_close_coverage_sufficient_rejects_an_empty_fetch():
    assert eas.close_coverage_sufficient({"fetched": 0, "closed": 0}) is False


def test_close_coverage_sufficient_accepts_a_normal_post_close_run():
    # 收盘后个别停牌股拿不到完整分钟数据是正常的，不该拖垮整轮扫描
    assert eas.close_coverage_sufficient({"fetched": 3000, "closed": 2950}) is True


# ======================== is_tail_anomaly ========================

def test_is_tail_anomaly_requires_both_thresholds():
    assert eas.is_tail_anomaly({"tail_volume_ratio": 2.5, "tail_price_change_pct": 1.5}) is True
    assert eas.is_tail_anomaly({"tail_volume_ratio": 2.4, "tail_price_change_pct": 1.5}) is False
    assert eas.is_tail_anomaly({"tail_volume_ratio": 2.5, "tail_price_change_pct": 1.4}) is False
    assert eas.is_tail_anomaly(None) is False


# ======================== compute_position_60d_pct ========================

def test_compute_position_60d_pct_at_range_midpoint():
    bars = [{"high": 12.0, "low": 8.0, "close": 8.0}, {"high": 11.0, "low": 9.0, "close": 10.0}]
    assert eas.compute_position_60d_pct(bars) == 50.0


def test_compute_position_60d_pct_none_when_insufficient_bars():
    assert eas.compute_position_60d_pct([{"high": 12.0, "low": 8.0, "close": 10.0}]) is None


def test_compute_position_60d_pct_none_when_flat_range():
    bars = [{"high": 10.0, "low": 10.0, "close": 10.0}] * 3
    assert eas.compute_position_60d_pct(bars) is None


# ======================== rank_anomalies ========================

def test_rank_anomalies_sorts_by_strength_descending_and_assigns_rank():
    ranked = eas.rank_anomalies([
        {"code": "A", "anomaly_strength": 5.0},
        {"code": "B", "anomaly_strength": 12.0},
        {"code": "C", "anomaly_strength": 8.0},
    ])
    assert [item["code"] for item in ranked] == ["B", "C", "A"]
    assert [item["rank"] for item in ranked] == [1, 2, 3]


# ======================== classify_gap ========================

def test_classify_gap_buckets():
    assert eas.classify_gap(2.5) == "强烈高开"
    assert eas.classify_gap(2.0) == "强烈高开"
    assert eas.classify_gap(1.5) == "高开"
    assert eas.classify_gap(1.0) == "高开"
    assert eas.classify_gap(0.0) == "平开"
    assert eas.classify_gap(-0.5) == "平开"
    assert eas.classify_gap(-1.0) == "小幅低开"
    assert eas.classify_gap(-2.5) == "小幅低开"
    assert eas.classify_gap(-3.5) == "低开"


# ======================== build_confirmations ========================

def test_build_confirmations_computes_gap_and_sorts_high_open_first():
    candidates = [
        {"code": "600001", "name": "A票"},
        {"code": "600002", "name": "B票"},
    ]
    quotes = {
        "sh600001": {"open": 10.1, "prev_close": 10.0},
        "sh600002": {"open": 11.5, "prev_close": 10.0},
    }
    rows = eas.build_confirmations(candidates, quotes)
    assert [row["code"] for row in rows] == ["600002", "600001"]
    assert rows[0]["gap_bucket"] == "强烈高开"
    assert rows[1]["gap_pct"] == 1.0


def test_build_confirmations_flags_missing_quote_without_crashing():
    rows = eas.build_confirmations([{"code": "600001", "name": "无行情"}], {})
    assert rows[0]["status"] == "quote_unavailable"


# ======================== example_scan / CLI 集成 ========================

def test_example_scan_produces_valid_schema_without_network():
    result = eas.example_scan()
    assert result["schema"] == eas.SCHEMA
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["code"] == "688213"


def test_format_scan_report_and_confirm_report_do_not_crash_on_empty():
    empty_scan = {"asof": "2026-06-30", "universe_count": 0, "candidates": []}
    empty_confirm = {"asof": "2026-07-01", "source_asof": "2026-06-30", "confirmations": []}
    assert "无标的" in eas.format_scan_report(empty_scan)
    assert "无待确认" in eas.format_confirm_report(empty_confirm)


def test_format_scan_report_marks_an_invalid_scan_instead_of_reporting_zero_hits():
    """无效扫描的报告不能和"今天没有异动"长得一样。"""
    invalid = {
        "asof": "2026-07-28",
        "status": "insufficient_data",
        "error": "分钟数据未覆盖收盘(1500)：0/3000 只达到收盘",
        "universe_count": 3000,
        "candidates": [],
    }
    report = eas.format_scan_report(invalid)
    assert "扫描无效" in report
    assert "无标的触发尾盘异动阈值" not in report


# ======================== scan() 收盘闸门（不触网） ========================

def _patch_scan_io(monkeypatch, minute_rows):
    """把 scan() 的三个网络出口换成合成数据，只保留闸门逻辑。"""
    spot = [_spot_row(code="600001"), _spot_row(code="600002")]
    monkeypatch.setattr(eas, "_fetch_universe_with_retry", lambda: spot)
    monkeypatch.setattr(
        eas, "fetch_tencent_minute", lambda code, market=None: minute_rows
    )
    monkeypatch.setattr(eas, "_fetch_60d_positions", lambda codes: {code: 30.0 for code in codes})


def test_scan_fails_closed_when_run_before_the_market_closes(monkeypatch):
    """Issue #135 的 7/28 场景：14:54 跑，全市场分钟数据都停在收盘前。

    此前这里会静默返回 candidates=[]，和真实的零命中无法区分。
    """
    _patch_scan_io(monkeypatch, [
        {"time": "0930", "price": 10.0, "cum_volume": 0.0},
        {"time": "1454", "price": 10.5, "cum_volume": 700000.0},
    ])

    result = eas.scan("2026-07-28")

    assert result["status"] == "insufficient_data"
    assert result["candidates"] == []
    assert result["minute_closed_count"] == 0
    assert result["minute_fetched_count"] == 2
    assert "未覆盖收盘" in result["error"]


def test_scan_reports_a_genuine_zero_hit_day_as_ok(monkeypatch):
    """7/27、7/29 场景：收盘后跑，确实没有标的达标 —— 这才是可信的零命中。"""
    _patch_scan_io(monkeypatch, [
        {"time": "0930", "price": 10.0, "cum_volume": 0.0},
        {"time": "1429", "price": 10.0, "cum_volume": 700000.0},
        {"time": "1500", "price": 10.01, "cum_volume": 720000.0},
    ])

    result = eas.scan("2026-07-29")

    assert result["status"] == "ok"
    assert result["candidates"] == []
    assert result["minute_closed_count"] == 2
    assert "error" not in result
