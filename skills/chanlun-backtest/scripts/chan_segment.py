#!/usr/bin/env python3
"""
缠论线段层 — 线段列表构造与对外契约
====================================
输入 chan_kline.build_bis 的笔列表，输出线段列表。底半部（特征序列 Eigen / 特征序列分型
EigenFX，含缺口即"第二类情形"处理）在同目录 chan_eigen.py；本文件负责用分型结果切段、
左侧收尾（left_method="peak"）与导出契约。**下游只 import 本文件。**

算法规格对齐 Vespa314/chan.py（pinned 429d6ed，MIT，`seg_algo="chan"` +
`left_seg_method="peak"`，二者均为其默认档且是本实现唯一支持的档位）：参照实现快照在仓库
third_party 目录下，**仅测试可 import**（生产引用会被 tests/test_chan_reference_guard.py
静态拦截），故本文件只复刻算法、不引用它；注释里的 `X.py::f` 均指该快照中的规格出处。

确定性状态（Seg/Seg.py::CSeg.is_sure）与笔层同义：分型证据涉及未确认笔、缺口情形没找到
反向分型、或末尾尚未成段（collect_left_seg 收尾）时，线段以 is_sure=False 输出。

纯函数边界：公开入口 build_segs / analyze_segs 不修改入参（bis 只读，内部转成不可变 Bi），
可变状态只存活于一次调用内。
"""

import importlib.util
import os
from typing import Any, Dict, List, Optional, Sequence

try:  # 调用方（chan_structure / CLI）已把本目录放进 sys.path
    import chan_eigen
