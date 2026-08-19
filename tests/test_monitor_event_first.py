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
    # 篡改绕过了本模块的事务（等价于另一个进程改了状态文件），而账本重放校验
    # 的定位是进程启动时的完整性守卫；重置缓存即模拟下一个进程首次访问。
    registry.reset_verification_cache()

    try:
        registry.load_registry()
    except RuntimeError as exc:
        assert "projection" in str(exc)
    else:
        raise AssertionError("tampered monitor projection must fail closed")


def _replay_ledger(events, *, registry_path, checkpoint_path, batched):
    """跑一次重放，返回落盘后的注册表内容。"""
    import event_projection

    registry_path.write_text(
        json.dumps([
            # 账本之外的既有记录（candidate_discovery 写的），折叠必须保住它
            {"id": "outsider", "kind": "stock", "key": "000001", "label": "场外",
             "status": "active", "source": "candidate_discovery", "extra": "keep-me"},
        ], ensure_ascii=False),
        encoding="utf-8",
    )
    kwargs = {}
    if batched:
        kwargs["batch_projector"] = registry._project_monitor_events
    result = event_projection.replay_events(
        events,
        projectors=[registry._project_monitor_event],
        checkpoint_file=str(checkpoint_path),
        **kwargs,
    )
    assert result["status"] == "ok", result
    return json.loads(registry_path.read_text(encoding="utf-8"))


def test_batched_cold_replay_is_byte_identical_to_per_event_replay(tmp_path, monkeypatch):
    """等价性用差分证明，不拿「测试全绿」当行为不变。

    冷重放从每事件一次落盘改成整批一次落盘（issue #167 的 O(事件 x 记录)）。
    折叠顺序、merge-don't-replace 语义、账本外既有记录的保留，全部必须逐字节一致
    —— 校验函数正是按字段比对注册表的，任何偏差都会变成 fail-closed 误报。
    """
    events = []
    sequence = 0
    for round_index in range(4):
        for entity in range(25):
            sequence += 1
            monitor_id = f"monitor-{entity:03d}"
            events.append({
                "schema": "signal_ledger_event_v2",
                "sequence": sequence,
                "event_id": f"ev-{sequence}",
                # 后面的轮次改状态，制造 merge 而不是新增
                "event_type": "monitor.cancelled" if round_index == 3 and entity % 3 == 0
                else "monitor.activated",
                "links": {"monitor_id": monitor_id, "correlation_id": f"c-{entity}"},
                "payload": {"entry": {
                    "id": monitor_id, "kind": "stock", "key": f"{600000 + entity:06d}",
                    "label": f"标的{entity}-r{round_index}",
                    "status": "cancelled" if round_index == 3 and entity % 3 == 0 else "active",
                    "source": "bench",
                }},
            })
        # 夹杂非 monitor 事件，两条路径都必须原样跳过
        sequence += 1
        events.append({
            "schema": "signal_ledger_event_v2", "sequence": sequence,
            "event_id": f"ev-{sequence}", "event_type": "cash.deposited",
            "links": {"correlation_id": "c-cash"}, "payload": {"amount": 1},
        })

    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "per_event.json"))
    per_event = _replay_ledger(
        events,
        registry_path=tmp_path / "per_event.json",
        checkpoint_path=tmp_path / "ckpt_per_event.json",
        batched=False,
    )

    monkeypatch.setattr(registry, "REGISTRY_FILE", str(tmp_path / "batched.json"))
    batched = _replay_ledger(
        events,
        registry_path=tmp_path / "batched.json",
        checkpoint_path=tmp_path / "ckpt_batched.json",
        batched=True,
    )

    assert batched == per_event
    assert json.dumps(batched, sort_keys=False) == json.dumps(per_event, sort_keys=False)
    # 账本外的记录与其额外字段必须留着
    assert batched[0]["id"] == "outsider"
    assert batched[0]["extra"] == "keep-me"
    # 最后一轮的取消确实落到了投影上
    assert any(item["status"] == "cancelled" for item in batched)


def test_cold_replay_writes_the_registry_once_not_once_per_event(tmp_path, monkeypatch):
    """成本证据：写次数必须与事件数解耦。"""
    _wire(tmp_path, monkeypatch)
    events = [
        {
            "schema": "signal_ledger_event_v2", "sequence": seq, "event_id": f"ev-{seq}",
            "event_type": "monitor.activated",
            "links": {"monitor_id": f"monitor-{seq:03d}", "correlation_id": f"c-{seq}"},
            "payload": {"entry": {
                "id": f"monitor-{seq:03d}", "kind": "stock", "key": f"{600000 + seq:06d}",
                "label": f"标的{seq}", "status": "active", "source": "bench",
            }},
        }
        for seq in range(1, 51)
    ]
    writes = []
    real_mutate = registry.mutate_json
    monkeypatch.setattr(
        registry,
        "mutate_json",
        lambda path, *args, **kwargs: (
            writes.append(str(path)) or real_mutate(path, *args, **kwargs)
        ),
    )

    registry._recover_registry_projection(events)

    registry_writes = [path for path in writes if path.endswith("registry.json")]
    assert len(registry_writes) == 1, f"50 个事件写了 {len(registry_writes)} 次注册表"
