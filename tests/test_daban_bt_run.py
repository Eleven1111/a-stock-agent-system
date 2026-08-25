"""打板回测 run 层 — 合成事件表单测（analyze 纯计算 + 闸门对接，不触网）"""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_run.py"
SPEC = importlib.util.spec_from_file_location("daban_bt_run", SCRIPT)
run = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run)


def _table():
    events = []
    # 12 个涨停事件，gap 与封板时间各异，日期跨 split
    for i in range(12):
        gap_open = 10.0 + (0.2 if i % 2 == 0 else 0.5)   # 偶数 gap=2(signal)，奇数 gap=5(out)
        seal = "092500" if i % 3 == 0 else "100000"      # 1/3 真竞价封
        date = "2026-04-10" if i < 6 else "2026-05-10"   # 前6 IS，后6 OOS
        events.append({
            "code": f"60020{i:01d}" if i < 10 else f"6002{i:02d}",
            "name": f"票{i}", "date": date,
            "t_close": 10.0, "t1_open": gap_open, "t1_close": 10.8,
            # v3 成交约束字段：T 日为盘中封板的 10cm 涨停（昨收 9.09 → 涨停 10.00），
            # 当日成交额 1e5 手 × 10 元 × 100 股 = 1 亿，回封换手充足 → 板上买得进。
            "t_prev_close": 9.09, "t_open": 9.3, "t_high": 10.0, "t_low": 9.2,
            "t_volume": 100000, "t_amount": None,
            "t1_high": max(gap_open, 10.8), "t1_low": min(gap_open, 10.8),
            "t1_volume": 100000, "t1_amount": None,
            "exit_date": "2026-04-13" if i < 6 else "2026-05-12",
            "exit_close": 11.0, "holding_sessions": 1,
            "first_seal": seal, "is_st": False,
        })
    return {
        "schema": "daban_bt_event_table_v3",
        "start": "20260401", "end": "20260531",
        "raw_count": 12, "event_count": 12, "dropped": {"no_kline": 0, "no_next_day": 0},
        "events": events,
        "control_pools": {"random_entry": [0.0, 0.01, -0.02, 0.03],
                          "simple_breakout": [0.01, -0.01, 0.02]},
    }


def test_analyze_gate_ready_for_oos():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    assert r["gate_result"]["decision"] == "ready_for_oos"
    assert r["gate_result"]["allowed_in_live_agent"] is False
    assert not r["gate_result"]["blocking_reasons"]


def test_analyze_sample_split_counts():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    assert r["sample"]["is_count"] == 6
    assert r["sample"]["oos_count"] == 6


def test_analyze_reports_both_hold_variants():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    assert set(r["exploratory"]["variants"]) == {
        "open_close", "board_overnight", "t1_open_next_sellable_close"
    }
    assert r["exploratory"]["primary_hold_mode"] == "t1_open_next_sellable_close"


def test_analyze_h1_signal_and_control_are_disjoint():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    h1 = r["exploratory"]["variants"]["board_overnight"]["h1"]
    assert h1["control"]["n"] == 6           # 窗口外涨停，不再包含 signal
    assert h1["signal"]["n"] == 6            # 偶数 gap=2 入窗
    assert "p_value" in h1["permutation"]


def test_board_overnight_includes_gap_open_close_does_not():
    # t_close=10, t1_close=10.8 → board_overnight 正收益；t1_open=10.5(奇)/10.2(偶)→ open_close 更低
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    bo = r["exploratory"]["variants"]["board_overnight"]["h1"]["control"]["mean"]
    oc = r["exploratory"]["variants"]["open_close"]["h1"]["control"]["mean"]
    assert bo > oc   # 含隔夜跳空的真打板收益高于切掉跳空的


def test_analyze_h2_groups_and_fdr_shape():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    h2 = r["exploratory"]["variants"]["board_overnight"]["h2"]
    assert h2["auction"]["n"] == 4           # i%3==0 → 0,3,6,9
    assert h2["intraday"]["n"] == 8
    assert len(r["exploratory"]["fdr"]) == 2  # 两假设


def test_research_state_has_required_controls_and_tests():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    rs = r["research_state"]
    assert set(rs["controls"]) == {"non_signal_limitups"}
    assert set(rs["stat_tests"]) == {"cluster_bootstrap", "paired_sign_flip"}
    assert rs["has_costs"] is True and rs["rules_locked"] is True


