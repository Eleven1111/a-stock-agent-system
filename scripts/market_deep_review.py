#!/usr/bin/env python3
"""Deterministic, fail-closed post-close A-share market review."""

from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = "market_deep_review_v1"
ROOT = Path(__file__).resolve().parents[1]
JOB_IDS = (
    "market-pulse-1500",
    "capital-flow",
    "theme-strength-daily",
    "news-daily-brief",
    "global-evening",
    "official-policy-watch",
)


def _state() -> Path:
    return Path(os.environ.get("A_STOCK_STATE_HOME", str(Path.home() / ".hermes")))


def _number(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _json_stdout(artifact: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(str(artifact.get("stdout") or ""))
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _artifact(job: str, trading_date: str) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = sorted(glob.glob(str(_state() / "cron/output" / job / "*.json")), key=os.path.getmtime, reverse=True)
    for path in paths:
        try:
            outer = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if outer.get("trading_date") == trading_date:
            return outer, _json_stdout(outer)
    return {}, {}


def _field(value: Any, source: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    result = {"value": value if value is not None else "unavailable", "source": source, "status": status}
    result.update(extra)
    return result


def _quotes(trading_date: str) -> tuple[dict[str, dict[str, Any]], str]:
    path = _state() / "skills/stock-triage/data/universe_quotes_cache.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if str(payload.get("updated_at", ""))[:10] == trading_date and isinstance(payload.get("quotes"), dict):
            return payload["quotes"], "universe_quotes_cache_v1/tencent-adapter-v3"
    except (OSError, json.JSONDecodeError):
        pass
    return {}, "unavailable"


def _sentiment(trading_date: str) -> dict[str, Any]:
    path = _state() / "market/sentiment_daily" / f"{trading_date}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("record", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _signal_context(trading_date: str) -> dict[str, Any]:
    path = _state() / "skills/stock-triage/cache/signal_context.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("trading_date") in (None, trading_date):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _metric(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return None


def _load_inputs(trading_date: str) -> dict[str, Any]:
    jobs = {job: _artifact(job, trading_date) for job in JOB_IDS}
    quotes, quote_source = _quotes(trading_date)
    return {
        "jobs": jobs,
        "quotes": quotes,
        "quote_source": quote_source,
        "sentiment": _sentiment(trading_date),
        "signal_context": _signal_context(trading_date),
    }


def _quote_metrics(quotes: dict[str, Any]) -> dict[str, Any]:
    valid = [q for q in quotes.values() if isinstance(q, dict) and _number(q.get("change_pct")) is not None]
    changes = [_number(q.get("change_pct")) for q in valid]
    return {
        "valid": valid,
        "amount": sum(_number(q.get("amount")) or 0 for q in valid) if valid else None,
        "advances": sum(x > 0 for x in changes),
        "declines": sum(x < 0 for x in changes),
        "limitups": sum(x >= 9.8 for x in changes),
        "limitdowns": sum(x <= -9.8 for x in changes),
    }


def _flow_data(inputs: dict[str, Any]) -> dict[str, Any]:
    outer, flow = inputs["jobs"]["capital-flow"]
    signal_context = inputs["signal_context"]
    flow_status = str(flow.get("status") or outer.get("status") or "unavailable")
    scanned = [
        _number(item.get("main_net_yi"))
        for item in (signal_context.get("stock_flows") or {}).values()
        if isinstance(item, dict) and _number(item.get("main_net_yi")) is not None
    ]
    names = ("main", "super_large", "large", "medium", "small", "ddx", "ddy", "ddz")
    fields = {name: _field(flow.get(name), "capital_flow_v2", flow_status) for name in names}
    if not flow:
        fields = {name: _field(None, "capital-flow artifact stdout", "unavailable") for name in names}
        if scanned:
            fields["main"] = _field(
                round(sum(scanned), 4), "signal_context.stock_flows", "degraded",
                coverage=f"{len(scanned)} scanned stocks; not market-wide",
            )
    return {"outer": outer, "flow": flow, "status": flow_status, "fields": fields, "scanned": scanned}


def _theme_data(inputs: dict[str, Any]) -> dict[str, Any]:
    outer, themes = inputs["jobs"]["theme-strength-daily"]
    transitions = themes.get("transitions") or themes.get("themes") or []
    strong = [x for x in transitions if isinstance(x, dict) and x.get("stage") in {"mainline", "diverging"}]
    return {"outer": outer, "themes": themes, "strong": strong}


def _score_data(metrics: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    valid = metrics["valid"]
    inputs = {
        "advance_decline": metrics["advances"] > metrics["declines"] if valid else None,
        "limit_up_down": metrics["limitups"] > metrics["limitdowns"] if valid else None,
        "theme_transitions": bool(theme["strong"]) if theme["themes"] else None,
        "flow_direction": None,
    }
    known = [value for value in inputs.values() if value is not None]
    score = round(sum(value is True for value in known) / len(known) * 100, 1) if known else None
    return {"inputs": inputs, "score": score, "status": "ok" if len(known) == 4 else ("degraded" if known else "unavailable")}


def _index_section() -> dict[str, Any]:
    return {
        "status": "unavailable",
        "note": "现有收盘产物未提供指数级收盘/涨跌幅/振幅；不得用个股汇总冒充指数",
        "fields": {key: _field(None, "unavailable", "unavailable") for key in ("close", "change_pct", "amount", "turnover_rate", "amplitude")},
    }


def _flow_section(flow: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "degraded" if flow["scanned"] and not flow["flow"] else (flow["status"] if flow["flow"] else "unavailable"),
        "fields": flow["fields"],
        "note": "只采用 capital_flow_v2；指数成交额未转作资金流",
    }


def _breadth_section(metrics: dict[str, Any], source: str) -> dict[str, Any]:
    valid = metrics["valid"]
    fields = {}
    for key, value in (("advance", metrics["advances"]), ("decline", metrics["declines"]), ("limit_up", metrics["limitups"]), ("limit_down", metrics["limitdowns"])):
        fields[key] = _field(value, source) if valid else _field(None, source, "unavailable")
    fields["up_down_ratio"] = _field(round(metrics["advances"] / metrics["declines"], 3) if metrics["declines"] else None, source, "ok" if metrics["declines"] else "unavailable")
    return {"status": "ok" if valid else "unavailable", "fields": fields}


def _structure_section(inputs: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    pulse = inputs["jobs"]["market-pulse-1500"][0]
    news = inputs["jobs"]["news-daily-brief"][1]
    return {"fields": {
        "trend_review": _field(None, "market-pulse-1500", "unavailable" if not pulse.get("stdout") else "ok"),
        "sector_strength": _field(theme["strong"], "theme_strength_v1", "ok" if theme["themes"] else "unavailable"),
        "core_logic": _field((news.get("aggregate") or {}).get("new_items", [])[:5], "news_daily_brief_v1", "ok" if news else "unavailable"),
    }}


def _sentiment_section(sentiment: dict[str, Any], theme: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    status = "ok" if sentiment else "unavailable"
    return {"fields": {
        "money_making_effect": _field(sentiment.get("adr"), "sentiment_daily_v1", status),
        "fund_direction": _field(None, "unavailable", "unavailable"),
        "volume_contraction": _field(None, "unavailable", "unavailable"),
        "sector_rotation": _field(len(theme["strong"]) if theme["themes"] else None, "theme_strength_v1", "ok" if theme["themes"] else "unavailable"),
        "limit_board_sentiment": _field(sentiment.get("max_board"), "sentiment_daily_v1", status),
        "composite_score": _field(score["score"], "transparent_rule: available evidence true/known count", score["status"], evidence=score["inputs"]),
    }}


def _build_sections(inputs: dict[str, Any], metrics: dict[str, Any], flow: dict[str, Any], theme: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    return {
        "index_performance": _index_section(),
        "capital_flow": _flow_section(flow),
        "stock_breadth": _breadth_section(metrics, inputs["quote_source"]),
        "market_structure": _structure_section(inputs, theme),
        "sentiment": _sentiment_section(inputs["sentiment"], theme, score),
        "institutional_views": {"status": "unavailable", "note": "暂无可验证机构观点；现有新闻/政策产物不等同于研报"},
        "conclusion": {
            "one_sentence": "基于现有可验证产物，指数级表现与机构观点不可用，个股广度可供参考但不足以形成完整大盘结论。",
            "strategy": "数据完整前保持谨慎，不据缺失字段交易。",
            "next_day_watch": "观察指数收盘与成交额、真实资金流及涨跌停结构是否补齐。",
        },
    }


def _evidence(inputs: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    jobs = inputs["jobs"]
    return {
        "artifacts": {job: bool(jobs[job][0]) for job in JOB_IDS},
        "global_evening_status": jobs["global-evening"][1].get("source_health") if jobs["global-evening"][1] else "unavailable",
        "policy_status": jobs["official-policy-watch"][1].get("status") if jobs["official-policy-watch"][1] else "unavailable",
        "quote_count": len(metrics["valid"]),
        "amount_yi_proxy": round(metrics["amount"] / 1e8, 2) if metrics["amount"] is not None else "unavailable",
    }


def _build(trading_date: str) -> dict[str, Any]:
    inputs = _load_inputs(trading_date)
    metrics = _quote_metrics(inputs["quotes"])
    flow = _flow_data(inputs)
    theme = _theme_data(inputs)
    score = _score_data(metrics, theme)
    return {
        "schema": SCHEMA,
        "status": "ok" if metrics["valid"] else "degraded",
        "date": trading_date,
        "sources": {
            "market_quotes": {"source": inputs["quote_source"], "status": "ok" if metrics["valid"] else "unavailable", "asof": trading_date},
            "sentiment_daily": {"source": "sentiment_daily_v1", "status": "ok" if inputs["sentiment"] else "unavailable", "asof": trading_date},
        },
        "sections": _build_sections(inputs, metrics, flow, theme, score),
        "evidence": _evidence(inputs, metrics),
    }


def core_data_available(report: dict[str, Any]) -> bool:
    """Return whether at least one real close-market core signal exists.

    Breadth from the dated quote cache is a core close signal.  It is enough to
    produce a degraded report when optional capital-flow fields are absent, but
    an entirely empty close input must remain fail-closed.
    """
    return report.get("evidence", {}).get("quote_count", 0) > 0


def render(report: dict[str, Any]) -> str:
    s = report["sections"]
    d = report["date"]

    def val(section: str, field: str) -> Any:
        item = s[section]["fields"].get(field, {})
        return f"{item.get('value', 'unavailable')}（source={item.get('source', 'unavailable')}; status={item.get('status', 'unavailable')}）"

    lines = [
        f"# A股大盘深度复盘 | {d}",
        "",
        "> 字段均保留 source/status；unavailable 不代表为零。",
        "",
        "## 一、指数表现",
        *[
            f"- {k}: {val('index_performance', k)}"
            for k in ("close", "change_pct", "amount", "turnover_rate", "amplitude")
        ],
        "",
        "## 二、资金流向",
        *[
            f"- {k}: {val('capital_flow', k)}"
            for k in ("main", "super_large", "large", "medium", "small", "ddx", "ddy", "ddz")
        ],
        "",
        "## 三、个股涨跌",
        *[
            f"- {k}: {val('stock_breadth', k)}"
            for k in ("advance", "decline", "limit_up", "limit_down", "up_down_ratio")
        ],
        "",
        "## 四、盘面结构",
        f"- 走势回顾: {val('market_structure', 'trend_review')}",
        f"- 板块强弱: {val('market_structure', 'sector_strength')}",
        f"- 核心逻辑: {val('market_structure', 'core_logic')}",
        "",
        "## 五、情绪判断",
        *[
            f"- {k}: {val('sentiment', k)}"
            for k in (
                "money_making_effect",
                "fund_direction",
                "volume_contraction",
                "sector_rotation",
                "limit_board_sentiment",
                "composite_score",
            )
        ],
        "",
        "## 六、机构观点摘要",
        f"- {s['institutional_views']['note']}",
        "",
        "## 七、结论",
        f"- 一句话：{s['conclusion']['one_sentence']}",
        f"- 策略：{s['conclusion']['strategy']}",
        f"- 次日观察：{s['conclusion']['next_day_watch']}",
    ]
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--json", action="store_true")
    a = p.parse_args()
    report = _build(a.date)
    if not core_data_available(report):
        # Do not emit a seven-section artifact or a push when the dated core
        # close input is absent.  The runner records this as blocked (75).
        print(json.dumps({
            "schema": SCHEMA,
            "status": "blocked",
            "date": a.date,
            "reason_code": "core_close_data_missing",
            "message": "核心收盘数据/涨跌结构全部缺失；不生成估算报告",
        }, ensure_ascii=False))
        return 75
    print(json.dumps(report, ensure_ascii=False, indent=2) if a.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
