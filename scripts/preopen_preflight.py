#!/usr/bin/env python3
"""开盘前体检 —— 在交易链启动之前把已知的「昨天改了、今天才炸」拦下来。

issue #239 的验收标准之一：开盘前能自动发现任务注册漂移、推送未配置、
模型认证/余额异常和网关端口冲突。

**这里几乎不写新检测逻辑**：仓库里早就有 6 个体检脚本
（config_doctor / state_doctor / provider_doctor / hermes_gateway_doctor /
dual_runtime_audit / datasource_fallback_smoke），manifest 里注册数却是 0。
缺的一直是排期和出口，不是代码。本脚本把其中开盘前真正用得上的几项聚合成
一个入口，跑在 08:05（早于 08:20 的 hot-money-context-backfill）。

三条设计约束，每条都对应一次真实教训：

- **单项失败不能拖垮整份报告**。体检工具本身崩掉等于没有体检，所以每一项都
  各自兜底，失败记成 ``error`` 继续往下走。
- **读不到 ≠ 没问题**。``unavailable`` 一律升成 ``warn``，绝不并入绿色；
  2026-08-18 那次就是「看起来没有告警」被当成了「没有故障」。
- **不发明依赖**。本仓库不直接调用任何模型厂商 API（模型回合发生在
  OpenClaw 网关侧），所以 401/402 与 EADDRINUSE 只能从网关日志里读，
  不能凭空造一个厂商客户端来「探活」。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from typing import Any, Callable

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
try:
    from ._repo_bootstrap import ensure_repo_importable  # type: ignore[import-not-found]
except ImportError:
    from _repo_bootstrap import ensure_repo_importable  # type: ignore[no-redef]

ensure_repo_importable(ROOT)
import skills.common  # noqa: F401,E402  -- puts skills/common on sys.path

from scripts import daily_diagnostics as diagnostics  # noqa: E402
from scripts import state_doctor  # noqa: E402

MANIFEST_PATH = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")

#: 严重度从轻到重。汇总取最重的一项。
SEVERITY = ("ok", "warn", "red")

#: 网关侧的错误类别 → 人话。这些都不是本仓库能修的，但开盘前必须有人知道。
GATEWAY_PATTERNS = {
    "model_auth": ("401", "unauthorized"),
    "model_balance": ("402", "insufficient balance"),
    "port_conflict": ("eaddrinuse", "address already in use"),
}

#: 证据摘录封顶，artifact 要走推送通道，不能被一条巨大的 traceback 撑爆。
MAX_SAMPLES = 5


def _worst(statuses: list[str]) -> str:
    """取最重的严重度；未知状态按最重处理，不给未知留静默通道。"""
    worst = "ok"
    for status in statuses:
        if status not in SEVERITY:
            return "red"
        if SEVERITY.index(status) > SEVERITY.index(worst):
            worst = status
    return worst


def _guard(name: str, title: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """跑一项检查；它自己炸了也只是这一项红，不影响其余项。"""
    try:
        result = dict(fn())
    except Exception as exc:  # noqa: BLE001 - 体检工具崩掉等于没有体检
        return {
            "name": name,
            "title": title,
            "status": "red",
            "reason": f"检查项自身异常: {type(exc).__name__}: {exc}",
        }
    result.setdefault("name", name)
    result.setdefault("title", title)
    result.setdefault("status", "red")
    return result


def _read_manifest(manifest_path: str) -> dict[str, Any]:
    return diagnostics.read_json(manifest_path, {}) or {}


def check_config() -> dict[str, Any]:
    """所有注册配置文件是否仍然合法。改坏一个 JSON，作业要到执行时才炸。"""
    from config_registry import validate_registered_configs

    report = validate_registered_configs()
    ok = report.get("status") == "ok"
    return {
        "status": "ok" if ok else "red",
        "reason": None if ok else "注册配置校验未通过",
        "detail": {"status": report.get("status"), "errors": (report.get("errors") or [])[:MAX_SAMPLES]},
    }


def check_state(runtime: str) -> dict[str, Any]:
    """状态根身份与关键 JSON。身份不一致 = 两台机器写同一份账（split brain）。"""
    report = state_doctor.inspect_state(runtime)
    status = str(report.get("status") or "")
    mapped = {"ok": "ok", "degraded": "warn"}.get(status, "red")
    broken = [
        item["file"] for item in report.get("files") or []
        if item.get("exists") and item.get("valid_json") is False
    ]
    if broken:
        mapped = "red"
    return {
        "status": mapped,
        "reason": None if mapped == "ok" else f"state_doctor: {status}",
        "detail": {
            "state_status": status,
            "split_brain": bool((report.get("split_brain") or {}).get("detected")),
            "invalid_json_files": broken[:MAX_SAMPLES],
        },
    }


def check_registration(manifest_path: str, openclaw_db: str, day: str) -> dict[str, Any]:
    """manifest ↔ OpenClaw 注册漂移。改了没同步注册，只在执行时才炸（#142）。"""
    openclaw = diagnostics.collect_openclaw(openclaw_db, day)
    drift = diagnostics.collect_drift(manifest_path, openclaw)
    status = {"ok": "ok", "drift": "red", "unavailable": "warn"}.get(
        str(drift.get("status")), "red"
    )
    return {
        "status": status,
        "reason": drift.get("reason") or (
            None if status == "ok" else "manifest 与 OpenClaw 注册不一致"
        ),
        "detail": {
            "enabled_count": drift.get("enabled_count"),
            "registered_count": drift.get("registered_count"),
            "missing": (drift.get("missing") or [])[:MAX_SAMPLES],
            "extra": (drift.get("extra") or [])[:MAX_SAMPLES],
            "unavailable_at": drift.get("unavailable_at"),
        },
    }


