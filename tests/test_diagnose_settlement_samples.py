"""结算样本诊断脚本的判据回归。

第一版判据出过错：拿"已结算记录总数"比门槛，而该总数被落选候选主导
（部署机实测 48855 条已结算里，通过 open_confirmed 的只有 9 条），
于是在执行层样本只有个位数时仍可能报"样本充足"。这里把正确判据钉死。
"""

import json

import pytest

from scripts import diagnose_settlement_samples as diag


# 已核对为 2026 年真实交易日（周一至周五且非休市日）。
TRADING_DAYS = [
    "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
    "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21",
]


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    return tmp_path


def _record(*, stages, resolved=True):
    return {
        "code": "600001",
        "stage_history": [{"stage": s, "selected": True} for s in stages],
        "outcome": (
            {"resolved": True, "t1_close_ret": 0.01, "t3_close_ret": 0.02}
            if resolved else {"resolved": False}
        ),
    }


def _write_day(state_home, asof, records):
    path = state_home / "skills" / "stock-triage" / "data" / "candidate_lifecycle"
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{asof}.json").write_text(
        json.dumps({"schema": "candidate_lifecycle_v1", "asof": asof, "records": records}),
        encoding="utf-8",
    )


# --- 核心回归：总数不得驱动判定 ----------------------------------------


def test_rejected_dominated_total_does_not_make_execution_layer_look_sufficient(state_home):
    """复刻部署机形状：已结算总数上万，但执行层只有个位数样本。"""
    for asof in TRADING_DAYS:
        records = [_record(stages=[]) for _ in range(2000)]          # 落选但已结算
        records += [_record(stages=["discovery"]) for _ in range(100)]
        records.append(_record(stages=["discovery", "auction_shortlist", "open_confirmed"]))
        _write_day(state_home, asof, records)

    report = diag.collect(asof_today="2026-09-30")

    assert report["settled_records_including_rejected"] > 20000
    # 研究层充足、执行层不足 —— 两者必须独立判定
    assert report["verdict"]["research_layer"]["sufficient"] is True
    assert report["verdict"]["execution_layer"]["sufficient"] is False
    assert report["verdict"]["execution_layer"]["samples"] == len(TRADING_DAYS)
    assert any("conditional_trade_enabled" in item
               for item in report["verdict"]["limitations"])


def test_execution_layer_binds_to_the_narrowest_stage(state_home):
    """执行层取最窄阶段，不能被较宽的 auction_shortlist 掩盖。"""
    for asof in TRADING_DAYS:
        records = [_record(stages=["discovery", "auction_shortlist"]) for _ in range(50)]
        records.append(_record(stages=["discovery", "auction_shortlist", "open_confirmed"]))
        _write_day(state_home, asof, records)

    report = diag.collect(asof_today="2026-09-30")

    assert report["settled_by_stage"]["auction_shortlist"] == 510
    assert report["verdict"]["execution_layer"]["binding_stage"] == "open_confirmed"
    assert report["verdict"]["execution_layer"]["sufficient"] is False


def test_both_layers_sufficient_when_execution_samples_are_real(state_home):
    for asof in TRADING_DAYS:
        _write_day(state_home, asof, [
            _record(stages=["discovery", "auction_shortlist", "open_confirmed"])
            for _ in range(40)
        ])

    report = diag.collect(asof_today="2026-09-30")

    assert report["verdict"]["research_layer"]["sufficient"] is True
    assert report["verdict"]["execution_layer"]["sufficient"] is True
    assert "两层样本均充足" in report["verdict"]["next_action"]


def test_research_shortfall_points_at_the_discovery_pipeline(state_home):
    _write_day(state_home, TRADING_DAYS[0], [_record(stages=["discovery"]) for _ in range(3)])

    report = diag.collect(asof_today="2026-09-30")

    assert report["verdict"]["research_layer"]["sufficient"] is False
    assert "candidate-discovery" in report["verdict"]["next_action"]


# --- 结算滞后：待结算 vs 逾期 -------------------------------------------


def test_recent_unsettled_day_is_pending_not_overdue(state_home):
    """t3 需要 3 个交易日，最近几天没结算是正常的，不能报成故障。"""
    _write_day(state_home, "2026-08-20", [_record(stages=["discovery"], resolved=False)])

    report = diag.collect(asof_today="2026-08-21")

    assert report["pending_settlement_days"] == ["2026-08-20"]
    assert report["overdue_settlement_days"] == []


