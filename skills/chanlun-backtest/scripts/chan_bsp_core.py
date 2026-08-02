#!/usr/bin/env python3
"""
缠论买卖点层（底半部）— 配置 / 只读投影 / 度量族 / 买卖点记录与导出契约
======================================================================
`chan_bsp.py` 的支撑模块（判据引擎在那边，数据结构与契约在这边）：把 T1–T3 的产物
（笔/线段/中枢字典）投影成不可变数据类，提供 chan.py `CBi.cal_macd_metric` 的
area / peak / slope 三档度量，并定义买卖点记录 `Bsp` 与它到 analyze() signals 的导出。
**下游只 import chan_bsp.py**，本文件不对外承诺契约。

算法规格对齐 Vespa314/chan.py（pinned 429d6ed，MIT）：参照实现快照在仓库 third_party
目录下，**仅测试可 import**（生产引用会被 tests/test_chan_reference_guard.py 静态拦截），
故本文件只复刻算法、不引用它；注释里的 `X.py::f` 均指该快照中的规格出处。

默认参数逐项对齐 `ChanConfig.py::set_bsp_config` 的 para_dict（含 divergence_rate=inf
的"保送"语义与 macd_algo="peak"）。

纯函数边界：所有投影都是 frozen dataclass，构造过程只读入参。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

DIR_UP = "up"
DIR_DOWN = "down"

# 买卖点类型（Common/CEnum.py::BSP_TYPE 的取值）
T1, T1P, T2, T2S, T3A, T3B = "1", "1p", "2", "2s", "3a", "3b"
BSP_TYPES: Tuple[str, ...] = (T1, T1P, T2, T2S, T3A, T3B)
# 本仓库实现的背驰度量（chan.py 另有 full_area/diff/amp/量额/RSI 等 9 档，不实现）
MACD_ALGOS: Tuple[str, ...] = ("area", "peak", "slope")
_EPS = 1e-7

# 新谱系 → 旧四类型（口径决定 2026-08-01）：三类买卖点两个变体都映射到三买/三卖，
# 一类买卖点映射到底/顶背驰；1p/2/2s 无 legacy 对应，只在新谱系出现。
LEGACY_TYPE = {(T3A, True): "third_buy", (T3B, True): "third_buy",
               (T3A, False): "third_sell", (T3B, False): "third_sell",
               (T1, True): "bottom_divergence", (T1, False): "top_divergence"}

BSP_LABEL = {T1: "一类(趋势背驰)", T1P: "盘整背驰", T2: "二类(回抽)",
             T2S: "类二", T3A: "三类(中枢后)", T3B: "三类(中枢前)"}


@dataclass(frozen=True)
class BspConfig:
    """买卖点配置。默认值逐项对齐 ChanConfig.py::set_bsp_config 的 para_dict。"""

    divergence_rate: float = float("inf")   # >100 即"保送"（ZS.py::is_divergence）
    min_zs_cnt: int = 1
    bsp1_only_multibi_zs: bool = True
    max_bs2_rate: float = 0.9999
    macd_algo: str = "peak"
    bs1_peak: bool = True
    bs_type: Tuple[str, ...] = BSP_TYPES
    bsp2_follow_1: bool = True
    bsp3_follow_1: bool = True
    bsp3_peak: bool = False
    bsp2s_follow_2: bool = False
    max_bsp2s_lv: Optional[int] = None
    strict_bsp3: bool = False
    bsp3a_max_zs_cnt: int = 1

    def __post_init__(self) -> None:
        if self.macd_algo not in MACD_ALGOS:
            raise ValueError(f"未知 macd_algo={self.macd_algo}，本仓库只支持 {MACD_ALGOS}")
        unknown = [t for t in self.bs_type if t not in BSP_TYPES]
        if unknown:
            raise ValueError(f"未知买卖点类型 {unknown}，应为 {BSP_TYPES} 的子集")
        if self.max_bs2_rate > 1:            # BSPointConfig.py::CPointConfig 的断言
            raise ValueError("max_bs2_rate 必须 <= 1")
        if self.bsp3a_max_zs_cnt < 1:
            raise ValueError("bsp3a_max_zs_cnt 必须 >= 1")

    def wants(self, bsp_type: str) -> bool:
        """该类型是否在 target_types 里（CPointConfig.target_types）。"""
        return bsp_type in self.bs_type


# ========== 只读投影 ==========

@dataclass(frozen=True)
class Bi:
    """笔投影（Bi/Bi.py::CBi 用到的只读部分）。idx 为笔序号，begin_idx/end_idx 为原始K线索引。"""

    idx: int
    dir: str
    begin_val: float
    end_val: float
    high: float
    low: float
    begin_idx: int
    end_idx: int
    seg_idx: int                    # KLine_List.py::cal_seg 赋的 bi.seg_idx（尾部笔 = len(segs)）
    parent_seg_idx: Optional[int]   # CSeg.update_bi_list 赋的 bi.parent_seg（尾部笔为 None）
    is_sure: bool

    @property
    def is_down(self) -> bool:
        return self.dir == DIR_DOWN

    @property
    def is_up(self) -> bool:
        return self.dir == DIR_UP

    @property
    def is_buy(self) -> bool:
        """买卖点方向 = 锚定笔方向（BSPointList.py::add_bs 的 is_buy = bi.is_down()）。"""
        return self.is_down

    @property
    def amp(self) -> float:
        return abs(self.end_val - self.begin_val)


@dataclass(frozen=True)
class Zs:
    """中枢投影（ZS/ZS.py::CZS 用到的只读部分），字段来自 chan_center.build_centers。"""

    high: float
    low: float
    peak_high: float
    peak_low: float
    begin_bi_idx: int
    end_bi_idx: int
    bi_in_idx: Optional[int]
    bi_out_idx: Optional[int]
    seg_idx: int
    is_one_bi: bool


@dataclass(frozen=True)
class Seg:
    """线段投影 + 段内中枢序号（KLine_List.py::update_zs_in_seg 的 seg.zs_lst）。"""

    idx: int
    dir: str
    start_bi_idx: int
    end_bi_idx: int
    is_sure: bool
    zs_idxs: Tuple[int, ...] = field(default_factory=tuple)

    @property
    def is_down(self) -> bool:
        return self.dir == DIR_DOWN

    @property
    def is_up(self) -> bool:
        return self.dir == DIR_UP


def project_bis(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]]) -> List[Bi]:
    """笔字典 → Bi。seg_idx/parent_seg_idx 由线段区间反查（KLine_List.py::cal_seg）。"""
    owner: List[Optional[int]] = [None] * len(bis)
    for seg_idx, seg in enumerate(segs):
        for bi_idx in range(seg["start_bi_idx"], min(seg["end_bi_idx"] + 1, len(bis))):
            owner[bi_idx] = seg_idx
    return [Bi(idx=i, dir=b["dir"], begin_val=float(b["start_price"]), end_val=float(b["end_price"]),
               high=float(b["high"]), low=float(b["low"]),
               begin_idx=int(b["start_idx"]), end_idx=int(b["end_idx"]),
               seg_idx=len(segs) if owner[i] is None else owner[i],
               parent_seg_idx=owner[i], is_sure=bool(b["is_sure"]))
            for i, b in enumerate(bis)]


def project_zss(centers: Sequence[Dict[str, Any]]) -> List[Zs]:
    return [Zs(high=float(c["zg"]), low=float(c["zd"]),
               peak_high=float(c["peak_high"]), peak_low=float(c["peak_low"]),
               begin_bi_idx=int(c["start_bi_idx"]), end_bi_idx=int(c["end_bi_idx"]),
               bi_in_idx=c["bi_in_idx"], bi_out_idx=c["bi_out_idx"],
               seg_idx=int(c["seg_idx"]), is_one_bi=int(c["bi_count"]) == 1)
            for c in centers]


def project_segs(segs: Sequence[Dict[str, Any]], zss: Sequence[Zs]) -> List[Seg]:
    """线段 + 归属中枢。归属判据 = 中枢首笔落在线段笔区间内（ZS.py::CZS.is_inside），
    与 chan_center 导出的 zs.seg_idx 同义；尾部未成段区间的中枢不属于任何线段。"""
    buckets: List[List[int]] = [[] for _ in segs]
    for zs_idx, zs in enumerate(zss):
        if 0 <= zs.seg_idx < len(segs):
            buckets[zs.seg_idx].append(zs_idx)
    return [Seg(idx=i, dir=s["dir"], start_bi_idx=int(s["start_bi_idx"]),
                end_bi_idx=int(s["end_bi_idx"]), is_sure=bool(s["is_sure"]),
                zs_idxs=tuple(buckets[i]))
            for i, s in enumerate(segs)]


# ========== 买卖点记录与导出契约（BuySellPoint/BS_Point.py::CBS_Point）==========

class Bsp:
    """一个买卖点。同一笔上可挂多个类型（add_another_bsp_prop），特征字典合并。"""

    def __init__(self, bi: Bi, bs_type: str, relate_bsp1_bi_idx: Optional[int],
                 feature_dict: Optional[Dict[str, Any]]):
        self.bi_idx = bi.idx
        self.is_buy = bi.is_buy
        self.is_sure = bi.is_sure
        self.end_idx = bi.end_idx
        self.end_val = bi.end_val
        self.types: List[str] = [bs_type]
        self.relate_bsp1_bi_idx = relate_bsp1_bi_idx
        self.features: Dict[str, Any] = {"bsp_bi_amp": bi.amp}   # CBS_Point.init_common_feature
        self.features.update(feature_dict or {})

    def merge(self, bs_type: str, relate_bsp1_bi_idx: Optional[int],
              feature_dict: Optional[Dict[str, Any]]) -> None:
        self.types.append(bs_type)
        if self.relate_bsp1_bi_idx is None:
            self.relate_bsp1_bi_idx = relate_bsp1_bi_idx
        self.features.update(feature_dict or {})


def export_bsp(bsp: Bsp) -> Dict[str, Any]:
    """Bsp → 纯字典（idx/price 沿用 chan_structure 旧信号口径：原始K线索引 + 端点价）。"""
    return {"bi_idx": bsp.bi_idx, "is_buy": bsp.is_buy, "is_sure": bsp.is_sure,
            "idx": bsp.end_idx, "price": bsp.end_val,
            "types": sorted(bsp.types, key=BSP_TYPES.index),
            "relate_bsp1_bi_idx": bsp.relate_bsp1_bi_idx,
            "feature_dict": dict(bsp.features)}


def _detail(bsp_type: str, is_buy: bool, features: Dict[str, Any]) -> str:
    parts = [f"{BSP_LABEL[bsp_type]}{'买' if is_buy else '卖'}点"]
    for key in ("divergence_rate", "bsp2_retrace_rate", "bsp2s_retrace_rate", "bsp3_zs_height"):
        value = features.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{key}={value:.3f}")
    return ":".join(parts) if len(parts) > 1 else parts[0]


def signal_of(bsp: Dict[str, Any], bsp_type: str, legacy: Optional[str]) -> Dict[str, Any]:
    """导出的买卖点 + 一个类型 → analyze() signals 的一条。legacy 为 None 时用新谱系类型名。"""
    is_buy = bsp["is_buy"]
    side = "buy" if is_buy else "sell"
    return {"type": legacy or f"bsp{bsp_type}_{side}",
            "idx": bsp["idx"], "price": bsp["price"],
            "detail": _detail(bsp_type, is_buy, bsp["feature_dict"]),
            "bsp_type": bsp_type, "is_buy": is_buy, "is_sure": bsp["is_sure"],
            "bi_idx": bsp["bi_idx"], "feature_dict": bsp["feature_dict"],
            "strategy_id_v2": f"chanlun_bsp{bsp_type}_{side}_v2"}


# ========== 几何判据（BuySellPoint/BSPointList.py 末尾的自由函数）==========

def has_overlap(low1: float, high1: float, low2: float, high2: float) -> bool:
    return high2 > low1 and high1 > low2


def bsp2s_break_bsp1(bsp2s_bi: Bi, break_bi: Bi) -> bool:
    return (bsp2s_bi.is_down and bsp2s_bi.low < break_bi.low) or \
           (bsp2s_bi.is_up and bsp2s_bi.high > break_bi.high)


def bsp3_back2zs(bsp3_bi: Bi, zs: Zs) -> bool:
    return (bsp3_bi.is_down and bsp3_bi.low < zs.high) or (bsp3_bi.is_up and bsp3_bi.high > zs.low)


def bsp3_break_zspeak(bsp3_bi: Bi, zs: Zs) -> bool:
    return (bsp3_bi.is_down and bsp3_bi.high >= zs.peak_high) or \
           (bsp3_bi.is_up and bsp3_bi.low <= zs.peak_low)


# ========== 背驰度量族（Bi/Bi.py::cal_macd_metric）==========

class MacdMetric:
    """area / peak / slope 三档度量。MACD 柱由 common/indicators.macd_hist 提供
    （与 chan.py 的 `klu.macd.macd = 2*(DIF-DEA)` 同口径）。

    与参考实现的已知口径差异：chan.py 在**合并K线**区间（begin_klc~end_klc 的全部原始K线）
    上取值，本实现在笔端点的原始K线区间 [begin_idx, end_idx] 上取值——两者只在端点合并K线
    含多根原始K线时相差首尾几根；area 档因遇符号翻转即停，实际几乎不受影响。
    """

    def __init__(self, hist: Sequence[Optional[float]],
                 highs: Sequence[float], lows: Sequence[float]):
        self.hist = hist
        self.highs = highs
        self.lows = lows

    def _bar(self, i: int) -> float:
        if 0 <= i < len(self.hist) and self.hist[i] is not None:
            return float(self.hist[i])
        return 0.0

    def value(self, bi: Bi, algo: str, is_reverse: bool) -> float:
        if algo == "peak":
            return self._peak(bi)
        if algo == "area":
            return self._half_reverse(bi) if is_reverse else self._half_obverse(bi)
        if algo == "slope":
            return self._slope(bi)
        raise ValueError(f"未知 macd_algo={algo}，本仓库只支持 {MACD_ALGOS}")

    def _peak(self, bi: Bi) -> float:
        """同向 MACD 柱的绝对值峰值（Cal_MACD_peak）。基数 1e-7 保证非零。"""
        peak = _EPS
        for i in range(bi.begin_idx, bi.end_idx + 1):
            bar = self._bar(i)
            if abs(bar) > peak and ((bi.is_down and bar < 0) or (bi.is_up and bar > 0)):
                peak = abs(bar)
        return peak

    def _half_obverse(self, bi: Bi) -> float:
        """自起点向后累加同号 MACD 柱，遇符号翻转即停（Cal_MACD_half_obverse）。"""
        return self._half(range(bi.begin_idx, bi.end_idx + 1), self._bar(bi.begin_idx))

    def _half_reverse(self, bi: Bi) -> float:
        """自终点向前累加同号 MACD 柱，遇符号翻转即停（Cal_MACD_half_reverse）。"""
        return self._half(range(bi.end_idx, bi.begin_idx - 1, -1), self._bar(bi.end_idx))

    def _half(self, idx_range: Sequence[int], anchor: float) -> float:
        total = _EPS
        for i in idx_range:
            bar = self._bar(i)
            if bar * anchor > 0:
                total += abs(bar)
            else:
                break
        return total

    def _slope(self, bi: Bi) -> float:
        """价格斜率（Cal_MACD_slope）：不依赖 MACD，用端点原始K线的高低点。"""
        span = bi.end_idx - bi.begin_idx + 1
        if bi.is_up:
            high = self.highs[bi.end_idx]
            return (high - self.lows[bi.begin_idx]) / high / span
        high = self.highs[bi.begin_idx]
        return (high - self.lows[bi.end_idx]) / high / span
