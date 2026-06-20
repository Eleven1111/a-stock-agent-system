#!/usr/bin/env python3
"""Full-market A-share candidate discovery with dual strategy ranking.

Data sources:
- Shanghai Stock Exchange and Shenzhen Stock Exchange listing APIs
- Tencent batch quotes and qfq daily K-lines

Usage:
  python candidate_discovery.py --json
"""

from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence

SCRIPT_DIR = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import monitor_registry  # noqa: E402
from config_registry import config_path  # noqa: E402
from a_stock_http import DataSourceError  # noqa: E402
from a_share_rules import add_trading_days  # noqa: E402
from market_adapters import fetch_tencent_kline, fetch_tencent_quote  # noqa: E402
from http_client import request_bytes  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from research_evidence import build_research_evidence  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


CONFIG_FILE = str(config_path("candidate_selection"))


def load_config() -> Dict[str, Any]:
    with open(CONFIG_FILE, encoding="utf-8") as file:
        return json.load(file)


def latest_pool_file() -> str:
    return data_file("stock-triage", "candidate_pool_latest.json")


def dated_pool_file(asof: str) -> str:
    return data_file("stock-triage", os.path.join("candidate_pools", f"{asof}.json"))


def universe_cache_file() -> str:
    return data_file("stock-triage", "exchange_universe.json")


def _request_bytes(
    url: str,
    headers: Mapping[str, str] | None = None,
    timeout: int = 20,
    attempts: int = 2,
) -> bytes:
    result = request_bytes(
        url,
        source="exchange_listing",
        timeout=timeout,
        max_attempts=min(attempts, 2),
        headers={"User-Agent": "Mozilla/5.0 (Hermes A-Stock Agent)", **dict(headers or {})},
    )
    return result.data


def _fetch_sse_type(stock_type: str) -> List[Dict[str, Any]]:
    params = {
        "STOCK_TYPE": stock_type,
        "REG_PROVINCE": "",
        "CSRC_CODE": "",
        "STOCK_CODE": "",
        "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
        "COMPANY_STATUS": "2,4,5,7,8",
        "type": "inParams",
        "isPagination": "true",
        "pageHelp.cacheSize": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "pageHelp.endPage": "1",
    }
    url = "https://query.sse.com.cn/sseQuery/commonQuery.do?" + "&".join(
        f"{key}={value}" for key, value in params.items()
    )
    raw = _request_bytes(
        url,
        headers={
            "Host": "query.sse.com.cn",
            "Referer": "https://www.sse.com.cn/assortment/stock/list/share/",
        },
    )
    payload = json.loads(raw.decode("utf-8"))
    result = []
    for row in payload.get("result", []):
        code = str(row.get("A_STOCK_CODE") or "").zfill(6)
        if not code.strip("0"):
            continue
        result.append({
            "code": code,
            "name": row.get("COMPANY_ABBR") or code,
            "listed_date": (
                datetime.strptime(str(row.get("LIST_DATE")), "%Y%m%d").date().isoformat()
                if str(row.get("LIST_DATE") or "").isdigit()
                else str(row.get("LIST_DATE") or "")[:10]
            ),
            "exchange": "SSE",
            "board": "STAR" if stock_type == "8" else "MAIN",
        })
    return result


def fetch_sse_universe() -> List[Dict[str, Any]]:
    return _fetch_sse_type("1") + _fetch_sse_type("8")


