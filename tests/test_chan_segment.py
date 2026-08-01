"""chan_segment 单测：特征序列包含处理 / 缺口情形成段 / 未确认尾段的 is_sure 状态迁移 / 入参只读。

差分对齐（与 chan.py oracle 比端点）在 test_chan_segment_diff.py；本文件用手工构造的笔序列
锁定规则本身，失败时能直接指到规则出处。
"""

import copy
import importlib.util
import math
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cs = _load("chan_segment")
ck = _load("chan_kline")
ce = cs.chan_eigen   # 特征序列层（chan_segment 的内部实现，单测直接打到规则上）


def _bis(prices, sure=True):
    """把折点价格序列转成笔列表（相邻折点一笔，方向由涨跌决定），K线索引取等距假值。"""
    return [{"dir": "up" if b > a else "down", "start_idx": i * 5, "end_idx": (i + 1) * 5,
             "start_price": a, "end_price": b, "high": max(a, b), "low": min(a, b),
             "is_sure": sure, "used_to_be_sure": sure}
            for i, (a, b) in enumerate(zip(prices, prices[1:]))]


def _shape(segs):
    return [(s["dir"], s["start_bi_idx"], s["end_bi_idx"], s["is_sure"]) for s in segs]


# 同一形态的两条折点序列，唯一差别：第一/第二特征元素之间是否有缺口。
# 缺口版：第一元素(12,11) 与第二元素(20,18) 不重叠 → Eigen.update_fx 置 gap=True
GAP_PRICES = [10, 12, 11, 20, 18, 19.5, 16, 18.5, 17, 19]
# 无缺口版：第一元素(19,17) 与第二元素(20,18) 重叠 → gap=False
NO_GAP_PRICES = [10, 19, 17, 20, 18, 19.5, 16, 18.5, 17, 19]


# ========== 特征序列包含处理（Combiner/KLine_Combiner.py + Seg/Eigen.py）==========

def test_eigen_merges_bi_contained_by_element():
    """元素包含来笔 → 合并；向上特征序列取"高高"（high/low 双双取大）。"""
    eigen = ce.Eigen(ce.Bi(0, "down", 20.0, 10.0, 20.0, 10.0, True), ce.DIR_UP)
    inner = ce.Bi(2, "down", 18.0, 12.0, 18.0, 12.0, True)
    assert eigen.try_add(inner, exclude_included=True) == ce.COMBINE
    assert (eigen.high, eigen.low) == (20.0, 12.0)
    assert len(eigen.lst) == 2


def test_eigen_included_element_starts_new_element_not_merge():
    """来笔反包元素时，线段特征序列 exclude_included=True → 不合并，另起一个元素
    （这正是线段去包含与K线去包含的关键差异）。"""
    eigen = ce.Eigen(ce.Bi(0, "down", 18.0, 12.0, 18.0, 12.0, True), ce.DIR_UP)
    outer = ce.Bi(2, "down", 20.0, 10.0, 20.0, 10.0, True)
    assert eigen.try_add(outer, exclude_included=True) == ce.INCLUDED
    assert (eigen.high, eigen.low) == (18.0, 12.0)      # 区间未被改写
    assert len(eigen.lst) == 1
    # 普通模式（第三元素判定时用）下同一对关系是合并
    assert ce.Eigen(ce.Bi(0, "down", 18.0, 12.0, 18.0, 12.0, True),
                     ce.DIR_UP).try_add(outer) == ce.COMBINE


def test_eigen_allow_top_equal_breaks_inclusion():
    """被包含但顶部相等时 allow_top_equal=1 判为向下（EigenFX.treat_third_ele 传入）。"""
    eigen = ce.Eigen(ce.Bi(0, "down", 18.0, 14.0, 18.0, 14.0, True), ce.DIR_UP)
    equal_top = ce.Bi(2, "down", 18.0, 10.0, 18.0, 10.0, True)
    assert eigen.try_add(equal_top, allow_top_equal=1) == ce.DIR_DOWN
    assert eigen.try_add(equal_top, allow_top_equal=None) == ce.COMBINE


def test_eigen_marks_gap_between_first_and_second_element():
    """第一元素整体低于第二元素 → 顶分型带缺口（Seg/Eigen.py::update_fx）。"""
    segs_gap = cs.build_segs(_bis(GAP_PRICES[:7]))
    segs_flat = cs.build_segs(_bis(NO_GAP_PRICES[:7]))
    # 形态相同（同样在第 2 笔见顶），差别只在缺口 → 缺口版第一段尚未确认
    assert _shape(segs_gap)[0] == ("up", 0, 2, False)
    assert _shape(segs_flat)[0] == ("up", 0, 2, True)


# ========== 缺口（第二类）情形成段 ==========

