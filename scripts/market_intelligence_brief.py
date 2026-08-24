#!/usr/bin/env python3
"""Render bounded morning intelligence from already-persisted stage outputs."""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import data_file  # noqa: E402
from state_store import read_json  # noqa: E402
import stage_intelligence  # noqa: E402


STAGE_PATHS = {
    "preopen": ("stock-triage", "candidate_pool_latest.json"),
    "auction": ("daban-stock-picker", "auction_shortlist_latest.json"),
    "open": ("daban-stock-picker", "open_confirmation_latest.json"),
}

STAGE_LABELS = {
    "preopen": "早盘情报简报",
    "auction": "集合竞价简报",
    "open": "开盘摘要",
}


def _label(item: Mapping[str, Any]) -> str:
    return f"{item.get('name') or item.get('code')}({item.get('code')})"


def _score(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        if item.get(key) is not None:
            return str(item.get(key))
    return "-"


def _sector_momentum_lines() -> list[str]:
    """从 signal_context 提取板块动量/轮动摘要（缺失/过期时静默省略）。"""
    try:
        from signal_context import read_signal_context

        ctx = read_signal_context() or {}
    except Exception:  # noqa: BLE001 — 简报缺一段不缺整份
        return []
    lines: list[str] = []
    momentum = ctx.get("sector_momentum") or {}
    hot = [
        f"{entry.get('name')}({entry.get('signal')})"
        for entry in momentum.get("sectors") or []
        if entry.get("signal") in ("strong", "emerging")
    ][:5]
    if hot:
        lines.append("板块动量：" + "、".join(hot))
    rotation = (ctx.get("sector_rotation") or {}).get("rotation_signal")
    if rotation:
        lines.append(f"板块轮动：{rotation}")
    return lines


_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _shanghai_time_label(generated_at: Any) -> str:
    """判定时点标签，统一折算到 Asia/Shanghai。

    这一行的用途是免责（"仅代表开盘阶段"），所以标错时区比不标更糟：上游若
    写入带 Z/+00:00 的 UTC 时戳，字符串切片会把 UTC 时刻当成北京时间显示。
    无时区信息的时戳按本地时间处理（与既有写入方一致），解析失败则不标。
    """
    raw = str(generated_at or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return "开盘阶段"
    if parsed.tzinfo is None:
        return parsed.strftime("%H:%M")
    return parsed.astimezone(_SHANGHAI).strftime("%H:%M")


def _preopen_no_candidate_line(result: Mapping[str, Any]) -> str:
    """Explain an empty pre-open pool without turning missing data into a regime.

    issue #260 §2.8：tier/context_fresh 实际位于 ``market_timing.temperature``
    之下，不在 ``market_timing`` 顶层——此前直接读顶层字段恒为空，导致这条
    分支恒判定为"证据未就绪"，双门禁落地后会继续误报。
    """
    selection = result.get("hot_money_selection") or {}
    timing = selection.get("market_timing") or {}
    temperature = timing.get("temperature") or {}
    # status 是选股就绪状态（ready / insufficient_data），tier 是温度档位——
    # 两者不同源。温度不可用时 _unavailable_temperature 会把 tier 直接设成
    # 状态字符串，故 tier 侧必须同时排除 stale 与 unknown，否则「选股就绪但
    # 温度未知」会掉进弱市分支，又把缺数据说成 regime。
    status = str(timing.get("status") or "")
    tier = str(temperature.get("tier") or "")
    fresh = temperature.get("context_fresh")
    if (
        status in {"insufficient_data", "unknown"}
        or tier in {"", "stale", "unknown"}
        or fresh is False
    ):
        return "⚠️ 盘前择时证据未就绪，暂不判定市场强弱；等待开盘确认"
    market_gate = dict(timing.get("market_gate") or {})
    local_theme_count = len(result.get("local_theme_candidates") or [])
    if market_gate.get("status") == "restricted" and local_theme_count:
        return (
            f"⚠️ 全局新增风险受限（{market_gate.get('temperature_substate') or tier}），"
            f"但发现 {local_theme_count} 只局部板块共振观察标的；等待竞价/开盘确认"
        )
    weak = (timing.get("weak_market") or {}).get("weak_regime")
    if weak:
        return "⚠️ 盘前弱市门禁生效，候选暂降级为 research_only；等待开盘确认"
    return "⚠️ 盘前暂无可交付候选，等待开盘确认"


def _market_gate_line(market_gate: Mapping[str, Any]) -> str:
    """issue #260 §4.5：区分"全局无机会"/"全局受限但有局部强势"/"数据不足无法判断"。"""
    status = market_gate.get("status")
    if status == "open":
        return "市场门禁：open（全局新增风险未受限，走现有门禁）"
    if status == "restricted":
        return (
            f"市场门禁：restricted（{market_gate.get('temperature_substate') or '冰点杀跌'}，"
            "全局新增仓关闭，可评估局部板块共振）"
        )
    if status == "blocked":
        return "市场门禁：blocked（数据不可信或全局战略禁入，局部主题不豁免）"
    return "市场门禁：未知（数据不足，无法判断全局强弱）"


def _degraded_lines(result: Mapping[str, Any], *, tail: str) -> list[str]:
    """降级警示行；未降级返回空列表。

    空榜单/无信号有两种含义：真的没有机会，或根本没采到数据。后者必须说出来，
    否则读者会把"没有观测"读成"没有行情"（issue #112 / #113）。
    """
    if str(result.get("status")) != "degraded":
        return []
    lines = [f"⚠️ 数据降级，{tail}"]
    reasons = "；".join(str(item) for item in result.get("degraded_reasons") or [])
    if reasons:
        lines.append(f"原因：{reasons}")
    return lines


DECISION_LABELS = {
    "buy": "买入",
    "add": "加仓",
    "conditional_buy": "条件买入",
    "watch": "观望",
    "avoid": "回避",
    "sell": "卖出",
    "reduce": "减仓",
    "hold_locked": "T+1锁定",
}

REASON_LABELS = {
    "strategy_unverified": "策略未验证",
    "strategy_not_allowed": "策略未启用",
    "quality_rejected": "质检未通过",
    "quality_not_passed": "质检未达标",
    "market_risk_off": "市场避险",
    "market_context_unknown": "市场环境未知",
    "market_context_stale": "市场环境过期",
    "existing_position_sector_unknown": "持仓板块未知",
    "single_position_limit": "单一持仓超限",
    "sector_exposure_limit": "板块暴露超限",
    "not_tradeable": "不可成交",
    "required_fields_missing": "关键字段缺失",
    "day_loss_stop": "当日亏损熔断",
    "week_trade_cap": "周交易次数上限",
    "week_loss_freeze": "周亏损冻结",
    "consecutive_losses_freeze": "连亏冻结",
    "announcement_hard_risk": "公告硬风险",
    "market_intelligence_hard_risk": "情报硬风险",
    "chanlun_live_bearish_signal": "缠论看空",
    "serenity_hard_risk": "serenity硬风险",
    "crowding_climax_reduced": "拥挤高潮降仓",
    "market_state_ebbing_reduced": "退潮降仓",
    "reflexivity_leader_isolation": "龙头孤立",
    "reflexivity_algorithmic_false_consensus": "算法伪共识",
    "reflexivity_institution_distribution": "机构派发",
}


def _decision_label(item: Mapping[str, Any]) -> str:
    decision = str(item.get("decision") or "").strip()
    if not decision:
        return "未评估"
    return DECISION_LABELS.get(decision, decision)


def _auction_lines(result: Mapping[str, Any], asof: str) -> list[str]:
    digest = stage_intelligence.auction_digest(result)
    lines = [
        f"## 集合竞价简报 | {asof}",
        f"全市场有效竞价因子：{digest['full_market_factor_count']}",
        f"业务结果：{digest.get('outcome_status') or 'unknown'}｜"
        f"研究{digest.get('research_count', 0)}｜执行{digest.get('execution_count', 0)}｜"
        f"扫描{digest.get('auction_scan_count', 0)}",
    ]
    if result.get("market_intelligence_degraded"):
        reasons = "；".join(
            str(item) for item in (result.get("market_intelligence") or {}).get("reasons") or []
        )
        lines.append("⚠️ 全市场增强情报降级，核心候选竞价收口不受影响")
        if reasons:
            lines.append(f"增强链原因：{reasons}")
    lines.extend(_degraded_lines(
        result,
        tail=f"collection_status={result.get('collection_status') or 'unknown'}，"
             "以下榜单不可用作决策依据",
    ))
    for title, items in (
        ("### 竞价涨幅 TOP", digest["market_gainers"]),
        ("### 竞价跌幅 TOP", digest["market_decliners"]),
    ):
        lines.append(title)
        for item in items:
            scope = "执行短名单" if item["in_execution_shortlist"] else "池外研究情报"
            lines.append(f"- {_label(item)}：{_score(item, 'auction_gap_pct')}%｜{scope}")
    if digest["high_daban_candidates"]:
        lines.append("### 打板评分≥90")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'daban_score')}｜"
            f"{_decision_label(item)}"
            for item in digest["high_daban_candidates"]
        )
    lines.append("### 研究评分 TOP（research_only）")
    if digest["research_top"]:
        lines.extend(
            f"- {_label(item)}：竞价分{_score(item, 'auction_score')}｜research_only"
            for item in digest["research_top"]
        )
    else:
        lines.append("- 无可用研究评分")
    lines.append("### 可执行候选")
    if digest["execution_candidates"]:
        lines.extend(
            f"- {_label(item)}：竞价分{_score(item, 'auction_score')}"
            for item in digest["execution_candidates"]
        )
    else:
        lines.append("- 无")
        reasons = list((digest.get("gate") or {}).get("reasons") or [])
        if reasons:
            lines.append("- 原因：" + "；".join(str(item) for item in reasons))
    if digest["decisions"]:
        lines.append("### 买卖决策建议")
        for item in digest["decisions"]:
            reasons = "；".join(
                REASON_LABELS.get(r, r) for r in item.get("reasons") or []
            )
            tail = f"（{reasons}）" if reasons else ""
            lines.append(
                f"- {_label(item)}：{_decision_label(item)}{tail}"
            )
    return lines


