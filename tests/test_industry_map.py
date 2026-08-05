"""全市场行业映射构建 / 缓存 / 注入 —— 纯逻辑单测（不触网）。"""

import sys
from datetime import date, timedelta

import pytest

import industry_map as im


# ── 注入式假数据源 ───────────────────────────────────────────────

def _boards():
    return [("BK0438", "食品饮料"), ("BK0447", "银行"), ("BK0900", "半导体")]


def _constituents(board_code):
    table = {
        "BK0438": ["600519", "000858"],     # 茅台(SSE) / 五粮液(SZSE)
        "BK0447": ["601398", "600036"],     # 工行 / 招行(均 SSE，原本无行业)
        "BK0900": ["688981", "002371"],     # 中芯国际(科创) / 北方华创
    }
    return table[board_code]


# ── 默认数据源端点 ───────────────────────────────────────────────

def test_default_board_sources_avoid_unreachable_eastmoney():
    """守护：行业映射不得再依赖生产机上不可达的 push2.eastmoney.com。

    2026-06-24 起 industry-map-refresh 每次运行都因该域名不通而全板块失败，
    最终以作业超时收场，缓存文件从未生成。
    """
    assert im._SINA_BOARD_LIST_URLS
    for url in im._SINA_BOARD_LIST_URLS:
        assert "eastmoney" not in url
    assert "eastmoney" not in im._SINA_CONSTITUENT_URL
    assert "eastmoney" not in im._BAIDU_RELATED_BLOCK_URL


def test_sina_board_list_parser_extracts_node_and_name():
    text = (
        'var S_Finance_bankuai_sinaindustry = {"new_blhy":"new_blhy,玻璃行业,19,15.5",'
        '"new_cbzz":"new_cbzz,船舶制造,8,13.8"};'
    )
    assert im.parse_sina_board_list(text) == [("new_blhy", "玻璃行业"), ("new_cbzz", "船舶制造")]


def test_sina_board_list_parser_tolerates_garbage():
    assert im.parse_sina_board_list("not json at all") == []


# ── build_industry_map ───────────────────────────────────────────

def test_build_maps_every_constituent_to_its_board():
    result = im.build_industry_map(
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        pace_seconds=0,
    )
    mapping = result["industry_by_code"]
    assert mapping["600519"] == "食品饮料"
    assert mapping["601398"] == "银行"
    assert mapping["688981"] == "半导体"
    assert result["stock_count"] == 6
    assert result["unit_count"] == 3
    assert result["failed_units"] == []


def test_build_skips_failing_board_without_aborting():
    def flaky(board_code):
        if board_code == "BK0447":
            raise ConnectionError("east rate-limited")
        return _constituents(board_code)

    result = im.build_industry_map(
        boards_fetcher=_boards,
        constituents_fetcher=flaky,
        pace_seconds=0,
    )
    mapping = result["industry_by_code"]
    assert "600519" in mapping and "688981" in mapping  # 其它板块照常
    assert "601398" not in mapping                       # 失败板块缺位
    assert result["failed_units"] == ["BK0447"]


def test_build_first_board_wins_on_duplicate_membership():
    def overlap(board_code):
        return ["600519"] if board_code in ("BK0438", "BK0447") else _constituents(board_code)

    result = im.build_industry_map(
        boards_fetcher=_boards,
        constituents_fetcher=overlap,
        pace_seconds=0,
    )
    # 食品饮料先于银行枚举 → 主行业取食品饮料
    assert result["industry_by_code"]["600519"] == "食品饮料"


# ── enrich_records ───────────────────────────────────────────────

def test_enrich_fills_missing_industry_and_is_immutable():
    records = [
        {"code": "600519", "name": "贵州茅台"},                       # SSE 原本无行业
        {"code": "000858", "name": "五粮液", "industry": "白酒制造"},  # 已有行业
        {"code": "999999", "name": "不在映射"},                       # 映射缺失
    ]
    mapping = {"600519": "食品饮料", "000858": "食品饮料"}
    enriched = im.enrich_records(records, mapping)

    assert enriched[0]["industry"] == "食品饮料"      # 填补 SSE 空缺
    assert enriched[0]["industry_source"] == "resilient_industry_board"
    assert enriched[1]["industry"] == "食品饮料"      # 映射统一口径，覆盖旧值
    assert enriched[1]["industry_source"] == "resilient_industry_board"
    assert enriched[2].get("industry", "") == ""      # 缺失则留空
    assert records[0] == {"code": "600519", "name": "贵州茅台"}  # 原对象不被 mutate


def test_enrich_keeps_existing_when_map_misses():
    records = [{"code": "000858", "industry": "白酒制造"}]
    enriched = im.enrich_records(records, {})
    assert enriched[0]["industry"] == "白酒制造"


