"""板块强度盘中时序落盘（宿主机建议第3条）。

重点守三条易被静默绕过的性质：同槽幂等、缺口不插值、
"作业没跑"与"跑了但数据降级"必须可区分。
"""

from datetime import datetime

import pytest

import sector_series as ss


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _frame(average_pct, *, member_count=6, positive_ratio=0.8, alerted=True, scope=None):
    frame = {
        "average_pct": average_pct,
        "positive_ratio": positive_ratio,
        "member_count": member_count,
        "alerted": alerted,
    }
    if scope:
        frame["participation_scope"] = scope
    return frame


# --- 分桶 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "minute,expected",
    [(30, "09:30"), (31, "09:30"), (44, "09:30"), (45, "09:45"), (46, "09:45"), (59, "09:45")],
)
def test_slot_of_floors_into_fifteen_minute_buckets(minute, expected):
    """cron 抖动/重试落在同一桶内，才能靠幂等覆盖而不是追加近似重复帧。"""
    assert ss.slot_of(datetime(2026, 8, 25, 9, minute)) == expected


def test_slot_of_covers_afternoon_hours():
    assert ss.slot_of(datetime(2026, 8, 25, 14, 47)) == "14:45"


# --- 幂等 ---------------------------------------------------------------


def test_same_slot_rerun_overwrites_instead_of_appending(state_home):
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2)})
    ss.record_slot(asof, "09:30", {"贵金属": _frame(7.4)})

    day = ss.load_day(asof)

    assert day["slots"] == ["09:30"]
    entries = day["sectors"]["贵金属"]
    assert len(entries) == 1
    assert entries[0]["average_pct"] == 7.4


def test_slot_rerun_with_fewer_sectors_drops_stale_sector(state_home):
    """重跑同一槽位时，上一次跑到、这一次没跑到的板块不得留下陈旧帧——
    同一槽位以最后一次执行为准。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2), "电子": _frame(3.1)})
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.5)})

    day = ss.load_day(asof)

    assert "电子" not in day["sectors"]
    assert len(day["sectors"]["贵金属"]) == 1


def test_distinct_slots_accumulate(state_home):
    asof = "2026-08-25"
    for slot, value in [("09:30", 6.0), ("09:45", 6.8), ("10:00", 7.5)]:
        ss.record_slot(asof, slot, {"贵金属": _frame(value)})

    day = ss.load_day(asof)

    assert day["slots"] == ["09:30", "09:45", "10:00"]
    assert [e["t"] for e in day["sectors"]["贵金属"]] == ["09:30", "09:45", "10:00"]


# --- degraded：跑了但没数据，必须与"没跑"可区分 -------------------------


def test_degraded_slot_is_recorded_in_both_slots_and_degraded_slots(state_home):
    """短名单降级时成员为空。若直接跳过写入，时序空洞会与"作业挂了"完全
    无法区分（issue #112/#113 的教训）。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2)})
    ss.record_slot(asof, "09:45", {}, degraded_reason="竞价短名单降级（collection_status=empty）")

    day = ss.load_day(asof)

    # 进了 slots：证明这一槽作业确实执行过
    assert day["slots"] == ["09:30", "09:45"]
    # 同时进 degraded_slots：证明执行了但观测不可用
    assert [item["t"] for item in day["degraded_slots"]] == ["09:45"]
    assert "竞价短名单降级" in day["degraded_slots"][0]["reason"]
    # 降级槽不给任何板块伪造条目
    assert [e["t"] for e in day["sectors"]["贵金属"]] == ["09:30"]


def test_missing_slot_is_absent_from_slots_entirely(state_home):
    """作业没跑的槽位根本不进 slots——这正是与 degraded 的区别。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2)})
    ss.record_slot(asof, "10:00", {"贵金属": _frame(7.0)})

    day = ss.load_day(asof)

    assert "09:45" not in day["slots"]
    assert all(item["t"] != "09:45" for item in day["degraded_slots"])


def test_degraded_slot_can_be_upgraded_on_rerun(state_home):
    """同槽重跑若这次拿到了数据，降级标记必须撤销，不能残留。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {}, degraded_reason="短名单降级")
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2)})

    day = ss.load_day(asof)

    assert day["degraded_slots"] == []
    assert len(day["sectors"]["贵金属"]) == 1


# --- 缺口不插值 ---------------------------------------------------------


