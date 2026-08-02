import deep_research_cache
import research_evidence
import strategy_registry


def test_research_evidence_combines_chanlun_gate_and_serenity_risk(
    tmp_path,
    monkeypatch,
    verified_gate_factory,
):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path))
    strategy_registry.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
    )
    deep_research_cache.write_deep_research(
        "600001",
        "测试股票",
        {
            "total": 58,
            "rating": "谨慎",
            "dimensions": {
                "financial_quality": {"score_1_to_5": 2},
                "risk_control": {"score_1_to_5": 1},
            },
        },
        asof="2026-06-10",
    )

    evidence = research_evidence.build_research_evidence(
        "600001",
        strategy_id="chanlun_third_buy",
        asof="2026-06-12",
    )

    assert evidence["chanlun"]["status"] == "live_allowed"
    assert evidence["serenity"]["available"] is True
    assert "risk_control=1/5" in evidence["serenity"]["hard_risks"]


def test_live_chanlun_signals_become_directional_strategy_attributions():
    evidence = {
        "chanlun": {
            "live_bullish_signals": [
                {
                    "type": "third_buy",
                    "strategy_id": "chanlun_third_buy",
                    "idx": 58,
                }
            ],
            "live_bearish_signals": [
                {
                    "type": "top_divergence",
                    "strategy_id": "chanlun_top_divergence",
                    "idx": 59,
                }
            ],
        }
    }

    attributions = research_evidence.strategy_attributions(evidence)

    assert attributions == [
        {
            "strategy_id": "chanlun_third_buy",
            "role": "research_evidence",
            "direction": "bullish",
            "signal_type": "third_buy",
            "signal_idx": 58,
        },
        {
            "strategy_id": "chanlun_top_divergence",
            "role": "research_evidence",
            "direction": "bearish",
            "signal_type": "top_divergence",
            "signal_idx": 59,
        },
    ]


def _fixture_analysis(last_close, last_center, last_seg, signals):
    return {
        "ok": True,
        "last_close": last_close,
        "structure": {
            "stroke_count": (last_seg or {}).get("_stroke_count", 5),
            "last_center": last_center,
            "last_seg": last_seg,
        },
        "signals": signals,
        "summary": "fixture",
    }


def test_structure_position_section_schema(monkeypatch):
    """structure_position section 的字段/口径：price_vs_center + segment + 最近确定信号摘要。"""
    center = {"zg": 12.0, "zd": 10.0}
    seg = {"dir": "up", "is_sure": True, "start_bi_idx": 2, "end_bi_idx": 4, "_stroke_count": 5}
    signals = [
        {"bsp_type": "3a", "is_buy": True, "is_sure": True, "idx": 40, "bi_idx": 4, "date": "2026-07-30"},
        {"bsp_type": "2", "is_buy": False, "is_sure": False, "idx": 38, "bi_idx": 3, "date": "2026-07-29"},
    ]
    monkeypatch.setattr(
        research_evidence.chan_structure, "analyze",
        lambda bars: _fixture_analysis(13.0, center, seg, signals),
    )

    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", asof="2026-08-01",
        bars=[{"close": 10.0}] * 30,
    )

    section = evidence["structure_position"]
    assert section["available"] is True
    assert section["price_vs_center"] == {
        "position": "above_zg", "zg": 12.0, "zd": 10.0,
        "distance_pct": round((13.0 - 12.0) / 12.0 * 100, 3),
    }
    assert section["segment"] == {"dir": "up", "is_sure": True, "current_stroke_ordinal": 3}
    # 只有 idx=40 的信号 is_sure=True，摘要只含它
    assert section["recent_sure_signals"] == [
        {"bsp_type": "3a", "is_buy": True, "date": "2026-07-30"}
    ]


def test_structure_position_unavailable_without_bars():
    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", asof="2026-08-01",
    )
    assert evidence["structure_position"] == {"available": False}


def test_structure_position_price_vs_center_inside_band(monkeypatch):
    center = {"zg": 12.0, "zd": 10.0}
    monkeypatch.setattr(
        research_evidence.chan_structure, "analyze",
        lambda bars: _fixture_analysis(11.0, center, None, []),
    )
    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", bars=[{"close": 10.0}] * 10,
    )
    assert evidence["structure_position"]["price_vs_center"] == {
        "position": "inside", "zg": 12.0, "zd": 10.0, "distance_pct": 50.0,
    }


def test_structure_position_risk_flag_seg_end_divergence(monkeypatch):
    """最新线段 is_sure，其末笔(end_bi_idx=4)上出现确定的一类卖点 → seg_end_divergence。"""
    seg = {"dir": "up", "is_sure": True, "start_bi_idx": 2, "end_bi_idx": 4}
    signals = [
        {"bsp_type": "1", "is_buy": False, "is_sure": True, "idx": 40, "bi_idx": 4, "date": "2026-07-30"},
    ]
    monkeypatch.setattr(
        research_evidence.chan_structure, "analyze",
        lambda bars: _fixture_analysis(13.0, {"zg": 12.0, "zd": 10.0}, seg, signals),
    )
    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", bars=[{"close": 10.0}] * 10,
    )
    assert "seg_end_divergence" in evidence["structure_position"]["risk_flags"]


def test_structure_position_risk_flag_third_sell_structure(monkeypatch):
    signals = [
        {"bsp_type": "3b", "is_buy": False, "is_sure": True, "idx": 40, "bi_idx": 4, "date": "2026-07-30"},
    ]
    monkeypatch.setattr(
        research_evidence.chan_structure, "analyze",
        lambda bars: _fixture_analysis(13.0, {"zg": 12.0, "zd": 10.0}, None, signals),
    )
    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", bars=[{"close": 10.0}] * 10,
    )
    assert evidence["structure_position"]["risk_flags"] == ["third_sell_structure"]


def test_structure_position_risk_flags_empty_without_matching_signals(monkeypatch):
    """反例：一类买点(is_buy=True)不触发 seg_end_divergence；三买(3a,is_buy=True)不触发 third_sell_structure。"""
    seg = {"dir": "up", "is_sure": True, "start_bi_idx": 2, "end_bi_idx": 4}
    signals = [
        {"bsp_type": "1", "is_buy": True, "is_sure": True, "idx": 40, "bi_idx": 4, "date": "2026-07-30"},
        {"bsp_type": "3a", "is_buy": True, "is_sure": True, "idx": 40, "bi_idx": 4, "date": "2026-07-30"},
    ]
    monkeypatch.setattr(
        research_evidence.chan_structure, "analyze",
        lambda bars: _fixture_analysis(13.0, {"zg": 12.0, "zd": 10.0}, seg, signals),
    )
    evidence = research_evidence.build_research_evidence(
        "600001", strategy_id="trend:test", bars=[{"close": 10.0}] * 10,
    )
    assert evidence["structure_position"]["risk_flags"] == []


def test_chanlun_evidence_records_point_in_time_signal_age(monkeypatch):
    monkeypatch.setattr(
        research_evidence.chan_structure,
        "analyze",
        lambda bars: {
            "summary": "fixture",
            "signals": [
                {"type": "third_buy", "strategy_id": "chanlun_third_buy", "idx": 7}
            ],
        },
    )
    monkeypatch.setattr(strategy_registry, "is_allowed_in_live", lambda strategy_id: False)

    evidence = research_evidence.build_research_evidence(
        "600001",
        strategy_id="trend:test",
        asof="2026-07-13",
        bars=[{"close": 10.0}] * 10,
    )

    signal = evidence["chanlun"]["signals"][0]
    assert signal["signal_age_bars"] == 2
    assert signal["gate_status"] == "display_only"
