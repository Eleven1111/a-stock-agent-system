#!/usr/bin/env python3
"""
资金流向监控 — 北向资金 + 主力/散户 + 板块资金
==============================================
数据源：东方财富 push2 API（需 Hermes agent env 中的 NO_PROXY）
       + 新浪财经北向汇总 + 腾讯实时量价作为替代指标

Usage:
  python3 capital_flow_monitor.py
  python3 capital_flow_monitor.py --json
"""

import json
import sys
import os
from datetime import datetime
from typing import Dict, Any, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'common'))
from a_stock_http import load_hermes_env
from data_provider import fetch_tencent_quote
from eastmoney_intelligence import eastmoney_json
from http_client import DataSourceError, request_json
from market_adapters import (
    fetch_northbound_flow,
    fetch_sector_fund_flow,
    fetch_stock_fund_flow,
)
from provider_contract import health_attempt, observation_error, observation_ok
import delivery_output
import runtime_targets

load_hermes_env()

KNOWN_SECTOR_CODES = {
    "BK0487": "封测",
    "BK0477": "半导体",
    "BK0719": "AI算力",
    "BK0710": "军工航天",
}
SECTOR_CODE_BY_NAME = {name: code for code, name in KNOWN_SECTOR_CODES.items()}

# --- Dynamic sector code resolution (EastMoney API) ---
_sector_code_cache: dict[str, str] = {}  # name -> BK code
_sector_code_cache_loaded = False


def _fetch_sector_code_map() -> dict[str, str]:
    """Fetch EastMoney sector list and build name->code mapping.
    Uses file cache to avoid repeated API calls."""
    global _sector_code_cache_loaded
    if _sector_code_cache_loaded:
        return _sector_code_cache

    cache_path = os.path.join(
        os.path.dirname(__file__), '..', '..', 'data',
        'sector_code_cache.json',
    )
    # Try loading from file cache first
    try:
        with open(cache_path, encoding='utf-8') as f:
            cached = json.load(f)
        if isinstance(cached, dict) and cached:
            _sector_code_cache.update(cached)
            _sector_code_cache_loaded = True
            return _sector_code_cache
    except (OSError, json.JSONDecodeError):
        pass

    # Fetch from resilient board adapter. EastMoney push2 clist is WAF-blocked
    # and only remains inside market_adapters as a degraded last-resort probe.
    try:
        from market_adapters import fetch_industry_boards

        for code, name in fetch_industry_boards():
            code = str(code or "").strip()
            name = str(name or "").strip()
            if code and name:
                _sector_code_cache[name] = code
        # Save to file cache
        if _sector_code_cache:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, 'w', encoding='utf-8') as f:
                    json.dump(_sector_code_cache, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
    except Exception:
        pass
    _sector_code_cache_loaded = True
    return _sector_code_cache


def resolve_sector_code(name: str) -> str | None:
    """Resolve a sector label to its EastMoney BK code.
    1. Exact match in hardcoded dict
    2. Dynamic lookup via EastMoney API (fuzzy prefix match for partial names)
    """
    # Exact match in hardcoded
    code = SECTOR_CODE_BY_NAME.get(name)
    if code:
        return code
    # Dynamic lookup
    dynamic_map = _fetch_sector_code_map()
    # Exact match in dynamic
    code = dynamic_map.get(name)
    if code:
        return code
    # Prefix match: "房地产开" -> "房地产开发"
    for full_name, bk_code in dynamic_map.items():
        if full_name.startswith(name) or name.startswith(full_name):
            return bk_code
    return None


def _market(code: str) -> str:
    return "sh" if code.startswith("6") else "sz"


def load_runtime_stocks() -> list[tuple[str, str, str]]:
    return [
        (target["code"], _market(target["code"]), target["name"])
        for target in runtime_targets.load_stock_targets()
    ]


def load_runtime_sectors() -> tuple[list[tuple[str, str]], list[str]]:
    sectors = []
    unmapped = []
    seen = set()
    for topic in runtime_targets.load_topics():
        label = topic["label"]
        code = SECTOR_CODE_BY_NAME.get(label) or SECTOR_CODE_BY_NAME.get(topic["key"])
        if not code:
            code = resolve_sector_code(label) or resolve_sector_code(topic["key"])
        if not code:
            unmapped.append(label)
            continue
        # Resolve display name: prefer dynamic, fallback to label
        display_name = KNOWN_SECTOR_CODES.get(code) or label
        if code not in seen:
            seen.add(code)
            sectors.append((code, display_name))
    return sectors, unmapped


def fetch_eastmoney(url: str) -> Optional[Dict]:
    """Legacy raw-payload facade retained for existing callers."""
    observation = fetch_eastmoney_observation(url)
    return observation.get("data") or {}


def fetch_eastmoney_observation(url: str) -> Dict[str, Any]:
    """Fetch Eastmoney while preserving transport and validation failures."""
    try:
        return observation_ok(
            "eastmoney",
            eastmoney_json(
                url,
                required_path=("data",),
                required_type=dict,
            ),
        )
    except (DataSourceError, AttributeError, TypeError) as exc:
        return observation_error("eastmoney", exc)


def fetch_sina_northbound() -> Dict:
    """Legacy raw-payload facade retained for existing callers."""
    observation = fetch_sina_northbound_observation()
    return observation.get("data") or {}


def _as_number(value: Any) -> Optional[float]:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_sina_northbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    row = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    net = None
    for key in (
        "net_flow_yi",
        "netFlow",
        "net_flow",
        "netInflow",
        "northMoney",
        "north_money",
    ):
        net = _as_number(row.get(key))
        if net is not None:
            break
    if net is None:
        raise ValueError("Sina northbound payload has no recognized net-flow field")
    unit = str(row.get("unit") or "").lower()
    if unit in {"yuan", "cny", "元"} or abs(net) > 100_000:
        net /= 100_000_000
    return {
        "date": str(row.get("date") or row.get("trade_date") or ""),
        "net_flow_yi": round(net, 1),
    }


def fetch_sina_northbound_observation() -> Dict[str, Any]:
    """Fetch and normalize the independent Sina northbound fallback."""
    try:
        url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow/GetNorthboundFlow"
        result = request_json(
            url,
            source="sina_northbound",
            timeout=10,
            max_attempts=2,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn",
            },
        )
        if not isinstance(result.data, dict):
            raise TypeError("Sina northbound response must be an object")
        return observation_ok("sina", _parse_sina_northbound(result.data))
    except (DataSourceError, AttributeError, TypeError, ValueError) as exc:
        return observation_error("sina", exc)


