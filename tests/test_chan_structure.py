"""缠论结构信号生成器 — 去包含 / 分型 / 笔 / 中枢 / 三买 / 背驰。"""

import importlib.util
import math
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "chan_structure.py"
SPEC = importlib.util.spec_from_file_location("chan_structure", SCRIPT)
cs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cs)


def _bar(h, l, c=None, d=None):
    c = c if c is not None else (h + l) / 2
    return {"high": h, "low": l, "close": c, "open": c, "date": d}


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


def test_build_strokes_alternate_dir():
    fractals = [
        {"type": "bottom", "mi": 0, "idx": 0, "price": 8},
        {"type": "top", "mi": 5, "idx": 5, "price": 12},
        {"type": "bottom", "mi": 10, "idx": 10, "price": 9},
        {"type": "top", "mi": 15, "idx": 15, "price": 13},
    ]
    strokes, cleaned = cs.build_strokes(fractals, min_gap=1)
    assert len(strokes) == 3
    assert [s["dir"] for s in strokes] == ["up", "down", "up"]


def test_build_strokes_skips_too_close():
    fractals = [
        {"type": "bottom", "mi": 0, "idx": 0, "price": 8},
        {"type": "top", "mi": 2, "idx": 2, "price": 12},   # 间隔 2 < min_gap 4 → 跳过
    ]
    strokes, cleaned = cs.build_strokes(fractals, min_gap=4)
    assert strokes == []


def test_build_centers_overlap():
    strokes = [
        {"dir": "up", "start_idx": 0, "end_idx": 3, "high": 12, "low": 10, "start_price": 10, "end_price": 12},
        {"dir": "down", "start_idx": 3, "end_idx": 6, "high": 12, "low": 10.5, "start_price": 12, "end_price": 10.5},
        {"dir": "up", "start_idx": 6, "end_idx": 9, "high": 11.8, "low": 10.5, "start_price": 10.5, "end_price": 11.8},
    ]
    centers = cs.build_centers(strokes)
    assert len(centers) == 1
    assert centers[0]["zg"] == 11.8
    assert centers[0]["zd"] == 10.5


def test_detect_third_buy():
    strokes = [
        {"dir": "up", "start_idx": 0, "end_idx": 3, "high": 12, "low": 10, "start_price": 10, "end_price": 12},
        {"dir": "down", "start_idx": 3, "end_idx": 6, "high": 12, "low": 10.5, "start_price": 12, "end_price": 10.5},
        {"dir": "up", "start_idx": 6, "end_idx": 9, "high": 11.8, "low": 10.5, "start_price": 10.5, "end_price": 11.8},
        {"dir": "up", "start_idx": 9, "end_idx": 12, "high": 13, "low": 11.9, "start_price": 11.9, "end_price": 13},
        {"dir": "down", "start_idx": 12, "end_idx": 15, "high": 13, "low": 12, "start_price": 13, "end_price": 12},
    ]
    centers = [{"zg": 11.8, "zd": 10.5, "start_stroke": 0, "end_stroke": 2,
                "start_idx": 0, "end_idx": 9, "stroke_count": 3}]
    sig = cs.detect_third_signals(strokes, centers)
    assert any(s["type"] == "third_buy" for s in sig)


def test_detect_top_divergence():
    strokes = [
        {"dir": "up", "start_idx": 0, "end_idx": 3, "high": 12, "low": 10, "start_price": 10, "end_price": 12},
        {"dir": "down", "start_idx": 4, "end_idx": 7, "high": 12, "low": 9, "start_price": 12, "end_price": 9},
        {"dir": "up", "start_idx": 8, "end_idx": 11, "high": 13, "low": 9, "start_price": 9, "end_price": 13},
    ]
    hist = [None] * 12
    for j in range(0, 4):
        hist[j] = 3.0    # prev up 动能面积 12
    for j in range(8, 12):
        hist[j] = 1.0    # last up 动能面积 4 < 12 → 顶背驰
    sig = cs.detect_divergence(strokes, hist)
    assert sig and sig[0]["type"] == "top_divergence"


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
