"""Morning note aggregation and Markdown rendering."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Sequence


SCHEMA = "morning_note_v1"


def _items(payload: Mapping[str, Any] | None, keys: Sequence[str]) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            out.extend(dict(item) for item in value if isinstance(item, Mapping))
    return out


def _headline(item: Mapping[str, Any]) -> str:
    for key in ("title", "summary", "message", "name", "label"):
        value = item.get(key)
        if value:
            return str(value)
    return json.dumps(dict(item), ensure_ascii=False)[:120]


def _top(items: Sequence[Mapping[str, Any]], limit: int = 5) -> list[str]:
    return [_headline(item) for item in items[:limit] if _headline(item)]


def build_morning_note(
    *,
    trading_date: str,
    batch_id: str | None = None,
    global_context: Mapping[str, Any] | None = None,
    news_context: Mapping[str, Any] | None = None,
    company_events_context: Mapping[str, Any] | None = None,
    event_calendar_context: Mapping[str, Any] | None = None,
    portfolio: Mapping[str, Any] | None = None,
    monitor_registry: Sequence[Mapping[str, Any]] | None = None,
    candidate_pool: Mapping[str, Any] | None = None,
    behavioral_context: Mapping[str, Any] | None = None,
    missing_inputs: Sequence[str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().isoformat(timespec="seconds")
    overnight = _top(_items(global_context, ("events", "signals", "alerts", "developments")), 5)
    overnight.extend(_top(_items(news_context, ("events", "signals", "news", "alerts")), 5))
    company_events = _top(_items(company_events_context, ("opportunities", "events", "signals")), 5)
    policy_macro = _top(_items(news_context, ("policy_macro", "policy", "macro")), 4)
    key_events_today = _top(_items(event_calendar_context, ("events", "items", "today")), 5)

    trade_ideas: list[str] = []
    for item in (candidate_pool or {}).get("candidates") or []:
        if not isinstance(item, Mapping):
            continue
        label = item.get("name") or item.get("code")
        reason = item.get("reason") or item.get("summary") or item.get("recall_source")
        if label and reason:
            trade_ideas.append(f"{label}：{reason}")
        if len(trade_ideas) >= 3:
            break
    for item in (company_events_context or {}).get("opportunities") or []:
        if not isinstance(item, Mapping) or item.get("suggestion") not in {"watch", "review"}:
            continue
        trade_ideas.append(f"{item.get('name') or item.get('code')}：公司事件{item.get('suggestion')}")
        if len(trade_ideas) >= 5:
            break

    risk_watch: list[str] = []
    behavior_phase = (behavioral_context or {}).get("sentiment_phase")
    if behavior_phase and behavior_phase != "unknown":
        risk_watch.append(f"行为金融：{behavior_phase}")
    for item in (company_events_context or {}).get("opportunities") or []:
        if isinstance(item, Mapping) and item.get("suggestion") == "avoid":
            risk_watch.append(f"{item.get('name') or item.get('code')}：{item.get('event_label') or item.get('event_type')}")
        if len(risk_watch) >= 5:
            break

    has_material = bool(overnight or company_events or policy_macro or key_events_today or trade_ideas or risk_watch)
    top_call = (
        "隔夜无重大变化，维持原计划"
        if not has_material
        else (overnight[0] if overnight else company_events[0] if company_events else risk_watch[0])
    )
    status = "ready" if has_material else "no_signal"
    return {
        "schema": SCHEMA,
        "generated_at": generated,
        "trading_date": trading_date,
        "batch_id": batch_id,
        "status": status,
        "top_call": top_call,
        "overnight_developments": overnight or ["隔夜无重大变化"],
        "company_events": company_events,
        "policy_macro": policy_macro,
        "key_events_today": key_events_today,
        "trade_ideas": trade_ideas,
        "risk_watch": risk_watch,
        "missing_inputs": list(missing_inputs or []),
        "summary": {
            "top_call": top_call,
            "overnight_count": 0 if overnight == ["隔夜无重大变化"] else len(overnight),
            "company_event_count": len(company_events),
            "trade_idea_count": len(trade_ideas),
        },
        "has_signal": has_material,
        "missing_errors": list(missing_inputs or []),
        "unavailable": list(missing_inputs or []),
    }


def render_morning_note_markdown(note: Mapping[str, Any], *, max_chars: int = 4500) -> str:
    def section(title: str, values: Sequence[Any]) -> list[str]:
        lines = [f"**{title}**"]
        if not values:
            lines.append("- 无")
        else:
            lines.extend(f"- {value}" for value in values[:8])
        return lines

    lines = [
        f"## {note.get('trading_date')} Morning Note",
        "",
        f"**Top Call**：{note.get('top_call') or '隔夜无重大变化'}",
        "",
        *section("隔夜/盘前", note.get("overnight_developments") or []),
        "",
        *section("公司事件", note.get("company_events") or []),
        "",
        *section("政策/宏观", note.get("policy_macro") or []),
        "",
        *section("今日重点", note.get("key_events_today") or []),
        "",
        *section("交易想法", note.get("trade_ideas") or []),
        "",
        *section("风险", note.get("risk_watch") or []),
    ]
    if note.get("missing_inputs"):
        lines.extend(["", *section("缺失输入", note.get("missing_inputs") or [])])
    text = "\n".join(lines).strip()
    limit = max(200, int(max_chars))
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
