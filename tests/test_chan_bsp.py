"""缠论买卖点层 chan_bsp — 六类买卖点判别 / 背驰度量族 / legacy 映射 / 入参只读。

用手工构造的笔+线段+中枢（而非真实K线）逼出每一类买卖点：这样每条断言都能指回
BSPointList.py 的具体判据，真实K线只在 test_chan_bsp_diff.py 里跑对齐率。
"""

import copy
import importlib.util
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
SCRIPTS = PROJ / "skills" / "chanlun-backtest" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cb = _load("chan_bsp")
core = _load("chan_bsp_core")
cs = _load("chan_structure")

BARS_PER_BI = 4          # 每笔占 4 根原始K线，笔 i 的原始区间 = [4i, 4i+3]


# ========== 构造工具 ==========

def _bi(idx, direction, begin, end):
    return {"dir": direction, "start_price": begin, "end_price": end,
            "high": max(begin, end), "low": min(begin, end),
            "start_idx": idx * BARS_PER_BI, "end_idx": idx * BARS_PER_BI + BARS_PER_BI - 1,
            "is_sure": True, "used_to_be_sure": True}


def _seg(direction, start_bi, end_bi, is_sure=True):
    return {"dir": direction, "start_bi_idx": start_bi, "end_bi_idx": end_bi,
            "is_sure": is_sure}


def _center(zg, zd, start_bi, end_bi, bi_in, bi_out, seg_idx, peak=None):
    peak_high, peak_low = peak or (zg, zd)
    return {"zg": zg, "zd": zd, "peak_high": peak_high, "peak_low": peak_low,
            "start_bi_idx": start_bi, "end_bi_idx": end_bi, "bi_count": end_bi - start_bi + 1,
            "bi_in_idx": bi_in, "bi_out_idx": bi_out, "seg_idx": seg_idx}


def _bars(bis, hist_map=None):
    """覆盖全部笔的原始K线 + MACD 柱（hist_map: 原始K线索引 → 柱值，其余为 0）。"""
    n = max(b["end_idx"] for b in bis) + 1
    bars = [{"high": 100.0, "low": 90.0, "close": 95.0, "open": 95.0, "date": f"2026-01-{i + 1:02d}"}
            for i in range(n)]
    hist = [(hist_map or {}).get(i, 0.0) for i in range(n)]
    return bars, hist


def _types(bsps):
    return sorted({t for b in bsps for t in b["types"]}, key=core.BSP_TYPES.index)


# ---- 场景 A：一类买卖点（趋势背驰）——下降线段末笔跌破中枢下沿 ----

def _scene_trend_bsp1():
    bis = [_bi(0, "down", 20, 15), _bi(1, "up", 15, 17), _bi(2, "down", 17, 14.5),
           _bi(3, "up", 14.5, 16.5), _bi(4, "down", 16.5, 12)]
    segs = [_seg("down", 0, 4)]
    centers = [_center(16.5, 15, start_bi=1, end_bi=3, bi_in=0, bi_out=4, seg_idx=0,
                       peak=(17, 14.5))]
    return bis, segs, centers


# ---- 场景 B：二类 / 类二（在场景 A 的一类买点之后回抽）----

def _scene_bsp2():
    bis, _, centers = _scene_trend_bsp1()
    bis = bis + [_bi(5, "up", 12, 16), _bi(6, "down", 16, 13),
                 _bi(7, "up", 13, 15), _bi(8, "down", 15, 13.5)]
    segs = [_seg("down", 0, 4), _seg("up", 5, 8)]
    return bis, segs, centers


# ---- 场景 C：三类买点（中枢后 T3A）——下一线段首个多笔中枢的出笔之后一笔不回中枢 ----

def _scene_bsp3a():
    bis = [_bi(0, "up", 10, 12), _bi(1, "down", 12, 11), _bi(2, "up", 11, 13),
           _bi(3, "down", 13, 9), _bi(4, "up", 9, 11), _bi(5, "down", 11, 9.5),
           _bi(6, "up", 9.5, 13), _bi(7, "down", 13, 11.5)]
    segs = [_seg("up", 0, 2), _seg("up", 3, 7)]
    centers = [_center(11, 9.5, start_bi=4, end_bi=5, bi_in=3, bi_out=6, seg_idx=1)]
    return bis, segs, centers


# ---- 场景 D：三类卖点（中枢前 T3B）——一类之后第 2 笔反弹不回中枢下沿 ----

def _scene_bsp3b():
    bis = [_bi(0, "up", 10, 14), _bi(1, "down", 14, 12), _bi(2, "up", 12, 13.5),
           _bi(3, "down", 13.5, 12.5), _bi(4, "up", 12.5, 18),
           _bi(5, "down", 18, 11), _bi(6, "up", 11, 12.4), _bi(7, "down", 12.4, 10)]
    segs = [_seg("up", 0, 4), _seg("down", 5, 7)]
    centers = [_center(13.5, 12.5, start_bi=1, end_bi=3, bi_in=0, bi_out=4, seg_idx=0,
                       peak=(14, 12))]
    return bis, segs, centers


