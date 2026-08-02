#!/usr/bin/env python3
"""
缠论买卖点层 — T1/T1P/T2/T2S/T3A/T3B 全谱系 + feature_dict
==========================================================
输入 T1–T3 的产物（chan_kline 的笔、chan_segment 的线段、chan_center 的中枢），输出买卖点。
取代 chan_structure 旧版"只看最后一个中枢的三买三卖 + 最近两笔 MACD 面积背驰"的四点升级：

1. **六类买卖点**：一类（趋势背驰 T1）、盘整背驰（T1P）、二类（回抽 T2）、类二（T2S）、
   三类（中枢后 T3A / 中枢前 T3B）——BuySellPoint/BSPointList.py::cal；
2. **线段口径**：一类买卖点锚定在线段末笔、三类锚定在段内中枢的出笔之后，
   不再是"最后一个笔中枢"的局部判断；
3. **背驰度量族**：area/peak/slope 三档 + divergence_rate 阈值（默认 inf = 保送）——
   Bi/Bi.py::cal_macd_metric + ZS/ZS.py::is_divergence；
4. **feature_dict**：每个买卖点带与参考实现同名的特征键（divergence_rate / retrace_rate /
   amp / zs_height…），直接供 four_dim_scorer 与未来 ML 消费。

配置默认值逐项对齐 `ChanConfig.py::set_bsp_config` 的 para_dict（见 chan_bsp_core.BspConfig）；
本仓库只用一份配置（chan.py 的 b_conf/s_conf 默认取值相同，买卖两侧差异化配置是其 CLI 特性，
不引入）。只读投影、度量族、买卖点记录与导出契约在同目录 `chan_bsp_core.py`，本文件只放
六类买卖点的判据引擎与公开入口；**下游只 import 本文件**。

算法规格对齐 Vespa314/chan.py（pinned 429d6ed，MIT）：参照实现快照在仓库 third_party 目录下，
**仅测试可 import**（生产引用会被 tests/test_chan_reference_guard.py 静态拦截），故本文件只
复刻算法、不引用它；注释里的 `X.py::f` 均指该快照中的规格出处。

与参考实现的口径差异（差分测试白名单的依据）：chan.py 是增量计算，`last_sure_pos` 之前的
买卖点由历史步骤沉淀、不再重算；本实现对最终笔/线段/中枢列表整体重算（纯函数）。确定结构
上两者等价，未确认结构的尾部可能出现少量差异。

纪律红线：本文件产出的一切都是**研究假设**。信号在通过 research_gate 之前，下游只能
display-only / 0 权重；新谱系类型（1p/2/2s）不带 legacy strategy_id，天然 0 权重。
"""

import importlib.util
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:  # 调用方（chan_structure / CLI）已把本目录放进 sys.path
    import chan_bsp_core
except ImportError:  # 被 importlib 按路径加载时（测试）本目录不在 sys.path，按文件名加载
    _CORE_SPEC = importlib.util.spec_from_file_location(
        "chan_bsp_core", os.path.join(os.path.dirname(os.path.abspath(__file__)), "chan_bsp_core.py"))
    chan_bsp_core = importlib.util.module_from_spec(_CORE_SPEC)
    _CORE_SPEC.loader.exec_module(chan_bsp_core)

BspConfig = chan_bsp_core.BspConfig
Bi, Zs, Seg, MacdMetric = chan_bsp_core.Bi, chan_bsp_core.Zs, chan_bsp_core.Seg, chan_bsp_core.MacdMetric
_Bsp = chan_bsp_core.Bsp
BSP_TYPES, LEGACY_TYPE = chan_bsp_core.BSP_TYPES, chan_bsp_core.LEGACY_TYPE
T1, T1P, T2, T2S, T3A, T3B = (chan_bsp_core.T1, chan_bsp_core.T1P, chan_bsp_core.T2,
                              chan_bsp_core.T2S, chan_bsp_core.T3A, chan_bsp_core.T3B)
