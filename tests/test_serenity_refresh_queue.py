from datetime import date

import research_bus as bus
import serenity_refresh_queue as queue


BUS_CONFIG = {
    "claim_ttl_minutes": 120,
    "max_attempts_per_role": 2,
    "budget": {"daily_char_budget": 400000, "instructions_chars_estimate": 3000},
    "task_kinds": {
        "serenity_refresh": {
            "experts": ["deep_researcher"],
            "priority": 75,
            "cooldown_days": 90,
            "pack_budget_chars": 4000,
            "pack_jobs": [],
            "required_sections": [],
        },
    },
    "experts": {"deep_researcher": {"max_output_chars": 3000}},
    "finding": {"max_summary_chars": 600, "max_finding_chars": 10000},
    "synthesis": {
        "veto_confidence": 0.7,
        "conflict_confidence": 0.6,
        "advance_min_support_confidence": 0.6,
        "escalation": {"enabled": False, "max_rounds": 1},
    },
}


def test_plan_refreshes_missing_and_stale_high_priority_targets():
    targets = [
        {"code": "600001", "name": "持仓股", "priority": 100, "source": "portfolio"},
        {"code": "000002", "name": "候选股", "priority": 50, "source": "candidate_pool"},
        {"code": "000003", "name": "新鲜股", "priority": 80, "source": "recommendation"},
    ]
    cache = {
        "600001": None,
        "000002": {"asof": "2026-01-01", "age_days": 100, "stale": True},
        "000003": {"asof": "2026-06-10", "age_days": 3, "stale": False},
    }

    result = queue.plan_refreshes(
        targets,
        cache_lookup=lambda code: cache[code],
        asof="2026-06-13",
        existing=[],
        limit=5,
    )

    assert [item["code"] for item in result["requests"]] == ["600001", "000002"]
    assert result["requests"][0]["reason"] == "missing_cache"
    assert result["requests"][1]["reason"] == "stale_cache"


def test_collect_targets_only_includes_top_five_candidates():
    candidates = [
        {"code": f"{index:06d}", "name": f"候选{index}"}
        for index in range(1, 8)
    ]

    targets = queue.collect_targets(
        portfolio={"positions": []},
        recommendations=[],
        monitors=[],
        candidates=candidates,
    )

    candidate_targets = [
        item for item in targets if item["source"] == "candidate_pool"
    ]
    assert [item["code"] for item in candidate_targets] == [
        "000001",
        "000002",
        "000003",
        "000004",
        "000005",
    ]


def test_collect_targets_respects_manual_cancel_tombstones():
    targets = queue.collect_targets(
        portfolio={"positions": [{"code": "600011", "name": "持仓股"}]},
        recommendations=[
            {
                "code": "600011",
                "name": "推荐股",
                "action": "buy",
                "outcome": "pending",
            }
        ],
        monitors=[],
        candidates=[{"code": "600011", "name": "候选股"}],
        registry=[
            {
                "kind": "stock",
                "key": "600011",
                "status": "cancelled",
                "manual_cancelled": True,
            }
        ],
    )

    assert targets == []


def test_collect_targets_excludes_expired_monitors():
    targets = queue.collect_targets(
        portfolio={"positions": []},
        recommendations=[],
        candidates=[],
        registry=[
            {
                "kind": "stock",
                "key": "600001",
                "label": "已过期",
                "status": "active",
                "expires_at": "2026-06-14",
            }
        ],
        asof=date(2026, 6, 15),
    )

    assert targets == []


def test_plan_is_idempotent_for_pending_or_claimed_requests():
    targets = [{"code": "600001", "name": "持仓股", "priority": 100, "source": "portfolio"}]
    existing = [
        {
            "id": "serenity-600001-2026-06-13",
            "code": "600001",
            "status": "claimed",
        }
    ]

    result = queue.plan_refreshes(
        targets,
        cache_lookup=lambda code: None,
        asof="2026-06-13",
        existing=existing,
    )

    assert result["created"] == 0
    assert result["requests"] == existing


def test_material_event_refreshes_cache_that_predates_event():
    targets = [
        {
            "code": "600001",
            "name": "事件股",
            "priority": 90,
            "source": "recommendation",
            "refresh_after": "2026-06-12",
        }
    ]

    result = queue.plan_refreshes(
        targets,
        cache_lookup=lambda code: {
            "asof": "2026-06-10",
            "age_days": 3,
            "stale": False,
        },
        asof="2026-06-13",
        existing=[],
    )

    assert result["created_requests"][0]["reason"] == "material_event"


def test_claim_and_complete_require_a_real_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setattr(queue, "read_deep_research", lambda code, today=None: None)
    queue.save_queue(
        [
            {
                "id": "serenity-600001-2026-06-13",
                "code": "600001",
                "name": "测试",
                "status": "pending",
                "requested_asof": "2026-06-13",
            }
        ]
    )

    claimed = queue.claim_next("hermes")
    failed = queue.complete_request(claimed["id"])

    assert claimed["status"] == "claimed"
    assert failed["ok"] is False
    assert "cache" in failed["error"]


def test_expired_claim_is_recovered_by_another_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, "QUEUE_FILE", str(tmp_path / "queue.json"))
    queue.save_queue(
        [
            {
                "id": "serenity-600001-2026-06-13",
                "code": "600001",
                "status": "claimed",
                "priority": 100,
                "claimed_by": "hermes",
                "claimed_at": "2026-06-13T10:00:00",
            }
        ]
    )

    claimed = queue.claim_next(
        "openclaw",
        now="2026-06-13T13:00:00",
        claim_ttl_minutes=120,
    )

    assert claimed["claimed_by"] == "openclaw"
    assert claimed["attempts"] == 1


def test_plan_bus_refreshes_enqueues_due_targets_on_the_research_bus(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        queue, "collect_targets",
        lambda **kwargs: [
            {"code": "600001", "name": "持仓股", "priority": 100, "source": "portfolio"},
            {"code": "000002", "name": "候选股", "priority": 50, "source": "candidate_pool"},
        ],
    )
    monkeypatch.setattr(queue, "read_deep_research", lambda code, today=None: None)

    result = queue.plan_bus_refreshes(
        trading_date="2026-07-02", config=BUS_CONFIG,
    )

    assert result["schema"] == "serenity_bus_plan_v1"
    assert result["enqueued"] == 2
    codes = {item["code"] for item in result["results"] if item["enqueued"]}
    assert codes == {"600001", "000002"}
    tasks = bus.load_tasks()
    assert {task["kind"] for task in tasks} == {"serenity_refresh"}
    assert {task["subject"]["code"] for task in tasks} == {"600001", "000002"}


def test_plan_bus_refreshes_is_idempotent_for_active_bus_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(
        queue, "collect_targets",
        lambda **kwargs: [
            {"code": "600001", "name": "持仓股", "priority": 100, "source": "portfolio"},
        ],
    )
    monkeypatch.setattr(queue, "read_deep_research", lambda code, today=None: None)

    first = queue.plan_bus_refreshes(trading_date="2026-07-02", config=BUS_CONFIG)
    second = queue.plan_bus_refreshes(trading_date="2026-07-02", config=BUS_CONFIG)

    assert first["enqueued"] == 1
    assert second["enqueued"] == 0
    assert second["results"][0]["skip_reason"] == "already_active"
    assert len(bus.load_tasks()) == 1
