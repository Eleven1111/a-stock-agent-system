#!/usr/bin/env python3
"""
深度投研缓存 — Serenity scorecard 回流四维深度面
================================================
serenity-investment-research 产出的 scorecard.json（六维 0-100 评分）与可选的
valuation_scenarios.json（熊/中/牛赔率）经此模块落入共享缓存；four_dim_scorer 的
深度面读取该缓存，把"行业空间/商业模式/竞争格局/财务质量/估值赔率/风险控制"六维
投研结论映射成 0-10 深度分，并按新鲜度衰减——深研一次、日评复用。

定位：解决"深度面 20% 其实只是 PE 分桶"的断点。serenity 是 LLM 重活，不可能每天
对每只票重跑；因此用缓存 + 新鲜度衰减，过期则向 PE 快照回归。

缓存路径：$HERMES_HOME/skills/stock-triage/cache/deep_research/{code}.json
数据源：本地 JSON，无网络调用，cron-safe（纯标准库）。

Usage:
  # 写入（serenity 流程产出 scorecard 后调用）
  python3 deep_research_cache.py write --code 600519 --name 贵州茅台 \
      --scorecard outputs/tongfu/scorecard.json [--valuation outputs/tongfu/valuation.json]
  # 读取（调试）
  python3 deep_research_cache.py read --code 600519 --json
"""

import json
import os
import sys
from datetime import date, datetime
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from paths import cache_dir  # noqa: E402
from state_store import atomic_write_json, read_json  # noqa: E402

# ========== 常量 ==========

DEFAULT_MAX_AGE_DAYS = 90        # 一个季度内视为新鲜
NEUTRAL_SCORE = 5.0              # 深度面中性基准
SCORECARD_DIMS = [
    "industry_space", "business_model", "competition",
    "financial_quality", "valuation_odds", "risk_control",
]


def cache_file(code: str) -> str:
    """某只票的深研缓存文件路径。"""
    code = str(code).zfill(6)
    return os.path.join(cache_dir("stock-triage"), "deep_research", f"{code}.json")


# ========== 纯函数：评分映射（可单测，不触网/不读盘）==========

def _clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def _base_scenario_upside(valuation: Optional[Dict[str, Any]]) -> Optional[float]:
    """从 valuation_scenarios 取中性(base)情景的上行/下行空间%。"""
    if not isinstance(valuation, dict):
        return None
    scenarios = valuation.get("scenarios")
    if not isinstance(scenarios, list):
        return None
    for sc in scenarios:
        if isinstance(sc, dict) and sc.get("scenario") == "base":
            up = sc.get("upside_downside_pct")
            try:
                return float(up) if up is not None else None
            except (TypeError, ValueError):
                return None
    return None


def valuation_adjustment(upside_pct: Optional[float]) -> float:
    """估值赔率温和调整（±0.6 上限）。scorecard 已含 valuation_odds 维度，
    这里只用中性情景上行空间做小幅微调，避免双重计数。"""
    if upside_pct is None:
        return 0.0
    if upside_pct >= 50:
        return 0.6
    if upside_pct >= 20:
        return 0.3
    if upside_pct <= -20:
        return -0.6
    if upside_pct <= -5:
        return -0.3
    return 0.0


