"""chan_center 单测：段内构造约束 / zs 模式合并 / bi_in-bi_out / 单笔中枢 / is_sure / 入参只读。

差分对齐（与 chan.py oracle 比中枢区间）在 test_chan_center_diff.py；本文件用手工构造的
笔+线段序列锁定规则本身，失败时能直接指到规则出处（third_party/chan_py_reference 下的
ZS/ZSList.py::cal_bi_zs、ZS/ZS.py::CZS.combine、KLine/KLine_List.py::update_zs_in_seg）。
"""

import copy
import importlib.util
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cc = _load("chan_center")


def _bis(prices, sure=True):
    """折点价格序列 → 笔列表（相邻折点一笔，方向由涨跌决定），K线索引取等距假值。"""
    return [{"dir": "up" if b > a else "down", "start_idx": i * 5, "end_idx": (i + 1) * 5,
             "start_price": a, "end_price": b, "high": max(a, b), "low": min(a, b),
             "is_sure": sure, "used_to_be_sure": sure}
            for i, (a, b) in enumerate(zip(prices, prices[1:]))]


def _seg(start_bi_idx, end_bi_idx, direction="up", is_sure=True):
    return {"dir": direction, "start_bi_idx": start_bi_idx, "end_bi_idx": end_bi_idx,
            "is_sure": is_sure}


# 8 笔上升线段。两组下降笔各成一个中枢，第二个中枢的下沿正好压在第一个的上沿上：
#   笔1[10,11] 笔3[10,12] → 中枢A=[zd 10, zg 11]
#   笔5[11,13] 笔7[10.5,11.5] → 中枢B=[zd 11, zg 11.5]，与 A 在 11 处相接 → zs 模式合并
MERGE_PRICES = [9, 11, 10, 12, 10, 13, 11, 11.5, 10.5]
# 同形态，只把最后两笔整体抬高：中枢B=[11.5, 12.5] 与 A=[10,11] 不相接 → 不合并
NO_MERGE_PRICES = [9, 11, 10, 12, 10, 13, 11, 12.5, 11.5]


# ========== 段内构造约束 ==========

def test_center_needs_two_counter_dir_bis_inside_one_seg():
    """中枢元素只取与线段方向相反的笔：上升线段里要两笔下降笔才够（cal_bi_zs/add_zs_from_bi_range）。"""
    bis = _bis(MERGE_PRICES)
    centers = cc.build_centers(bis, [_seg(0, 4)])
    assert len(centers) == 1
    assert (centers[0]["start_bi_idx"], centers[0]["end_bi_idx"]) == (1, 3)


def test_overlapping_bis_split_across_segs_form_no_center():
    """同一组重叠笔被切进两条线段后不再成中枢——旧滑窗实现会跨线段凑出中枢，这是 T3 修掉的偏差。"""
    bis = _bis(MERGE_PRICES)
    assert cc.build_centers(bis, [_seg(0, 4)]), "同一线段内应有中枢（对照组）"
    split = [_seg(0, 2), _seg(3, 4)]          # 笔1 与 笔3 分属两段 → 各段只剩一笔反向笔
    assert cc.build_centers(bis, split) == []


def test_no_seg_means_no_center():
    """无线段时参考实现 cal_bi_zs 的 normal 分支不产出任何中枢。"""
    assert cc.build_centers(_bis(MERGE_PRICES), []) == []
    assert cc.build_centers([], [_seg(0, 4)]) == []


# ========== 合并（zs 模式）==========

def test_zs_mode_combine_merges_touching_centers():
    """区间相接（has_overlap equal=True）即合并；合并后区间取并集、终点取后者（CZS.do_combine）。"""
    bis = _bis(MERGE_PRICES)
    centers = cc.build_centers(bis, [_seg(0, 7)])
    assert len(centers) == 1
    c = centers[0]
    assert (c["zd"], c["zg"]) == (10, 11.5)          # 并集：min(10,11) / max(11,11.5)
    assert (c["start_bi_idx"], c["end_bi_idx"], c["bi_count"]) == (1, 7, 7)
    assert (c["peak_low"], c["peak_high"]) == (10, 13)
    assert c["mid"] == (10 + 11.5) / 2
    assert c["sub_count"] == 2                       # 由两个子中枢合并而来


def test_zs_mode_no_combine_when_ranges_disjoint():
    """区间不相接 → 两个中枢各自独立（同一条线段内也不合并）。"""
    bis = _bis(NO_MERGE_PRICES)
    centers = cc.build_centers(bis, [_seg(0, 7)])
    assert [(c["zd"], c["zg"]) for c in centers] == [(10, 11), (11.5, 12.5)]
    assert [c["sub_count"] for c in centers] == [0, 0]


def test_combine_disabled_by_config():
    bis = _bis(MERGE_PRICES)
    centers = cc.build_centers(bis, [_seg(0, 7)], cc.ZsConfig(need_combine=False))
    assert [(c["zd"], c["zg"]) for c in centers] == [(10, 11), (11, 11.5)]


# ========== bi_in / bi_out ==========

