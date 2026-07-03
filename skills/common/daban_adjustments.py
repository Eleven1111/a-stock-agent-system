"""打板策略三项调整机制（upgrade-plan v2 §7b）— 全部 config-gated，默认关闭。

启用任何一项都必须先引用部署机上 scripts/strategy_attribution_report.py 的
分层归因数据（按市场温度/主题阶段/板位/入场模式的 T+1 溢价分布），本模块只
提供杠杆，不做启用决策：

1. regime_gate            — 环境门禁：温度不足或主题退潮时打板候选降级 research_only
2. entry_mode_weights     — 入场模式权重：按 pattern 调整候选闸门评分
3. auction_premium_exit   — T+1 竞价溢价套利退出：溢价达标即挂卖出计划

配置在 config/daban_thresholds.yaml 的 ``adjustments`` 小节（daban_config
统一加载，缺失回退 DEFAULTS，行为与未配置时完全一致）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

try:
    from .daban_config import section as _daban_section
except ImportError:  # pragma: no cover - script-style sys.path imports
    from daban_config import section as _daban_section


ADJUSTMENT_DEFAULTS: dict[str, dict[str, Any]] = {
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
}


def _merged(name: str, override: Mapping[str, Any] | None) -> dict[str, Any]:
    base = dict(ADJUSTMENT_DEFAULTS[name])
    if override is None:
        try:
            adjustments = _daban_section("adjustments")
        except Exception:  # noqa: BLE001 - config load must never break callers
            adjustments = {}
        override = (adjustments or {}).get(name) or {}
    for key, value in dict(override).items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key] = {**base[key], **dict(value)}
        else:
            base[key] = value
    return base


def regime_gate_assessment(
    *,
    temperature_score: float | None,
    theme_stage: str | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """环境门禁判定。返回 {enabled, blocked, reasons}。

    fail-closed：门禁启用时温度数据缺失视为"无法确认环境健康"，阻断交付
    （数据缺失不得当中性证据）。主题阶段缺失（标的不属于任何活跃主题）不
    构成阻断——无主题不等于主题退潮。
    """
    cfg = _merged("regime_gate", config)
    if not cfg.get("enabled"):
        return {"enabled": False, "blocked": False, "reasons": []}
    reasons: list[str] = []
    min_score = float(cfg.get("min_temperature_score") or 0.0)
    if temperature_score is None:
        reasons.append("regime_gate: 市场温度不可用，无法确认环境健康")
    elif float(temperature_score) < min_score:
        reasons.append(
            f"regime_gate: 市场温度 {float(temperature_score):.0f} < {min_score:.0f}"
        )
    blocked_stages = {str(s) for s in cfg.get("blocked_theme_stages") or []}
    if theme_stage and str(theme_stage) in blocked_stages:
        reasons.append(f"regime_gate: 所属主题处于 {theme_stage} 阶段")
    return {"enabled": True, "blocked": bool(reasons), "reasons": reasons}


def entry_mode_multiplier(
    pattern: str | None,
    config: Mapping[str, Any] | None = None,
) -> float:
    """入场模式评分乘数。未启用/未知模式恒为 1.0。"""
    cfg = _merged("entry_mode_weights", config)
    if not cfg.get("enabled") or not pattern:
        return 1.0
    weights = cfg.get("weights") or {}
    try:
        return max(0.0, float(weights.get(str(pattern), 1.0)))
    except (TypeError, ValueError):
        return 1.0


def check_auction_premium_exit(
    *,
    entry_date: date | str | None,
    open_premium_pct: float | None,
    asof: date | str | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """T+1 竞价溢价套利退出信号（exit_signals 契约形状）。

    仅在 T+1 当日（entry_date 恰为 asof 前一交易日或更早且非当日）触发；
    T+0 不允许卖出（A 股 T+1 铁律由调用方与本检查双重保证）。
    """
    signal = {"triggered": False, "signal_type": "auction_premium_exit"}
    cfg = _merged("auction_premium_exit", config)
    if not cfg.get("enabled") or open_premium_pct is None:
        return signal
    entry = _to_date(entry_date)
    today = _to_date(asof) or date.today()
    if entry is None or entry >= today:
        return signal
    premium = float(open_premium_pct)
    full_at = float(cfg.get("full_exit_premium_pct") or 6.0)
    min_at = float(cfg.get("min_premium_pct") or 3.0)
    if premium >= full_at:
        return {
            "triggered": True,
            "signal_type": "auction_premium_exit",
            "severity": "critical",
            "reason": f"T+1竞价溢价{premium:.1f}%达全额套利线{full_at:.1f}%",
            "action": "sell",
        }
    if premium >= min_at:
        return {
            "triggered": True,
            "signal_type": "auction_premium_exit",
            "severity": "warning",
            "reason": f"T+1竞价溢价{premium:.1f}%达套利线{min_at:.1f}%",
            "action": "sell",
        }
    return signal


def _to_date(value: date | str | None) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None
