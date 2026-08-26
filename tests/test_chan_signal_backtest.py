import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "chanlun-backtest"
    / "scripts"
    / "chan_signal_backtest.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("chan_signal_backtest_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(count=9):
    return [
        {
            "date": f"2026-01-{index + 1:02d}",
            "open": 10.0 + index,
            "close": 10.5 + index,
            "high": 11.0 + index,
            "low": 9.5 + index,
            "volume": 1000,
        }
        for index in range(count)
    ]


def test_signal_enters_after_first_detection_without_lookahead(monkeypatch):
    backtest = load_module()

    def fake_analyze(prefix):
        if len(prefix) < 5:
            return {"signals": []}
        return {
            "signals": [
                {
                    "type": "third_buy",
                    "strategy_id": "chanlun_third_buy",
                    "idx": 2,
                }
            ]
        }

    monkeypatch.setattr(backtest.chan_structure, "analyze", fake_analyze)

    events = backtest.extract_signal_events("000001", _bars())

    assert len(events) == 1
    assert events[0]["signal_date"] == "2026-01-03"
    assert events[0]["detection_date"] == "2026-01-05"
    assert events[0]["entry_date"] == "2026-01-06"
    assert events[0]["entry_price"] == 15.0
    assert events[0]["t1_exit_date"] == "2026-01-07"
    assert events[0]["t1_return"] == backtest._directional_net_return(
        15.0,
        16.5,
        "bullish",
    )


def test_all_four_signal_ids_receive_separate_gate_results(monkeypatch):
    backtest = load_module()
    events = []
    for index, strategy_id in enumerate(backtest.STRATEGY_DIRECTIONS):
        events.extend(
            {
                "strategy_id": strategy_id,
                "detection_date": f"2026-02-{day:02d}",
                "t1_return": 0.03 + index * 0.001,
                "t3_return": 0.04 + index * 0.001,
                "control_t1_return": -0.01,
                "control_t3_return": -0.005,
            }
            for day in range(1, 9)
        )

    result = backtest.analyze_events(
        events,
        split_date="2026-02-05",
        min_oos_samples=3,
        n_perm=100,
    )

    assert set(result["strategies"]) == set(backtest.STRATEGY_DIRECTIONS)
    for strategy_id, strategy in result["strategies"].items():
        assert strategy["research_state"]["strategy_id"] == strategy_id
        assert strategy["research_state"]["oos_sample_count"] == 4
        assert strategy["gate_result"]["decision"] in {
            "passed_for_reference",
            "failed",
            "blocked",
        }


def test_control_pools_include_real_random_breakout_and_buy_hold_samples():
    backtest = load_module()
    bars = _bars(30)

    pools = backtest.build_control_pools(
        [{"code": "000001", "bars": bars}],
        benchmark_bars=bars,
    )

    bullish = pools["chanlun_third_buy"]
    bearish = pools["chanlun_third_sell"]
    assert bullish["random_entry"]
    assert bullish["simple_breakout"]
    assert bullish["buy_hold"]
    assert bullish["random_entry"][0]["t1"] > 0
    assert bearish["random_entry"][0]["t1"] < 0


def test_formal_registration_is_idempotent_but_blocks_changed_oos(tmp_path):
    backtest = load_module()
    registry = str(tmp_path / "chan_oos_runs.json")
    registered = []
    base = {
        "research_protocol": {
            "split_date": "2025-01-01",
            "rules_fingerprint": "rules-v1",
            "dataset_fingerprint": "data-v1",
        },
        "strategies": {
            strategy_id: {
                "research_state": {
                    "strategy_id": strategy_id,
                    "phase": "oos_complete",
                    "rules_locked": True,
                    "has_costs": True,
                    "reports_all_variants": True,
                    "controls": ["random_entry", "simple_breakout", "buy_hold"],
                    "stat_tests": ["t_test", "bootstrap", "permutation"],
                    "oos_run_count": 1,
                    "changed_after_oos": False,
                    "permutation_p": 0.5,
                    "fdr_p": 0.5,
                    "oos_alpha": -0.01,
                    "benchmark_alpha": 0.0,
                    "oos_sample_count": 40,
                },
                "variants": {
                    "t1": {"oos": {"controls": {
                        "random_entry": {"n": 40},
                        "simple_breakout": {"n": 40},
                        "buy_hold": {"n": 40},
                    }}}
                },
                "gate_result": {
                    "strategy_id": strategy_id,
                    "decision": "failed",
                    "allowed_in_live_agent": False,
                }
            }
            for strategy_id in backtest.STRATEGY_DIRECTIONS
        },
    }
    input_path = tmp_path / "chan-input.json"
    input_path.write_text(json.dumps({"series": []}), encoding="utf-8")
    base = backtest.persist_evidence(
        base,
        input_path=str(input_path),
        artifact_dir=str(tmp_path / "artifacts"),
    )

    first = backtest.register_oos_results(
        base,
        registry_file=registry,
        gate_registrar=lambda sid, gate: registered.append(sid),
    )
    second = backtest.register_oos_results(
        base,
        registry_file=registry,
        gate_registrar=lambda sid, gate: registered.append(sid),
    )
    changed = {
        **base,
        "research_protocol": {
            **base["research_protocol"],
            "dataset_fingerprint": "data-v2",
        },
    }
    blocked = backtest.register_oos_results(
        changed,
        registry_file=registry,
        gate_registrar=lambda sid, gate: registered.append(sid),
    )

    assert first["status"] == "registered"
    assert second["status"] == "idempotent"
    assert blocked["status"] == "blocked"
    assert len(registered) == len(backtest.STRATEGY_DIRECTIONS) * 2


