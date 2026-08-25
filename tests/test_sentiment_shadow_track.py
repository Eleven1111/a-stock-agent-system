"""S_t 的 shadow 接线与双轨对照落盘（升级方案 P0-c/P0-d）。

两个断言目标是**消费端行为**，不是 JSON 里有没有那个 key：
1. selection_context 的下游投影（进 cron 产物与账本的 compact 视图）真的读到了
   S_t；
2. S_t 变化不改变 market_gate / daban_ready / 板块排序 —— shadow 就得是 shadow。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

import hot_money_selection as hms
import sentiment_daily as sd


ROOT = Path(__file__).resolve().parents[1]


def _shadow_script():
    """scripts/cycle_state_shadow.py 不是包内模块，按路径加载（与其他 cron
    脚本测试同一套装载方式）；脚本自身的 _repo_bootstrap 需要 scripts/ 在
    sys.path 上。"""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "cycle_state_shadow", ROOT / "scripts" / "cycle_state_shadow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _quotes(limit_ups=3, total=800):
    """构造一份可用的全市场快照：涨停若干，其余平盘。"""
    rows = []
    for index in range(total):
        code = f"60{index:04d}"
        limit_up = index < limit_ups
        rows.append({
            "code": code, "name": "", "prev_close": 10.0,
            "price": 11.0 if limit_up else 10.0,
            "open": 10.0, "high": 11.0 if limit_up else 10.0,
            "change_pct": 10.0 if limit_up else 0.0,
            "amount": 1e8,
        })
    return rows


def _selection_state(shadow):
    return {
        "status": "ready",
        "sectors": [{"sector": "半导体", "rank": 1, "qualified_for_daban": True,
                     "limitup_count": 4}],
        "market_timing": {"status": "ready", "daban_ready": True,
                          "temperature": {"tier": "发酵"}},
        "sentiment_shadow": shadow,
    }


SHADOW = {"schema": "sentiment_shadow_v1", "status": "ok", "score": 63.5,
          "band": "加速", "delta": 12.5, "calibrated": False, "shadow_only": True}


# --- 消费端行为：compact 投影真的读到了 S_t -----------------------------


def test_compact_projection_reads_the_shadow_score():
    context = hms.selection_context_for(
        {"code": "600001", "sector": "半导体", "hot_money_qualified": True},
        _selection_state(SHADOW),
        window="D0_close",
    )
    compact = hms.compact_selection_context(context)
    assert compact["sentiment_score"] == 63.5
    assert compact["sentiment_band"] == "加速"
    assert compact["sentiment_delta"] == 12.5
    assert compact["sentiment_status"] == "ok"


def test_compact_projection_distinguishes_unavailable_from_zero():
    """S_t 不可用时投影出 None + status，而不是 0 分——0 分是"极度冰点"。"""
    shadow = {"status": "unavailable", "reason": "insufficient_history",
              "score": None, "band": None, "delta": None}
    context = hms.selection_context_for(
        {"code": "600001", "sector": "半导体"}, _selection_state(shadow),
        window="D0_close",
    )
    compact = hms.compact_selection_context(context)
    assert compact["sentiment_score"] is None
    assert compact["sentiment_status"] == "unavailable"


def test_advance_selection_context_preserves_the_shadow_block():
    """D0 → D1 传递不得把影子归因丢掉，否则次日账本无从回溯当时的 S_t。"""
    context = hms.selection_context_for(
        {"code": "600001", "sector": "半导体"}, _selection_state(SHADOW),
        window="D0_close",
    )
    advanced = hms.advance_selection_context(
        {"code": "600001", "selection_context": context}, window="D1_open"
    )
    assert advanced["sentiment_shadow"]["score"] == 63.5


# --- shadow 不得影响任何实盘判定 ----------------------------------------


def test_shadow_score_does_not_move_market_gate_or_ranking():
    """同一份行情下把 S_t 从"极热"换成"冰点"，门禁/排序必须逐字节不变。"""
    quotes = _quotes()
    context = {"lianban_ladder": {"600000": {"height": 2}},
               "prev_lianban_ladder": {"600000": {"height": 1}},
               "ladder_asof": "2026-08-20"}
    timing = hms.build_market_timing(quotes, context, event_asof="2026-08-20")
    hot = dict(timing)
    cold = dict(timing)
    hot["sentiment_shadow"] = {"status": "ok", "score": 99.0}
    cold["sentiment_shadow"] = {"status": "ok", "score": 1.0}
    assert hot["market_gate"] == cold["market_gate"]
    assert hot["daban_ready"] == cold["daban_ready"]
    leadership_hot = hms.build_sector_leadership(quotes, context, hot)
    leadership_cold = hms.build_sector_leadership(quotes, context, cold)
    assert (json.dumps(leadership_hot.get("sectors"), sort_keys=True, default=str)
            == json.dumps(leadership_cold.get("sectors"), sort_keys=True, default=str))


def test_market_timing_never_emits_a_sentiment_field():
    """S_t 的唯一入口是 selection_state；timing 自己算不出也不该带这个字段。"""
    timing = hms.build_market_timing(
        _quotes(), {"lianban_ladder": {}, "ladder_asof": "2026-08-20"},
        event_asof="2026-08-20",
    )
    assert not [key for key in timing if key.startswith("sentiment")]


# --- shadow 状态本身：序列不足即 unavailable -----------------------------


def test_sentiment_shadow_state_is_unavailable_without_series(state_home):
    shadow = hms.sentiment_shadow_state("2026-08-20")
    assert shadow["status"] == "unavailable"
    assert shadow["score"] is None
    assert shadow["calibrated"] is False
    assert shadow["series_days"] == 0


def test_sentiment_shadow_state_ignores_future_rows(state_home):
    """asof 之后的行不得进入当日 S_t —— 那是前视偏差。"""
    for day in ("2026-08-19", "2026-08-20", "2026-08-21"):
        computed = sd.compute_sentiment_metrics(
            {"600000": {"code": "600000", "prev_close": 10.0, "price": 11.0,
                        "high": 11.0, "open": 10.5}},
            prev_limit_codes=["600000"], ladder={"600000": {"height": 1}},
            leader_code="600000", sector_breadth_top=3,
        )
        sd.persist_metrics(computed, trading_date=day, snapshot_ref=f"t:{day}",
                           source="unit_test")
    assert hms.sentiment_shadow_state("2026-08-20")["series_days"] == 2
    assert hms.sentiment_shadow_state("2026-08-21")["series_days"] == 3


# --- 双轨对照行 ---------------------------------------------------------


def test_dual_track_row_records_all_three_readings(state_home):
    shadow_script = _shadow_script()
    track = shadow_script.build_sentiment_track(
        {"market_timing": {"temperature": {"tier": "冰点"}},
         "market_state": {"dominant_state": "S0", "dominant_label": "冰点"},
         "sectors": [{"sector": "半导体", "rank": 1, "limitup_count": 5}]},
        "2026-08-20",
    )
    assert track["temperature_tier"] == "冰点"
    assert track["market_state"] == "S0"
    assert track["sentiment_status"] == "unavailable"     # 序列为空，fail-closed
    assert track["sentiment_score"] is None
    assert track["calibrated"] is False
    assert track["ice_confirm"]["confirmed"] is False
    assert track["dataset"]["status"] == "blocked"        # 无固化输入 → 不写空行


def test_dual_track_row_lands_in_the_shadow_log(state_home):
    shadow_script = _shadow_script()
    row = shadow_script.run(skip_index=True, skip_dataset=True)
    assert row["sentiment_track"]["schema"] == "sentiment_dual_track_v1"
    assert row["sentiment_track"]["dataset"]["status"] == "skipped"
    log = state_home / "skills" / "stock-triage" / "data" / "cycle_shadow_log.jsonl"
    persisted = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert persisted[-1]["sentiment_track"]["shadow_only"] is True
