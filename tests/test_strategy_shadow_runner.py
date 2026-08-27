"""每日六策略 shadow runner 的隔离与幂等契约。"""

import json

import pytest

from scripts import preleader_pretable_build as builder
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


def test_canonical_strategy_evidence_is_consumed_without_remerging_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({
        "schema": "strategy_evidence_daily_v1",
        "asof": "2026-08-26",
        "canonical_forward": True,
        "exploratory_reconstruction": False,
        "records": [{"code": "600001", "date": "2026-08-26", "sector": "通信"}],
        "market_state": {"available": True, "dominant_state": "S2"},
        "coverage": {"rank_surprise": {"ready_records": 0}},
        "research_only": True,
        "execution_eligible": False,
    }), encoding="utf-8")

    result = runner.run(str(path), asof="2026-08-26")

    assert result["evidence_schema"] == "strategy_evidence_daily_v1"
    assert result["canonical_forward"] is True
    assert result["evidence_sidecars"] == []
    assert result["evidence_coverage"]["rank_surprise"]["ready_records"] == 0
    assert len(result["strategies"]["ice_point_reversal"]["results"]) == 1


def test_noncanonical_evidence_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps({
        "schema": "strategy_evidence_daily_v1", "asof": "2026-08-26",
        "canonical_forward": False, "records": [],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical forward"):
        runner.run(str(path), asof="2026-08-26")


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


def _write_pretable(tmp_path, monkeypatch, *, as_of):
    """在 ``as_of`` 落一张可用盘前表（证据齐全路径）。"""
    pool = tmp_path / f"pool-{as_of}.json"
    pool.write_text(json.dumps({
        "asof": as_of,
        "candidates": [
            {"code": "600001", "sector": "通信设备", "leader_role": "sector_leader"},
            {"code": "600002", "sector": "通信设备", "leader_role": "sector_follower"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(builder, "average_turnover",
                        lambda codes, as_of_: {code: 5e7 for code in codes})
    monkeypatch.setattr(builder, "scan_material_bad_news",
                        lambda codes, as_of_: ({"600002": False}, []))
    return builder.run(str(pool), as_of=as_of)


def test_preleader_reports_the_missing_pretable_reason_not_a_no_signal(tmp_path, monkeypatch):
    """没有 D-1 盘前表时必须是 unavailable + 具体原因。

    传一张空表进去会让它输出 no_signal，把"没数据"伪装成"明确不在表内"——
    零样本于是看起来像已验证的负结果。
    """
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    result = runner.run(str(_input(tmp_path)), asof="2026-08-26")

    preleader = result["strategies"]["preleader_arbitrage"]
    assert preleader["status"] == "unavailable"
    assert preleader["reasons"] == ["no_prior_pretable_artifact"]
    assert result["preleader_pretable_asof"] is None


def test_preleader_consumes_the_prior_day_pretable(tmp_path, monkeypatch):
    """正向对照：D-1 有可用盘前表时，它必须真的被读进来。

    只配"缺表→unavailable"的用例，一个恒不加载的实现也能全过。
    """
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    _write_pretable(tmp_path, monkeypatch, as_of="2026-08-25")

    result = runner.run(str(_input(tmp_path)), asof="2026-08-26")
    assert result["preleader_pretable_asof"] == "2026-08-25"
    assert result["preleader_pretable_status"] == "ok"
    # 表被读进来后走的是真实评估路径：产出 summary，而不是缺表短路的 reasons。
    preleader = result["strategies"]["preleader_arbitrage"]
    assert "summary" in preleader
    assert "reasons" not in preleader


def test_same_day_pretable_is_not_accepted_as_a_preopen_table(tmp_path, monkeypatch):
    """D0 当天建的表不是盘前表——接受它等于用当日信息选样本。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    _write_pretable(tmp_path, monkeypatch, as_of="2026-08-26")

    result = runner.run(str(_input(tmp_path)), asof="2026-08-26")
    assert result["preleader_pretable_asof"] is None
    assert result["strategies"]["preleader_arbitrage"]["status"] == "unavailable"


def test_first_seal_becomes_the_leader_confirmation_input():
    """封板时刻映射成 S4 的 confirmed/confirmed_time/evaluation_time。

    这是口径判断而非字段搬运：把"当日封上板"等同于"该标的已确认"。没封板的行
    不给 confirmed，让龙头保持不可判定，而不是用涨幅之类的代理值凑一个。
    """
    rows = [{"code": "600001", "first_seal": "0935"}, {"code": "600002"}]
    mapped = {row["code"]: row for row in runner._preleader_records(rows)}

    assert mapped["600001"]["confirmed"] is True
    assert mapped["600001"]["confirmed_time"] == "0935"
    assert mapped["600001"]["evaluation_time"] == "0935"
    assert "confirmed" not in mapped["600002"]
    # 不得就地改调用方的记录。
    assert "confirmed" not in rows[0]


def test_existing_confirmation_fields_are_not_overwritten():
    rows = [{"code": "600001", "first_seal": "0935",
             "confirmed": False, "confirmed_time": "1000"}]
    mapped = runner._preleader_records(rows)[0]
    assert mapped["confirmed"] is False
    assert mapped["confirmed_time"] == "1000"
