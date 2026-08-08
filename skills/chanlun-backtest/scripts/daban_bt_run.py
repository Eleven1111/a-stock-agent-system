#!/usr/bin/env python3
"""
打板回测 — 顶层串联 + research_gate 对接
==========================================
把数据层事件表喂进引擎算两个假设收益，统计层做 permutation + FDR，
组装 research_state 交给现成 research_gate.py 判上线资格。

MVP 框定：规则（H1 gap 窗口 / H2 9:25 口径）盘前即锁定、无样本内拟合，
因此把 OOS「一次性验证」名额留给后续 2 年正式样本——本 MVP 提交 phase=pre_oos、
oos_run_count=0，闸门预期返回 ready_for_oos；附带的 exploratory 仅为管道验证与预读，
不构成可上线结论。

用法：
  python daban_bt_run.py --build 20260301 20260531 --split 20260501 --json
  python daban_bt_run.py --table event_table.json --split 20260501 --json
"""

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "chanlun-backtest", "scripts"))
import daban_bt_engine as eng  # noqa: E402
import daban_bt_stats as st  # noqa: E402
import research_gate  # noqa: E402
from research_artifact import write_artifact  # noqa: E402
from state_store import atomic_write_json  # noqa: E402

STRATEGY_ID = "daban_auction_factors_mvp"
EVENT_TABLE_SCHEMA = "daban_bt_event_table_v2"


def _norm_date(value: str) -> str:
    """'20260501' → '2026-05-01'，对齐事件表内日期格式，保证 IS/OOS 切分正确。"""
    text = str(value).strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(value)
REQUIRED_CONTROLS = ["non_signal_limitups"]
REQUIRED_TESTS = ["cluster_bootstrap", "paired_sign_flip"]


