#!/usr/bin/env python3
"""
缠论线段层之一 — 特征序列与特征序列分型
=======================================
线段层的底半部：把笔序列压成特征序列元素（Eigen），再在元素上做分型判定（EigenFX）。
上半部（线段列表、左侧收尾、对外契约）在同目录 chan_segment.py —— 拆两个文件是为了each
文件落在 200~400 行的可维护区间，与参照实现的 Seg/Eigen.py + Seg/EigenFX.py 与
Seg/SegList*.py 的分法一致。**唯一的对外入口是 chan_segment.py**，本文件是其内部实现。

算法规格对齐 Vespa314/chan.py（pinned 429d6ed，MIT）：参照实现快照在仓库 third_party 目录下，
**仅测试可 import**（生产引用会被 tests/test_chan_reference_guard.py 静态拦截），故本文件只
复刻算法、不引用它；注释里的 `X.py::f` 均指该快照中的规格出处。

要点：
- 特征序列元素由**与线段方向相反**的笔构成，元素间按包含关系合并；exclude_included=True 时
  "被反包"不合并而是另起元素——这是线段去包含与K线去包含的关键差异；
- 三元素成顶/底分型即线段结束候选；第一、第二元素之间有缺口（gap=True，"第二类情形"）时，
  必须在后续笔里找到反向特征序列分型才算破坏原线段（find_revert_fx）；
- actual_break 处理"第二元素因为合并导致后面没有实际突破"，证据不足时把 actual_break_flag
  置 False，线段只能以 is_sure=False 输出。

纯函数边界：本文件的可变状态只存活于 chan_segment 的一次调用内，Bi 为不可变投影。
"""

from dataclasses import dataclass
from typing import List, Optional, Sequence

DIR_UP, DIR_DOWN = "up", "down"
FX_TOP, FX_BOTTOM, FX_NONE = "top", "bottom", ""
COMBINE, INCLUDED = "combine", "included"


@dataclass(frozen=True)
class Bi:
    """笔的只读投影。sure = chan.py 的 `is_used_to_be_sure`（曾经确定过也算证据）。"""

    idx: int
    dir: str
    high: float
    low: float
    begin_val: float
    end_val: float
    sure: bool


def revert(direction: str) -> str:
    return DIR_DOWN if direction == DIR_UP else DIR_UP


# ========== 特征序列元素（Seg/Eigen.py + Combiner/KLine_Combiner.py 的笔特化）==========

class Eigen:
    """一个特征序列元素：若干同向笔按包含关系合并后的 (high, low) 区间。"""

    def __init__(self, bi: Bi, direction: str):
        self.high, self.low = bi.high, bi.low
        self.dir = direction
        self.lst: List[Bi] = [bi]
        self.fx = FX_NONE
        self.gap = False

    def _test_combine(self, bi: Bi, exclude_included: bool, allow_top_equal: Optional[int]) -> str:
        """KLine_Combiner.test_combine。allow_top_equal=1/-1：被反包但顶/底相等时不算包含。"""
        if self.high >= bi.high and self.low <= bi.low:
            return COMBINE
        if self.high <= bi.high and self.low >= bi.low:
            if allow_top_equal == 1 and self.high == bi.high and self.low > bi.low:
                return DIR_DOWN
            if allow_top_equal == -1 and self.low == bi.low and self.high < bi.high:
                return DIR_UP
            return INCLUDED if exclude_included else COMBINE
        if self.high > bi.high and self.low > bi.low:
            return DIR_DOWN
        if self.high < bi.high and self.low < bi.low:
            return DIR_UP
        raise ValueError(f"特征序列包含关系无法判定: eigen={self.high}/{self.low} bi={bi.high}/{bi.low}")

    def try_add(self, bi: Bi, exclude_included: bool = False,
                allow_top_equal: Optional[int] = None) -> str:
        """合并成功返回 combine（就地更新区间），否则返回 up/down/included。"""
        direction = self._test_combine(bi, exclude_included, allow_top_equal)
        if direction != COMBINE:
            return direction
        self.lst.append(bi)
        flat = bi.high == bi.low        # 一字笔特判（KLine_Combiner.try_add）
        if self.dir == DIR_UP:
            if not flat or bi.high != self.high:
                self.high, self.low = max(self.high, bi.high), max(self.low, bi.low)
        elif not flat or bi.low != self.low:
            self.high, self.low = min(self.high, bi.high), min(self.low, bi.low)
        return COMBINE

    def update_fx(self, pre: "Eigen", nxt: "Eigen", allow_top_equal: Optional[int]) -> None:
        """特征序列分型（KLine_Combiner.update_fx 的 exclude_included 分支——线段层恒为 True）
        + 缺口标记（Eigen.update_fx）。"""
        if pre.high < self.high and nxt.high <= self.high and nxt.low < self.low:
            if allow_top_equal == 1 or nxt.high < self.high:
                self.fx = FX_TOP
        elif nxt.high > self.high and pre.low > self.low and nxt.low >= self.low:
            if allow_top_equal == -1 or nxt.low > self.low:
                self.fx = FX_BOTTOM
        if (self.fx == FX_TOP and pre.high < self.low) or (self.fx == FX_BOTTOM and pre.low > self.high):
            self.gap = True

    def peak_bi_idx(self) -> int:
        """分型元素里取到极值的**最后**一笔的前一笔 = 线段终点笔（Eigen.GetPeakBiIdx）。"""
        is_high = self.lst[0].dir == DIR_DOWN   # 上升线段的特征序列由下降笔构成
        target = self.high if is_high else self.low
        for bi in reversed(self.lst):
            if (bi.high if is_high else bi.low) == target:
                return bi.idx - 1
        raise ValueError("特征序列元素里找不到极值笔")


