"""三桶召回：把全市场公告压成一个可深读的小集合。

这一层是**分流器，不是评分器**。2026-08-04 实测（07-31 / 08-01 双日）：

* Top-N 排序没有唯一解 —— Top20 门槛分 7 分上并列 45 只，取哪 20 只取决于
  接口返回顺序，不是分数；
* anchored 模式下 68~69% 的条目 ``bayes == nss``，双评分实质是单评分。

因此这里不排序、不出名单，只把 ~1450 条打成三个桶（合计 ~165 条 / ~115 家）
交给 Agent 深读：

* **A 分歧** —— ``|bayes − nss| ≥ 3``：规则分与历史统计位置不一致。
* **B 漏网** —— 分类未命中但标题含强事件词。实测漏掉的恰是「核电项目核准」
  「GMP 证书」「一致行动协议」这类高价值非常规事件，命中率 92% 掩盖了这个结构。
* **C 极值** —— ``|nss| ≥ 6`` 且非回购/股权激励常态噪音。

纯计算，无 I/O、无网络。
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Sequence

ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets"
)

BUCKET_DIVERGENCE = "divergence"
BUCKET_UNMATCHED = "unmatched"
BUCKET_EXTREME = "extreme"
BUCKET_ORDER = (BUCKET_DIVERGENCE, BUCKET_UNMATCHED, BUCKET_EXTREME)

# artifact 里每条公告保留的字段。正文与 PDF 一律不进 artifact。
PROJECTION_FIELDS = (
    "code", "name", "title", "l1", "l2", "stage",
    "nss_prior", "bayes_nss", "divergence", "industry_group",
    "polarity_flipped", "bucket", "url",
)


class RecallGuardrailError(RuntimeError):
    """产出规模或数据质量越界，拒绝静默交付。"""


def load_rules(assets_dir: str = ASSETS) -> dict[str, Any]:
    with open(os.path.join(assets_dir, "recall_rules.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _is_scored(row: dict[str, Any]) -> bool:
    return not row.get("skipped") and row.get("nss_prior") is not None


def _in_divergence(row: dict[str, Any], rules: dict[str, Any]) -> bool:
    if not _is_scored(row):
        return False
    threshold = rules["divergence"]["min_abs_divergence"]
    return abs(int(row["bayes_nss"]) - int(row["nss_prior"])) >= threshold


def _in_unmatched(row: dict[str, Any], rules: dict[str, Any]) -> bool:
    if row.get("l2"):
        return False
    title = row.get("title") or ""
    return any(term in title for term in rules["unmatched"]["strong_event_terms"])


def _in_extreme(row: dict[str, Any], rules: dict[str, Any]) -> bool:
    if not _is_scored(row):
        return False
    cfg = rules["extreme"]
    if abs(int(row["nss_prior"])) < cfg["min_abs_nss"]:
        return False
    categories = set(row.get("l2") or [])
    return not categories & set(cfg["exclude_categories"])


_BUCKET_TESTS = {
    BUCKET_DIVERGENCE: _in_divergence,
    BUCKET_UNMATCHED: _in_unmatched,
    BUCKET_EXTREME: _in_extreme,
}


def assign_buckets(row: dict[str, Any], rules: dict[str, Any]) -> list[str]:
    """一条公告可同时落入多个桶；返回值按 BUCKET_ORDER 稳定排序。"""
    return [name for name in BUCKET_ORDER if _BUCKET_TESTS[name](row, rules)]


def _project(row: dict[str, Any], buckets: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in PROJECTION_FIELDS:
        value = row.get(field)
        if field == "bucket":
            value = list(buckets)
        elif isinstance(value, list):
            value = "|".join(value)
        out[field] = value
    return out


def select(rows: Iterable[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """把分类+评分后的全量公告压成召回集。

    返回 ``{"rows": [...], "counts": {...}, "companies": n}``。
    ``rows`` 已投影到 :data:`PROJECTION_FIELDS`，可直接落 artifact。
    """
    selected: list[dict[str, Any]] = []
    counts = dict.fromkeys(BUCKET_ORDER, 0)

    for row in rows:
        buckets = assign_buckets(row, rules)
        if not buckets:
            continue
        for name in buckets:
            counts[name] += 1
        selected.append(_project(row, buckets))

    return {
        "rows": selected,
        "counts": counts,
        "companies": len({r["code"] for r in selected if r.get("code")}),
    }


def check_guardrails(
    *,
    fetched: int,
    classified_rate: float,
    selected: int,
    rules: dict[str, Any],
) -> list[str]:
    """返回告警列表；规模越界直接抛错。

    抓取不足与命中率偏低只告警（周末/半日市/接口未放量都会触发，
    是已知的正常场景），但召回规模越界说明阈值或上游数据变了，
    此时交付一份规模失控的 artifact 比不交付更危险。
    """
    limits = rules["limits"]
    warnings: list[str] = []

    if fetched < limits["min_fetch_rows"]:
        warnings.append(
            f"抓取量 {fetched} 条低于 {limits['min_fetch_rows']}，"
            "可能是非交易日或数据未放量，召回集统计意义受限"
        )
    if classified_rate < limits["min_classification_rate"]:
        warnings.append(
            f"分类命中率 {classified_rate:.1%} 低于 "
            f"{limits['min_classification_rate']:.0%}，需补 taxonomy.json 关键词"
        )

    if fetched >= limits["min_fetch_rows"] and not (
        limits["min_expected_rows"] <= selected <= limits["max_expected_rows"]
    ):
        raise RecallGuardrailError(
            f"召回 {selected} 条越出预期区间 "
            f"[{limits['min_expected_rows']}, {limits['max_expected_rows']}]，"
            "阈值或上游数据已变，拒绝交付；请复跑 2026-07-31 / 2026-08-01 重新定标"
        )

    return warnings


def build_brief(result: dict[str, Any], rules: dict[str, Any], *, day: str) -> str:
    """回上下文的 lite 投影：桶计数 + 每桶前 N 条一行摘要。

    全量 artifact 走文件，**不进上下文**。
    """
    top_n = rules["limits"]["brief_top_per_bucket"]
    labels = {
        BUCKET_DIVERGENCE: "分歧（规则分与历史统计不一致）",
        BUCKET_UNMATCHED: "漏网（分类未命中 + 强事件词）",
        BUCKET_EXTREME: "极值（先验强信号，已剔常态噪音）",
    }

    lines = [
        f"公告召回雷达 {day}：{len(result['rows'])} 条 / "
        f"{result['companies']} 家"
    ]
    for name in BUCKET_ORDER:
        rows = [r for r in result["rows"] if name in r["bucket"]]
        lines.append(f"\n[{labels[name]}] {result['counts'][name]} 条")
        for row in rows[:top_n]:
            score = (
                f"nss {row['nss_prior']}→bayes {row['bayes_nss']}"
                if row.get("nss_prior") is not None
                else "未评分"
            )
            lines.append(
                f"  {row['code']} {row['name']} | {row['l2'] or '未分类'}"
                f"/{row['stage']} | {score} | {row['title'][:34]}"
            )
        if len(rows) > top_n:
            lines.append(f"  …另 {len(rows) - top_n} 条见 artifact")
    return "\n".join(lines)


__all__ = [
    "ASSETS",
    "BUCKET_ORDER",
    "PROJECTION_FIELDS",
    "RecallGuardrailError",
    "assign_buckets",
    "build_brief",
    "check_guardrails",
    "load_rules",
    "select",
]
