"""Research-only behavioral-drift metrics for the agent itself (游资方法论报告 7.4).

退学炒股的"第二观察者", 用机器变量表达: 治理 Agent 因近期盈亏而漂移风险偏好
(连胜后扩大动作 / 亏损后急于翻本 / 动作频率漂移 / 策略过度单一), 而不是评价个股。

纯函数, 读 signal_ledger.project_signals 的产物。字段缺失的指标降级 None 并在
unavailable 标注, 绝不臆造。one_sided_evidence / horizon_drift 需要 thesis 与检索
日志, 现有账本无此数据, 故明确标注 unavailable —— 符合报告第十章"不假装复刻"。
cron-safe, 纯标准库。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional, Sequence


SCHEMA = "behavior_risk_v1"

DEFAULT_CONFIG: dict[str, Any] = {
    "window_days": 10,
    "baseline_days": 30,
    "win_streak_alert": 4,
    "loss_streak_alert": 3,
    "action_drift_alert": 0.5,   # 近期日均开仓较基线高 50%
    "concentration_alert": 0.5,  # 近窗策略 HHI 阈值
}

# 现有账本不记录 thesis 与检索过程, 这两项无法度量, 明确标注而非臆造。
UNAVAILABLE = ["one_sided_evidence", "horizon_drift"]


def _config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    return {**DEFAULT_CONFIG, **dict(config or {})}


def _parse_date(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def _outcome_class(signal: Mapping[str, Any]) -> Optional[str]:
    outcome = str(signal.get("outcome") or "").lower()
    if outcome in {"win", "profit"}:
        return "win"
    if outcome == "loss":
        return "loss"
    pnl = signal.get("pnl_pct")
    if isinstance(pnl, (int, float)):
        return "win" if pnl > 0 else "loss" if pnl < 0 else None
    return None


def _tail_streak(classes: Sequence[str]) -> tuple[int, Optional[str]]:
    """尾部连续同类的长度与类别。"""
    if not classes:
        return 0, None
    last = classes[-1]
    count = 0
    for cls in reversed(classes):
        if cls == last:
            count += 1
        else:
            break
    return count, last


def assess_behavior_risk(
    signals: Sequence[Mapping[str, Any]],
    *,
    asof: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """从 signal 序列算 Agent 行为漂移。无足够数据的指标降级 None，不臆造。"""
    cfg = _config(config)
    dated = [
        (parsed, dict(s))
        for s in (signals or [])
        if (parsed := _parse_date(s.get("signal_date") or s.get("date")))
    ]
    dated.sort(key=lambda pair: pair[0])
    requested_asof = _parse_date(asof)
    if requested_asof is not None:
        dated = [(day, signal) for day, signal in dated if day <= requested_asof]
    asof_date = requested_asof or (dated[-1][0] if dated else None)

    classes = [cls for _, s in dated if (cls := _outcome_class(s))]
    streak_len, streak_kind = _tail_streak(classes)
    win_streak = streak_len if streak_kind == "win" else 0
    loss_streak = streak_len if streak_kind == "loss" else 0

    action_rate_drift = None
    concentration = None
    if asof_date and dated:
        window_start = asof_date - timedelta(days=int(cfg["window_days"]))
        base_start = asof_date - timedelta(days=int(cfg["baseline_days"]))
        recent = [s for d, s in dated if d > window_start]
        baseline = [s for d, s in dated if base_start < d <= window_start]
        recent_rate = len(recent) / float(cfg["window_days"])
        base_days = float(cfg["baseline_days"]) - float(cfg["window_days"])
        base_rate = (len(baseline) / base_days) if base_days > 0 else 0.0
        if base_rate > 0:
            action_rate_drift = round(recent_rate / base_rate - 1.0, 4)
        recent_strats = [str(s.get("strategy_id") or "") for s in recent]
        if recent_strats:
            counts = Counter(recent_strats)
            total = len(recent_strats)
            concentration = round(sum((c / total) ** 2 for c in counts.values()), 4)

    drift_high = action_rate_drift is not None and action_rate_drift >= float(cfg["action_drift_alert"])
    streak_expansion = bool(win_streak >= int(cfg["win_streak_alert"]) and drift_high)
    loss_recovery_pressure = bool(
        loss_streak >= int(cfg["loss_streak_alert"])
        and action_rate_drift is not None
        and action_rate_drift > 0
    )

    flags: list[str] = []
    score = 0.0
    if streak_expansion:
        flags.append(f"连胜{win_streak}后动作频率上升{action_rate_drift:+.0%}(风险预算非线性扩张)")
        score += 0.4
    if loss_recovery_pressure:
        flags.append(f"连亏{loss_streak}仍在增加出手(急于翻本)")
        score += 0.4
    if drift_high and not streak_expansion:
        flags.append(f"近期动作频率较基线{action_rate_drift:+.0%}")
        score += 0.2
    if concentration is not None and concentration >= float(cfg["concentration_alert"]):
        flags.append(f"策略过度集中(HHI={concentration})")
        score += 0.2

    return {
        "schema": SCHEMA,
        "asof": asof_date.isoformat() if asof_date else None,
        "settled_count": len(classes),
        "signal_count": len(dated),
        "win_streak": win_streak,
        "loss_streak": loss_streak,
        "action_rate_drift": action_rate_drift,
        "streak_expansion": streak_expansion,
        "loss_recovery_pressure": loss_recovery_pressure,
        "strategy_concentration_hhi": concentration,
        "behavior_risk_score": round(min(1.0, score), 4),
        "flags": flags,
        "unavailable": list(UNAVAILABLE),
    }


if __name__ == "__main__":
    import json
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from signal_ledger import project_signals

    print(json.dumps(assess_behavior_risk(project_signals()), ensure_ascii=False, indent=2))