_has_overlap = chan_bsp_core.has_overlap
_bsp2s_break_bsp1 = chan_bsp_core.bsp2s_break_bsp1
_bsp3_back2zs = chan_bsp_core.bsp3_back2zs
_bsp3_break_zspeak = chan_bsp_core.bsp3_break_zspeak
_EPS = 1e-7


# ========== 引擎（BuySellPoint/BSPointList.py::CBSPointList）==========

class _BspBuilder:
    """从零构建买卖点列表。chan.py 逐 K 增量更新并保留 last_sure_pos 之前的历史结果，
    本实现对最终结构整体重算——确定结构上两者等价（见模块 docstring 的口径差异）。"""

    def __init__(self, bis: Sequence[Bi], segs: Sequence[Seg], zss: Sequence[Zs],
                 cfg: BspConfig, metric: MacdMetric):
        self.bis, self.segs, self.zss = bis, segs, zss
        self.cfg = cfg
        self.metric = metric
        self.stored: Dict[int, _Bsp] = {}        # bsp_store_flat_dict：只放 target 买卖点
        self.bsp1: Dict[int, _Bsp] = {}          # bsp1_dict：全部 T1/T1P（含非 target）

    # ---- 通用工具 ----

    def _bi(self, idx: int) -> Optional[Bi]:
        return self.bis[idx] if 0 <= idx < len(self.bis) else None

    def _zs_bi_lst(self, zs: Zs) -> Sequence[Bi]:
        return self.bis[zs.begin_bi_idx:zs.end_bi_idx + 1]

    def _seg_zss(self, seg: Seg) -> List[Zs]:
        return [self.zss[i] for i in seg.zs_idxs]

    def _multi_bi_zss(self, seg: Seg) -> List[Zs]:
        return [zs for zs in self._seg_zss(seg) if not zs.is_one_bi]

    def _metric(self, bi: Bi, is_reverse: bool) -> float:
        return self.metric.value(bi, self.cfg.macd_algo, is_reverse)

    def add_bs(self, bs_type: str, bi: Bi, relate_bsp1_bi_idx: Optional[int],
               is_target_bsp: bool = True, feature_dict: Optional[Dict[str, Any]] = None) -> None:
        """BSPointList.py::add_bs。同笔已有买卖点则并类型；非 target 的 T1/T1P 仍进 bsp1_dict。"""
        exist = self.stored.get(bi.idx)
        if exist is not None:
            exist.merge(bs_type, relate_bsp1_bi_idx, feature_dict)
            return
        if not self.cfg.wants(bs_type):
            is_target_bsp = False
        if not (is_target_bsp or bs_type in (T1, T1P)):
            return
        bsp = _Bsp(bi, bs_type, relate_bsp1_bi_idx, feature_dict)
        if is_target_bsp:
            self.stored[bi.idx] = bsp
        if bs_type in (T1, T1P):
            self.bsp1[bi.idx] = bsp

    def build(self) -> List[_Bsp]:
        for seg in self.segs:
            self._cal_bs1(seg)
        for seg in self.segs:
            self._cal_bs2(seg)
        for seg in self.segs:
            self._cal_bs3(seg)
        return [self.stored[k] for k in sorted(self.stored)]

    # ---- 背驰判定（ZS/ZS.py）----

    def _end_bi_break(self, zs: Zs, end_bi: Bi) -> bool:
        """出笔必须突破中枢（CZS.end_bi_break）。"""
        return (end_bi.is_down and end_bi.low < zs.low) or (end_bi.is_up and end_bi.high > zs.high)

    def _is_divergence(self, zs: Zs, out_bi: Bi) -> Tuple[bool, Optional[float]]:
        """CZS.is_divergence：divergence_rate > 100 视为保送（默认 inf）。"""
        if not self._end_bi_break(zs, out_bi):
            return False, None
        bi_in = self._bi(zs.bi_in_idx) if zs.bi_in_idx is not None else None
        if bi_in is None:
            return False, None
        in_metric = self._metric(bi_in, is_reverse=False)
        out_metric = self._metric(out_bi, is_reverse=True)
        rate = out_metric / in_metric
        if self.cfg.divergence_rate > 100:
            return True, rate
        return out_metric <= self.cfg.divergence_rate * in_metric, rate

    def _out_bi_is_peak(self, zs: Zs, end_bi_idx: int) -> bool:
        """出笔是否为中枢内的极值（CZS.out_bi_is_peak 的布尔部分）。"""
        bi_out = self._bi(zs.bi_out_idx) if zs.bi_out_idx is not None else None
        if bi_out is None:
            return False
        for bi in self._zs_bi_lst(zs):
            if bi.idx > end_bi_idx:
                break
            if (bi_out.is_down and bi.low < bi_out.low) or (bi_out.is_up and bi.high > bi_out.high):
                return False
        return True

    # ---- 一类买卖点（cal_single_bs1point）----

    def _cal_bs1(self, seg: Seg) -> None:
        zs_lst = self._seg_zss(seg)
        zs_cnt = len(self._multi_bi_zss(seg)) if self.cfg.bsp1_only_multibi_zs else len(zs_lst)
        is_target = self.cfg.min_zs_cnt <= 0 or zs_cnt >= self.cfg.min_zs_cnt
        last = zs_lst[-1] if zs_lst else None
        if last is not None and not last.is_one_bi and last.bi_in_idx is not None and \
                ((last.bi_out_idx is not None and last.bi_out_idx >= seg.end_bi_idx)
                 or last.end_bi_idx >= seg.end_bi_idx) and \
                seg.end_bi_idx - last.bi_in_idx > 2:
            self._treat_bsp1(seg, last, is_target)
        else:
            self._treat_pz_bsp1(seg, is_target)

    def _treat_bsp1(self, seg: Seg, last_zs: Zs, is_target: bool) -> None:
        """趋势背驰一类买卖点（treat_bsp1）。"""
        end_bi = self.bis[seg.end_bi_idx]
        if self.cfg.bs1_peak and not self._out_bi_is_peak(last_zs, seg.end_bi_idx):
            is_target = False
        is_diver, divergence_rate = self._is_divergence(last_zs, end_bi)
        if not is_diver:
            is_target = False
        self.add_bs(T1, end_bi, None, is_target,
                    {"divergence_rate": divergence_rate, "zs_cnt": len(seg.zs_idxs)})

    def _treat_pz_bsp1(self, seg: Seg, is_target: bool) -> None:
        """盘整背驰一类买卖点（treat_pz_bsp1）：末笔与同向前笔比动能。"""
        last_bi = self.bis[seg.end_bi_idx]
        # 参考实现直接取 bi_list[idx-2]（idx<2 时会负索引回绕，属其隐患），这里 fail-closed
        pre_bi = self._bi(last_bi.idx - 2) if last_bi.idx >= 2 else None
        if pre_bi is None or last_bi.seg_idx != pre_bi.seg_idx or last_bi.dir != seg.dir:
            return
        if last_bi.is_down and last_bi.low > pre_bi.low:      # 未创新低
            return
        if last_bi.is_up and last_bi.high < pre_bi.high:      # 未创新高
            return
        in_metric = self._metric(pre_bi, is_reverse=False)
        out_metric = self._metric(last_bi, is_reverse=True)
        if out_metric > self.cfg.divergence_rate * in_metric:
            is_target = False
        self.add_bs(T1P, last_bi, None, is_target,
                    {"divergence_rate": out_metric / (in_metric + _EPS), "bsp1_bi_amp": last_bi.amp})

    # ---- 二类/类二买卖点（treat_bsp2 / treat_bsp2s）----

    def _bsp2_anchors(self, seg: Seg) -> Optional[Tuple[Optional[Bi], Bi, Bi]]:
        """(bsp1_bi, break_bi, bsp2_bi)；只有一条线段时退化为前两笔（treat_bsp2 的 else 分支）。"""
        if len(self.segs) > 1:
            bsp1_bi = self.bis[seg.end_bi_idx]
            if bsp1_bi.idx + 2 >= len(self.bis):
                return None
            return bsp1_bi, self.bis[bsp1_bi.idx + 1], self.bis[bsp1_bi.idx + 2]
        if len(self.bis) <= 1:
            return None
        return None, self.bis[0], self.bis[1]

    def _cal_bs2(self, seg: Seg) -> None:
        if not (self.cfg.wants(T2) or self.cfg.wants(T2S)):
            return
        anchors = self._bsp2_anchors(seg)
        if anchors is None:
            return
        bsp1_bi, break_bi, bsp2_bi = anchors
        if self.cfg.bsp2_follow_1 and (bsp1_bi is None or bsp1_bi.idx not in self.stored):
            return
        real_bsp1 = bsp1_bi.idx if bsp1_bi is not None and bsp1_bi.idx in self.bsp1 else None
        retrace_rate = bsp2_bi.amp / break_bi.amp if break_bi.amp else float("inf")
        if retrace_rate <= self.cfg.max_bs2_rate:
            self.add_bs(T2, bsp2_bi, real_bsp1, feature_dict={
                "bsp2_retrace_rate": retrace_rate, "bsp2_break_bi_amp": break_bi.amp,
                "bsp2_bi_amp": bsp2_bi.amp})
        elif self.cfg.bsp2s_follow_2:
            return
        if self.cfg.wants(T2S):
            self._treat_bsp2s(bsp2_bi, break_bi, real_bsp1)

    def _bsp2s_seg_break(self, bsp2s_bi: Bi, bsp2_bi: Bi) -> bool:
        """类二笔跨到别的线段就停（treat_bsp2s 的 seg_idx 分支）。"""
        if bsp2s_bi.seg_idx == bsp2_bi.seg_idx:
            return False
        return (bsp2s_bi.seg_idx < len(self.segs) - 1
                or bsp2s_bi.seg_idx - bsp2_bi.seg_idx >= 2
                or self.segs[bsp2_bi.seg_idx].is_sure)

    def _treat_bsp2s(self, bsp2_bi: Bi, break_bi: Bi, real_bsp1: Optional[int]) -> None:
        """类二买卖点（treat_bsp2s）：逐层向后找与二买重叠、且不破 break_bi 的同向笔。"""
        bias, low, high = 2, 0.0, 0.0
        while bsp2_bi.idx + bias < len(self.bis):
            bsp2s_bi = self.bis[bsp2_bi.idx + bias]
            if self.cfg.max_bsp2s_lv is not None and bias / 2 > self.cfg.max_bsp2s_lv:
                break
            if self._bsp2s_seg_break(bsp2s_bi, bsp2_bi):
                break
            if bias == 2:
                if not _has_overlap(bsp2_bi.low, bsp2_bi.high, bsp2s_bi.low, bsp2s_bi.high):
                    break
                low, high = max(bsp2_bi.low, bsp2s_bi.low), min(bsp2_bi.high, bsp2s_bi.high)
            elif not _has_overlap(low, high, bsp2s_bi.low, bsp2s_bi.high):
                break
            if _bsp2s_break_bsp1(bsp2s_bi, break_bi) or not break_bi.amp:
                break
            retrace_rate = abs(bsp2s_bi.end_val - break_bi.end_val) / break_bi.amp
            if retrace_rate > self.cfg.max_bs2_rate:
                break
            self.add_bs(T2S, bsp2s_bi, real_bsp1, feature_dict={
                "bsp2s_retrace_rate": retrace_rate, "bsp2s_break_bi_amp": break_bi.amp,
                "bsp2s_bi_amp": bsp2s_bi.amp, "bsp2s_lv": bias / 2})
            bias += 2

    # ---- 三类买卖点（cal_seg_bs3point）----

    def _cal_bs3(self, seg: Seg) -> None:
        if not (self.cfg.wants(T3A) or self.cfg.wants(T3B)):
            return
        if len(self.segs) > 1:
            bsp1_bi: Optional[Bi] = self.bis[seg.end_bi_idx]
            bsp1_bi_idx = bsp1_bi.idx
            next_seg_idx = seg.idx + 1
            next_seg = self.segs[next_seg_idx] if next_seg_idx < len(self.segs) else None
        else:
            bsp1_bi, bsp1_bi_idx = None, -1
            next_seg, next_seg_idx = seg, seg.idx
        if self.cfg.bsp3_follow_1 and (bsp1_bi is None or bsp1_bi.idx not in self.stored):
            return
        real_bsp1 = bsp1_bi.idx if bsp1_bi is not None and bsp1_bi.idx in self.bsp1 else None
        if next_seg is not None:
            self._treat_bsp3_after(next_seg, real_bsp1, bsp1_bi_idx, next_seg_idx)
        self._treat_bsp3_before(seg, next_seg, bsp1_bi, real_bsp1, next_seg_idx)

    def _bsp3a_bi_break(self, bsp3_bi: Bi, next_seg: Seg, next_seg_idx: int) -> bool:
        """treat_bsp3_after 里四条 break 判据的合并（parent_seg / 方向 / seg_idx）。"""
        if bsp3_bi.parent_seg_idx is None:
            if next_seg.idx != len(self.segs) - 1:
                return True
        elif bsp3_bi.parent_seg_idx != next_seg.idx:
            parent = self.segs[bsp3_bi.parent_seg_idx]
            if parent.end_bi_idx - parent.start_bi_idx + 1 >= 3:
                return True
        if bsp3_bi.dir == next_seg.dir:
            return True
        return bsp3_bi.seg_idx != next_seg_idx and next_seg_idx < len(self.segs) - 2

    def _treat_bsp3_after(self, next_seg: Seg, real_bsp1: Optional[int],
                          bsp1_bi_idx: int, next_seg_idx: int) -> None:
        """一类买卖点之后、下一线段内首批多笔中枢的出笔之后一笔（treat_bsp3_after）。"""
        multi_zss = self._multi_bi_zss(next_seg)
        if not multi_zss:
            return
        if self.cfg.strict_bsp3 and multi_zss[0].bi_in_idx != bsp1_bi_idx + 1:
            return
        for zs_idx, zs in enumerate(multi_zss):
            if zs_idx >= self.cfg.bsp3a_max_zs_cnt:
                break
            if zs.bi_out_idx is None or zs.bi_out_idx + 1 >= len(self.bis):
                break
            bsp3_bi = self.bis[zs.bi_out_idx + 1]
            if self._bsp3a_bi_break(bsp3_bi, next_seg, next_seg_idx):
                break
            if _bsp3_back2zs(bsp3_bi, zs):
                continue
            if self.cfg.bsp3_peak and not _bsp3_break_zspeak(bsp3_bi, zs):
                continue
            self.add_bs(T3A, bsp3_bi, real_bsp1, feature_dict={
                "bsp3_zs_height": (zs.high - zs.low) / zs.low, "bsp3_bi_amp": bsp3_bi.amp})

    def _treat_bsp3_before(self, seg: Seg, next_seg: Optional[Seg], bsp1_bi: Optional[Bi],
                           real_bsp1: Optional[int], next_seg_idx: int) -> None:
        """一类买卖点之后、相对**本段末个多笔中枢**的三类买卖点（treat_bsp3_before）。"""
        cmp_zs = next((zs for zs in reversed(self._seg_zss(seg)) if not zs.is_one_bi), None)
        if cmp_zs is None or bsp1_bi is None:
            return
        if self.cfg.strict_bsp3 and (cmp_zs.bi_out_idx is None or cmp_zs.bi_out_idx != bsp1_bi.idx):
            return
        end_bi_idx = self._bsp3_bi_end_idx(next_seg)
        for bsp3_bi in self.bis[bsp1_bi.idx + 2::2]:
            if bsp3_bi.idx > end_bi_idx:
                break
            if bsp3_bi.seg_idx != next_seg_idx and bsp3_bi.seg_idx < len(self.segs) - 1:
                break
            if _bsp3_back2zs(bsp3_bi, cmp_zs):
                continue
            self.add_bs(T3B, bsp3_bi, real_bsp1, feature_dict={
                "bsp3_zs_height": (cmp_zs.high - cmp_zs.low) / cmp_zs.low,
                "bsp3_bi_amp": bsp3_bi.amp})
            break

    def _bsp3_bi_end_idx(self, seg: Optional[Seg]) -> float:
        """三类买卖点（中枢前）的搜索右界（BSPointList.py::cal_bsp3_bi_end_idx）。"""
        if seg is None:
            return float("inf")
        zs_lst = self._seg_zss(seg)
        if not any(not zs.is_one_bi for zs in zs_lst) and seg.idx == len(self.segs) - 1:
            return float("inf")
        for zs in zs_lst:                       # 参考实现只在 bi_out 存在时 break，缺 bi_out 继续找下一个
            if not zs.is_one_bi and zs.bi_out_idx is not None:
                return zs.bi_out_idx
        return seg.end_bi_idx - 1


