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
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", "..", ".."))
COMMON = os.path.join(ROOT, "skills", "common")
sys.path.insert(0, COMMON)

import candidate_fsm  # noqa: E402
import candidate_lifecycle  # noqa: E402
import candidate_pipeline  # noqa: E402
import hot_money_selection  # noqa: E402
import industry_map  # noqa: E402
import nl_screening  # noqa: E402
import stage_intelligence  # noqa: E402
import monitor_registry  # noqa: E402
from config_registry import config_path  # noqa: E402
from a_stock_http import DataSourceError  # noqa: E402
from a_share_rules import add_trading_days  # noqa: E402
from market_adapters import (  # noqa: E402
    fetch_a_share_spot,
    fetch_a_share_daily_kline,
    fetch_a_share_daily_series,
    fetch_tencent_quote_with_provenance as fetch_tencent_quote,
)
from http_client import request_bytes  # noqa: E402
from market_snapshot import compact_ref, materialize_input_snapshot, write_snapshot  # noqa: E402
from paths import data_file  # noqa: E402
from research_evidence import build_research_evidence  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402


CONFIG_FILE = str(config_path("candidate_selection"))
MAX_BOOTSTRAP_POOL_AGE_DAYS = 4


def load_config() -> Dict[str, Any]:
    with open(CONFIG_FILE, encoding="utf-8") as file:
        return json.load(file)


def latest_pool_file() -> str:
    return data_file("stock-triage", "candidate_pool_latest.json")


def dated_pool_file(asof: str) -> str:
    return data_file("stock-triage", os.path.join("candidate_pools", f"{asof}.json"))


def universe_cache_file() -> str:
    return data_file("stock-triage", "exchange_universe.json")


def quotes_cache_file() -> str:
    return data_file("stock-triage", "universe_quotes_cache.json")


def is_auction_window(now: datetime | None = None) -> bool:
    """Return whether A-share opening-auction quotes are not yet tradeable."""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    current_time = now.time()
    return dtime(9, 15) <= current_time <= dtime(9, 25)


def load_cached_quotes(max_age_minutes: int = 1440) -> Dict[str, Dict[str, Any]]:
    """Load the last complete quote cache when it is not older than the limit."""
    cached = read_json(quotes_cache_file(), {})
    if not isinstance(cached, Mapping):
        return {}
    updated_at = cached.get("updated_at")
    try:
        captured = datetime.fromisoformat(str(updated_at))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        age_minutes = (
            datetime.now(ZoneInfo("Asia/Shanghai")) - captured
        ).total_seconds() / 60
    except (TypeError, ValueError):
        return {}
    if age_minutes < 0 or age_minutes > max_age_minutes:
        return {}
    quotes = cached.get("quotes")
    if not isinstance(quotes, Mapping):
        return {}
    return {
        candidate_pipeline.naked_code(code): dict(fields)
        for code, fields in quotes.items()
        if isinstance(fields, Mapping) and candidate_pipeline.naked_code(code)
    }


def hot_money_selection_file() -> str:
    return data_file("stock-triage", "hot_money_selection_latest.json")


def reusable_pool(
    pool: Mapping[str, Any],
    event_asof: str,
    max_age_days: int = MAX_BOOTSTRAP_POOL_AGE_DAYS,
) -> bool:
    if pool.get("status") not in ("ready", "degraded") or not pool.get("candidates"):
        return False
    # Reject pools that claim ready but have zero candidates (stale data artifact).
    # 旧池没有 candidate_count 字段，回退到 candidates 长度，不误杀合法池。
    count = pool.get("candidate_count")
    if count is None:
        count = len(pool.get("candidates") or [])
    if int(count or 0) == 0:
        return False
    scan_codes = pool.get("auction_scan_codes")
    if not isinstance(scan_codes, list) or len(scan_codes) < int(pool.get("eligible_count") or 1):
        return False
    try:
        age = (
            datetime.fromisoformat(event_asof).date()
            - datetime.fromisoformat(str(pool.get("asof"))).date()
        ).days
    except (TypeError, ValueError):
        return False
    return 0 <= age <= max_age_days


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


def _fetch_quote_batch(batch: List[str], retries: int) -> Dict[str, Dict[str, Any]]:
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