def test_two_slots_yield_no_slope_instead_of_zero(state_home):
    """两点不构成趋势。返回 0.0 会被读成"走平"，必须是 None（不知道）。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.0)})
    ss.record_slot(asof, "09:45", {"贵金属": _frame(7.0)})

    summary = ss.derive_persistence(ss.load_day(asof), "贵金属")

    assert summary["average_pct_slope"] is None
    assert summary["status"] == "ok"


def test_three_slots_yield_a_real_slope(state_home):
    asof = "2026-08-25"
    for slot, value in [("09:30", 6.0), ("09:45", 7.0), ("10:00", 8.0)]:
        ss.record_slot(asof, slot, {"贵金属": _frame(value)})

    summary = ss.derive_persistence(ss.load_day(asof), "贵金属")

    assert summary["average_pct_slope"] == 1.0


def test_declining_strength_yields_negative_slope(state_home):
    asof = "2026-08-25"
    for slot, value in [("09:30", 8.0), ("09:45", 7.0), ("10:00", 6.0)]:
        ss.record_slot(asof, slot, {"贵金属": _frame(value)})

    assert ss.derive_persistence(ss.load_day(asof), "贵金属")["average_pct_slope"] == -1.0


# --- 空集不产生恒真 -----------------------------------------------------


def test_empty_day_reports_insufficient_instead_of_a_ratio():
    summary = ss.derive_persistence({}, "贵金属")

    assert summary["status"] == "insufficient_slots"
    assert summary["observed_slot_ratio"] is None
    assert summary["average_pct_slope"] is None


def test_unknown_sector_reports_insufficient(state_home):
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.2)})

    assert ss.derive_persistence(ss.load_day(asof), "不存在板块")["status"] == "insufficient_slots"


# --- 消费方语义 ---------------------------------------------------------


def test_observed_ratio_counts_degraded_slots_in_the_denominator(state_home):
    """看了 4 次、见到 3 次 → 0.75。降级槽算"看过但没见到"，不能从分母消失，
    否则持续性会被系统性高估。"""
    asof = "2026-08-25"
    for slot in ("09:30", "09:45", "10:00"):
        ss.record_slot(asof, slot, {"贵金属": _frame(6.0)})
    ss.record_slot(asof, "10:15", {}, degraded_reason="短名单降级")

    summary = ss.derive_persistence(ss.load_day(asof), "贵金属")

    assert summary["recorded_slot_count"] == 4
    assert summary["observed_slot_count"] == 3
    assert summary["observed_slot_ratio"] == 0.75


def test_member_delta_tracks_new_members_joining(state_home):
    asof = "2026-08-25"
    for slot, count in [("09:30", 4), ("09:45", 6), ("10:00", 9)]:
        ss.record_slot(asof, slot, {"贵金属": _frame(6.0, member_count=count)})

    assert ss.derive_persistence(ss.load_day(asof), "贵金属")["member_delta"] == 5


def test_participation_scope_is_carried_into_the_series(state_home):
    """#260 的来源标记要能追溯，但只是记录——本模块不据此改变任何门禁。"""
    asof = "2026-08-25"
    ss.record_slot(asof, "09:30", {"贵金属": _frame(6.0, scope="local_theme_only")})

    entry = ss.load_day(asof)["sectors"]["贵金属"][0]

    assert entry["participation_scope"] == "local_theme_only"


# --- 跨日隔离与保留 -----------------------------------------------------


def test_days_are_isolated_from_each_other(state_home):
    ss.record_slot("2026-08-25", "09:30", {"贵金属": _frame(6.0)})
    ss.record_slot("2026-08-26", "09:30", {"电子": _frame(3.0)})

    assert list(ss.load_day("2026-08-25")["sectors"]) == ["贵金属"]
    assert list(ss.load_day("2026-08-26")["sectors"]) == ["电子"]


def test_prune_keeps_only_the_newest_days(state_home):
    for day in range(1, 26):
        ss.record_slot(f"2026-08-{day:02d}", "09:30", {"贵金属": _frame(6.0)}, keep_days=999)

    removed = ss.prune_old_days(keep_days=20)

    assert removed == 5
    assert ss.load_day("2026-08-01") == {}
    assert ss.load_day("2026-08-25")["sectors"]


def test_prune_is_a_noop_below_the_threshold(state_home):
    ss.record_slot("2026-08-25", "09:30", {"贵金属": _frame(6.0)})

    assert ss.prune_old_days(keep_days=20) == 0
    assert ss.load_day("2026-08-25")["sectors"]


# --- artifact 体积 ------------------------------------------------------


def test_summary_is_bounded_and_excludes_the_series_body(state_home):
    """intraday-alert 的 max_output_chars=2500，时序本体绝不能进 artifact。"""
    asof = "2026-08-25"
    for index in range(12):
        ss.record_slot(
            asof,
            f"{9 + index // 4:02d}:{(index % 4) * 15:02d}",
            {f"板块{n}": _frame(5.0 + n) for n in range(30)},
        )

    summary = ss.summarize_day(ss.load_day(asof))

    assert summary["tracked_sector_count"] == 30
    assert len(summary["tracked_sectors"]) == 5  # 只留少量样本名
    assert "sectors" not in summary
    assert len(str(summary)) < 500