def scorecard_to_deep_score(scorecard: Dict[str, Any],
                            valuation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """六维 scorecard(0-100) → 0-10 深度分 + 估值微调。返回明细供透明展示。"""
    total = scorecard.get("total") if isinstance(scorecard, dict) else None
    try:
        total = float(total)
    except (TypeError, ValueError):
        total = None

    if total is None:
        return {"score": None, "base": None, "valuation_adj": 0.0,
                "upside_pct": None, "rating": None, "dimensions": {}}

    base = total / 10.0
    upside = _base_scenario_upside(valuation)
    adj = valuation_adjustment(upside)
    score = round(_clamp(base + adj), 1)

    dims = {}
    raw_dims = scorecard.get("dimensions", {}) if isinstance(scorecard, dict) else {}
    if isinstance(raw_dims, dict):
        for k, v in raw_dims.items():
            if isinstance(v, dict):
                dims[k] = v.get("score_1_to_5")

    return {
        "score": score,
        "base": round(base, 2),
        "valuation_adj": adj,
        "upside_pct": upside,
        "rating": scorecard.get("rating") if isinstance(scorecard, dict) else None,
        "scorecard_total": round(total, 1),
        "dimensions": dims,
    }


def decay_stale_score(deep_score: float, fallback_score: float,
                      age_days: int, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> float:
    """过期衰减：新鲜期内用 deep_score；超期后按超出比例从 deep_score 线性插值回退到
    fallback_score（通常是 PE 快照分），超期满一个周期则完全回退。"""
    if age_days <= max_age_days or max_age_days <= 0:
        return round(deep_score, 1)
    extra = age_days - max_age_days
    t = min(1.0, extra / float(max_age_days))
    return round(deep_score + (fallback_score - deep_score) * t, 1)


def _age_days(asof: str, today: Optional[str] = None) -> Optional[int]:
    try:
        a = datetime.strptime(asof[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    t = date.today()
    if today:
        try:
            t = datetime.strptime(today[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            pass
    return (t - a).days


# ========== 读写 ==========

def write_deep_research(code: str, name: str, scorecard: Dict[str, Any],
                        valuation: Optional[Dict[str, Any]] = None,
                        asof: Optional[str] = None,
                        report_path: Optional[str] = None) -> Dict[str, Any]:
    """把一份 serenity 投研结论落入共享缓存（原子写）。"""
    mapped = scorecard_to_deep_score(scorecard, valuation)
    record = {
        "schema": "deep_research_cache_v1",
        "code": str(code).zfill(6),
        "name": name,
        "asof": asof or date.today().isoformat(),
        "generated_at": datetime.now().isoformat(),
        "deep_score": mapped["score"],
        "rating": mapped["rating"],
        "scorecard_total": mapped.get("scorecard_total"),
        "valuation_upside_pct": mapped["upside_pct"],
        "dimensions": mapped["dimensions"],
        "report_path": report_path,
    }
    atomic_write_json(cache_file(code), record)
    return record


def read_deep_research(code: str, max_age_days: int = DEFAULT_MAX_AGE_DAYS,
                       today: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """读取某只票的深研缓存。

    深研 scorecard 是研究意见，不是可执行风控证据。当前 v1 schema 没有经过
    校验的 hard-risk evidence 绑定，因此无论分数高低都明确标记为不可直接交易。
    日期缺失、非法或未来日期同样按陈旧处理，避免未知新鲜度被误当成新鲜。
    """
    record = read_json(cache_file(code), None)
    if not isinstance(record, dict) or record.get("deep_score") is None:
        return None
    age = _age_days(record.get("asof", ""), today)
    stale = age is None or age < 0 or age > max_age_days
    freshness_qualified = not stale
    return {
        "found": True,
        "code": record.get("code"),
        "name": record.get("name"),
        "asof": record.get("asof"),
        "age_days": age,
        "stale": stale,
        "freshness_qualified": freshness_qualified,
        "freshness_status": "fresh" if freshness_qualified else "stale",
        "deep_score": record.get("deep_score"),
        "rating": record.get("rating"),
        "scorecard_total": record.get("scorecard_total"),
        "valuation_upside_pct": record.get("valuation_upside_pct"),
        "dimensions": record.get("dimensions", {}),
        "report_path": record.get("report_path"),
        # deep_research_cache_v1 does not bind independently verified hard-risk
        # evidence. Keep these fields explicit so downstream consumers fail
        # closed instead of turning an LLM score into a sell instruction.
        "hard_risk_evidence": [],
        "execution_eligible": False,
        "evidence_status": "unbound_score",
    }


# ========== CLI ==========

def _load_json_file(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="深度投研缓存读写")
    sub = parser.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write", help="写入一份 serenity 投研结论")
    w.add_argument("--code", required=True)
    w.add_argument("--name", default="")
    w.add_argument("--scorecard", required=True, help="scorecard.json 路径")
    w.add_argument("--valuation", help="valuation_scenarios.json 路径（可选）")
    w.add_argument("--asof", help="投研基准日 YYYY-MM-DD（默认今天）")
    w.add_argument("--report", help="report.md 路径（仅留档）")

    r = sub.add_parser("read", help="读取某只票的深研缓存")
    r.add_argument("--code", required=True)
    r.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    r.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.cmd == "write":
        scorecard = _load_json_file(args.scorecard)
        valuation = _load_json_file(args.valuation)
        rec = write_deep_research(args.code, args.name, scorecard, valuation,
                                  asof=args.asof, report_path=args.report)
        print(json.dumps({"ok": True, "code": rec["code"], "deep_score": rec["deep_score"],
                          "rating": rec["rating"], "asof": rec["asof"]}, ensure_ascii=False))
        return 0

    rec = read_deep_research(args.code, args.max_age_days)
    if rec is None:
        print(json.dumps({"found": False, "code": str(args.code).zfill(6)}, ensure_ascii=False))
        return 0
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=2))
    else:
        flag = "（过期）" if rec["stale"] else ""
        print(f"{rec['name']}({rec['code']}) 深度分 {rec['deep_score']}/10 "
              f"| {rec['rating']} | 基准日 {rec['asof']} {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
