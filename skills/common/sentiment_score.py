"""统一情绪评分 S_t（升级方案 P0-c）— SHADOW ONLY，纯函数。

``market_temperature`` 的五档与 S0-S6 都是离散档位：能说"今天是发酵"，说不出
"今天比昨天好了多少"。S_t 把 ``sentiment_daily`` 的七个指标各自换算成滚动分位后
加权成 0-100 的连续分，并给出 ΔS / Δ²S 与冰点确认谓词，供 P1 校准与 P3 回测使用。

**权重、窗口、分档阈值全部来自 config/scoring.yaml 的 ``sentiment_score`` 节。**
本模块内没有等价的数字默认值：配置缺失即 ``status=unavailable``，而不是回退到一份
影子副本——两份数字并存迟早会分叉，而分叉后没人知道回测用的是哪一份。

Fail-closed 三处：
- 历史长度不足 ``min_history`` 预热 → ``unavailable``，不给 50 分；
- 某个分量字段在窗口内全缺 → 该分量剔除并重新归一化，可用权重低于
  ``min_available_weight`` 时整体 ``unavailable``；
- 冰点确认四条件缺一不成立，任一条件所需数据不可用即判否（缺证据 ≠ 满足）。

输出恒带 ``calibrated=false``：阈值从未用历史收益校准过，P1 之前它不构成任何
实盘含义。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

try:  # config_registry 依赖 PyYAML；缺失时本模块整体 fail-closed，不猜数字。
    from config_registry import config_path
except ImportError:  # pragma: no cover - 环境缺 PyYAML 时的降级路径
    config_path = None  # type: ignore[assignment]


SCHEMA = "sentiment_score_v1"
CONFIG_SECTION = "sentiment_score"

_REQUIRED_KEYS = (
    "quantile_window",
    "min_history",
    "min_available_weight",
    "components",
    "bands",
    "ice_confirm",
)


def load_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """读取 ``config/scoring.yaml`` 的 ``sentiment_score`` 节；缺项返回 None。"""
    if payload is None:
        if config_path is None:
            return None
        try:
            import yaml

            with open(config_path("scoring"), encoding="utf-8") as handle:
                payload = yaml.safe_load(handle)
        except (OSError, ValueError, ImportError):
            return None
    section = (payload or {}).get(CONFIG_SECTION) if isinstance(payload, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    if any(section.get(key) in (None, "", [], {}) for key in _REQUIRED_KEYS):
        return None
    return dict(section)


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "unavailable",
        "calibrated": False,
        "score": None,
        "band": None,
        "delta": None,
        "delta_squared": None,
        "reason": reason,
    }
    result.update(extra)
    return result


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def rolling_quantile(window: Sequence[float], current: float) -> float:
    """当前值在窗口内的经验分位（0-1）。窗口含当前值，调用方保证非空。"""
    return sum(1 for value in window if value <= current) / len(window)


def _component_quantile(
    records: Sequence[Mapping[str, Any]], index: int, field: str, window_size: int
) -> float | None:
    """某个分量在 ``index`` 日的滚动分位。窗口内该字段全缺 → None（不可用）。"""
    current = _numeric(records[index].get(field))
    if current is None:
        return None
    start = max(0, index - window_size + 1)
    window = [
        value for value in
        (_numeric(row.get(field)) for row in records[start:index + 1])
        if value is not None
    ]
    if not window:
        return None
    return rolling_quantile(window, current)


def _band(score: float, bands: Sequence[Mapping[str, Any]]) -> str | None:
    """分档：取 ``min`` 不高于 score 的最高一档。配置未覆盖则返回 None。"""
    ordered = sorted(
        (item for item in bands if isinstance(item, Mapping)),
        key=lambda item: float(item.get("min", 0.0)),
    )
    label = None
    for item in ordered:
        if score >= float(item.get("min", 0.0)):
            label = str(item.get("name") or "") or None
    return label


def score_at(
    records: Sequence[Mapping[str, Any]], index: int, config: Mapping[str, Any]
) -> dict[str, Any]:
    """单日 S_t。``index`` 前的历史不足 ``min_history`` 即不可用（预热 fail-closed）。"""
    window_size = int(config["quantile_window"])
    if index < 0 or index >= len(records):
        return _unavailable("index_out_of_range")
    if index + 1 < int(config["min_history"]):
        return _unavailable(
            "insufficient_history",
            observed_days=index + 1,
            required_days=int(config["min_history"]),
        )
    components: dict[str, Any] = {}
    missing: list[str] = []
    total_weight = 0.0
    weighted = 0.0
    for name, spec in dict(config["components"]).items():
        weight = float(spec.get("weight", 0.0))
        quantile = _component_quantile(records, index, str(spec.get("field") or ""), window_size)
        if quantile is None:
            missing.append(str(name))
            continue
        value = (1.0 - quantile) if bool(spec.get("invert")) else quantile
        components[str(name)] = {
            "quantile": round(quantile, 4),
            "weight": weight,
            "inverted": bool(spec.get("invert")),
            "contribution": round(value * weight * 100.0, 4),
        }
        total_weight += weight
        weighted += value * weight
    if total_weight < float(config["min_available_weight"]):
        return _unavailable(
            "insufficient_component_weight",
            available_weight=round(total_weight, 4),
            unavailable_components=sorted(missing),
        )
    score = round(weighted / total_weight * 100.0, 4)
    return {
        "schema": SCHEMA,
        "status": "ok",
        "calibrated": False,
        "trading_date": records[index].get("trading_date"),
        "score": score,
        "band": _band(score, list(config["bands"])),
        "delta": None,
        "delta_squared": None,
        "available_weight": round(total_weight, 4),
        "unavailable_components": sorted(missing),
        "components": components,
    }


def compute_sentiment_score(
    records: Sequence[Mapping[str, Any]],
    *,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """序列末日的 S_t + ΔS + Δ²S。``records`` 按交易日升序，缺配置即不可用。"""
    cfg = load_config() if config is None else dict(config)
    if not cfg:
        return _unavailable("config_missing")
    rows = list(records or [])
    if not rows:
        return _unavailable("empty_series", observed_days=0)
    latest = score_at(rows, len(rows) - 1, cfg)
    if latest.get("status") != "ok":
        return latest
    previous = score_at(rows, len(rows) - 2, cfg)
    earlier = score_at(rows, len(rows) - 3, cfg)
    if previous.get("status") == "ok":
        latest["delta"] = round(float(latest["score"]) - float(previous["score"]), 4)
        latest["previous_score"] = previous["score"]
        if earlier.get("status") == "ok":
            prior_delta = float(previous["score"]) - float(earlier["score"])
            latest["delta_squared"] = round(float(latest["delta"]) - prior_delta, 4)
    return latest


def ice_point_confirmed(
    score: Mapping[str, Any] | None,
    *,
    leader_confirm: bool | None,
    sector_breadth_top: int | None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """冰点确认谓词（方案 §3.1c）：极弱 ∧ ΔS 改善 ∧ 标杆确认 ∧ 板块扩散。

    四条件**全部**成立才 confirmed；任何一条所需数据不可用即判否并记 reason ——
    "单独冰点永不触发买入"这条纪律靠的就是缺一不可。
    """
    cfg = load_config() if config is None else dict(config)
    if not cfg:
        return {"confirmed": False, "reasons": ["config_missing"], "shadow_only": True}
    thresholds = dict(cfg["ice_confirm"])
    row = dict(score or {})
    reasons: list[str] = []
    if row.get("status") != "ok":
        reasons.append("score_unavailable")
    previous = _numeric(row.get("previous_score"))
    delta = _numeric(row.get("delta"))
    if previous is None or previous >= float(thresholds["prev_score_max"]):
        reasons.append("previous_score_not_extreme")
    if delta is None or delta <= float(thresholds["delta_min"]):
        reasons.append("delta_below_threshold")
    if leader_confirm is not True:
        reasons.append("leader_not_confirmed")
    breadth = _numeric(sector_breadth_top)
    if breadth is None or breadth < float(thresholds["sector_breadth_min"]):
        reasons.append("sector_breadth_below_threshold")
    return {
        "schema": "sentiment_ice_confirm_v1",
        "confirmed": not reasons,
        "calibrated": False,
        "shadow_only": True,
        "reasons": reasons,
    }