def test_bi_in_and_bi_out_point_to_neighbour_bis():
    """进中枢笔 = 首笔前一笔，出中枢笔 = 末笔后一笔；末笔已是最后一笔时 bi_out 为 None
    （update_zs_in_seg::set_bi_in/set_bi_out）。"""
    bis = _bis(NO_MERGE_PRICES)
    first, second = cc.build_centers(bis, [_seg(0, 7)])
    assert (first["bi_in_idx"], first["bi_out_idx"]) == (0, 4)
    assert bis[first["bi_in_idx"]]["dir"] == "up" and bis[first["bi_out_idx"]]["dir"] == "up"
    assert (second["start_bi_idx"], second["end_bi_idx"]) == (5, 7)
    assert (second["bi_in_idx"], second["bi_out_idx"]) == (4, None)


def test_center_never_starts_at_first_bi():
    """第一笔不能是中枢起点（add_to_free_lst 的 begin_bi.idx > 0 约束）→ bi_in 必然存在。"""
    bis = _bis([9, 11, 10, 12, 10, 13])           # 下降线段：反向笔为 0/2/4
    centers = cc.build_centers(bis, [_seg(0, 4, direction="down")])
    assert all(c["start_bi_idx"] > 0 and c["bi_in_idx"] is not None for c in centers)


# ========== 单笔中枢 ==========

def test_one_bi_zs_disabled_by_default():
    """one_bi_zs=False（默认）时单笔构不成中枢；打开该档才产出单笔中枢（try_construct_zs）。"""
    bis = _bis(MERGE_PRICES[:4])              # 只留 3 笔，避免尾部未成段区间另产中枢
    assert cc.build_centers(bis, [_seg(0, 2)]) == []
    one_bi = cc.build_centers(bis, [_seg(0, 2)], cc.ZsConfig(one_bi_zs=True))
    assert len(one_bi) == 1
    assert one_bi[0]["start_bi_idx"] == one_bi[0]["end_bi_idx"] == 1


# ========== is_sure（含虚笔口径）==========

def test_center_is_sure_follows_seg():
    bis = _bis(MERGE_PRICES)
    assert cc.build_centers(bis, [_seg(0, 7)])[0]["is_sure"] is True
    assert cc.build_centers(bis, [_seg(0, 7, is_sure=False)])[0]["is_sure"] is False


def test_center_after_last_seg_is_not_sure():
    """尚未成段的尾部笔按最后线段的反向构造，中枢一律 is_sure=False，seg_idx = len(segs)。"""
    bis = _bis(MERGE_PRICES)
    centers = cc.build_centers(bis, [_seg(0, 2)])
    assert [(c["start_bi_idx"], c["end_bi_idx"]) for c in centers] == [(4, 6)]
    assert centers[0]["is_sure"] is False
    assert centers[0]["seg_idx"] == 1


def test_virtual_bis_participate_in_construction():
    """口径决定：中枢在含虚笔的全量笔列表上构造，不预先过滤 is_sure=False 的笔
    （对齐 cal_seg_and_zs）；过滤与否留给消费方。"""
    sure_bis = _bis(MERGE_PRICES)
    with_virtual = [dict(b) for b in sure_bis]
    with_virtual[7] = {**with_virtual[7], "is_sure": False, "used_to_be_sure": False}
    assert cc.build_centers(with_virtual, [_seg(0, 7)]) == cc.build_centers(sure_bis, [_seg(0, 7)])


# ========== 契约与纯函数 ==========

def test_build_centers_does_not_mutate_input():
    bis, segs = _bis(MERGE_PRICES), [_seg(0, 7)]
    bis_snapshot, segs_snapshot = copy.deepcopy(bis), copy.deepcopy(segs)
    cc.build_centers(bis, segs)
    assert bis == bis_snapshot and segs == segs_snapshot


def test_center_fields_and_indices():
    bis = _bis(MERGE_PRICES)
    center = cc.build_centers(bis, [_seg(0, 7)])[0]
    assert set(center) == {"zg", "zd", "mid", "peak_high", "peak_low", "start_bi_idx",
                           "end_bi_idx", "start_idx", "end_idx", "bi_count", "seg_idx",
                           "is_sure", "bi_in_idx", "bi_out_idx", "sub_count"}
    assert center["start_idx"] == bis[center["start_bi_idx"]]["start_idx"]
    assert center["end_idx"] == bis[center["end_bi_idx"]]["end_idx"]
    assert center["zg"] > center["zd"]


def test_unknown_algo_and_combine_mode_rejected():
    bis, segs = _bis(MERGE_PRICES), [_seg(0, 7)]
    for bad in (cc.ZsConfig(zs_algo="over_seg"), cc.ZsConfig(zs_combine_mode="peek")):
        try:
            cc.build_centers(bis, segs, bad)
        except ValueError:
            continue
        raise AssertionError(f"未知配置应报错: {bad}")


def test_analyze_centers_summary():
    bis, segs = _bis(NO_MERGE_PRICES), [_seg(0, 7)]
    result = cc.analyze_centers(bis, segs)
    assert result["center_count"] == len(result["centers"]) == 2
    assert result["sure_center_count"] == 2
    assert result["last_center"] == result["centers"][-1]
    assert cc.analyze_centers([], []) == {"centers": [], "center_count": 0,
                                          "sure_center_count": 0, "last_center": None}
