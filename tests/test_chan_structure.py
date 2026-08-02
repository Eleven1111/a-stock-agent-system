"""缠论结构信号生成器 — 去包含 / 分型 / 笔 / 中枢 / 三买 / 背驰。"""

import importlib.util
import math
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "chan_structure.py"
SPEC = importlib.util.spec_from_file_location("chan_structure", SCRIPT)
cs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cs)


def _bar(h, low, c=None, d=None):
    c = c if c is not None else (h + low) / 2
    return {"high": h, "low": low, "close": c, "open": c, "date": d}


def test_merge_klines_removes_inclusion():
    bars = [_bar(10, 8), _bar(9, 8.5), _bar(11, 9)]
    merged = cs.merge_klines(bars)
    assert len(merged) == 2          # 前两根存在包含 → 合并
    assert merged[0]["high"] == 10   # 向上取高高
    assert merged[0]["low"] == 8.5


def test_find_fractals_top_and_bottom():
    merged = [
        {"high": 10, "low": 9, "high_idx": 0, "low_idx": 0, "idx": 0},
        {"high": 12, "low": 11, "high_idx": 1, "low_idx": 1, "idx": 1},  # 顶
        {"high": 10, "low": 9, "high_idx": 2, "low_idx": 2, "idx": 2},
        {"high": 8, "low": 7, "high_idx": 3, "low_idx": 3, "idx": 3},    # 底
        {"high": 10, "low": 9, "high_idx": 4, "low_idx": 4, "idx": 4},
    ]
    fr = cs.find_fractals(merged)
    types = [f["type"] for f in fr]
    assert "top" in types and "bottom" in types


def _zigzag(levels, bars_per_leg):
    """在给定价位之间线性游走，生成走势明确的合成日K。"""
    bars, price = [], levels[0]
    for leg_end in levels[1:]:
        step = (leg_end - price) / bars_per_leg
        for _ in range(bars_per_leg):
            nxt = price + step
            bars.append({"high": max(price, nxt) + abs(step) * 0.2,
                         "low": min(price, nxt) - abs(step) * 0.2,
                         "close": nxt, "open": price, "date": None})
            price = nxt
    return bars


def test_build_bis_alternate_dir():
    """旧 test_build_strokes_alternate_dir 的等价用例。
    规则变更：笔不再由"分型列表顶底交替"拼接，改为 chan_kline 状态机逐根推进
    （出处 third_party/chan_py_reference/Bi/BiList.py::update_bi_sure / can_make_bi），
    入口参数因此从 fractals 变为原始 bars，函数名 build_strokes → chan_kline.build_bis。"""
    bars = _zigzag([12.0, 10.0, 14.0, 11.0, 15.0], bars_per_leg=6)
    bis = cs.chan_kline.build_bis(bars, cs.bi_config(4))
    assert [b["dir"] for b in bis] == ["up", "down", "up"]
    # 末段涨势尚未被顶分型确认 → 虚笔；前两笔已确认
    assert [b["is_sure"] for b in bis] == [True, True, False]


def test_build_bis_skips_too_short_swing():
    """旧 test_build_strokes_skips_too_close 的等价用例。
    规则变更：跨度不足的判据从"相邻分型合并K线间隔 < min_gap"改为
    chan.py 严格笔跨度条件 satisfy_bi_span（合并K线跨度 >= 4，出处 Bi/BiList.py 第 149-163 行）。"""
    bars = _zigzag([10.0, 11.0, 10.0], bars_per_leg=2)   # 每段仅 2 根 → 跨度不足 4
    assert cs.chan_kline.build_bis(bars, cs.bi_config(4)) == []


def test_centers_come_from_chan_center():
    """旧 test_build_centers_overlap 的等价用例。
    规则变更：中枢不再由"滑窗 3 笔重叠"近似生成（chan_structure.build_centers 已删除），
    改为 chan_center 的段内构造（出处 third_party/chan_py_reference/ZS/ZSList.py::cal_bi_zs
    + add_zs_from_bi_range）。行为差异：① 必须给线段，无线段不产中枢；② 构造元素只取与线段
    方向相反的笔，故上升线段里需要两笔下降笔（原 3 笔样例扩到 5 笔）；③ 中枢区间由这两笔
    决定（zg=11.8/zd=10.8），而非窗口内 3 笔（原为 zd=10.5）。"""
    strokes = [
        {"dir": "up", "start_idx": 0, "end_idx": 3, "high": 12, "low": 10, "start_price": 10, "end_price": 12},
        {"dir": "down", "start_idx": 3, "end_idx": 6, "high": 12, "low": 10.5, "start_price": 12, "end_price": 10.5},
        {"dir": "up", "start_idx": 6, "end_idx": 9, "high": 11.8, "low": 10.5, "start_price": 10.5, "end_price": 11.8},
        {"dir": "down", "start_idx": 9, "end_idx": 12, "high": 11.8, "low": 10.8, "start_price": 11.8, "end_price": 10.8},
        {"dir": "up", "start_idx": 12, "end_idx": 15, "high": 12.5, "low": 10.8, "start_price": 10.8, "end_price": 12.5},
    ]
    segs = [{"dir": "up", "start_bi_idx": 0, "end_bi_idx": 4, "is_sure": True}]
    centers = cs.chan_center.build_centers(strokes, segs)
    assert len(centers) == 1
    assert (centers[0]["zg"], centers[0]["zd"]) == (11.8, 10.8)
    assert (centers[0]["start_bi_idx"], centers[0]["end_bi_idx"]) == (1, 3)