_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _xlsx_cell_text(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_XLSX_NS}t"))
    value = cell.find(f"{_XLSX_NS}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return shared_strings[int(value.text)]
    return value.text


def _xlsx_rows(raw: bytes) -> Iterable[List[str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as workbook:
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            shared_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.iter(f"{_XLSX_NS}t"))
                for item in shared_root.iter(f"{_XLSX_NS}si")
            ]
        sheet_names = sorted(
            name for name in workbook.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        if not sheet_names:
            raise ValueError("SZSE workbook missing worksheet")
        with workbook.open(sheet_names[0]) as sheet:
            for _event, row in ET.iterparse(sheet, events=("end",)):
                if row.tag != f"{_XLSX_NS}row":
                    continue
                yield [_xlsx_cell_text(cell, shared_strings) for cell in row.findall(f"{_XLSX_NS}c")]
                row.clear()


def parse_szse_workbook(raw: bytes) -> List[Dict[str, Any]]:
    rows = iter(_xlsx_rows(raw))
    header = [str(value or "").strip() for value in next(rows)]
    if "A股代码" not in header:
        raise ValueError("SZSE workbook missing header: A股代码")
    indexes = {name: header.index(name) for name in ("A股代码", "A股简称")}
    listed_index = header.index("A股上市日期") if "A股上市日期" in header else None
    board_index = header.index("板块") if "板块" in header else None
    industry_index = header.index("所属行业") if "所属行业" in header else None
    result = []
    for row in rows:
        raw_code = row[indexes["A股代码"]] if indexes["A股代码"] < len(row) else None
        if raw_code in (None, ""):
            continue
        code = str(raw_code).split(".")[0].zfill(6)
        listed = row[listed_index] if listed_index is not None and listed_index < len(row) else ""
        result.append({
            "code": code,
            "name": str(row[indexes["A股简称"]] or code),
            "listed_date": str(listed)[:10],
            "exchange": "SZSE",
            "board": str(row[board_index] or "") if board_index is not None else "",
            "industry": str(row[industry_index] or "") if industry_index is not None else "",
        })
    return result


def fetch_szse_universe() -> List[Dict[str, Any]]:
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1110",
        "TABKEY": "tab1",
        "random": f"{time.time():.6f}",
    }
    url = "https://www.szse.cn/api/report/ShowReport?" + "&".join(
        f"{key}={value}" for key, value in params.items()
    )
    return parse_szse_workbook(_request_bytes(url, timeout=30))


def fetch_exchange_universe() -> List[Dict[str, Any]]:
    universe_config = load_config()["universe"]
    minimums = {
        "SSE": int(universe_config["min_sse_count"]),
        "SZSE": int(universe_config["min_szse_count"]),
    }
    errors = []
    rows: List[Dict[str, Any]] = []
    source_counts = {"SSE": 0, "SZSE": 0}
    for name, fetcher in (("SSE", fetch_sse_universe), ("SZSE", fetch_szse_universe)):
        try:
            source_rows = list(fetcher())
            source_counts[name] = len({
                candidate_pipeline.naked_code(row.get("code"))
                for row in source_rows
                if row.get("code")
            })
            rows.extend(source_rows)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    deduplicated = {candidate_pipeline.naked_code(row["code"]): row for row in rows}
    complete = all(source_counts[name] >= minimums[name] for name in minimums)
    if not complete:
        cached = read_json(universe_cache_file(), {})
        cached_rows = cached.get("stocks", []) if isinstance(cached, dict) else []
        cached_counts = {
            name: len({
                candidate_pipeline.naked_code(row.get("code"))
                for row in cached_rows
                if row.get("exchange") == name and row.get("code")
            })
            for name in minimums
        }
        cache_age = None
        try:
            cache_age = (
                datetime.now() - datetime.fromisoformat(str(cached.get("updated_at")))
            ).days
        except (TypeError, ValueError):
            pass
        max_age = int(universe_config["cache_max_age_days"])
        cache_complete = all(cached_counts[name] >= minimums[name] for name in minimums)
        if cache_complete and cache_age is not None and 0 <= cache_age <= max_age:
            return cached_rows
        raise DataSourceError(
            "exchange_universe",
            (
                "交易所股票列表不完整"
                f"(SSE={source_counts['SSE']}, SZSE={source_counts['SZSE']}); "
                f"{'; '.join(errors)}"
            ),
        )
    result = list(deduplicated.values())
    atomic_write_json(universe_cache_file(), {
        "schema": "exchange_universe_v1",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "source_counts": source_counts,
        "stocks": result,
    })
    return result


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def fetch_universe_quotes(universe: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    config = load_config()["network"]
    batch_size = int(config["quote_batch_size"])
    workers = int(config["quote_workers"])
    retries = int(config["request_retries"])
    metadata = {candidate_pipeline.naked_code(item["code"]): dict(item) for item in universe}
    batches = [
        [candidate_pipeline.market_code(item["code"]) for item in batch]
        for batch in _chunks(list(universe), batch_size)
    ]

    def _fetch(batch: List[str]) -> Dict[str, Dict[str, Any]]:
        last_error = None
        attempts = min(retries + 1, 2)
        for attempt in range(attempts):
            try:
                return fetch_tencent_quote(batch)
            except DataSourceError as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2 ** attempt))
        raise last_error or DataSourceError("tencent", "unknown quote failure")

    quotes: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch, batch) for batch in batches]
        for future in as_completed(futures):
            try:
                batch_quotes = future.result()
            except DataSourceError:
                continue
            for prefixed, fields in batch_quotes.items():
                code = candidate_pipeline.naked_code(prefixed)
                quotes[code] = {**metadata.get(code, {}), **fields, "code": code}
    minimum_coverage = max(
        500,
        int(len(universe) * float(config["quote_min_coverage"])),
    )
    if len(quotes) < minimum_coverage:
        raise DataSourceError("tencent", f"全市场行情覆盖不足: {len(quotes)}/{len(universe)}")
    return quotes