def test_gap_seg_confirmed_only_after_revert_fx():
    """缺口情形必须在后续笔中找到反向特征序列分型才确认（EigenFX.can_be_end → find_revert_fx）。
    逐笔喂入：第 6~8 笔阶段该段一直是 is_sure=False，反向分型在第 9 笔成型后才转 True。"""
    states = {n: _shape(cs.build_segs(_bis(GAP_PRICES[:n + 1])))[0] for n in range(3, 10)}
    for n in range(3, 9):
        assert states[n] == ("up", 0, 2, False), f"{n} 笔时第一段不应确认：{states[n]}"
    assert states[9] == ("up", 0, 2, True), f"9 笔时反向分型已成型，应确认：{states[9]}"


def test_no_gap_seg_confirmed_at_fractal():
    """无缺口时不需要反向分型，特征序列分型一成立即确认。"""
    states = {n: _shape(cs.build_segs(_bis(NO_GAP_PRICES[:n + 1])))[0] for n in range(3, 10)}
    assert states[5] == ("up", 0, 2, False)     # 分型尚未成型
    assert states[6] == ("up", 0, 2, True)      # 第三元素到位即确认


# ========== 未确认尾段 ==========

def test_tail_seg_is_not_sure():
    """末尾未被特征序列分型确认的部分由 collect_left_seg 收尾，必须 is_sure=False。"""
    segs = cs.build_segs(_bis(GAP_PRICES))
    assert segs[-1]["is_sure"] is False
    assert [s["is_sure"] for s in segs[:-1]] == [True] * (len(segs) - 1)


def test_unsure_input_bi_blocks_seg_confirmation():
    """分型证据里含未确认笔（is_sure/used_to_be_sure 皆 False）→ 线段不能确认
    （EigenFX.all_bi_is_sure）。"""
    bis = _bis(NO_GAP_PRICES[:7])
    bis[5] = {**bis[5], "is_sure": False, "used_to_be_sure": False}
    assert _shape(cs.build_segs(bis))[0] == ("up", 0, 2, False)
    bis[5] = {**bis[5], "used_to_be_sure": True}          # 曾经确定过 → 仍算证据
    assert _shape(cs.build_segs(bis))[0] == ("up", 0, 2, True)


def test_segs_are_contiguous_and_cover_bis_from_zero():
    """线段首尾相接：段 i+1 的起始笔 = 段 i 的结束笔 +1，且第一段从第 0 笔开始。"""
    prices = [30 + 5 * math.sin(i / 11.0) + 1.2 * math.sin(i / 3.3) for i in range(400)]
    bars = [{"open": round(p, 2), "close": round(p, 2),
             "high": round(p + 0.3, 2), "low": round(p - 0.3, 2)} for p in prices]
    segs = cs.build_segs(ck.build_bis(bars))
    assert segs, "400 根合成K线应至少产出一条线段"
    assert segs[0]["start_bi_idx"] == 0
    for prev, cur in zip(segs, segs[1:]):
        assert cur["start_bi_idx"] == prev["end_bi_idx"] + 1


# ========== 契约与纯函数 ==========

def test_build_segs_does_not_mutate_input():
    bis = _bis(GAP_PRICES)
    snapshot = copy.deepcopy(bis)
    cs.build_segs(bis)
    assert bis == snapshot


def test_seg_fields_point_back_to_bi_endpoints():
    bis = _bis(GAP_PRICES)
    for seg in cs.build_segs(bis):
        start, end = bis[seg["start_bi_idx"]], bis[seg["end_bi_idx"]]
        assert seg["start_idx"] == start["start_idx"]
        assert seg["end_idx"] == end["end_idx"]
        assert seg["start_price"] == start["start_price"]
        assert seg["end_price"] == end["end_price"]
        assert seg["high"] == max(seg["start_price"], seg["end_price"])
        assert seg["low"] == min(seg["start_price"], seg["end_price"])
        assert set(seg) == {"dir", "start_bi_idx", "end_bi_idx", "start_idx", "end_idx",
                            "start_price", "end_price", "high", "low", "is_sure"}


def test_too_few_bis_yield_no_seg():
    assert cs.build_segs([]) == []
    assert cs.build_segs(_bis([10, 12, 11])) == []


def test_analyze_segs_summary():
    result = cs.analyze_segs(_bis(GAP_PRICES))
    assert result["seg_count"] == len(result["segs"])
    assert result["sure_seg_count"] == sum(1 for s in result["segs"] if s["is_sure"])
    assert result["last_seg"] == result["segs"][-1]
    assert cs.analyze_segs([]) == {"segs": [], "seg_count": 0, "sure_seg_count": 0,
                                   "last_seg": None}