def test_enrich_normalizes_prefixed_codes():
    records = [{"code": "sh600519"}]
    enriched = im.enrich_records(records, {"600519": "食品饮料"})
    assert enriched[0]["industry"] == "食品饮料"


# ── 缓存 refresh / load_cached ────────────────────────────────────

def test_refresh_persists_then_load_cached_reads_back(tmp_path):
    cache = tmp_path / "industry_map.json"
    im.refresh(
        "2026-06-24",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )
    loaded = im.load_cached("2026-06-24", cache_file=str(cache), max_age_days=5)
    assert loaded["600519"] == "食品饮料"
    assert len(loaded) == 6


def test_load_cached_returns_empty_when_absent(tmp_path):
    assert im.load_cached(
        "2026-06-24", cache_file=str(tmp_path / "missing.json"), max_age_days=5
    ) == {}


def test_load_cached_rejects_stale_cache(tmp_path):
    cache = tmp_path / "industry_map.json"
    stale_asof = (date.fromisoformat("2026-06-24") - timedelta(days=10)).isoformat()
    im.refresh(
        stale_asof,
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )
    assert im.load_cached("2026-06-24", cache_file=str(cache), max_age_days=5) == {}


def test_refresh_merges_prior_cache_to_absorb_partial_failures(tmp_path):
    cache = tmp_path / "industry_map.json"
    # Day 1: 全量
    im.refresh(
        "2026-06-23",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )

    # Day 2: 银行板块整块失败，但缓存应保留昨日的 601398/600036
    def flaky(board_code):
        if board_code == "BK0447":
            raise ConnectionError("down")
        return _constituents(board_code)

    im.refresh(
        "2026-06-24",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=flaky,
        gap_fill=False,
        pace_seconds=0,
    )
    loaded = im.load_cached("2026-06-24", cache_file=str(cache), max_age_days=5)
    assert loaded["601398"] == "银行"   # 来自昨日缓存合并
    assert loaded["600519"] == "食品饮料"


def test_refresh_keeps_prior_cache_when_build_totally_fails(tmp_path):
    cache = tmp_path / "industry_map.json"
    im.refresh(
        "2026-06-23",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )

    def dead_boards():
        raise ConnectionError("east unreachable")

    result = im.refresh(
        "2026-06-24",
        cache_file=str(cache),
        boards_fetcher=dead_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )
    assert result["status"] == "stale_cache"
    # 仍可用昨日数据（按 5 日有效期）
    loaded = im.load_cached("2026-06-24", cache_file=str(cache), max_age_days=5)
    assert loaded["600519"] == "食品饮料"


# ── 批量直查路径（补口源）───────────────────────────────────────

_UNIVERSE = ["600519", "000858", "601398", "600036", "688981", "002371", "300750"]


def _batch(codes):
    table = {
        "600519": "食品饮料", "000858": "食品饮料", "601398": "银行",
        "600036": "银行", "688981": "电子", "002371": "电子", "300750": "电力设备",
    }
    return {code: table[code] for code in codes if code in table}


def test_batched_build_covers_whole_universe():
    result = im.build_industry_map_batched(
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=_batch,
        batch_size=3,
        pace_seconds=0,
    )
    assert result["mode"] == "batched"
    assert result["industry_by_code"]["688981"] == "电子"
    assert result["stock_count"] == 7
    assert result["universe_count"] == 7
    assert result["unit_count"] == 3          # 7 只 / 每批 3 只 → 3 批
    assert result["failed_units"] == []


def test_batched_build_records_failed_batch_without_aborting():
    def flaky(codes):
        if "601398" in codes:
            raise ConnectionError("rate limited")
        return _batch(codes)

    result = im.build_industry_map_batched(
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=flaky,
        batch_size=3,
        pace_seconds=0,
    )
    assert "688981" in result["industry_by_code"]      # 其它批照常
    assert "601398" not in result["industry_by_code"]   # 失败批（batch#0）整批缺位
    assert "600519" not in result["industry_by_code"]
    assert result["failed_units"] == ["batch#0"]


def test_batched_build_aborts_early_when_source_is_systemically_down():
    """数据源整体不通时必须早停报错，而不是把重试跑满、以作业超时的形态收场。"""
    calls = []

    def dead(codes):
        calls.append(codes)
        raise ConnectionError("host unreachable")

    with pytest.raises(im.IndustrySourceDown, match="系统性故障"):
        im.build_industry_map_batched(
            universe_fetcher=lambda: [f"{i:06d}" for i in range(1000)],
            batch_fetcher=dead,
            batch_size=1,
            pace_seconds=0,
            retry=0,
            max_consecutive_failures=5,
        )
    assert len(calls) == 5   # 早停，没有把 1000 批全部试完