def fetch_candidate_klines(candidates: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    network_config = load_config()["network"]
    workers = int(network_config["kline_workers"])
    retries = int(network_config["request_retries"])

    def _fetch(item: Mapping[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
        code = candidate_pipeline.naked_code(item["code"])
        market = "sh" if code.startswith("6") else "sz"
        attempts = min(retries + 1, 2)
        for attempt in range(attempts):
            bars = fetch_tencent_kline(code, market=market, days=70)
            if bars:
                return code, bars
            if attempt + 1 < attempts:
                time.sleep(0.25 * (2 ** attempt))
        return code, []

    result: Dict[str, List[Dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch, item) for item in candidates]
        for future in as_completed(futures):
            try:
                code, bars = future.result()
            except Exception:  # noqa: BLE001
                continue
            if bars:
                result[code] = bars
    minimum_coverage = (
        max(1, int(len(candidates) * float(network_config["kline_min_coverage"])))
        if candidates else 0
    )
    if len(result) < minimum_coverage:
        raise DataSourceError("tencent_kline", f"K线覆盖不足: {len(result)}/{len(candidates)}")
    return result


def _reason_counts(rejected: Mapping[str, Sequence[str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for reasons in rejected.values():
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def _persist_pool(asof: str, result: Dict[str, Any]) -> None:
    atomic_write_json(dated_pool_file(asof), result)
    atomic_write_json(latest_pool_file(), result)


def observe_recent_candidates(
    asof: str,
    quote_map: Mapping[str, Mapping[str, Any]],
    horizon: int = 3,
) -> None:
    pattern = data_file("stock-triage", os.path.join("candidate_lifecycle", "*.json"))
    prior_dates = sorted(
        os.path.splitext(os.path.basename(path))[0]
        for path in glob.glob(pattern)
        if os.path.splitext(os.path.basename(path))[0] < asof
    )
    timeline = prior_dates + [asof]
    for source_asof in prior_dates[-horizon:]:
        trading_horizon = len([day for day in timeline if source_asof < day <= asof])
        if trading_horizon <= horizon:
            candidate_lifecycle.observe_day(
                source_asof,
                asof,
                trading_horizon,
                quote_map,
            )


def load_signal_context_for_discovery(
    asof: str,
) -> tuple[Mapping[str, Any] | None, Dict[str, Any]]:
    """Expose only same-day evidence; validate market and social clocks separately."""
    try:
        from market_temperature import temperature_from_context
        from signal_context import read_signal_context

        signal_ctx = read_signal_context() or {}
        temperature = temperature_from_context(
            signal_ctx,
            event_asof=asof,
            max_age_days=0,
        )
        ranking_ctx = dict(signal_ctx) if temperature.get("context_fresh") else {}
        if not temperature.get("context_fresh"):
            for key in (
                "lianban_ladder",
                "prev_lianban_ladder",
                "sector_limitups",
                "market_sentiment",
                "stock_flows",
                "sector_flows",
                "northbound_net_yi",
            ):
                ranking_ctx.pop(key, None)

        social = signal_ctx.get("social_attention")
        social_asof = (
            signal_ctx.get("social_attention_asof")
            or (
                social.get("trading_date")
                if isinstance(social, Mapping)
                else None
            )
        )
        if (
            not isinstance(social, Mapping)
            or social.get("schema") != "social_attention_snapshot_v1"
            or str(social_asof or "") != asof
        ):
            ranking_ctx.pop("social_attention", None)
            ranking_ctx.pop("social_attention_asof", None)
            ranking_ctx.pop("social_attention_snapshot", None)
        else:
            ranking_ctx["social_attention"] = social
            ranking_ctx["social_attention_asof"] = asof
            if signal_ctx.get("social_attention_snapshot"):
                ranking_ctx["social_attention_snapshot"] = signal_ctx[
                    "social_attention_snapshot"
                ]
        return ranking_ctx or None, temperature
    except Exception as exc:  # noqa: BLE001
        return None, {
            "tier": "neutral",
            "height": 0,
            "promotion_rate": None,
            "limitup_total": None,
            "allow_new_daban": True,
            "position_multiplier": 1.0,
            "top_n_limit": None,
            "retreat_signal": None,
            "advice": "温度数据不可用，不施加情绪约束",
            "notes": [f"情绪上下文读取失败: {exc}"],
            "context_asof": None,
            "context_fresh": False,
        }


def run_discovery(
    asof: str,
    watch_limit: int | None = None,
    prefilter_limit: int | None = None,
    universe_fetcher: Callable[[], Sequence[Mapping[str, Any]]] = fetch_exchange_universe,
    quote_fetcher: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Mapping[str, Any]]]
    = fetch_universe_quotes,
    kline_fetcher: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Sequence[Mapping[str, Any]]]]
    = fetch_candidate_klines,
    settle_previous: bool = True,
) -> Dict[str, Any]:
    config = load_config()
    universe_config = config["universe"]
    pipeline_config = config["pipeline"]
    watch_limit = int(watch_limit or pipeline_config["watch_limit"])
    prefilter_limit = int(prefilter_limit or universe_config["prefilter_limit"])

    universe = list(universe_fetcher())
    quote_map = dict(quote_fetcher(universe))

    # 游资因子只能消费当日收盘缓存，避免历史梯队污染新的候选排名。
    signal_ctx, temperature = load_signal_context_for_discovery(asof)
    quotes = list(quote_map.values())
    eligible, base_rejected = candidate_pipeline.filter_universe(
        quotes,
        min_amount=float(universe_config["min_amount"]),
        min_price=float(universe_config["min_price"]),
        min_listed_days=int(universe_config["min_listed_days"]),
    )
    enrichment_universe = candidate_pipeline.select_enrichment_universe(
        eligible,
        limit=prefilter_limit,
    )
    kline_by_code = dict(kline_fetcher(enrichment_universe))
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    input_snapshot = materialize_input_snapshot(
        "candidate-discovery-input",
        {
            "schema": "candidate_discovery_inputs_v1",
            "universe": universe,
            "quotes": quote_map,
            "klines": kline_by_code,
            "signal_context": signal_ctx,
            "market_temperature": temperature,
        },
        trading_date=asof,
        batch_id=batch_id,
        producer="candidate-discovery",
        source_versions={
            "exchange_listing": "exchange-listing-v1",
            "tencent": "tencent-adapter-v2",
            "tencent_kline": "tencent-kline-adapter-v2",
            **dict(
                (
                    (signal_ctx or {}).get("social_attention") or {}
                ).get("source_versions") or {}
            ),
        },
    )
    inputs = input_snapshot["payload"]
    quote_map = dict(inputs["quotes"])
    kline_by_code = dict(inputs["klines"])
    signal_ctx = inputs.get("signal_context")
    temperature = dict(inputs["market_temperature"])
    if settle_previous:
        observe_recent_candidates(asof, quote_map)
    quotes = list(quote_map.values())
    result = candidate_pipeline.build_watch_pool(
        quotes,
        kline_by_code,
        watch_limit=watch_limit,
        min_amount=float(universe_config["min_amount"]),
        min_price=float(universe_config["min_price"]),
        min_listed_days=int(universe_config["min_listed_days"]),
        signal_ctx=signal_ctx,
    )
    for item in result.get("candidates", []):
        selected_by = item.get("selected_by") or {}
        strategy_id = (
            "daban:first_board_reseal"
            if selected_by.get("daban")
            else "trend_pullback"
        )
        code = candidate_pipeline.naked_code(item.get("code"))
        item["research_evidence"] = build_research_evidence(
            code,
            strategy_id=strategy_id,
            asof=asof,
            bars=list(kline_by_code.get(code) or []),
        )
    evaluated = result.pop("evaluated_candidates")
    result.update({
        "asof": asof,
        "status": "ready",
        "universe_source": "SSE+SZSE listings / Tencent quotes",
        "enriched_count": len(kline_by_code),
        "market_temperature": temperature,
        "input_snapshot": compact_ref(input_snapshot),
    })
    _persist_pool(asof, result)
    monitor_registry.reconcile_automatic(
        "stock",
        [
            {
                "code": item["code"],
                "name": item.get("name") or item["code"],
                "metadata": {
                    "candidate_rank": index,
                    "daban_rank": item.get("daban_rank"),
                    "trend_rank": item.get("trend_rank"),
                    "selected_by": dict(item.get("selected_by") or {}),
                    "candidate_pool_asof": asof,
                },
            }
            for index, item in enumerate(result["candidates"], start=1)
        ],
        source="candidate_discovery",
        source_group="daily_observation",
        replace_source_groups=[
            "daily_observation",
            "candidate_discovery",
            "event_watch",
            "realtime_catalyst_trigger",
            "auction_shortlist",
            "auction_finalize",
            "open_confirmation",
        ],
        trading_date=asof,
        batch_id=batch_id,
        expires_at=add_trading_days(asof, 1),
    )
    monitor_registry.gc_expired(asof=asof)

    selected_codes = {item["code"] for item in result["candidates"]}
    selection_by_code = {
        item["code"]: dict(item.get("selected_by") or {})
        for item in result["candidates"]
    }
    lifecycle_candidates = []
    for item in evaluated:
        record = dict(item)
        if item["code"] in selected_codes:
            record["selected_by"] = selection_by_code[item["code"]]
        else:
            record["selected_by"] = {"daban": False, "trend": False}
            record["rejection_reasons"] = [f"双策略综合排名未进入前{watch_limit}"]
        lifecycle_candidates.append(record)
    candidate_lifecycle.initialize_day(
        asof,
        lifecycle_candidates,
        metadata={
            "scanned_count": result["scanned_count"],
            "eligible_count": result["eligible_count"],
            "watch_count": result["candidate_count"],
            "enriched_count": result["enriched_count"],
            "base_rejection_counts": _reason_counts(base_rejected),
        },
    )
    return result


def format_report(result: Mapping[str, Any]) -> str:
    lines = [
        f"## 动态候选池 | {result.get('asof')}",
        f"全市场 {result.get('scanned_count', 0)} | 可交易 {result.get('eligible_count', 0)} | "
        f"观察池 {result.get('candidate_count', 0)}",
    ]
    temp = result.get("market_temperature")
    if isinstance(temp, Mapping) and temp.get("tier") not in (None, "neutral"):
        promo = temp.get("promotion_rate")
        promo_str = f"{promo:.0%}" if isinstance(promo, (int, float)) else "N/A"
        lines.append(
            f"🌡️ 情绪温度：**{temp['tier']}**（高度板{temp.get('height')} | 晋级率{promo_str}）"
            f"→ {temp.get('advice')}｜打板仓位×{temp.get('position_multiplier')}"
            + (f"｜当日最多{temp['top_n_limit']}只" if temp.get("top_n_limit") else "")
        )
    lines.extend([
        "",
        "| 代码 | 名称 | 打板排名 | 打板分 | 趋势排名 | 趋势分 |",
        "|------|------|----------|--------|----------|--------|",
    ])
    for item in result.get("candidates", [])[:20]:
        lines.append(
            f"| {item['code']} | {item['name']} | {item['daban_rank']} | "
            f"{item['daban_score']} | {item['trend_rank']} | {item['trend_score']} |"
        )
    return "\n".join(lines)


def json_report(result: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": result.get("schema"),
        "asof": result.get("asof"),
        "generated_at": result.get("generated_at"),
        "status": result.get("status"),
        "universe_source": result.get("universe_source"),
        "scanned_count": result.get("scanned_count", 0),
        "eligible_count": result.get("eligible_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "enriched_count": result.get("enriched_count", 0),
        "candidate_count": result.get("candidate_count", 0),
        "market_temperature": result.get("market_temperature"),
        "rejection_reason_counts": _reason_counts(result.get("rejected", {})),
        "top_candidates": [
            {
                key: item.get(key)
                for key in (
                    "code", "name", "daban_rank", "daban_score",
                    "trend_rank", "trend_score", "selected_by",
                )
            }
            for item in result.get("candidates", [])[:5]
        ],
    }


def main() -> None:
    config = load_config()
    parser = argparse.ArgumentParser(description="A股全市场动态候选发现")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--watch-limit", type=int, default=config["pipeline"]["watch_limit"])
    parser.add_argument("--prefilter-limit", type=int, default=config["universe"]["prefilter_limit"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = run_discovery(
            args.asof,
            watch_limit=args.watch_limit,
            prefilter_limit=args.prefilter_limit,
        )
    except Exception as exc:  # noqa: BLE001
        result = {
            "schema": "candidate_watch_pool_v1",
            "asof": args.asof,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "insufficient_data",
            "error": str(exc),
            "candidates": [],
            "candidate_count": 0,
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"候选发现失败：{exc}")
        raise SystemExit(1)

    if args.json:
        print(json.dumps(json_report(result), ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(result))


if __name__ == "__main__":
    main()
