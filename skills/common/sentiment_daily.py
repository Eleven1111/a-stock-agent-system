"""每日情绪数据集 ``sentiment_daily``（升级方案 P0-a/P0-b）。

系统此前把涨跌停家数、梯队溢价、晋级率算完就丢：``build_market_timing`` 每天重算
一遍，只有当日那一帧进产物，没有任何可回溯的时序。P1 的状态机校准与 P3 的策略回测
都要问"这一天的炸板率相对过去 120 天处在什么位置"，没有落盘的日频序列就无从回答。
本模块把那些指标固化成一份 append-only 的每日数据集。

三条硬性质（与 sector_series 同源的教训，不是装饰）：

1. **空集不产出数字。** 全市场快照为空、或触及涨停家数为 0 时，``break_rate`` 等
   派生比率返回 ``None`` 并进 ``unavailable_fields``，绝不返回 0.0 —— "今天没有
   炸板" 和 "今天没有数据" 必须可区分。
2. **回填不到的字段显式标 unavailable，不插值。** 日线只能给出龙头当日收盘跌幅，
   给不出分钟级日内最大回撤，后者恒为 ``None`` + unavailable，不用 (close-high)/high
   之类的日线代理冒充。
3. **写入前过 dataset_contract 契约。** 字段名/类型/point-in-time 顺序由
   ``config/dataset_catalog.json`` 的 ``sentiment_daily_v1`` 契约判定，契约不过
   一行都不写。

只消费已固化的产物（candidate-discovery 输入快照 / 本地日线缓存），不触网。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from config_registry import CONFIG_DIR
from dataset_contract import DatasetContractError, load_catalog, resolve_dataset, validate_records
from market_snapshot import read_snapshot
from paths import data_file, hermes_home
from signal_context import context_file
from state_store import file_lock, read_json
from tradeability import limit_pct, round_limit


SCHEMA = "sentiment_daily_v1"
DATASET_ID = "sentiment_daily_v1"
CATALOG_FILE = str(CONFIG_DIR / "dataset_catalog.json")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_CLOSE = time(15, 0)

_SERIES_SUBDIR = "sentiment_daily"
_SUMMARY_FILE = "sentiment_daily.jsonl"

#: 涨跌停价比较容差（元）。交易所限价四舍五入到分，浮点比较必须留半分容差。
_PRICE_EPSILON = 0.005

#: 方案 §3.1(a) 的字段口径表。顺序即报告顺序。
METRIC_FIELDS = (
    "limit_count",
    "limit_down_count",
    "touch_limit_count",
    "break_rate",
    "limit_premium_open",
    "limit_premium_close",
    "limit_red_ratio",
    "advance_count",
    "decline_count",
    "adr",
    "max_board",
    "board4plus",
    "leader_damage",
    "sector_breadth_top",
)


def sentiment_daily_dir() -> str:
    return os.path.join(hermes_home(), "market", _SERIES_SUBDIR)


def record_file(trading_date: str) -> str:
    return os.path.join(sentiment_daily_dir(), f"{trading_date}.json")


def summary_file() -> str:
    return os.path.join(sentiment_daily_dir(), _SUMMARY_FILE)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _code(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    if text.startswith(("sh", "sz", "bj")):
        text = text[2:]
    return text.zfill(6) if text.isdigit() else ""


def _row(code: str, quote: Mapping[str, Any]) -> dict[str, Any] | None:
    """一行行情 → 统一口径。收盘或昨收缺失即丢弃，不用当前价冒充昨收。"""
    close = _number(quote.get("close"))
    if close is None:
        close = _number(quote.get("price"))
    preclose = _number(quote.get("preclose"))
    if preclose is None:
        preclose = _number(quote.get("prev_close"))
    if close is None or preclose is None or preclose <= 0 or close <= 0:
        return None
    return {
        "code": code,
        "name": str(quote.get("name") or ""),
        "open": _number(quote.get("open")),
        "high": _number(quote.get("high")),
        "close": close,
        "preclose": preclose,
    }


def normalize_rows(quotes: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """接受 {code: quote} 映射或行序列（实时快照与日线缓存两条来源同一口径）。"""
    rows: list[dict[str, Any]] = []
    if isinstance(quotes, Mapping):
        items: Iterable[tuple[Any, Any]] = quotes.items()
    else:
        items = ((item.get("code") if isinstance(item, Mapping) else None, item)
                 for item in (quotes or []))
    for key, quote in items:
        if not isinstance(quote, Mapping):
            continue
        code = _code(quote.get("code") or key)
        row = _row(code, quote) if code else None
        if row is not None:
            rows.append(row)
    return rows


def _limit_prices(row: Mapping[str, Any]) -> tuple[float, float]:
    pct = limit_pct(str(row.get("code") or ""), str(row.get("name") or ""))
    preclose = float(row["preclose"])
    return round_limit(preclose, pct, up=True), round_limit(preclose, pct, up=False)


def limit_flags(row: Mapping[str, Any]) -> dict[str, Any]:
    """单只个股的封板/触及/跌停判定。

    触及用 ``high == 当日涨停价``（方案 §12 已记录其局限：盘中触及后回落且 high
    失真的极端情形会漏计）。``high`` 缺失时 ``touched`` 为 ``None``——未知不是"没触及"。
    """
    up, down = _limit_prices(row)
    high = _number(row.get("high"))
    close = float(row["close"])
    return {
        "limit_up_price": up,
        "limit_down_price": down,
        "sealed": close >= up - _PRICE_EPSILON,
        "touched": None if high is None else high >= up - _PRICE_EPSILON,
        "limit_down": close <= down + _PRICE_EPSILON,
    }


def compute_limit_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """封板/跌停/触及家数与炸板率。触及样本为空 → break_rate 不可用（非 0.0）。"""
    flags = [limit_flags(row) for row in rows]
    sealed = sum(1 for item in flags if item["sealed"])
    touched = sum(1 for item in flags if item["touched"])
    touch_observed = sum(1 for item in flags if item["touched"] is not None)
    limit_down = sum(1 for item in flags if item["limit_down"])
    if not rows:
        return {"limit_count": None, "limit_down_count": None,
                "touch_limit_count": None, "break_rate": None}
    return {
        "limit_count": sealed,
        "limit_down_count": limit_down,
        "touch_limit_count": touched if touch_observed else None,
        "break_rate": (round((touched - sealed) / touched, 4)
                       if touch_observed and touched > 0 else None),
    }


def compute_breadth_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """涨跌家数与涨跌比。跌家数为 0 时 adr 不可用，不伪造无穷大。"""
    if not rows:
        return {"advance_count": None, "decline_count": None, "adr": None}
    advance = sum(1 for row in rows if row["close"] > row["preclose"])
    decline = sum(1 for row in rows if row["close"] < row["preclose"])
    return {
        "advance_count": advance,
        "decline_count": decline,
        "adr": round(advance / decline, 4) if decline > 0 else None,
    }


def compute_premium_metrics(
    rows: Sequence[Mapping[str, Any]], prev_limit_codes: Iterable[str] | None
) -> dict[str, Any]:
    """昨日封板股今日的开盘/收盘溢价与收红比例。样本为空 → 三项全不可用。"""
    codes = {_code(item) for item in (prev_limit_codes or [])} - {""}
    cohort = [row for row in rows if row["code"] in codes]
    if not cohort:
        return {"limit_premium_open": None, "limit_premium_close": None,
                "limit_red_ratio": None}
    opens = [
        (row["open"] - row["preclose"]) / row["preclose"] * 100.0
        for row in cohort if row.get("open") is not None
    ]
    closes = [
        (row["close"] - row["preclose"]) / row["preclose"] * 100.0 for row in cohort
    ]
    return {
        "limit_premium_open": round(mean(opens), 4) if opens else None,
        "limit_premium_close": round(mean(closes), 4) if closes else None,
        "limit_red_ratio": round(
            sum(1 for row in cohort if row["close"] > row["preclose"]) / len(cohort), 4
        ),
    }


def _ladder_heights(ladder: Mapping[str, Any] | None) -> list[int]:
    heights: list[int] = []
    for entry in (ladder or {}).values():
        if not isinstance(entry, Mapping):
            continue
        value = entry.get("height") or entry.get("board_height") or entry.get("boards")
        try:
            height = int(value)
        except (TypeError, ValueError):
            continue
        if height > 0:
            heights.append(height)
    return heights


def compute_ladder_metrics(ladder: Mapping[str, Any] | None) -> dict[str, Any]:
    """最高连板与 ≥4 板家数。梯队缺失 → 不可用；梯队为空字典 → 0（真的没有连板）。"""
    if ladder is None:
        return {"max_board": None, "board4plus": None}
    heights = _ladder_heights(ladder)
    return {
        "max_board": max(heights) if heights else 0,
        "board4plus": sum(1 for height in heights if height >= 4),
    }


def compute_leader_damage(
    rows: Sequence[Mapping[str, Any]], leader_code: str | None
) -> dict[str, Any]:
    """标杆股当日收盘涨跌幅。日内最大回撤需分钟线，日线路径恒为不可用。"""
    code = _code(leader_code)
    row = next((item for item in rows if item["code"] == code), None) if code else None
    if row is None:
        return {"leader_damage": None, "leader_damage_intraday_drawdown": None,
                "leader_code": code or None}
    return {
        "leader_damage": round((row["close"] - row["preclose"]) / row["preclose"] * 100.0, 4),
        "leader_damage_intraday_drawdown": None,
        "leader_code": code,
    }


def compute_sentiment_metrics(
    quotes: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    prev_limit_codes: Iterable[str] | None = None,
    ladder: Mapping[str, Any] | None = None,
    leader_code: str | None = None,
    sector_breadth_top: int | None = None,
) -> dict[str, Any]:
    """方案 §3.1(a) 全部 14 个字段的纯函数计算。

    返回 ``metrics`` 与 ``unavailable_fields``：后者是显式的缺口清单，消费端凭它
    区分"该字段为 0"与"该字段没算出来"。行情为空时 ``status`` 为 ``unavailable``。
    """
    rows = normalize_rows(quotes)
    metrics: dict[str, Any] = {}
    metrics.update(compute_limit_metrics(rows))
    metrics.update(compute_breadth_metrics(rows))
    metrics.update(compute_premium_metrics(rows, prev_limit_codes))
    metrics.update(compute_ladder_metrics(ladder))
    damage = compute_leader_damage(rows, leader_code)
    leader_code_value = damage.pop("leader_code")
    metrics.update(damage)
    metrics["sector_breadth_top"] = (
        int(sector_breadth_top) if sector_breadth_top is not None else None
    )
    unavailable = sorted(name for name, value in metrics.items() if value is None)
    return {
        "status": "ok" if rows else "unavailable",
        "metrics": metrics,
        "leader_code": leader_code_value,
        "universe_count": len(rows),
        "unavailable_fields": unavailable,
    }


def _session_timestamp(trading_date: str) -> str:
    return datetime.combine(
        date.fromisoformat(trading_date), SESSION_CLOSE, tzinfo=SHANGHAI
    ).isoformat()


def coverage_status(coverage_ratio: float | None, minimum_ratio: float) -> str:
    """覆盖率低于契约下限的日期记 ``partial``：仍落盘（研究需要），但带标记，
    消费端不能把半个市场的涨停家数当全市场口径用。"""
    if coverage_ratio is None:
        return "unknown"
    return "full" if coverage_ratio >= minimum_ratio else "partial"


def contract() -> dict[str, Any]:
    return resolve_dataset(load_catalog(CATALOG_FILE), DATASET_ID)


def build_record(
    computed: Mapping[str, Any],
    *,
    trading_date: str,
    snapshot_ref: str,
    source: str,
    universe_expected: int | None = None,
) -> dict[str, Any]:
    """把纯函数结果组装成一条**契约字段严格对齐**的记录。"""
    observed_at = _session_timestamp(trading_date)
    universe_count = int(computed.get("universe_count") or 0)
    ratio = (
        round(universe_count / universe_expected, 4)
        if universe_expected else None
    )
    minimum = float(contract()["coverage"]["minimum_ratio"])
    metrics = dict(computed.get("metrics") or {})
    return {
        "trading_date": trading_date,
        "session_end_date": trading_date,
        "observed_at": observed_at,
        "available_at": observed_at,
        "snapshot_ref": snapshot_ref,
        "source": source,
        "status": str(computed.get("status") or "unavailable"),
        "universe_count": universe_count,
        "coverage_ratio": ratio,
        "coverage_status": coverage_status(ratio, minimum),
        "leader_code": computed.get("leader_code"),
        "unavailable_fields": ",".join(computed.get("unavailable_fields") or []),
        "leader_damage_intraday_drawdown": metrics.get("leader_damage_intraday_drawdown"),
        **{name: metrics.get(name) for name in METRIC_FIELDS},
    }


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """契约校验；不满足直接抛 ``DatasetContractError``（写入前 fail-closed）。"""
    return validate_records([dict(record)], contract())


def _write_summary(records: Sequence[Mapping[str, Any]]) -> None:
    path = summary_file()
    lines = [json.dumps(dict(item), ensure_ascii=False, sort_keys=True) for item in records]
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(temporary, path)


def load_summary() -> list[dict[str, Any]]:
    """按交易日升序返回汇总视图；坏行跳过而不是让整份序列不可读。"""
    try:
        with open(summary_file(), encoding="utf-8") as handle:
            raw = handle.readlines()
    except OSError:
        return []
    records = []
    for line in raw:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and value.get("trading_date"):
            records.append(dict(value))
    return sorted(records, key=lambda item: str(item.get("trading_date")))


def write_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """落一天的记录：每日 JSON + 汇总 JSONL。

    幂等：同一交易日重跑覆盖当日文件，并在汇总里**替换**该日那一行而不是追加第
    二行——重跑一天不该让 120 日滚动窗口里出现两次同一天。
    """
    validation = validate_record(record)
    row = dict(record)
    os.makedirs(sentiment_daily_dir(), exist_ok=True)
    path = record_file(str(row["trading_date"]))
    with file_lock(summary_file()):
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"schema": SCHEMA, "record": row}, handle,
                      ensure_ascii=False, sort_keys=True, indent=2)
        os.replace(temporary, path)
        kept = [item for item in load_summary()
                if str(item.get("trading_date")) != str(row["trading_date"])]
        _write_summary(sorted([*kept, row], key=lambda item: str(item.get("trading_date"))))
    return {"schema": SCHEMA, "path": path, "validation": validation,
            "trading_date": row["trading_date"]}


def persist_metrics(
    computed: Mapping[str, Any],
    *,
    trading_date: str,
    snapshot_ref: str,
    source: str,
    universe_expected: int | None = None,
) -> dict[str, Any]:
    """组装 + 校验 + 落盘的一步封装；契约不过返回 ``status=blocked`` 而不抛。"""
    record = build_record(
        computed,
        trading_date=trading_date,
        snapshot_ref=snapshot_ref,
        source=source,
        universe_expected=universe_expected,
    )
    try:
        written = write_record(record)
    except DatasetContractError as exc:
        return {"schema": SCHEMA, "status": "blocked", "trading_date": trading_date,
                "reason": "contract_violation", "errors": list(exc.errors)}
    return {
        "schema": SCHEMA,
        "status": record["status"],
        "trading_date": trading_date,
        "path": written["path"],
        "coverage_status": record["coverage_status"],
        "unavailable_fields": record["unavailable_fields"],
    }


# ========== 生产路径：只读 DAG 已固化的产物，不取数、不触网 ==========

def _snapshot_inputs(pool: Mapping[str, Any], asof: str) -> dict[str, Any] | None:
    """candidate-discovery 的输入快照（内容寻址）。日期不符即 fail-closed。"""
    path = (pool.get("input_snapshot") or {}).get("snapshot_path")
    if not path or not os.path.exists(str(path)):
        return None
    try:
        record = read_snapshot(str(path))
    except (ValueError, OSError):
        return None
    payload = record.get("payload") or {}
    if str(payload.get("schema") or "") != "candidate_discovery_inputs_v1":
        return None
    if str(record.get("trading_date") or "") != asof:
        return None
    return {
        "quotes": payload.get("quotes") or {},
        "signal_context": payload.get("signal_context") or {},
        "snapshot_ref": str(record.get("snapshot_id") or path),
    }


def load_frozen_inputs(asof: str) -> dict[str, Any] | None:
    """全市场行情 + 连板梯队。优先输入快照，回退到 discovery 写快照的原始来源
    （``universe_quotes_cache`` + ``signal_context``，等价口径）。两者都没有 →
    None，调用方按 ``blocked`` 处理，不拿隔日数据顶替。"""
    pool = read_json(data_file("stock-triage", "candidate_pool_latest.json"), {})
    if not isinstance(pool, Mapping):
        return None
    if str(pool.get("asof") or "") not in ("", asof):
        return None
    resolved = _snapshot_inputs(pool, asof)
    if resolved is not None:
        return resolved
    cache = read_json(data_file("stock-triage", "universe_quotes_cache.json"), {})
    quotes = cache.get("quotes") if isinstance(cache, Mapping) else None
    context = read_json(context_file(), {})
    if not isinstance(quotes, Mapping) or not quotes or not isinstance(context, Mapping):
        return None
    if not context.get("lianban_ladder"):
        return None
    return {"quotes": quotes, "signal_context": context,
            "snapshot_ref": f"universe_quotes_cache:{asof}"}


def sector_breadth_top(selection_state: Mapping[str, Any] | None, *, top_n: int = 3) -> int | None:
    """主线板块（排名前 ``top_n``）里最高的涨停家数。板块产物缺失 → None。"""
    rows = [row for row in (selection_state or {}).get("sectors") or [] if isinstance(row, Mapping)]
    if not rows:
        return None
    ranked = sorted(rows, key=lambda row: int(row.get("rank") or 10_000))[:top_n]
    counts = [int(row.get("limitup_count") or 0) for row in ranked]
    return max(counts) if counts else None


def universe_expected() -> int | None:
    """覆盖率分母 = 交易所全量证券数。名册缺失时返回 None（覆盖率未知，
    ``coverage_status`` 记 ``unknown``），不拿当日行情条数当分母自证 100%。"""
    payload = read_json(data_file("stock-triage", "exchange_universe.json"), {})
    rows = payload.get("stocks") if isinstance(payload, Mapping) else None
    return len(rows) if isinstance(rows, list) and rows else None


def _leader_from_ladder(ladder: Mapping[str, Any] | None) -> str | None:
    entries = [
        (int(entry.get("height") or entry.get("board_height") or 0), str(code))
        for code, entry in (ladder or {}).items()
        if isinstance(entry, Mapping)
    ]
    ranked = sorted((item for item in entries if item[0] > 0), reverse=True)
    return ranked[0][1] if ranked else None


def produce_daily_record(
    asof: str, selection_state: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """当日 ``sentiment_daily`` 记录的生产入口（cycle-state-shadow 作业调用）。

    输入缺失时返回 ``status=blocked`` 并说明缺口——不写一行"全 null"的记录，那会
    让"这天没跑"和"这天跑了但没数据"再次混为一谈；缺口由作业产物暴露。
    """
    inputs = load_frozen_inputs(asof)
    if inputs is None:
        return {"schema": SCHEMA, "status": "blocked", "trading_date": asof,
                "reason": "frozen_inputs_unavailable"}
    context = inputs["signal_context"]
    computed = compute_sentiment_metrics(
        inputs["quotes"],
        prev_limit_codes=list((context.get("prev_lianban_ladder") or {}).keys()),
        ladder=context.get("lianban_ladder"),
        leader_code=_leader_from_ladder(context.get("prev_lianban_ladder")),
        sector_breadth_top=sector_breadth_top(selection_state),
    )
    return persist_metrics(
        computed,
        trading_date=asof,
        snapshot_ref=str(inputs["snapshot_ref"]),
        source="candidate_discovery_inputs",
        universe_expected=universe_expected(),
    )


def summarize(records: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """artifact 安全的计数摘要——序列本体绝不进 cron 产物。"""
    rows = list(records if records is not None else load_summary())
    dates = [str(item.get("trading_date")) for item in rows if item.get("trading_date")]
    return {
        "schema": SCHEMA,
        "trading_day_count": len(dates),
        "first_trading_date": dates[0] if dates else None,
        "last_trading_date": dates[-1] if dates else None,
        "partial_coverage_days": sum(
            1 for item in rows if item.get("coverage_status") == "partial"
        ),
    }
