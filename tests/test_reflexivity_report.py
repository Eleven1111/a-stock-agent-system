import importlib.util
import os


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SPEC = importlib.util.spec_from_file_location(
    "reflexivity_report", os.path.join(ROOT, "scripts", "reflexivity_report.py")
)
report_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(report_module)


def test_report_is_research_only_and_preserves_insufficient_data(monkeypatch):
    monkeypatch.setattr(report_module.la, "available_days", lambda: [])
    monkeypatch.setattr(report_module.la, "load_settled_records", lambda _days: [])

    report = report_module.build_report(round_trip_cost_bps=20)

    assert report["schema"] == "reflexivity_ablation_report_v1"
    assert report["status"] == "insufficient_data"
    assert report["research_only"] is True
    assert report["live_effect"] == "none"


def test_report_passes_frozen_config_hash_to_ablation(monkeypatch):
    captured = []
    monkeypatch.setattr(report_module.la, "available_days", lambda: ["2026-07-13"])
    monkeypatch.setattr(report_module.la, "load_settled_records", lambda _days: [])
    monkeypatch.setattr(
        report_module.la,
        "reflexivity_ablation",
        lambda records, **kwargs: captured.append(kwargs) or {
            "status": "insufficient_data", "research_only": True, "live_effect": "none"
        },
    )

    report_module.build_report(expected_config_sha256="a" * 64)

    assert captured[0]["expected_config_sha256"] == "a" * 64
