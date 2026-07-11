"""打板口径胜率追踪 — 结算逻辑测试（纯函数，不触网）"""

import json
import threading

import performance_tracker as pt
import signal_ledger
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
    assert r["settlement_status"] == "provisional"
    assert r["resolved"] is False


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
    assert r["settlement_status"] == "final"
    assert r["resolved"] is True


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


def test_strategy_gating_uses_only_final_settlements():
    records = [
        {
            "code": "1",
            "name": "a",
            "grade": "A",
            "strategy_id": "daban:first_board_reseal",
            "outcome": "loss",
            "t1_close_ret": -8.0,
            "t1_open_premium": -5.0,
            "promoted": False,
            "settlement_status": "provisional",
        },
        {
            "code": "2",
            "name": "b",
            "grade": "A",
            "strategy_id": "daban:first_board_reseal",
            "outcome": "win",
            "t1_close_ret": 3.0,
            "t1_open_premium": 2.0,
            "promoted": False,
            "settlement_status": "final",
        },
    ]

    stats = compute_stats(records)

    assert stats["by_strategy"]["daban:first_board_reseal"]["closed"] == 2
    assert stats["gating_by_strategy"]["daban:first_board_reseal"]["closed"] == 1
    assert stats["gating_by_strategy"]["daban:first_board_reseal"]["expectancy"] == 3.0


def test_compute_stats_reports_directional_research_attribution():
    records = [
        {
            "code": "1",
            "name": "a",
            "grade": "A",
            "strategy_id": "trend_pullback",
            "outcome": "win",
            "t1_close_ret": 4.0,
            "t1_open_premium": 2.0,
            "promoted": False,
            "settlement_status": "final",
            "strategy_attributions": [
                {
                    "strategy_id": "chanlun_third_buy",
                    "direction": "bullish",
                },
                {
                    "strategy_id": "chanlun_top_divergence",
                    "direction": "bearish",
                },
            ],
        }
    ]

    stats = compute_stats(records)

    assert stats["by_attribution_strategy"]["chanlun_third_buy"]["expectancy"] == 4.0
    assert stats["by_attribution_strategy"]["chanlun_top_divergence"]["expectancy"] == -4.0
    assert stats["gating_by_attribution_strategy"]["chanlun_third_buy"]["closed"] == 1


def test_compute_stats_aggregates_by_evidence_source_and_inactive_pipeline():
    records = [
        {
            "code": "1",
            "name": "a",
            "grade": "A",
            "strategy_id": "trend_pullback",
            "outcome": "win",
            "t1_close_ret": 4.0,
            "t1_open_premium": 2.0,
            "alpha_t1": 2.0,
            "promoted": False,
            "settlement_status": "final",
            "signal_date": "2026-06-20",
            "evidence_sources": [
                {
                    "source": "open-confirmation",
                    "artifact": {"snapshot_id": "snap-open"},
                    "weight_hint": "primary",
                },
                {
                    "source": "auction-finalize",
                    "artifact": {"path": "auction_shortlist_2026-06-20.json"},
                    "weight_hint": "supporting",
                },
            ],
        },
        {
            "code": "2",
            "name": "b",
            "grade": "A",
            "strategy_id": "trend_pullback",
            "outcome": "loss",
            "t1_close_ret": -2.0,
            "t1_open_premium": -1.0,
            "alpha_t1": -1.0,
            "promoted": False,
            "settlement_status": "final",
            "signal_date": "2026-06-21",
            "evidence_sources": [
                {
                    "source": "open-confirmation",
                    "artifact": {"snapshot_id": "snap-open-2"},
                    "weight_hint": "primary",
                }
            ],
        },
        {
            "code": "3",
            "name": "c",
            "grade": "B",
            "strategy_id": "trend_pullback",
            "outcome": "win",
            "t1_close_ret": 1.0,
            "t1_open_premium": 0.5,
            "alpha_t1": 0.3,
            "promoted": False,
            "settlement_status": "final",
            "signal_date": "2026-05-20",
            "evidence_sources": [
                {
                    "source": "candidate-discovery",
                    "artifact": "candidate_pool_latest.json",
                    "weight_hint": "primary",
                }
            ],
        },
    ]

    stats = compute_stats(
        records,
        asof="2026-07-02",
        known_evidence_pipelines={
            "open-confirmation",
            "auction-finalize",
            "candidate-discovery",
        },
    )

    by_source = stats["by_evidence_source"]
    assert by_source["open-confirmation"]["primary_recommendations"] == 2
    assert by_source["open-confirmation"]["t3_hit_rate"] == 50.0
    assert by_source["open-confirmation"]["avg_excess_return"] == 0.5
    assert by_source["auction-finalize"]["primary_recommendations"] == 0
    assert by_source["auction-finalize"]["t3_hit_rate"] == 100.0
    assert stats["inactive_evidence_pipelines_30d"] == ["candidate-discovery"]

    rendered = pt.format_stats(stats, records)
    assert "证据来源归因" in rendered
    assert "candidate-discovery" in rendered


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
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))

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
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))

    result = pt.record_signal("002156", "通富微电", "S", 9.7, 11.0, "daban:first_board_reseal")

    history = pt.load_history()
    assert result["ok"] is True
    assert history[0]["strategy_id"] == "daban:first_board_reseal"
    assert history[0]["signal_id"]
    assert signal_ledger.project_signals(ledger_file=pt.LEDGER_FILE)[0]["outcome"] == "pending"


