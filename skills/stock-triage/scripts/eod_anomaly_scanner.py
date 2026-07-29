#!/usr/bin/env python3
"""
尾盘异动扫描器 — 捕捉尾盘放量拉升标的
====================================
背景（2026-06-30）：思特威-W(688213) 开盘后 9:45 被资金集中点火拉升 +7%。
回溯发现前一交易日 14:15 就有异动：5分钟内涨2%，量能放大3倍。如果能在
尾盘捕捉到这个信号，次日开盘就能提前布局。

信号 = 尾盘异动(选股) + 次日板块共振确认(需人工判断) + 位置/估值合理(风控)

尾盘异动定义（14:30-15:00 最后30分钟窗口，需收盘后运行才有完整数据）：
- 量比 >= 2.5x：尾盘30分钟成交量 / 全天其余7个30分钟窗口的平均成交量
- 涨幅 >= 1.5%：尾盘30分钟区间价格上涨

过滤条件（减少假信号）：60日位置 < 70%、PE 0-100、市值 > 50亿、成交额 > 1亿、
排除 ST/退市。这些阈值来自设计说明，未经 chanlun-backtest 离线回测校准，属于
观察性信号，不构成买入依据——用法见下方"次日板块共振"。

用法：
  Day1 14:30后  扫描模式 -> 发现尾盘异动标的，自动存档
    python3 eod_anomaly_scanner.py [--top 15] [--out file] [--json]
  Day2 9:30后   确认模式 -> 读取存档，对比今日开盘跳空
    python3 eod_anomaly_scanner.py --confirm [-i file.json] [--json]

次日使用方式：如果异动标的所属板块次日也走强 -> 真信号，可参与；如果板块无
共振 -> 假信号，观望；如果板块走弱 -> 可能是诱多，回避。板块共振判断不在本
脚本范围内，需配合 hot-money-tactics / news-to-sector 人工确认。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from a_stock_http import DataSourceError  # noqa: E402
from market_adapters import (  # noqa: E402
    fetch_a_share_spot,
    fetch_tencent_kline,
    fetch_tencent_minute,
    fetch_tencent_quote,
)
from paths import data_file  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

SCHEMA = "eod_anomaly_scan_v1"
CONFIRM_SCHEMA = "eod_anomaly_confirm_v1"

TAIL_WINDOW_START = "1430"
BUCKETS_BEFORE_TAIL = 7  # A股标准交易时段 9:30-11:30(4) + 13:00-14:30(3) = 7 个30分钟窗口
VOLUME_RATIO_MIN = 2.5
PRICE_CHANGE_MIN_PCT = 1.5
POSITION_60D_MAX_PCT = 70.0
PE_MIN, PE_MAX = 0.0, 100.0
MARKET_CAP_MIN_YI = 50.0
AMOUNT_MIN_YI = 1.0

SPOT_RETRIES = 2  # 东财CDN（经akshare）间歇性不可用，需重试
MINUTE_WORKERS = 8
KLINE_WORKERS = 8


def _archive_path() -> str:
    return data_file("stock-triage", "eod_anomaly_latest.json")


def _dated_path(asof: str) -> str:
    return data_file("stock-triage", f"eod_anomaly_{asof}.json")


def _market_of(code: str) -> str:
    return "sh" if str(code).startswith(("6", "9")) else "sz"


# ======================== 纯函数：全A粗筛 ========================

def _is_excluded_name(name: str) -> bool:
    upper = (name or "").upper()
    return "ST" in upper or "退" in (name or "")


def screen_universe(spot_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """全A快照 -> 市值/成交额/PE/ST 粗筛，把全市场缩到值得做分钟级检测的范围。

    注意：akshare Sina 路线（stock_zh_a_spot）不返回 总市值 和 市盈率-动态 字段，
    此时跳过这两个维度的过滤，仅保留成交额和价格粗筛（补丁 2026-07-27）。
    """
    screened = []
    for record in spot_rows:
        name = str(record.get("名称") or "")
        code = str(record.get("代码") or "").zfill(6)
        if not code or _is_excluded_name(name):
            continue
        try:
            price = float(record.get("最新价") or 0)
            amount = float(record.get("成交额") or 0)
            market_cap_raw = record.get("总市值")
            market_cap = float(market_cap_raw) if market_cap_raw not in (None, "", "-") else None
            pe_raw = record.get("市盈率-动态")
            pe = float(pe_raw) if pe_raw not in (None, "", "-") else None
        except (TypeError, ValueError):
            continue
        if price <= 0 or amount < AMOUNT_MIN_YI * 1e8:
            continue
        # 可选过滤：源数据有总市值才做市值过滤（akshare Sina 路线没有此字段）
        if market_cap is not None and market_cap < MARKET_CAP_MIN_YI * 1e8:
            continue
        # 可选过滤：源数据有 PE 才做 PE 范围过滤
        if pe is not None and not (PE_MIN <= pe <= PE_MAX):
            continue
        screened.append({
            "code": code, "name": name, "price": price,
            "amount": amount, "market_cap": market_cap, "pe": pe,
        })
    return screened


# ======================== 纯函数：尾盘异动计算 ========================

def compute_tail_anomaly(minute_rows: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """从全天分钟数据算尾盘量比+涨幅。数据未覆盖到收盘（如盘中运行）返回 None，不臆造。"""
    rows = sorted((row for row in minute_rows if row.get("time")), key=lambda row: row["time"])
    if not rows or rows[-1]["time"] < "1500":
        return None

    before_tail = [row for row in rows if row["time"] < TAIL_WINDOW_START]
    tail = [row for row in rows if row["time"] >= TAIL_WINDOW_START]
    if not before_tail or not tail:
        return None

    baseline_volume = before_tail[-1]["cum_volume"]
    tail_volume = tail[-1]["cum_volume"] - baseline_volume
    avg_30min_volume = baseline_volume / BUCKETS_BEFORE_TAIL if BUCKETS_BEFORE_TAIL else 0
    if avg_30min_volume <= 0:
        return None

    tail_start_price = before_tail[-1]["price"]
    close_price = tail[-1]["price"]
    if tail_start_price <= 0:
        return None

    return {
        "tail_volume_ratio": round(tail_volume / avg_30min_volume, 2),
        "tail_price_change_pct": round((close_price - tail_start_price) / tail_start_price * 100, 2),
        "close_price": close_price,
    }


def is_tail_anomaly(signal: Optional[Mapping[str, Any]]) -> bool:
    if not signal:
        return False
    ratio = signal.get("tail_volume_ratio")
    change = signal.get("tail_price_change_pct")
    return ratio is not None and ratio >= VOLUME_RATIO_MIN and change is not None and change >= PRICE_CHANGE_MIN_PCT


# ======================== 纯函数：60日位置 ========================

def compute_position_60d_pct(daily_bars: Sequence[Mapping[str, Any]]) -> Optional[float]:
    """(现价-60日最低)/(60日最高-60日最低)*100。数据不足或平盘返回 None。"""
    bars = [bar for bar in daily_bars if bar.get("high") is not None and bar.get("low") is not None]
    if len(bars) < 2:
        return None
    high = max(bar["high"] for bar in bars)
    low = min(bar["low"] for bar in bars)
    if high <= low:
        return None
    return round((bars[-1]["close"] - low) / (high - low) * 100, 1)


# ======================== 纯函数：排序 & 开盘确认分类 ========================

def rank_anomalies(candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """异动强度 = 量比 x 涨幅，降序排列。"""
    ranked = [dict(item) for item in sorted(
        candidates,
        key=lambda item: -float(item.get("anomaly_strength") or 0),
    )]
    for index, item in enumerate(ranked, 1):
        item["rank"] = index
    return ranked


def classify_gap(gap_pct: float) -> str:
    """跳空分档：强烈高开>=2% / 高开>=1% / 平开(-1%,1%) / 小幅低开[-3%,-1%] / 低开<-3%。"""
    if gap_pct >= 2.0:
        return "强烈高开"
    if gap_pct >= 1.0:
        return "高开"
    if gap_pct > -1.0:
        return "平开"
    if gap_pct >= -3.0:
        return "小幅低开"
    return "低开"


# ======================== IO: 全A扫描 ========================

def _fetch_universe_with_retry() -> List[Dict[str, Any]]:
    last_error: Optional[BaseException] = None
    for attempt in range(SPOT_RETRIES + 1):
        try:
            df = fetch_a_share_spot()
            return df.to_dict("records")
        except Exception as exc:  # noqa: BLE001 — akshare 直接抛原始异常，非 DataSourceError
            last_error = exc
            if attempt < SPOT_RETRIES:
                time.sleep(0.5 * (2 ** attempt))
    raise DataSourceError("akshare_spot", "全A行情获取失败", last_error)


def _fetch_minute_signals(codes: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """并发拉取分钟数据并算尾盘信号。返回 code -> anomaly dict（仅含算出信号的）。"""
    def _fetch(code: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        rows = fetch_tencent_minute(code, market=_market_of(code))
        return code, compute_tail_anomaly(rows)

    results: Dict[str, Dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=MINUTE_WORKERS) as executor:
        futures = [executor.submit(_fetch, code) for code in codes]
        for future in as_completed(futures):
            try:
                code, signal = future.result()
            except Exception:  # noqa: BLE001
                continue
            if signal:
                results[code] = signal
    return results


def _fetch_60d_positions(codes: Sequence[str]) -> Dict[str, Optional[float]]:
    def _fetch(code: str) -> Tuple[str, Optional[float]]:
        bars = fetch_tencent_kline(code, market=_market_of(code), days=60)
        return code, compute_position_60d_pct(bars)

    results: Dict[str, Optional[float]] = {}
    with ThreadPoolExecutor(max_workers=KLINE_WORKERS) as executor:
        futures = [executor.submit(_fetch, code) for code in codes]
        for future in as_completed(futures):
            try:
                code, position = future.result()
            except Exception:  # noqa: BLE001
                continue
            results[code] = position
    return results


def _build_candidates(
    screened_by_code: Mapping[str, Mapping[str, Any]],
    tail_signals: Mapping[str, Mapping[str, Any]],
    positions: Mapping[str, Optional[float]],
) -> List[Dict[str, Any]]:
    candidates = []
    for code, signal in tail_signals.items():
        if not is_tail_anomaly(signal):
            continue
        position_60d = positions.get(code)
        if position_60d is not None and position_60d >= POSITION_60D_MAX_PCT:
            continue
        base = screened_by_code.get(code, {})
        candidates.append({
            "code": code,
            "name": base.get("name"),
            "pe": base.get("pe"),
            "market_cap": base.get("market_cap"),
            "position_60d_pct": position_60d,
            **signal,
            "anomaly_strength": round(
                signal["tail_volume_ratio"] * signal["tail_price_change_pct"], 2
            ),
        })
    return rank_anomalies(candidates)


def scan(asof: Optional[str] = None) -> Dict[str, Any]:
    asof = asof or date.today().isoformat()
    universe_rows = _fetch_universe_with_retry()
    screened = screen_universe(universe_rows)
    screened_by_code = {item["code"]: item for item in screened}

    tail_signals = _fetch_minute_signals(list(screened_by_code))
    anomaly_codes = [code for code, signal in tail_signals.items() if is_tail_anomaly(signal)]
    positions = _fetch_60d_positions(anomaly_codes)

    candidates = _build_candidates(screened_by_code, tail_signals, positions)
    return {
        "schema": SCHEMA,
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_count": len(universe_rows),
        "screened_count": len(screened),
        "tail_signal_count": len(anomaly_codes),
        "candidates": candidates,
    }


def example_scan(asof: str = "2026-06-30") -> Dict[str, Any]:
    """合成数据演示：直接跑纯计算函数，验证结构，不触网。"""
    screened_by_code = {
        "688213": {"code": "688213", "name": "思特威-W", "pe": 45.0, "market_cap": 8.0e9},
        "600000": {"code": "600000", "name": "平淡股份", "pe": 12.0, "market_cap": 6.0e9},
    }
    tail_signals = {
        "688213": compute_tail_anomaly([
            {"time": "0930", "price": 30.0, "cum_volume": 10000.0},
            {"time": "1429", "price": 31.0, "cum_volume": 700000.0},
            {"time": "1500", "price": 33.17, "cum_volume": 1050000.0},
        ]),
        "600000": compute_tail_anomaly([
            {"time": "0930", "price": 10.0, "cum_volume": 10000.0},
            {"time": "1429", "price": 10.1, "cum_volume": 700000.0},
            {"time": "1500", "price": 10.12, "cum_volume": 720000.0},
        ]),
    }
    positions = {"688213": 45.0, "600000": 30.0}
    candidates = _build_candidates(screened_by_code, tail_signals, positions)
    return {
        "schema": SCHEMA,
        "asof": asof,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_count": len(screened_by_code),
        "screened_count": len(screened_by_code),
        "tail_signal_count": len(candidates),
        "candidates": candidates,
    }


def persist_scan(result: Mapping[str, Any]) -> None:
    atomic_write_json(_dated_path(result["asof"]), dict(result))
    atomic_write_json(_archive_path(), dict(result))


# ======================== IO: 次日开盘确认 ========================

def confirm(archive: Mapping[str, Any], asof: Optional[str] = None) -> Dict[str, Any]:
    asof = asof or date.today().isoformat()
    candidates = list(archive.get("candidates") or [])
    if not candidates:
        return {
            "schema": CONFIRM_SCHEMA,
            "asof": asof,
            "source_asof": archive.get("asof"),
            "status": "no_candidates",
            "confirmations": [],
        }

    codes = [str(item["code"]) for item in candidates if item.get("code")]
    prefixed = [f"{_market_of(code)}{code}" for code in codes]
    try:
        quotes = fetch_tencent_quote(prefixed)
    except DataSourceError as exc:
        return {
            "schema": CONFIRM_SCHEMA,
            "asof": asof,
            "source_asof": archive.get("asof"),
            "status": "insufficient_data",
            "error": str(exc),
            "confirmations": [],
        }

    confirmations = build_confirmations(candidates, quotes)
    return {
        "schema": CONFIRM_SCHEMA,
        "asof": asof,
        "source_asof": archive.get("asof"),
        "status": "ready",
        "confirmations": confirmations,
    }


def build_confirmations(
    candidates: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    """纯函数：候选 + 行情字典 -> 跳空确认列表，高开优先排序。"""
    confirmations = []
    for item in candidates:
        code = str(item.get("code"))
        quote = quotes.get(f"{_market_of(code)}{code}") or {}
        open_price = quote.get("open")
        prev_close = quote.get("prev_close")
        base = {
            "code": code,
            "name": item.get("name"),
            "tail_volume_ratio": item.get("tail_volume_ratio"),
            "tail_price_change_pct": item.get("tail_price_change_pct"),
        }
        if open_price is None or not prev_close:
            confirmations.append({**base, "status": "quote_unavailable"})
            continue
        gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
        confirmations.append({
            **base,
            "open_price": open_price,
            "prev_close": prev_close,
            "gap_pct": gap_pct,
            "gap_bucket": classify_gap(gap_pct),
            "status": "confirmed",
        })
    confirmations.sort(key=lambda row: -(row.get("gap_pct") if row.get("gap_pct") is not None else -999))
    return confirmations


# ======================== 展示 ========================

def format_scan_report(result: Mapping[str, Any]) -> str:
    lines = [f"## 尾盘异动扫描 | {result['asof']}"]
    candidates = result.get("candidates") or []
    if not candidates:
        lines.append(f"- 全A{result.get('universe_count', 0)}只，无标的触发尾盘异动阈值")
        return "\n".join(lines)
    lines.append(
        f"- 全A{result.get('universe_count', 0)}只 -> 粗筛{result.get('screened_count', 0)}只 "
        f"-> 命中{len(candidates)}只"
    )
    for item in candidates:
        lines.append(
            f"- #{item['rank']} {item['name']}({item['code']}): "
            f"量比={item['tail_volume_ratio']}x 尾盘涨幅={item['tail_price_change_pct']}% "
            f"60日位置={item.get('position_60d_pct')}% PE={item.get('pe')} "
            f"异动强度={item['anomaly_strength']}"
        )
    return "\n".join(lines)


def format_confirm_report(result: Mapping[str, Any]) -> str:
    lines = [f"## 尾盘异动次日开盘确认 | {result['asof']} (源: {result.get('source_asof')})"]
    confirmations = result.get("confirmations") or []
    if not confirmations:
        lines.append("- 无待确认标的")
        return "\n".join(lines)
    for item in confirmations:
        if item.get("status") != "confirmed":
            lines.append(f"- {item.get('name')}({item['code']}): 行情不可用")
            continue
        lines.append(
            f"- {item['name']}({item['code']}): {item['gap_bucket']} "
            f"跳空{item['gap_pct']:+.2f}% (开{item['open_price']} / 昨收{item['prev_close']})"
        )
    return "\n".join(lines)


# ======================== CLI ========================

def main() -> None:
    parser = argparse.ArgumentParser(description="尾盘异动扫描器")
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--top", type=int, default=None, help="仅展示前N只（不影响存档）")
    parser.add_argument("--out", help="扫描结果额外写入的文件路径")
    parser.add_argument("--confirm", action="store_true", help="早盘确认模式：读取存档对比今日开盘")
    parser.add_argument("-i", "--input", help="--confirm 模式下指定存档文件（默认读取最新存档）")
    parser.add_argument("--example", action="store_true", help="合成数据演示，不触网")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.confirm:
        archive = read_json(args.input, {}) if args.input else read_json(_archive_path(), {})
        if not archive:
            print(json.dumps({"status": "insufficient_data", "error": "无存档可确认"}, ensure_ascii=False))
            sys.exit(1)
        result = confirm(archive, asof=args.asof)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(format_confirm_report(result))
        return

    if args.example:
        result = example_scan()
    else:
        try:
            result = scan(args.asof)
        except DataSourceError as exc:
            print(json.dumps({"status": "insufficient_data", "error": str(exc)}, ensure_ascii=False))
            sys.exit(1)
        persist_scan(result)
        if args.out:
            atomic_write_json(args.out, dict(result))

    display = dict(result)
    if args.top:
        display["candidates"] = display["candidates"][: args.top]
    if args.json:
        print(json.dumps(display, ensure_ascii=False))
    else:
        print(format_scan_report(display))


if __name__ == "__main__":
    main()