# ---- 场景 E：盘整背驰（T1P）——段内有中枢但末笔未由中枢出笔驱动 ----

def _scene_pz_bsp1():
    bis = [_bi(0, "down", 20, 16), _bi(1, "up", 16, 18), _bi(2, "down", 18, 16.5),
           _bi(3, "up", 16.5, 17.8), _bi(4, "down", 17.8, 15.5),
           _bi(5, "up", 15.5, 16.2), _bi(6, "down", 16.2, 15.0)]
    segs = [_seg("down", 0, 6)]
    centers = [_center(18, 16.5, start_bi=1, end_bi=3, bi_in=0, bi_out=4, seg_idx=0,
                       peak=(18, 16.5))]
    return bis, segs, centers


# ========== 六类买卖点的判别用例 ==========

def test_bsp1_trend_divergence_on_seg_end():
    """T1：末笔跌破中枢下沿 + 出笔是区间极值（treat_bsp1 / CZS.is_divergence）。"""
    bis, segs, centers = _scene_trend_bsp1()
    bars, hist = _bars(bis)
    bsps = cb.build_bsps(bis, segs, centers, bars, hist)
    one = [b for b in bsps if "1" in b["types"]]
    assert len(one) == 1, bsps
    assert one[0]["bi_idx"] == 4 and one[0]["is_buy"] is True
    assert one[0]["feature_dict"]["zs_cnt"] == 1


def test_bsp1p_panzheng_divergence():
    """T1P：末笔与同向前笔比动能（treat_pz_bsp1），中枢未驱动末笔时才走这条分支。"""
    bis, segs, centers = _scene_pz_bsp1()
    bars, hist = _bars(bis)
    bsps = cb.build_bsps(bis, segs, centers, bars, hist)
    pz = [b for b in bsps if "1p" in b["types"]]
    assert len(pz) == 1, bsps
    assert pz[0]["bi_idx"] == 6 and pz[0]["is_buy"] is True
    assert "bsp1_bi_amp" in pz[0]["feature_dict"]


def test_bsp1p_not_triggered_without_new_low():
    """反例：末笔未创新低（treat_pz_bsp1 的 `last_bi._low() > pre_bi._low()` 直接 return）。"""
    bis, segs, centers = _scene_pz_bsp1()
    bis = bis[:-1] + [_bi(6, "down", 16.2, 15.8)]     # 15.8 > bi4 的低点 15.5
    bars, hist = _bars(bis)
    assert not [b for b in cb.build_bsps(bis, segs, centers, bars, hist) if "1p" in b["types"]]


def test_bsp2_retrace_after_bsp1():
    """T2：一类买点后第二笔回抽幅度 ≤ max_bs2_rate（treat_bsp2）。"""
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    bsps = cb.build_bsps(bis, segs, centers, bars, hist)
    two = [b for b in bsps if "2" in b["types"]]
    assert len(two) == 1, bsps
    assert two[0]["bi_idx"] == 6 and two[0]["is_buy"] is True
    assert two[0]["feature_dict"]["bsp2_retrace_rate"] == 0.75      # 3/4


def test_bsp2_disappears_when_max_bs2_rate_tightened():
    """参数收紧的反例：max_bs2_rate 从 0.9999 收到 0.5，回抽率 0.75 不再成立 → 二类消失。

    出处 BSPointList.py::treat_bsp2 的 `bsp2_flag = retrace_rate <= BSP_CONF.max_bs2_rate`。
    """
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    loose = cb.build_bsps(bis, segs, centers, bars, hist)
    tight = cb.build_bsps(bis, segs, centers, bars, hist,
                          config=core.BspConfig(max_bs2_rate=0.5))
    assert "2" in _types(loose)
    assert "2" not in _types(tight), tight


def test_bsp2s_follows_bsp2():
    """T2S：与二类笔重叠、不破 break_bi 的后续同向笔逐层成立（treat_bsp2s）。"""
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    bsps = cb.build_bsps(bis, segs, centers, bars, hist)
    s2 = [b for b in bsps if "2s" in b["types"]]
    assert len(s2) == 1, bsps
    assert s2[0]["bi_idx"] == 8 and s2[0]["feature_dict"]["bsp2s_lv"] == 1.0


def test_bsp2s_stops_at_max_bsp2s_lv():
    """反例：max_bsp2s_lv=0 时层级 1 已越界，类二不再产出（treat_bsp2s 的 lv 判据）。"""
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    tight = cb.build_bsps(bis, segs, centers, bars, hist,
                          config=core.BspConfig(max_bsp2s_lv=0))
    assert "2s" not in _types(tight), tight