except ImportError:  # 被 importlib 按路径加载时（测试）本目录不在 sys.path，按文件名加载
    _EIGEN_SPEC = importlib.util.spec_from_file_location(
        "chan_eigen", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chan_eigen.py"))
    chan_eigen = importlib.util.module_from_spec(_EIGEN_SPEC)
    _EIGEN_SPEC.loader.exec_module(chan_eigen)

Bi, EigenFX = chan_eigen.Bi, chan_eigen.EigenFX
DIR_UP, DIR_DOWN = chan_eigen.DIR_UP, chan_eigen.DIR_DOWN


class _SegValueError(ValueError):
    """线段首尾值与方向矛盾（SEG_END_VALUE_ERR）：只有第一段允许吞掉并改走别的路径。"""


def _find_peak_bi(seq: Sequence[Bi], is_high: bool, bis: Sequence[Bi]) -> Optional[Bi]:
    """区间内终点最高的向上笔 / 最低的向下笔（SegListComm.py::FindPeakBi）。"""
    peak_val = float("-inf") if is_high else float("inf")
    peak = None
    for bi in seq:
        if bi.dir != (DIR_UP if is_high else DIR_DOWN):
            continue
        if (bi.end_val < peak_val) if is_high else (bi.end_val > peak_val):
            continue
        pre2 = bis[bi.idx - 2] if bi.idx >= 2 else None
        if pre2 is not None and ((pre2.end_val > bi.end_val) if is_high else (pre2.end_val < bi.end_val)):
            continue
        peak_val, peak = bi.end_val, bi
    return peak


# ========== 线段列表（Seg/SegListChan.py + Seg/SegListComm.py）==========

class _SegList:
    """从零构建线段列表。chan.py 是增量更新（do_init 弹掉不确定段后从最后一个确定段续算），
    确定段一旦成立不再变化，故"对最终笔列表整体重算"与其最终态等价，这里取后者（纯函数）。"""

    def __init__(self, bis: Sequence[Bi]):
        self.bis = bis
        self.segs: List[Dict[str, Any]] = []

    def _end_bi(self) -> Bi:
        return self.bis[self.segs[-1]["end_bi_idx"]]

    # ---- 建段 ----

    def _make_seg(self, start_idx: int, end_idx: int, is_sure: bool,
                  seg_dir: Optional[str], reason: str) -> Dict[str, Any]:
        start, end = self.bis[start_idx], self.bis[end_idx]
        if not (start.idx == 0 or start.dir == end.dir or not is_sure):
            raise ValueError(f"线段首尾笔方向不一致: {start.idx}->{end.idx}")
        direction = end.dir if seg_dir is None else seg_dir
        if end_idx - start_idx < 2:     # 不足 2 笔的段一律不确定（CSeg.__init__）
            is_sure = False
        if is_sure and ((start.begin_val < end.end_val) if direction == DIR_DOWN
                        else (start.begin_val > end.end_val)):
            raise _SegValueError(f"线段方向与首尾值矛盾: {direction} {start_idx}->{end_idx}")
        return {"dir": direction, "start_bi_idx": start_idx, "end_bi_idx": end_idx,
                "is_sure": is_sure, "reason": reason}

    def _try_add_new_seg(self, end_bi_idx: int, is_sure: bool, seg_dir: Optional[str],
                         split_first: bool, reason: str) -> None:
        if not self.segs and split_first and end_bi_idx >= 3:
            peak = _find_peak_bi(self.bis[end_bi_idx - 3::-1],
                                 self.bis[end_bi_idx].dir == DIR_DOWN, self.bis)
            # 左侧存在比第一笔开头还高/低的极值笔 → 先把它切成第一段
            if peak is not None and (
                (peak.low < self.bis[0].low or peak.idx == 0) if peak.dir == DIR_DOWN
                else (peak.high > self.bis[0].high or peak.idx == 0)
            ):
                self._add_new_seg(peak.idx, is_sure=False, seg_dir=peak.dir, reason="split_first_1st")
                self._add_new_seg(end_bi_idx, is_sure=False, reason="split_first_2nd")
                return
        start_idx = self.segs[-1]["end_bi_idx"] + 1 if self.segs else 0
        self.segs.append(self._make_seg(start_idx, end_bi_idx, is_sure, seg_dir, reason))

    def _add_new_seg(self, end_bi_idx: int, is_sure: bool = True, seg_dir: Optional[str] = None,
                     split_first: bool = True, reason: str = "normal") -> bool:
        try:
            self._try_add_new_seg(end_bi_idx, is_sure, seg_dir, split_first, reason)
        except _SegValueError:
            if not self.segs:           # 只有第一段允许"方向定错了"，交给调用方换个起点重来
                return False
            raise
        return True

    # ---- 确定段（SegListChan）----

    def build(self) -> List[Dict[str, Any]]:
        begin_idx: Optional[int] = 0
        while begin_idx is not None:    # 上游为递归，这里改等价循环
            begin_idx = self._scan_sure_seg(begin_idx)
        self._collect_left_seg()
        return self.segs

    def _first_dir_guess(self, last_dir: Optional[str], bi: Bi,
                         up_eigen: EigenFX, down_eigen: EigenFX) -> Optional[str]:
        """第一段方向不以"谁先成分型"决定（SegListChan.cal_seg_sure 内联块）。"""
        if up_eigen.ele[1] is not None and bi.dir == DIR_DOWN:
            down_eigen.clear()
            last_dir = DIR_DOWN
        elif down_eigen.ele[1] is not None and bi.dir == DIR_UP:
            up_eigen.clear()
            last_dir = DIR_UP
        if up_eigen.ele[1] is None and last_dir == DIR_DOWN and bi.dir == DIR_DOWN:
            return None
        if down_eigen.ele[1] is None and last_dir == DIR_UP and bi.dir == DIR_UP:
            return None
        return last_dir

    def _scan_sure_seg(self, begin_idx: int) -> Optional[int]:
        """从 begin_idx 起找第一个特征序列分型并处理；返回下一轮起点（None = 结束）。"""
        up_eigen = EigenFX(DIR_UP, self.bis)        # 上升线段的特征序列 = 下降笔
        down_eigen = EigenFX(DIR_DOWN, self.bis)    # 下降线段的特征序列 = 上升笔
        last_dir = self.segs[-1]["dir"] if self.segs else None
        for bi in self.bis[begin_idx:]:
            fx_eigen = None
            if bi.dir == DIR_DOWN and last_dir != DIR_UP:
                fx_eigen = up_eigen if up_eigen.add(bi) else None
            elif bi.dir == DIR_UP and last_dir != DIR_DOWN:
                fx_eigen = down_eigen if down_eigen.add(bi) else None
            if not self.segs:
                last_dir = self._first_dir_guess(last_dir, bi, up_eigen, down_eigen)
            if fx_eigen is not None:
                return self._treat_fx_eigen(fx_eigen)
        return None

    def _treat_fx_eigen(self, fx_eigen: EigenFX) -> Optional[int]:
        test = fx_eigen.can_be_end()
        if test is False:
            return fx_eigen.lst[1].idx
        assert fx_eigen.ele[1] is not None
        end_bi_idx = fx_eigen.ele[1].peak_bi_idx()
        is_true = test is not None      # None = 缺口情形没找到反向分型 → 段不确定且不再续算
        if not self._add_new_seg(end_bi_idx, is_sure=is_true and fx_eigen.all_bi_is_sure()):
            return end_bi_idx + 1       # 第一段方向定错，换起点重来
        return end_bi_idx + 1 if is_true else None

    # ---- 左侧收尾（SegListComm，left_method="peak"）----

    def _collect_left_seg(self) -> None:
        while True:
            if not self.segs:
                self._collect_first_seg()
                return
            self._collect_segs()
            if (self.bis and self.segs and not self.segs[-1]["is_sure"]
                    and self.bis[-1].idx - self.segs[-1]["end_bi_idx"] > 2):
                self.segs.pop()
                continue
            return

    def _collect_first_seg(self) -> None:
        if len(self.bis) < 3:
            return
        first_val = self.bis[0].begin_val
        is_high = abs(max(b.high for b in self.bis) - first_val) >= \
            abs(min(b.low for b in self.bis) - first_val)
        peak = _find_peak_bi(self.bis, is_high, self.bis)
        if peak is None:
            return
        self._add_new_seg(peak.idx, is_sure=False, seg_dir=DIR_UP if is_high else DIR_DOWN,
                          split_first=False, reason="0seg_find_high" if is_high else "0seg_find_low")
        self._collect_left_as_seg()

    def _collect_segs(self) -> None:
        last_bi, end_bi = self.bis[-1], self._end_bi()
        if last_bi.idx - end_bi.idx < 3:
            return
        force_up = end_bi.dir == DIR_DOWN and last_bi.end_val <= end_bi.end_val
        force_down = end_bi.dir == DIR_UP and last_bi.end_val >= end_bi.end_val
        if not (force_up or force_down):
            self._collect_left_seg_peak(end_bi)
            return
        # 剩下的笔没走出与最后线段方向一致的高低关系 → 强行按极值笔切一段
        peak = _find_peak_bi(self.bis[end_bi.idx + 3:], force_up, self.bis)
        if peak is not None:
            self._add_new_seg(peak.idx, is_sure=False, seg_dir=DIR_UP if force_up else DIR_DOWN,
                              reason="collectleft_find_high_force" if force_up else "collectleft_find_low_force")
            self._collect_left_seg()

    def _collect_left_seg_peak(self, end_bi: Bi) -> None:
        while True:
            is_high = end_bi.dir == DIR_DOWN
            peak = _find_peak_bi(self.bis[end_bi.idx + 3:], is_high, self.bis)
            found = peak is not None and peak.idx - end_bi.idx >= 3
            if found and peak is not None:
                self._add_new_seg(peak.idx, is_sure=False, seg_dir=DIR_UP if is_high else DIR_DOWN,
                                  reason="collectleft_find_high" if is_high else "collectleft_find_low")
            end_bi = self._end_bi()
            if not found:
                self._collect_left_as_seg()
                return

    def _collect_left_as_seg(self) -> None:
        last_bi, end_bi = self.bis[-1], self._end_bi()
        if end_bi.idx + 1 >= len(self.bis):
            return
        same_dir = end_bi.dir == last_bi.dir
        self._add_new_seg(last_bi.idx - 1 if same_dir else last_bi.idx, is_sure=False,
                          reason="collect_left_1" if same_dir else "collect_left_0")


# ========== 公开入口（纯函数）==========

def _to_internal(bis: Sequence[Dict[str, Any]]) -> List[Bi]:
    return [Bi(idx=i, dir=b["dir"], high=float(b["high"]), low=float(b["low"]),
               begin_val=float(b["start_price"]), end_val=float(b["end_price"]),
               sure=bool(b["is_sure"]) or bool(b.get("used_to_be_sure", False)))
            for i, b in enumerate(bis)]


def _export(seg: Dict[str, Any], bis: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """线段 → 对外契约。high/low 沿用笔层口径（端点价格的极值），与参考实现 CSeg._high/_low
    的"端点原始K线影线"口径可能相差一根K线的上下影，不影响端点比对。"""
    start, end = bis[seg["start_bi_idx"]], bis[seg["end_bi_idx"]]
    start_price, end_price = float(start["start_price"]), float(end["end_price"])
    return {"dir": seg["dir"],
            "start_bi_idx": seg["start_bi_idx"], "end_bi_idx": seg["end_bi_idx"],
            "start_idx": start["start_idx"], "end_idx": end["end_idx"],
            "start_price": start_price, "end_price": end_price,
            "high": max(start_price, end_price), "low": min(start_price, end_price),
            "is_sure": seg["is_sure"]}


def build_segs(bis: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """笔列表 → 线段列表（末段未确认时 is_sure=False）。纯函数：不修改 bis。"""
    if len(bis) < 3:
        return []
    return [_export(seg, bis) for seg in _SegList(_to_internal(bis)).build()]


def analyze_segs(bis: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """线段层汇总：{"segs", "seg_count", "sure_seg_count", "last_seg"}。纯函数。"""
    segs = build_segs(bis)
    return {"segs": segs, "seg_count": len(segs),
            "sure_seg_count": sum(1 for s in segs if s["is_sure"]),
            "last_seg": segs[-1] if segs else None}
