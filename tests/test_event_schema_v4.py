"""事件表 EVENT_SCHEMA v4 —— 逐字段可得性 / 派生语义 / S1·S2 端到端受益（合成数据，不触网）

本文件守的是一条纪律：**不同来源可得性不同，缺就标 unavailable，绝不用日线代理值伪造**。
每个新字段都必须在 field_availability 里表态；akshare 与 mootdx 两条来源分别断言。
"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "chanlun-backtest" / "scripts"
if str(SCRIPTS) not in sys.path:      # 与 tests/test_rank_surprise.py 同样的兄弟模块加载方式
    sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dat = _load("daban_bt_data", SCRIPTS / "daban_bt_data.py")
mootdx = _load("mootdx_source_v4", ROOT / "skills" / "common" / "mootdx_source.py")
s1 = _load("daban_bt_rank_surprise", SCRIPTS / "daban_bt_rank_surprise.py")
s2 = _load("daban_bt_divergence_reseal", SCRIPTS / "daban_bt_divergence_reseal.py")
import divergence_reseal as dr  # noqa: E402  -- 由上面的适配器 import 把 skills/common 挂上 sys.path

DATE = "2026-06-26"
SECTOR = "合成板块"
# 流通市值 5.5e9 ÷ 收盘 11.0 = 流通股本 5e8 股；日线 volume 20 万手 ×100 股 ÷ 5e8 = 4.00%
FLOAT_MKTCAP = 5.5e9
BASE_VOLUME = 200000
EXPECTED_BASELINE_PCT = 4.0


def _dates(n=30):
    return [f"2026-06-{d:02d}" for d in range(1, n + 1)]


def _kline(t1_open=11.2, t1_close=11.5):
    """30 根日线：前 25 根平稳（换手基准 20 天全部可用），第 26 根(2026-06-26)是涨停日。"""
    bars = []
    for date in _dates()[:25]:
        bars.append({"date": date, "open": 10.0, "high": 10.0, "low": 10.0,
                     "close": 10.0, "volume": BASE_VOLUME})
    bars.append({"date": DATE, "open": 10.5, "high": 11.0, "low": 10.3,
                 "close": 11.0, "volume": BASE_VOLUME})
    bars.append({"date": "2026-06-27", "open": t1_open, "high": max(t1_open, t1_close),
                 "low": min(t1_open, t1_close), "close": t1_close, "volume": BASE_VOLUME})
    for date in ("2026-06-28", "2026-06-29"):
        bars.append({"date": date, "open": t1_close, "high": t1_close + 0.1,
                     "low": t1_close - 0.1, "close": t1_close, "volume": BASE_VOLUME})
    return bars


def _zt_row(code, *, first_seal="093000", last_seal="103200", open_boards=1.0,
            turnover=8.0, sector=SECTOR):
    """东财 stock_zt_pool_em 原始行（含 v3 之前被丢弃的三列）。"""
    return {
        "代码": code, "名称": "合成股", "首次封板时间": first_seal,
        "最后封板时间": last_seal, "炸板次数": open_boards, "换手率": turnover,
        "连板数": 1, "封板资金": 1.0e8, "流通市值": FLOAT_MKTCAP, "所属行业": sector,
    }


# --------------------------------------------------------------------------- #
# 验收 1：逐字段可得性矩阵 —— 有值 / 显式 unavailable，绝不出现伪造值
# --------------------------------------------------------------------------- #
def test_akshare_source_every_v4_field_is_available_or_explicitly_unavailable():
    raw = [dat._map_zt_row(_zt_row(f"60010{i}"), "20260626") for i in range(4)]
    events, _ = dat.assemble_events(raw, {f"60010{i}": _kline() for i in range(4)})
    assert events, "样本非空断言：空集下任何可得性结论都是恒真的假绿"

    matrix = events[0]["field_availability"]
    assert set(matrix) == set(dat.V4_FIELDS), "每个 v4 字段都必须表态，不允许悄悄缺键"
    for field, state in matrix.items():
        assert state.split(":")[0] in {dat.AVAILABLE, dat.UNAVAILABLE, dat.NOT_APPLICABLE}
        if state != dat.AVAILABLE:
            assert ":" in state, f"{field} 的非可用状态必须带原因"
            assert events[0][field] is None, f"{field} 标了 {state} 却还带着值 = 伪造"

    # akshare 能给的：换手率/最后封板时间/炸板次数/回封时刻/板块聚合/换手基准
    for field in ("turnover_pct", "last_seal_time", "open_board_count", "reseal_time",
                  "sector_limitup_count", "sector_one_word_count",
                  "sector_fast_board_count",
                  "turnover_baseline_median", "turnover_baseline_sample_days"):
        assert matrix[field] == dat.AVAILABLE, (field, matrix[field])
    # akshare 也给不了的（要分钟线）：必须 unavailable 且写明原因，不许拿全日口径顶替
    assert matrix["volume_ratio"].startswith(f"{dat.UNAVAILABLE}:needs_intraday_minute_bars")
    assert matrix["pre_reseal_turnover_pct"].startswith(
        f"{dat.UNAVAILABLE}:needs_intraday_minute_bars")


def test_mootdx_source_every_v4_field_unavailable_and_never_fabricated():
    """mootdx 只有 code/date/lianban —— 全部 v4 字段必须 unavailable，一个值都不许造。"""
    raw = [
        mootdx.standardize_event(f"60010{i}", "合成股", {"date": DATE, "lianban": 1})
        for i in range(4)
    ]
    events, _ = dat.assemble_events(raw, {f"60010{i}": _kline() for i in range(4)})
    assert events

    matrix = events[0]["field_availability"]
    assert set(matrix) == set(dat.V4_FIELDS)
    for field in dat.V4_FIELDS:
        assert matrix[field].startswith(dat.UNAVAILABLE), (field, matrix[field])
        assert events[0][field] is None, f"mootdx 路线的 {field} 有值 = 伪造"
    # 原因必须准确：mootdx 缺的是上游字段与行业映射，不是"分钟线"这一种
    assert "sector_missing" in matrix["sector_limitup_count"]
    assert "float_shares_unavailable" in matrix["turnover_baseline_median"]


# --------------------------------------------------------------------------- #
# 验收 2：reseal_time 语义 —— 没炸板就不存在"回封"
# --------------------------------------------------------------------------- #
def test_reseal_time_is_last_seal_when_board_was_opened():
    value, state = dat.derive_reseal_time(
        {"open_board_count": 2.0, "last_seal_time": "104500"})
    assert (value, state) == ("104500", dat.AVAILABLE)


def test_reseal_time_is_none_when_board_never_opened():
    """炸板次数=0 → 不存在回封时刻。把当日最后封板时间当回封是伪造。"""
    value, state = dat.derive_reseal_time(
        {"open_board_count": 0, "last_seal_time": "093000"})
    assert value is None
    assert state == f"{dat.NOT_APPLICABLE}:never_opened_board_no_reseal"


def test_reseal_time_unavailable_when_open_board_count_missing():
    value, state = dat.derive_reseal_time({"last_seal_time": "093000"})
    assert value is None
    assert state == f"{dat.UNAVAILABLE}:open_board_count_missing"


def test_reseal_time_unavailable_when_last_seal_time_missing():
    value, state = dat.derive_reseal_time({"open_board_count": 1, "last_seal_time": None})
    assert value is None
    assert state == f"{dat.UNAVAILABLE}:last_seal_time_missing"


# --------------------------------------------------------------------------- #
# 验收 3：板块横截面聚合
# --------------------------------------------------------------------------- #
def _cross(rows):
    return dat.sector_cross_section(rows)


def test_sector_cross_section_counts_same_sector_same_day():
    rows = [{"code": f"60010{i}", "date": "20260626", "sector": "A", "first_seal": "100000"}
            for i in range(3)]
    rows.append({"code": "600200", "date": "20260626", "sector": "B", "first_seal": "100000"})
    groups = _cross(rows)
    assert groups[(DATE, "A")]["sector_limitup_count"] == 3
    assert groups[(DATE, "B")]["sector_limitup_count"] == 1


def test_sector_cross_section_tiers_one_word_and_fast_board_by_time():
    rows = [
        {"code": "600101", "date": DATE, "sector": "A", "first_seal": "092500"},  # 一字
        {"code": "600102", "date": DATE, "sector": "A", "first_seal": "092000"},  # 一字
        {"code": "600103", "date": DATE, "sector": "A", "first_seal": "093100"},  # 快速板
        {"code": "600104", "date": DATE, "sector": "A", "first_seal": "093200"},  # 都不算
    ]
    bucket = _cross(rows)[(DATE, "A")]
    assert bucket["sector_limitup_count"] == 4
    assert bucket["sector_one_word_count"] == 2       # ≤09:25
    assert bucket["sector_fast_board_count"] == 3     # ≤09:31（含一字）


def test_sector_cross_section_excludes_missing_sector_from_every_bucket():
    """sector 缺失的票不进任何板块，也不能自成一个"未知板块"参与排名。"""
    rows = [
        {"code": "600101", "date": DATE, "sector": "A", "first_seal": "092500"},
        {"code": "600102", "date": DATE, "sector": None, "first_seal": "092500"},
        {"code": "600103", "date": DATE, "sector": "  ", "first_seal": "092500"},
    ]
    groups = _cross(rows)
    assert list(groups) == [(DATE, "A")]
    assert groups[(DATE, "A")]["sector_limitup_count"] == 1
    assert not any(str(sector).strip() in ("", "None", "未知") for _, sector in groups)


def test_missing_sector_event_marks_all_cross_section_fields_unavailable():
    raw = [dat._map_zt_row(_zt_row("600101", sector=None), "20260626")]
    events, _ = dat.assemble_events(raw, {"600101": _kline()})
    matrix = events[0]["field_availability"]
    for field in ("sector_limitup_count", "sector_one_word_count", "sector_fast_board_count"):
        assert matrix[field] == f"{dat.UNAVAILABLE}:sector_missing"
        assert events[0][field] is None


def test_sector_counts_unavailable_when_any_first_seal_unparseable():
    """组内有一条封板时间不可解析 → 一字/快速家数是已知低估值，必须 unavailable 而不是报小数。"""
    rows = [
        {"code": "600101", "date": DATE, "sector": "A", "first_seal": "092500"},
        {"code": "600102", "date": DATE, "sector": "A", "first_seal": None},
    ]
    bucket = _cross(rows)[(DATE, "A")]
    assert bucket["sector_limitup_count"] == 2       # 家数本身仍是事实
    assert bucket["sector_one_word_count"] is None
    assert bucket["sector_fast_board_count"] is None


# --------------------------------------------------------------------------- #
# 验收 4：20 日换手基准 —— 绝不用成交量冒充换手率
# --------------------------------------------------------------------------- #
def test_turnover_baseline_median_from_klines():
    result = dat.turnover_baseline(_kline(), DATE, FLOAT_MKTCAP, 11.0)
    assert result["availability"] == dat.AVAILABLE
    assert result["sample_days"] == 20
    assert abs(result["median"] - EXPECTED_BASELINE_PCT) < 1e-6


def test_turnover_baseline_unavailable_when_sample_days_insufficient():
    short = _kline()[20:]          # 事件日前只剩 5 根
    result = dat.turnover_baseline(short, DATE, FLOAT_MKTCAP, 11.0)
    assert result["median"] is None
    assert result["sample_days"] == 5
    assert "baseline_sample_insufficient" in result["availability"]


def test_turnover_baseline_never_substitutes_volume_for_turnover_rate():
    """成交量巨大但流通股本不可得 → 必须 unavailable，而不是拿 volume 当换手率报个数。"""
    huge = [dict(bar, volume=9.9e9) for bar in _kline()]
    result = dat.turnover_baseline(huge, DATE, None, 11.0)     # 流通市值缺失
    assert result["median"] is None
    assert result["availability"] == f"{dat.UNAVAILABLE}:float_shares_unavailable"
    assert result["sample_days"] is None


def test_event_level_turnover_baseline_unavailable_without_float_mktcap():
    row = _zt_row("600101")
    row.pop("流通市值")
    events, _ = dat.assemble_events([dat._map_zt_row(row, "20260626")],
                                    {"600101": [dict(b, volume=9.9e9) for b in _kline()]})
    event = events[0]
    assert event["turnover_baseline_median"] is None
    assert event["turnover_baseline_sample_days"] is None
    assert event["field_availability"]["turnover_baseline_median"].endswith(
        "float_shares_unavailable")


# --------------------------------------------------------------------------- #
# 验收 6：S1/S2 在合成 v4 事件表上真正命中（此前在真实 v3 表上都是 0 命中）
# --------------------------------------------------------------------------- #
# 板块内 6 只票；目标票 600105：昨日强度最弱（封板最晚→tiebreak 最低）、今日竞价最强、
# 回封最早。first_seal 分档让板块聚合出 2 家一字 / 3 家快速板。
_PEERS = [
    # (code, first_seal, last_seal, open_boards, t1_open)
    ("600100", "092500", None, 0, 11.05),
    ("600101", "092000", "103500", 1, 11.06),
    ("600102", "093100", None, 0, 11.07),
    ("600103", "094500", None, 0, 11.08),
    ("600104", "100000", None, 0, 11.09),
    ("600105", "103000", "103200", 1, 11.60),   # 目标票：竞价最强、回封最早、封板最晚
]
TARGET = "600105"
# 合成表专供、真实管道拿不到的两个字段（都需要分钟线）——这里显式注入，
# 是为了证明"补齐后 S1/S2 确实能命中"，不是宣称真实数据里有。
SYNTHETIC_MINUTE_FIELDS = {
    "volume_ratio": 2.0,                 # S1 条件3：09:45 前量比
    "volume_ratio_source": "synthetic_fixture_intraday_0945",
    "pre_reseal_turnover_pct": 8.0,      # S2 条件4：封板前累计换手（基准 4.0 → 倍数 2.0）
}


def _v4_table():
    raw = [
        dat._map_zt_row(
            _zt_row(code, first_seal=seal, last_seal=last, open_boards=opens), "20260626")
        for code, seal, last, opens, _ in _PEERS
    ]
    klines = {code: _kline(t1_open=t1_open) for code, _, _, _, t1_open in _PEERS}
    events, _ = dat.assemble_events(raw, klines)
    return events


def _with_synthetic_minute_fields(events):
    return [dict(event, **SYNTHETIC_MINUTE_FIELDS) for event in events]


def test_pipeline_v4_table_supplies_three_of_four_s2_evidence_groups():
    """真实管道能补齐的部分：板块聚合 / 回封时刻 / 换手基准；封板前换手仍缺。"""
    events = _v4_table()
    assert len(events) == len(_PEERS)
    target = next(e for e in events if e["code"] == TARGET)
    assert target["sector_limitup_count"] == 6
    assert target["sector_one_word_count"] == 2
    assert target["sector_fast_board_count"] == 3
    assert target["reseal_time"] == "103200"
    assert abs(target["turnover_baseline_median"] - EXPECTED_BASELINE_PCT) < 1e-6
    assert target["turnover_baseline_sample_days"] == 20
    # 管道自己绝不伪造这两个
    assert target["pre_reseal_turnover_pct"] is None and target["volume_ratio"] is None


def test_s2_fires_on_synthetic_v4_table():
    events = _with_synthetic_minute_fields(_v4_table())
    report = s2.run(events, hold_mode="board_overnight")
    assert report["universe_count"] == len(_PEERS), "样本非空断言"
    assert report["signal_count"] >= 1, report["signal_summary"]
    fired = dr.signal_codes(dr.evaluate_universe(s2.event_records(events)))
    assert (TARGET, DATE) in fired, fired
    assert report["filled_count"] >= 1, "命中必须能落到可成交样本上，否则收益仍是 0 条"
    assert report["returns"]["n"] >= 1 and report["returns"]["mean"] is not None


def test_s1_fires_on_synthetic_v4_table():
    events = _with_synthetic_minute_fields(_v4_table())
    state = {"available": True, "dominant_state": "S3"}
    report = s1.run(events, market_state=state, hold_mode="board_overnight")
    assert report["universe_count"] == len(_PEERS), "样本非空断言"
    assert report["signal_count"] >= 1, report["signal_summary"]
    assert report["filled_count"] >= 1
    assert report["returns"]["n"] >= 1 and report["returns"]["mean"] is not None


def test_s1_and_s2_still_zero_without_the_minute_only_fields():
    """不注入分钟线字段（＝真实 v4 表的样子）时必须是 0 命中且 unavailable 有原因。

    这条守的是"本轮补的是数据不是策略"：剩下的缺口要看得见，不能被悄悄补上代理值。
    """
    events = _v4_table()
    s1_report = s1.run(events, market_state={"available": True, "dominant_state": "S3"},
                       hold_mode="board_overnight")
    s2_report = s2.run(events, hold_mode="board_overnight")
    assert s1_report["signal_count"] == 0
    assert "volume_ratio_missing" in s1_report["signal_summary"]["unavailable_reasons"]
    assert s2_report["signal_count"] == 0
    reasons = s2_report["signal_summary"]["unavailable_reasons"]
    assert any("pre_reseal_turnover_missing" in key for key in reasons), reasons


# --------------------------------------------------------------------------- #
# 可得性汇总
# --------------------------------------------------------------------------- #
def test_availability_summary_counts_every_field():
    summary = dat.availability_summary(_v4_table())
    assert set(summary) == set(dat.V4_FIELDS)
    assert summary["reseal_time"][dat.AVAILABLE] == 2
    assert summary["reseal_time"][f"{dat.NOT_APPLICABLE}:never_opened_board_no_reseal"] == 4
    assert sum(summary["volume_ratio"].values()) == len(_PEERS)