def _open_lines(result: Mapping[str, Any], asof: str) -> list[str]:
    digest = stage_intelligence.open_digest(result)
    temperature = result.get("market_temperature") or {}
    regime = result.get("market_regime") or {}
    time_label = _shanghai_time_label(result.get("generated_at"))
    lines = [
        f"## 开盘摘要 | {asof}",
        f"判定时点：{time_label}（仅代表开盘阶段，不代表全天走势）",
        f"市场概况：温度={temperature.get('tier') or 'N/A'}｜"
        f"状态={regime.get('regime') or regime.get('label') or 'N/A'}",
    ]
    # 降级时档位仍如实显示（诊断线索），但必须点明风险预算已归零——
    # 否则"温度=发酵｜无可执行信号"会被读成"市场不错，只是今天没标的"。
    lines.extend(_degraded_lines(
        result, tail="上方温度档位不代表可参与，新仓已阻断",
    ))
    lines.append("### 门禁后信号")
    if digest["signals"]:
        lines.extend(
            f"- {_label(item)}：{item.get('decision') or item.get('action')}｜"
            f"开盘分{_score(item, 'open_score')}"
            for item in digest["signals"]
        )
    else:
        lines.append("- 无可执行信号")
    if digest["filtered_highlights"]:
        lines.append("### 被过滤高分票（研究情报）")
        for item in digest["filtered_highlights"]:
            reasons = "；".join(item.get("filter_reasons") or [])
            lines.append(
                f"- {_label(item)}：打板{_score(item, 'daban_score', 'auction_daban_score')}｜{reasons}"
            )
    return lines


