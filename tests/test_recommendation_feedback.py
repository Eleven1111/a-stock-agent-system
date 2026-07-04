"""推荐反馈回流 CLI 测试（record/list，纯逻辑，不触网）。"""

import signal_ledger
from scripts import recommendation_feedback as rf


def _open_signal(ledger_file, signal_id, *, strategy_id="daban:first_board_reseal", source="recommendation"):
    links = signal_ledger.make_links(f"rec-{signal_id}", signal_id=signal_id)
    event = signal_ledger.signal_opened_event(
        {
            "code": "002156",
            "name": "通富微电",
            "signal_date": "2026-06-12",
            "entry_price": 11.0,
            "grade": "A",
            "strategy_id": strategy_id,
            "action": "buy",
            "source": source,
        },
        links,
    )
    signal_ledger.append_events([event], ledger_file=ledger_file)
    return links


def test_record_feedback_appends_event(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    _open_signal(ledger_file, "sig-1")

    result = rf.record_feedback(
        signal_id="sig-1",
        verdict="useful",
        note="很准",
        ledger_file=ledger_file,
    )

    assert result["ok"] is True
    events = signal_ledger.read_events(ledger_file)
    feedback_events = [e for e in events if e["event_type"] == "recommendation.feedback"]
    assert len(feedback_events) == 1
    assert feedback_events[0]["payload"]["verdict"] == "useful"
    assert feedback_events[0]["payload"]["note"] == "很准"
    assert feedback_events[0]["links"]["signal_id"] == "sig-1"


def test_record_feedback_rejects_invalid_verdict(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    _open_signal(ledger_file, "sig-1")

    result = rf.record_feedback(signal_id="sig-1", verdict="maybe", ledger_file=ledger_file)

    assert "error" in result
    events = signal_ledger.read_events(ledger_file)
    assert [e["event_type"] for e in events] == ["signal.opened"]


def test_record_feedback_requires_known_signal(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")

    result = rf.record_feedback(signal_id="does-not-exist", verdict="useful", ledger_file=ledger_file)

    assert "error" in result


def test_record_feedback_allows_multiple_submissions_for_same_signal(tmp_path):
    """反馈可能被更正/追加，每次都是新事实，不做同 signal_id 去重。"""
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    _open_signal(ledger_file, "sig-1")

    rf.record_feedback(signal_id="sig-1", verdict="useful", ledger_file=ledger_file)
    rf.record_feedback(signal_id="sig-1", verdict="not_useful", note="后来发现是假突破", ledger_file=ledger_file)

    events = signal_ledger.read_events(ledger_file)
    feedback_events = [e for e in events if e["event_type"] == "recommendation.feedback"]
    assert len(feedback_events) == 2


def test_list_feedback_returns_all_recorded(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    _open_signal(ledger_file, "sig-1")
    _open_signal(ledger_file, "sig-2")
    rf.record_feedback(signal_id="sig-1", verdict="useful", ledger_file=ledger_file)
    rf.record_feedback(signal_id="sig-2", verdict="not_useful", ledger_file=ledger_file)

    listing = rf.list_feedback(ledger_file=ledger_file)

    assert len(listing) == 2
    assert {row["signal_id"] for row in listing} == {"sig-1", "sig-2"}
    assert {row["verdict"] for row in listing} == {"useful", "not_useful"}


def test_list_feedback_empty_when_none_recorded(tmp_path):
    ledger_file = str(tmp_path / "signal_ledger.jsonl")
    assert rf.list_feedback(ledger_file=ledger_file) == []
