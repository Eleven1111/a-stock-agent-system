"""Bridge Chanlun admission and Serenity research into live policy evidence."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from deep_research_cache import read_deep_research
from stock_intelligence import read_cache as read_stock_intelligence
import strategy_registry

_CHANLUN_FILE = (
    Path(__file__).resolve().parents[1] / "chanlun-backtest" / "scripts" / "chan_structure.py"
)


def _load_chan_structure():
    spec = importlib.util.spec_from_file_location("research_chan_structure", _CHANLUN_FILE)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError:
        return None
    return module


chan_structure = _load_chan_structure()


HARD_RISK_DIMENSIONS = {
    "financial_quality": 2,
    "risk_control": 2,
}

_CHAN_DIRECTIONS = {
    "third_buy": "bullish",
    "bottom_divergence": "bullish",
    "third_sell": "bearish",
    "top_divergence": "bearish",
}

# structure_position risk_flags（verdict B 遗留项 2）：一类/盘整背驰卖点、三类卖点，
# 均只作结构位置证据陈列，不预测方向、不参与打分。
_DIVERGENCE_BSP_TYPES = {"1", "1p"}
_THIRD_SELL_BSP_TYPES = {"3a", "3b"}


def _chanlun_evidence(strategy_id: str) -> dict[str, Any]:
    is_chanlun_strategy = strategy_id.startswith("chanlun_")
    record = strategy_registry.get(strategy_id) if is_chanlun_strategy else None
    if not is_chanlun_strategy:
        status = "no_signal"
    elif not record:
        status = "unregistered"
    elif record.get("gating_status", "enabled") == "disabled":
        status = "disabled"
    elif record.get("allowed_in_live_agent") is True:
        status = "live_allowed"
    else:
        status = "display_only"
    return {
        "selection_strategy_id": strategy_id,
        "strategy_id": strategy_id if is_chanlun_strategy else None,
        "status": status,
        "allowed_in_live_agent": status == "live_allowed",
        "gate_decision": (record or {}).get("gate_decision"),
        "gate_asof": (record or {}).get("gate_asof"),
        "gating_reason": (record or {}).get("gating_reason"),
        "gate_stats": (record or {}).get("gate_stats"),
        "signals": [],
        "live_bullish_signals": [],
        "live_bearish_signals": [],
        "display_only_signals": [],
    }


def _with_chanlun_signals(
    evidence: dict[str, Any],
    bars: list[dict[str, Any]] | None,
    recent_window: int = 10,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not bars or chan_structure is None:
        return evidence
    if analysis is None:
        analysis = chan_structure.analyze(bars)
    signals = []
    total = len(bars)
    for raw in analysis.get("signals") or []:
        signal = dict(raw)
        idx = signal.get("idx")
        if idx is None or idx < total - recent_window:
            continue
        signal["gate_status"] = (
            "live_allowed"
            if strategy_registry.is_allowed_in_live(str(signal.get("strategy_id") or ""))
            else "display_only"
        )
        signal["signal_age_bars"] = total - 1 - int(idx)
        signals.append(signal)
    bearish = {"third_sell", "top_divergence"}
    bullish = {"third_buy", "bottom_divergence"}
    evidence.update({
        "structure_summary": analysis.get("summary"),
        "signals": signals,
        "live_bullish_signals": [
            signal for signal in signals
            if signal["gate_status"] == "live_allowed" and signal.get("type") in bullish
        ],
        "live_bearish_signals": [
            signal for signal in signals
            if signal["gate_status"] == "live_allowed" and signal.get("type") in bearish
        ],
        "display_only_signals": [
            signal for signal in signals if signal["gate_status"] == "display_only"
        ],
    })
    if evidence["live_bullish_signals"] or evidence["live_bearish_signals"]:
        evidence["status"] = "live_allowed"
    elif evidence["display_only_signals"]:
        evidence["status"] = "display_only"
    else:
        evidence["status"] = "no_signal"
    return evidence


def _price_vs_center(last_close: Any, center: dict[str, Any] | None) -> dict[str, Any] | None:
    """当前价相对最新中枢（chan_structure structure.last_center）的位置。

    distance_pct 口径：above_zg/below_zd 时为相对被突破边界的偏离百分比；
    inside 时为在 [zd, zg] 区间内的相对位置百分比（0=zd 上，100=zg 上）。
    """
    if last_close is None or not center:
        return None
    zg, zd = float(center["zg"]), float(center["zd"])
    if last_close > zg:
        distance_pct = (last_close - zg) / zg * 100 if zg else None
        return {"position": "above_zg", "zg": zg, "zd": zd,
                "distance_pct": round(distance_pct, 3) if distance_pct is not None else None}
    if last_close < zd:
        distance_pct = (last_close - zd) / zd * 100 if zd else None
        return {"position": "below_zd", "zg": zg, "zd": zd,
                "distance_pct": round(distance_pct, 3) if distance_pct is not None else None}
    band = zg - zd
    distance_pct = (last_close - zd) / band * 100 if band else 0.0
    return {"position": "inside", "zg": zg, "zd": zd, "distance_pct": round(distance_pct, 3)}


def _segment_position(structure: dict[str, Any]) -> dict[str, Any] | None:
    """最新线段方向/is_sure + 当前（最新）笔在段内的序号（1-based，段起笔=1）。"""
    last_seg = structure.get("last_seg")
    stroke_count = structure.get("stroke_count") or 0
    if not last_seg or not stroke_count:
        return None
    current_bi_idx = stroke_count - 1
    return {
        "dir": last_seg.get("dir"),
        "is_sure": last_seg.get("is_sure"),
        "current_stroke_ordinal": current_bi_idx - int(last_seg["start_bi_idx"]) + 1,
    }


def _recent_sure_signals(signals: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """最新确定信号摘要：is_sure=True，按 idx 降序，最多 limit 条。"""
    sure = [s for s in signals if s.get("is_sure")]
    sure.sort(key=lambda s: s.get("idx") if s.get("idx") is not None else -1, reverse=True)
    return [
        {"bsp_type": s.get("bsp_type"), "is_buy": s.get("is_buy"), "date": s.get("date")}
        for s in sure[:limit]
    ]


def _structure_risk_flags(structure: dict[str, Any], signals: list[dict[str, Any]]) -> list[str]:
    """结构风险标记（证据陈列，不构成否决条件）。

    - seg_end_divergence: 最新线段 is_sure，其末笔（end_bi_idx）上出现确定的一类/
      盘整背驰卖点（bsp_type in {1,1p}，is_buy=False）——"线段末端背驰"。
    - third_sell_structure: 存在确定的三类卖点（bsp_type in {3a,3b}，is_buy=False）
      ——"三卖后反弹未过中枢下沿"。
    """
    flags: list[str] = []
    last_seg = structure.get("last_seg")
    if last_seg and last_seg.get("is_sure"):
        end_bi_idx = last_seg.get("end_bi_idx")
        if any(
            s.get("is_sure") and s.get("bi_idx") == end_bi_idx
            and s.get("bsp_type") in _DIVERGENCE_BSP_TYPES and s.get("is_buy") is False
            for s in signals
        ):
            flags.append("seg_end_divergence")
    if any(
        s.get("is_sure") and s.get("bsp_type") in _THIRD_SELL_BSP_TYPES and s.get("is_buy") is False
        for s in signals
    ):
        flags.append("third_sell_structure")
    return flags


def _structure_position_section(
    bars: list[dict[str, Any]] | None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """结构位置证据包 section（verdict B 遗留项 2）：全部来自 chan_structure.analyze()
    的结构输出，不新增网络调用（bars 由调用方沿用现有 chanlun 证据入口传入）。

    只作位置证据，不预测方向：本 section 不产生任何 delta/score，只供
    risk_redteam 等下游引用。
    """
    if analysis is None:
        if not bars or chan_structure is None:
            return {"available": False}
        analysis = chan_structure.analyze(bars)
    if not analysis or not analysis.get("ok"):
        return {"available": False}
    structure = analysis.get("structure") or {}
    signals = analysis.get("signals") or []
    return {
        "available": True,
        "price_vs_center": _price_vs_center(analysis.get("last_close"), structure.get("last_center")),
        "segment": _segment_position(structure),
        "recent_sure_signals": _recent_sure_signals(signals),
        "risk_flags": _structure_risk_flags(structure, signals),
    }


def _serenity_evidence(code: str, asof: str | None) -> dict[str, Any]:
    record = read_deep_research(code, today=asof)
    if not record:
        return {
            "available": False,
            "stale": None,
            "hard_risks": [],
        }
    dimensions = record.get("dimensions") or {}
    hard_risks = []
    for dimension, threshold in HARD_RISK_DIMENSIONS.items():
        value = dimensions.get(dimension)
        try:
            score = int(value)
        except (TypeError, ValueError):
            continue
        if score <= threshold:
            hard_risks.append(f"{dimension}={score}/5")
    return {
        "available": True,
        "stale": bool(record.get("stale")),
        "asof": record.get("asof"),
        "age_days": record.get("age_days"),
        "deep_score": record.get("deep_score"),
        "rating": record.get("rating"),
        "valuation_upside_pct": record.get("valuation_upside_pct"),
        "dimensions": dimensions,
        "hard_risks": hard_risks,
        "report_path": record.get("report_path"),
    }


def strategy_attributions(evidence: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract live research signals without replacing the primary strategy.

    These tags support conditional/co-occurrence performance reporting. They
    are not a causal allocation of the recommendation's full return.
    """
    chanlun = (evidence or {}).get("chanlun") or {}
    output = []
    seen = set()
    for bucket in ("live_bullish_signals", "live_bearish_signals"):
        for raw in chanlun.get(bucket) or []:
            strategy_id = str(raw.get("strategy_id") or "").strip()
            signal_type = str(raw.get("type") or "").strip()
            direction = _CHAN_DIRECTIONS.get(signal_type)
            if not strategy_id or not direction:
                continue
            key = (strategy_id, signal_type, raw.get("idx"))
            if key in seen:
                continue
            seen.add(key)
            output.append({
                "strategy_id": strategy_id,
                "role": "research_evidence",
                "direction": direction,
                "signal_type": signal_type,
                "signal_idx": raw.get("idx"),
            })
    return output


def build_research_evidence(
    code: str,
    *,
    strategy_id: str,
    asof: str | None = None,
    bars: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the auditable research evidence supplied to live policy."""
    analysis = chan_structure.analyze(bars) if bars and chan_structure is not None else None
    return {
        "schema": "research_evidence_v1",
        "code": str(code).zfill(6),
        "asof": asof,
        "chanlun": _with_chanlun_signals(_chanlun_evidence(strategy_id), bars, analysis=analysis),
        "structure_position": _structure_position_section(bars, analysis=analysis),
        "serenity": _serenity_evidence(code, asof),
        "market_intelligence": read_stock_intelligence(code, asof=asof),
    }