def _bi(idx, direction, begin, end):
    return {"dir": direction, "start_price": begin, "end_price": end,
            "high": max(begin, end), "low": min(begin, end),
            "start_idx": idx * 4, "end_idx": idx * 4 + 3, "is_sure": True, "used_to_be_sure": True}


def _flat_bars(bis):
    return [{"high": 100.0, "low": 90.0, "close": 95.0, "open": 95.0, "date": None}
            for _ in range(max(b["end_idx"] for b in bis) + 1)]


def test_third_buy_comes_from_chan_bsp():
    """旧 test_detect_third_buy 的等价用例。
    规则变更：三买不再由"最后一个中枢之后回踩不破上沿"近似生成（chan_structure.detect_third_signals
    已删除），改为 chan_bsp 的正宗三类买卖点（出处 third_party/chan_py_reference/BuySellPoint/
    BSPointList.py::treat_bsp3_after + bsp3_back2zs）。行为差异：① 中枢必须落在**下一条线段**内且
    是多笔中枢；② 锚定笔是中枢出笔的后一笔，其低点必须不低于中枢上沿；③ 判据不再看"离开笔"。
    穷尽判别在 tests/test_chan_bsp.py，本用例只守 chan_structure 仍产出 third_buy 这条契约。"""
    bis = [_bi(0, "up", 10, 12), _bi(1, "down", 12, 11), _bi(2, "up", 11, 13),
           _bi(3, "down", 13, 9), _bi(4, "up", 9, 11), _bi(5, "down", 11, 9.5),
           _bi(6, "up", 9.5, 13), _bi(7, "down", 13, 11.5)]
    segs = [{"dir": "up", "start_bi_idx": 0, "end_bi_idx": 2, "is_sure": True},
            {"dir": "up", "start_bi_idx": 3, "end_bi_idx": 7, "is_sure": True}]
    centers = [{"zg": 11, "zd": 9.5, "peak_high": 11, "peak_low": 9.5, "start_bi_idx": 4,
                "end_bi_idx": 5, "bi_count": 2, "bi_in_idx": 3, "bi_out_idx": 6, "seg_idx": 1}]
    cfg = cs.chan_bsp.BspConfig(bsp3_follow_1=False)
    sig = cs.chan_bsp.build_signals(bis, segs, centers, _flat_bars(bis), config=cfg)
    assert any(s["type"] == "third_buy" and s["bsp_type"] == "3a" for s in sig), sig


def test_top_divergence_comes_from_chan_bsp():
    """旧 test_detect_top_divergence 的等价用例。
    规则变更：顶背驰不再由"最近两段同向笔 MACD 面积比较"近似生成（chan_structure.detect_divergence
    已删除），改为 chan_bsp 的一类卖点（出处 BuySellPoint/BSPointList.py::treat_bsp1 +
    ZS/ZS.py::is_divergence）。行为差异：① 锚定笔固定为**线段末笔**且必须突破中枢；
    ② 背驰比较的是中枢进笔 vs 出笔（不是最近两段同向笔）；③ divergence_rate 默认 inf = 保送，
    动能大小不再是成立条件，只写进 feature_dict。"""
    bis = [_bi(0, "up", 10, 15), _bi(1, "down", 15, 13), _bi(2, "up", 13, 15.5),
           _bi(3, "down", 15.5, 13.5), _bi(4, "up", 13.5, 18)]
    segs = [{"dir": "up", "start_bi_idx": 0, "end_bi_idx": 4, "is_sure": True}]
    centers = [{"zg": 15.5, "zd": 13.5, "peak_high": 15.5, "peak_low": 13, "start_bi_idx": 1,
                "end_bi_idx": 3, "bi_count": 3, "bi_in_idx": 0, "bi_out_idx": 4, "seg_idx": 0}]
    sig = cs.chan_bsp.build_signals(bis, segs, centers, _flat_bars(bis))
    top = [s for s in sig if s["type"] == "top_divergence"]
    assert top and top[0]["bsp_type"] == "1" and top[0]["is_buy"] is False, sig


def test_analyze_end_to_end():
    bars = []
    for i in range(60):
        price = 10 + 2 * math.sin(i / 3.0)
        bars.append({"high": price + 0.3, "low": price - 0.3, "close": price,
                     "open": price, "date": f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"})
    out = cs.analyze(bars, min_gap=2)
    assert out["ok"] is True
    assert isinstance(out["signals"], list)
    assert out["structure"]["stroke_count"] >= 1
    for s in out["signals"]:
        assert s["strategy_id"] in cs.SIGNAL_STRATEGY.values()