def _universe_spot_quotes(metadata: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    spot = fetch_a_share_spot()
    if hasattr(spot, "to_dict"):
        rows = spot.to_dict("records")
    elif isinstance(spot, Sequence) and not isinstance(spot, (str, bytes)):
        rows = list(spot)
    else:
        rows = []
    source = str(getattr(fetch_a_share_spot, "last_source", "akshare_sina"))
    if source == "eastmoney_push2_degraded":
        source = "eastmoney_push2"
    mapped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = candidate_pipeline.naked_code(
            row.get("代码") or row.get("code") or row.get("stock_code")
        )
        if not code or code not in metadata:
            continue
        mapped[code] = {
            **metadata[code],
            **dict(row),
            "code": code,
            "name": row.get("名称") or row.get("name") or metadata[code].get("name") or code,
            "price": row.get("price") or row.get("最新价") or row.get("close") or row.get("trade"),
            "volume": row.get("volume") or row.get("成交量") or row.get("vol"),
            "amount": row.get("amount") or row.get("成交额") or row.get("turnover"),
            "prev_close": row.get("prev_close") or row.get("昨收") or row.get("settlement"),
            "change_pct": row.get("change_pct") or row.get("涨跌幅") or row.get("changepercent"),
            "quote_source": source,
        }
    return mapped


def fetch_universe_quotes(universe: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    config = load_config()["network"]
    batch_size = int(config["quote_batch_size"])
    workers = int(config["quote_workers"])
    retries = int(config["request_retries"])
    minimum_coverage = max(
        500,
        int(len(universe) * float(config["quote_min_coverage"])),
    )
    metadata = {
        candidate_pipeline.naked_code(item["code"]): {
            **dict(item),
            "is_st": (
                item.get("is_st")
                if isinstance(item.get("is_st"), bool)
                else "ST" in str(item.get("name") or "").upper()
            ),
        }
        for item in universe
    }
    if is_auction_window():
        cached_quotes = load_cached_quotes()
        if len(cached_quotes) >= minimum_coverage:
            result = {
                code: {**metadata.get(code, {}), **fields, "code": code, "quote_source": "cache"}
                for code, fields in cached_quotes.items()
                if code in metadata
            }
            if len(result) >= minimum_coverage:
                fetch_universe_quotes.last_quote_source = "cache"
                return result
    batches = [
        [candidate_pipeline.market_code(item["code"]) for item in batch]
        for batch in _chunks(list(universe), batch_size)
    ]

    quotes: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch_quote_batch, batch, retries) for batch in batches]
        for future in as_completed(futures):
            try:
                batch_quotes = future.result()
            except DataSourceError:
                continue
            for prefixed, fields in batch_quotes.items():
                code = candidate_pipeline.naked_code(prefixed)
                quotes[code] = {**metadata.get(code, {}), **fields, "code": code}
    if len(quotes) < minimum_coverage:
        try:
            spot_quotes = _universe_spot_quotes(metadata)
        except DataSourceError:
            spot_quotes = {}
        for code, fields in spot_quotes.items():
            quotes.setdefault(code, fields)
    if len(quotes) < minimum_coverage:
        raise DataSourceError("market_quotes", f"全市场行情覆盖不足: {len(quotes)}/{len(universe)}")
    quotes = {
        code: {**fields, "quote_source": fields.get("quote_source", "tencent")}
        for code, fields in quotes.items()
    }
    atomic_write_json(quotes_cache_file(), {
        "schema": "universe_quotes_cache_v1",
        "updated_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "quotes": quotes,
    })
    sources = {str(fields.get("quote_source") or "tencent") for fields in quotes.values()}
    fetch_universe_quotes.last_quote_source = (
        next(iter(sources)) if len(sources) == 1 else "mixed"
    )
    return quotes


def fetch_candidate_klines(
    candidates: Sequence[Mapping[str, Any]],
    *,
    event_asof: str | None = None,
    decision_mode: str = "live",
) -> Dict[str, List[Dict[str, Any]]]:
    network_config = load_config()["network"]
    workers = int(network_config["kline_workers"])
    retries = int(network_config["request_retries"])

    def _fetch(item: Mapping[str, Any]) -> tuple[str, List[Dict[str, Any]]]:
        code = candidate_pipeline.naked_code(item["code"])
        market = "sh" if code.startswith("6") else "sz"
        attempts = min(retries + 1, 2)
        for attempt in range(attempts):
            if event_asof is None:
                bars = fetch_a_share_daily_kline(code, market=market, days=70)
            else:
                series = fetch_a_share_daily_series(
                    code,
                    market=market,
                    days=70,
                    event_asof=event_asof,
                    adjustment="qfq",
                    decision_mode=decision_mode,
                )
                bars = list(series.get("data") or []) if series.get("status") == "ok" else []
                provenance = series.get("series_provenance")
                if provenance:
                    bars = [{**bar, "series_provenance": dict(provenance)} for bar in bars]
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


