import json

import pytest

import news_pipeline
import research_bus as bus
from scripts import news_grader


L1_CONFIG = {
    "rank_weight": {"S5": 5},
    "materiality_keywords": {"critical": ["降准"]},
    "min_title_len": 4,
    "pass_threshold_score": 5,
}


@pytest.fixture(autouse=True)
def state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture(autouse=True)
def research_config(tmp_path, monkeypatch):
    config = {
        "task_kinds": {
            "anomaly_review": {
                "experts": ["risk_redteam"],
                "priority": 85,
                "cooldown_days": 1,
                "pack_budget_chars": 16000,
                "pack_jobs": [],
                "required_sections": [],
            },
        },
        "experts": {"risk_redteam": {"max_output_chars": 5000}},
        "budget": {"daily_char_budget": 0, "instructions_chars_estimate": 3000},
    }
    path = tmp_path / "research_committee.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("A_STOCK_RESEARCH_CONFIG", str(path))
    return config


@pytest.fixture(autouse=True)
def quiet_feishu(monkeypatch):
    pushes = []
    monkeypatch.setattr(
        news_grader.feishu_push, "push_text",
        lambda job_id, text, **kwargs: pushes.append((job_id, text)) or {"ok": True},
    )
    return pushes


def _seed_queue(title="央行宣布全面降准0.5个百分点"):
    scored = news_pipeline.score_item({
        "title": title,
        "url": "https://example.gov.cn/a",
        "source_id": "gov_test",
        "source_name": "测试官方源",
        "source_rank": "S5",
    }, L1_CONFIG)
    fresh, _ = news_pipeline.dedupe_items([scored])
    news_pipeline.enqueue_l1_items(fresh)
    return fresh[0]["fingerprint"]


def _run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["news_grader.py", *argv])
    code = news_grader.main()
    return code, json.loads(capsys.readouterr().out.strip())


def test_next_idle_when_queue_empty(monkeypatch, capsys):
    code, output = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    assert code == 0
    assert output["status"] == "idle"


def test_next_emits_bounded_work_order(monkeypatch, capsys):
    fp = _seed_queue()
    code, order = _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    assert code == 0
    assert order["schema"] == "news_grade_work_order_v1"
    assert [item["fingerprint"] for item in order["items"]] == [fp]
    assert order["output_contract"]["materiality_range"] == [0, 3]
    assert order["instructions"].strip()


def test_validate_grades_enforces_contract():
    expected = {"fp1", "fp2"}
    payload = {
        "schema": "news_grade_batch_v1",
        "grades": [
            {"fingerprint": "fp1", "materiality": 5},
            {"fingerprint": "fp-unknown", "materiality": 1},
            {"fingerprint": "fp2", "materiality": True,
             "time_window": "someday"},
        ],
    }
    errors = news_grader._validate_grades(payload, expected)
    text = "\n".join(errors)
    assert "materiality must be an integer in [0,3]" in text
    assert "not in claimed batch" in text
    assert "time_window" in text


def test_submit_rejects_without_claimed_batch(monkeypatch, capsys, tmp_path):
    grades = tmp_path / "grades.json"
    grades.write_text(json.dumps({
        "schema": "news_grade_batch_v1",
        "grades": [{"fingerprint": "invented", "materiality": 1,
                    "affected_sectors": [], "time_window": "unknown",
                    "needs_deep_review": False}],
    }), encoding="utf-8")
    code, result = _run_cli(monkeypatch, capsys, "submit", "--file", str(grades))
    assert code == 2
    assert result["ok"] is False
    assert "no claimed batch" in result["errors"][0]


def test_submit_rejects_invented_fingerprint_against_claimed_batch(
    monkeypatch, capsys, tmp_path,
):
    _seed_queue()
    _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    grades = tmp_path / "grades.json"
    grades.write_text(json.dumps({
        "schema": "news_grade_batch_v1",
        "grades": [{"fingerprint": "invented", "materiality": 1,
                    "affected_sectors": [], "time_window": "unknown",
                    "needs_deep_review": False}],
    }), encoding="utf-8")
    code, result = _run_cli(monkeypatch, capsys, "submit", "--file", str(grades))
    assert code == 2
    assert any("not in claimed batch" in e for e in result["errors"])


def test_submit_grades_and_breaking_bypass(
    monkeypatch, capsys, tmp_path, quiet_feishu,
):
    fp = _seed_queue()
    _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")

    grades = tmp_path / "grades.json"
    grades.write_text(json.dumps({
        "schema": "news_grade_batch_v1",
        "grades": [{
            "fingerprint": fp,
            "materiality": 3,
            "affected_sectors": ["银行", "地产"],
            "time_window": "1-3d",
            "needs_deep_review": True,
        }],
    }), encoding="utf-8")
    code, result = _run_cli(monkeypatch, capsys, "submit", "--file", str(grades))

    assert code == 0
    assert result["ok"] is True
    assert result["graded"] == 1
    assert len(result["breaking_bypass"]) == 1
    assert result["breaking_bypass"][0]["research_enqueue"]["enqueued"] is True

    tasks = bus.load_tasks()
    assert len(tasks) == 1
    assert tasks[0]["kind"] == "anomaly_review"
    assert tasks[0]["reason"] == "news_l2_materiality_3"
    assert len(quiet_feishu) == 1
    assert "[重大]" in quiet_feishu[0][1]
    assert news_pipeline.queue_summary()["by_status"] == {"graded": 1}


def test_submit_low_materiality_skips_bypass(
    monkeypatch, capsys, tmp_path, quiet_feishu,
):
    fp = _seed_queue()
    _run_cli(monkeypatch, capsys, "next", "--worker", "openclaw")
    grades = tmp_path / "grades.json"
    grades.write_text(json.dumps({
        "schema": "news_grade_batch_v1",
        "grades": [{"fingerprint": fp, "materiality": 1,
                    "affected_sectors": [], "time_window": "unknown",
                    "needs_deep_review": False}],
    }), encoding="utf-8")
    code, result = _run_cli(monkeypatch, capsys, "submit", "--file", str(grades))
    assert code == 0
    assert result["breaking_bypass"] == []
    assert bus.load_tasks() == []
    assert quiet_feishu == []
