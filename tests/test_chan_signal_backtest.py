import importlib.util
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
                "gate_result": {
                    "strategy_id": strategy_id,
                    "decision": "failed",
                    "allowed_in_live_agent": False,
                }
            }
            for strategy_id in backtest.STRATEGY_DIRECTIONS
        },
    }

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