def test_bsp3a_after_first_multi_bi_center():
    """T3A：下一线段首个多笔中枢的出笔之后一笔不回中枢（treat_bsp3_after + bsp3_back2zs）。"""
    bis, segs, centers = _scene_bsp3a()
    bars, hist = _bars(bis)
    cfg = core.BspConfig(bsp3_follow_1=False)     # 本场景不构造一类买点，单独考察三类判据
    bsps = cb.build_bsps(bis, segs, centers, bars, hist, config=cfg)
    a3 = [b for b in bsps if "3a" in b["types"]]
    assert len(a3) == 1, bsps
    assert a3[0]["bi_idx"] == 7 and a3[0]["is_buy"] is True
    assert a3[0]["feature_dict"]["bsp3_zs_height"] > 0


def test_bsp3a_disappears_when_pullback_returns_into_center():
    """反例：回抽笔跌回中枢内（bsp3_back2zs 为真）→ 三类不成立。"""
    bis, segs, centers = _scene_bsp3a()
    bis = bis[:-1] + [_bi(7, "down", 13, 10.5)]   # 10.5 < 中枢上沿 11 → 回到中枢里
    bars, hist = _bars(bis)
    cfg = core.BspConfig(bsp3_follow_1=False)
    assert not [b for b in cb.build_bsps(bis, segs, centers, bars, hist, config=cfg)
                if "3a" in b["types"]]


def test_bsp3b_before_center():
    """T3B：一类之后第 2 笔（反弹笔）不回中枢下沿 → 三类卖点（treat_bsp3_before）。"""
    bis, segs, centers = _scene_bsp3b()
    bars, hist = _bars(bis)
    bsps = cb.build_bsps(bis, segs, centers, bars, hist)
    b3 = [b for b in bsps if "3b" in b["types"]]
    assert len(b3) == 1, bsps
    assert b3[0]["bi_idx"] == 6 and b3[0]["is_buy"] is False


def test_bsp3b_disappears_when_rebound_returns_into_center():
    """反例：反弹笔重新站上中枢下沿（bsp3_back2zs 为真）→ 三类卖点不成立。"""
    bis, segs, centers = _scene_bsp3b()
    bis = bis[:6] + [_bi(6, "up", 11, 13.0)] + bis[7:]    # 13.0 > 中枢下沿 12.5
    bars, hist = _bars(bis)
    assert not [b for b in cb.build_bsps(bis, segs, centers, bars, hist) if "3b" in b["types"]]


# ========== 背驰度量族 ==========

def _metric_of(bis, hist_map, algo, bi_idx=0, is_reverse=False):
    bars, hist = _bars(bis, hist_map)
    view = core.project_bis(bis, [_seg("down", 0, len(bis) - 1)])
    metric = core.MacdMetric(hist, [b["high"] for b in bars], [b["low"] for b in bars])
    return metric.value(view[bi_idx], algo, is_reverse)


def test_macd_metric_monotonic_in_momentum():
    """同一段价格走势下，MACD 柱整体变弱 → area / peak 度量同步变小；slope 只看价格不变。"""
    bi = [_bi(0, "down", 20, 15)]
    strong = {i: -10.0 + i for i in range(4)}          # -10,-9,-8,-7
    weak = {i: -5.0 + i * 0.5 for i in range(4)}       # -5,-4.5,-4,-3.5
    for algo in ("area", "peak"):
        hi = _metric_of(bi, strong, algo)
        lo = _metric_of(bi, weak, algo)
        assert hi > lo, f"{algo}: 强动能={hi} 弱动能={lo}"
    assert _metric_of(bi, strong, "slope") == _metric_of(bi, weak, "slope")


def test_macd_metric_area_stops_at_sign_flip():
    """area 档遇符号翻转即停（Cal_MACD_half_obverse），正向/反向取到的区段不同。"""
    bi = [_bi(0, "down", 20, 15)]
    hist_map = {0: -4.0, 1: -3.0, 2: 5.0, 3: 6.0}     # 前两根为负，后两根翻正
    obverse = _metric_of(bi, hist_map, "area", is_reverse=False)
    reverse = _metric_of(bi, hist_map, "area", is_reverse=True)
    assert abs(obverse - 7.0) < 1e-6, obverse          # |−4|+|−3|
    assert abs(reverse - 11.0) < 1e-6, reverse         # 从末尾回看：6+5


def test_macd_metric_slope_monotonic_in_price_span():
    """slope 档：同样的涨跌幅走得越久，斜率越小（Cal_MACD_slope 的 /(idx 跨度)）。"""
    short = [_bi(0, "down", 20, 15)]
    long_bi = [{**short[0], "end_idx": short[0]["end_idx"] + 8}]
    assert _metric_of(short, {}, "slope") > _metric_of(long_bi, {}, "slope")


