"""板块动量与轮动 — issue #89（医药板块爆发零信号）的信号分级/轮动/加成测试。"""

import sector_momentum as sm
import signal_context as sc


def _row(name, return_1d=0.0, return_5d=0.0, net_1d=0.0, net_5d=0.0, **extra):
    row = {
        "code": "BK0001",
        "name": name,
        "return_1d": return_1d,
        "return_5d": return_5d,
        "turnover_pct": 2.0,
        "net_inflow_1d": net_1d,
        "net_inflow_5d": net_5d,
        "net_inflow_prior_4d": round(net_5d - net_1d, 2),
        "up_count": 10,
        "down_count": 5,
    }
    row.update(extra)
    return row


# ========== 东财 clist 行解析 ==========

def test_parse_board_rows_normalizes_units_and_skips_invalid():
    diff = [
        {"f12": "BK1325", "f14": "半导体材料", "f3": 9.69, "f8": 8.97,
         "f62": 2_937_654_272.0, "f104": 31, "f105": 0,
         "f109": 0.29, "f164": -10_764_738_048.0},
        {"f12": "BK9999", "f14": "", "f3": 1.0},          # 无名称
        {"f12": "BK9998", "f14": "停牌板块", "f3": "-"},   # 无当日涨幅
    ]
    rows = sm.parse_board_rows(diff)
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "半导体材料"
    assert row["net_inflow_1d"] == 29.38          # 元 → 亿
    assert row["net_inflow_5d"] == -107.65
    assert row["net_inflow_prior_4d"] == -137.03  # 5日 - 当日


# ========== 信号分级（issue #89 规则） ==========

def test_strong_requires_5d_over_10_and_beats_index():
    verdict = sm.classify_sector_signal(
        _row("医药生物", return_1d=1.0, return_5d=12.18), index_return_5d=3.5,
    )
    assert verdict["signal"] == "strong"
    assert verdict["vs_index_5d"] == 8.68


def test_strong_not_triggered_when_barely_beats_index():
    verdict = sm.classify_sector_signal(
        _row("银行", return_1d=1.0, return_5d=11.0), index_return_5d=8.0,
    )
    assert verdict["signal"] == "neutral"


def test_emerging_catches_day1_breakout_with_inflow():
    verdict = sm.classify_sector_signal(
        _row("创新药", return_1d=4.2, return_5d=5.0, net_1d=15.0, net_5d=20.0),
        index_return_5d=1.0,
    )
    assert verdict["signal"] == "emerging"


def test_emerging_requires_positive_inflow():
    verdict = sm.classify_sector_signal(
        _row("创新药", return_1d=4.2, return_5d=5.0, net_1d=-3.0, net_5d=1.0),
        index_return_5d=1.0,
    )
    assert verdict["signal"] == "neutral"


def test_weakening_flags_pullback_after_big_run():
    verdict = sm.classify_sector_signal(
        _row("贵金属", return_1d=-2.5, return_5d=13.0), index_return_5d=2.0,
    )
    assert verdict["signal"] == "weakening"


def test_rotating_out_needs_sustained_outflow_and_decline():
    verdict = sm.classify_sector_signal(
        _row("消费电子", return_1d=-1.5, return_5d=-4.0, net_1d=-8.0, net_5d=-30.0),
        index_return_5d=2.0,
    )
    assert verdict["signal"] == "rotating_out"


def test_rotating_out_ignores_routine_small_outflow():
    # A股板块主力常态小额净流出，宽阈值会命中上百板块（实测170/496）全是噪声
    verdict = sm.classify_sector_signal(
        _row("港口", return_1d=-0.6, return_5d=0.3, net_1d=-0.4, net_5d=-2.7),
        index_return_5d=0.2,
    )
    assert verdict["signal"] == "neutral"


def test_missing_index_baseline_degrades_to_no_strong():
    verdict = sm.classify_sector_signal(
        _row("医药生物", return_1d=1.0, return_5d=12.18), index_return_5d=None,
    )
    assert verdict["signal"] == "neutral"
    assert verdict["vs_index_5d"] is None


# ========== 载荷构建 ==========

