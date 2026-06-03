"""打板口径胜率追踪 — 结算逻辑测试（纯函数，不触网）"""

from performance_tracker import evaluate_signal, _expectancy, compute_stats


def bar(o, c, h, l):
    return {"open": o, "close": c, "high": h, "low": l}


def test_evaluate_win_big_and_promoted():
    # 信号收盘 10，T+1 继续涨停到 11 → 隔日溢价/收益 +10%，晋级
    r = evaluate_signal(10.0, [bar(10.5, 11.0, 11.0, 10.4)], limit_pct_val=10.0)
    assert r["t1_open_premium"] == 5.0
    assert r["t1_close_ret"] == 10.0
    assert r["promoted"] is True
    assert r["outcome"] == "win_big"


def test_evaluate_not_promoted():
    # 隔日高开但未封板 → 不算晋级
    r = evaluate_signal(10.0, [bar(10.5, 10.5, 10.8, 10.3)], limit_pct_val=10.0)
    assert r["promoted"] is False
    assert r["outcome"] == "win_big"  # +5% 恰好 win_big 边界


def test_evaluate_loss_big():
    r = evaluate_signal(10.0, [bar(9.8, 9.3, 9.9, 9.2)], limit_pct_val=10.0)
    assert r["t1_close_ret"] == -7.0
    assert r["outcome"] == "loss_big"


def test_evaluate_symmetric_loss():
    r = evaluate_signal(10.0, [bar(10.0, 9.7, 10.1, 9.6)], limit_pct_val=10.0)
    assert r["outcome"] == "loss"   # -3% 落在 (-5, 0)


def test_evaluate_alpha_vs_benchmark():
    r = evaluate_signal(
        10.0, [bar(10.5, 11.0, 11.0, 10.4)], limit_pct_val=10.0,
        index_signal_close=4000.0, index_future_bars=[bar(4010, 4040, 4050, 4000)],
    )
    # 个股 +10%，指数 +1% → alpha +9%
    assert r["alpha_t1"] == 9.0


def test_evaluate_pending_when_no_future():
    assert evaluate_signal(10.0, [], limit_pct_val=10.0) is None


def test_evaluate_horizon_metrics():
    bars = [bar(10.5, 11.0, 11.0, 10.4), bar(11.0, 10.0, 11.2, 9.9), bar(10.0, 12.0, 12.5, 9.8)]
    r = evaluate_signal(10.0, bars, limit_pct_val=10.0)
    assert r["horizon_ret"] == 20.0       # T+3 收盘 12 → +20%
    assert r["max_gain"] == 25.0          # 最高 12.5 → +25%
    assert r["max_drawdown"] == -2.0      # 最低 9.8 → -2%
    assert r["bars_observed"] == 3


def test_expectancy_positive():
    e = _expectancy([10.0, -5.0, 3.0])
    # win_rate 2/3, avg_win 6.5, avg_loss 5 → 0.667*6.5 - 0.333*5 ≈ 2.67
    assert e["expectancy"] == 2.67
    assert e["payoff_ratio"] == 1.3


def test_expectancy_empty():
    assert _expectancy([])["expectancy"] == 0.0


def test_compute_stats_aggregates():
    records = [
        {"code": "1", "name": "a", "grade": "A", "outcome": "win", "t1_close_ret": 4.0,
         "t1_open_premium": 2.0, "alpha_t1": 3.0, "promoted": False},
        {"code": "2", "name": "b", "grade": "A", "outcome": "loss", "t1_close_ret": -3.0,
         "t1_open_premium": -1.0, "alpha_t1": -2.0, "promoted": False},
        {"code": "3", "name": "c", "grade": "S", "outcome": "win_big", "t1_close_ret": 10.0,
         "t1_open_premium": 8.0, "alpha_t1": 9.0, "promoted": True},
        {"code": "4", "name": "d", "grade": "S", "outcome": "pending"},
    ]
    s = compute_stats(records)
    assert s["closed"] == 3
    assert s["pending"] == 1
    assert s["win_rate"] == round(2 / 3 * 100, 1)
    assert s["promote_rate"] == round(1 / 3 * 100, 1)
    assert s["by_grade"]["S"]["win_rate"] == 100.0
    assert s["by_grade"]["A"]["closed"] == 2


def test_compute_stats_no_closed():
    s = compute_stats([{"code": "1", "outcome": "pending"}])
    assert s["closed"] == 0
    assert s["pending"] == 1


def test_compute_stats_excludes_legacy_records():
    # 旧 schema 记录（有 outcome 但无 t1_close_ret）不得混入新口径统计
    records = [
        {"code": "1", "grade": "A", "outcome": "win", "t1_close_ret": 4.0,
         "t1_open_premium": 2.0, "promoted": False},                       # 新口径
        {"code": "2", "grade": "S", "outcome": "win_big", "pnl_pct": 12.0},  # 旧口径(污染源)
    ]
    s = compute_stats(records)
    assert s["closed"] == 1               # 只算新口径
    assert s["legacy_excluded"] == 1
    assert s["win_rate"] == 100.0         # 不被旧记录污染
    assert s["by_grade"]["S"]["closed"] == 0


def test_compute_stats_all_legacy():
    records = [{"code": "1", "grade": "A", "outcome": "win_big", "pnl_pct": 12.0}]
    s = compute_stats(records)
    assert s["closed"] == 0
    assert s["legacy_excluded"] == 1
