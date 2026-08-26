"""晚间复盘清单装配（升级方案 P6 §9.1，源自原书第二十课）。

原书要求每天最少记录：连板梯队、四板以上、涨停溢价、炸板率、涨停数、涨跌比，
再加标杆股状态、大面案例、题材新时广、明日预案、候选失效条件、明日最高总仓、
以及当日系统外交易。方案把它固化成 12 个必填节点。

本模块只做**装配与完备性核算**，不取数、不触网：各节点的证据由已有确定性模块产出
（`sentiment_daily` / `big_loss_ledger` / `discipline_score` / `theme_strength` /
`market_temperature`），这里把它们摆进清单并回答一个问题——**今天这份复盘到底完整
不完整**。

一条贯穿全模块的纪律：**缺项必须显式**。

- 有节点但内容为空（如今天确实没有大面）→ 状态 ``empty``，正文写"今日无"；
- 节点根本没有证据（上游模块没跑/数据缺失）→ 状态 ``missing``，并计入
  ``missing_sections``。

两者绝不能都表现为"这一节不见了"。原书第十四课要求专门复盘大面案例，如果某天因为
采集失败而没有大面节点，读的人会默认"今天没人吃面"——那正是幸存者偏差被系统固化的
方式。同理，``complete`` 只在**零 missing** 时为真，``empty`` 不影响完整性判定。
"""

from __future__ import annotations

from typing import Any, Mapping

OK = "ok"
EMPTY = "empty"
MISSING = "missing"

SCHEMA = "review_checklist_v1"

#: 12 个必填节点（方案 §9.1 的清单表）。顺序即复盘顺序。
SECTIONS: tuple[tuple[str, str], ...] = (
    ("market", "市场数据：涨跌家数 / 涨停 / 跌停 / 炸板率 / 溢价"),
    ("ladder", "连板梯队：1板 / 2板 / 3板 / 4板+，明确最高板"),
    ("benchmarks", "标杆股：前 3 强状态，要写逻辑不只写涨跌"),
    ("big_losses", "大面复盘：当日/次日大亏股，至少 3 只"),
    ("themes", "题材：新、时、广，分主线 / 辅线 / 争夺"),
    ("sectors", "板块：每个热门板块涨停数，≥3 视为有效"),
    ("fund_direction", "资金方向：竞价 / 早盘 / 下午，写清是否切线"),
    ("scenarios", "明日预案：乐观 / 中性 / 悲观，至少 3 套"),
    ("candidates", "候选股：龙头 / 助攻 / 套利，每只写失效条件"),
    ("position_cap", "仓位：明日最高总仓，盘前决定"),
    ("discipline", "心理：今日系统外交易，必须记录"),
    ("errors", "错误：最大错误及修正，可执行不写空话"),
)

#: 原书第十四课：大面案例至少复盘 3 只。少于此数是"够不够"的问题，不是"有没有"。
MIN_BIG_LOSS_CASES = 3

#: 明日预案至少 3 套（乐观/中性/悲观），不押单一剧本。
MIN_SCENARIOS = 3

__all__ = ["OK", "EMPTY", "MISSING", "SCHEMA", "SECTIONS",
           "MIN_BIG_LOSS_CASES", "MIN_SCENARIOS",
           "section_state", "build_checklist", "format_checklist"]


def _is_absent(value: Any) -> bool:
    """区分"没有证据"与"证据说今天是空的"。

    ``None`` 表示上游没给出这一节（missing）；空列表/空字典表示上游跑了、结果为空
    （empty）。这个区别是本模块存在的理由，不要把它们合并成一个 falsy 判断。
    """
    return value is None


def section_state(value: Any, *, min_items: int | None = None) -> dict[str, Any]:
    """单节点状态。

    ``min_items`` 给定时，条目数不足只降级为 ``empty`` 并说明差多少——它是"复盘做得
    够不够"的问题，不是"数据有没有"的问题，因此不计入 missing、不阻断完整性。
    """
    if _is_absent(value):
        return {"status": MISSING, "count": None, "note": "上游未提供该节证据"}
    if isinstance(value, Mapping):
        count = len(value)
    elif isinstance(value, (list, tuple, set)):
        count = len(value)
    elif isinstance(value, str):
        count = 1 if value.strip() else 0
    else:
        count = 1
    if count == 0:
        return {"status": EMPTY, "count": 0, "note": "今日无"}
    if min_items is not None and count < int(min_items):
        return {"status": EMPTY, "count": count,
                "note": f"仅 {count} 条，少于要求的 {int(min_items)} 条"}
    return {"status": OK, "count": count, "note": None}


def build_checklist(evidence: Mapping[str, Any] | None, *, asof: str | None = None
                    ) -> dict[str, Any]:
    """装配当日复盘清单。

    ``evidence`` 的键对应 ``SECTIONS`` 的第一列；**不在 evidence 里的键一律 missing**，
    不会因为"这一节通常没人填"就被悄悄跳过。
    """
    source = dict(evidence or {})
    minimums = {"big_losses": MIN_BIG_LOSS_CASES, "scenarios": MIN_SCENARIOS}
    sections: dict[str, Any] = {}
    for key, title in SECTIONS:
        state = section_state(source.get(key), min_items=minimums.get(key))
        sections[key] = {"title": title, **state}
    missing = [key for key, item in sections.items() if item["status"] == MISSING]
    empty = [key for key, item in sections.items() if item["status"] == EMPTY]
    return {
        "schema": SCHEMA,
        "asof": asof,
        "sections": sections,
        "missing_sections": missing,
        "empty_sections": empty,
        # 完整性只看 missing：某天确实没有大面是事实，不是复盘没做。
        "complete": not missing,
        "note": ("complete 只在零 missing 时为真；empty 表示上游跑了但今天没有内容，"
                 "不影响完整性判定。"),
    }


def format_checklist(checklist: Mapping[str, Any] | None) -> str:
    """渲染成人读文本。缺项与空项都必须出现在输出里，不允许静默消失。"""
    data = dict(checklist or {})
    sections = data.get("sections") or {}
    lines = [f"## 晚间复盘清单 | {data.get('asof') or '未指定日期'}"]
    if not sections:
        return "\n".join(lines + ["", "⚠️ 无任何证据，复盘未开始。"])
    marks = {OK: "[x]", EMPTY: "[-]", MISSING: "[ ]"}
    for key, _title in SECTIONS:
        item = sections.get(key) or {}
        mark = marks.get(item.get("status"), "[ ]")
        suffix = f" —— {item['note']}" if item.get("note") else ""
        lines.append(f"{mark} {item.get('title', key)}{suffix}")
    missing = data.get("missing_sections") or []
    if missing:
        lines += ["", f"⚠️ 缺 {len(missing)} 节未采集：{'、'.join(missing)}。"
                      "缺项不等于今日无——先补数据再下结论。"]
    else:
        lines += ["", "✅ 12 节齐备（标 [-] 的是今日确实无内容，非缺采）。"]
    return "\n".join(lines)
