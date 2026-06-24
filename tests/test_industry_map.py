"""全市场行业映射构建 / 缓存 / 注入 —— 纯逻辑单测（不触网）。"""

from datetime import date, timedelta

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
    assert result["board_count"] == 3
    assert result["failed_boards"] == []


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
    assert result["failed_boards"] == ["BK0447"]


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
    assert enriched[1]["industry"] == "食品饮料"      # 映射统一口径，覆盖旧值
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
        pace_seconds=0,
    )

    def dead_boards():
        raise ConnectionError("east unreachable")

    result = im.refresh(
        "2026-06-24",
        cache_file=str(cache),
        boards_fetcher=dead_boards,
        constituents_fetcher=_constituents,
        pace_seconds=0,
    )
    assert result["status"] == "stale_cache"
    # 仍可用昨日数据（按 5 日有效期）
    loaded = im.load_cached("2026-06-24", cache_file=str(cache), max_age_days=5)
    assert loaded["600519"] == "食品饮料"
