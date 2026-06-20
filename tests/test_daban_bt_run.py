"""打板回测 run 层 — 合成事件表单测（analyze 纯计算 + 闸门对接，不触网）"""

import importlib.util
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
            "first_seal": seal, "is_st": False,
        })
    return {
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
    assert set(r["exploratory"]["variants"]) == {"open_close", "board_overnight"}
    assert r["exploratory"]["primary_hold_mode"] == "board_overnight"


def test_analyze_h1_signal_subset_of_control():
    r = run.analyze(_table(), split_date="20260501", n_perm=500)
    h1 = r["exploratory"]["variants"]["board_overnight"]["h1"]
    assert h1["control"]["n"] == 12          # 全部主板涨停
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
    assert set(rs["controls"]) == {"random_entry", "simple_breakout", "buy_hold"}
    assert set(rs["stat_tests"]) == {"t_test", "bootstrap", "permutation"}
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
    assert rs["oos_sample_count"] == 3                 # OOS H1 signal: i=6,8,10
    assert "permutation_p" in rs and "oos_alpha" in rs
    assert rs["h2_status"] == "not_tested_no_first_seal"  # mootdx 缺 first_seal，H2 不验
    # 基线=买全部涨停 control（非沪深300），方向性透明字段在册
    assert "oos_signal_minus_control" in rs and "oos_index_benchmark" in rs
    assert rs["benchmark_alpha"] == pytest.approx(  # benchmark 即 board_overnight control 均值
        rs["oos_alpha"] - rs["oos_signal_minus_control"])
    # 已进入 OOS 完成态：闸门按统计如实判 passed/failed，不再 ready_for_oos
    assert r["gate_result"]["phase"] == "oos_complete"
    assert r["gate_result"]["decision"] in {"passed_for_reference", "failed"}
    assert not r["gate_result"]["blocking_reasons"]   # 6 检查项全过