# ========== 特征序列分型状态机（Seg/EigenFX.py）==========

class EigenFX:
    """线段方向 direction 的特征序列分型探测器：逐笔喂入反向笔，add() 返回是否出现分型。"""

    def __init__(self, direction: str, bis: Sequence[Bi]):
        self.dir = direction
        self.bis = bis                  # 全量笔，只读；用于取 next / next.next 证据
        self.ele: List[Optional[Eigen]] = [None, None, None]
        self.lst: List[Bi] = []
        self.last_evidence_bi_is_sure = False
        self.actual_break_flag = True

    def _up(self) -> bool:
        return self.dir == DIR_UP

    def clear(self) -> None:
        self.ele, self.lst = [None, None, None], []

    def add(self, bi: Bi) -> bool:
        if bi.dir == self.dir:
            raise ValueError("特征序列只接受与线段方向相反的笔")
        self.lst.append(bi)
        if self.ele[0] is None:
            self.ele[0] = Eigen(bi, self.dir)
            return False
        if self.ele[1] is None:
            return self._treat_second(bi)
        if self.ele[2] is None:
            return self._treat_third(bi)
        raise ValueError("特征序列3个元素都找齐了还没处理")

    def _treat_second(self, bi: Bi) -> bool:
        ele0 = self.ele[0]
        assert ele0 is not None
        if ele0.try_add(bi, exclude_included=True) == COMBINE:
            return False
        self.ele[1] = Eigen(bi, self.dir)
        if (self.ele[1].high < ele0.high) if self._up() else (self.ele[1].low > ele0.low):
            return self.reset()         # 前两元素不可能成分型
        return False

    def _treat_third(self, bi: Bi) -> bool:
        ele1 = self.ele[1]
        assert self.ele[0] is not None and ele1 is not None
        self.last_evidence_bi_is_sure = bi.sure
        allow_top_equal = 1 if bi.dir == DIR_DOWN else -1
        direction = ele1.try_add(bi, allow_top_equal=allow_top_equal)
        if direction == COMBINE:
            return False
        self.ele[2] = Eigen(bi, direction)
        if not self._actual_break():
            return self.reset()
        ele1.update_fx(self.ele[0], self.ele[2], allow_top_equal)
        return True if ele1.fx == (FX_TOP if self._up() else FX_BOTTOM) else self.reset()

    def reset(self) -> bool:
        """丢掉第一笔重新喂（EigenFX.reset 的 exclude_included 分支）。返回重放中是否又成分型。"""
        replay = list(self.lst[1:])
        self.clear()
        return any(self.add(bi) for bi in replay)

    def _nth(self, bi: Bi, step: int) -> Optional[Bi]:
        nxt = bi.idx + step
        return self.bis[nxt] if 0 <= nxt < len(self.bis) else None

    def _actual_break(self) -> bool:
        """防止第二元素因为合并导致后面没有实际突破（EigenFX.actual_break）。"""
        ele1, ele2 = self.ele[1], self.ele[2]
        assert ele1 is not None and ele2 is not None
        if (ele2.low < ele1.lst[-1].low) if self._up() else (ele2.high > ele1.lst[-1].high):
            return True
        ele2_bi = ele2.lst[0]
        nxt, nxt2 = self._nth(ele2_bi, 1), self._nth(ele2_bi, 2)
        if nxt2 is not None:
            broke = (nxt2.low < ele2_bi.low) if ele2_bi.dir == DIR_DOWN else (nxt2.high > ele2_bi.high)
            if broke:
                self.last_evidence_bi_is_sure = nxt2.sure
                return True
            if not nxt2.sure or self._nth(nxt2, 1) is None:
                self.actual_break_flag = False      # 证据尚未成型 → 线段只能是不确定的
                return True
            return False
        if nxt is not None and ((nxt.high > ele1.high) if self._up() else (nxt.low < ele1.low)):
            return False
        self.actual_break_flag = False
        return True

    def can_be_end(self) -> Optional[bool]:
        """线段能否在此结束。True=正常结束；False=不成立；None=缺口情形找到末尾也没反向分型。"""
        ele1 = self.ele[1]
        assert ele1 is not None
        if not ele1.gap:
            return True if self.actual_break_flag else None
        # 第二类情形：缺口后必须出现反向特征序列分型，才算真正破坏原线段
        return self._find_revert_fx(ele1.peak_bi_idx() + 2)

    def _find_revert_fx(self, begin_idx: int) -> Optional[bool]:
        """在 begin_idx 之后找反向线段的特征序列分型（EigenFX.find_revert_fx）。上游把"跌破
        thred_value 即判否"的分支注释掉了（其 issue #272），此处同样不实现。"""
        if begin_idx >= len(self.bis):
            return None
        sub = EigenFX(revert(self.bis[begin_idx].dir), self.bis)
        for bi in self.bis[begin_idx::2]:
            if not sub.add(bi):
                continue
            while True:
                test = sub.can_be_end()
                if not sub.actual_break_flag:
                    test = None
                if test is False:
                    if not sub.reset():
                        break
                    continue
                assert sub.ele[2] is not None
                if test is True:
                    self.last_evidence_bi_is_sure = (sub.ele[2].lst[-1].sure
                                                     and sub.last_evidence_bi_is_sure)
                return test
        return None

    def all_bi_is_sure(self) -> bool:
        return all(bi.sure for bi in self.lst) and self.last_evidence_bi_is_sure
