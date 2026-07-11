import json

import monitor_ledger
import monitor_registry as registry
import signal_ledger


def _wire(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "registry.json"))
    monkeypatch.setattr(registry, "LEDGER_FILE", str(tmp_path / "canonical.jsonl"))
    monkeypatch.setattr(
        registry, "MIRROR_LEDGER_FILE", str(tmp_path / "monitor_mirror.jsonl")
    )
    monkeypatch.setattr(
        registry, "CHECKPOINT_FILE", str(tmp_path / "monitor_checkpoint.json")
    )


def test_monitor_event_is_durable_before_registry_projection(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    order = []
    real_mutate = registry.mutate_json

    def mutate(*args, **kwargs):
        value = real_mutate(*args, **kwargs)
        order.append("projection")
        return value

    def record(*args, **kwargs):
        order.append("event")

    monkeypatch.setattr(registry, "mutate_json", mutate)
    monkeypatch.setattr(registry, "_record_monitor_event", record)
    assert registry.activate("stock", "600001", "示例", source="manual")[
        "changed"
    ] is True
    assert order == ["event", "projection"]


def test_cancel_event_is_durable_before_registry_projection(tmp_path, monkeypatch):
    _wire(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "_record_monitor_event", lambda *a, **k: None)
    registry.activate("stock", "600001", "示例", source="manual")
    registry.load_registry()
    order = []
    real_mutate = registry.mutate_json

    def mutate(*args, **kwargs):
        value = real_mutate(*args, **kwargs)
        order.append("projection")
        return value

    def record(*args, **kwargs):
        order.append("event")

    monkeypatch.setattr(registry, "mutate_json", mutate)
    monkeypatch.setattr(registry, "_record_monitor_event", record)
    assert registry.cancel("stock", "600001", reason="manual")["changed"] is True
    assert order == ["event", "projection"]


def test_monitor_event_enters_canonical_ledger_and_compatibility_mirror(
    tmp_path, monkeypatch
):
    _wire(tmp_path, monkeypatch)

    registry.activate("stock", "600001", "示例", source="manual", force=True)

    canonical = signal_ledger.read_events(registry.LEDGER_FILE)
    mirror = monitor_ledger.read_events(registry.MIRROR_LEDGER_FILE)
    assert [event["event_type"] for event in canonical] == ["monitor.activated"]
    assert canonical[0]["links"]["monitor_id"] == "stock:600001"
    assert [event["event_type"] for event in mirror] == ["monitor.activated"]
    checkpoint = json.loads((tmp_path / "monitor_checkpoint.json").read_text())
    assert checkpoint["sequence"] == canonical[0]["sequence"]


def test_monitor_projection_crash_recovers_from_canonical_ledger_and_keeps_tombstone(
    tmp_path, monkeypatch
):
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    real_mutate = registry.mutate_json

    def crash_after_event(path, mutator, default=None):
        current = registry.read_json(path, default)
        mutator(current)
        raise OSError("projection crash")

    monkeypatch.setattr(registry, "mutate_json", crash_after_event)
    try:
        registry.cancel("stock", "600001", reason="用户取消", manual=True)
    except OSError:
        pass

    assert registry.read_json(registry.REGISTRY_FILE, [])[0]["status"] == "active"
    canonical = signal_ledger.read_events(registry.LEDGER_FILE)
    assert canonical[-1]["event_type"] == "monitor.cancelled"

    monkeypatch.setattr(registry, "mutate_json", real_mutate)
    recovered = registry.load_registry()
    assert recovered[0]["status"] == "cancelled"
    assert recovered[0]["manual_cancelled"] is True
    assert registry.activate(
        "stock", "600001", "示例", source="scheduled_job"
    )["reason"] == "manual_cancel_tombstone"


def test_monitor_reconciliation_fails_closed_after_projection_is_tampered(
    tmp_path, monkeypatch
):
    _wire(tmp_path, monkeypatch)
    registry.activate("stock", "600001", "示例", source="manual", force=True)
    records = registry.read_json(registry.REGISTRY_FILE, [])
    records[0]["status"] = "active-but-tampered"
    registry.mutate_json(registry.REGISTRY_FILE, lambda _old: records, [])

    try:
        registry.load_registry()
    except RuntimeError as exc:
        assert "projection" in str(exc)
    else:
        raise AssertionError("tampered monitor projection must fail closed")
