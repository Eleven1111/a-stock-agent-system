#!/usr/bin/env python3
"""弱市扭转预警（regime_reversal_watch）— 影子预警，research_only。

读取已产出的竞价/梯队/资金流产物，按 4 个客观因子评估"弱市扭转迹象"：
  F1 竞价一字板/顶格高开集群   （auction_shortlist factors）
  F2 昨日涨停梯队竞价承接率    （signal_ctx 梯队 ∩ 竞价因子）
  F3 主线板块涨停集群 + 承接   （sector_limitups + 梯队红盘方向证据）
  F4 全市场竞价高开广度        （竞价因子分布）

只输出预警文本与影子日志：不改任何门禁/评分，不产生执行候选。
数据缺失的因子 fail-closed 记为不可用，全部缺失时输出"证据不足"。
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
from datetime import date, datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from paths import data_file  # noqa: E402
import yaml  # noqa: E402

CONFIG_PATH = os.path.join(ROOT, "config", "regime_reversal.yaml")
SHADOW_LOG = ("stock-triage", "regime_reversal_watch.jsonl")
_SHORTLIST = ("daban-stock-picker", "auction_shortlist_latest.json")

DEFAULTS: dict = {
    "f1_one_word_min": 5,
    "f1_gap_tolerance": 0.15,
    "f2_red_ratio_min": 0.6,
    "f2_coverage_min": 3,
    "f3_cluster_limitup_min": 3,
    "f4_gap_up_ratio_min": 0.55,
    "f4_min_coverage": 100,
    "strong_min_factors": 3,
    "watch_min_factors": 2,
    "shadow_until": "2026-09-24",
}

STATE_HOME = os.environ.get("A_STOCK_STATE_HOME", "/Users/eleven/.hermes")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as fh:
            data = yaml.safe_load(fh) or {}
        if isinstance(data, dict):
            for key, value in data.items():
                if key != "schema" and value is not None:
                    cfg[key] = value
    except (OSError, UnicodeError, yaml.YAMLError):
        pass
    return cfg


def _trading_date() -> str:
    env = os.environ.get("HERMES_TRADING_DATE", "").strip()
    return env or date.today().isoformat()


def _read_json(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _shortlist(asof: str):
    payload = _read_json(data_file(*_SHORTLIST))
    if not isinstance(payload, dict):
        return None
    if str(payload.get("asof") or "")[:10] != asof:
        return None  # same-day discipline：过期短名单视同缺数据
    if payload.get("status") != "ready":
        return None
    factors = payload.get("factors")
    return factors if isinstance(factors, list) else None


def _signal_ctx():
    try:
        from signal_context import read_signal_context

        ctx = read_signal_context() or {}
        return ctx if isinstance(ctx, dict) else {}
    except (ImportError, OSError, RuntimeError, TimeoutError):
        return {}


def _capflow_sectors() -> dict:
    snaps = sorted(
        glob.glob(
            os.path.join(STATE_HOME, "market", "snapshots", "*", "capital-flow", "snap-*.json")
        ),
        key=os.path.getmtime,
    )
    if not snaps:
        return {}
    payload = _read_json(snaps[-1])
    payload = (payload.get("payload") if isinstance(payload, dict) else None) or payload
    sectors = (payload or {}).get("sectors")
    out: dict = {}
    for item in sectors or []:
        if isinstance(item, dict) and item.get("name") is not None:
            value = item.get("main_net_yi")
            out[str(item["name"])] = value if isinstance(value, (int, float)) else None
    return out


def _checkpoint(asof: str):
    pattern = os.path.join(
        STATE_HOME, "cron", "output", "hot-money-morning-checkpoint", f"*{asof.replace('-', '')}*.json"
    )
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not files:
        return None
    payload = _read_json(files[-1])
    if isinstance(payload, dict) and str(payload.get("stdout", "")).strip().startswith("{"):
        return json.loads(payload["stdout"])
    return payload


def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _factor(fid: str, name: str, *, available: bool, triggered: bool, detail: str) -> dict:
    return {"id": fid, "name": name, "available": available,
            "triggered": triggered if available else False, "detail": detail}


def _f1(factors, cfg) -> dict:
    tol = float(cfg["f1_gap_tolerance"])
    count = 0
    for item in factors:
        gap = _num(item.get("auction_gap_pct"))
        limit_up = _num(item.get("limit_up"))
        prev = _num(item.get("prev_close"))
        if gap is None or not limit_up or not prev or prev <= 0:
            continue
        gap_to_limit = (limit_up - prev) / prev * 100.0
        if gap >= gap_to_limit - tol:
            count += 1
    need = int(cfg["f1_one_word_min"])
    return _factor("F1", "一字板/顶格高开集群", available=True,
                   triggered=count >= need, detail=f"{count} 家 ≥{need}")


def _f2(factors, ladder, cfg) -> dict:
    need = int(cfg["f2_coverage_min"])
    ladder_codes = {str(code) for code in (ladder or {})}
    gaps = []
    for item in factors:
        code = str(item.get("code") or "")
        if code in ladder_codes:
            gap = _num(item.get("auction_gap_pct"))
            if gap is not None:
                gaps.append(gap)
    if len(gaps) < need:
        return _factor("F2", "梯队竞价承接率", available=False,
                       triggered=False, detail=f"覆盖不足 {len(gaps)}/{need}")
    red = sum(1 for gap in gaps if gap > 0)
    ratio = red / len(gaps)
    ok = ratio >= float(cfg["f2_red_ratio_min"])
    return _factor("F2", "梯队竞价承接率", available=True, triggered=ok,
                   detail=f"{red}/{len(gaps)} 红盘（{ratio:.0%}）")


def _f3(sector_limitups, ladder, capflow_sectors, factors, cfg) -> dict:
    need = int(cfg["f3_cluster_limitup_min"])
    clusters = {
        str(sector): int(count)
        for sector, count in (sector_limitups or {}).items()
        if isinstance(count, (int, float)) and count >= 2
    }
    total = sum(clusters.values())
    if total < need or not clusters:
        return _factor("F3", "主线板块共振", available=True, triggered=False,
                       detail=f"昨日板块集群 {total} 板 <{need}")
    # 承接方向证据：集群板块内、梯队红盘
    cluster_names = set(clusters)
    red_by_sector: dict = {}
    for item in factors:
        code = str(item.get("code") or "")
        meta = (ladder or {}).get(code) or {}
        sector = str(meta.get("sector") or "")
        gap = _num(item.get("auction_gap_pct"))
        if sector in cluster_names and gap is not None:
            red_by_sector.setdefault(sector, [0, 0])
            red_by_sector[sector][0 if gap > 0 else 1] += 1
    confirmed = any(red > 0 for red, _ in red_by_sector.values())
    # 资金方向为信息性补充：THS 板块名与东财资金口径可能不一致，未匹配记"方向未知"
    flow_bits = []
    for sector in cluster_names:
        for name, value in capflow_sectors.items():
            if value is not None and (sector in str(name) or str(name) in sector):
                flow_bits.append(f"{name}{value:+.1f}亿")
                break
    flow_text = "；".join(flow_bits[:2]) if flow_bits else "方向未知"
    return _factor("F3", "主线板块共振", available=True, triggered=confirmed,
                   detail=f"集群 {total} 板｜承接 {len(red_by_sector)} 板块｜资金{flow_text}")


def _f4(factors, cfg) -> dict:
    gaps = [g for item in factors if (g := _num(item.get("auction_gap_pct"))) is not None]
    if len(gaps) < int(cfg["f4_min_coverage"]):
        return _factor("F4", "高开广度", available=False, triggered=False,
                       detail=f"样本不足 {len(gaps)}")
    ratio = sum(1 for gap in gaps if gap > 0) / len(gaps)
    ok = ratio >= float(cfg["f4_gap_up_ratio_min"])
    return _factor("F4", "高开广度", available=True, triggered=ok,
                   detail=f"{ratio:.1%} ≥{float(cfg['f4_gap_up_ratio_min']):.0%}")


def evaluate(factors, ladder, sector_limitups, capflow_sectors, checkpoint,
             asof: str, stage: str, cfg: dict) -> dict:
    if factors is None:
        missing = "竞价短名单缺失或过期"
        results = [
            _factor("F1", "一字板/顶格高开集群", available=False, triggered=False,
                    detail=missing),
            _factor("F2", "梯队竞价承接率", available=False, triggered=False,
                    detail=missing),
            _factor("F3", "主线板块共振", available=False, triggered=False,
                    detail=missing),
            _factor("F4", "高开广度", available=False, triggered=False,
                    detail=missing),
        ]
    else:
        results = [
            _f1(factors, cfg),
            _f2(factors, ladder, cfg),
            _f3(sector_limitups, ladder, capflow_sectors, factors, cfg),
            _f4(factors, cfg),
        ]
    triggered = [r for r in results if r["available"] and r["triggered"]]
    count = len(triggered)
    if count >= int(cfg["strong_min_factors"]):
        level = "strong"
    elif count >= int(cfg["watch_min_factors"]):
        level = "watch"
    else:
        level = "silent"
    confirm_note = None
    if stage == "confirm":
        cp = checkpoint if isinstance(checkpoint, dict) else {}
        confirmed = cp.get("confirmed_count")
        if isinstance(confirmed, (int, float)) and confirmed >= 1:
            confirm_note = f"承接确认 {int(confirmed)} 只"
    return {
        "schema": "regime_reversal_watch_v1",
        "asof": asof,
        "stage": stage,
        "level": level,
        "triggered_count": count,
        "factors": results,
        "confirm_note": confirm_note,
        "research_only": True,
    }


def render(result: dict, cfg: dict) -> str:
    level = result["level"]
    if level == "silent":
        return ""
    icon = "🔴" if level == "strong" else "🟡"
    label = "弱市扭转预警（强信号" if level == "strong" else "弱市扭转观察（"
    head = f"{icon} {label}{result['triggered_count']}/4）| {result['asof']} {result['stage']}"
    lines = [head]
    for item in result["factors"]:
        mark = "✅" if item["available"] and item["triggered"] else (
            "❌" if item["available"] else "➖不可用"
        )
        lines.append(f"- {item['id']} {item['name']}: {item['detail']} {mark}")
    if result.get("confirm_note"):
        lines.append(f"- 承接: {result['confirm_note']}")
    tail = "research_only｜预警不构成操作指令｜弱市解除以明日盘前重算为准"
    until = str(cfg.get("shadow_until") or "")
    if until and date.today().isoformat() <= until:
        tail += f"｜校准期至 {until}"
    lines.append(tail)
    return "\n".join(lines)[:600]


def _append_shadow(result: dict) -> None:
    entry = {
        "schema": "regime_reversal_watch_shadow_v1",
        "asof": result["asof"],
        "stage": result["stage"],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "level": result["level"],
        "triggered_count": result["triggered_count"],
        "factors": [
            {k: v for k, v in item.items() if k != "name"}
            for item in result["factors"]
        ],
        "shadow": True,
    }
    path = data_file(*SHADOW_LOG)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=["auction", "confirm"], default="auction")
    parser.add_argument("--asof", default="today")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    asof = _trading_date() if args.asof == "today" else args.asof
    factors = _shortlist(asof)
    ctx = _signal_ctx()
    ladder = ctx.get("lianban_ladder") or {}
    sector_limitups = ctx.get("sector_limitups") or {}
    checkpoint = _checkpoint(asof) if args.stage == "confirm" else None

    result = evaluate(
        factors, ladder, sector_limitups, _capflow_sectors(), checkpoint,
        asof=asof, stage=args.stage, cfg=cfg,
    )
    _append_shadow(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    text = render(result, cfg)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())