def check_delivery(manifest_path: str) -> dict[str, Any]:
    """声明了要推送的作业，推送目标必须已配置 —— 否则告警生成了也送不出去。"""
    jobs = _read_manifest(manifest_path).get("jobs") or []
    pushing = sorted(
        str(job.get("id"))
        for job in jobs
        if job.get("enabled") and job.get("deliver") == "feishu_direct"
    )
    configured = bool(os.environ.get("A_STOCK_FEISHU_CHAT_ID"))
    if not pushing:
        return {
            "status": "ok",
            "reason": None,
            "detail": {"feishu_jobs": [], "chat_id_configured": configured},
        }
    return {
        "status": "ok" if configured else "red",
        "reason": None if configured else (
            f"{len(pushing)} 个作业声明 feishu_direct，但 A_STOCK_FEISHU_CHAT_ID 未配置："
            "告警会生成、但送不出去"
        ),
        "detail": {"feishu_jobs": pushing[:MAX_SAMPLES], "chat_id_configured": configured},
    }


def check_auction_sources(*, probe: bool = True) -> dict[str, Any]:
    """竞价链的数据源。

    easy_tdx 是 09:15-09:25 竞价的唯一真源，缺它整条链只能降级；mootdx 是前收
    兜底之一。``probe`` 关掉时只查可导入性 —— 那不是连通性，字段名如实区分。
    """
    detail: dict[str, Any] = {}
    try:
        import easy_tdx  # noqa: F401
        detail["easy_tdx_importable"] = True
    except ImportError:
        detail["easy_tdx_importable"] = False
    try:
        from mootdx_adapter import mootdx_available
        detail["mootdx_importable"] = bool(mootdx_available())
    except ImportError:
        detail["mootdx_importable"] = False

    if probe and detail["easy_tdx_importable"]:
        # 通达信 HQ 服务器盘前不可达不等于开盘也不可达，所以这一项只报 warn。
        try:
            from easy_tdx import MacClient

            with MacClient.from_best_host():
                detail["easy_tdx_reachable"] = True
        except Exception as exc:  # noqa: BLE001 - 第三方 TCP，异常类型不可穷举
            detail["easy_tdx_reachable"] = False
            detail["easy_tdx_error"] = f"{type(exc).__name__}: {exc}"[:200]
    else:
        detail["easy_tdx_reachable"] = None

    if not detail["easy_tdx_importable"]:
        return {
            "status": "red",
            "reason": "easy_tdx 未安装：竞价链没有真源，只能整体降级",
            "detail": detail,
        }
    if detail["easy_tdx_reachable"] is False:
        return {
            "status": "warn",
            "reason": "easy_tdx 盘前连不上通达信服务器（开盘可能恢复，链路有兜底）",
            "detail": detail,
        }
    return {"status": "ok", "reason": None, "detail": detail}


