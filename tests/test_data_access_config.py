"""Central data-access configuration keeps historical defaults compatible."""

import json

import data_access_config as config


def test_missing_config_uses_historical_defaults(tmp_path):
    loaded = config.load_config(tmp_path / "missing.json")

    assert loaded["providers"]["tencent"] == {"timeout_seconds": 10, "max_attempts": 2}
    assert loaded["risk"]["stop_loss_pct"] == -8.0
    assert loaded["risk"]["max_single_position_pct"] == 25
    assert loaded["news_monitor"]["default_limit"] == 3
    assert loaded["news_monitor"]["queries"] == [
        "国务院 发改委 工信部 证监会 A股 产业政策",
        "地缘冲突 制裁 关税 大宗商品 A股 风险",
    ]
    assert loaded["storage"]["snapshot_input_retention_days"] == 30
    assert loaded["storage"]["snapshot_max_total_mb"] == 4096
    assert loaded["providers"]["eastmoney"]["circuit_failure_threshold"] == 3
    assert loaded["providers"]["eastmoney"]["coordination_backend"] == "shared_file"
    assert loaded["providers"]["xueqiu"]["max_attempts"] == 2
    assert loaded["social_attention"]["min_sources_for_boost"] == 2
    assert loaded["social_attention"]["theme_min_confirmed_stocks"] == 1
    assert loaded["social_attention"]["theme_min_attention_score"] == 60.0
    assert loaded["social_attention"]["baidu_enabled"] is False


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
    assert loaded["providers"]["serper"]["timeout_seconds"] == 10
    assert loaded["risk"]["stop_loss_pct"] == -6.5
    assert "portfolio_size" not in loaded["risk"]
    assert loaded["news_monitor"]["queries"] == ["自定义查询"]
    assert loaded["news_monitor"]["default_limit"] == 3
    assert loaded["social_attention"]["candidate_bonus_max"] == 3.0


def test_invalid_values_fall_back_to_safe_defaults(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "providers": {
                "tencent": {"timeout_seconds": -1, "max_attempts": 99},
                "serper": "bad",
                "eastmoney": {
                    "circuit_failure_threshold": 0,
                    "coordination_backend": "memory",
                },
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
    assert loaded["providers"]["serper"] == {"timeout_seconds": 10, "max_attempts": 2}
    assert loaded["providers"]["eastmoney"]["circuit_failure_threshold"] == 3
    assert loaded["providers"]["eastmoney"]["coordination_backend"] == "shared_file"
    assert loaded["risk"]["stop_loss_pct"] == -8.0
    assert "portfolio_size" not in loaded["risk"]
    assert loaded["intraday_monitor"]["surge_pct"] == 5.0
    assert loaded["news_monitor"]["default_limit"] == 3
    assert loaded["news_monitor"]["queries"] == config.DEFAULTS["news_monitor"]["queries"]
    assert loaded["storage"] == config.DEFAULTS["storage"]
    assert loaded["social_attention"] == config.DEFAULTS["social_attention"]


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
    assert "portfolio_size" not in loaded["risk"]
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
                "switches": {"serper": False},
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

    assert settings["switches"]["serper"] is False
    assert settings["switches"]["yfinance"] is True
    assert settings["thresholds"]["vix_extreme"] == 31
    assert settings["thresholds"]["key_stock_move_notable"] == 5.0
    assert settings["a_share_sector_stock_map"]["AI算力"] == [["000977", "浪潮信息"]]

    settings["us_indices"].clear()
    assert config.global_market_settings(path)["us_indices"]


def test_social_attention_settings_are_sanitized(tmp_path):
    path = tmp_path / "data_access.json"
    path.write_text(
        json.dumps({
            "social_attention": {
                "top_limit": 0,
                "min_sources_for_boost": 1,
                "candidate_bonus_max": 99,
                "sentiment_delta_max": -1,
                "theme_min_confirmed_stocks": 0,
                "theme_min_attention_score": 101,
                "baidu_enabled": "yes",
            }
        }),
        encoding="utf-8",
    )

    settings = config.social_attention_settings(path)

    assert settings == config.DEFAULTS["social_attention"]
