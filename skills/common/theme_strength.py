"""Deterministic daily theme strength review + lifecycle state machine (§4b/§4c).

For each live L3 theme this module computes four strength dimensions from facts
the DAG already produced, then runs a pure-function lifecycle FSM over the
resulting daily record plus persisted history. Everything here is deterministic
and model-free; unavailable inputs are reported as ``unavailable`` rather than
guessed (fail-closed).

Dimensions
==========
- **breadth**: member limit-up count, up-ratio, and a per-theme lianban ladder
  height (reusing market_temperature.ladder_height on the members' slice).
- **capital**: aggregated main net inflow across members, sourced from the
  signal_context capital-flow lineage (which itself flows through the
  field_arbiter ``capital_flow`` chain). Missing -> ``unavailable``.
- **relative_strength**: theme equal-weight return minus the *whole-A median*
  return, rolled 5/10/20 trading days. The market median comes from the full
  candidate-discovery-input universe quotes — never an index proxy. Without a
  real market basis or without enough persisted history, the window is
  ``unavailable``. An index basis, if ever supplied, is surfaced separately and
  clearly labelled as a degraded option, never as the whole-A median.
- **persistence**: consecutive days the theme scored "strong" (RS-led), from
  persisted history.

Lifecycle
=========
emerging -> mainline -> diverging -> fading, with fading/archived single
directional. Rules are pure functions of the daily record + short history and
target a decision lag of <= 2 trading days.
"""

from __future__ import annotations

import os
import sys
from statistics import median
from typing import Any, Mapping, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from market_temperature import ladder_height  # noqa: E402
from paths import data_file  # noqa: E402
from state_store import mutate_json, read_json  # noqa: E402

SCHEMA = "theme_strength_v1"
UNAVAILABLE = "unavailable"

RS_WINDOWS = (5, 10, 20)

# A theme counts as "strong" for persistence when its 5-day RS is available and
# positive — i.e. it is outperforming the whole-A median over the short window.
STRONG_RS_WINDOW = 5

DEFAULT_LIFECYCLE = {
    # ladder height at/above this with a positive short RS => mainline-eligible.
    "mainline_ladder_height": 3,
    "mainline_rs_window": 5,
    # RS turning negative or ladder collapsing beyond this fraction => diverging.
    "diverging_ladder_drop_frac": 0.5,
    # consecutive weak (RS<=0 or unavailable) days that retire a theme to fading.
    "fading_weak_days": 2,
    # persistence (strong-day streak) needed to *hold* mainline.
    "mainline_min_persistence": 2,
    # fading themes archived after this many additional weak days.
    "archive_after_fading_days": 3,
}


def _num(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _norm_code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6) if text else ""


# ── dimensions ────────────────────────────────────────────────────────────

def compute_breadth(
    members: Sequence[str],
    quotes_by_code: Mapping[str, Mapping[str, Any]],
    ladder: Mapping[str, Any] | None,
    *,
    limit_up_pct: float = 9.8,
) -> dict[str, Any]:
    """Member breadth. ``unavailable`` when no member quote is observed."""
    members = [_norm_code(code) for code in members if _norm_code(code)]
    observed = [
        (code, quotes_by_code[code])
        for code in members
        if isinstance(quotes_by_code.get(code), Mapping)
    ]
    if not observed:
        return {"status": UNAVAILABLE, "reason": "no_member_quotes"}
    changes = [_num(q.get("change_pct")) for _, q in observed]
    valid_changes = [c for c in changes if c is not None]
    up = sum(1 for c in valid_changes if c > 0)
    limit_ups = sum(1 for c in valid_changes if c >= limit_up_pct)
    member_ladder = {
        code: entry for code, entry in (ladder or {}).items()
        if _norm_code(code) in set(members) and isinstance(entry, Mapping)
    }
    return {
        "status": "ok",
        "observed_members": len(observed),
        "up_count": up,
        "up_ratio": round(up / len(valid_changes), 4) if valid_changes else None,
        "limit_up_count": limit_ups,
        "ladder_height": ladder_height(member_ladder),
    }


