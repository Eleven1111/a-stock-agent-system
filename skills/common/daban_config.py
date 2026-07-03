#!/usr/bin/env python3
"""
打板阈值读取器 — 单一事实源
============================
实盘候选闸门(daban_candidate_api)与回测引擎(daban_bt_engine)共读 config/daban_thresholds.yaml。
yaml 缺失或字段缺失时回退到 DEFAULTS（与历史硬编码完全一致），保证无配置时行为不变、
现有测试不破坏。

变更纪律：阈值只能在 daban_bt_run → research_gate 通过后改动；实盘表现差走门控停用。
"""

from typing import Any, Dict, Optional

from config_registry import config_path

try:
    import yaml  # type: ignore
except Exception:  # noqa: BLE001
    yaml = None

DEFAULTS: Dict[str, Dict[str, Any]] = {
    "cost": {"commission": 0.00025, "stamp": 0.0005, "slippage": 0.002},
    "auction": {"gap_window_low": -1.0, "gap_window_high": 3.0, "auction_seal_minute": 565},
    "universe": {
        "float_mktcap_min": 1.5e9, "float_mktcap_max": 12.0e9,
        "avg_turnover_20d_min": 2.0e8, "close_prev_min": 4.0, "close_prev_max": 35.0,
        "listed_days_min": 60,
    },
    "first_board_reseal": {
        "first_limitup_latest": "10:30", "open_board_max": 2, "reseal_minutes_max": 15,
        "seal_amount_ratio_min": 0.003, "active_buy_ratio_min": 0.60,
        "big_order_inflow_ratio_min": 0.08, "sector_limitup_min": 3,
    },
    "second_board_weak_to_strong": {
        "auction_gap_low": -1.0, "auction_gap_high": 3.0,
        "first_limitup_latest": "09:45", "sector_companion_min": 2,
    },
    "market_gate": {
        "yday_limitup_index_open_min": -2.0, "broken_rate_first20m_max": 35.0,
        "week_trades_max": 3, "day_loss_pct_stop": -2.0, "week_loss_pct_freeze": -5.0,
        "consecutive_losses_max": 3, "position_time_stop_trading_days": 2,
    },
    # §7b 三项调整机制，默认全部关闭；启用必须引用打板归因报告数据
    # （scripts/strategy_attribution_report.py），杠杆实现见 daban_adjustments.py。
    "adjustments": {
        "regime_gate": {
            "enabled": False,
            "blocked_theme_stages": ["diverging", "fading"],
            "min_temperature_score": 40.0,
        },
        "entry_mode_weights": {
            "enabled": False,
            "weights": {
                "first_board_reseal": 1.0,
                "second_board_weak_to_strong": 1.0,
            },
        },
        "auction_premium_exit": {
            "enabled": False,
            "min_premium_pct": 3.0,
            "full_exit_premium_pct": 6.0,
        },
    },
}


def _config_path() -> str:
    return str(config_path("daban_thresholds"))


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """加载阈值（yaml 覆盖 DEFAULTS）。yaml 缺失/解析失败 → DEFAULTS。"""
    if yaml is None:
        return _deep_merge(DEFAULTS, {})
    p = path or _config_path()
    try:
        with open(p, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):  # type: ignore[attr-defined]
        loaded = {}
    return _deep_merge(DEFAULTS, loaded if isinstance(loaded, dict) else {})


def section(name: str, path: Optional[str] = None) -> Dict[str, Any]:
    """取某一节阈值（带默认）。"""
    return load_config(path).get(name, dict(DEFAULTS.get(name, {})))


if __name__ == "__main__":
    import json
    print(json.dumps(load_config(), ensure_ascii=False, indent=2))
