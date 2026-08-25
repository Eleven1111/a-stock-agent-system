"""Bounded 09:50/13:15 hot-money research checkpoint tests."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest
from state_store import atomic_write_json, read_json


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "daban-stock-picker"
    / "scripts"
    / "hot_money_checkpoint.py"
)
SPEC = importlib.util.spec_from_file_location("hot_money_checkpoint", SCRIPT)
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


def _candidate(code="sh600001", sector="半导体", qualified=True):
    return {
        "code": code,
        "name": f"股票{code[-6:]}",
        "sector": sector,
        "strategy_id": "daban:mainline_leader_confirm",
        "hot_money_qualified": qualified,
        "open_score": 88.0,
        "selection_context": {
            "window": "09:35",
            "market_timing": {"tier": "发酵", "daban_ready": True},
            "sector": {"name": sector, "rank": 1, "state": "confirmed"},
            "leader": {"rank": 1, "role": "sector_leader", "qualified": qualified},
        },
    }


def _quote(code="sh600001", change_pct=6.0, price=10.6, open_price=10.4):
    quote = {
        "code": code,
        "name": f"股票{code[-6:]}",
        "price": price,
        "prev_close": 10.0,
        "open": open_price,
        "high": max(price, open_price),
        "low": min(price, open_price),
        "volume": 100_000,
        "change_pct": change_pct,
    }
    quote["minute_bars"] = [
        {"time": f"{(570 + index) // 60:02d}{(570 + index) % 60:02d}",
         "price": open_price + index / 100, "vwap": open_price}
        for index in range(30)
    ]
    return quote


def test_checkpoint_is_research_only_and_t1_safe():
    result = checkpoint.evaluate_checkpoint(
        [_candidate()],
        {"sh600001": _quote()},
        profile="morning_confirm",
        asof="2026-06-22",
    )

    item = result[0]
    assert item["research_state"] == "confirmed"
    assert item["execution_action"] == "none"
    assert item["same_day_sell_allowed"] is False
    assert item["earliest_sell_date"] > "2026-06-22"
    assert item["selection_context"]["window"] == "09:50"


def test_non_mainline_or_missing_quote_fails_closed():
    result = checkpoint.evaluate_checkpoint(
        [_candidate(qualified=False), _candidate("sh600002")],
        {"sh600001": _quote()},
        profile="morning_confirm",
        asof="2026-06-22",
    )

    by_code = {item["code"]: item for item in result}
    assert by_code["sh600001"]["research_state"] == "invalidated"
    assert by_code["sh600002"]["research_state"] == "invalidated"
    assert "行情" in "；".join(by_code["sh600002"]["reasons"])


def test_sector_relative_rank_can_downgrade_follower():
    candidates = [
        _candidate("sh600001"),
        _candidate("sh600002"),
        _candidate("sh600003"),
    ]
    quotes = {
        "sh600001": _quote("sh600001", 7.0, 10.7),
        "sh600002": _quote("sh600002", 6.0, 10.6),
        "sh600003": _quote("sh600003", 5.0, 10.5),
    }

    result = checkpoint.evaluate_checkpoint(
        candidates,
        quotes,
        profile="morning_confirm",
        asof="2026-06-22",
    )

    assert [item["checkpoint_sector_rank"] for item in result] == [1, 2, 3]
    assert result[2]["research_state"] == "watch"
    assert "板块内强度" in "；".join(result[2]["reasons"])


def test_run_checkpoint_reuses_open_surface_and_writes_immutable_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = "2026-06-22"
    atomic_write_json(
        checkpoint.open_confirmation_path(asof),
        {
            "schema": "open_confirmation_v3",
            "status": "ready",
            "asof": asof,
            "source_asof": "2026-06-19",
            "signals": [_candidate()],
        },
    )
    monkeypatch.setattr(
        checkpoint,
        "fetch_quotes",
        lambda codes: {code: _quote(code) for code in codes},
    )
    captured = {}
    monkeypatch.setattr(
        checkpoint.candidate_lifecycle,
        "transition",
        lambda *args, **kwargs: captured.update({"args": args, "kwargs": kwargs}) or {},
    )

    result = checkpoint.run_checkpoint("morning_confirm", asof)

    assert result["status"] == "ready"
    assert result["confirmed_count"] == 1
    assert result["input_snapshot"]["snapshot_id"].startswith("snap-")
    persisted = read_json(checkpoint.latest_output_path("morning_confirm"), {})
    assert persisted["observations"][0]["research_state"] == "confirmed"
    assert persisted["output_snapshot"]["snapshot_id"].startswith("snap-")
    assert captured["args"][1] == "morning_reconfirmed"


@pytest.mark.parametrize("source_status", ["degraded", "insufficient_data"])
def test_morning_checkpoint_recovers_candidates_from_degraded_open_surface(
    tmp_path,
    monkeypatch,
    source_status,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = "2026-06-22"
    atomic_write_json(
        checkpoint.open_confirmation_path(asof),
        {
            "schema": "open_confirmation_v3",
            "status": source_status,
            "asof": asof,
            "source_asof": "2026-06-19",
            "signals": [],
            "evaluated_confirmations": [_candidate()],
        },
    )
    monkeypatch.setattr(
        checkpoint,
        "fetch_quotes",
        lambda codes: {code: {**_quote(code), "minute_bars": [
            {"time": f"09{minute:02d}", "price": 10.0 + minute / 100, "vwap": 10.0}
            for minute in range(30, 51)
        ]} for code in codes},
    )
    monkeypatch.setattr(checkpoint.candidate_lifecycle, "transition", lambda *a, **k: {})

    result = checkpoint.run_checkpoint("morning_confirm", asof)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["source_status"] == source_status
    assert result["observations"][0]["execution_action"] == "none"
    assert result["observations"][0]["same_day_sell_allowed"] is False


def test_fetch_quotes_attaches_bounded_minute_bars(monkeypatch):
    codes = [f"sh60{index:04d}" for index in range(21)]
    calls = []
    monkeypatch.setattr(
        checkpoint,
        "fetch_tencent_snapshot",
        lambda requested: {code: _quote(code) for code in requested},
    )
    monkeypatch.setattr(
        checkpoint,
        "fetch_tencent_minute",
        lambda code, *, market: calls.append((market, code)) or [
            {"time": "0930", "price": 10.0, "cum_volume": 100, "cum_amount": 100_000}
        ],
    )

    quotes = checkpoint.fetch_quotes(codes)

    assert len(quotes) == checkpoint.MAX_CANDIDATES
    assert len(calls) == checkpoint.MAX_CANDIDATES
    assert all(quote["minute_bars"] for quote in quotes.values())


def test_morning_checkpoint_ignores_bars_after_profile_cutoff():
    bars = [
        {"time": f"09{minute:02d}", "price": 10.0 + minute / 100,
         "cum_volume": 1000 * (minute - 29), "cum_amount": 1_000_000 * (minute - 29)}
        for minute in range(30, 51)
    ] + [
        {"time": "1030", "price": 1.0, "cum_volume": 999_999, "cum_amount": 999_999}
    ]
    quote = {**_quote(), "minute_bars": bars}

    candidate = {**_candidate(), "vwap_above_time_ratio": 0.0, "open_relative_volume": 0.1,
                 "previous_volume": 50_000}
    result = checkpoint.evaluate_checkpoint(
        [candidate], {"sh600001": quote},
        profile="morning_confirm", asof="2026-06-22",
    )[0]

    assert result["open_15m_drawdown_pct"] < 1.0
    assert result["vwap_above_time_ratio"] is not None
    assert result["vwap_above_time_ratio"] > 0.0
    assert result["open_relative_volume"] == 0.42
    assert result["price"] == 10.5


def test_morning_checkpoint_does_not_confirm_without_complete_minute_evidence():
    quote = {**_quote(), "minute_bars": _quote()["minute_bars"][:6]}

    result = checkpoint.evaluate_checkpoint(
        [_candidate()], {"sh600001": quote},
        profile="morning_confirm", asof="2026-06-22",
    )[0]

    assert result["research_state"] == "watch"
    assert "分钟证据不足" in "；".join(result["reasons"])


@pytest.mark.parametrize("invalid_kind", ["nan_prices", "partial_vwap"])
def test_morning_checkpoint_rejects_invalid_minute_window(invalid_kind):
    bars = [
        {"time": f"{(570 + index) // 60:02d}{(570 + index) % 60:02d}",
         "price": 10.5, "vwap": 10.0}
        for index in range(15)
    ]
    if invalid_kind == "nan_prices":
        for bar in bars:
            bar["price"] = float("nan")
    else:
        for bar in bars[1:]:
            bar.pop("vwap")

    result = checkpoint.evaluate_checkpoint(
        [_candidate()], {"sh600001": {**_quote(), "minute_bars": bars}},
        profile="morning_confirm", asof="2026-06-22",
    )[0]

    assert result["research_state"] != "confirmed"
    assert result["fifteen_minute_ready"] is False


def test_historical_checkpoint_live_fetch_is_blocked():
    with pytest.raises(checkpoint.DataSourceError, match="replay"):
        checkpoint._require_same_day_live("2026-06-22")


def test_load_open_confirmation_rejects_wrong_day(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    atomic_write_json(
        checkpoint.open_confirmation_path("2026-06-22"),
        {"status": "ready", "asof": "2026-06-21", "signals": [_candidate()]},
    )

    with pytest.raises(checkpoint.DataSourceError, match="日期"):
        checkpoint.load_open_confirmation("2026-06-22")


def _run_main(monkeypatch, capsys, asof):
    monkeypatch.setattr(
        sys,
        "argv",
        ["hot_money_checkpoint.py", "--profile", "morning_confirm", "--asof", asof, "--json"],
    )
    code = checkpoint.main()
    payload = json.loads(capsys.readouterr().out)
    return code, payload


def test_main_exits_zero_when_same_day_shortlist_is_empty(tmp_path, monkeypatch, capsys):
    """同日短名单存在但为空 = 弱市零候选常态，exit 0，不再触发调度器错误退避。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = date.today().isoformat()
    atomic_write_json(
        checkpoint.auction_shortlist_path(asof),
        {"schema": "auction_shortlist_v1", "asof": asof, "shortlist": []},
    )

    code, payload = _run_main(monkeypatch, capsys, asof)

    assert code == 0
    assert payload["status"] == "insufficient_data"
    assert payload["confirmed_count"] == 0
    assert payload["research_only"] is True


