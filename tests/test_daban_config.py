"""打板阈值单一事实源 — 默认值 + yaml 与默认一致（无行为漂移）。"""

import daban_config as dc


def test_defaults_loaded():
    cfg = dc.load_config()
    assert cfg["auction"]["gap_window_low"] == -1.0
    assert cfg["auction"]["gap_window_high"] == 3.0
    assert cfg["auction"]["auction_seal_minute"] == 565
    assert cfg["cost"]["commission"] == 0.00025


def test_section_helper():
    fbr = dc.section("first_board_reseal")
    assert fbr["active_buy_ratio_min"] == 0.60
    assert fbr["seal_amount_ratio_min"] == 0.003
    assert fbr["sector_limitup_min"] == 3


def test_yaml_matches_defaults_no_drift():
    """config/daban_thresholds.yaml 的值必须与 DEFAULTS 完全一致，
    否则等于偷偷改了实盘/回测阈值（应走 research_gate）。"""
    cfg = dc.load_config()
    for sec, vals in dc.DEFAULTS.items():
        for k, v in vals.items():
            assert cfg[sec][k] == v, f"{sec}.{k}: yaml={cfg[sec][k]} != default={v}"


def test_missing_yaml_falls_back_to_defaults(tmp_path):
    cfg = dc.load_config(path=str(tmp_path / "nonexistent.yaml"))
    assert cfg["auction"]["gap_window_high"] == 3.0
