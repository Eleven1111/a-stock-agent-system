"""Unified configuration for data providers and monitoring defaults."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, Optional

from config_registry import config_path


DEFAULTS: Dict[str, Any] = {
    "providers": {
        "tencent": {"timeout_seconds": 10, "max_attempts": 2},
        "serper": {"timeout_seconds": 10, "max_attempts": 2},
        "sina": {"timeout_seconds": 10, "max_attempts": 2},
        "usgs": {"timeout_seconds": 10, "max_attempts": 2},
        "gdacs": {"timeout_seconds": 10, "max_attempts": 2},
        "eastmoney": {
            "timeout_seconds": 10,
            "max_attempts": 2,
            "minimum_interval_seconds": 1.1,
            "jitter_max_seconds": 0.25,
            "backoff_base_seconds": 0.5,
            "circuit_failure_threshold": 3,
            "circuit_open_seconds": 300,
            "coordination_backend": "shared_file",
            "coordination_timeout_seconds": 30,
            "coordination_stale_seconds": 90,
        },
        "akshare": {"timeout_seconds": 15, "max_attempts": 1},
        "adata": {"timeout_seconds": 15, "max_attempts": 1},
        "eastmoney_datacenter": {"timeout_seconds": 10, "max_attempts": 2},
        "eastmoney_push2_degraded": {"timeout_seconds": 8, "max_attempts": 1},
        "cninfo": {"timeout_seconds": 8, "max_attempts": 2},
        "sse": {"timeout_seconds": 8, "max_attempts": 2},
        "xueqiu": {"timeout_seconds": 10, "max_attempts": 2},
        "baidu_attention": {"timeout_seconds": 10, "max_attempts": 1},
    },
    "risk": {
        "stop_loss_pct": -8.0,
        "take_profit_pct": 20.0,
        "trailing_stop_pct": 5.0,
        "max_single_position_pct": 25,
        "max_sector_exposure_pct": 40,
        "max_correlation": 0.80,
        "max_beta": 1.30,
        "max_style_exposure_pct": 40.0,
        "max_adv_participation_pct": 10.0,
        "max_portfolio_volatility_pct": 25.0,
        "factor_min_coverage": 0.95,
        "factor_max_age_days": 1,
    },
    "storage": {
        "snapshot_input_retention_days": 30,
        "snapshot_output_retention_days": 90,
        "cron_artifact_retention_days": 30,
        "reference_protection_days": 30,
        "snapshot_min_keep_per_dataset": 3,
        "snapshot_max_total_mb": 4096,
        "gc_max_delete_files": 10000,
        "snapshot_cold_archive_enabled": True,
    },
    "intraday_monitor": {
        "limit_move_pct": 9.5,
        "high_turnover_pct": 10.0,
        "surge_pct": 5.0,
        "directional_move_pct": 2.0,
        "quote_batch_size": 60,
        "sector_min_members": 3,
        "sector_min_positive_ratio": 0.67,
        "sector_min_average_pct": 2.5,
        "sector_min_acceleration_pct": 0.8,
    },
    "news_monitor": {
        "default_limit": 5,
        "intraday_limit": 2,
        "freshness_sla_minutes": 180,
        "intraday_freshness_sla_minutes": 10,
        "intraday_candidate_limit": 10,
        "intraday_stock_limit": 10,
        "intraday_theme_limit": 5,
        "query_cache_ttl_seconds": 600,
        "stock_query_cache_ttl_seconds": 1800,
        "intraday_query_budget_seconds": 45,
        "intraday_provider_timeout_seconds": 5,
        "queries": [
            "国务院 发改委 工信部 证监会 A股 产业政策",
            "地缘冲突 制裁 关税 大宗商品 A股 风险",
            "国有资本 央企 增持 回购 再贷款 ETF A股",
        ],
    },
    "social_attention": {
        "top_limit": 200,
        "cache_max_age_hours": 8.0,
        "min_sources_for_boost": 2,
        "candidate_bonus_max": 3.0,
        "sentiment_delta_max": 0.8,
        "theme_min_confirmed_stocks": 1,
        "theme_min_attention_score": 60.0,
        "baidu_enabled": True,
    },
    "provider_health": {
        "window_size": 200,
        "min_samples": 10,
        "open_threshold": 0.5,
        "cooldown_seconds": 300,
        "probe_ttl_seconds": 60,
    },
    "field_chains": {
        "capital_flow": ["akshare", "adata", "eastmoney_datacenter", "eastmoney_push2_degraded", "tencent"],
        "quote": ["akshare", "adata", "eastmoney_datacenter", "eastmoney_push2_degraded", "tencent"],
        "kline": ["akshare", "adata", "tencent", "eastmoney_push2_degraded"],
        "board_quote": ["akshare", "adata", "eastmoney_push2_degraded"],
    },
    "global_market": {
        "switches": {
            "yfinance": True,
            "sina": True,
            "serper": True,
        },
        "us_indices": {
            "^GSPC": {"name": "标普500", "sina_code": "gb_$spx", "weight": "major"},
            "^IXIC": {"name": "纳斯达克", "sina_code": "gb_$ixic", "weight": "major"},
            "^DJI": {"name": "道琼斯", "sina_code": "gb_$dji", "weight": "major"},
            "^RUT": {"name": "罗素2000", "sina_code": None, "weight": "minor"},
        },
        "us_sector_etfs": {
            "XLK": {"name": "科技", "a_impact": ["AI算力", "半导体", "消费电子"]},
            "XLF": {"name": "金融", "a_impact": ["券商金融", "银行"]},
            "XLE": {"name": "能源", "a_impact": ["石油", "煤炭", "新能源"]},
            "XLV": {"name": "医疗健康", "a_impact": ["医药", "医疗"]},
            "XLI": {"name": "工业", "a_impact": ["军工航天", "机械", "汽车"]},
            "XLY": {"name": "可选消费", "a_impact": ["汽车", "家电", "消费"]},
            "XLP": {"name": "必需消费", "a_impact": ["食品饮料", "农业"]},
            "XLB": {"name": "原材料", "a_impact": ["有色", "化工", "钢铁"]},
            "XLRE": {"name": "房地产", "a_impact": ["地产", "建材"]},
            "XLU": {"name": "公用事业", "a_impact": ["电力", "电网"]},
        },
        "global_indices": {
            "^N225": {"name": "日经225", "region": "asia"},
            "^KS11": {"name": "韩国KOSPI", "region": "asia"},
            "^HSI": {"name": "恒生指数", "region": "asia"},
            "^GDAXI": {"name": "德国DAX", "region": "europe"},
            "^FTSE": {"name": "英国富时100", "region": "europe"},
            "^FCHI": {"name": "法国CAC40", "region": "europe"},
        },
        "commodities": {
            "GC=F": {"name": "黄金", "unit": "美元/盎司", "a_impact": ["黄金", "贵金属"]},
            "SI=F": {"name": "白银", "unit": "美元/盎司", "a_impact": ["贵金属", "有色"]},
            "HG=F": {"name": "铜", "unit": "美元/磅", "a_impact": ["有色", "电网", "新能源"]},
            "CL=F": {"name": "原油WTI", "unit": "美元/桶", "a_impact": ["石油", "石化", "航空", "交运"]},
            "NG=F": {"name": "天然气", "unit": "美元/MMBtu", "a_impact": ["天然气", "化工"]},
            "ZC=F": {"name": "玉米", "unit": "美分/蒲式耳", "a_impact": ["农业", "养殖"]},
            "ZS=F": {"name": "大豆", "unit": "美分/蒲式耳", "a_impact": ["农业", "养殖"]},
            "ZW=F": {"name": "小麦", "unit": "美分/蒲式耳", "a_impact": ["农业", "食品"]},
        },
        "fx": {
            "CNY=X": {
                "name": "美元/人民币",
                "a_impact_up": ["外贸", "家电", "纺织"],
                "a_impact_down": ["航空", "造纸"],
            },
            "DX-Y.NYB": {"name": "美元指数", "a_impact": ["有色", "黄金", "大宗商品"]},
        },
        "key_stocks": {
            "NVDA": {"name": "英伟达", "a_impact": ["AI算力", "半导体"]},
            "AAPL": {"name": "苹果", "a_impact": ["消费电子", "果链"]},
            "MSFT": {"name": "微软", "a_impact": ["AI算力", "云计算"]},
            "TSLA": {"name": "特斯拉", "a_impact": ["新能源车", "汽车零部件"]},
            "AMD": {"name": "AMD", "a_impact": ["半导体", "AI算力"]},
            "SMCI": {"name": "超微电脑", "a_impact": ["AI算力", "服务器"]},
        },
        "china_adrs": {
            "BABA": {"name": "阿里巴巴", "a_impact": ["互联网", "电商", "云计算"]},
            "JD": {"name": "京东", "a_impact": ["电商", "物流"]},
            "PDD": {"name": "拼多多", "a_impact": ["电商", "消费"]},
            "BIDU": {"name": "百度", "a_impact": ["AI", "互联网"]},
            "NIO": {"name": "蔚来", "a_impact": ["新能源车"]},
        },
        "vix_ticker": "^VIX",
        "treasury_tickers": {
            "^TNX": {"name": "10年期美债", "a_impact": ["科技", "成长股", "金融"]},
            "2YY": {"name": "2年期美债", "format": "2YY=F"},
        },
        "thresholds": {
            "vix_fear": 25,
            "vix_extreme": 30,
            "index_move_notable": 1.0,
            "index_move_major": 2.0,
            "oil_move_notable": 3.0,
            "gold_move_notable": 2.0,
            "yield_move_notable": 5,
            "fx_move_notable": 0.5,
            "adr_move_notable": 3.0,
            "technology_spread_notable": 1.0,
            "key_stock_move_notable": 5.0,
            "copper_move_notable": 2.0,
            "sector_etf_move_notable": 2.0,
            "sector_etf_move_major": 3.0,
            "global_index_move_notable": 2.0,
            "summary_move_notable": 1.0,
        },
        "a_share_sector_stock_map": {
            "AI算力": [["000977", "浪潮信息"], ["603019", "中科曙光"]],
            "半导体": [["002371", "北方华创"], ["688981", "中芯国际"]],
            "消费电子": [["002475", "立讯精密"], ["002241", "歌尔股份"]],
            "黄金": [["600547", "山东黄金"], ["600489", "中金黄金"]],
            "贵金属": [["600547", "山东黄金"], ["600489", "中金黄金"]],
            "石油": [["601857", "中国石油"], ["600938", "中国海油"]],
            "石化": [["600028", "中国石化"], ["600346", "恒力石化"]],
            "航空": [["601111", "中国国航"], ["600029", "南方航空"]],
            "电力": [["600900", "长江电力"], ["600011", "华能国际"]],
            "银行": [["600036", "招商银行"], ["601398", "工商银行"]],
            "军工": [["600760", "中航沈飞"], ["600893", "航发动力"]],
            "新能源": [["300750", "宁德时代"], ["601012", "隆基绿能"]],
            "新能源车": [["002594", "比亚迪"], ["601127", "赛力斯"]],
            "互联网": [["601360", "三六零"], ["300418", "昆仑万维"]],
            "电商": [["002315", "焦点科技"], ["300792", "壹网壹创"]],
            "有色": [["601899", "紫金矿业"], ["603993", "洛阳钼业"]],
            "电网": [["600406", "国电南瑞"], ["000400", "许继电气"]],
            "交运": [["601816", "京沪高铁"], ["601006", "大秦铁路"]],
            "化工": [["600309", "万华化学"], ["600426", "华鲁恒升"]],
            "家电": [["000333", "美的集团"], ["000651", "格力电器"]],
            "造纸": [["002078", "太阳纸业"], ["600567", "山鹰国际"]],
        },
    },
}

CONFIG_FILE = config_path("data_access")


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _number(value: Any, default: float, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if positive and number <= 0:
        return default
    return number


def _sanitize(config: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(config)
    for section in DEFAULTS:
        if not isinstance(result.get(section), dict):
            result[section] = copy.deepcopy(DEFAULTS[section])

    for name, defaults in DEFAULTS["providers"].items():
        raw = result.get("providers", {}).get(name)
        raw = raw if isinstance(raw, dict) else {}
        result["providers"][name] = {
            "timeout_seconds": _number(
                raw.get("timeout_seconds"),
                defaults["timeout_seconds"],
                positive=True,
            ),
            "max_attempts": (
                int(raw["max_attempts"])
                if isinstance(raw.get("max_attempts"), int)
                and not isinstance(raw.get("max_attempts"), bool)
                and 1 <= raw["max_attempts"] <= 2
                else defaults["max_attempts"]
            ),
        }
        if name == "eastmoney":
            eastmoney = result["providers"][name]
            for key in (
                "minimum_interval_seconds",
                "backoff_base_seconds",
                "circuit_open_seconds",
                "coordination_timeout_seconds",
                "coordination_stale_seconds",
            ):
                eastmoney[key] = _number(
                    raw.get(key),
                    defaults[key],
                    positive=True,
                )
            eastmoney["jitter_max_seconds"] = max(
                0.0,
                _number(
                    raw.get("jitter_max_seconds"),
                    defaults["jitter_max_seconds"],
                ),
            )
            threshold = raw.get("circuit_failure_threshold")
            eastmoney["circuit_failure_threshold"] = (
                threshold
                if isinstance(threshold, int)
                and not isinstance(threshold, bool)
                and threshold > 0
                else defaults["circuit_failure_threshold"]
            )
            backend = raw.get("coordination_backend")
            eastmoney["coordination_backend"] = (
                backend
                if backend == "shared_file"
                else defaults["coordination_backend"]
            )

    risk = result["risk"]
    # Legacy static capital is unsafe: runtime portfolio.json is authoritative.
    risk.pop("portfolio_size", None)
    for key, default in DEFAULTS["risk"].items():
        positive = key != "stop_loss_pct"
        risk[key] = _number(risk.get(key), default, positive=positive)

    storage = result["storage"]
    for key, default in DEFAULTS["storage"].items():
        value = storage.get(key)
        if key == "snapshot_min_keep_per_dataset":
            storage[key] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 0
                else default
            )
        elif key == "snapshot_max_total_mb":
            storage[key] = _number(value, default, positive=True)
        elif key == "snapshot_cold_archive_enabled":
            storage[key] = value if isinstance(value, bool) else default
        else:
            storage[key] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
                else default
            )

    intraday = result["intraday_monitor"]
    for key, default in DEFAULTS["intraday_monitor"].items():
        intraday[key] = _number(intraday.get(key), default, positive=True)

    news = result["news_monitor"]
    for key in (
        "default_limit",
        "intraday_limit",
        "freshness_sla_minutes",
        "intraday_freshness_sla_minutes",
        "intraday_candidate_limit",
        "intraday_stock_limit",
        "intraday_theme_limit",
        "query_cache_ttl_seconds",
        "stock_query_cache_ttl_seconds",
        "intraday_query_budget_seconds",
        "intraday_provider_timeout_seconds",
    ):
        value = news.get(key)
        default = DEFAULTS["news_monitor"][key]
        news[key] = (
            value
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else default
        )
    queries = news.get("queries")
    if not isinstance(queries, list) or not queries or any(
        not isinstance(item, str) or not item.strip()
        for item in queries
    ):
        news["queries"] = list(DEFAULTS["news_monitor"]["queries"])
    else:
        news["queries"] = [item.strip() for item in queries]

    social = result["social_attention"]
    top_limit = social.get("top_limit")
    social["top_limit"] = (
        top_limit
        if isinstance(top_limit, int)
        and not isinstance(top_limit, bool)
        and 20 <= top_limit <= 500
        else DEFAULTS["social_attention"]["top_limit"]
    )
    social["cache_max_age_hours"] = _number(
        social.get("cache_max_age_hours"),
        DEFAULTS["social_attention"]["cache_max_age_hours"],
        positive=True,
    )
    min_sources = social.get("min_sources_for_boost")
    social["min_sources_for_boost"] = (
        min_sources
        if isinstance(min_sources, int)
        and not isinstance(min_sources, bool)
        and 2 <= min_sources <= 3
        else DEFAULTS["social_attention"]["min_sources_for_boost"]
    )
    for key, upper in (("candidate_bonus_max", 3.0), ("sentiment_delta_max", 0.8)):
        value = social.get(key)
        parsed = _number(value, -1)
        social[key] = (
            parsed
            if 0 < parsed <= upper
            else DEFAULTS["social_attention"][key]
        )
    theme_min = social.get("theme_min_confirmed_stocks")
    social["theme_min_confirmed_stocks"] = (
        theme_min
        if isinstance(theme_min, int)
        and not isinstance(theme_min, bool)
        and 1 <= theme_min <= 5
        else DEFAULTS["social_attention"]["theme_min_confirmed_stocks"]
    )
    theme_score = _number(
        social.get("theme_min_attention_score"),
        DEFAULTS["social_attention"]["theme_min_attention_score"],
    )
    social["theme_min_attention_score"] = (
        theme_score
        if 0 < theme_score <= 100
        else DEFAULTS["social_attention"]["theme_min_attention_score"]
    )
    social["baidu_enabled"] = (
        social.get("baidu_enabled")
        if isinstance(social.get("baidu_enabled"), bool)
        else DEFAULTS["social_attention"]["baidu_enabled"]
    )

    provider_health = result["provider_health"]
    window_size = provider_health.get("window_size")
    provider_health["window_size"] = (
        window_size
        if isinstance(window_size, int) and not isinstance(window_size, bool) and window_size > 0
        else DEFAULTS["provider_health"]["window_size"]
    )
    min_samples = provider_health.get("min_samples")
    provider_health["min_samples"] = (
        min_samples
        if isinstance(min_samples, int) and not isinstance(min_samples, bool) and min_samples > 0
        else DEFAULTS["provider_health"]["min_samples"]
    )
    open_threshold = _number(provider_health.get("open_threshold"), -1)
    provider_health["open_threshold"] = (
        open_threshold
        if 0 < open_threshold <= 1
        else DEFAULTS["provider_health"]["open_threshold"]
    )
    for key in ("cooldown_seconds", "probe_ttl_seconds"):
        provider_health[key] = _number(
            provider_health.get(key),
            DEFAULTS["provider_health"][key],
            positive=True,
        )

    field_chains = result["field_chains"]
    if not isinstance(field_chains, dict):
        field_chains = copy.deepcopy(DEFAULTS["field_chains"])
    else:
        sanitized_chains: Dict[str, Any] = {}
        for data_type, chain in field_chains.items():
            if not isinstance(data_type, str) or not data_type.strip():
                continue
            if not isinstance(chain, list) or not chain:
                continue
            providers = [item for item in chain if isinstance(item, str) and item.strip()]
            if providers:
                sanitized_chains[data_type] = providers
        field_chains = sanitized_chains or copy.deepcopy(DEFAULTS["field_chains"])
    result["field_chains"] = field_chains

    global_market = result["global_market"]
    for section in (
        "switches",
        "us_indices",
        "us_sector_etfs",
        "global_indices",
        "commodities",
        "fx",
        "key_stocks",
        "china_adrs",
        "treasury_tickers",
        "thresholds",
        "a_share_sector_stock_map",
    ):
        if not isinstance(global_market.get(section), dict):
            global_market[section] = copy.deepcopy(DEFAULTS["global_market"][section])

    switches = global_market["switches"]
    for key, default in DEFAULTS["global_market"]["switches"].items():
        if not isinstance(switches.get(key), bool):
            switches[key] = default

    thresholds = global_market["thresholds"]
    for key, default in DEFAULTS["global_market"]["thresholds"].items():
        thresholds[key] = _number(thresholds.get(key), default, positive=True)

    if not isinstance(global_market.get("vix_ticker"), str) or not global_market["vix_ticker"].strip():
        global_market["vix_ticker"] = DEFAULTS["global_market"]["vix_ticker"]
    return result


def load_config(path: Optional[str | Path] = None) -> Dict[str, Any]:
    """Load repository config and preserve historical defaults on any read error."""
    config_path = Path(path) if path is not None else CONFIG_FILE
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    return _sanitize(_deep_merge(DEFAULTS, loaded))


def provider_settings(name: str, path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["providers"].get(name, DEFAULTS["providers"].get(name, {})))


def risk_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["risk"])


def storage_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["storage"])


def intraday_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["intraday_monitor"])


def news_monitor_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    config = dict(load_config(path)["news_monitor"])
    config["queries"] = list(config.get("queries") or DEFAULTS["news_monitor"]["queries"])
    return config


def social_attention_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["social_attention"])


def provider_health_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return dict(load_config(path)["provider_health"])


def field_chains_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return copy.deepcopy(load_config(path)["field_chains"])


def global_market_settings(path: Optional[str | Path] = None) -> Dict[str, Any]:
    return copy.deepcopy(load_config(path)["global_market"])
