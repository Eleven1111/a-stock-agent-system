"""chan_kline 单测 — 分型 4 档有效性检查 / 虚笔状态迁移 / 入参不可变。

差分对齐（与 chan.py oracle 比对）在 tests/test_chan_kline_diff.py，本文件只做
规则级判别用例：每条断言都能指到 third_party/chan_py_reference/ 的具体规格行。
"""

import copy
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "chan_kline.py"
SPEC = importlib.util.spec_from_file_location("chan_kline", SCRIPT)
ck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ck)


def _zigzag(levels, bars_per_leg):
    """在给定价位之间线性游走，生成走势明确的合成日K（每段严格单调）。"""
    bars, price = [], levels[0]
    for leg_end in levels[1:]:
        step = (leg_end - price) / bars_per_leg
        for _ in range(bars_per_leg):
            nxt = price + step
            bars.append({"high": round(max(price, nxt) + abs(step) * 0.2, 4),
                         "low": round(min(price, nxt) - abs(step) * 0.2, 4),
                         "close": round(nxt, 4), "open": round(price, 4)})
            price = nxt
    return bars


def _klines(specs):
    """按 (high, low) 序列构造合并K线数组；方向按相邻高点走向填充（本组用例不用到方向）。"""
    out = []
    for i, (high, low) in enumerate(specs):
        direction = ck.DIR_UP if i == 0 or high > specs[i - 1][0] else ck.DIR_DOWN
        out.append(ck._new_klc(high, low, i, direction))
    return out


# ========== 分型有效性检查 4 档（规格：KLine/KLine.py::check_fx_valid）==========
#
# 统一场景：合并K线 0..5，index 1 是顶分型，index 4 是候选底（笔的终点候选）。
# 只有 index 3/5（候选点的左右邻居）在各用例间变化 —— 这正是四档方法的分歧所在：
#   strict  看候选点的左右邻居 + 顶分型的左右邻居
#   half    只看候选点的左邻居 + 顶分型的右邻居
#   loss    只看两个分型自身
#   totally 要求顶分型的低点完全高于候选区间的高点（最严）
_FX_TOP_CASES = {
    # 名称: (kl3, kl5, {method: 期望})
    "候选点右侧新高": ((10.5, 9.5), (13.0, 12.0),
                 {"strict": False, "half": True, "loss": True, "totally": False}),
    "候选点左侧偏高": ((11.5, 10.5), (8.5, 7.5),
                 {"strict": True, "half": True, "loss": True, "totally": False}),
    "上下完全分离": ((10.5, 9.5), (8.5, 7.5),
                {"strict": True, "half": True, "loss": True, "totally": True}),
    "候选点左侧越过顶": ((12.5, 11.5), (8.5, 7.5),
                  {"strict": False, "half": False, "loss": True, "totally": False}),
}


def test_fx_check_methods_discriminate():
    for name, (kl3, kl5, expected) in _FX_TOP_CASES.items():
        kl = _klines([(10.0, 9.0), (12.0, 11.0), (11.0, 10.0), kl3, (9.0, 8.0), kl5])
        fx = [ck.FX_NONE, ck.FX_TOP, ck.FX_NONE, ck.FX_NONE, ck.FX_BOTTOM, ck.FX_NONE]
        for method, want in expected.items():
            machine = ck._BiMachine(ck.BiConfig(fx_check=method), kl, fx)
            got = machine._check_fx_valid(1, 4, for_virtual=False)
            assert got is want, f"{name} / fx_check={method}: 期望 {want}，实得 {got}"


def test_fx_check_bottom_side_is_symmetric():
    """底分型分支与顶分型分支镜像（check_fx_valid 的 FX_TYPE.BOTTOM 段）。"""
    kl = _klines([(11.0, 10.0), (9.0, 8.0), (10.0, 9.0), (10.5, 9.5), (12.0, 11.0), (7.0, 6.0)])
    fx = [ck.FX_NONE, ck.FX_BOTTOM, ck.FX_NONE, ck.FX_NONE, ck.FX_TOP, ck.FX_NONE]
    results = {m: ck._BiMachine(ck.BiConfig(fx_check=m), kl, fx)._check_fx_valid(1, 4, False)
               for m in ck.FX_CHECK_METHODS}
    # index 5 的新低把 strict/totally 打掉，half/loss 不看它
    assert results == {"strict": False, "half": True, "loss": True, "totally": False}


