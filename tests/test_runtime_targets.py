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
