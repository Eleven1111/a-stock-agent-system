"""每日情绪数据集 sentiment_daily（升级方案 P0-a/P0-b）。

守三条最容易被静默绕过的性质：空集不产出数字、回填不到的字段显式 unavailable、
同一交易日重跑不在 120 日窗口里留下两行。
"""

import json

import pytest

import sentiment_daily as sd
from dataset_contract import DatasetContractError


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def quote(code, *, name="", prev_close=10.0, price=10.0, open_=None, high=None):
    return {
        "code": code,
        "name": name,
        "prev_close": prev_close,
        "price": price,
        "open": prev_close if open_ is None else open_,
        "high": price if high is None else high,
    }


LIMIT_UP_MAIN = 11.0     # 10.0 主板涨停
LIMIT_DOWN_MAIN = 9.0
LIMIT_UP_GEM = 12.0      # 10.0 创业板涨停


# --- 字段口径 -----------------------------------------------------------


def test_limit_metrics_split_sealed_touched_and_break_rate():
    """封板/触及分别统计，炸板率 = (触及−封住)/触及。"""
    rows = sd.normalize_rows([
        quote("600000", price=LIMIT_UP_MAIN, high=LIMIT_UP_MAIN),          # 封住
        quote("600001", price=10.2, high=LIMIT_UP_MAIN),                    # 触及后炸板
        quote("300001", price=LIMIT_UP_GEM, high=LIMIT_UP_GEM),             # 创业板 20% 封住
        quote("600002", price=LIMIT_DOWN_MAIN, high=9.6),                   # 跌停
    ])
    metrics = sd.compute_limit_metrics(rows)
    assert metrics["limit_count"] == 2
    assert metrics["touch_limit_count"] == 3
    assert metrics["limit_down_count"] == 1
    assert metrics["break_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_st_name_lowers_the_limit_threshold():
    """主板 ST 按 5% 判封板；同样的价格在非 ST 上不算涨停。"""
    st_row = sd.normalize_rows([quote("600000", name="ST某某", price=10.5, high=10.5)])
    plain_row = sd.normalize_rows([quote("600000", price=10.5, high=10.5)])
    assert sd.compute_limit_metrics(st_row)["limit_count"] == 1
    assert sd.compute_limit_metrics(plain_row)["limit_count"] == 0


def test_break_rate_is_unavailable_when_nothing_touched():
    """零触及是空集，不是 0% 炸板率——否则冰点日会读成"炸板率极低=情绪好"。"""
    rows = sd.normalize_rows([quote("600000", price=10.1, high=10.2)])
    metrics = sd.compute_limit_metrics(rows)
    assert metrics["touch_limit_count"] == 0
    assert metrics["break_rate"] is None


def test_touch_count_unavailable_when_high_missing():
    """没有 high 就无法判触及；"未知"不得折叠成"没触及"。"""
    rows = sd.normalize_rows([{"code": "600000", "prev_close": 10.0, "price": 10.5}])
    assert rows[0]["high"] is None
    assert sd.compute_limit_metrics(rows)["touch_limit_count"] is None


def test_breadth_and_adr():
    rows = sd.normalize_rows([
        quote("600000", price=10.5), quote("600001", price=10.2),
        quote("600002", price=9.5), quote("600003", price=10.0),
    ])
    metrics = sd.compute_breadth_metrics(rows)
    assert (metrics["advance_count"], metrics["decline_count"]) == (2, 1)
    assert metrics["adr"] == pytest.approx(2.0)


def test_adr_unavailable_without_decliners():
    rows = sd.normalize_rows([quote("600000", price=10.5)])
    assert sd.compute_breadth_metrics(rows)["adr"] is None


def test_premium_metrics_use_yesterday_sealed_cohort():
    rows = sd.normalize_rows([
        quote("600000", price=10.5, open_=10.2),
        quote("600001", price=9.8, open_=9.9),
        quote("600002", price=20.0, open_=20.0),   # 不在昨日梯队，不该进均值
    ])
    metrics = sd.compute_premium_metrics(rows, ["600000", "600001"])
    assert metrics["limit_premium_open"] == pytest.approx(0.5, abs=1e-4)
    assert metrics["limit_premium_close"] == pytest.approx(1.5, abs=1e-4)
    assert metrics["limit_red_ratio"] == pytest.approx(0.5)


def test_premium_metrics_unavailable_on_empty_cohort():
    """昨日无封板股 → 三项不可用；给 0.0 溢价会被 S_t 读成"溢价极差"。"""
    rows = sd.normalize_rows([quote("600000", price=10.5)])
    metrics = sd.compute_premium_metrics(rows, [])
    assert metrics == {"limit_premium_open": None, "limit_premium_close": None,
                       "limit_red_ratio": None}


def test_ladder_metrics_distinguish_missing_from_empty():
    """梯队缺失 → 不可用；梯队为空 → 真的 0 连板。"""
    assert sd.compute_ladder_metrics(None)["max_board"] is None
    assert sd.compute_ladder_metrics({})["max_board"] == 0
    metrics = sd.compute_ladder_metrics(
        {"600000": {"height": 5}, "600001": {"height": 4}, "600002": {"height": 2}}
    )
    assert (metrics["max_board"], metrics["board4plus"]) == (5, 2)


def test_leader_damage_intraday_drawdown_always_unavailable_on_daily_bars():
    """日内最大回撤需分钟线；日线路径必须留空而不是用 (close-high)/high 冒充。"""
    rows = sd.normalize_rows([quote("600000", price=9.0)])
    damage = sd.compute_leader_damage(rows, "600000")
    assert damage["leader_damage"] == pytest.approx(-10.0)
    assert damage["leader_damage_intraday_drawdown"] is None


# --- 空集 fail-closed ----------------------------------------------------


def test_empty_snapshot_reports_unavailable_not_zero():
    """空快照必须整体 unavailable，14 个字段全进 unavailable_fields。"""
    computed = sd.compute_sentiment_metrics({})
    assert computed["status"] == "unavailable"
    assert computed["universe_count"] == 0
    assert all(value is None for value in computed["metrics"].values())
    for field in sd.METRIC_FIELDS:
        assert field in computed["unavailable_fields"]


def test_non_empty_snapshot_reports_ok_with_named_gaps():
    """样本非空断言：有行情就是 ok，缺口只列该缺的字段。"""
    computed = sd.compute_sentiment_metrics(
        {"600000": quote("600000", price=LIMIT_UP_MAIN, high=LIMIT_UP_MAIN)},
        ladder={"600000": {"height": 1}},
    )
    assert computed["status"] == "ok"
    assert computed["universe_count"] == 1
    assert computed["metrics"]["limit_count"] == 1
    assert "sector_breadth_top" in computed["unavailable_fields"]
    assert "limit_count" not in computed["unavailable_fields"]


# --- 落盘：契约 + 幂等 ---------------------------------------------------


def _persist(trading_date, *, universe_expected=None, price=LIMIT_UP_MAIN):
    computed = sd.compute_sentiment_metrics(
        {"600000": quote("600000", price=price, high=price)},
        prev_limit_codes=["600000"],
        ladder={"600000": {"height": 2}},
        leader_code="600000",
        sector_breadth_top=3,
    )
    return sd.persist_metrics(
        computed, trading_date=trading_date, snapshot_ref=f"test:{trading_date}",
        source="unit_test", universe_expected=universe_expected,
    )


def test_persist_writes_daily_file_and_summary(state_home):
    outcome = _persist("2026-08-20")
    assert outcome["status"] == "ok"
    payload = json.loads(open(outcome["path"], encoding="utf-8").read())
    assert payload["schema"] == sd.SCHEMA
    assert payload["record"]["limit_count"] == 1
    rows = sd.load_summary()
    assert [row["trading_date"] for row in rows] == ["2026-08-20"]


def test_rerunning_one_day_replaces_its_row(state_home):
    """幂等：重跑同一交易日只能有一行，否则 120 日滚动窗口会重复计入该日。"""
    _persist("2026-08-20")
    _persist("2026-08-21")
    _persist("2026-08-20", price=10.5)
    rows = sd.load_summary()
    assert [row["trading_date"] for row in rows] == ["2026-08-20", "2026-08-21"]
    assert rows[0]["limit_count"] == 0          # 覆盖成重跑后的值，不是旧值
    assert sd.summarize()["trading_day_count"] == 2


def test_summary_is_sorted_by_trading_date(state_home):
    _persist("2026-08-21")
    _persist("2026-08-19")
    _persist("2026-08-20")
    assert [row["trading_date"] for row in sd.load_summary()] == [
        "2026-08-19", "2026-08-20", "2026-08-21"
    ]


def test_partial_universe_is_stamped_not_silently_full(state_home):
    """覆盖率低于契约下限 → coverage_status=partial，消费端才不会把半个市场
    的涨停家数当全市场口径。"""
    outcome = _persist("2026-08-20", universe_expected=100)
    assert outcome["coverage_status"] == "partial"
    full = _persist("2026-08-21", universe_expected=1)
    assert full["coverage_status"] == "full"


def test_unknown_coverage_when_universe_size_unknown(state_home):
    assert _persist("2026-08-20")["coverage_status"] == "unknown"


def test_record_matches_the_registered_contract(state_home):
    """契约即字段口径的唯一真相：多一个字段或类型不符都必须报错。"""
    computed = sd.compute_sentiment_metrics(
        {"600000": quote("600000", price=LIMIT_UP_MAIN, high=LIMIT_UP_MAIN)}
    )
    record = sd.build_record(computed, trading_date="2026-08-20",
                             snapshot_ref="test:1", source="unit_test")
    assert sd.validate_record(record)["status"] == "valid"
    with pytest.raises(DatasetContractError):
        sd.validate_record({**record, "unexpected_field": 1})
    with pytest.raises(DatasetContractError):
        sd.validate_record({**record, "limit_count": "many"})


def test_contract_violation_blocks_the_write(state_home):
    computed = sd.compute_sentiment_metrics({})
    record = sd.build_record(computed, trading_date="2026-08-20",
                             snapshot_ref="", source="unit_test")
    outcome = sd.persist_metrics(
        {**computed, "metrics": computed["metrics"]},
        trading_date="2026-08-20", snapshot_ref="", source="unit_test",
    )
    assert record["snapshot_ref"] == ""
    assert outcome["status"] == "blocked"
    assert outcome["reason"] == "contract_violation"
    assert sd.load_summary() == []


def test_produce_daily_record_blocks_without_frozen_inputs(state_home):
    """输入快照缺失 → blocked，而不是写一行全 null 的记录。"""
    outcome = sd.produce_daily_record("2026-08-20", {})
    assert outcome["status"] == "blocked"
    assert outcome["reason"] == "frozen_inputs_unavailable"
    assert sd.load_summary() == []


def test_sector_breadth_top_reads_the_top_ranked_sectors():
    state = {"sectors": [
        {"sector": "A", "rank": 1, "limitup_count": 4},
        {"sector": "B", "rank": 2, "limitup_count": 6},
        {"sector": "C", "rank": 9, "limitup_count": 11},
    ]}
    assert sd.sector_breadth_top(state, top_n=2) == 6
    assert sd.sector_breadth_top({}) is None