def test_build_momentum_keeps_all_signaled_and_counts():
    rows = [
        _row("医药生物", return_1d=1.0, return_5d=12.18, net_1d=5, net_5d=28),
        _row("创新药", return_1d=4.0, return_5d=6.0, net_1d=10, net_5d=12),
        _row("消费电子", return_1d=-1.5, return_5d=-4.0, net_1d=-8, net_5d=-30),
    ] + [_row(f"中性板块{i}", return_1d=0.1, return_5d=0.5) for i in range(40)]
    payload = sm.build_sector_momentum(
        rows, index_return_5d=3.5, trading_date="2026-07-08",
        sector_limitups={"医药生物": 4},
    )
    assert payload["schema"] == "sector_momentum_v1"
    assert payload["total_sectors"] == 43
    assert len(payload["sectors"]) == sm.MAX_SECTORS_IN_CONTEXT
    assert payload["signal_counts"] == {
        "strong": 1, "emerging": 1, "weakening": 0, "rotating_out": 1,
    }
    top = payload["sectors"][0]
    assert top["name"] == "医药生物" and top["signal"] == "strong"
    assert top["limitup_count"] == 4


# ========== 轮动检测 ==========

def test_rotation_detects_rank_shift_between_today_and_prior4d():
    # 医药：前4日净流出、今日大幅流入 → 流入方向；消费电子反向 → 流出方向。
    # 噪声板块 5日净额=当日×5，两期排名一致（位移0），不干扰目标板块。
    rows = [
        _row("医药生物", net_1d=20.0, net_5d=15.0),
        _row("消费电子", net_1d=-15.0, net_5d=30.0),
    ] + [_row(f"板块{i}", net_1d=round(i * 0.1 - 1, 1),
              net_5d=round((i * 0.1 - 1) * 5, 1))
         for i in range(20)]
    rotation = sm.detect_sector_rotation(rows, trading_date="2026-07-08")
    assert rotation["status"] == "ok"
    assert "医药生物" in rotation["inflow_sectors"]
    assert "消费电子" in rotation["outflow_sectors"]
    assert "医药生物" in rotation["rotation_signal"]


def test_rotation_insufficient_data():
    rotation = sm.detect_sector_rotation(
        [_row("医药生物", net_1d=1.0, net_5d=2.0)], trading_date="2026-07-08",
    )
    assert rotation["status"] == "insufficient_data"
    assert rotation["inflow_sectors"] == []


# ========== 指数基准 ==========

def test_index_return_from_klines():
    klines = [
        "2026-07-02,4028.90", "2026-07-03,4043.64", "2026-07-06,4041.24",
        "2026-07-07,3990.24", "2026-07-08,3970.88", "2026-07-09,4036.59",
    ]
    assert sm.index_return_from_klines(klines) == 0.19


def test_index_return_needs_six_closes():
    assert sm.index_return_from_klines(["2026-07-08,4000", "2026-07-09,4100"]) is None


# ========== 情绪面消费（signal_context.sentiment_boost 集成） ==========

def _momentum_ctx(signal):
    return {
        "sector_momentum": {
            "schema": "sector_momentum_v1",
            "sectors": [{"name": "医药生物", "signal": signal,
                         "signal_reason": "5日涨幅12.2%且强于大盘8.7pp"}],
        },
    }


def test_momentum_boost_strong_adds_one_point():
    out = sm.momentum_boost("医药生物", _momentum_ctx("strong")["sector_momentum"])
    assert out["delta"] == 1.0
    assert "板块主升" in out["note"]


def test_momentum_boost_unlisted_sector_is_zero():
    out = sm.momentum_boost("银行", _momentum_ctx("strong")["sector_momentum"])
    assert out["delta"] == 0.0 and out["note"] is None


def test_sentiment_boost_folds_in_sector_momentum():
    ctx = {"sector_limitups": {"医药生物": 3}, **_momentum_ctx("strong")}
    out = sc.sentiment_boost("600276", ctx, sector="医药生物")
    # 0.5(板块涨停≥3) + 1.0(板块动量strong)
    assert out["delta"] == 1.5
    assert any("板块主升" in n for n in out["notes"])


def test_sentiment_boost_penalizes_rotating_out():
    out = sc.sentiment_boost("002475", _momentum_ctx("rotating_out"),
                             sector="医药生物")
    assert out["delta"] == -0.5