def test_unknown_fx_check_is_rejected():
    try:
        ck.build_bis(_zigzag([10.0, 12.0], 6), ck.BiConfig(fx_check="whatever"))
        raise AssertionError("非法 fx_check 应当抛错")
    except ValueError as exc:
        assert "fx_check" in str(exc)


# ========== 虚笔：生成 / 延伸 / 撤销（规格：Bi/BiList.py::try_add_virtual_bi / delete_virtual_bi）==========

_VIRTUAL_BASE = _zigzag([12.0, 10.0, 14.0, 11.0], bars_per_leg=6)
_SPIKE_BAR = {"high": 15.5, "low": 14.8, "close": 15.4, "open": 15.0}


def _snapshot(bars):
    return [(b["dir"], b["start_idx"], b["end_idx"], b["is_sure"]) for b in ck.build_bis(bars)]


def test_virtual_bi_is_created_for_unconfirmed_leg():
    """回撤段尚未走出底分型 → 以 is_sure=False 挂出虚笔（首次可观察时点）。"""
    assert _snapshot(_VIRTUAL_BASE[:14]) == [("up", 6, 11, True)]           # 只有确定笔
    assert _snapshot(_VIRTUAL_BASE[:17]) == [("up", 6, 11, True), ("down", 11, 16, False)]


def test_virtual_bi_extends_with_new_bar():
    """回撤继续创新低 → 虚笔终点顺延，仍未确认。"""
    assert _snapshot(_VIRTUAL_BASE[:18])[-1] == ("down", 11, 17, False)


def test_virtual_bi_is_revoked_when_price_breaks_back():
    """新K线把价格拉回前高之上 → 撤销虚笔，前一笔重新变虚并延伸（笔数减少 1）。"""
    before = _snapshot(_VIRTUAL_BASE[:17])
    after = _snapshot(_VIRTUAL_BASE[:17] + [_SPIKE_BAR])
    assert before == [("up", 6, 11, True), ("down", 11, 16, False)]
    assert after == [("up", 6, 17, False)]


def test_virtual_bi_turns_sure_after_confirmation():
    """回撤走出合法底分型后，虚笔转为确定笔（is_sure=True），终点不再漂移。"""
    bars = _zigzag([12.0, 10.0, 14.0, 11.0, 13.0], bars_per_leg=6)
    assert _snapshot(bars[:19])[-1] == ("down", 11, 17, False)
    assert _snapshot(bars[:20])[-1] == ("down", 11, 17, True)


def test_prefix_is_sure_is_monotonic_observable():
    """point-in-time 语义：任一前缀上的确定笔，是该前缀下"已可观察"的全部结构。"""
    bars = _zigzag([12.0, 10.0, 14.0, 11.0, 13.0, 9.0], bars_per_leg=6)
    full_sure = {(b["dir"], b["start_idx"], b["end_idx"]) for b in ck.build_bis(bars) if b["is_sure"]}
    for cut in range(10, len(bars) + 1):
        prefix_sure = {(b["dir"], b["start_idx"], b["end_idx"])
                       for b in ck.build_bis(bars[:cut]) if b["is_sure"]}
        assert prefix_sure <= full_sure, f"前缀 {cut} 的确定笔不是全量确定笔的子集：{prefix_sure - full_sure}"


# ========== 纯函数边界 ==========

def test_inputs_are_not_mutated():
    bars = _zigzag([12.0, 10.0, 14.0, 11.0, 13.0], bars_per_leg=6)
    original = copy.deepcopy(bars)
    ck.build_bis(bars)
    ck.analyze_klines(bars)
    ck.merge_klines(bars)
    ck.find_fractals(ck.merge_klines(bars))
    assert bars == original


def test_merged_peak_index_points_at_extreme_raw_bar():
    """high_idx/low_idx 必须指向真正贡献极值的原始K线（下游按原始索引取价）。"""
    bars = _zigzag([12.0, 10.0, 14.0, 11.0], bars_per_leg=6)
    for klc in ck.merge_klines(bars):
        assert bars[klc["high_idx"]]["high"] == klc["high"]
        assert bars[klc["low_idx"]]["low"] == klc["low"]


def test_short_series_yields_no_bi():
    assert ck.build_bis([]) == []
    assert ck.build_bis(_zigzag([10.0, 10.4], 2)) == []