def test_update_outcomes_preserves_concurrent_append(tmp_path, monkeypatch):
    """结算 pending 期间并发写入的新信号不得被陈旧快照覆盖丢失。

    复现 Codex 实测：update_outcomes 读快照→结算→写回，若期间有 record_signal 追加，
    旧实现会把快照原样写回，吞掉新追加的记录。修复后用 mutate_json 在单锁内重读最新历史。
    """
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
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
    canonical = signal_ledger.project_signals(ledger_file=pt.LEDGER_FILE)
    settled = next(record for record in canonical if record["code"] == "600001")
    assert settled["outcome"] == "win_big"
    assert settled["settlement_id"]
    assert settled["settlement_status"] == "provisional"


def test_update_outcomes_upgrades_t1_to_t3_final(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(pt, "limit_pct", lambda code, name: 10.0)
    pt.record_signal(
        "600001",
        "阶段结算",
        "A",
        5.0,
        10.0,
        signal_date="2026-06-10",
    )
    one_bar = {
        "signal_close": 10.0,
        "future": [bar(10.5, 11.0, 11.0, 10.4)],
    }
    three_bars = {
        "signal_close": 10.0,
        "future": [
            bar(10.5, 11.0, 11.0, 10.4),
            bar(11.0, 10.0, 11.2, 9.9),
            bar(10.0, 12.0, 12.5, 9.8),
        ],
    }
    current = {"value": one_bar}

    def fake_fetch(code, signal_date, market):
        return None if code == pt.BENCH_CODE else current["value"]

    monkeypatch.setattr(pt, "_fetch_future_bars", fake_fetch)

    provisional = pt.update_outcomes()
    assert provisional[0]["settlement_status"] == "provisional"
    assert provisional[0]["resolved"] is False

    current["value"] = three_bars
    final = pt.update_outcomes()
    assert final[0]["settlement_status"] == "final"
    assert final[0]["resolved"] is True
    assert final[0]["horizon_ret"] == 20.0

    events = signal_ledger.read_events(pt.LEDGER_FILE)
    assert [event["event_type"] for event in events].count("signal.t1_settled") == 1
    assert [event["event_type"] for event in events].count("signal.t3_settled") == 1


def test_update_outcomes_uses_observable_signal_price_not_signal_day_close(
    tmp_path,
    monkeypatch,
):
    """09:35 信号只能按当时记录价结算，不能偷看当日收盘价。"""
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(pt, "limit_pct", lambda code, name: 10.0)
    pt.record_signal(
        "600001",
        "盘中信号",
        "A",
        5.0,
        9.5,
        signal_date="2026-06-10",
    )

    def fake_fetch(code, signal_date, market):
        if code == pt.BENCH_CODE:
            return None
        return {
            # 这是 2026-06-10 收盘价，在 09:35 信号发生时尚不可观察。
            "signal_close": 10.0,
            "future": [bar(10.5, 11.0, 11.0, 10.4)],
        }

    monkeypatch.setattr(pt, "_fetch_future_bars", fake_fetch)

    settled = pt.update_outcomes()[0]

    assert settled["t1_close_ret"] == 15.79
    assert settled["settlement_entry_price"] == 9.5
    assert settled["settlement_entry_price_source"] == "signal_price"
    # 晋级仍按交易所 T+1 涨停基准（信号日收盘 10.0）判定，而非盘中入场价。
    assert settled["promoted"] is True


def test_update_outcomes_terminalizes_signal_without_observable_entry_price(
    tmp_path,
    monkeypatch,
):
    """没有事前可观察价格时应 fail closed，不能回退到信号日收盘。"""
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    monkeypatch.setattr(pt, "limit_pct", lambda code, name: 10.0)
    links = signal_ledger.make_links("rec-without-entry")
    signal_ledger.append_events(
        [
            signal_ledger.signal_opened_event(
                {
                    "code": "600001",
                    "name": "无价格信号",
                    "date": "2026-06-10",
                    "grade": "A",
                    "strategy_id": "trend_pullback",
                    "action": "buy",
                },
                links,
            )
        ],
        ledger_file=pt.LEDGER_FILE,
    )

    def fake_fetch(code, signal_date, market):
        if code == pt.BENCH_CODE:
            return None
        return {
            "signal_close": 10.0,
            "future": [bar(10.5, 11.0, 11.0, 10.4)],
        }

    monkeypatch.setattr(pt, "_fetch_future_bars", fake_fetch)

    unresolved = pt.update_outcomes()[0]

    assert unresolved["outcome"] == "unresolved"
    assert unresolved["settlement_status"] == "terminal_unresolved"
    assert unresolved["settlement_observation_status"] == "entry_price_missing"
    assert any(
        event["event_type"] == "signal.settled"
        for event in signal_ledger.read_events(pt.LEDGER_FILE)
    )


def test_update_outcomes_terminalizes_aged_market_data_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    pt.record_signal(
        "600001", "长期无行情", "A", 5.0, 10.0, signal_date="2020-01-01"
    )
    monkeypatch.setattr(pt, "_fetch_future_bars", lambda *args: None)

    unresolved = pt.update_outcomes()[0]

    assert unresolved["settlement_status"] == "terminal_unresolved"
    assert unresolved["settlement_observation_status"] == (
        "market_data_unavailable_or_tradeability_unknown"
    )


def test_settlement_coverage_blocks_strategy_gate():
    records = [
        {
            "code": "1",
            "strategy_id": "trend_pullback",
            "outcome": "win",
            "t1_close_ret": 2.0,
            "t1_open_premium": 1.0,
            "promoted": False,
            "settlement_status": "final",
        },
        {"code": "2", "strategy_id": "trend_pullback", "outcome": "pending"},
    ]
    stats = compute_stats(records)
    assert stats["settlement_coverage"]["status"] == "coverage_insufficient"
    decision = pt.evaluate_strategy_gating(
        {"trend_pullback": {"closed": 20, "expectancy": 1.0}},
        coverage_sufficient=False,
    )[0]
    assert decision["action"] == "skip"
    assert decision["reason"] == "coverage_insufficient"


def test_terminal_ambiguity_cannot_enable_survivor_only_strategy():
    records = [
        {
            "code": str(index),
            "strategy_id": "survivor",
            "outcome": "win",
            "t1_close_ret": 2.0,
            "t1_open_premium": 1.0,
            "promoted": False,
            "settlement_status": "final",
        }
        for index in range(12)
    ] + [
        {
            "code": f"u{index}",
            "strategy_id": "survivor",
            "outcome": "unresolved",
            "settlement_status": "terminal_unresolved",
            "settlement_observation_status": (
                "market_data_unavailable_or_tradeability_unknown"
            ),
        }
        for index in range(100)
    ]
    stats = compute_stats(records)
    coverage = stats["settlement_coverage"]
    assert coverage["ratio"] == 1.0
    assert coverage["gating_reason"] == "terminal_ambiguity"

    decision = pt.evaluate_strategy_gating(
        stats["gating_by_strategy"],
        coverage_sufficient=coverage["gating_status"] == "sufficient",
        coverage_reason=coverage["gating_reason"],
    )[0]
    assert decision["action"] == "skip"
    assert decision["reason"] == "terminal_ambiguity"


def test_observable_entry_price_rejects_malformed_values_and_supports_legacy_key():
    assert pt._observable_entry_price({
        "signal_price": float("nan"),
        "entry_price": float("inf"),
        "reference_price": -1,
        "recommendation_price": "10.25",
    }) == (10.25, "recommendation_price")
    assert pt._observable_entry_price({
        "signal_price": 0,
        "entry_price": True,
        "reference_price": "not-a-price",
    }) is None


def test_gate_stats_include_recommendation_ledger_signals(tmp_path, monkeypatch):
    monkeypatch.setattr(pt, "HISTORY_FILE", str(tmp_path / "signal_history.json"))
    monkeypatch.setattr(pt, "LEDGER_FILE", str(tmp_path / "signal_ledger.jsonl"))
    links = signal_ledger.make_links("rec-auto")
    opened = signal_ledger.signal_opened_event(
        {
            "code": "002156",
            "name": "通富微电",
            "date": "2026-06-10",
            "entry_price": 10.0,
            "grade": "A",
            "strategy_id": "trend_pullback",
            "action": "buy",
        },
        links,
    )
    signal_ledger.append_events(
        [
            opened,
            signal_ledger.settlement_event(
                {**opened["payload"], **links},
                {"outcome": "win", "t1_close_ret": 4.0, "t1_open_premium": 2.0, "promoted": False},
            ),
        ],
        ledger_file=pt.LEDGER_FILE,
    )

    stats = pt.compute_stats(pt.load_history())

    assert stats["by_strategy"]["trend_pullback"]["closed"] == 1
    assert stats["by_strategy"]["trend_pullback"]["expectancy"] == 4.0


def test_attach_push_report_adds_weekly_output_section(tmp_path, monkeypatch):
    telemetry = tmp_path / "state" / "cron" / "push_telemetry.jsonl"
    telemetry.parent.mkdir(parents=True)
    telemetry.write_text(
        "\n".join([
            json.dumps({
                "job_id": "alpha",
                "trading_date": "2026-06-10",
                "delivered": True,
                "output_chars": 120,
                "was_compressed": False,
                "silent_reason": "none",
            }),
            json.dumps({
                "job_id": "alpha",
                "trading_date": "2026-06-11",
                "delivered": False,
                "output_chars": 0,
                "was_compressed": False,
                "silent_reason": "local",
            }),
        ]),
        encoding="utf-8",
    )
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))

    stats = pt.attach_push_report({"closed": 0})

    assert stats["push_report"]["jobs"]["alpha"]["daily_avg_chars"] == 60.0
    formatted = pt.format_push_report(stats["push_report"])
    assert "推送与token计量" in formatted
    assert "alpha" in formatted