def _parse_eastmoney_northbound(payload: Dict[str, Any]) -> Dict[str, Any]:
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise ValueError("Eastmoney northbound response has no klines")
    parts = str(klines[-1]).split(",")
    if len(parts) < 2:
        raise ValueError("Eastmoney northbound row is incomplete")
    net = _as_number(parts[1])
    if net is None:
        raise ValueError("Eastmoney northbound value is invalid")
    return {"date": parts[0], "net_flow_yi": round(net / 10000, 1)}


def _parse_eastmoney_flow(payload: Dict[str, Any]) -> Dict[str, float]:
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise ValueError("Eastmoney fund-flow response has no klines")
    parts = str(klines[-1]).split(",")
    if len(parts) < 6:
        raise ValueError("Eastmoney fund-flow row is incomplete")
    main = _as_number(parts[3])
    retail = _as_number(parts[5])
    if main is None or retail is None:
        raise ValueError("Eastmoney fund-flow values are invalid")
    return {
        "main_net_yi": round(main / 10000, 1),
        "retail_net_yi": round(retail / 10000, 1),
    }


def fetch_tencent_flow(code: str, market: str) -> Dict:
    """腾讯实时行情作为量价替代指标"""
    try:
        quote = fetch_tencent_quote(f"{market}{code}")
        return {
            "price": quote.get("price"),
            "change_pct": quote.get("change_pct"),
            "volume": quote.get("volume"),
            "amount": quote.get("amount"),
            "turnover": quote.get("turnover"),
        }
    except (DataSourceError, AttributeError, TypeError):
        return {}