def fetch_index_benchmark(start_norm: str, end_norm: str, index_code: str = "000300") -> float:
    """沪深300在 [start,end] 区间的日均 close-to-close 收益，作 buy_hold 基准（per-trade 单日可比）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "common"))
    from a_stock_http import fetch_tencent_kline, DataSourceError

    from datetime import date as _date
    y, m, d = (int(x) for x in start_norm.split("-"))
    days = int((_date.today() - _date(y, m, d)).days * 5 / 7) + 30
    try:
        kl = fetch_tencent_kline(index_code, market="sh", days=days)
    except DataSourceError:
        return 0.0
    window = [b for b in kl if start_norm <= str(b["date"]) <= end_norm]
    if len(window) < 2:
        return 0.0
    rets = [window[i]["close"] / window[i - 1]["close"] - 1 for i in range(1, len(window))]
    return float(sum(rets) / len(rets))


def _two_group(name: str, a: List[float], b: List[float], a_label: str, b_label: str,
               n_perm: int) -> Dict[str, Any]:
    perm = st.permutation_test_diff(a, b, n_perm=n_perm)
    return {
        "hypothesis": name,
        a_label: st.summarize(a),
        b_label: st.summarize(b),
        "permutation": perm,
    }


def _oos_h1_validation(oos_events: List[Dict[str, Any]], index_benchmark: float,
                       n_perm: int) -> Dict[str, Any]:
    """OOS 一次性验证 → research_state 增量。仅 H1 gap 假设（A股 T+1 合法持有主检验）。
    signal 与窗口外涨停对照互斥，并先聚合成每日等权组合，再对每日差值做配对符号置换。
    这避免把同日横截面和同一 signal 重复放进 control 后夸大样本量。
    H2 真竞价封需 first_seal=盘口分笔，mootdx 深历史不提供，本次明确标 not_tested。"""
    paired = eng.daily_h1_returns(oos_events, hold_mode="t1_open_next_sellable_close")
    sig = [row["signal_mean"] for row in paired]
    ctrl = [row["control_mean"] for row in paired]
    differences = [a - b for a, b in zip(sig, ctrl)]
    perm = st.sign_flip_test_mean(differences, n_perm=n_perm)
    fdr = st.benjamini_hochberg([perm["p_value"]], q=0.10)
    sig_mean = st.summarize(sig)["mean"]
    ctrl_mean = st.summarize(ctrl)["mean"]
    return {
        "phase": "oos_complete",
        "oos_run_count": 1,
        "permutation_p": perm["p_value"],
        "fdr_p": fdr[0]["adjusted"] if fdr else perm["p_value"],
        "oos_alpha": sig_mean,
        "benchmark_alpha": ctrl_mean,                        # 基线=买全部涨停，非沪深300
        "oos_signal_minus_control": round(sig_mean - ctrl_mean, 6),
        "oos_index_benchmark": index_benchmark,              # 沪深300 仅参考，不进闸门判定
        "oos_sample_count": len(paired),
        "oos_event_count": sum(row["signal_n"] for row in paired),
        "primary_hold_mode": "t1_open_next_sellable_close",
        "cluster_unit": "trading_date",
        "oos_signal_ci": st.cluster_bootstrap_mean(sig, n_boot=2000),
        "oos_excess_ci": st.cluster_bootstrap_mean(differences, n_boot=2000),
        "h2_status": "not_tested_no_first_seal",
    }


def analyze(event_table: Dict[str, Any], split_date: str,
            benchmark_alpha: float = 0.0, n_perm: int = 5000,
            oos_validation: bool = False) -> Dict[str, Any]:
    """纯计算：事件表 → 两假设统计 + FDR + research_gate 判定。不触网。
    oos_validation=True：在 oos_events 上跑 H1 一次性 OOS 验证，填 research_state 交闸门判上线。"""
    events = event_table.get("events", [])
    pools = event_table.get("control_pools", {})
    is_events, oos_events = eng.split_by_date(events, _norm_date(split_date))

    # 两个持有窗口变体并排（report-all-variants）。board_overnight=真打板(含隔夜跳空)为主检验。
    variants: Dict[str, Dict[str, Any]] = {}
    for mode in eng.HOLD_MODES:
        ret = eng.split_returns(events, hold_mode=mode)
        h1 = _two_group("H1_gap_filter", ret["h1"]["signal"], ret["h1"]["control"],
                        "signal", "control", n_perm)
        h1["t_test_signal"] = dict(zip(("t", "p"), st.t_test_vs_zero(ret["h1"]["signal"])))
        h1["bootstrap_ci_signal"] = st.bootstrap_ci_mean(ret["h1"]["signal"])
        h2 = _two_group("H2_auction_vs_intraday", ret["h2"]["auction"], ret["h2"]["intraday"],
                        "auction", "intraday", n_perm)
        variants[mode] = {"h1": h1, "h2": h2}

    primary = variants["t1_open_next_sellable_close"]
    fdr = st.benjamini_hochberg(
        [primary["h1"]["permutation"]["p_value"], primary["h2"]["permutation"]["p_value"]], q=0.10)

    research_state = {
        "asof": event_table.get("end"),
        "strategy_id": STRATEGY_ID,
        "strategy_kind": "event_signal",
        "phase": "pre_oos",
        "rules_locked": True,
        "has_costs": True,
        "reports_all_variants": True,
        "controls": REQUIRED_CONTROLS,
        "required_controls": REQUIRED_CONTROLS,
        "stat_tests": REQUIRED_TESTS,
        "required_stat_tests": REQUIRED_TESTS,
        "oos_run_count": 0,
        "changed_after_oos": False,
    }
    if oos_validation:
        research_state.update(_oos_h1_validation(oos_events, benchmark_alpha, n_perm))
    gate = research_gate.evaluate_gate(research_state)

    return {
        "schema": "daban_bt_run_v1",
        "generated_at": datetime.now().isoformat(),
        "disclaimer": ("MVP 管道验证与预读，非可上线结论；OOS 名额保留给 2 年正式样本。"
                       "正式主检验使用次日开盘买、至少隔一交易日卖的 T+1 合法口径并按交易日聚类；"
                       "board_overnight 与 open_close 仅作诊断。"
                       "benchmark_alpha 默认 0 仅占位，正式 OOS 必须换真实指数收益，否则 alpha 混入大盘 beta。"
                       "日线无法识别所有 T+1 一字板，仍需更细粒度成交数据复核。"),
        "sample": {
            "raw_count": event_table.get("raw_count"),
            "event_count": event_table.get("event_count", len(events)),
            "is_count": len(is_events),
            "oos_count": len(oos_events),
            "dropped": event_table.get("dropped"),
            "coverage": event_table.get("coverage"),
        },
        "exploratory": {
            "primary_hold_mode": "t1_open_next_sellable_close",
            "variants": variants,
            "fdr": fdr,
            "controls": {
                "buy_hold_benchmark_alpha": benchmark_alpha,
                "random_entry": st.summarize(pools.get("random_entry", [])),
                "simple_breakout": st.summarize(pools.get("simple_breakout", [])),
            },
        },
        "research_state": research_state,
        "gate_result": gate,
    }


def persist_evidence(
    result: Dict[str, Any],
    *,
    event_table: Dict[str, Any],
    input_path: str,
    artifact_path: str,
    split_date: str,
) -> Dict[str, Any]:
    """Bind one OOS result to its exact input table before gate evaluation."""
    if event_table.get("schema") != EVENT_TABLE_SCHEMA:
        raise ValueError(
            f"formal OOS requires {EVENT_TABLE_SCHEMA}; rebuild the legacy event table"
        )
    if result.get("research_state", {}).get("phase") != "oos_complete":
        raise ValueError("persist_evidence requires an oos_complete result")
    oos_events = eng.split_by_date(event_table.get("events", []), _norm_date(split_date))[1]
    paired = eng.daily_h1_returns(oos_events, hold_mode="t1_open_next_sellable_close")
    control_counts = {
        "non_signal_limitups": sum(row["control_n"] for row in paired),
    }
    state = dict(result["research_state"])
    metrics = {
        field: state.get(field)
        for field in (
            "permutation_p",
            "fdr_p",
            "oos_alpha",
            "benchmark_alpha",
            "oos_sample_count",
        )
    }
    rules = {
        "strategy_id": STRATEGY_ID,
        "split_date": _norm_date(split_date),
        "gap_window": list(eng.GAP_WINDOW),
        "primary_hold_mode": "t1_open_next_sellable_close",
        "cluster_unit": "trading_date",
        "cost": dict(eng.DEFAULT_COST),
    }
    artifact = write_artifact(
        os.path.abspath(os.path.expanduser(artifact_path)),
        input_path=input_path,
        strategy_id=STRATEGY_ID,
        rules=rules,
        result=result,
        gate_metrics=metrics,
        control_counts=control_counts,
    )
    state.update({
        "evidence_artifact": os.path.abspath(os.path.expanduser(artifact_path)),
        "evidence_sha256": artifact["artifact_sha256"],
    })
    output = dict(result)
    output["research_state"] = state
    output["gate_result"] = research_gate.evaluate_gate(state)
    output["evidence"] = {
        "artifact": state["evidence_artifact"],
        "sha256": state["evidence_sha256"],
    }
    return output


def format_report(r: Dict[str, Any]) -> str:
    lines = [f"## 打板竞价因子回测（MVP）| {r['research_state']['asof']}"]
    cov = r["sample"].get("coverage") or {}
    if cov.get("warning"):
        lines += [cov["warning"], ""]
    lines += [
        f"样本：涨停事件 {r['sample']['event_count']} 丢弃 {r['sample']['dropped']}"
        + (f" | 覆盖 {cov.get('covered_trading_days')}/{cov.get('expected_trading_days')} 交易日"
           f"（{cov.get('covered_first')}~{cov.get('covered_last')}）" if cov else ""),
        f"闸门结论：{r['gate_result']['decision']}（可供实时引用：{r['gate_result']['allowed_in_live_agent']}）",
    ]
    for mode, v in r["exploratory"]["variants"].items():
        tag = "★真打板(含隔夜跳空)" if mode == "board_overnight" else "保守(切跳空)"
        h1, h2 = v["h1"], v["h2"]
        lines += [
            "",
            f"### [{mode} {tag}]",
            f"  H1 signal n={h1['signal']['n']} 均={h1['signal']['mean']:.4f} 胜率={h1['signal']['win_rate']:.2f}"
            f" | control n={h1['control']['n']} 均={h1['control']['mean']:.4f} | perm p={h1['permutation']['p_value']:.4f}",
            f"  H2 auction n={h2['auction']['n']} 均={h2['auction']['mean']:.4f}"
            f" | intraday n={h2['intraday']['n']} 均={h2['intraday']['mean']:.4f} | perm p={h2['permutation']['p_value']:.4f}",
        ]
    lines += [
        "",
        f"FDR(q=0.10, 主检验=board_overnight) reject: {[o['reject'] for o in r['exploratory']['fdr']]}",
        f"⚠️ {r['disclaimer']}",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="打板竞价因子回测顶层")
    parser.add_argument("--build", nargs=2, metavar=("START", "END"), help="构建事件表，如 20260301 20260531")
    parser.add_argument("--table", help="直接读已构建事件表 JSON")
    parser.add_argument("--split", required=True, help="IS/OOS 切分日 YYYYMMDD")
    parser.add_argument("--source", choices=["akshare", "mootdx"], default="akshare",
                        help="事件源：akshare(默认,免费近3-4周) / mootdx(通达信深历史6年+,仅H1可验)")
    parser.add_argument("--oos", action="store_true",
                        help="正式 OOS 一次性验证(phase=oos_complete)；跑后禁止看结果改规则")
    parser.add_argument("--artifact", help="OOS 研究产物输出路径；--oos 时必需")
    parser.add_argument("--benchmark-alpha", type=float, default=0.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.oos and not args.artifact:
        parser.error("--oos requires --artifact so the gate can verify evidence")

    if args.table:
        with open(args.table, encoding="utf-8") as f:
            table = json.load(f)
    elif args.build:
        import daban_bt_data as dat
        table = dat.build_event_table(args.build[0], args.build[1], source=args.source)
    else:
        parser.error("需 --build 或 --table 之一")
    if args.oos and table.get("schema") != EVENT_TABLE_SCHEMA:
        parser.error(
            f"formal OOS requires {EVENT_TABLE_SCHEMA}; rebuild the legacy event table"
        )

    benchmark = args.benchmark_alpha
    bench_end = args.build[1] if args.build else table.get("end")
    if benchmark == 0.0 and bench_end:
        benchmark = fetch_index_benchmark(_norm_date(args.split), _norm_date(bench_end))

    result = analyze(table, split_date=args.split, benchmark_alpha=benchmark,
                     oos_validation=args.oos)
    if args.oos:
        input_path = args.table
        if not input_path:
            input_path = args.artifact + ".input.json"
            atomic_write_json(input_path, table)
        result = persist_evidence(
            result,
            event_table=table,
            input_path=input_path,
            artifact_path=args.artifact,
            split_date=args.split,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else format_report(result))


if __name__ == "__main__":
    main()
