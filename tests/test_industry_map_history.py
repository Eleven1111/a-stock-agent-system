"""行业归属变更日志 —— PIT 查询的三条不变量。

``industry_map.json`` 是单文件覆盖写的「最新归属向前滚」，用它回放历史等于把
今天的归属倒灌进过去。变更日志要成立，必须同时守住：

1. **早于历史起点 → 不可用**，绝不用首日快照往前铺（否则偏差原样搬家）。
2. **沿用 ≠ 观测**：``refresh`` 会把昨日缓存合并进来补主源缺口，那些条目不得
   产生变更事件，否则一次数据源抖动会被记成一次行业重分类。
3. **只写变化**：同一归属重复观测不追加行，否则日志会随天数线性膨胀。
"""

from __future__ import annotations

import json

import industry_map as im


def _boards():
    return [("BK0438", "食品饮料"), ("BK0447", "银行")]


def _constituents(board_code):
    return {"BK0438": ["600519", "000858"], "BK0447": ["601398", "600036"]}[board_code]


def _refresh(tmp_path, asof, constituents=None):
    return im.refresh(
        asof,
        cache_file=str(tmp_path / "industry_map.json"),
        history_file=str(tmp_path / "industry_map_history.jsonl"),
        boards_fetcher=_boards,
        constituents_fetcher=constituents or _constituents,
        gap_fill=False,
        pace_seconds=0,
    )


def _history(tmp_path, asof):
    return im.history_asof(asof, history_file=str(tmp_path / "industry_map_history.jsonl"))


def test_first_refresh_bootstraps_then_repeat_writes_nothing(tmp_path):
    first = _refresh(tmp_path, "2026-06-23")["history"]
    assert first["bootstrap"] is True
    assert first["appended"] == 4
    assert first["reclassified"] == 0

    second = _refresh(tmp_path, "2026-06-24")["history"]
    assert second["bootstrap"] is False
    assert second["appended"] == 0


def test_query_before_history_start_is_unavailable_not_backfilled(tmp_path):
    _refresh(tmp_path, "2026-06-23")

    earlier = _history(tmp_path, "2026-06-01")
    assert earlier["status"] == "before_history"
    assert earlier["industry_by_code"] == {}
    assert earlier["history_start"] == "2026-06-23"

    same_day = _history(tmp_path, "2026-06-23")
    assert same_day["status"] == "ok"
    assert same_day["industry_by_code"]["600519"] == "食品饮料"


def test_missing_history_reports_missing_rather_than_empty_success(tmp_path):
    result = _history(tmp_path, "2026-06-23")
    assert result["status"] == "missing"
    assert result["industry_by_code"] == {}


def test_reclassification_is_recorded_and_replayed_at_the_right_date(tmp_path):
    _refresh(tmp_path, "2026-06-23")

    def moved(board_code):
        # 600036 从银行挪到食品饮料（构造用，只为验证事件与回放）
        table = {"BK0438": ["600519", "000858", "600036"], "BK0447": ["601398"]}
        return table[board_code]

    second = _refresh(tmp_path, "2026-06-25", constituents=moved)["history"]
    assert second["appended"] == 1
    assert second["reclassified"] == 1

    before = _history(tmp_path, "2026-06-24")["industry_by_code"]
    after = _history(tmp_path, "2026-06-25")["industry_by_code"]
    assert before["600036"] == "银行"
    assert after["600036"] == "食品饮料"


def test_carried_over_entries_never_become_history_events(tmp_path):
    """整块板块失败当天：昨日缓存补上的 601398/600036 是沿用，不是观测。"""
    _refresh(tmp_path, "2026-06-23")

    def bank_down(board_code):
        if board_code == "BK0447":
            raise ConnectionError("down")
        return _constituents(board_code)

    result = _refresh(tmp_path, "2026-06-24", constituents=bank_down)
    assert result["history"]["appended"] == 0
    # 当日快照仍然靠合并保住了银行两只（既有行为不变）
    assert result["industry_by_code"]["601398"] == "银行"

    replayed = _history(tmp_path, "2026-06-24")["industry_by_code"]
    assert replayed["601398"] == "银行"


def test_bootstrap_does_not_claim_observation_of_pre_existing_cache(tmp_path):
    """首日：生产缓存里已有几千条**历史沿用**条目，它们没有在今天被观测过。

    把 merged（沿用 ∪ 观测）当成观测写进 bootstrap，会让日志一上来就声称
    「这些归属是 2026-06-23 那天看到的」—— 而它们可能来自几个月前的一次构建。
    历史的第一天必须只包含当天真的取到的东西。
    """
    cache = tmp_path / "industry_map.json"
    cache.write_text(
        json.dumps(
            {
                "schema": im.SCHEMA,
                "asof": "2026-06-20",
                # 只在旧缓存里存在：今天的两个板块都不包含它
                "industry_by_code": {"300750": "电池", "600519": "食品饮料"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    history = _refresh(tmp_path, "2026-06-23")["history"]
    assert history["bootstrap"] is True
    assert history["appended"] == 4

    replayed = _history(tmp_path, "2026-06-23")["industry_by_code"]
    assert "300750" not in replayed
    assert set(replayed) == {"600519", "000858", "601398", "600036"}


def test_truncated_line_does_not_poison_the_whole_log(tmp_path):
    _refresh(tmp_path, "2026-06-23")
    path = tmp_path / "industry_map_history.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "industry_map_history_v1", "asof": "2026-06-2')

    result = _history(tmp_path, "2026-06-23")
    assert result["status"] == "ok"
    assert len(result["industry_by_code"]) == 4


def test_log_only_grows_by_actual_changes(tmp_path):
    _refresh(tmp_path, "2026-06-23")
    path = tmp_path / "industry_map_history.jsonl"
    after_first = len(path.read_text(encoding="utf-8").splitlines())

    for day in ("2026-06-24", "2026-06-25", "2026-06-26"):
        _refresh(tmp_path, day)

    assert len(path.read_text(encoding="utf-8").splitlines()) == after_first