def collect_flow_data(
    stocks: Optional[list[tuple[str, str, str]]] = None,
    sectors: Optional[list[tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """采集资金流向数据"""
    stocks = load_runtime_stocks() if stocks is None else stocks
    if sectors is None:
        sectors, unmapped_sectors = load_runtime_sectors()
    else:
        unmapped_sectors = []
    result = {
        "schema": "capital_flow_v2",
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "northbound": {},
        "stocks": [],
        "sectors": [],
        "unmapped_sectors": unmapped_sectors,
        "alerts": [],
        "source_health": {
            "northbound": {"selected_provider": None, "attempts": []},
            "stock_main_flow": [],
            "sector_main_flow": [],
        },
        "directional_ready": False,
    }
    exact_requested = 1 + len(stocks) + len(sectors)
    exact_available = 0
    degraded = False

    # 1. Northbound flow: AkShare first, allowed Eastmoney kamt endpoint only as fallback.
    # The blocked stock/board push2 paths are no longer on the primary route.
    nb_data = fetch_northbound_flow()
    nb_observation = (
        observation_ok(str(nb_data.get("provider") or "market_adapters"), nb_data)
        if nb_data
        else fetch_sina_northbound_observation()
    )
    result["source_health"]["northbound"]["attempts"].append(health_attempt(nb_observation))
    if nb_observation.get("status") != "ok":
        degraded = True
    if nb_observation.get("status") == "ok":
        selected = str(nb_observation["provider"])
        result["source_health"]["northbound"]["selected_provider"] = selected
        result["northbound"] = {**nb_observation["data"], "provider": selected}
        exact_available += 1
        net = result["northbound"]["net_flow_yi"]
        if net > 50:
            result["alerts"].append({"level": "🟢", "msg": f"北向大幅净流入{net:.0f}亿，看多信号"})
        elif net < -30:
            result["alerts"].append({"level": "🔴", "msg": f"北向大幅净流出{abs(net):.0f}亿，外资撤离信号"})

    # 2. 个股资金流
    for code, market, name in stocks:
        exact_stock_flow = fetch_stock_fund_flow(code, market=market, days=3)
        ff_observation = (
            observation_ok(str(exact_stock_flow.get("provider") or "market_adapters"), exact_stock_flow)
            if exact_stock_flow
            else observation_error("market_adapters", DataSourceError("market_adapters", "stock fund flow unavailable"))
        )

        # 量价替代数据（腾讯）
        qt_data = fetch_tencent_flow(code, market)

        stock_flow = {
            "code": code,
            "name": name,
            "price": qt_data.get("price"),
            "change_pct": qt_data.get("change_pct"),
            "turnover": qt_data.get("turnover"),
            "amount_yi": round(qt_data.get("amount", 0) / 1e8, 1) if qt_data.get("amount") else None,
            "main_flow_status": "unavailable",
            "proxy_metrics": {
                "provider": "tencent",
                "metric_type": "volume_price_proxy",
                "available": bool(qt_data),
            },
        }

        if ff_observation.get("status") == "ok":
            exact_flow = dict(ff_observation["data"])
            stock_flow.update({
                "main_net_yi": exact_flow.get("main_net_yi"),
                "retail_net_yi": exact_flow.get("retail_net_yi"),
            })
            stock_flow["main_flow_status"] = "ok"
            stock_flow["main_flow_provider"] = exact_flow.get("provider")
            exact_available += 1
            main = stock_flow.get("main_net_yi")
            if main is not None and main > 1:
                stock_flow["signal"] = "主力流入"
            elif main is not None and main < -1:
                stock_flow["signal"] = "主力流出"
        if ff_observation.get("status") != "ok":
            degraded = True
        result["source_health"]["stock_main_flow"].append({
            "code": code,
            **health_attempt(ff_observation),
        })

        result["stocks"].append(stock_flow)

        # 放量检测
        turnover = qt_data.get("turnover")
        if turnover and turnover > 15:
            direction = "大涨" if qt_data.get("change_pct", 0) > 3 else "异动"
            result["alerts"].append({
                "level": "🟡",
                "msg": f"{name} 换手率{turnover:.1f}%{direction}，关注资金博弈"
            })

    # 3. 板块资金流
    for bk_code, bk_name in sectors:
        exact_sector_flow = fetch_sector_fund_flow(bk_code, name=bk_name, days=3)
        bk_observation = (
            observation_ok(str(exact_sector_flow.get("provider") or "market_adapters"), exact_sector_flow)
            if exact_sector_flow
            else observation_error("market_adapters", DataSourceError("market_adapters", "sector fund flow unavailable"))
        )
        sector = {"code": bk_code, "name": bk_name, "main_flow_status": "unavailable"}
        if bk_observation.get("status") == "ok":
            exact_flow = dict(bk_observation["data"])
            sector["main_net_yi"] = exact_flow.get("main_net_yi")
            sector["main_flow_status"] = "ok"
            sector["main_flow_provider"] = exact_flow.get("provider")
            exact_available += 1
            if sector["main_net_yi"] is not None and sector["main_net_yi"] > 10:
                result["alerts"].append({
                    "level": "🟢",
                    "msg": f"{bk_name}板块主力净流入{sector['main_net_yi']:.0f}亿，板块级别看多"
                })
            elif sector["main_net_yi"] is not None and sector["main_net_yi"] < -10:
                result["alerts"].append({
                    "level": "🟡",
                    "msg": f"{bk_name}板块主力净流出{abs(sector['main_net_yi']):.0f}亿，注意风险"
                })
        if bk_observation.get("status") != "ok":
            degraded = True
        result["source_health"]["sector_main_flow"].append({
            "code": bk_code,
            **health_attempt(bk_observation),
        })
        result["sectors"].append(sector)

    result["directional_ready"] = exact_available == exact_requested
    if exact_available == 0:
        result["status"] = "insufficient_data"
    elif degraded or exact_available < exact_requested:
        result["status"] = "degraded"
    return result


def format_report(data: Dict) -> str:
    lines = [
        "💰 **资金流向监控**",
        f"⏰ {data['timestamp']}",
        "",
    ]

    nb = data.get("northbound", {})
    if nb:
        flow = nb.get("net_flow_yi", 0)
        direction = "流入" if flow >= 0 else "流出"
        lines.append(f"## 🧭 北向资金：{direction} **{abs(flow):.0f}亿**")
    else:
        lines.append("## 🧭 北向资金：数据暂不可用（非交易时段或网络问题）")

    # 个股
    lines.append("\n## 📊 跟踪标的资金流")
    for s in data.get("stocks", []):
        sig = s.get("signal", "")
        sig_str = f" [{sig}]" if sig else ""
        amount = f"{s.get('amount_yi', 0):.0f}亿" if s.get("amount_yi") else ""
        change = f"{s.get('change_pct', 0):+.2f}%" if s.get('change_pct') is not None else ""
        main = f"主力{s.get('main_net_yi', 0):+.1f}亿" if 'main_net_yi' in s else ""
        lines.append(f"- {s['name']}: {s.get('price','N/A')} {change} {amount} {main}{sig_str}")

    # 板块
    lines.append("\n## 🏭 板块资金")
    for s in data.get("sectors", []):
        main_str = f"主力{s['main_net_yi']:+.1f}亿" if 'main_net_yi' in s else "无数据"
        lines.append(f"- {s['name']}: {main_str}")

    # 警报
    alerts = data.get("alerts", [])
    if alerts:
        lines.append("\n## ⚡ 资金异动")
        for a in alerts:
            lines.append(f"- {a['level']} {a['msg']}")

    return "\n".join(lines)


def has_delivery_anomaly(data: Dict[str, Any]) -> bool:
    status = str(data.get("status") or "ready")
    return status not in {"ready", "degraded"} or bool(data.get("alerts"))


def delivery_summary_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    nb = data.get("northbound") or {}
    net = nb.get("net_flow_yi")
    net_text = "NA" if net is None else f"{net:+.1f}亿"
    return {
        "schema": "delivery_summary_v1",
        "job_id": "capital-flow",
        "status": data.get("status") or "ready",
        "summary": (
            f"资金流 {str(data.get('timestamp') or '')[:10]}："
            f"北向{net_text}；跟踪股{len(data.get('stocks') or [])}；"
            f"板块{len(data.get('sectors') or [])}；无资金异动。"
        ),
        "alerts": [],
    }


def cache_signal_context(data: Dict[str, Any]) -> None:
    """把资金流采集结果落入情绪上下文缓存（northbound/板块/个股主力资金），
    供 four_dim 情绪面消费。失败不阻塞主输出。"""
    try:
        from signal_context import update_signal_context
        partial: Dict[str, Any] = {}
        nb = (data.get("northbound") or {}).get("net_flow_yi")
        if nb is not None:
            partial["northbound_net_yi"] = nb
        sector_flows = {s["name"]: s.get("main_net_yi")
                        for s in data.get("sectors", [])
                        if s.get("name") and s.get("main_net_yi") is not None}
        if sector_flows:
            partial["sector_flows"] = sector_flows
        stock_flows = {str(s["code"]).zfill(6): {"main_net_yi": s.get("main_net_yi")}
                       for s in data.get("stocks", [])
                       if s.get("code") and s.get("main_net_yi") is not None}
        if stock_flows:
            partial["stock_flows"] = stock_flows
        if partial:
            update_signal_context(partial)
    except Exception as e:  # noqa: BLE001
        print(f"signal_context 写入失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--cache", action="store_true",
                   help="把资金流落入情绪上下文缓存，供四维情绪面 overlay")
    p.add_argument("--delivery-job-id", help="启用该 cron job 的无异常摘要投递")
    args = p.parse_args()
    data = collect_flow_data()
    if args.cache:
        cache_signal_context(data)
    if args.json:
        if args.delivery_job_id:
            print(delivery_output.maybe_summarize_json(
                data,
                {**delivery_summary_payload(data), "job_id": args.delivery_job_id},
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(data),
            ))
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        report = format_report(data)
        if args.delivery_job_id:
            print(delivery_output.maybe_summarize_text(
                report,
                delivery_summary_payload(data)["summary"],
                job_id=args.delivery_job_id,
                has_anomaly=has_delivery_anomaly(data),
            ))
        else:
            print(report)