def check_gateway_log(log_dir: str, day: str) -> dict[str, Any]:
    """网关侧的模型认证 / 余额 / 端口冲突。

    本仓库不直接调用模型厂商 API，这些只在 OpenClaw 网关日志里出现，
    所以按错误类别数行数，不凭空造探针。日志默认在 ``/tmp/openclaw``，
    重启即清空 —— 读不到就是 ``warn``，不是绿。
    """
    candidates = [
        os.path.join(log_dir, f"openclaw-{day}.log"),
        os.path.join(log_dir, "openclaw.log"),
    ]
    path = next((item for item in candidates if os.path.exists(item)), None)
    if path is None:
        return {
            "status": "warn",
            "reason": f"未找到网关日志（查过 `{log_dir}`），无法判断认证/余额/端口",
            "detail": {"log_dir": log_dir},
        }
    counts = {name: 0 for name in GATEWAY_PATTERNS}
    samples: dict[str, list[str]] = {name: [] for name in GATEWAY_PATTERNS}
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                low = line.lower()
                for name, needles in GATEWAY_PATTERNS.items():
                    if any(needle in low for needle in needles):
                        counts[name] += 1
                        if len(samples[name]) < 2:
                            samples[name].append(diagnostics.redact(line.strip())[:200])
    except OSError as exc:
        return {
            "status": "warn",
            "reason": f"读取网关日志失败：{exc}",
            "detail": {"log_path": path},
        }
    hit = {name: count for name, count in counts.items() if count}
    return {
        "status": "red" if hit else "ok",
        "reason": (
            "网关日志出现 " + "、".join(f"{name}×{count}" for name, count in hit.items())
            if hit else None
        ),
        "detail": {
            "log_path": path,
            "counts": counts,
            "samples": {name: samples[name] for name in hit},
        },
    }


def run_preflight(
    *,
    day: str,
    runtime: str,
    manifest_path: str,
    openclaw_db: str,
    log_dir: str,
    probe_sources: bool = True,
) -> dict[str, Any]:
    checks = [
        _guard("config", "注册配置校验", check_config),
        _guard("state", "状态根与关键 JSON", lambda: check_state(runtime)),
        _guard(
            "registration",
            "manifest ↔ OpenClaw 注册漂移",
            lambda: check_registration(manifest_path, openclaw_db, day),
        ),
        _guard("delivery", "推送目标可用性", lambda: check_delivery(manifest_path)),
        _guard(
            "auction_sources",
            "竞价数据源",
            lambda: check_auction_sources(probe=probe_sources),
        ),
        _guard(
            "gateway",
            "网关认证/余额/端口",
            lambda: check_gateway_log(log_dir, day),
        ),
    ]
    status = _worst([str(item.get("status")) for item in checks])
    blocking = [item for item in checks if item.get("status") != "ok"]
    return {
        "schema": "a_stock_preopen_preflight_v1",
        "status": status,
        # 全绿时作业静默（silent_when_no_signal），所以这里必须如实反映
        "has_signal": status != "ok",
        "asof": day,
        "runtime": runtime,
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "total": len(checks),
            "red": sum(1 for item in checks if item.get("status") == "red"),
            "warn": sum(1 for item in checks if item.get("status") == "warn"),
        },
        "alerts": [
            {
                "name": item.get("name"),
                "title": item.get("title"),
                "status": item.get("status"),
                "reason": item.get("reason"),
            }
            for item in blocking
        ],
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="开盘前体检（08:05）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（cron 用这个）")
    parser.add_argument("--date", default=dt.date.today().isoformat())
    parser.add_argument(
        "--runtime", choices=["hermes", "openclaw", "local"], default="hermes"
    )
    parser.add_argument("--manifest", default=MANIFEST_PATH)
    parser.add_argument(
        "--openclaw-db", default=os.path.expanduser("~/.openclaw/state/openclaw.sqlite")
    )
    parser.add_argument("--openclaw-log-dir", default="/tmp/openclaw")
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="跳过 easy_tdx 建连探测，只查可导入性",
    )
    args = parser.parse_args(argv)

    report = run_preflight(
        day=args.date,
        runtime=args.runtime,
        manifest_path=args.manifest,
        openclaw_db=os.path.expanduser(args.openclaw_db),
        log_dir=os.path.expanduser(args.openclaw_log_dir),
        probe_sources=not args.no_probe,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 体检发现问题不等于本次运行失败：作业照常终态，由 artifact/推送把红项送出去。
    # 返回非 0 会让 DAG 把它当失败依赖，反而挡住后面的链。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