def compute_capital(
    members: Sequence[str],
    stock_flows: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Aggregate member main net inflow (亿) from the capital-flow lineage.

    ``stock_flows`` is the signal_context capital-flow section, itself resolved
    through the field_arbiter ``capital_flow`` chain upstream. If it is absent
    or no member has a flow record, the dimension is ``unavailable`` (§4b).
    """
    members = {_norm_code(code) for code in members if _norm_code(code)}
    flows = stock_flows or {}
    if not flows:
        return {"status": UNAVAILABLE, "reason": "capital_flow_absent"}
    total = 0.0
    counted = 0
    for code in members:
        record = flows.get(code)
        if not isinstance(record, Mapping):
            continue
        main = _num(record.get("main_net_yi"))
        if main is None:
            continue
        total += main
        counted += 1
    if counted == 0:
        return {"status": UNAVAILABLE, "reason": "no_member_flow_records"}
    return {
        "status": "ok",
        "main_net_yi": round(total, 3),
        "members_with_flow": counted,
    }


def market_median_return(quotes_by_code: Mapping[str, Mapping[str, Any]]) -> float | None:
    """Whole-A median day return from the full-universe quote map. ``None`` when
    no real universe basis is available (never substitute an index)."""
    changes = [
        _num(q.get("change_pct"))
        for q in quotes_by_code.values()
        if isinstance(q, Mapping)
    ]
    changes = [c for c in changes if c is not None]
    if len(changes) < 100:  # a genuine whole-A basis, not a handful of quotes
        return None
    return round(median(changes), 4)


def theme_daily_excess(
    members: Sequence[str],
    quotes_by_code: Mapping[str, Mapping[str, Any]],
    market_median: float | None,
) -> dict[str, Any]:
    """Single-day equal-weight theme return minus whole-A median.

    Returns ``unavailable`` when the theme has no observed member returns or
    when no real market median basis exists. This daily excess is the atom the
    rolling RS windows accumulate from persisted history.
    """
    members = [_norm_code(code) for code in members if _norm_code(code)]
    member_returns = [
        _num(quotes_by_code[code].get("change_pct"))
        for code in members
        if isinstance(quotes_by_code.get(code), Mapping)
    ]
    member_returns = [r for r in member_returns if r is not None]
    if not member_returns:
        return {"status": UNAVAILABLE, "reason": "no_member_returns"}
    if market_median is None:
        return {"status": UNAVAILABLE, "reason": "no_whole_a_basis"}
    theme_return = round(sum(member_returns) / len(member_returns), 4)
    return {
        "status": "ok",
        "theme_return": theme_return,
        "market_median": market_median,
        "daily_excess": round(theme_return - market_median, 4),
    }


def rolling_rs(
    daily_excess_series: Sequence[float | None],
    windows: Sequence[int] = RS_WINDOWS,
) -> dict[str, Any]:
    """Sum of daily excess returns over each rolling window, newest-last.

    A window is ``unavailable`` unless it has that many *consecutive, present*
    daily-excess values ending today — a single missing day fails the window
    closed rather than silently shortening it.
    """
    result: dict[str, Any] = {}
    for window in windows:
        tail = list(daily_excess_series)[-window:]
        if len(tail) < window or any(value is None for value in tail):
            result[f"rs_{window}d"] = {"status": UNAVAILABLE}
            continue
        result[f"rs_{window}d"] = {
            "status": "ok",
            "value": round(sum(float(v) for v in tail), 4),
        }
    return result


# ── daily record assembly ───────────────────────────────────────────────────

def build_theme_record(
    theme: Mapping[str, Any],
    *,
    asof: str,
    quotes_by_code: Mapping[str, Mapping[str, Any]],
    ladder: Mapping[str, Any] | None,
    stock_flows: Mapping[str, Mapping[str, Any]] | None,
    market_median: float | None,
    prior_excess_series: Sequence[float | None],
    index_basis: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one theme's daily strength record (pure)."""
    members = list(theme.get("members") or [])
    breadth = compute_breadth(members, quotes_by_code, ladder)
    capital = compute_capital(members, stock_flows)
    excess = theme_daily_excess(members, quotes_by_code, market_median)
    today_excess = excess.get("daily_excess") if excess.get("status") == "ok" else None
    excess_series = [*prior_excess_series, today_excess]
    rs = rolling_rs(excess_series)

    strong_key = f"rs_{STRONG_RS_WINDOW}d"
    strong_rs = rs.get(strong_key, {})
    is_strong = strong_rs.get("status") == "ok" and float(strong_rs.get("value", 0.0)) > 0.0

    record: dict[str, Any] = {
        "schema": SCHEMA,
        "theme_id": theme.get("id"),
        "name": theme.get("name"),
        "asof": asof,
        "member_count": len(members),
        "breadth": breadth,
        "capital": capital,
        "relative_strength": rs,
        "daily_excess": excess,
        "is_strong": is_strong,
    }
    if index_basis:
        # Degraded, clearly-labelled option — never the whole-A median.
        record["relative_strength_index_basis"] = {
            "note": "index-relative degraded basis, not whole-A median",
            **dict(index_basis),
        }
    return record


def compute_persistence(strong_flags: Sequence[bool]) -> int:
    """Consecutive strong days ending today (newest-last)."""
    streak = 0
    for flag in reversed(list(strong_flags)):
        if not flag:
            break
        streak += 1
    return streak


# ── lifecycle FSM (pure) ────────────────────────────────────────────────────

LIFECYCLE_STAGES = ("emerging", "mainline", "diverging", "fading", "archived")


def _rs_value(record: Mapping[str, Any], window: int) -> float | None:
    entry = (record.get("relative_strength") or {}).get(f"rs_{window}d") or {}
    return float(entry["value"]) if entry.get("status") == "ok" else None


def decide_stage(
    current_stage: str,
    record: Mapping[str, Any],
    *,
    persistence: int,
    prior_ladder_height: int | None,
    weak_streak: int,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure lifecycle decision. Returns {"stage", "reason"}.

    Transitions (target lag <= 2 trading days):
      emerging  -> mainline  : ladder height >= threshold AND short RS > 0.
      mainline  -> diverging : short RS turned <= 0, OR ladder collapsed past
                               the drop fraction (high-position break / member
                               divergence).
      diverging -> fading    : weak_streak >= fading_weak_days (RS stays <=0 /
                               unavailable) — the retreat is confirmed.
      * -> fading            : any live stage with a confirmed weak streak.
      fading    -> archived  : still weak after archive_after_fading_days.

    fading and archived are single-directional here: a recovering theme is not
    auto-promoted back out of fading by this function (that would risk chasing a
    dead-cat bounce); revival is an explicit ``register(force=True)`` act.
    """
    cfg = {**DEFAULT_LIFECYCLE, **dict(config or {})}
    if current_stage == "archived":
        return {"stage": "archived", "reason": "tombstone"}

    ladder = int(((record.get("breadth") or {}).get("ladder_height")) or 0)
    short_rs = _rs_value(record, int(cfg["mainline_rs_window"]))
    weak = short_rs is None or short_rs <= 0.0

    # fading / archiving take priority: a confirmed retreat overrides everything.
    if current_stage == "fading":
        if weak and weak_streak >= int(cfg["archive_after_fading_days"]):
            return {"stage": "archived", "reason": "weak_streak_archived"}
        return {"stage": "fading", "reason": "still_fading" if weak else "fading_hold"}

    if weak and weak_streak >= int(cfg["fading_weak_days"]):
        return {"stage": "fading", "reason": f"weak_streak_{weak_streak}"}

    if current_stage in ("mainline", "diverging"):
        ladder_collapsed = (
            prior_ladder_height is not None
            and prior_ladder_height > 0
            and ladder <= prior_ladder_height * (1.0 - float(cfg["diverging_ladder_drop_frac"]))
        )
        if weak or ladder_collapsed:
            reason = "rs_turned_negative" if weak else "ladder_collapsed"
            return {"stage": "diverging", "reason": reason}
        if current_stage == "diverging":
            # recovered structure lifts diverging back to mainline.
            if persistence >= int(cfg["mainline_min_persistence"]):
                return {"stage": "mainline", "reason": "structure_recovered"}
            return {"stage": "diverging", "reason": "unresolved"}
        return {"stage": "mainline", "reason": "structure_intact"}

    # emerging
    if (
        ladder >= int(cfg["mainline_ladder_height"])
        and short_rs is not None
        and short_rs > 0.0
    ):
        return {"stage": "mainline", "reason": "resonance_confirmed"}
    return {"stage": "emerging", "reason": "not_yet_mainline"}


# ── persisted history ────────────────────────────────────────────────────────

def history_file() -> str:
    return data_file("stock-triage", "theme_strength_history.json")


def load_history() -> dict[str, Any]:
    value = read_json(history_file(), {})
    return value if isinstance(value, dict) else {}


def theme_history(theme_id: str, *, history: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    history = history if history is not None else load_history()
    entries = (history.get("themes") or {}).get(theme_id) or []
    return [e for e in entries if isinstance(e, Mapping)]


def prior_excess_series(theme_id: str, *, history: Mapping[str, Any] | None = None,
                        max_len: int = 30) -> list[float | None]:
    """Persisted daily-excess series (oldest-first) for rolling RS, excluding
    today. ``None`` entries are preserved so a gap fails the window closed."""
    series: list[float | None] = []
    for entry in theme_history(theme_id, history=history)[-max_len:]:
        excess = (entry.get("daily_excess") or {})
        series.append(excess.get("daily_excess") if excess.get("status") == "ok" else None)
    return series


def prior_strong_flags(theme_id: str, *, history: Mapping[str, Any] | None = None,
                       max_len: int = 30) -> list[bool]:
    return [bool(e.get("is_strong")) for e in theme_history(theme_id, history=history)[-max_len:]]


def weak_streak(theme_id: str, *, include_today: bool | None = None,
                history: Mapping[str, Any] | None = None) -> int:
    """Consecutive non-strong days ending at the latest persisted day."""
    flags = prior_strong_flags(theme_id, history=history)
    if include_today is not None:
        flags = [*flags, bool(include_today)]
    streak = 0
    for flag in reversed(flags):
        if flag:
            break
        streak += 1
    return streak


# ── ThemeScore 新·时·广（升级方案 P2-b，SHADOW ONLY）────────────────────────
#
# 既有的主线板块分是 4 因子（涨停 45% / 成交额 20% / 前十涨幅 25% / 关注度 10%），
# 只描述"现在多强"，说不出"是不是新题材"和"现在是不是它的窗口"。本节补上"新"与
# "时"两维，与既有 breadth 合成 ThemeScore = 0.35N + 0.30T + 0.35B。
# 它是**平行研究字段**：不替换 sector_weights，不参与任何 gate/排序判定。
# 权重与阈值全部来自 config/scoring.yaml 的 ``theme_score`` 节，模块内无数字副本。

THEME_SCORE_SCHEMA = "theme_score_v1"
THEME_SCORE_SECTION = "theme_score"
_THEME_SCORE_REQUIRED = ("weights", "min_available_weight", "novelty", "timing", "breadth")


def load_theme_score_config(payload: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """读取 ``config/scoring.yaml`` 的 ``theme_score`` 节；缺项返回 None。"""
    if payload is None:
        try:
            import yaml
            from config_registry import config_path

            with open(config_path("scoring"), encoding="utf-8") as handle:
                payload = yaml.safe_load(handle)
        except (OSError, ValueError, ImportError):
            return None
    section = (payload or {}).get(THEME_SCORE_SECTION) if isinstance(payload, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    if any(section.get(key) in (None, "", [], {}) for key in _THEME_SCORE_REQUIRED):
        return None
    return dict(section)


def registry_age_days(created_at: Any, asof: Any) -> int | None:
    """题材首次注册距 ``asof`` 的自然日数；任一端不可解析返回 None。"""
    from datetime import date as _date

    try:
        created = _date.fromisoformat(str(created_at or "")[:10])
        current = _date.fromisoformat(str(asof or "")[:10])
    except ValueError:
        return None
    return (current - created).days


def news_novelty(
    items: Sequence[Mapping[str, Any]] | None, *, seen_keys: Sequence[str] | None
) -> float | None:
    """当日新闻 novelty 比例 = 未见过的去重内容键 / 全部内容键。

    键的口径直接复用 ``novelty_gate.content_key``（纯函数，无缓存 IO），不另造一套
    归一化——两套归一化迟早对同一条新闻给出两个键。``seen_keys`` 必须由调用方显式
    给出（空列表 = "此前什么都没见过"这一明确主张）；为 None 时返回不可用，绝不默认
    当作"全都是新的"。
    """
    if items is None or seen_keys is None:
        return None
    from novelty_gate import content_key

    keys = [content_key(item) for item in items if isinstance(item, Mapping)]
    if not keys:
        return None
    seen = {str(key) for key in seen_keys}
    unique = set(keys)
    return round(len(unique - seen) / len(unique), 6)


def compute_novelty(
    theme: Mapping[str, Any],
    *,
    asof: str,
    news_items: Sequence[Mapping[str, Any]] | None = None,
    seen_news_keys: Sequence[str] | None = None,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """N 新鲜度：注册年龄衰减 ⊕ 当日新闻 novelty。两个子分量各自可单独降级并在
    N 内部重新归一化；两个都不可用时 N 整体 ``unavailable``。"""
    cfg = dict(config)
    age = registry_age_days(theme.get("created_at"), asof)
    half_life = float(cfg.get("half_life_days") or 0) or 1.0
    parts: list[tuple[float, float]] = []
    detail: dict[str, Any] = {"registry_age_days": age}
    if age is not None and age >= 0:
        decay = 0.5 ** (age / half_life)
        detail["age_decay"] = round(decay, 6)
        parts.append((decay, float(cfg.get("age_weight") or 0.0)))
    ratio = news_novelty(news_items, seen_keys=seen_news_keys)
    detail["news_novelty"] = ratio
    if ratio is not None:
        parts.append((ratio, float(cfg.get("news_weight") or 0.0)))
    total = sum(weight for _value, weight in parts)
    if total <= 0.0:
        return {"status": UNAVAILABLE, "reason": "no_novelty_component", **detail}
    value = sum(value * weight for value, weight in parts) / total
    return {"status": "ok", "value": round(value, 6), "component_weight": round(total, 4), **detail}


def compute_timing(
    sentiment: Mapping[str, Any] | None, *, config: Mapping[str, Any]
) -> dict[str, Any]:
    """T 时机：消费 P0 的 S_t（``sentiment_score.compute_sentiment_score`` 的输出）。

    S_t 不可用、或分档不在配置表里 → ``unavailable``，由 ThemeScore 重归一化处理；
    绝不给一个"中性 0.5"冒充观测。
    """
    cfg = dict(config)
    row = dict(sentiment or {})
    if row.get("status") != "ok":
        return {"status": UNAVAILABLE, "reason": "sentiment_score_unavailable",
                "sentiment_status": row.get("status")}
    band = str(row.get("band") or "")
    bands = dict(cfg.get("band_scores") or {})
    if band not in bands:
        return {"status": UNAVAILABLE, "reason": "band_not_mapped", "band": band or None}
    base = float(bands[band])
    delta = _num(row.get("delta"))
    bonus = float(cfg.get("delta_bonus") or 0.0) if (delta is not None and delta > 0) else 0.0
    return {
        "status": "ok",
        "value": round(min(1.0, max(0.0, base + bonus)), 6),
        "band": band,
        "band_score": base,
        "delta": delta,
        "delta_bonus_applied": bonus > 0.0,
    }


def compute_breadth_score(
    breadth: Mapping[str, Any] | None, *, config: Mapping[str, Any]
) -> dict[str, Any]:
    """B 广度：直接复用既有 ``compute_breadth`` 的结果，不重算成员行情。"""
    cfg = dict(config)
    row = dict(breadth or {})
    if row.get("status") != "ok":
        return {"status": UNAVAILABLE, "reason": "breadth_unavailable"}
    full = float(cfg.get("full_scale_limit_ups") or 0) or 1.0
    limit_score = min(1.0, (_num(row.get("limit_up_count")) or 0.0) / full)
    ratio = _num(row.get("up_ratio"))
    ratio_weight = float(cfg.get("up_ratio_weight") or 0.0)
    if ratio is None:
        return {"status": "ok", "value": round(limit_score, 6),
                "limit_up_score": round(limit_score, 6), "up_ratio": None}
    value = (1.0 - ratio_weight) * limit_score + ratio_weight * min(1.0, max(0.0, ratio))
    return {"status": "ok", "value": round(value, 6),
            "limit_up_score": round(limit_score, 6), "up_ratio": ratio}


def theme_score(
    theme: Mapping[str, Any],
    record: Mapping[str, Any],
    *,
    asof: str,
    sentiment: Mapping[str, Any] | None = None,
    news_items: Sequence[Mapping[str, Any]] | None = None,
    seen_news_keys: Sequence[str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """ThemeScore = 0.35N + 0.30T + 0.35B（0-100，SHADOW ONLY，方案 §5.1b）。

    维度不可用即把它的权重移出分母重新归一化；可用权重低于
    ``min_available_weight``（或三维全缺）→ 整体 ``unavailable``，不返回 0 分。
    """
    cfg = load_theme_score_config() if config is None else dict(config)
    if not cfg:
        return {"schema": THEME_SCORE_SCHEMA, "status": UNAVAILABLE,
                "shadow_only": True, "calibrated": False,
                "score": None, "reason": "config_missing"}
    dimensions = {
        "novelty": compute_novelty(
            theme, asof=asof, news_items=news_items,
            seen_news_keys=seen_news_keys, config=dict(cfg["novelty"]),
        ),
        "timing": compute_timing(sentiment, config=dict(cfg["timing"])),
        "breadth": compute_breadth_score(record.get("breadth"), config=dict(cfg["breadth"])),
    }
    weights = dict(cfg["weights"])
    available = {name: row for name, row in dimensions.items() if row.get("status") == "ok"}
    total = sum(float(weights.get(name) or 0.0) for name in available)
    result: dict[str, Any] = {
        "schema": THEME_SCORE_SCHEMA,
        "shadow_only": True,
        "calibrated": False,
        "theme_id": theme.get("id") or record.get("theme_id"),
        "asof": asof,
        "weights": {name: float(weights.get(name) or 0.0) for name in weights},
        "available_weight": round(total, 4),
        "unavailable_dimensions": sorted(set(dimensions) - set(available)),
        "dimensions": dimensions,
    }
    if total <= 0.0 or total < float(cfg["min_available_weight"]):
        return {**result, "status": UNAVAILABLE, "score": None,
                "reason": "insufficient_dimension_weight"}
    weighted = sum(
        float(row["value"]) * float(weights.get(name) or 0.0) for name, row in available.items()
    )
    return {**result, "status": "ok", "score": round(weighted / total * 100.0, 4), "reason": None}


def append_history(theme_id: str, record: Mapping[str, Any], *, max_len: int = 60) -> dict[str, Any]:
    """Append today's record to the theme's persisted series (idempotent per
    asof: re-running the same day overwrites that day's entry)."""
    asof = str(record.get("asof") or "")

    def _mut(state: Any) -> dict[str, Any]:
        data = dict(state) if isinstance(state, dict) else {}
        data.setdefault("schema", SCHEMA)
        themes = dict(data.get("themes") or {})
        series = [e for e in (themes.get(theme_id) or []) if isinstance(e, Mapping)]
        series = [e for e in series if str(e.get("asof") or "") != asof]
        series.append(dict(record))
        series.sort(key=lambda e: str(e.get("asof") or ""))
        themes[theme_id] = series[-max_len:]
        data["themes"] = themes
        return data

    return mutate_json(history_file(), _mut, {})
