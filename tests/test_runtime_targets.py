from datetime import date

import runtime_targets


def test_runtime_targets_merge_positions_and_active_monitors_without_duplicates():
    targets = runtime_targets.build_stock_targets(
        portfolio={
            "positions": [
                {"code": "600001", "name": "持仓股"},
            ]
        },
        registry=[
            {
                "id": "stock:600001",
                "kind": "stock",
                "key": "600001",
                "label": "监控名称",
                "status": "active",
            },
            {
                "id": "stock:000002",
                "kind": "stock",
                "key": "000002",
                "label": "主动监控",
                "status": "active",
            },
        ],
    )

    assert targets == [
        {"code": "600001", "name": "持仓股", "source": "portfolio"},
        {"code": "000002", "name": "主动监控", "source": "monitor"},
    ]


def test_manual_cancel_tombstone_excludes_stock_from_all_runtime_sources():
    targets = runtime_targets.build_stock_targets(
        portfolio={
            "positions": [
                {"code": "600001", "name": "持仓股"},
            ]
        },
        registry=[
            {
                "id": "stock:600001",
                "kind": "stock",
                "key": "600001",
                "label": "持仓股",
                "status": "cancelled",
                "manual_cancelled": True,
            },
        ],
        candidate_pool={
            "candidates": [
                {"code": "600001", "name": "候选名称"},
            ]
        },
        candidate_limit=5,
    )

    assert targets == []


def test_runtime_topics_only_include_active_sector_and_theme_entries():
    topics = runtime_targets.build_topics(
        registry=[
            {
                "id": "theme:机器人",
                "kind": "theme",
                "key": "机器人",
                "label": "人形机器人",
                "status": "active",
            },
            {
                "id": "sector:煤炭",
                "kind": "sector",
                "key": "煤炭",
                "label": "煤炭",
                "status": "cancelled",
                "manual_cancelled": True,
            },
            {
                "id": "stock:600001",
                "kind": "stock",
                "key": "600001",
                "label": "测试股",
                "status": "active",
            },
        ]
    )

    assert topics == [
        {"kind": "theme", "key": "机器人", "label": "人形机器人"},
    ]


def test_runtime_topics_include_current_dynamic_mainline_when_registry_empty():
    topics = runtime_targets.build_topics(
        registry=[],
        hot_money_selection={
            "asof": "2026-08-14",
            "config": {"mainline_top_n": 2},
            "sectors": [
                {"sector": "通信设备", "rank": 1, "state": "emerging"},
                {"sector": "医药生物", "rank": 3, "state": "neutral"},
            ],
        },
        candidate_pool={
            "asof": "2026-08-14",
            "candidates": [
                {
                    "sector": "电子信息",
                    "sector_rank": 2,
                    "sector_state": "confirmed",
                },
                {
                    "sector": "机械设备",
                    "sector_rank": 4,
                    "sector_state": "neutral",
                },
            ],
        },
        asof=date(2026, 8, 14),
    )

    assert topics == [
        {"kind": "sector", "key": "通信设备", "label": "通信设备"},
        {"kind": "sector", "key": "电子信息", "label": "电子信息"},
    ]


def test_manual_cancelled_topic_is_not_revived_by_dynamic_mainline():
    topics = runtime_targets.build_topics(
        registry=[
            {
                "kind": "sector",
                "key": "通信设备",
                "label": "通信设备",
                "status": "cancelled",
                "manual_cancelled": True,
            },
        ],
        hot_money_selection={
            "asof": "2026-08-14",
            "sectors": [
                {"sector": "通信设备", "rank": 1, "state": "emerging"},
            ],
        },
        candidate_pool={
            "asof": "2026-08-14",
            "candidates": [
                {
                    "sector": "通信设备",
                    "sector_rank": 1,
                    "sector_state": "emerging",
                },
            ],
        },
        asof=date(2026, 8, 14),
    )

    assert topics == []


def test_load_topics_merges_active_registry_with_current_mainline(
    monkeypatch,
):
    monkeypatch.setattr(
        runtime_targets.monitor_registry,
        "load_registry",
        lambda: [
            {
                "kind": "theme",
                "key": "机器人",
                "label": "人形机器人",
                "status": "active",
            },
        ],
    )

    def fake_read_json(path, default):
        if str(path).endswith("hot_money_selection_latest.json"):
            return {
                "asof": "2026-08-14",
                "sectors": [
                    {"sector": "通信设备", "rank": 1, "state": "emerging"},
                ],
            }
        if str(path).endswith("candidate_pool_latest.json"):
            return {"asof": "2026-08-14", "candidates": []}
        return default

    monkeypatch.setattr(runtime_targets, "read_json", fake_read_json)

    assert runtime_targets.load_topics(asof=date(2026, 8, 14)) == [
        {"kind": "theme", "key": "机器人", "label": "人形机器人"},
        {"kind": "sector", "key": "通信设备", "label": "通信设备"},
    ]


def test_current_candidate_pool_embedded_selection_supplies_mainline():
    topics = runtime_targets.build_topics(
        registry=[],
        hot_money_selection={},
        candidate_pool={
            "asof": "2026-08-14",
            "hot_money_selection": {
                "config": {"mainline_top_n": 2},
                "sectors": [
                    {"sector": "电子信息", "rank": 1, "state": "emerging"},
                    {"sector": "机械设备", "rank": 4, "state": "neutral"},
                ],
            },
            "candidates": [],
        },
        asof=date(2026, 8, 14),
    )

    assert topics == [
        {"kind": "sector", "key": "电子信息", "label": "电子信息"},
    ]


def test_expired_or_malformed_registry_entries_are_inactive():
    registry = [
        {
            "kind": "stock",
            "key": "600001",
            "label": "已过期",
            "status": "active",
            "expires_at": "2026-06-14",
        },
        {
            "kind": "stock",
            "key": "000002",
            "label": "日期损坏",
            "status": "active",
            "expires_at": "not-a-date",
        },
    ]

    assert runtime_targets.build_stock_targets(
        registry=registry,
        asof=date(2026, 6, 15),
    ) == []


def test_invalid_zero_stock_code_is_ignored():
    assert runtime_targets.build_stock_targets(
        portfolio={"positions": [{"code": "0", "name": "无效"}]},
    ) == []