def test_analyze_default_stays_pre_oos():
    # 不开 OOS：保持 pre_oos / ready_for_oos（向后兼容，OOS 名额未消耗）
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    assert r["research_state"]["phase"] == "pre_oos"
    assert r["research_state"]["oos_run_count"] == 0
    assert r["gate_result"]["decision"] == "ready_for_oos"


def test_analyze_oos_validation_fills_state_and_advances_gate():
    r = run.analyze(_table(), split_date="20260501", n_perm=500, oos_validation=True)
    rs = r["research_state"]
    assert rs["phase"] == "oos_complete"
    assert rs["oos_run_count"] == 1                    # OOS 墙：一次性
    assert rs["oos_sample_count"] == 1                 # 同一交易日聚类成一个配对样本
    assert rs["oos_event_count"] == 3                  # OOS H1 signal: i=6,8,10
    assert "permutation_p" in rs and "oos_alpha" in rs
    assert rs["h2_status"] == "not_tested_no_first_seal"  # mootdx 缺 first_seal，H2 不验
    # 基线=窗口外涨停 control（非重叠样本），方向性透明字段在册
    assert "oos_signal_minus_control" in rs and "oos_index_benchmark" in rs
    assert rs["benchmark_alpha"] == pytest.approx(
        rs["oos_alpha"] - rs["oos_signal_minus_control"], abs=1e-6)
    assert rs["primary_hold_mode"] == "t1_open_next_sellable_close"
    assert rs["cluster_unit"] == "trading_date"
    # 纯计算结果未绑定落盘证据，必须阻断注册。
    assert r["gate_result"]["phase"] == "oos_complete"
    assert r["gate_result"]["decision"] == "blocked"
    assert any("evidence" in reason.lower() for reason in r["gate_result"]["blocking_reasons"])


def test_persisted_oos_artifact_advances_gate(tmp_path):
    table = _table()
    input_path = tmp_path / "events.json"
    input_path.write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    artifact_path = tmp_path / "daban-oos.json"
    result = run.analyze(table, split_date="20260501", n_perm=500, oos_validation=True)

    persisted = run.persist_evidence(
        result,
        event_table=table,
        input_path=str(input_path),
        artifact_path=str(artifact_path),
        split_date="20260501",
    )

    assert artifact_path.exists()
    assert persisted["gate_result"]["decision"] in {"passed_for_reference", "failed"}
    assert not persisted["gate_result"]["blocking_reasons"]
    assert persisted["research_state"]["evidence_artifact"] == str(artifact_path.resolve())


def test_persist_evidence_rejects_legacy_event_schema(tmp_path):
    table = _table()
    table["schema"] = "daban_bt_event_table_v1"
    input_path = tmp_path / "legacy.json"
    input_path.write_text(json.dumps(table), encoding="utf-8")
    result = run.analyze(table, split_date="20260501", n_perm=100, oos_validation=True)

    with pytest.raises(ValueError, match="daban_bt_event_table_v3"):
        run.persist_evidence(
            result,
            event_table=table,
            input_path=str(input_path),
            artifact_path=str(tmp_path / "artifact.json"),
            split_date="20260501",
        )


def test_stale_event_table_reports_degradation_not_a_silent_empty_sample():
    """v2 旧表跑 board_overnight 会被 fail-closed 判空样本。

    空样本必须显式说明是「数据过期」而不是「没有信号」——两者在 n=0 上不可区分，
    而探索路径（不带 --oos）没有 schema 闸门拦截。
    """
    table = _table()
    table["schema"] = "daban_bt_event_table_v2"
    for ev in table["events"]:          # v2 形状：无 T 日行情字段
        for field in ("t_open", "t_high", "t_low", "t_volume", "t_amount", "t_prev_close"):
            ev.pop(field, None)

    result = run.analyze(table, split_date="20260501")

    assert result["exploratory"]["variants"]["board_overnight"]["h1"]["signal"]["n"] == 0
    degraded = result["data_degradation"]
    assert degraded["stale_event_table"] is True
    assert degraded["required_schema"] == "daban_bt_event_table_v3"
    assert degraded["board_overnight_events_blocked"] == len(table["events"]) > 0
    assert "不是没有信号" in degraded["note"]
    assert "数据过期" in run.format_report(result)


def test_current_event_table_is_not_flagged_as_degraded():
    result = run.analyze(_table(), split_date="20260501")
    assert result["data_degradation"] == {
        "stale_event_table": False,
        "schema": "daban_bt_event_table_v3",
    }
    assert "数据过期" not in run.format_report(result)