def test_circuit_breaker_keeps_what_it_already_fetched():
    """熔断不得丢掉已经成功取到的批次——那是已经付出的请求。"""
    def dies_after_two(codes):
        if codes[0] in ("000001", "000002"):
            return {codes[0]: "测试行业"}
        raise ConnectionError("host unreachable")

    with pytest.raises(im.IndustrySourceDown) as excinfo:
        im.build_industry_map_batched(
            universe_fetcher=lambda: [f"{i:06d}" for i in range(1, 100)],
            batch_fetcher=dies_after_two,
            batch_size=1,
            pace_seconds=0,
            retry=0,
            max_consecutive_failures=3,
        )
    assert excinfo.value.partial == {"000001": "测试行业", "000002": "测试行业"}


def test_gap_fill_keeps_partial_results_when_the_source_is_cut_off():
    def dies_after_one(codes):
        if "600519" in codes:
            return {"600519": "食品饮料"}
        raise ConnectionError("banned")

    # 首批命中 600519，其后 10 批连续失败 → 触发熔断
    filled, report = im.fill_industry_gaps(
        {},
        universe_fetcher=lambda: ["600519"] + [f"{i:06d}" for i in range(1, 240)],
        batch_fetcher=dies_after_one,
        pace_seconds=0,
        retry=0,
        max_batches=50,
    )
    assert report["status"] == "aborted"
    assert filled == {"600519": "食品饮料"}   # 熔断前取到的照单收下
    assert report["filled"] == 1
    assert "系统性故障" in report["error"]


def test_batched_build_rejects_empty_universe():
    with pytest.raises(RuntimeError, match="代码清单为空"):
        im.build_industry_map_batched(
            universe_fetcher=lambda: [],
            batch_fetcher=_batch,
            pace_seconds=0,
        )


# ── 两段式 refresh：板块主源 + 逐只补口 ──────────────────────────

def test_gap_fill_only_queries_codes_the_boards_missed():
    asked = []

    def batch(codes):
        asked.extend(codes)
        return _batch(codes)

    filled, report = im.fill_industry_gaps(
        {"600519": "食品饮料", "000858": "食品饮料"},
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=batch,
        pace_seconds=0,
    )
    assert "600519" not in asked            # 主源已覆盖的不再重复请求
    assert filled["300750"] == "电力设备"
    assert report["status"] == "ok"
    assert report["missing"] == 5 and report["filled"] == 5


def test_gap_fill_respects_per_run_batch_budget():
    """单轮补口有预算上限，补不完的留给下一个交易日（缓存合并累积覆盖）。"""
    universe = [f"{i:06d}" for i in range(1, 121)]        # 120 只 → 满编 24 批
    asked = []

    def batch(codes):
        asked.extend(codes)
        return {code: "测试行业" for code in codes}

    filled, report = im.fill_industry_gaps(
        {},
        universe_fetcher=lambda: universe,
        batch_fetcher=batch,
        pace_seconds=0,
        max_batches=2,
    )
    assert report["missing"] == 120
    assert report["attempted"] == 40                      # 2 批 × 20 只封顶
    assert len(asked) == 40 and len(filled) == 40


def test_gap_fill_failure_never_blocks_the_primary_source(tmp_path):
    """补口源被封（当下的百度 403）时，主源结果照常落盘。"""
    cache = tmp_path / "industry_map.json"

    def banned(codes):
        raise RuntimeError("ResultCode=403")

    payload = im.refresh(
        "2026-08-04",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=banned,
        pace_seconds=0,
        gap_pace_seconds=0,
    )
    assert payload["status"] == "partial"
    assert payload["gap_fill"]["status"] == "partial"   # 补口整批失败，主源不受影响
    assert payload["gap_fill"]["filled"] == 0
    assert im.load_cached("2026-08-04", cache_file=str(cache))["600519"] == "食品饮料"


def test_refresh_fills_board_gaps_and_reports_coverage(tmp_path):
    cache = tmp_path / "industry_map.json"
    payload = im.refresh(
        "2026-08-04",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=_batch,
        pace_seconds=0,
        gap_pace_seconds=0,
    )
    assert payload["status"] == "ok"
    assert payload["coverage_rate"] == 1.0
    assert payload["gap_fill"]["filled"] == 1          # 板块只差 300750
    assert im.load_cached("2026-08-04", cache_file=str(cache))["300750"] == "电力设备"


