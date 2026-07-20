#!/usr/bin/env python3
"""Cron 分发器 — 由 launchd 每 60 秒唤醒，触发 manifest 中到期的作业。

launchd 只当心跳；所有 cron 匹配、去重与分发在这里，因此本模块是纯脚本、
可单测、不依赖调度器语义。

设计要点：
- **同分钟去重**：launchd 的 60 秒间隔会漂移，同一分钟可能被唤醒两次。
  作业以 "job_id -> YYYY-MM-DDTHH:MM" 记账，同一分钟只认领一次。
- **单作业故障隔离**：一个作业的表达式写坏或启动失败，不得连累同一分钟
  的其他作业——调度器停摆是静默的，比单个作业失败严重得多。
- **脱离进程组**：子任务用 start_new_session 派生，launchd 回收 dispatcher
  时不会连带杀掉正在跑的作业。
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
COMMON = os.path.join(ROOT, "skills", "common")
MANIFEST = os.path.join(ROOT, "cron", "hermes-cron-manifest.json")
DEFAULT_TZ = "Asia/Shanghai"
# 状态文件保留的作业条目上限（远大于 manifest 作业数即可，仅防无限增长）
MAX_STATE_ENTRIES = 20


def parse_field(expr: str, lo: int, hi: int) -> set[int]:
    """解析单个 cron 字段，返回命中的整数集合。非法输入抛 ValueError。"""
    text = str(expr).strip()
    if not text:
        raise ValueError("empty cron field")
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise ValueError(f"invalid step: {raw_step!r}")
            step = int(raw_step)
        if part == "*":
            start, end = lo, hi
        elif "-" in part.lstrip("-"):
            raw_start, _, raw_end = part.partition("-")
            start, end = _int_in_range(raw_start, lo, hi), _int_in_range(raw_end, lo, hi)
        else:
            start = end = _int_in_range(part, lo, hi)
        if start > end:
            raise ValueError(f"inverted range: {part!r}")
        values.update(range(start, end + 1, step))
    return values


def _int_in_range(raw: str, lo: int, hi: int) -> int:
    text = raw.strip()
    if not text.isdigit():
        raise ValueError(f"non-numeric cron value: {raw!r}")
    value = int(text)
    if not lo <= value <= hi:
        raise ValueError(f"cron value {value} out of range [{lo},{hi}]")
    return value


def cron_matches(schedule: str, moment: datetime) -> bool:
    """标准 5 字段 cron 是否在该分钟触发。表达式非法时抛 ValueError。"""
    fields = str(schedule).split()
    if len(fields) != 5:
        raise ValueError(f"expected 5 cron fields, got {len(fields)}: {schedule!r}")
    minute, hour, dom, month, dow = fields
    # 周日在 cron 里既是 0 也是 7；Python weekday() 周一=0，需转成 cron 口径
    weekday = (moment.weekday() + 1) % 7
    dow_values = parse_field(dow, 0, 7)
    if 7 in dow_values:
        dow_values.add(0)
    return (
        moment.minute in parse_field(minute, 0, 59)
        and moment.hour in parse_field(hour, 0, 23)
        and moment.day in parse_field(dom, 1, 31)
        and moment.month in parse_field(month, 1, 12)
        and weekday in dow_values
    )


def due_jobs(manifest: Mapping[str, Any], moment: datetime) -> List[Dict[str, Any]]:
    """该分钟到期且已启用的作业。单个表达式非法只跳过该作业。"""
    due: List[Dict[str, Any]] = []
    for job in manifest.get("jobs") or []:
        if not job.get("enabled"):
            continue
        try:
            if cron_matches(job.get("schedule") or "", moment):
                due.append(dict(job))
        except ValueError as exc:
            _log(f"skip job {job.get('id')!r}: bad schedule ({exc})")
    return due


def claim(state_path: str, job_id: str, moment: datetime) -> bool:
    """认领该作业在该分钟的触发权；已被认领过返回 False。

    状态文件损坏时按"未认领"处理并重建——宁可某个作业重跑一次，
    也不能因为一个坏文件让整个调度静默停摆。
    """
    stamp = moment.strftime("%Y-%m-%dT%H:%M")
    state: Dict[str, str] = {}
    try:
        with open(state_path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            state = {str(k): str(v) for k, v in loaded.items()}
    except (OSError, ValueError):
        state = {}
    if state.get(job_id) == stamp:
        return False
    state[job_id] = stamp
    if len(state) > MAX_STATE_ENTRIES:
        keep = sorted(state.items(), key=lambda kv: kv[1], reverse=True)[:MAX_STATE_ENTRIES]
        state = dict(keep)
    try:
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        tmp = f"{state_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp, state_path)
    except OSError as exc:
        _log(f"warn: cannot persist dispatch state: {exc}")
    return True


def launch(job: Mapping[str, Any], *, log_path: str) -> Optional[int]:
    """派生作业子进程（脱离进程组）。返回 pid；启动失败返回 None。"""
    command = str(job.get("command") or "").strip()
    if not command:
        _log(f"skip job {job.get('id')!r}: empty command")
        return None
    cwd = os.path.abspath(os.path.join(ROOT, str(job.get("cwd") or ".")))
    try:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        handle = open(log_path, "a", encoding="utf-8")
    except OSError as exc:
        _log(f"warn: cannot open job log {log_path}: {exc}")
        handle = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(  # noqa: S602 — manifest 是仓库内受控配置
            command,
            shell=True,
            cwd=cwd,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": COMMON},
        )
        return proc.pid
    except (OSError, ValueError) as exc:
        _log(f"error: failed to launch {job.get('id')!r}: {exc}")
        return None


def _log(message: str) -> None:
    print(f"[cron-dispatch {datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def _state_home() -> str:
    """状态根目录。

    刻意不 import skills/common/paths.py：dispatcher 是启动一切的组件，
    任何仓库内导入失败都会让整个调度静默停摆。这里复刻 paths.hermes_home()
    的解析顺序（A_STOCK_STATE_HOME > HERMES_HOME > ~/.hermes）；该顺序若变更，
    两处需同步。
    """
    return (
        os.environ.get("A_STOCK_STATE_HOME")
        or os.environ.get("HERMES_HOME")
        or os.path.expanduser("~/.hermes")
    )


def main() -> int:
    try:
        with open(MANIFEST, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        _log(f"fatal: cannot read manifest {MANIFEST}: {exc}")
        return 1
    tz = ZoneInfo(str(manifest.get("timezone") or DEFAULT_TZ))
    now = datetime.now(tz)
    jobs = due_jobs(manifest, now)
    if not jobs:
        return 0
    cron_dir = os.path.join(_state_home(), "cron")
    state_path = os.path.join(cron_dir, "dispatch_state.json")
    log_path = os.path.join(cron_dir, "dispatch-jobs.log")
    for job in jobs:
        job_id = str(job.get("id") or "")
        if not job_id or not claim(state_path, job_id, now):
            continue
        pid = launch(job, log_path=log_path)
        if pid is not None:
            _log(f"launched {job_id} pid={pid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