def test_old_unsettled_day_is_overdue(state_home):
    _write_day(state_home, "2026-08-10", [_record(stages=["discovery"], resolved=False)])

    report = diag.collect(asof_today="2026-08-21")

    assert report["overdue_settlement_days"] == ["2026-08-10"]
    assert report["pending_settlement_days"] == []


def test_settled_day_is_neither_pending_nor_overdue(state_home):
    _write_day(state_home, "2026-08-10", [_record(stages=["discovery"])])

    report = diag.collect(asof_today="2026-08-21")

    assert report["overdue_settlement_days"] == []
    assert report["pending_settlement_days"] == []


# --- 缺失日 vs 空文件日 --------------------------------------------------


def test_missing_trading_day_file_is_reported_separately_from_empty_file(state_home):
    """"根本没产出"与"产出了但零记录"是两种不同故障，不能混为一谈。"""
    _write_day(state_home, "2026-08-10", [_record(stages=["discovery"])])
    _write_day(state_home, "2026-08-12", [])            # 有文件、零记录
    _write_day(state_home, "2026-08-14", [_record(stages=["discovery"])])

    report = diag.collect(asof_today="2026-09-30")

    assert "2026-08-11" in report["missing_days"]       # 完全没文件
    assert "2026-08-13" in report["missing_days"]
    assert report["empty_days"] == ["2026-08-12"]       # 有文件但空
    assert "2026-08-12" not in report["missing_days"]


def test_weekend_is_not_counted_as_a_missing_trading_day(state_home):
    _write_day(state_home, "2026-08-14", [_record(stages=["discovery"])])  # 周五
    _write_day(state_home, "2026-08-17", [_record(stages=["discovery"])])  # 周一

    report = diag.collect(asof_today="2026-09-30")

    assert report["missing_days"] == []
    assert report["calendar_status"] == "ok"


def test_uncovered_calendar_year_fails_closed_instead_of_guessing(state_home):
    """日历未覆盖该年份时报不出来就说报不出来，不猜缺失日。"""
    _write_day(state_home, "2031-08-10", [_record(stages=["discovery"])])
    _write_day(state_home, "2031-08-14", [_record(stages=["discovery"])])

    report = diag.collect(asof_today="2031-09-30")

    assert report["missing_days"] == []
    assert report["calendar_status"].startswith("unavailable")


# --- 其它 ---------------------------------------------------------------


def test_empty_state_reports_no_data_without_crashing(state_home):
    report = diag.collect(asof_today="2026-08-21")

    assert report["lifecycle_day_count"] == 0
    assert report["lifecycle_date_range"] is None
    assert report["verdict"]["research_layer"]["sufficient"] is False


def test_collect_does_not_modify_any_data_file(state_home):
    """数据文件内容必须逐字节不变。

    读取走 state_store 的文件锁（生产机上 cron 正在写这些文件，无锁读会读到
    写一半的内容），因此允许新增 ``.lock`` 边车——但只允许这一种。
    """
    _write_day(state_home, "2026-08-10", [_record(stages=["discovery"])])

    def _snapshot():
        return {
            p.relative_to(state_home).as_posix(): p.read_bytes()
            for p in state_home.rglob("*")
            if p.is_file() and not p.name.endswith(".lock")
        }

    before = _snapshot()
    before_paths = {p.relative_to(state_home).as_posix() for p in state_home.rglob("*") if p.is_file()}

    diag.collect(asof_today="2026-08-21")

    assert _snapshot() == before, "诊断脚本不得改动任何数据文件"
    after_paths = {p.relative_to(state_home).as_posix() for p in state_home.rglob("*") if p.is_file()}
    assert all(p.endswith(".lock") for p in after_paths - before_paths), (
        "除 state_store 锁边车外不得新增文件"
    )


def test_render_marks_the_total_as_unusable_for_the_verdict(state_home):
    """渲染层必须显式警告总数不可用于判定，否则容易再被误读一次。"""
    _write_day(state_home, "2026-08-10", [_record(stages=["discovery"])])

    text = diag._render(diag.collect(asof_today="2026-08-21"))

    assert "不可据此判定样本充足" in text
    assert "各阶段通过的已结算样本（判定依据）" in text
