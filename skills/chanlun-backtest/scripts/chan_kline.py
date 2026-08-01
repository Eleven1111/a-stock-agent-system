#!/usr/bin/env python3
"""
缠论 K 线层 — 去包含 → 分型（4 档有效性检查）→ 笔（含虚笔 is_sure）
==================================================================
从 chan_structure.py 拆出的底层结构模块。算法规格对齐 Vespa314/chan.py（pinned 429d6ed，
MIT）：参照实现快照在仓库 third_party 目录下，**仅测试可 import**（生产引用会被
tests/test_chan_reference_guard.py 静态拦截），故本文件只复刻算法、不引用它；下文注释里的
`X.py::f` 均指该快照中的规格出处。相对旧实现（顶底交替 + 固定 4 根间隔）的三点升级：

1. 分型有效性检查 4 档（strict/half/loss/totally）——KLine/KLine.py::check_fx_valid；
2. 笔成立三条件：跨度 satisfy_bi_span + 分型有效 + 端点为区间峰值 end_is_peak——
   Bi/BiList.py::can_make_bi；
3. 虚笔：末段未被后续分型确认的走势以 is_sure=False 输出，后续K线可延伸或撤销
   （try_add_virtual_bi / delete_virtual_bi）——"信号首次可观察时点"因此成为模块内建属性，
   不再依赖下游 O(n²) 前缀重放。

纯函数边界：公开入口（merge_klines / find_fractals / build_bis / analyze_klines）不修改入参，
bars 只读；内部状态机 _BiMachine 只操作 analyze_klines 局部创建的合并K线列表。
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

# 分型/方向常量（沿用仓内既有字符串枚举风格，不引入 Enum 依赖）
FX_TOP = "top"
FX_BOTTOM = "bottom"
FX_NONE = ""
DIR_UP = "up"
DIR_DOWN = "down"
_COMBINE = "combine"
FX_CHECK_METHODS = ("strict", "half", "loss", "totally")


@dataclass(frozen=True)
class BiConfig:
    """笔算法配置。默认值对齐 chan.py 的 CChanConfig 默认档（ChanConfig.py 第 21-28 行），
    只有 fx_check 由本仓库显式钉死为 strict（升级方案 §4 T1 的验收口径）。"""

    fx_check: str = "strict"        # 分型有效性检查：strict/half/loss/totally
    bi_algo: str = "normal"         # "fx" 表示不校验跨度（chan.py 的宽松档）
    is_strict: bool = True          # 严格笔：合并K线跨度 >= 4
    gap_as_kl: bool = False         # 缺口是否折算成一根K线参与跨度
    bi_end_is_peak: bool = True     # 笔端点必须是区间峰值
    bi_allow_sub_peak: bool = True  # True 时禁用 update_peak 次高点改笔
    cal_virtual: bool = True        # 是否输出虚笔（is_sure=False）


def _new_klc(high: float, low: float, idx: int, direction: str) -> Dict[str, Any]:
    """新建一根合并K线。high_idx/low_idx 指向贡献极值的原始K线索引（对外契约）。"""
    return {"high": high, "low": low, "dir": direction, "idx": idx, "high_idx": idx,
            "low_idx": idx, "count": 1, "raw_high": high, "raw_low": low}


def _test_combine(klc: Dict[str, Any], high: float, low: float) -> str:
    """返回 combine（存在包含关系，含互相包含）/ up / down。"""
    if (klc["high"] >= high and klc["low"] <= low) or (klc["high"] <= high and klc["low"] >= low):
        return _COMBINE
    if klc["high"] > high and klc["low"] > low:
        return DIR_DOWN
    if klc["high"] < high and klc["low"] < low:
        return DIR_UP
    raise ValueError(f"无法判定包含关系: klc={klc['high']}/{klc['low']} bar={high}/{low}")


def _try_add(klc: Dict[str, Any], high: float, low: float, idx: int) -> str:
    """尝试把第 idx 根原始K线并入 klc。返回 combine 表示已并入（klc 就地更新）。"""
    direction = _test_combine(klc, high, low)
    if direction != _COMBINE:
        return direction
    klc["count"] += 1
    klc["idx"] = idx
    klc["raw_high"] = max(klc["raw_high"], high)
    klc["raw_low"] = min(klc["raw_low"], low)
    up = klc["dir"] == DIR_UP          # 向上取"高高"，向下取"低低"
    # 一字K线且极值与本合并K线相同时不更新高低点（KLine_Combiner.try_add 的一字特判）
    skip = high == low and (high == klc["high"] if up else low == klc["low"])
    if not skip:
        pick = max if up else min
        klc["high"], klc["low"] = pick(klc["high"], high), pick(klc["low"], low)
    # get_peak_klu 取"最后一根取到极值的原始K线"，故等值时后者覆盖前者
    if high == klc["high"]:
        klc["high_idx"] = idx
    if low == klc["low"]:
        klc["low_idx"] = idx
    return _COMBINE


def merge_klines(bars: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去除K线包含关系。纯函数：不修改 bars。"""
    return analyze_klines(bars)["merged"]


