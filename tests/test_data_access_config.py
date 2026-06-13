"""Central data-access configuration keeps historical defaults compatible."""

import json

import data_access_config as config


def test_missing_config_uses_historical_defaults(tmp_path):
    loaded = config.load_config(tmp_path / "missing.json")

    assert loaded["providers"]["tencent"] == {"timeout_seconds": 10, "max_attempts": 2}
    assert loaded["risk"]["stop_loss_pct"] == -8.0
    assert loaded["risk"]["max_single_position_pct"] == 25
    assert loaded["news_monitor"]["default_limit"] == 3
    assert len(loaded["news_monitor"]["queries"]) == 4
    assert loaded["storage"]["snapshot_input_retention_days"] == 30
    assert loaded["storage"]["snapshot_max_total_mb"] == 4096


def test_partial_config_deep_merges_without_losing_defaults(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "providers": {"tencent": {"timeout_seconds": 4}},
            "risk": {"stop_loss_pct": -6.5},
            "news_monitor": {"queries": ["自定义查询"]},
        }),
        encoding="utf-8",
    )

    loaded = config.load_config(path)

    assert loaded["providers"]["tencent"] == {"timeout_seconds": 4, "max_attempts": 2}
    assert loaded["providers"]["serpapi"]["timeout_seconds"] == 15
    assert loaded["risk"]["stop_loss_pct"] == -6.5
    assert loaded["risk"]["portfolio_size"] == 100000
    assert loaded["news_monitor"]["queries"] == ["自定义查询"]
    assert loaded["news_monitor"]["default_limit"] == 3


def test_invalid_values_fall_back_to_safe_defaults(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "providers": {
                "tencent": {"timeout_seconds": -1, "max_attempts": 99},
                "serpapi": "bad",
            },
            "risk": {"stop_loss_pct": "bad", "portfolio_size": 0},
            "intraday_monitor": {"surge_pct": -5},
            "news_monitor": {"default_limit": 0, "queries": ["", 123]},
            "storage": {
                "snapshot_input_retention_days": 0,
                "snapshot_min_keep_per_dataset": -1,
                "snapshot_max_total_mb": "bad",
            },
        }),
        encoding="utf-8",
    )

    loaded = config.load_config(path)

    assert loaded["providers"]["tencent"] == {"timeout_seconds": 10, "max_attempts": 2}
    assert loaded["providers"]["serpapi"] == {"timeout_seconds": 15, "max_attempts": 2}
    assert loaded["risk"]["stop_loss_pct"] == -8.0
    assert loaded["risk"]["portfolio_size"] == 100000
    assert loaded["intraday_monitor"]["surge_pct"] == 5.0
    assert loaded["news_monitor"]["default_limit"] == 3
    assert loaded["news_monitor"]["queries"] == config.DEFAULTS["news_monitor"]["queries"]
    assert loaded["storage"] == config.DEFAULTS["storage"]


def test_invalid_top_level_sections_fall_back_without_crashing(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "providers": "bad",
            "risk": [],
            "intraday_monitor": None,
            "news_monitor": 123,
            "global_market": "bad",
            "storage": "bad",
        }),
        encoding="utf-8",
    )

    loaded = config.load_config(path)

    assert loaded["providers"]["tencent"]["timeout_seconds"] == 10
    assert loaded["risk"]["portfolio_size"] == 100000
    assert loaded["intraday_monitor"]["surge_pct"] == 5.0
    assert loaded["news_monitor"]["default_limit"] == 3
    assert loaded["global_market"]["switches"]["yfinance"] is True
    assert loaded["storage"]["cron_artifact_retention_days"] == 30


def test_storage_settings_are_independently_copied(tmp_path):
    settings = config.storage_settings(tmp_path / "missing.json")

    settings["snapshot_input_retention_days"] = 1

    assert config.storage_settings(tmp_path / "missing.json")[
        "snapshot_input_retention_days"
    ] == 30


def test_global_market_settings_are_sanitized_and_deep_copied(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "global_market": {
                "switches": {"serpapi": False},
                "thresholds": {
                    "vix_extreme": 31,
                    "key_stock_move_notable": -1,
                },
                "a_share_sector_stock_map": {
                    "AI算力": [["000977", "浪潮信息"]],
                },
            },
        }),
        encoding="utf-8",
    )

    settings = config.global_market_settings(path)

    assert settings["switches"]["serpapi"] is False
    assert settings["switches"]["yfinance"] is True
    assert settings["thresholds"]["vix_extreme"] == 31
    assert settings["thresholds"]["key_stock_move_notable"] == 5.0
    assert settings["a_share_sector_stock_map"]["AI算力"] == [["000977", "浪潮信息"]]

    settings["us_indices"].clear()
    assert config.global_market_settings(path)["us_indices"]