def format_brief(stage: str, result: Mapping[str, Any], *, max_chars: int = 2400) -> str:
    asof = result.get("asof") or "unknown"
    lines: list[str] = []
    if stage == "preopen":
        digest = stage_intelligence.preopen_digest(result)
        lines.extend([
            f"## 早盘情报简报 | {asof}",
            f"全市场{digest['scanned_count']}｜合格{digest['eligible_count']}｜"
            f"研究池{digest['research_count']}｜执行池{digest['execution_count']}｜"
            f"局部主题观察池{digest['local_theme_count']}｜"
            f"竞价扫描预备{digest['auction_scan_count']}",
            _market_gate_line(digest.get("market_gate") or {}),
        ])
        lines.extend(_sector_momentum_lines())
        lines.append("### 研究评分 TOP（research_only）")
        lines.append("#### 打板评分 TOP")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'daban_score')}｜research_only"
            for item in digest["top_daban"]
        )
        lines.append("#### 趋势评分 TOP")
        lines.extend(
            f"- {_label(item)}：{_score(item, 'trend_score')}｜research_only"
            for item in digest["top_trend"]
        )
        lines.append("### 可执行候选")
        if digest["execution_candidates"]:
            lines.extend(
                f"- {_label(item)}：打板{_score(item, 'daban_score')}｜"
                f"趋势{_score(item, 'trend_score')}"
                for item in digest["execution_candidates"]
            )
        else:
            lines.append("- 无")
            reasons = list((digest.get("gate") or {}).get("reasons") or [])
            if reasons:
                lines.append("- 原因：" + "；".join(str(item) for item in reasons))
            else:
                lines.append("- " + _preopen_no_candidate_line(result))
        if digest.get("local_theme_candidates"):
            lines.append("### 局部主题观察（research_only，不可执行）")
            lines.extend(
                f"- {_label(item)}：{item.get('sector') or '-'}"
                for item in digest["local_theme_candidates"]
            )
    elif stage == "auction":
        lines.extend(_auction_lines(result, asof))
    elif stage == "open":
        lines.extend(_open_lines(result, asof))
    else:
        raise ValueError(f"unknown stage: {stage}")
    text = "\n".join(lines).strip()
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


def load_stage(stage: str, *, asof: str) -> dict[str, Any]:
    skill, filename = STAGE_PATHS[stage]
    result = read_json(data_file(skill, filename), {})
    accepted = {"ready"} if stage == "preopen" else {"ready", "degraded"}
    if not isinstance(result, dict) or result.get("status") not in accepted:
        return {}
    if stage in {"auction", "open"} and str(result.get("asof") or "") != asof:
        return {}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=sorted(STAGE_PATHS), required=True)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--max-chars", type=int, default=2400)
    args = parser.parse_args()
    result = load_stage(args.stage, asof=args.asof)
    if result:
        print(format_brief(args.stage, result, max_chars=args.max_chars))
    else:
        print(
            f"⚠️ {STAGE_LABELS[args.stage]}未生成 | {args.asof} | "
            "上游快照缺失或过期，请检查对应采集任务。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
