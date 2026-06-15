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
import runtime_targets

load_hermes_env()

KNOWN_SECTOR_CODES = {
    "BK0487": "封测",
    "BK0477": "半导体",
    "BK0719": "AI算力",
    "BK0710": "军工航天",
}
SECTOR_CODE_BY_NAME = {name: code for code, name in KNOWN_SECTOR_CODES.items()}


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
            unmapped.append(label)
            continue
        if code not in seen:
            seen.add(code)
            sectors.append((code, KNOWN_SECTOR_CODES[code]))
    return sectors, unmapped


def fetch_eastmoney(url: str) -> Optional[Dict]:
    """东方财富 API（需要 NO_PROXY）"""
    try:
        return eastmoney_json(
            url,
            required_path=("data",),
            required_type=dict,
        )
    except (DataSourceError, AttributeError, TypeError):
        return {}


def fetch_sina_northbound() -> Dict:
    """新浪财经北向资金汇总"""
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
        return result.data if isinstance(result.data, dict) else {}
    except (DataSourceError, AttributeError, TypeError):
        return {}


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
        "timestamp": datetime.now().isoformat(),
        "northbound": {},
        "stocks": [],
        "sectors": [],
        "unmapped_sectors": unmapped_sectors,
        "alerts": [],
    }

    # 1. 北向资金（东财）
    nb_data = fetch_eastmoney(
        "https://push2his.eastmoney.com/api/qt/kamt.kline/get?"
        "fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54&klt=1&lmt=5&secid=1.000001"
    )
    if nb_data and nb_data.get("data") and nb_data["data"].get("klines"):
        latest = nb_data["data"]["klines"][-1]
        parts = latest.split(",")
        if len(parts) >= 4:
            result["northbound"] = {
                "date": parts[0],
                "net_flow_yi": round(float(parts[1]) / 10000, 1) if parts[1] != "-" else 0,
            }
            net = result["northbound"]["net_flow_yi"]
            if net > 50:
                result["alerts"].append({"level": "🟢", "msg": f"北向大幅净流入{net:.0f}亿，看多信号"})
            elif net < -30:
                result["alerts"].append({"level": "🔴", "msg": f"北向大幅净流出{abs(net):.0f}亿，外资撤离信号"})

    # 2. 个股资金流
    for code, market, name in stocks:
        secid = f"1.{code}" if market == "sh" else f"0.{code}"
        ff_data = fetch_eastmoney(
            f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
            f"fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&lmt=3&secid={secid}"
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
        }

        if ff_data and ff_data.get("data") and ff_data["data"].get("klines"):
            latest_ff = ff_data["data"]["klines"][-1]
            parts = latest_ff.split(",")
            if len(parts) >= 6:
                stock_flow["main_net_yi"] = round(float(parts[3]) / 10000, 1) if parts[3] != "-" else 0
                stock_flow["retail_net_yi"] = round(float(parts[5]) / 10000, 1) if parts[5] != "-" else 0
                main = stock_flow["main_net_yi"]
                if main > 1:
                    stock_flow["signal"] = "主力流入"
                elif main < -1:
                    stock_flow["signal"] = "主力流出"

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
        secid = f"90.{bk_code}"
        bk_data = fetch_eastmoney(
            f"https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
            f"fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56&lmt=3&secid={secid}"
        )
        sector = {"code": bk_code, "name": bk_name}
        if bk_data and bk_data.get("data") and bk_data["data"].get("klines"):
            latest_bk = bk_data["data"]["klines"][-1]
            parts = latest_bk.split(",")
            if len(parts) >= 6:
                sector["main_net_yi"] = round(float(parts[3]) / 10000, 1) if parts[3] != "-" else 0
                if sector["main_net_yi"] > 10:
                    result["alerts"].append({
                        "level": "🟢",
                        "msg": f"{bk_name}板块主力净流入{sector['main_net_yi']:.0f}亿，板块级别看多"
                    })
                elif sector["main_net_yi"] < -10:
                    result["alerts"].append({
                        "level": "🟡",
                        "msg": f"{bk_name}板块主力净流出{abs(sector['main_net_yi']):.0f}亿，注意风险"
                    })
        result["sectors"].append(sector)

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
    args = p.parse_args()
    data = collect_flow_data()
    if args.cache:
        cache_signal_context(data)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_report(data))
