"""全市场行业归属映射 —— 构建 / 按日缓存 / 注入。

背景
====
候选筛选的板块聚类、主线识别、龙头识别都依赖每只股票的 ``industry`` 字段，
但交易所上市列表只有深市带（粗口径的证监会）行业，沪市主板/科创板全为空，
导致 ``hot_money_selection`` 的板块覆盖长期偏低、主线判断有系统性偏差。

本模块用 adata 的东方财富行业板块（一致口径）反向映射出全市场
``code -> 行业``，按交易日缓存，并把结果注入 universe 记录的 ``industry``。

设计要点
========
- **消费端零触网**：``candidate_discovery`` 只调用 :func:`load_cached` 读缓存；
  缓存缺失即注入为 no-op，绝不在主链上发起网络请求（无回归、无阻塞）。
- **构建端容错**：:func:`refresh` 由独立 CLI / cron 调用，限速 + 重试 +
  与昨日缓存合并；整体失败时保留旧缓存而非清空。
- **纯逻辑可测**：数据源以参数注入，核心 build / enrich / cache 全部可单测。
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple

from paths import data_file
from state_store import atomic_write_json, read_json

SCHEMA = "industry_map_v1"
SOURCE = "resilient_industry_board"
_UA = "Mozilla/5.0 (Hermes A-Stock Agent)"
_EAST_INDUSTRY_BOARDS_URL = "degraded:last-resort:push2-clist"
_EAST_INDUSTRY_BOARDS_PARAMS = {
    "pn": "1",
    "pz": "500",
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f3",
    "fs": "m:90 t:2 f:!50",
    "fields": "f12,f14",
}

BoardsFetcher = Callable[[], Sequence[Tuple[str, str]]]
ConstituentsFetcher = Callable[[str], Sequence[str]]


# ── 代码规整 ──────────────────────────────────────────────────────

def _norm_code(code: Any) -> str:
    text = str(code or "").strip().lower()
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6) if text else ""


# ── 默认数据源（真实，惰性导入，尊重环境代理）────────────────────

def _default_boards_fetcher() -> List[Tuple[str, str]]:
    """行业板块清单 → [(板块码, 行业名)]，优先 THS/AkShare，不再直打 push2 clist。"""
    try:
        from market_adapters import fetch_industry_boards

        boards = fetch_industry_boards()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"行业板块清单获取失败: {exc}") from exc
    if not boards:
        raise RuntimeError("行业板块清单为空")
    return boards


def _default_constituents_fetcher(board_code: str) -> List[str]:
    """adata 东财板块成分股 → [code]。空结果视为可重试失败。"""
    import adata  # 惰性导入

    frame = adata.stock.info.concept_constituent_east(concept_code=board_code)
    codes = (
        []
        if frame is None or len(frame) == 0
        else [str(value) for value in frame["stock_code"].tolist()]
    )
    if not codes:
        raise RuntimeError(f"板块 {board_code} 成分股为空")
    return codes


# ── 构建 ─────────────────────────────────────────────────────────

def _members_with_retry(
    fetcher: ConstituentsFetcher,
    board_code: str,
    retry: int,
    pace_seconds: float,
) -> List[str] | None:
    last_error: Exception | None = None
    for attempt in range(retry + 1):
        try:
            return list(fetcher(board_code))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retry and pace_seconds:
                time.sleep(pace_seconds)
    _ = last_error
    return None


def build_industry_map(
    *,
    boards_fetcher: BoardsFetcher,
    constituents_fetcher: ConstituentsFetcher,
    pace_seconds: float = 0.3,
    retry: int = 2,
) -> Dict[str, Any]:
    """枚举行业板块并取成分股，反向汇成 ``code -> 行业``。

    同一只股票若分属多个板块（行业 + 细分行业），以**首个**枚举到的板块为主行业。
    单个板块失败不致整体中断，仅记入 ``failed_boards``。
    """
    boards = list(boards_fetcher())
    industry_by_code: Dict[str, str] = {}
    failed_boards: List[str] = []
    for board_code, industry_name in boards:
        members = _members_with_retry(constituents_fetcher, board_code, retry, pace_seconds)
        if members is None:
            failed_boards.append(board_code)
            continue
        for raw_code in members:
            code = _norm_code(raw_code)
            if code and code not in industry_by_code:
                industry_by_code[code] = industry_name
        if pace_seconds:
            time.sleep(pace_seconds)
    return {
        "schema": SCHEMA,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source": SOURCE,
        "industry_by_code": industry_by_code,
        "board_count": len(boards),
        "stock_count": len(industry_by_code),
        "failed_boards": failed_boards,
    }


# ── 注入 ─────────────────────────────────────────────────────────

def enrich_records(
    records: Sequence[Mapping[str, Any]],
    industry_by_code: Mapping[str, str],
) -> List[Dict[str, Any]]:
    """把行业映射注入 universe 记录（不可变，返回新列表）。

    命中映射 → 统一采用映射口径（覆盖旧的粗行业）；未命中 → 保留原有行业。
    """
    enriched: List[Dict[str, Any]] = []
    for raw in records:
        item = dict(raw)
        mapped = industry_by_code.get(_norm_code(item.get("code")))
        if mapped:
            item["industry"] = mapped
            item["industry_source"] = SOURCE
        enriched.append(item)
    return enriched


# ── 缓存 ─────────────────────────────────────────────────────────

def _default_cache_file() -> str:
    return data_file("stock-triage", "industry_map.json")


def _resolve_pace(pace_seconds: float | None) -> float:
    if pace_seconds is not None:
        return float(pace_seconds)
    try:
        from config_registry import load_registered

        cfg = load_registered("candidate_selection").get("industry_map") or {}
        return float(cfg.get("pace_seconds", 0.3))
    except Exception:  # noqa: BLE001
        return 0.3


def refresh(
    asof: str,
    *,
    cache_file: str | None = None,
    boards_fetcher: BoardsFetcher | None = None,
    constituents_fetcher: ConstituentsFetcher | None = None,
    pace_seconds: float | None = None,
    retry: int = 2,
) -> Dict[str, Any]:
    """构建并落盘行业映射，与昨日缓存合并；整体失败则保留旧缓存。"""
    cache_file = cache_file or _default_cache_file()
    prior = read_json(cache_file, {})
    prior_map = (
        dict(prior.get("industry_by_code") or {})
        if isinstance(prior, Mapping)
        else {}
    )
    try:
        built = build_industry_map(
            boards_fetcher=boards_fetcher or _default_boards_fetcher,
            constituents_fetcher=constituents_fetcher or _default_constituents_fetcher,
            pace_seconds=_resolve_pace(pace_seconds),
            retry=retry,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "schema": SCHEMA,
            "status": "stale_cache",
            "error": str(exc),
            "asof": prior.get("asof") if isinstance(prior, Mapping) else None,
            "industry_by_code": prior_map,
            "stock_count": len(prior_map),
        }

    new_map = built["industry_by_code"]
    if not new_map:
        return {
            "schema": SCHEMA,
            "status": "stale_cache",
            "error": "构建结果为空",
            "asof": prior.get("asof") if isinstance(prior, Mapping) else None,
            "industry_by_code": prior_map,
            "stock_count": len(prior_map),
        }

    merged = {**prior_map, **new_map}
    payload = {
        "schema": SCHEMA,
        "asof": asof,
        "built_at": built["built_at"],
        "source": SOURCE,
        "status": "ok" if not built["failed_boards"] else "partial",
        "industry_by_code": merged,
        "board_count": built["board_count"],
        "stock_count": len(merged),
        "fresh_stock_count": len(new_map),
        "failed_boards": built["failed_boards"],
    }
    atomic_write_json(cache_file, payload)
    return payload


def load_cached(
    asof: str,
    *,
    cache_file: str | None = None,
    max_age_days: int = 5,
) -> Dict[str, str]:
    """读取按日缓存的 ``code -> 行业``；缺失或过期返回空（注入退化为 no-op）。"""
    cache_file = cache_file or _default_cache_file()
    data = read_json(cache_file, {})
    if not isinstance(data, Mapping):
        return {}
    mapping = data.get("industry_by_code")
    if not isinstance(mapping, Mapping) or not mapping:
        return {}
    try:
        age = (date.fromisoformat(asof) - date.fromisoformat(str(data.get("asof")))).days
    except (TypeError, ValueError):
        return {}
    if 0 <= age <= max_age_days:
        return {str(code): str(name) for code, name in mapping.items()}
    return {}


# ── CLI ──────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="全市场行业映射刷新")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--refresh", action="store_true", help="构建并刷新缓存")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.refresh:
        result = refresh(args.asof)
    else:
        mapping = load_cached(args.asof)
        result = {
            "schema": SCHEMA,
            "asof": args.asof,
            "status": "cached" if mapping else "empty",
            "stock_count": len(mapping),
        }

    summary = {key: value for key, value in result.items() if key != "industry_by_code"}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"行业映射 {args.asof}: status={summary.get('status')} "
            f"覆盖={summary.get('stock_count')} "
            f"失败板块={len(summary.get('failed_boards') or [])}"
        )


if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
