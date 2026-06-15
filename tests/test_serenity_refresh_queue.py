from datetime import date

import serenity_refresh_queue as queue


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
