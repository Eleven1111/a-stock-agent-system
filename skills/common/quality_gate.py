"""Data quality gate for the DAG pipeline — hard checks before data enters analysis.

Two-layer validation:

1. Layer 1 (hard check): null-ratio, freshness, key-field completeness
2. Layer 2 (marker): per-source grade for downstream decisions

Usage:
    from quality_gate import check_quality, quality_gate_summary

    report = check_quality("northbound_flow", data, {
        "source": "northbound_flow",
        "expected_keys": ["date", "net_flow_yi"],
        "max_null_pct": 50.0,
        "max_age_hours": 72,
        "date_key": "date",
    })
    if not report["passed"]:
        downgrade_or_alert(report)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

# ── Quality grades ──────────────────────────────────────────────────────────
GRADE_GREEN = "green"      # ✓ 数据质量良好，可以直接使用
GRADE_YELLOW = "yellow"    # ⚠ 部分字段异常，谨慎使用
GRADE_RED = "red"          # ✗ 数据质量不达标，建议降级或告警

# ── Default thresholds ──────────────────────────────────────────────────────
DEFAULT_THRESHOLDS: dict[str, dict[str, Any]] = {
    "northbound_flow": {
        "expected_keys": ["date", "net_flow_yi"],
        "max_null_pct": 50.0,
        "max_age_hours": 72,
        "date_key": "date",
    },
    "board_quotes": {
        "expected_keys": ["f12", "f14", "f3"],
        "max_null_pct": 30.0,
        "max_age_hours": 48,
        "date_key": None,
    },
    "capital_flow": {
        "expected_keys": ["code", "net_inflow"],
        "max_null_pct": 40.0,
        "max_age_hours": 48,
        "date_key": None,
    },
    "tencent_quote": {
        "expected_keys": ["code", "price", "change_pct"],
        "max_null_pct": 20.0,
        "max_age_hours": 24,
        "date_key": None,
    },
    "reports": {
        "expected_keys": ["title", "institution"],
        "max_null_pct": 60.0,
        "max_age_hours": 720,  # 30 days — 研报可以较老
        "date_key": "date",
    },
    "news": {
        "expected_keys": ["title", "url"],
        "max_null_pct": 30.0,
        "max_age_hours": 48,
        "date_key": "date",
    },
    "serper_news": {
        "expected_keys": ["title", "link"],
        "max_null_pct": 30.0,
        "max_age_hours": 72,
        "date_key": "date",
    },
    "fund_flow": {
        "expected_keys": ["code", "net_amount"],
        "max_null_pct": 40.0,
        "max_age_hours": 48,
        "date_key": None,
    },
    "industry_comparison": {
        "expected_keys": ["name", "change_pct"],
        "max_null_pct": 20.0,
        "max_age_hours": 24,
        "date_key": None,
    },
}


def _null_pct(items: Sequence[Mapping[str, Any]], key: str) -> float:
    """计算某个 key 的空值比例（包括 None / NaN / \"\" / 0 的特殊处理）。"""
    if not items:
        return 100.0
    null_count = sum(
        1 for item in items
        if item.get(key) is None or (isinstance(item.get(key), str) and not item[key])
    )
    return null_count / len(items) * 100.0


def _most_recent_date(items: Sequence[Mapping[str, Any]], date_key: str) -> str | None:
    """找最新日期（支持 ISO 格式字符串和 date 对象）。"""
    latest: str | None = None
    for item in items:
        val = item.get(date_key)
        if val is None:
            continue
        d = str(val)[:10]
        if latest is None or d > latest:
            latest = d
    return latest