def test_main_exits_one_when_shortlist_file_is_missing(tmp_path, monkeypatch, capsys):
    """短名单文件整体缺失（上游可能没跑/崩了）仍保持大声失败 exit 1。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    asof = date.today().isoformat()

    code, payload = _run_main(monkeypatch, capsys, asof)

    assert code == 1
    assert payload["status"] == "insufficient_data"


# --------------------------------------------------------------------------- #
# 分钟派生落盘（路径 B）—— 挂在本作业已抓的分时上，不新增任何网络请求
# --------------------------------------------------------------------------- #
def _cumulative_bars(lots_per_minute=100.0, minutes=30):
    """腾讯口径的分时：累计成交量（手）、累计成交额（元）。"""
    total = 0.0
    rows = []
    for index in range(minutes):
        minute = 570 + index
        total += lots_per_minute
        rows.append({
            "time": f"{minute // 60:02d}{minute % 60:02d}",
            "price": 10.0,
            "cum_volume": total,
            "cum_amount": total * 100 * 10.0,
        })
    return rows


def test_persist_minute_derived_writes_curve_without_raw_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    result = checkpoint.persist_minute_derived(
        {"sh600001": {"minute_bars": _cumulative_bars()}}, "2026-08-25")
    assert result["status"] == "ok" and result["count"] == 1

    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
    record = payload["records"]["600001"]
    # 落的是 5 分钟增量曲线，不是 240 根原始分时。
    assert record["slots_step_minutes"] == 5
    assert len(record["slots"]) == 6          # 09:35 … 10:00
    assert "minute_bars" not in record
    # 量比要过去 5 日日线，本作业手上没有、也不许为此新增请求 → 保持 unavailable。
    assert record["volume_ratio"] is None
    assert record["volume_ratio_availability"].startswith("unavailable:")


def test_persist_minute_derived_skips_when_no_minute_bars(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    result = checkpoint.persist_minute_derived({"sh600001": {"price": 10.0}}, "2026-08-25")
    assert result == {"status": "skipped", "reason": "no_minute_bars", "count": 0}
