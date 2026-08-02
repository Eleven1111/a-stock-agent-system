"""缠论门控一次性评估 runner — 合成管线自检 + 真实抓取路径 mock 化验证。

网络/mootdx 一律 mock，测试不触网。真实数据结论的可信度由
docs/chanlun-gate-evaluation-2026-07.md 的真实运行记录负责，不在本文件断言。
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "chanlun_gate_evaluation.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("chanlun_gate_evaluation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(start_price=10.0, n=80, seed=1):
    import random

    rng = random.Random(seed)
    price = start_price
    bars = []
    for i in range(n):
        price = max(1.0, price * (1 + rng.uniform(-0.02, 0.02)))
        bars.append({
            "date": f"2024-{1 + i // 25:02d}-{1 + i % 25:02d}",
            "open": round(price * 0.999, 3),
            "high": round(price * 1.01, 3),
            "low": round(price * 0.99, 3),
            "close": round(price, 3),
            "volume": 100000,
        })
    return bars


def test_synthetic_mode_runs_end_to_end_and_is_never_a_or_b():
    module = load_module()

    result = module.run_evaluation(
        mode="synthetic",
        split_date="2024-07-01",
        start_date="2024-01-01",
        min_oos_samples=3,
        n_perm=50,
        persist=False,
    )
    summary = module.summarize_for_report(result)

    assert result["evaluation"]["data_mode"] == "synthetic"
    assert set(result["strategies"]) == set(module.backtest.STRATEGY_DIRECTIONS)
    # Synthetic fixtures must never produce an A/B positioning verdict.
    assert summary["verdict"] == "pending_real_data_run"


def test_real_mode_fetches_via_mootdx_index_bars_for_benchmark():
    module = load_module()
    fake_series_bars = {
        "600519": _bars(1700.0, n=90, seed=2),
        "000001": _bars(12.0, n=90, seed=3),
    }
    fake_benchmark = _bars(3800.0, n=90, seed=4)

    with patch.object(module.mootdx, "get_client", return_value="fake-client"), \
         patch.object(module.mootdx, "fetch_klines", return_value=fake_series_bars) as fk, \
         patch.object(module.mootdx, "fetch_index_daily", return_value=fake_benchmark) as fi:
        result = module.run_evaluation(
            mode="real",
            split_date="2024-07-01",
            start_date="2024-01-01",
            codes=["600519", "000001"],
            min_oos_samples=3,
            n_perm=50,
            persist=False,
        )

    fk.assert_called_once()
    fi.assert_called_once()
    assert result["evaluation"]["data_mode"] == "real"
    assert result["evaluation"]["data_source"] == "mootdx"
    assert result["evaluation"]["fetched_codes"] == ["000001", "600519"]
    assert result["evaluation"]["data_range"]["symbol_count"] == 2


def test_real_mode_skips_symbols_with_short_history():
    module = load_module()
    fake_series_bars = {
        "600519": _bars(1700.0, n=90, seed=2),
        "000001": _bars(12.0, n=10, seed=3),  # below the 60-bar floor
    }
    with patch.object(module.mootdx, "get_client", return_value="fake-client"), \
         patch.object(module.mootdx, "fetch_klines", return_value=fake_series_bars), \
         patch.object(module.mootdx, "fetch_index_daily", return_value=_bars(3800.0, n=90, seed=4)):
        payload = module.fetch_real_payload(
            codes=["600519", "000001"], start_date="2024-01-01"
        )

    assert [item["code"] for item in payload["series"]] == ["600519"]
    assert payload["skipped_short_history"] == ["000001"]


def test_summarize_for_report_verdict_a_when_any_strategy_passes():
    module = load_module()
    result = {
        "evaluation": {"data_mode": "real", "data_range": {}},
        "split_date": "2024-07-01",
        "strategies": {
            "chanlun_third_buy": {
                "direction": "bullish",
                "research_state": {
                    "oos_sample_count": 40,
                    "permutation_p": 0.01,
                    "fdr_p": 0.02,
                    "oos_alpha": 0.03,
                    "benchmark_alpha": 0.0,
                },
                "gate_result": {
                    "decision": "passed_for_reference",
                    "allowed_in_live_agent": True,
                    "blocking_reasons": [],
                },
            },
        },
    }
    summary = module.summarize_for_report(result)
    assert summary["verdict"] == "A_register_candidate"


def test_summarize_for_report_verdict_b_when_all_fail_or_block():
    module = load_module()
    result = {
        "evaluation": {"data_mode": "real", "data_range": {}},
        "split_date": "2024-07-01",
        "strategies": {
            "chanlun_third_sell": {
                "direction": "bearish",
                "research_state": {
                    "oos_sample_count": 6,
                    "permutation_p": 0.8,
                    "fdr_p": 0.8,
                    "oos_alpha": -0.01,
                    "benchmark_alpha": 0.0,
                },
                "gate_result": {
                    "decision": "blocked",
                    "allowed_in_live_agent": False,
                    "blocking_reasons": ["样本量不足: 6<30"],
                },
            },
        },
    }
    summary = module.summarize_for_report(result)
    assert summary["verdict"] == "B_structure_filter_only"


def test_v2_lineage_synthetic_pipeline_stays_pending_and_never_registers():
    """2026-08 T6：v2 谱系（12 个 chanlun_bsp{...}_v2）合成管线自检。输出必须标注
    pending_real_data_run，不可作为 A/B 结论——真实结论只来自
    docs/chanlun-gate-evaluation-2026-08.md 记录的 --mode real 运行。"""
    module = load_module()

    result = module.run_evaluation(
        mode="synthetic",
        split_date="2024-07-01",
        start_date="2024-01-01",
        min_oos_samples=3,
        n_perm=50,
        persist=False,
        lineage="v2",
    )
    summary = module.summarize_for_report(result)

    assert result["evaluation"]["lineage"] == "v2"
    assert set(result["strategies"]) == set(module.backtest.STRATEGY_DIRECTIONS_V2)
    assert summary["verdict"] == "pending_real_data_run"
    assert result["research_protocol"]["lineage"] == "v2"


def test_v2_lineage_output_paths_are_separate_from_legacy():
    module = load_module()
    legacy_output, legacy_dir = module._output_paths("legacy")
    v2_output, v2_dir = module._output_paths("v2")

    assert legacy_output != v2_output
    assert legacy_dir != v2_dir
    assert legacy_output == module.OUTPUT_FILE
    assert v2_output == module.OUTPUT_FILE_V2


def test_run_evaluation_persists_evidence_and_output_file(tmp_path, monkeypatch):
    module = load_module()
    output_file = tmp_path / "gate_evaluation_latest.json"
    artifact_dir = tmp_path / "evidence"
    monkeypatch.setattr(module, "OUTPUT_FILE", str(output_file))
    monkeypatch.setattr(module, "ARTIFACT_DIR", str(artifact_dir))

    result = module.run_evaluation(
        mode="synthetic",
        split_date="2024-07-01",
        start_date="2024-01-01",
        min_oos_samples=3,
        n_perm=50,
        persist=True,
    )

    for strategy_id, item in result["strategies"].items():
        evidence = item.get("evidence") or {}
        assert evidence.get("artifact"), strategy_id
        assert Path(evidence["artifact"]).is_file()
        artifact = json.loads(Path(evidence["artifact"]).read_text(encoding="utf-8"))
        assert artifact["strategy_id"] == strategy_id
