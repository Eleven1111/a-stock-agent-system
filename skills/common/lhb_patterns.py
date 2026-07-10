"""龙虎榜多日模式识别 — 洗盘建仓 / 高潮见顶 / 换手率趋势 / 持有策略。

Issue #88（翔鹭钨业 -25.2% 复盘）的系统化沉淀。单日席位分类已有
（dragon_tiger.classify_seat），缺的是跨日序列模式：
- 洗盘-建仓：大单砸盘（净卖 ≥ 2亿）后 3 个上榜日内连续净买入 → 主力建仓候选；
- 高潮见顶：单日净买 > 前 3 个上榜日均值 3 倍 → 最后疯狂预警（7/02 +2.72亿）；
- 换手率趋势：换手递减 + 价格上升 = 筹码集中健康上涨；换手持续高位 = 游资倒手；
- 持有策略：机构/北向主导 → 放宽持有（利润奔跑）；游资主导 → 严止损快止盈。

输入序列 seq：按日期升序的 [{date, net_yi(净买额亿), turnover_pct, close?}]。
全部纯函数，可合成数据单测。触网侧在 dragon_tiger.py。

红线（与 dragon_tiger.py 一致）：本模块产出只允许进入风控/退出信号链路，
接入打分（four_dim）前必须过 research_gate 研究闸门。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

SCHEMA = "lhb_profile_v1"

WASH_SELL_THRESHOLD_YI = 2.0      # 恐慌洗盘：单日净卖 ≥ 2亿
WASH_CONFIRM_WINDOW = 3           # 砸盘后 N 个上榜日内确认回补
CLIMAX_MULTIPLE = 3.0             # 高潮：净买 > 前3上榜日均值的倍数
HIGH_TURNOVER_PCT = 20.0          # 游资倒手：换手率持续高位阈值


def _num(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def detect_wash_accumulation(seq: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """大单砸盘后连续净买入 → 主力建仓候选。

    返回 {matched, wash_date, accumulation_dates, note}。匹配最近一次砸盘。
    """
    result = {"matched": False, "wash_date": None, "accumulation_dates": [], "note": ""}
    rows = [r for r in seq if _num(r.get("net_yi")) is not None]
    for i in range(len(rows) - 1, -1, -1):
        net = float(rows[i]["net_yi"])
        if net > -WASH_SELL_THRESHOLD_YI:
            continue
        window = rows[i + 1:i + 1 + WASH_CONFIRM_WINDOW]
        buys = []
        for row in window:
            if float(row["net_yi"]) > 0:
                buys.append(str(row.get("date") or ""))
            else:
                break
        if len(buys) >= 2:
            result.update({
                "matched": True,
                "wash_date": str(rows[i].get("date") or ""),
                "accumulation_dates": buys,
                "note": (
                    f"{rows[i].get('date')}净卖{abs(net):.1f}亿砸盘后，"
                    f"连续{len(buys)}个上榜日净买入 → 主力建仓模式"
                ),
            })
            return result
    return result


def detect_climax_volume(seq: Sequence[Mapping[str, Any]],
                         multiple: float = CLIMAX_MULTIPLE) -> dict[str, Any]:
    """最新上榜日净买突然放量 > 前3上榜日均值 N 倍 → 高潮见顶预警。"""
    result = {"matched": False, "note": "", "latest_net_yi": None, "baseline_yi": None}
    rows = [r for r in seq if _num(r.get("net_yi")) is not None]
    if len(rows) < 4:
        return result
    latest = float(rows[-1]["net_yi"])
    baseline = sum(abs(float(r["net_yi"])) for r in rows[-4:-1]) / 3
    result["latest_net_yi"] = round(latest, 2)
    result["baseline_yi"] = round(baseline, 2)
    if latest > 0 and baseline > 0 and latest > baseline * multiple:
        result["matched"] = True
        result["note"] = (
            f"{rows[-1].get('date')}净买{latest:.1f}亿，为前3上榜日均值"
            f"{baseline:.1f}亿的{latest / baseline:.1f}倍 → 高潮见顶信号，立即止盈"
        )
    return result


def turnover_price_trend(seq: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """换手率与价格的组合趋势。

    返回 pattern ∈ {concentrating(筹码集中健康上涨), churning(游资高位倒手),
    neutral}。concentrating 需要换手率整体递减且价格上升；churning 为近段
    换手率持续 ≥ HIGH_TURNOVER_PCT。
    """
    result = {"pattern": "neutral", "note": ""}
    rows = [
        r for r in seq
        if _num(r.get("turnover_pct")) is not None
    ]
    if len(rows) < 3:
        return result
    turnovers = [float(r["turnover_pct"]) for r in rows]
    recent = turnovers[-3:]
    if all(t >= HIGH_TURNOVER_PCT for t in recent):
        result.update({
            "pattern": "churning",
            "note": f"换手率持续高位({recent[0]:.0f}%/{recent[1]:.0f}%/{recent[2]:.0f}%)，游资倒手特征",
        })
        return result
    closes = [
        _num(r.get("close")) for r in rows
        if _num(r.get("close")) is not None
    ]
    turnover_declining = turnovers[-1] < turnovers[0] and turnovers[-1] <= min(turnovers[:2])
    price_rising = len(closes) >= 2 and closes[-1] > closes[0]
    if turnover_declining and price_rising:
        result.update({
            "pattern": "concentrating",
            "note": (
                f"换手率{turnovers[0]:.0f}%→{turnovers[-1]:.0f}%递减且价格上升，"
                "筹码集中，健康上涨"
            ),
        })
    return result


def holding_policy(dominant_force: Optional[str],
                   climax_matched: bool = False) -> dict[str, Any]:
    """龙虎榜主体 → 持有策略。

    机构/北向主导：趋势有支撑，允许更长持有、回撤止盈放宽（利润奔跑）；
    游资主导：快进快出，回撤止盈收紧、时间窗口缩短；出现高潮信号立即止盈。
    """
    if dominant_force in ("institution", "northbound"):
        policy = {
            "style": "institution_led",
            "trailing_pct": 8.0,
            "horizon_days": 10,
            "note": "机构/北向主导，趋势有支撑：让利润奔跑，回撤止盈放宽至8%",
        }
    elif dominant_force == "hot_money":
        policy = {
            "style": "hot_money_led",
            "trailing_pct": 4.0,
            "horizon_days": 3,
            "note": "游资营业部主导：快进快出，回撤止盈收紧至4%，持有窗口3日",
        }
    else:
        policy = {
            "style": "unknown",
            "trailing_pct": 5.0,
            "horizon_days": 5,
            "note": "龙虎榜主体不明，使用默认持有纪律",
        }
    if climax_matched:
        policy["climax_exit"] = True
        policy["note"] += "；已出现高潮见顶信号，立即止盈"
    return policy


def build_lhb_profile(seq: Sequence[Mapping[str, Any]],
                      seat_summary: Optional[Mapping[str, Any]] = None,
                      *,
                      code: Optional[str] = None,
                      asof: Optional[str] = None) -> dict[str, Any]:
    """多日龙虎榜序列 + 席位汇总 → 完整模式画像与持有策略。"""
    wash = detect_wash_accumulation(seq)
    climax = detect_climax_volume(seq)
    trend = turnover_price_trend(seq)
    dominant = None
    if isinstance(seat_summary, Mapping):
        dominant = seat_summary.get("dominant_force")
    policy = holding_policy(dominant, climax["matched"])
    notes = [n for n in (wash["note"], climax["note"], trend["note"], policy["note"]) if n]
    return {
        "schema": SCHEMA,
        "code": str(code).zfill(6) if code else None,
        "asof": asof,
        "sample_days": len(seq),
        "wash_accumulation": wash,
        "climax": climax,
        "turnover_trend": trend,
        "dominant_force": dominant,
        "policy": policy,
        "notes": notes,
    }