def test_v2_lineage_uses_strategy_id_v2_and_filters_unsure_signals(monkeypatch):
    """2026-08 T6：v2 谱系接入合成数据管线自检（pending_real_data_run）——只验证
    strategy_id_v2 路由 + is_sure 过滤生效，不构成任何 A/B 结论（结论只能来自
    docs_private/ 的真实数据运行）。"""
    backtest = load_module()

    def fake_analyze(prefix):
        if len(prefix) < 5:
            return {"signals": []}
        return {
            "signals": [
                {
                    "type": "bsp3a_buy",
                    "strategy_id": None,
                    "strategy_id_v2": "chanlun_bsp3a_buy_v2",
                    "idx": 2,
                    "is_sure": True,
                },
                {
                    "type": "bsp1_buy",
                    "strategy_id": "chanlun_bottom_divergence",
                    "strategy_id_v2": "chanlun_bsp1_buy_v2",
                    "idx": 2,
                    "is_sure": False,   # 锚定笔未确认，v2 口径下必须被过滤
                },
            ]
        }

    monkeypatch.setattr(backtest.chan_structure, "analyze", fake_analyze)

    v2_events = backtest.extract_signal_events(
        "000001",
        _bars(),
        strategy_directions=backtest.STRATEGY_DIRECTIONS_V2,
        strategy_id_field="strategy_id_v2",
        require_is_sure=True,
    )

    assert len(v2_events) == 1
    assert v2_events[0]["strategy_id"] == "chanlun_bsp3a_buy_v2"
    assert v2_events[0]["direction"] == "bullish"


def test_v2_lineage_has_twelve_ids_with_distinct_rules_fingerprint():
    backtest = load_module()
    assert len(backtest.STRATEGY_DIRECTIONS_V2) == 12
    for bsp_type in ("1", "1p", "2", "2s", "3a", "3b"):
        assert backtest.STRATEGY_DIRECTIONS_V2[f"chanlun_bsp{bsp_type}_buy_v2"] == "bullish"
        assert backtest.STRATEGY_DIRECTIONS_V2[f"chanlun_bsp{bsp_type}_sell_v2"] == "bearish"

    bars = _bars(30)
    payload = {"series": [{"code": "000001", "bars": bars}], "benchmark_bars": bars}
    legacy_result = backtest.analyze_payload(
        payload, split_date="2026-01-05", min_oos_samples=1, n_perm=20, lineage="legacy",
    )
    v2_result = backtest.analyze_payload(
        payload, split_date="2026-01-05", min_oos_samples=1, n_perm=20, lineage="v2",
    )

    assert set(legacy_result["strategies"]) == set(backtest.STRATEGY_DIRECTIONS)
    assert set(v2_result["strategies"]) == set(backtest.STRATEGY_DIRECTIONS_V2)
    assert legacy_result["research_protocol"]["lineage"] == "legacy"
    assert v2_result["research_protocol"]["lineage"] == "v2"
    # 版本化 ID + 独立规则指纹：v2 结果不可能被误当成 legacy 协议的重跑。
    assert (
        legacy_result["research_protocol"]["rules_fingerprint"]
        != v2_result["research_protocol"]["rules_fingerprint"]
    )


def test_persist_evidence_uses_item_direction_not_legacy_lookup(tmp_path):
    """回归：persist_evidence 曾硬编码 STRATEGY_DIRECTIONS.get(strategy_id) 取方向，
    v2 策略 ID 不在该表中会静默取到 None。改为读 item['direction']（analyze_events
    的输出，两条谱系都会设置）。"""
    backtest = load_module()
    bars = _bars(30)
    payload = {"series": [{"code": "000001", "bars": bars}], "benchmark_bars": bars}
    result = backtest.analyze_payload(
        payload, split_date="2026-01-05", min_oos_samples=1, n_perm=20, lineage="v2",
    )
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    persisted = backtest.persist_evidence(
        result, input_path=str(input_path), artifact_dir=str(tmp_path / "artifacts"),
    )

    for strategy_id, item in persisted["strategies"].items():
        artifact = json.loads(Path(item["evidence"]["artifact"]).read_text(encoding="utf-8"))
        assert artifact["rules"]["direction"] == backtest.STRATEGY_DIRECTIONS_V2[strategy_id]
        assert artifact["rules"]["rules_version"] == backtest.RULES_VERSION_V2


def test_registration_rejects_unpersisted_gate_results(tmp_path):
    backtest = load_module()
    result = {
        "research_protocol": {
            "split_date": "2025-01-01",
            "rules_fingerprint": "rules-v1",
            "dataset_fingerprint": "data-v1",
        },
        "strategies": {
            "chanlun_third_buy": {
                "research_state": {"strategy_id": "chanlun_third_buy"},
                "gate_result": {"decision": "failed", "allowed_in_live_agent": False},
            }
        },
    }

    outcome = backtest.register_oos_results(
        result,
        registry_file=str(tmp_path / "registry.json"),
    )

    assert outcome["status"] == "blocked"
    assert "evidence" in outcome["reason"].lower()
