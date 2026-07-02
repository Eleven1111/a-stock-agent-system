import json

from skills.common import delivery_output


def test_summary_json_stays_valid_and_bounded():
    text = delivery_output.maybe_summarize_json(
        {"status": "ready", "payload": "x" * 1000},
        {
            "schema": "delivery_summary_v1",
            "job_id": "capital-flow",
            "status": "ready",
            "summary": "资金流正常，" * 50,
            "alerts": [],
        },
        job_id="capital-flow",
        has_anomaly=False,
        policy={"summary_mode": {"enabled": True, "mode": "enforce"}},
        max_chars=200,
    )

    parsed = json.loads(text)
    assert len(text) <= 200
    assert parsed["schema"] == "delivery_summary_v1"
    assert parsed["alerts"] == []


def test_summary_shadow_keeps_full_text_and_records_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    full = {"status": "ready", "payload": "x" * 1000}

    text = delivery_output.maybe_summarize_json(
        full,
        {
            "schema": "delivery_summary_v1",
            "job_id": "global-preopen",
            "status": "ready",
            "summary": "无跨市场异常",
            "alerts": [],
        },
        job_id="global-preopen",
        has_anomaly=False,
        policy={"summary_mode": {"enabled": True, "mode": "shadow"}},
    )

    assert json.loads(text)["payload"] == "x" * 1000
    rows = [
        json.loads(line)
        for line in (tmp_path / "cron" / "push_telemetry.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["would_suppress"] is True
    assert rows[-1]["suppression_reason"] == "summary_mode"