def test_refresh_keeps_prior_cache_when_coverage_is_too_low(tmp_path):
    """宁可保留旧缓存，也不用一份残缺映射覆盖它。"""
    cache = tmp_path / "industry_map.json"
    im.refresh(
        "2026-08-03",
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        gap_fill=False,
        pace_seconds=0,
    )
    wide_universe = _UNIVERSE + [f"9{i:05d}" for i in range(100)]
    payload = im.refresh(
        "2026-08-04",
        cache_file=str(cache),
        boards_fetcher=lambda: [("BK0438", "食品饮料")],
        constituents_fetcher=lambda node: ["600519"],
        universe_fetcher=lambda: wide_universe,
        batch_fetcher=lambda codes: {},
        pace_seconds=0,
        gap_pace_seconds=0,
    )
    assert payload["status"] == "stale_cache"
    assert "覆盖率" in payload["error"]
    # 旧缓存未被覆盖
    assert im.load_cached("2026-08-03", cache_file=str(cache))["601398"] == "银行"


# ── 百度关联板块响应解析 ─────────────────────────────────────────

def test_parse_baidu_prefers_sw_level1_over_other_blocks():
    payload = {
        "Result": {
            "000001": [
                {"name": "行业", "list": [
                    {"name": "股份制银行", "describe": "申万二级"},
                    {"name": "银行", "describe": "申万一级"},
                ]},
                {"name": "概念", "list": [{"name": "深圳本地股", "describe": "概念"}]},
            ],
        }
    }
    assert im.parse_baidu_related_blocks(payload) == {"000001": "银行"}


def test_parse_baidu_tolerates_missing_industry_block():
    payload = {"Result": {"000001": [{"name": "概念", "list": [{"name": "AI"}]}]}}
    assert im.parse_baidu_related_blocks(payload) == {}
    assert im.parse_baidu_related_blocks({"ResultCode": "1"}) == {}


# ── 缓存状态诊断（失败可见性）─────────────────────────────────────

def _seed_cache(tmp_path, asof):
    cache = tmp_path / "industry_map.json"
    im.refresh(
        asof,
        cache_file=str(cache),
        boards_fetcher=_boards,
        constituents_fetcher=_constituents,
        universe_fetcher=lambda: _UNIVERSE,
        batch_fetcher=_batch,
        pace_seconds=0,
        gap_pace_seconds=0,
    )
    return str(cache)


def test_status_reports_missing_cache_rather_than_silent_empty(tmp_path):
    status = im.load_cached_status("2026-08-04", cache_file=str(tmp_path / "nope.json"))
    assert status["status"] == "missing"
    assert status["reason"]
    assert status["stock_count"] == 0


def test_status_distinguishes_stale_from_missing(tmp_path):
    cache = _seed_cache(tmp_path, "2026-07-01")
    status = im.load_cached_status("2026-08-04", cache_file=cache, max_age_days=5)
    assert status["status"] == "stale"
    assert status["age_days"] == 34
    assert status["stock_count"] == 7        # 缓存本身有内容，只是过期


def test_status_ok_carries_coverage(tmp_path):
    cache = _seed_cache(tmp_path, "2026-08-04")
    status = im.load_cached_status("2026-08-04", cache_file=cache)
    assert status["status"] == "ok"
    assert status["stock_count"] == 7
    assert status["age_days"] == 0


def test_backfill_accepts_cache_built_after_the_queried_date(tmp_path):
    """回补历史日期：缓存 asof 晚于查询 asof，在容忍窗口内应可用而非静默返回空。"""
    cache = _seed_cache(tmp_path, "2026-08-04")
    assert im.load_cached("2026-07-20", cache_file=cache)["600519"] == "食品饮料"
    assert im.load_cached_status("2026-07-20", cache_file=cache)["status"] == "ok"


def test_backfill_rejects_cache_far_in_the_future(tmp_path):
    cache = _seed_cache(tmp_path, "2026-08-04")
    status = im.load_cached_status("2026-01-01", cache_file=cache, max_future_days=30)
    assert status["status"] == "future"
    assert im.load_cached("2026-01-01", cache_file=cache, max_future_days=30) == {}


# ── CLI 退出码（cron 可见性）──────────────────────────────────────

def test_cli_exits_nonzero_when_refresh_falls_back_to_stale_cache(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["industry_map.py", "--refresh", "--json"])
    monkeypatch.setattr(
        im, "refresh", lambda asof: {"schema": im.SCHEMA, "status": "stale_cache", "error": "源不通"}
    )
    assert im.main() == 1
    assert "stale_cache" in capsys.readouterr().out


def test_cli_exits_zero_on_successful_refresh(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["industry_map.py", "--refresh", "--json"])
    monkeypatch.setattr(im, "refresh", lambda asof: {"status": "ok", "stock_count": 7})
    assert im.main() == 0