def _freshness_hours(date_str: str | None) -> float | None:
    """计算数据最新日期距离现在的时数。"""
    if not date_str:
        return None
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        diff = datetime.now() - dt
        return diff.total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def check_quality(
    source_key: str,
    data: Any,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """单数据源质量检查。

    Args:
        source_key: 数据源标识符，用于查找阈值和日志。
        data: 待检查的数据（list[dict] 或单个 dict）。
        thresholds: 阈值覆盖，None 用 DEFAULT_THRESHOLDS[source_key]。

    Returns:
        dict with keys:
            source: str
            passed: bool
            grade: str — green / yellow / red
            completeness_score: float — 0~100
            freshness_score: float — 0~100
            null_rates: dict[str, float] — 每个 key 的空值比例
            latest_date: str | None
            age_hours: float | None
            total_records: int
            failures: list[str]
    """
    cfg = thresholds or DEFAULT_THRESHOLDS.get(source_key, {})
    expected_keys = cfg.get("expected_keys", [])
    max_null_pct = cfg.get("max_null_pct", 50.0)
    max_age_hours = cfg.get("max_age_hours", 72.0)
    date_key = cfg.get("date_key")

    failures: list[str] = []

    # Normalize to list
    if isinstance(data, dict):
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        items = []

    total = len(items)

    # ── completeness check ──
    null_rates: dict[str, float] = {}
    for key in expected_keys:
        rate = _null_pct(items, key)
        null_rates[key] = round(rate, 1)
        if rate > max_null_pct:
            failures.append(f"key '{key}': null_pct={rate:.1f}% > threshold={max_null_pct}%")

    completeness = 100.0
    if total == 0:
        completeness = 0.0
        failures.append("zero records")
    else:
        avg_null = sum(null_rates.values()) / len(null_rates) if null_rates else 0.0
        completeness = max(0.0, 100.0 - avg_null)

    # ── freshness check ──
    latest_date = _most_recent_date(items, date_key) if date_key else None
    age_h = _freshness_hours(latest_date)
    freshness = 100.0
    if age_h is not None:
        if age_h > max_age_hours:
            failures.append(f"data age {age_h:.1f}h > threshold {max_age_hours}h")
            freshness = max(0.0, 100.0 * (1 - age_h / (max_age_hours * 2)))
        else:
            freshness = max(0.0, 100.0 * (1 - age_h / max_age_hours))
    elif total == 0:
        freshness = 0.0

    # ── grade ──
    passed = len(failures) == 0
    if not passed:
        grade = GRADE_RED
    elif completeness < 60.0 or freshness < 60.0:
        grade = GRADE_YELLOW
    else:
        grade = GRADE_GREEN

    return {
        "source": source_key,
        "passed": passed,
        "grade": grade,
        "completeness_score": round(completeness, 1),
        "freshness_score": round(freshness, 1),
        "source_grade": grade,
        "null_rates": null_rates,
        "latest_date": latest_date,
        "age_hours": round(age_h, 1) if age_h is not None else None,
        "total_records": total,
        "failures": failures,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def quality_gate_summary(
    reports: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """聚合多个数据源的质量报告，给出总体结论。

    Returns:
        dict with:
            overall_grade: str
            all_passed: bool
            green: list[str]
            yellow: list[str]
            red: list[str]
            sources: dict — 原始报告
    """
    if not reports:
        return {
            "overall_grade": GRADE_RED,
            "all_passed": False,
            "green": [],
            "yellow": [],
            "red": ["no_sources"],
            "sources": {},
        }

    greens = [k for k, v in reports.items() if v.get("grade") == GRADE_GREEN]
    yellows = [k for k, v in reports.items() if v.get("grade") == GRADE_YELLOW]
    reds = [k for k, v in reports.items() if v.get("grade") == GRADE_RED]

    all_passed = len(reds) == 0
    if not all_passed:
        overall = GRADE_RED
    elif yellows:
        overall = GRADE_YELLOW
    else:
        overall = GRADE_GREEN

    return {
        "overall_grade": overall,
        "all_passed": all_passed,
        "green": greens,
        "yellow": yellows,
        "red": reds,
        "total_sources": len(reports),
        "sources": reports,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def check_dag_run_quality(
    artifact: dict[str, Any] | None,
    *,
    source_key: str = "dag_run",
    expected_keys: list[str] | None = None,
) -> dict[str, Any]:
    """对 DAG run 产出的 artifact 做快速质量检查。

    在 run_agent_dag.py 的 execute_dag 返回值中使用。
    """
    if artifact is None:
        return {
            "source": source_key,
            "passed": False,
            "grade": GRADE_RED,
            "completeness_score": 0.0,
            "freshness_score": 0.0,
            "failures": ["artifact is None"],
        }
    items = [artifact] if isinstance(artifact, dict) else (artifact or [])
    return check_quality(source_key, items, {
        "expected_keys": expected_keys or ["job_id", "status"],
        "max_null_pct": 50.0,
        "max_age_hours": 72,
    })