def test_divergence_rate_gates_bsp1():
    """divergence_rate 生效：默认 inf 保送出一类买点；收到 0.5 后 out/in=0.8 不达标 → 不产出。

    出处 ZS/ZS.py::is_divergence（`config.divergence_rate > 100` 为保送分支）。
    """
    bis, segs, centers = _scene_trend_bsp1()
    hist_map = {i: -10.0 for i in range(0, 4)}          # 进笔 bi0 的 MACD 峰值 10
    hist_map.update({i: -8.0 for i in range(16, 20)})   # 出笔 bi4 的 MACD 峰值 8
    bars, hist = _bars(bis, hist_map)
    loose = cb.build_bsps(bis, segs, centers, bars, hist)
    assert [b for b in loose if "1" in b["types"]]
    assert abs(loose[0]["feature_dict"]["divergence_rate"] - 0.8) < 1e-6
    tight = cb.build_bsps(bis, segs, centers, bars, hist,
                          config=core.BspConfig(divergence_rate=0.5))
    assert "1" not in _types(tight), tight


# ========== legacy 映射 / 契约 ==========

def test_legacy_mapping_bsp3a_buy_is_third_buy():
    """口径决定：buy 侧 3a/3b → third_buy，且 strategy_id 仍是 chanlun_third_buy。"""
    bis, segs, centers = _scene_bsp3a()
    bars, hist = _bars(bis)
    cfg = core.BspConfig(bsp3_follow_1=False)
    sigs = cb.build_signals(bis, segs, centers, bars, hist, config=cfg)
    third = [s for s in sigs if s["bsp_type"] == "3a"]
    assert len(third) == 1
    assert third[0]["type"] == "third_buy"
    assert cs.SIGNAL_STRATEGY[third[0]["type"]] == "chanlun_third_buy"
    assert third[0]["strategy_id_v2"] == "chanlun_bsp3a_buy_v2"
    assert third[0]["is_buy"] is True and third[0]["is_sure"] is True


def test_legacy_mapping_bsp1_sell_is_top_divergence():
    """sell 侧一类买卖点 → top_divergence（买侧对应 bottom_divergence）。"""
    bis = [_bi(0, "up", 10, 15), _bi(1, "down", 15, 13), _bi(2, "up", 13, 15.5),
           _bi(3, "down", 15.5, 13.5), _bi(4, "up", 13.5, 18)]
    segs = [_seg("up", 0, 4)]
    centers = [_center(15.5, 13.5, start_bi=1, end_bi=3, bi_in=0, bi_out=4, seg_idx=0,
                       peak=(15.5, 13))]
    bars, hist = _bars(bis)
    sigs = cb.build_signals(bis, segs, centers, bars, hist)
    one = [s for s in sigs if s["bsp_type"] == "1"]
    assert len(one) == 1 and one[0]["type"] == "top_divergence"
    assert cs.SIGNAL_STRATEGY[one[0]["type"]] == "chanlun_top_divergence"


def test_new_spectrum_types_have_no_legacy_strategy_id():
    """1p/2/2s 只在新谱系出现：type 用新名字，legacy strategy_id 查不到 → 天然 0 权重。"""
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    sigs = cb.build_signals(bis, segs, centers, bars, hist)
    new_ones = [s for s in sigs if s["bsp_type"] in ("1p", "2", "2s")]
    assert new_ones
    for s in new_ones:
        assert s["type"].startswith("bsp")
        assert cs.SIGNAL_STRATEGY.get(s["type"]) is None
        assert s["strategy_id_v2"].endswith("_v2")


def test_build_bsps_does_not_mutate_inputs():
    """纯函数边界：笔/线段/中枢/K线四个入参在调用前后逐字节相同。"""
    bis, segs, centers = _scene_bsp2()
    bars, hist = _bars(bis)
    snapshot = copy.deepcopy((bis, segs, centers, bars, hist))
    cb.build_bsps(bis, segs, centers, bars, hist)
    cb.build_signals(bis, segs, centers, bars, hist)
    assert (bis, segs, centers, bars, hist) == snapshot


def test_config_rejects_unknown_macd_algo():
    """未实现的度量档位 fail-closed（chan.py 另有 9 档，本仓库只支持 area/peak/slope）。"""
    for bad in (dict(macd_algo="full_area"), dict(bs_type=("1", "9")),
                dict(max_bs2_rate=1.5), dict(bsp3a_max_zs_cnt=0)):
        try:
            core.BspConfig(**bad)
        except ValueError:
            continue
        raise AssertionError(f"非法配置未被拒绝: {bad}")