# ========== 公开入口（纯函数）==========

def build_bsps(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]],
               centers: Sequence[Dict[str, Any]], bars: Sequence[Dict[str, Any]],
               hist: Optional[Sequence[Optional[float]]] = None,
               config: Optional[BspConfig] = None) -> List[Dict[str, Any]]:
    """笔 + 线段 + 中枢 → 买卖点列表（按笔序号升序）。纯函数：不修改任何入参。

    bars 提供原始K线高低点（slope 度量用），hist 为 MACD 柱（缺省视为全 0）。
    """
    cfg = config or BspConfig()
    if not bis or not segs:
        return []
    bi_view = chan_bsp_core.project_bis(bis, segs)
    zs_view = chan_bsp_core.project_zss(centers)
    seg_view = chan_bsp_core.project_segs(segs, zs_view)
    metric = MacdMetric(hist if hist is not None else [None] * len(bars),
                        [float(b["high"]) for b in bars], [float(b["low"]) for b in bars])
    builder = _BspBuilder(bi_view, seg_view, zs_view, cfg, metric)
    return [chan_bsp_core.export_bsp(bsp) for bsp in builder.build()]


def build_signals(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]],
                  centers: Sequence[Dict[str, Any]], bars: Sequence[Dict[str, Any]],
                  hist: Optional[Sequence[Optional[float]]] = None,
                  config: Optional[BspConfig] = None) -> List[Dict[str, Any]]:
    """买卖点 → analyze() 的 signals 元素（每个"买卖点×类型"一条）。

    legacy 四类型（third_buy/third_sell/top_divergence/bottom_divergence）继续产出且
    strategy_id 不变；同一个买卖点上同一 legacy 类型只产一条（3a/3b 同笔并存时），其余
    类型以新谱系名（bsp1p_buy / bsp2_sell …）出现，strategy_id 留空 = 天然 0 权重。
    纯函数：不修改任何入参。
    """
    signals = []
    for bsp in build_bsps(bis, segs, centers, bars, hist, config):
        emitted: set = set()
        for bsp_type in bsp["types"]:
            legacy = LEGACY_TYPE.get((bsp_type, bsp["is_buy"]))
            if legacy in emitted:
                legacy = None
            elif legacy is not None:
                emitted.add(legacy)
            signals.append(chan_bsp_core.signal_of(bsp, bsp_type, legacy))
    return signals
