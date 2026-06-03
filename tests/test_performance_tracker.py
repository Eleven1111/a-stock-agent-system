"""打板口径胜率追踪 — 结算逻辑测试（纯函数，不触网）"""

import threading

import performance_tracker as pt
from performance_tracker import evaluate_signal, _expectancy, compute_stats


def bar(o, c, h, lo):
    return {"open": o, "close": c, "high": h, "low": lo}


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
        {"code": "1", "name": "a", "grade": "A", "strategy_id": "four_dim", "outcome": "win", "t1_close_ret": 4.0,
         "t1_open_premium": 2.0, "alpha_t1": 3.0, "promoted": False},
        {"code": "2", "name": "b", "grade": "A", "strategy_id": "daban:first_board_reseal", "outcome": "loss", "t1_close_ret": -3.0,
         "t1_open_premium": -1.0, "alpha_t1": -2.0, "promoted": False},
        {"code": "3", "name": "c", "grade": "S", "strategy_id": "daban:first_board_reseal", "outcome": "win_big", "t1_close_ret": 10.0,
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
    assert s["by_strategy"]["four_dim"]["closed"] == 1
    assert s["by_strategy"]["daban:first_board_reseal"]["closed"] == 2
    assert s["by_strategy"]["daban:first_board_reseal"]["win_rate"] == 50.0


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


# ========== 反馈闭环并发安全（这是系统唯一反馈闭环，丢记录 = 胜率不可信）==========

def test_record_signal_concurrent_no_loss(tmp_path, monkeypatch):
    """40 个并发 record_signal 不得丢记录（读-追加-写回必须在单锁内）。"""
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))

    n = 40
    threads = [
        threading.Thread(target=pt.record_signal,
                         args=(f"{600000 + i}", f"股票{i}", "A", 5.0, 10.0 + i))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = pt.load_history()
    assert len(history) == n, f"并发 record 丢记录: 期望 {n}, 实际 {len(history)}"
    assert len({r["code"] for r in history}) == n


def test_record_signal_persists_strategy_id(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))

    result = pt.record_signal("002156", "通富微电", "S", 9.7, 11.0, "daban:first_board_reseal")

    history = pt.load_history()
    assert result["ok"] is True
    assert history[0]["strategy_id"] == "daban:first_board_reseal"


def test_update_outcomes_preserves_concurrent_append(tmp_path, monkeypatch):
    """结算 pending 期间并发写入的新信号不得被陈旧快照覆盖丢失。

    复现 Codex 实测：update_outcomes 读快照→结算→写回，若期间有 record_signal 追加，
    旧实现会把快照原样写回，吞掉新追加的记录。修复后用 mutate_json 在单锁内重读最新历史。
    """
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "limit_pct", lambda code, name: 10.0)

    # 初始一条 pending（信号日 P1）
    pt.record_signal("600001", "老信号", "A", 5.0, 10.0)

    fetch_started = threading.Event()
    append_done = threading.Event()

    def fake_fetch(code, signal_date, market):
        if code == pt.BENCH_CODE:        # 跳过基准，alpha=None
            return None
        # 个股结算：先通知主线程"已进入结算窗口"，等并发追加落地后再返回，
        # 把"追加发生在 update_outcomes 的读快照之后、写回之前"的竞态固定下来。
        fetch_started.set()
        assert append_done.wait(timeout=5), "并发追加未在窗口内完成"
        return {"signal_close": 10.0,
                "future": [{"open": 10.5, "close": 11.0, "high": 11.0, "low": 10.4}]}

    monkeypatch.setattr(pt, "_fetch_future_bars", fake_fetch)

    result_holder = {}

    def run_update():
        result_holder["records"] = pt.update_outcomes()

    updater = threading.Thread(target=run_update)
    updater.start()

    assert fetch_started.wait(timeout=5), "update_outcomes 未进入结算窗口"
    # 在结算窗口内并发追加一条新信号 P2
    pt.record_signal("600002", "新信号", "B", 6.0, 20.0)
    append_done.set()

    updater.join(timeout=10)
    assert not updater.is_alive(), "update_outcomes 未结束"

    history = pt.load_history()
    codes = {r["code"] for r in history}
    assert codes == {"600001", "600002"}, f"并发追加被覆盖丢失: {codes}"

    p1 = next(r for r in history if r["code"] == "600001")
    p2 = next(r for r in history if r["code"] == "600002")
    assert p1["outcome"] != "pending", "P1 应被结算"
    assert p2["outcome"] == "pending", "P2 刚追加应仍为 pending"