def _candidate_pit_contract(
    asof: str,
    quote_map: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any] | None:
    fetched = [
        str(quote.get("fetched_at"))
        for quote in quote_map.values()
        if quote.get("fetched_at")
    ]
    if not fetched:
        return None
    try:
        timestamps = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in fetched]
        local = max(timestamps).astimezone(ZoneInfo("Asia/Shanghai")).isoformat(
            timespec="seconds"
        )
    except ValueError:
        return None
    if local[:10] != asof:
        return None
    return {
        "schema": "pit_stage_contract_v1",
        "decision_mode": "live",
        "event_asof": asof,
        "evidence_time": local,
        "captured_at": local,
        "stage_policy": {
            "schema": "pit_stage_contract_v1",
            "stage": "candidate_discovery",
            "cutoff_time": "23:59:59",
            "timezone": "Asia/Shanghai",
            "publication_delay_seconds": 0,
        },
    }


def _propagate_execution_contracts(
    result: Dict[str, Any],
    quote_map: Mapping[str, Mapping[str, Any]],
    kline_by_code: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    point_in_time: Mapping[str, Any] | None,
    decision_mode: str,
) -> None:
    for candidate in result.get("candidates", []):
        code = candidate_pipeline.naked_code(candidate.get("code"))
        quote = quote_map.get(code) or {}
        bars = list(kline_by_code.get(code) or [])
        provenance = (bars[0].get("series_provenance") if bars else None) or {}
        transport = {
            key: quote.get(key)
            for key in (
                "provider", "provider_version", "fetched_at", "transport_trust",
                "directional_eligible", "transport_reason",
            )
        }
        complete = bool(
            point_in_time
            and quote.get("listed_date")
            and isinstance(quote.get("is_st"), bool)
            and provenance.get("provider")
            and transport.get("provider")
            and transport.get("directional_eligible") is not None
        )
        candidate.update({
            "strict_execution": complete,
            "decision_mode": decision_mode,
            "point_in_time": dict(point_in_time or {}),
            "listing_date": quote.get("listed_date"),
            "listing_stage": "normal" if quote.get("listed_date") else None,
            "is_st": quote.get("is_st"),
            "series_provenance": dict(provenance),
            "transport_provenance": transport,
            "directional_eligible": transport.get("directional_eligible"),
        })


def _advance_fsm_to_watching(asof: str, selected_codes: Iterable[str]) -> None:
    """Route newly-selected watch-pool codes through the FSM's single entry
    point: screened -> watching. Bounded to the selected watch pool (a few
    hundred codes), not the full scanned universe, to keep the daily
    transition log small. Idempotent: codes already at watching/candidate/
    confirmed are left alone so re-running discovery the same day (e.g. the
    candidate-preopen bootstrap job) does not spam rejected transitions.
    Best-effort: an FSM write failure must never fail discovery, which is
    the authoritative watch-pool producer."""
    config = candidate_fsm.load_fsm_config()
    for code in selected_codes:
        try:
            state = candidate_fsm.current_state(code)
            if state is not None and state.get("to_state") != "screened":
                continue
            if state is None:
                candidate_fsm.transition(
                    code, "screened", "score_above_threshold", asof=asof, config=config,
                )
            candidate_fsm.transition(
                code, "watching", "score_above_threshold", asof=asof, config=config,
            )
        except Exception:  # noqa: BLE001
            continue


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
            max_age_days=1,
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
                "sector_momentum",
                "sector_rotation",
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
            "tier": "unknown",
            "context_status": "unknown",
            "height": 0,
            "promotion_rate": None,
            "limitup_total": None,
            "allow_new_daban": False,
            "position_multiplier": 0.0,
            "top_n_limit": 0,
            "retreat_signal": None,
            "advice": "温度数据不可用，阻断新增风险",
            "notes": [f"情绪上下文读取失败: {exc}"],
            "context_asof": None,
            "context_fresh": False,
        }