def _fx_at(prev: Dict[str, Any], cur: Dict[str, Any], nxt: Dict[str, Any]) -> str:
    """顶/底分型：高低点须同时严格突破左右邻居（KLine_Combiner.py::update_fx 非 exclude 分支）。"""
    if max(prev["high"], nxt["high"]) < cur["high"] and max(prev["low"], nxt["low"]) < cur["low"]:
        return FX_TOP
    if min(prev["high"], nxt["high"]) > cur["high"] and min(prev["low"], nxt["low"]) > cur["low"]:
        return FX_BOTTOM
    return FX_NONE


def _fractal(klc: Dict[str, Any], mi: int, kind: str) -> Dict[str, Any]:
    """分型的对外表示（旧契约）：type/mi(合并索引)/idx(原始K线索引)/price。"""
    top = kind == FX_TOP
    return {"type": kind, "mi": mi, "idx": klc["high_idx" if top else "low_idx"],
            "price": klc["high" if top else "low"]}


def find_fractals(merged: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """在合并K线上找顶/底分型。首尾两根不参与（无左右邻居），与 chan.py 一致。"""
    kinds = [_fx_at(merged[i - 1], merged[i], merged[i + 1]) for i in range(1, len(merged) - 1)]
    return [_fractal(merged[i + 1], i + 1, kind) for i, kind in enumerate(kinds) if kind]


def _has_gap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    """两根合并K线之间是否有缺口（用原始K线极值，相等不算缺口）——KLine.has_gap_with_next。"""
    return not (right["raw_high"] >= left["raw_low"] and left["raw_high"] >= right["raw_low"])


# ========== 笔状态机（Bi/BiList.py 的等价移植）==========

class _BiMachine:
    """CBiList 的等价状态机：逐根合并K线推进，维护确定笔与虚笔。内部可变，仅存活于
    analyze_klines 一次调用内；kl/fx 由驱动函数持续追加，本类只读。
    笔的内部表示：{dir, begin, end(合并索引), is_sure, used_to_be_sure, sure_end}。"""

    def __init__(self, cfg: BiConfig, kl: List[Dict[str, Any]], fx: List[str]):
        self.cfg = cfg
        self.kl = kl
        self.fx = fx
        self.bis: List[Dict[str, Any]] = []
        self.last_end: Optional[int] = None
        self.free: List[int] = []   # 第一笔画出前的分型缓存（try_create_first_bi）

    def _begin_val(self, bi: Dict[str, Any]) -> float:
        return self.kl[bi["begin"]]["low" if bi["dir"] == DIR_UP else "high"]

    def _end_val(self, bi: Dict[str, Any]) -> float:
        return self.kl[bi["end"]]["high" if bi["dir"] == DIR_UP else "low"]

    def _last_end_klu(self) -> Optional[int]:
        """最后一笔终点对应的原始K线索引（get_last_klu_of_last_bi）。"""
        if not self.bis:
            return None
        bi = self.bis[-1]
        return self.kl[bi["end"]]["high_idx" if bi["dir"] == DIR_UP else "low_idx"]

    def _check_fx_valid(self, i: int, j: int, for_virtual: bool) -> bool:
        """i 为已确认分型的合并索引，j 为候选终点。规格：KLine/KLine.py::check_fx_valid。

        规格里 FX_TYPE.BOTTOM 段与 FX_TYPE.TOP 段逐行镜像（高低互换、比较方向反转），
        这里把底分型的价格取负并交换高低（hi/lo 两个取值器），统一走顶分型公式，等价。
        """
        top = self.fx[i] == FX_TOP
        if not top and self.fx[i] != FX_BOTTOM:
            raise ValueError("只有顶/底分型可以做 check_fx_valid")
        if for_virtual and self.kl[j]["dir"] != (DIR_DOWN if top else DIR_UP):
            return False
        hi = (lambda k: self.kl[k]["high"]) if top else (lambda k: -self.kl[k]["low"])
        lo = (lambda k: self.kl[k]["low"]) if top else (lambda k: -self.kl[k]["high"])
        method = self.cfg.fx_check
        if method == "half":            # 只看候选点左邻 + 分型右邻
            cand_high, cur_low = max(hi(j - 1), hi(j)), min(lo(i), lo(i + 1))
        elif method == "loss":          # 只看两个分型自身
            cand_high, cur_low = hi(j), lo(i)
        else:                           # strict / totally：两侧邻居都看（虚笔时右邻尚不存在）
            cand_high = max(hi(j - 1), hi(j)) if for_virtual else max(hi(j - 1), hi(j), hi(j + 1))
            cur_low = min(lo(i - 1), lo(i), lo(i + 1))
        if method == "totally":         # 最严：分型低点必须完全高于候选区间高点
            return lo(i) > cand_high
        return hi(i) > cand_high and lo(j) < cur_low

    def _end_is_peak(self, last_end: int, cur_end: int) -> bool:
        """笔的终点必须是区间内的极值（BiList.end_is_peak，等值放行）。"""
        kind = self.fx[last_end]
        if kind not in (FX_TOP, FX_BOTTOM):
            return True
        key = "high" if kind == FX_BOTTOM else "low"    # 底起笔看高点，顶起笔看低点
        sign = 1 if kind == FX_BOTTOM else -1
        thred = sign * self.kl[cur_end][key]
        return all(sign * self.kl[i][key] <= thred for i in range(last_end + 1, cur_end))

    def _klc_span(self, j: int, last_end: int) -> int:
        span = j - last_end
        if not self.cfg.gap_as_kl or span >= 4:
            return span
        return span + sum(1 for i in range(last_end, j) if _has_gap(self.kl[i], self.kl[i + 1]))

    def _satisfy_bi_span(self, j: int, last_end: int) -> bool:
        span = self._klc_span(j, last_end)
        if self.cfg.is_strict:
            return span >= 4
        cnt, i = 0, last_end + 1
        while i < len(self.kl):
            cnt += self.kl[i]["count"]
            if i + 1 >= len(self.kl):   # 尾部虚笔时下一根尚不存在 → 不成笔
                return False
            if i + 1 < j:
                i += 1
            else:
                break
        return span >= 3 and cnt >= 3

    def _can_make_bi(self, j: int, last_end: int, for_virtual: bool = False) -> bool:
        """成笔三条件（can_make_bi）：跨度 + 分型有效性 + 端点是区间峰值。"""
        if self.cfg.bi_algo != "fx" and not self._satisfy_bi_span(j, last_end):
            return False
        if not self._check_fx_valid(last_end, j, for_virtual):
            return False
        return self._end_is_peak(last_end, j) if self.cfg.bi_end_is_peak else True

    def _check_bi(self, direction: str, begin: int, end: int) -> None:
        ok = (self.kl[begin]["high"] > self.kl[end]["low"]) if direction == DIR_DOWN \
            else (self.kl[begin]["low"] < self.kl[end]["high"])
        if not ok:
            raise ValueError(f"笔的方向与首尾位置不一致: {direction} {begin}->{end}")

    def _add_new_bi(self, begin: int, end: int, is_sure: bool = True) -> None:
        if self.fx[begin] not in (FX_TOP, FX_BOTTOM):
            raise ValueError("建笔起点必须是顶/底分型")
        direction = DIR_UP if self.fx[begin] == FX_BOTTOM else DIR_DOWN
        self._check_bi(direction, begin, end)
        self.bis.append({"dir": direction, "begin": begin, "end": end,
                         "is_sure": is_sure, "used_to_be_sure": is_sure, "sure_end": []})

    def _update_new_end(self, bi: Dict[str, Any], end: int) -> None:
        self._check_bi(bi["dir"], bi["begin"], end)
        bi["end"] = end

    def _update_virtual_end(self, bi: Dict[str, Any], end: int) -> None:
        bi["sure_end"].append(bi["end"])
        self._update_new_end(bi, end)
        bi["used_to_be_sure"] = bi["is_sure"]
        bi["is_sure"] = False

    def _delete_virtual_bi(self) -> None:
        """撤销虚笔：有 sure_end 则回滚到确定终点（并把多余确定终点补成新笔），否则整笔删除。"""
        if self.bis and not self.bis[-1]["is_sure"]:
            bi = self.bis[-1]
            sure_ends = list(bi["sure_end"])
            if sure_ends:
                bi["is_sure"] = bi["used_to_be_sure"] = True
                self._update_new_end(bi, sure_ends[0])
                bi["sure_end"] = []
                self.last_end = bi["end"]
                for sure_end in sure_ends[1:]:
                    self._add_new_bi(self.last_end, sure_end, is_sure=True)
                    self.last_end = self.bis[-1]["end"]
            else:
                self.bis.pop()
        self.last_end = self.bis[-1]["end"] if self.bis else None

    def _try_update_end(self, j: int, for_virtual: bool = False) -> bool:
        if not self.bis:
            return False
        last_bi = self.bis[-1]
        # 虚笔阶段用合并K线的方向代替尚未成型的分型（try_update_end 的 check_top/check_bottom）
        is_top = self.kl[j]["dir"] == DIR_UP if for_virtual else self.fx[j] == FX_TOP
        is_bottom = self.kl[j]["dir"] == DIR_DOWN if for_virtual else self.fx[j] == FX_BOTTOM
        extend_up = last_bi["dir"] == DIR_UP and is_top and self.kl[j]["high"] >= self._end_val(last_bi)
        extend_down = last_bi["dir"] == DIR_DOWN and is_bottom and self.kl[j]["low"] <= self._end_val(last_bi)
        if not (extend_up or extend_down):
            return False
        self._update_virtual_end(last_bi, j) if for_virtual else self._update_new_end(last_bi, j)
        self.last_end = j
        return True

    def _can_update_peak(self, j: int) -> bool:
        """能否用次高/次低点改写最后一笔（BiList.can_update_peak；各条件均为纯判据，可合并）。"""
        if self.cfg.bi_allow_sub_peak or len(self.bis) < 2:
            return False
        last, prev = self.bis[-1], self.bis[-2]
        if last["dir"] == DIR_DOWN and (self.kl[j]["high"] < self._begin_val(last)
                                        or self._end_val(last) < self._begin_val(prev)):
            return False
        if last["dir"] == DIR_UP and (self.kl[j]["low"] > self._begin_val(last)
                                      or self._end_val(last) > self._begin_val(prev)):
            return False
        return self._end_is_peak(prev["begin"], j)

    def _update_peak(self, j: int, for_virtual: bool = False) -> bool:
        """次高/次低点改笔：删掉最后一笔后尝试把前一笔的终点延伸到 j。"""
        if not self._can_update_peak(j):
            return False
        dropped = self.bis.pop()
        if not self._try_update_end(j, for_virtual=for_virtual):
            self.bis.append(dropped)
            return False
        if for_virtual:
            self.bis[-1]["sure_end"].append(dropped["end"])
        return True

    def _try_create_first_bi(self, j: int) -> bool:
        for free_idx in self.free:
            if self.fx[free_idx] == self.fx[j]:
                continue
            if self._can_make_bi(j, free_idx):
                self._add_new_bi(free_idx, j)
                self.last_end = j
                return True
        self.free.append(j)
        self.last_end = j
        return False

    def _update_bi_sure(self, j: int) -> bool:
        prev_end_klu = self._last_end_klu()
        self._delete_virtual_bi()
        if self.fx[j] == FX_NONE:
            return prev_end_klu != self._last_end_klu()
        if self.last_end is None or not self.bis:
            return self._try_create_first_bi(j)
        if self.fx[j] == self.fx[self.last_end]:
            return self._try_update_end(j)
        if self._can_make_bi(j, self.last_end):
            self._add_new_bi(self.last_end, j)
            self.last_end = j
            return True
        if self._update_peak(j):
            return True
        return prev_end_klu != self._last_end_klu()

    def try_add_virtual_bi(self, j: int, need_del_end: bool = False) -> bool:
        """把尚未被分型确认的末段挂成虚笔（延伸已有虚笔 / 新建 is_sure=False 的笔）。"""
        if need_del_end:
            self._delete_virtual_bi()
        if not self.bis or j == self.bis[-1]["end"]:
            return False
        last_bi = self.bis[-1]
        beyond_up = last_bi["dir"] == DIR_UP and self.kl[j]["high"] >= self.kl[last_bi["end"]]["high"]
        beyond_down = last_bi["dir"] == DIR_DOWN and self.kl[j]["low"] <= self.kl[last_bi["end"]]["low"]
        if beyond_up or beyond_down:
            self._update_virtual_end(last_bi, j)
            return True
        cur = j
        while cur >= 0 and cur > self.bis[-1]["end"]:
            if self._can_make_bi(cur, self.bis[-1]["end"], for_virtual=True):
                self._add_new_bi(self.last_end, cur, is_sure=False)
                return True
            if self._update_peak(cur, for_virtual=True):
                return True
            cur -= 1
        return False

    def update_bi(self, j: int, last: int) -> None:
        """j = 倒数第二根合并K线，last = 最后一根（KLine_List.add_single_klu 的调用契约）。"""
        self._update_bi_sure(j)
        if self.cfg.cal_virtual:
            self.try_add_virtual_bi(last)


# ========== 公开入口（纯函数）==========

def _export_bi(bi: Dict[str, Any], kl: List[Dict[str, Any]]) -> Dict[str, Any]:
    """内部笔 → 对外契约（idx 指原始K线索引，与 chan_structure 旧契约一致）。"""
    up = bi["dir"] == DIR_UP
    begin, end = kl[bi["begin"]], kl[bi["end"]]
    start_price, end_price = (begin["low"], end["high"]) if up else (begin["high"], end["low"])
    return {"dir": bi["dir"],
            "start_idx": begin["low_idx"] if up else begin["high_idx"],
            "end_idx": end["high_idx"] if up else end["low_idx"],
            "start_price": start_price, "end_price": end_price,
            "high": max(start_price, end_price), "low": min(start_price, end_price),
            "is_sure": bi["is_sure"],
            # 曾经确定过的笔（后被虚笔延伸）在线段层仍算"确定证据"——CBi.is_used_to_be_sure
            "used_to_be_sure": bi["used_to_be_sure"]}


def analyze_klines(bars: Sequence[Dict[str, Any]], config: Optional[BiConfig] = None) -> Dict[str, Any]:
    """一次走完 去包含 → 分型 → 笔。返回 {"merged", "fx", "fractals", "bis"}。

    逐根喂入原始K线，复刻 chan.py KLine_List.add_single_klu 的推进顺序：新合并K线诞生时
    才为倒数第二根定分型并推进笔；被并入（combine）时只尝试更新虚笔。
    """
    cfg = config or BiConfig()
    if cfg.fx_check not in FX_CHECK_METHODS:
        raise ValueError(f"未知 fx_check={cfg.fx_check}，应为 {FX_CHECK_METHODS} 之一")

    kl: List[Dict[str, Any]] = []
    fx: List[str] = []
    machine = _BiMachine(cfg, kl, fx)
    for i, bar in enumerate(bars):
        high, low = float(bar["high"]), float(bar["low"])
        direction = _try_add(kl[-1], high, low, i) if kl else DIR_UP  # chan.py 首根缺省方向 UP
        if direction == _COMBINE:
            if cfg.cal_virtual:
                machine.try_add_virtual_bi(len(kl) - 1, need_del_end=True)
            continue
        kl.append(_new_klc(high, low, i, direction))
        fx.append(FX_NONE)
        if len(kl) >= 3:
            fx[-2] = _fx_at(kl[-3], kl[-2], kl[-1])
        if len(kl) >= 2:
            machine.update_bi(len(kl) - 2, len(kl) - 1)

    return {"merged": kl, "fx": fx,
            "fractals": [_fractal(kl[i], i, fx[i]) for i in range(len(kl)) if fx[i]],
            "bis": [_export_bi(bi, kl) for bi in machine.bis]}


def build_bis(bars: Sequence[Dict[str, Any]], config: Optional[BiConfig] = None) -> List[Dict[str, Any]]:
    """笔列表（含末段虚笔 is_sure=False）。纯函数：不修改 bars。"""
    return analyze_klines(bars, config)["bis"]
