"""每日六策略 shadow runner 的隔离与幂等契约。"""

import json

import pytest

from scripts import strategy_shadow_runner as runner


def _input(tmp_path, *, asof="2026-08-26"):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({
        "schema": "strategy_shadow_input_v1",
        "asof": asof,
        "candidates": [{"code": "600001", "name": "fixture"}],
    }), encoding="utf-8")
    return path


def test_all_six_strategies_run_and_remain_non_live(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    result = runner.run(str(_input(tmp_path)), asof="2026-08-26")

    assert set(result["strategies"]) == set(runner.STRATEGY_IDS)
    assert result["research_only"] is True
    assert result["execution_eligible"] is False
    assert result["live_order_sent"] is False
    assert all(item["research_only"] for item in result["strategies"].values())
    assert all(item["execution_eligible"] is False for item in result["strategies"].values())
    assert any(item["status"] == "unavailable" for item in result["strategies"].values())


def test_same_input_is_idempotent_and_conflicting_input_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    path = _input(tmp_path)
    first = runner.run(str(path), asof="2026-08-26")
    second = runner.run(str(path), asof="2026-08-26")
    assert second["result_sha256"] == first["result_sha256"]

    path.write_text(path.read_text(encoding="utf-8").replace("fixture", "changed"), encoding="utf-8")
    with pytest.raises(ValueError, match="different input"):
        runner.run(str(path), asof="2026-08-26")


def test_input_date_mismatch_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="asof mismatch"):
        runner.run(str(_input(tmp_path, asof="2026-08-25")), asof="2026-08-26")


def test_same_day_auction_and_market_sidecars_are_merged(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    auction_dir = state / "skills" / "daban-stock-picker" / "data"
    selection_dir = state / "skills" / "stock-triage" / "data"
    auction_dir.mkdir(parents=True)
    selection_dir.mkdir(parents=True)
    (auction_dir / "auction_shortlist_latest.json").write_text(json.dumps({
        "asof": "2026-08-26",
        "factors": [{"code": "600001", "auction_strength": 8.5}],
    }), encoding="utf-8")
    (selection_dir / "hot_money_selection_latest.json").write_text(json.dumps({
        "asof": "2026-08-26", "market_state": {"dominant_state": "S3"},
    }), encoding="utf-8")

    payload, sidecars = runner._merge_auction_evidence(
        json.loads(_input(tmp_path).read_text(encoding="utf-8")), "2026-08-26"
    )
    assert payload["candidates"][0]["auction_strength"] == 8.5
    assert payload["market_state"]["dominant_state"] == "S3"
    assert len(sidecars) == 2


def test_prefixed_candidate_codes_still_receive_auction_evidence(tmp_path, monkeypatch):
    """两侧代码归一化口径必须一致，否则 sidecar 记为「已用」而证据其实丢了。

    候选池的 code 带 `sh/sz` 前缀是真实形态（runner 自己就有 market_code 回退，
    竞价侧也显式剥前缀）。只归一化一侧会让合并静默落空——比缺证据更糟，因为
    artifact 仍然声称用了这份 sidecar。
    """
    state = tmp_path / "state"
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(state))
    auction_dir = state / "skills" / "daban-stock-picker" / "data"
    auction_dir.mkdir(parents=True)
    (auction_dir / "auction_shortlist_latest.json").write_text(json.dumps({
        "asof": "2026-08-26",
        "factors": [{"code": "sh600001", "auction_strength": 8.5}],
    }), encoding="utf-8")

    payload, sidecars = runner._merge_auction_evidence(
        {"asof": "2026-08-26", "candidates": [{"code": "sh600001", "name": "fixture"}]},
        "2026-08-26",
    )
    assert sidecars, "同日竞价 sidecar 应被采用"
    assert payload["candidates"][0]["auction_strength"] == 8.5
