"""板块拥挤度日产物的端到端契约 —— 生产者必须真的产出过东西。

本仓有过一次「作业已注册、进程在跑、缓存文件存在，但从未产出过正确结果」的事故
（台账 F001）。所以这份产物的守卫不是「函数能调用」，而是把真实的本地缓存
（SQLite 日线 + 行业归属缓存）喂进去，跑完整条 CLI 路径，看它是否给出数字。

同样重要的是三条 fail-closed 路径都能被区分：行业归属不可用 → blocked、
日线缓存为空 → blocked，两者的 reason 不同；分位样本不足 → 产物照落但判不可得。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import industry_map
import local_market_history as history

ROOT = Path(__file__).resolve().parents[1]


def _load_cli():
    """按路径加载 cron 脚本（与其它 cron 脚本测试同一套装载方式）；脚本自身的
    ``_repo_bootstrap`` 需要 ``scripts/`` 在 sys.path 上。"""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    path = ROOT / "scripts" / "sector_crowding_daily.py"
    spec = importlib.util.spec_from_file_location("sector_crowding_daily", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bar(code, trading_date, amount, turn=1.0):
    return {
        "code": code,
        "trading_date": trading_date,
        "adjust_flag": "qfq",
        "open": 10.0,
        "high": 10.5,
        "low": 9.5,
        "close": 10.0,
        "preclose": 9.8,
        "volume": 1000,
        "amount": amount,
        "turn": turn,
        "pct_chg": 2.0,
        "source": "fixture",
        "source_version": "v1",
    }


def _sessions(count):
    # 只需要单调递增且可排序的日期串；交易日历不参与本模块。
    return [f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}" for index in range(count)]


def _seed(tmp_path, monkeypatch, *, sessions, hot_last_day=True):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.ensure_schema()

    codes = [f"{600000 + index}" for index in range(12)]
    membership = {code: ("半导体" if index < 6 else "银行") for index, code in enumerate(codes)}

    rows = []
    for day_index, trading_date in enumerate(sessions):
        last = hot_last_day and day_index == len(sessions) - 1
        for index, code in enumerate(codes):
            if index < 6:
                # 最后一天让半导体又放量又高度集中（一只吃掉大部分成交）
                amount = (9000.0 if index == 0 else 200.0) if last else 500.0
                turn = 6.0 if last else 1.0
            else:
                amount = 500.0
                turn = 1.0
            rows.append(_bar(code, trading_date, amount, turn))
    history.upsert_daily_bars(rows)

    cache = Path(industry_map._default_cache_file())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {
                "schema": industry_map.SCHEMA,
                "asof": sessions[-1],
                "industry_by_code": membership,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return sessions[-1]


def test_end_to_end_run_produces_a_crowding_score(tmp_path, monkeypatch):
    asof = _seed(tmp_path, monkeypatch, sessions=_sessions(70))
    payload = _load_cli().run(asof=asof, write=False)

    assert payload["status"] == "ok"
    assert payload["evidence_qualification"] == "exploratory_reconstruction"
    assert payload["live_effect"] == "none"

    scored = {row["sector"]: row for row in payload["sectors"] if row["status"] == "ok"}
    assert "半导体" in scored, payload
    # 最后一天既放量又集中 -> 分位应当落在高位，且状态机给出限制档
    assert scored["半导体"]["score"] > 90
    assert scored["半导体"]["state"] in {"NO_ADD", "EXIT_RISK"}
    assert scored["半导体"]["allow_new_entry"] is False
    # 对照组：银行每天都一样，不该被判拥挤
    assert scored["银行"]["score"] < scored["半导体"]["score"]


def test_short_cache_still_writes_an_artifact_but_scores_nothing(tmp_path, monkeypatch):
    asof = _seed(tmp_path, monkeypatch, sessions=_sessions(10))
    payload = _load_cli().run(asof=asof, write=False)

    assert payload["scored_count"] == 0
    assert payload["unavailable_count"] == payload["sector_count"] > 0
    assert all(row["status"] == "unavailable" for row in payload["sectors"])


def test_missing_industry_map_blocks_with_its_own_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.ensure_schema()
    payload = _load_cli().run(asof="2026-09-02", write=False)
    assert payload["status"] == "blocked"
    assert "行业归属" in payload["reason"]


def test_empty_daily_cache_blocks_with_a_different_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    history.ensure_schema()
    cache = Path(industry_map._default_cache_file())
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {"schema": industry_map.SCHEMA, "asof": "2026-09-02",
             "industry_by_code": {"600000": "银行"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload = _load_cli().run(asof="2026-09-02", write=False)
    assert payload["status"] == "blocked"
    assert "日线缓存" in payload["reason"]


def test_artifact_is_written_when_requested(tmp_path, monkeypatch):
    asof = _seed(tmp_path, monkeypatch, sessions=_sessions(70))
    module = _load_cli()
    payload = module.run(asof=asof, write=True)
    written = json.loads(Path(payload["artifact"]).read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert written["live_effect"] == "none"
