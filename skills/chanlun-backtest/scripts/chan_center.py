#!/usr/bin/env python3
"""
缠论中枢层 — 段内构造 + 重叠合并 + 进出笔（bi_in / bi_out）
==========================================================
输入 chan_kline.build_bis 的笔列表与 chan_segment.build_segs 的线段列表，输出中枢列表。
取代 chan_structure 旧版"滑窗 3 笔重叠"近似的三点升级：

1. **段内构造**：中枢只在一条线段内部生成（跨线段的重叠笔不再凑成中枢），
   且构造元素是与线段方向相反的笔——ZS/ZSList.py::cal_bi_zs + add_zs_from_bi_range；
2. **合并**：相邻中枢区间重叠即合并成一个大中枢（zs_combine_mode="zs"），
   合并后区间取并集、终点取后者——ZS/ZS.py::CZS.combine / do_combine；
3. **bi_in / bi_out**：进中枢笔 = 首笔前一笔，出中枢笔 = 末笔后一笔（可为 None），
   背驰度量的标准进出口径——KLine/KLine_List.py::update_zs_in_seg 的 set_bi_in/set_bi_out。

算法规格对齐 Vespa314/chan.py（pinned 429d6ed，MIT，配置对齐 CChanConfig 默认档：
zs_combine=True / zs_combine_mode="zs" / one_bi_zs=False / zs_algo="normal"）：参照实现快照在
third_party 目录下，**仅测试可 import**（生产引用会被 tests/test_chan_reference_guard.py
静态拦截），故本文件只复刻算法、不引用它；注释里的 `X.py::f` 均指该快照中的规格出处。

口径：中枢在**含虚笔的全量笔列表**上构造（对齐 cal_seg_and_zs 的行为，不预先过滤
is_sure=False 的笔），中枢自身带 is_sure（来自所属线段，CZS.is_sure 同义），
"要不要采信未确认中枢"的决策留给消费方。

纯函数边界：公开入口 build_centers / analyze_centers 不修改入参（bis/segs 只读，
内部转成不可变 _Bi），可变状态只存活于一次调用内。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

DIR_UP = "up"
DIR_DOWN = "down"
ZS_ALGOS = ("normal",)              # over_seg / auto 档本仓库不支持（见 __init__ 校验）
ZS_COMBINE_MODES = ("zs", "peak")


@dataclass(frozen=True)
class ZsConfig:
    """中枢算法配置。默认值对齐 chan.py 的 CChanConfig 默认档（ZS/ZSConfig.py::CZSConfig）。"""

    need_combine: bool = True       # 是否做相邻中枢合并
    zs_combine_mode: str = "zs"     # "zs"=按中枢区间重叠合并；"peak"=按笔极值区间重叠
    one_bi_zs: bool = False         # 是否允许单笔中枢
    zs_algo: str = "normal"         # 段内构造（chan.py 另有 over_seg/auto，本仓库不支持）


@dataclass(frozen=True)
class _Bi:
    """中枢层用到的笔投影（不可变）。high/low 与 CBi._high()/_low() 同义：
    向上笔 high=终点价、low=起点价，向下笔反之——即端点价格的极值。"""

    idx: int
    dir: str
    high: float
    low: float
    seg_idx: int


def _has_overlap(low1: float, high1: float, low2: float, high2: float, equal: bool = False) -> bool:
    """区间重叠判定（Common/func_util.py::has_overlap）。"""
    return (high2 >= low1 and high1 >= low2) if equal else (high2 > low1 and high1 > low2)


def _revert_dir(direction: str) -> str:
    return DIR_DOWN if direction == DIR_UP else DIR_UP


# ========== 单个中枢（ZS/ZS.py::CZS）==========

class _Zs:
    """中枢的构造态。区间 low/high 只由构造元素决定；try_add_to_end 只延伸终点与
    peak 区间，不改区间（CZS.try_add_to_end 在 one_bi_zs 时才改区间）。"""

    def __init__(self, items: Sequence[_Bi], is_sure: bool):
        self.is_sure = is_sure
        self.sub_count = 0                      # 参与合并的子中枢数（0 = 未发生过合并）
        self.begin_bi = items[0]
        self.end_bi = items[0]
        self._update_range(items)
        self.peak_high = float("-inf")
        self.peak_low = float("inf")
        for item in items:
            self._update_end(item)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def _update_range(self, items: Sequence[_Bi]) -> None:
        self.low = max(bi.low for bi in items)      # ZD：诸笔低点的最大值
        self.high = min(bi.high for bi in items)    # ZG：诸笔高点的最小值

    def _update_end(self, item: _Bi) -> None:
        self.end_bi = item
        self.peak_low = min(self.peak_low, item.low)
        self.peak_high = max(self.peak_high, item.high)

    def is_one_bi_zs(self) -> bool:
        return self.begin_bi.idx == self.end_bi.idx

    def in_range(self, bi: _Bi) -> bool:
        return _has_overlap(self.low, self.high, bi.low, bi.high)

    def try_add_to_end(self, bi: _Bi, one_bi_zs: bool) -> bool:
        if not self.in_range(bi):
            return False
        if one_bi_zs and self.is_one_bi_zs():
            self._update_range([self.begin_bi, bi])
        self._update_end(bi)
        return True

    def combine(self, other: "_Zs", mode: str) -> bool:
        """能否吞并后一个中枢（CZS.combine）：单笔中枢不参与；必须同属一条线段。"""
        if other.is_one_bi_zs() or self.begin_bi.seg_idx != other.begin_bi.seg_idx:
            return False
        if mode == "zs":
            overlap = _has_overlap(self.low, self.high, other.low, other.high, equal=True)
        else:
            overlap = _has_overlap(self.peak_low, self.peak_high, other.peak_low, other.peak_high)
        if not overlap:
            return False
        self._do_combine(other)
        return True

    def _do_combine(self, other: "_Zs") -> None:
        """区间取并集（不是交集）、终点与出笔取后者——CZS.do_combine。"""
        self.sub_count = (self.sub_count or 1) + 1
        self.low = min(self.low, other.low)
        self.high = max(self.high, other.high)
        self.peak_low = min(self.peak_low, other.peak_low)
        self.peak_high = max(self.peak_high, other.peak_high)
        self.end_bi = other.end_bi


# ========== 中枢列表（ZS/ZSList.py::CZSList）==========

class _ZsBuilder:
    """从零构建中枢列表。chan.py 是增量更新（弹掉最后一个确定线段之后的中枢再重算），
    确定线段内的中枢一旦成立不再变化，故"对最终笔/线段列表整体重算"与其最终态等价。"""

    def __init__(self, bis: Sequence[_Bi], cfg: ZsConfig):
        self.bis = bis
        self.cfg = cfg
        self.zs_lst: List[_Zs] = []
        self.free: List[_Bi] = []       # 尚未凑成中枢的候选笔（CZSList.free_item_lst）

    def _try_construct(self, is_sure: bool) -> Optional[_Zs]:
        """zs_algo="normal"：one_bi_zs=False 时用最后两笔（同向、隔一笔）试构造。"""
        lst = self.free
        if not self.cfg.one_bi_zs:
            if len(lst) == 1:
                return None
            lst = lst[-2:]
        if min(bi.high for bi in lst) > max(bi.low for bi in lst):
            return _Zs(lst, is_sure)
        return None

    def _add_to_free(self, item: _Bi, is_sure: bool) -> None:
        if self.free and item.idx == self.free[-1].idx:      # 防笔新高/新低更新带来重复
            self.free = self.free[:-1]
        self.free = self.free + [item]
        zs = self._try_construct(is_sure)
        if zs is not None and zs.begin_bi.idx > 0:           # 禁止第一笔就是中枢起点
            self.zs_lst.append(zs)
            self.free = []
            self._try_combine()

    def _update(self, item: _Bi, is_sure: bool) -> None:
        if not self.free and self.zs_lst and \
                self.zs_lst[-1].try_add_to_end(item, self.cfg.one_bi_zs):
            self._try_combine()
            return
        self._add_to_free(item, is_sure)

    def _try_combine(self) -> None:
        if not self.cfg.need_combine:
            return
        while len(self.zs_lst) >= 2 and \
                self.zs_lst[-2].combine(self.zs_lst[-1], self.cfg.zs_combine_mode):
            self.zs_lst.pop()

    def _add_range(self, bis: Sequence[_Bi], seg_dir: str, seg_is_sure: bool) -> None:
        """段内笔区间 → 中枢（add_zs_from_bi_range）：只有与线段方向相反的笔参与构造；
        区间内第一笔强制走 free 列表，避免 try_add_to_end 延伸到上一线段的中枢里去。"""
        dealt = 0
        for bi in bis:
            if bi.dir == seg_dir:
                continue
            if dealt < 1:
                self._add_to_free(bi, seg_is_sure)
                dealt += 1
            else:
                self._update(bi, seg_is_sure)

    def build(self, segs: Sequence[Dict[str, Any]]) -> List[_Zs]:
        if not segs:            # 无线段时 cal_bi_zs 的 normal 分支不产出任何中枢
            return []
        for seg in segs:
            self.free = []
            self._add_range(self.bis[seg["start_bi_idx"]:seg["end_bi_idx"] + 1],
                            seg["dir"], seg["is_sure"])
        # 尚未成段的尾部笔：方向取最后线段的反向，中枢一律 is_sure=False
        self.free = []
        self._add_range(self.bis[segs[-1]["end_bi_idx"] + 1:],
                        _revert_dir(segs[-1]["dir"]), False)
        return self.zs_lst


# ========== 公开入口（纯函数）==========

def _seg_idx_of_bis(n_bis: int, segs: Sequence[Dict[str, Any]]) -> List[int]:
    """每一笔归属的线段序号；尚未成段的尾部笔记为 len(segs)（KLine_List.py::cal_seg）。"""
    owner = [len(segs)] * n_bis
    for seg_idx, seg in enumerate(segs):
        for bi_idx in range(seg["start_bi_idx"], min(seg["end_bi_idx"] + 1, n_bis)):
            owner[bi_idx] = seg_idx
    return owner


def _to_internal(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]]) -> List[_Bi]:
    owner = _seg_idx_of_bis(len(bis), segs)
    return [_Bi(idx=i, dir=b["dir"], high=float(b["high"]), low=float(b["low"]),
                seg_idx=owner[i])
            for i, b in enumerate(bis)]


def _export(zs: _Zs, bis: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    start, end = zs.begin_bi.idx, zs.end_bi.idx
    return {"zg": zs.high, "zd": zs.low, "mid": zs.mid,
            "peak_high": zs.peak_high, "peak_low": zs.peak_low,
            "start_bi_idx": start, "end_bi_idx": end,
            "start_idx": bis[start]["start_idx"], "end_idx": bis[end]["end_idx"],
            "bi_count": end - start + 1, "seg_idx": zs.begin_bi.seg_idx,
            "is_sure": zs.is_sure,
            "bi_in_idx": start - 1 if start > 0 else None,
            "bi_out_idx": end + 1 if end + 1 < len(bis) else None,
            "sub_count": zs.sub_count}


def build_centers(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]],
                  config: Optional[ZsConfig] = None) -> List[Dict[str, Any]]:
    """笔列表 + 线段列表 → 中枢列表。纯函数：不修改 bis / segs。"""
    cfg = config or ZsConfig()
    if cfg.zs_algo not in ZS_ALGOS:
        raise ValueError(f"未知 zs_algo={cfg.zs_algo}，本仓库只支持 {ZS_ALGOS}")
    if cfg.zs_combine_mode not in ZS_COMBINE_MODES:
        raise ValueError(f"未知 zs_combine_mode={cfg.zs_combine_mode}，应为 {ZS_COMBINE_MODES} 之一")
    if not bis or not segs:
        return []
    zs_lst = _ZsBuilder(_to_internal(bis, segs), cfg).build(segs)
    return [_export(zs, bis) for zs in zs_lst]


def analyze_centers(bis: Sequence[Dict[str, Any]], segs: Sequence[Dict[str, Any]],
                    config: Optional[ZsConfig] = None) -> Dict[str, Any]:
    """中枢层汇总：{"centers", "center_count", "sure_center_count", "last_center"}。纯函数。"""
    centers = build_centers(bis, segs, config)
    return {"centers": centers, "center_count": len(centers),
            "sure_center_count": sum(1 for c in centers if c["is_sure"]),
            "last_center": centers[-1] if centers else None}