def load_cached_industry(asof: str) -> Mapping[str, str]:
    """读取按日缓存的全市场行业映射（消费端只读缓存，不触网）。

    缓存缺失/过期/被禁用 → 返回空映射，注入退化为 no-op，主链无回归。
    """
    industry_config = dict(load_config().get("industry_map") or {})
    if not industry_config.get("enabled", True):
        return {}
    return industry_map.load_cached(
        asof,
        max_age_days=int(industry_config.get("cache_max_age_days", 5)),
    )


def universe_sector_fallback(universe: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    """Use exchange/universe sector fields when the external industry cache is missing."""
    sectors: Dict[str, str] = {}
    for item in universe:
        code = candidate_pipeline.naked_code(item.get("code"))
        sector = str(
            item.get("sector")
            or item.get("industry")
            or item.get("sw_industry")
            or ""
        ).strip()
        if code and sector:
            sectors[code] = sector
    return sectors


def fetch_nl_screening_recall() -> Dict[str, Any]:
    """Second candidate-recall channel: natural-language screener backends.

    Read-only, additive: it only ever contributes extra candidate *codes* for
    the full-market enumeration to price, filter, and rank exactly like every
    other code — it never sets or influences a score directly. A configured
    but failing channel is reported "blocked" by nl_screening.recall_candidates
    and simply contributes zero extra codes this run; it must never be
    conflated with "no natural-language candidates exist".
    """
    return nl_screening.recall_candidates()


def merge_nl_screening_recall(
    universe: List[Dict[str, Any]],
    recall: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Tag every universe row with its recall source and append any
    NL-screening code missing from the full-market enumeration.

    Existing full-market rows are tagged ``full_market_enumeration`` (never
    overwritten by a narrower channel) so `funnel_recall_report.py` can always
    attribute every candidate to exactly one recall_source. Codes recalled by
    NL screening but already present in the full-market universe keep the
    ``full_market_enumeration`` tag — they were always reachable by the
    primary channel, so counting them again as an NL-exclusive recall would
    overstate the second channel's incremental contribution.

    A net-new code carries only ``code``/``name``/``recall_source`` here; it
    still has to clear the same `candidate_pipeline.filter_universe` gates
    (listing age, price, liquidity) as every other row once quotes are
    fetched. If the exchange listing snapshot has no record of it (e.g. a
    fresh IPO not yet in the cached SSE/SZSE universe), it fails closed via
    the existing "listed date missing" rejection reason rather than being
    special-cased into the watch pool — the second recall channel adds
    candidates to consider, it does not add a bypass around the existing
    tradeability gates.
    """
    merged = [
        {**item, "recall_source": item.get("recall_source") or "full_market_enumeration"}
        for item in universe
    ]
    known_codes = {candidate_pipeline.naked_code(item.get("code")) for item in merged}
    for item in recall.get("candidates", []) if isinstance(recall, Mapping) else []:
        code = candidate_pipeline.naked_code(item.get("code"))
        if not code or code in known_codes:
            continue
        merged.append({
            "code": code,
            "name": item.get("name") or code,
            "recall_source": item.get("recall_source") or "nl_screening",
        })
        known_codes.add(code)
    return merged


def run_discovery(
    asof: str,
    watch_limit: int | None = None,
    prefilter_limit: int | None = None,
    universe_fetcher: Callable[[], Sequence[Mapping[str, Any]]] = fetch_exchange_universe,
    quote_fetcher: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Mapping[str, Any]]]
    = fetch_universe_quotes,
    kline_fetcher: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Sequence[Mapping[str, Any]]]]
    = fetch_candidate_klines,
    industry_provider: Callable[[str], Mapping[str, str]] = load_cached_industry,
    nl_screening_recall_provider: Callable[[], Mapping[str, Any]] = fetch_nl_screening_recall,
    settle_previous: bool = True,
    max_ladder_age_days: int | None = None,
) -> Dict[str, Any]:
    config = load_config()
    universe_config = config["universe"]
    pipeline_config = config["pipeline"]
    nl_screening_config = dict(config.get("nl_screening_recall") or {})
    watch_limit = int(watch_limit or pipeline_config["watch_limit"])
    prefilter_limit = int(prefilter_limit or universe_config["prefilter_limit"])

    universe = list(universe_fetcher())
    industry_by_code = dict(industry_provider(asof) or {})
    if industry_by_code:
        universe = industry_map.enrich_records(universe, industry_by_code)
    else:
        industry_by_code = universe_sector_fallback(universe)

    nl_recall_report: Dict[str, Any] | None = None
    if nl_screening_config.get("enabled", True):
        try:
            nl_recall_report = dict(nl_screening_recall_provider())
        except Exception as exc:  # noqa: BLE001 - second recall channel must never block discovery
            nl_recall_report = {
                "schema": "nl_screening_recall_v1",
                "channels": [],
                "candidate_count": 0,
                "candidates": [],
                "error": str(exc),
            }
    universe = merge_nl_screening_recall(universe, nl_recall_report or {})
    quote_map = dict(quote_fetcher(universe))
    quote_source = str(
        getattr(quote_fetcher, "last_quote_source", "")
        or next(
            (
                item.get("quote_source")
                for item in quote_map.values()
                if item.get("quote_source")
            ),
            "live",
        )
    )

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
    production_kline = kline_fetcher is fetch_candidate_klines
    kline_by_code = dict(
        fetch_candidate_klines(
            enrichment_universe,
            event_asof=asof,
            decision_mode="live",
        )
        if production_kline
        else kline_fetcher(enrichment_universe)
    )
    batch_id = os.environ.get("A_STOCK_BATCH_ID") or f"a-share-{asof.replace('-', '')}"
    point_in_time = _candidate_pit_contract(asof, quote_map) if production_kline else None
    snapshot_pit = (
        {
            "event_asof": point_in_time["event_asof"],
            "evidence_time": point_in_time["evidence_time"],
            "captured_at": point_in_time["captured_at"],
            "decision_mode": point_in_time["decision_mode"],
            "stage_policy": point_in_time["stage_policy"],
        }
        if point_in_time
        else {}
    )
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
        **snapshot_pit,
    )
    inputs = input_snapshot["payload"]
    quote_map = dict(inputs["quotes"])
    kline_by_code = dict(inputs["klines"])
    signal_ctx = inputs.get("signal_context")
    temperature = dict(inputs["market_temperature"])
    if settle_previous:
        observe_recent_candidates(asof, quote_map)
    quotes = list(quote_map.values())
    selection_config = dict(config.get("hot_money_selection") or {})
    if max_ladder_age_days is not None:
        selection_config["max_ladder_age_days"] = int(max_ladder_age_days)
    prior_selection = read_json(hot_money_selection_file(), {})
    if str(prior_selection.get("asof") or "") >= asof:
        prior_selection = {}
    market_timing = hot_money_selection.build_market_timing(
        quotes,
        signal_ctx,
        event_asof=asof,
        config=selection_config,
    )
    selection_state = hot_money_selection.build_sector_leadership(
        quotes,
        signal_ctx,
        market_timing,
        previous_snapshot=prior_selection,
        config=selection_config,
    )
    selection_state.update({
        "asof": asof,
        "batch_id": batch_id,
        "input_snapshot": compact_ref(input_snapshot),
    })
    selection_snapshot = write_snapshot(
        "hot-money-selection-state",
        selection_state,
        trading_date=asof,
        batch_id=batch_id,
        producer="candidate-discovery",
        producer_version="hot-money-selection-v1",
        source_versions=input_snapshot.get("source_versions") or {},
    )
    selection_state["snapshot"] = compact_ref(selection_snapshot)
    result = candidate_pipeline.build_watch_pool(
        quotes,
        kline_by_code,
        watch_limit=watch_limit,
        min_amount=float(universe_config["min_amount"]),
        min_price=float(universe_config["min_price"]),
        min_listed_days=int(universe_config["min_listed_days"]),
        signal_ctx=signal_ctx,
        selection_state=selection_state,
    )
    for item in result.get("candidates", []):
        selected_by = item.get("selected_by") or {}
        lane = "daban" if selected_by.get("daban") else "trend"
        strategy_id = hot_money_selection.selection_strategy_id(
            item,
            lane,
        )
        item["strategy_id"] = strategy_id
        item["selection_context"] = hot_money_selection.selection_context_for(
            item,
            selection_state,
            window="D0_close",
        )
        code = candidate_pipeline.naked_code(item.get("code"))
        item["research_evidence"] = build_research_evidence(
            code,
            strategy_id=strategy_id,
            asof=asof,
            bars=list(kline_by_code.get(code) or []),
        )
    _propagate_execution_contracts(
        result,
        quote_map,
        kline_by_code,
        point_in_time=(input_snapshot.get("point_in_time") or point_in_time),
        decision_mode="live",
    )
    evaluated = result.pop("evaluated_candidates")
    result.update({
        "asof": asof,
        "status": "ready",
        "universe_source": "SSE+SZSE listings / Tencent quotes",
        "quote_source": quote_source,
        "enriched_count": len(kline_by_code),
        "market_temperature": temperature,
        "hot_money_selection": selection_state,
        "input_snapshot": compact_ref(input_snapshot),
        "nl_screening_recall": {
            "channels": (nl_recall_report or {}).get("channels", []),
            "candidate_count": (nl_recall_report or {}).get("candidate_count", 0),
            "error": (nl_recall_report or {}).get("error"),
        },
        "auction_scan_codes": [
            candidate_pipeline.market_code(item.get("code"))
            for item in evaluated
            if item.get("code")
        ],
        "auction_scan_count": len(evaluated),
    })
    # --- candidate_count > 0 assertion ---
    # Zero deliverable candidates used to mean an upstream data failure. After
    # the weak-market delivery gate, it can also be a valid "research-only"
    # outcome: we still have ranked scan codes for auction intelligence, but no
    # stock should be surfaced as an actionable watch target.
    if result.get("status") == "ready" and result.get("candidate_count", 0) == 0:
        has_scan_universe = bool(result.get("auction_scan_codes"))
        weak_regime = bool(
            (((selection_state or {}).get("market_timing") or {}).get("weak_market") or {}).get("weak_regime")
        )
        if has_scan_universe and weak_regime:
            result["research_only"] = True
            result["warnings"] = list(result.get("warnings") or []) + [
                "candidate_count=0_after_weak_market_delivery_gate"
            ]
        else:
            result["status"] = "error"
            result["error"] = (
                "candidate_count=0 after successful discovery — "
                "likely stale quotes, broken provider, or universe filter too tight"
            )

    _persist_pool(asof, result)
    atomic_write_json(hot_money_selection_file(), selection_state)
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
                    "strategy_id": item.get("strategy_id"),
                    "sector_rank": item.get("sector_rank"),
                    "leader_rank": item.get("leader_rank"),
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
    selected_item_by_code = {
        item["code"]: item
        for item in result["candidates"]
    }
    lifecycle_candidates = []
    for item in evaluated:
        record = dict(item)
        if item["code"] in selected_codes:
            selected_item = selected_item_by_code[item["code"]]
            record.update({
                "selected_by": dict(selected_item.get("selected_by") or {}),
                "strategy_id": selected_item.get("strategy_id"),
                "selection_context": dict(selected_item.get("selection_context") or {}),
            })
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
    _advance_fsm_to_watching(asof, selected_codes)
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
    selection = result.get("hot_money_selection") or {}
    timing = selection.get("market_timing") or {}
    timing_temp = timing.get("temperature") or {}
    lines.append(
        f"游资门禁：**{selection.get('status', 'insufficient_data')}** | "
        f"打板可用={bool(selection.get('daban_ready'))} | "
        f"时点={timing_temp.get('tier') or 'N/A'} | "
        f"板块覆盖={float(selection.get('sector_coverage') or 0):.1%}"
    )
    mainlines = [
        item for item in selection.get("sectors") or []
        if int(item.get("rank") or 999) <= 2
    ]
    if mainlines:
        lines.append(
            "主线板块：" + "；".join(
                f"{item.get('rank')}.{item.get('sector')}({item.get('state')},"
                f"涨停{item.get('limitup_count')},"
                f"证据{item.get('evidence_count')}:"
                f"{','.join(item.get('evidence_types') or []) or '-'},"
                f"分{item.get('score')})"
                for item in mainlines
            )
        )
    elif selection.get("reasons"):
        lines.append("打板关闭原因：" + "；".join(selection.get("reasons") or []))
    lines.extend([
        "",
        "| 代码 | 名称 | 板块/行业/排名 | 龙头排名 | 打板门禁 | 打板分 | 趋势分 |",
        "|------|------|-----------|----------|----------|--------|--------|",
    ])
    for item in result.get("candidates", [])[:20]:
        lines.append(
            f"| {item['code']} | {item['name']} | "
            f"{item.get('sector') or item.get('industry') or '-'} / "
            f"{item.get('sector_rank') or '-'} | "
            f"{item.get('leader_rank') or '-'} | "
            f"{'通过' if item.get('hot_money_qualified') else '关闭'} | "
            f"{item['daban_score']} | {item['trend_score']} |"
        )
    return "\n".join(lines)


def json_report(result: Mapping[str, Any]) -> Dict[str, Any]:
    selection = result.get("hot_money_selection") or {}
    timing = selection.get("market_timing") or {}
    report = {
        "schema": result.get("schema"),
        "asof": result.get("asof"),
        "status": result.get("status"),
        "bootstrap_status": result.get("bootstrap_status"),
        "quote_source": result.get("quote_source", "live"),
        "scanned_count": result.get("scanned_count", 0),
        "eligible_count": result.get("eligible_count", 0),
        "rejected_count": result.get("rejected_count", 0),
        "enriched_count": result.get("enriched_count", 0),
        "candidate_count": result.get("candidate_count", 0),
        "hot_money_selection": {
            "status": selection.get("status") or "insufficient_data",
            "daban_ready": bool(selection.get("daban_ready")),
            "research_only": bool(selection.get("research_only", True)),
            "sector_coverage": selection.get("sector_coverage"),
            "market_timing": {
                "status": timing.get("status"),
                "daban_ready": bool(timing.get("daban_ready")),
                "breadth": timing.get("breadth"),
                "previous_ladder_premium": timing.get("previous_ladder_premium"),
                "tier": (timing.get("temperature") or {}).get("tier"),
                "reasons": timing.get("reasons"),
                "context_asof": timing.get("context_asof"),
                "context_fresh": (timing.get("temperature") or {}).get("context_fresh"),
                "temperature_notes": (timing.get("temperature") or {}).get("notes"),
            },
            "mainline_sectors": [
                {
                    key: item.get(key)
                    for key in (
                        "sector", "rank", "score", "state",
                        "limitup_count", "qualified_for_daban",
                        "evidence_count", "evidence_types",
                        "theme_confirmed", "theme_attention_score",
                        "theme_confirmed_stock_count", "sector_flow_yi",
                    )
                }
                for item in selection.get("sectors") or []
                if int(item.get("rank") or 999) <= 2
            ],
            "reasons": list(selection.get("reasons") or []),
            "snapshot_id": (selection.get("snapshot") or {}).get("snapshot_id"),
        },
        "rejection_reason_counts": _reason_counts(result.get("rejected", {})),
        "top_candidates": [
            {
                key: item.get(key)
                for key in (
                    "code", "name", "daban_rank", "daban_score",
                    "trend_rank", "trend_score", "strategy_id",
                    "sector", "sector_source", "industry", "industry_source",
                    "sector_rank", "sector_state",
                    "sector_evidence_count", "sector_evidence_types",
                    "sector_theme_confirmed", "sector_theme_attention_score",
                    "sector_flow_yi",
                    "leader_rank", "leader_role", "hot_money_qualified",
                )
            }
            for item in result.get("candidates", [])[:5]
        ],
    }
    report["intelligence"] = stage_intelligence.preopen_digest(result)
    return report


def _check_pool_freshness(
    asof: str,
    as_json: bool = False,
    alert_mode: bool = False,
) -> None:
    """Heartbeat check: verify the latest candidate pool exists, is fresh, and has candidates.

    Exit codes:
      0 — pool is healthy
      1 — pool is stale, empty, or missing
    """
    pool = read_json(latest_pool_file(), {})
    issues: List[str] = []

    # 1. Pool exists?
    if not pool:
        issues.append("no_pool_file")
    else:
        # 2. Status OK?
        status = pool.get("status")
        if status not in ("ready", "degraded"):
            issues.append(f"status={status}")

        # 3. Has candidates?
        candidate_count = pool.get("candidate_count", 0)
        if candidate_count == 0:
            issues.append("candidate_count=0")

        # 4. Fresh? (generated within last 2 trading days)
        try:
            generated = datetime.fromisoformat(str(pool.get("generated_at")))
            age_hours = (datetime.now() - generated).total_seconds() / 3600
            if age_hours > 48:  # 48 hours = ~2 trading days
                issues.append(f"stale:{age_hours:.0f}h_old")
        except (TypeError, ValueError):
            issues.append("invalid_generated_at")

        # 5. Asof matches today?
        pool_asof = str(pool.get("asof", ""))
        if pool_asof != asof:
            issues.append(f"asof_mismatch:pool={pool_asof},expected={asof}")

    healthy = len(issues) == 0
    alerts = []
    if not healthy:
        alerts.append({
            "severity": "error",
            "message": "候选池异常",
            "issues": issues,
        })
    report = {
        "schema": "candidate_pool_freshness_v1",
        "status": "ok" if healthy else "error",
        "healthy": healthy,
        "asof": asof,
        "candidate_count": pool.get("candidate_count", 0) if pool else 0,
        "pool_status": pool.get("status") if pool else None,
        "pool_asof": pool.get("asof") if pool else None,
        "generated_at": pool.get("generated_at") if pool else None,
        "issues": issues,
        "alerts": alerts,
    }

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if healthy:
            print(f"✅ 候选池健康: {report['candidate_count']} 只候选, asof={report['pool_asof']}")
        else:
            print(f"❌ 候选池异常: {', '.join(issues)}")

    raise SystemExit(0 if (healthy or alert_mode) else 1)


def _run_warmup_cache(args: argparse.Namespace) -> None:
    try:
        universe = fetch_exchange_universe()
        quotes = fetch_universe_quotes(universe)
        result = {
            "schema": "universe_quotes_cache_warmup_v1",
            "status": "ok",
            "quote_source": getattr(fetch_universe_quotes, "last_quote_source", "live"),
            "quote_count": len(quotes),
        }
    except Exception as exc:  # noqa: BLE001 - cron reports warmup failures as data errors
        result = {"schema": "universe_quotes_cache_warmup_v1", "status": "error", "error": str(exc)}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"行情缓存预热失败：{exc}")
        raise SystemExit(75)
    print(json.dumps(result, ensure_ascii=False) if args.json else f"行情缓存预热完成：{result['quote_count']} 只")


def _build_arg_parser(config: Dict[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A股全市场动态候选发现")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--watch-limit", type=int, default=config["pipeline"]["watch_limit"])
    parser.add_argument("--prefilter-limit", type=int, default=config["universe"]["prefilter_limit"])
    parser.add_argument(
        "--no-settle",
        action="store_true",
        help="Skip prior-candidate settlement for pre-open bootstrap runs",
    )
    parser.add_argument(
        "--bootstrap-if-missing",
        action="store_true",
        help="Reuse a recent closing pool and scan only for cold start or expiry",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--warmup-cache",
        action="store_true",
        help="Refresh the full-market quote cache without generating a candidate pool",
    )
    parser.add_argument("--max-ladder-age-days", type=int)
    parser.add_argument(
        "--check-freshness",
        action="store_true",
        help="Check if the latest candidate pool is fresh and has candidates; exit non-zero if stale or empty",
    )
    parser.add_argument(
        "--alert-mode",
        action="store_true",
        help="In freshness mode, report unhealthy pools as alert payloads and exit 0 for delivery",
    )
    return parser


def main() -> None:
    config = load_config()
    args = _build_arg_parser(config).parse_args()

    if args.warmup_cache:
        _run_warmup_cache(args)
        return

    # --- freshness heartbeat mode ---
    if args.check_freshness:
        _check_pool_freshness(asof=args.asof, as_json=args.json, alert_mode=args.alert_mode)
        return

    try:
        existing = read_json(latest_pool_file(), {})
        if args.bootstrap_if_missing and reusable_pool(existing, args.asof):
            result = {**existing, "bootstrap_status": "reused_existing"}
        else:
            result = run_discovery(
                args.asof,
                watch_limit=args.watch_limit,
                prefilter_limit=args.prefilter_limit,
                settle_previous=not args.no_settle,
                max_ladder_age_days=args.max_ladder_age_days,
            )
            if args.bootstrap_if_missing:
                result["bootstrap_status"] = "generated_missing_or_stale"
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
        raise SystemExit(75)

    if args.json:
        print(json.dumps(json_report(result), ensure_ascii=False, default=str))
    else:
        print(format_report(result))

    # Exit non-zero if discovery produced an error (e.g. candidate_count=0 assertion)
    if result.get("status") == "error":
        raise SystemExit(75)


if __name__ == "__main__":
    main()
