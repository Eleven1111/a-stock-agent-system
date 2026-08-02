"""四维技术面接入缠论结构 — 过闸计权 / 未过闸 display-only / 信号新鲜度。"""

import pytest

import four_dim_scorer as fds


def _allow_all(_sid):
    return True


def _deny_all(_sid):
    return False


def test_chan_third_buy_allowed_adds():
    sigs = [{"type": "third_buy", "idx": 59, "strategy_id": "chanlun_third_buy"}]
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_allow_all)
    assert delta == 1.5
    assert lock is None
    assert any("缠论三买" in n for n in notes)


def test_chan_unregistered_display_only():
    sigs = [{"type": "third_buy", "idx": 59, "strategy_id": "chanlun_third_buy"}]
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_deny_all)
    assert delta == 0.0
    assert any("研究假设" in n for n in notes)


def test_chan_third_sell_locks_6():
    sigs = [{"type": "third_sell", "idx": 59, "strategy_id": "chanlun_third_sell"}]
    delta, lock, _ = fds.chan_adjustment(sigs, 10, 60, _allow_all)
    assert delta == -1.5
    assert lock == 6.0


def test_chan_top_divergence_locks_5():
    sigs = [{"type": "top_divergence", "idx": 59, "strategy_id": "chanlun_top_divergence"}]
    _, lock, _ = fds.chan_adjustment(sigs, 10, 60, _allow_all)
    assert lock == 5.0


def test_chan_bottom_divergence_adds():
    sigs = [{"type": "bottom_divergence", "idx": 59, "strategy_id": "chanlun_bottom_divergence"}]
    delta, _, _ = fds.chan_adjustment(sigs, 10, 60, _allow_all)
    assert delta == 1.0


def test_chan_stale_signal_ignored():
    sigs = [{"type": "third_buy", "idx": 10, "strategy_id": "chanlun_third_buy"}]  # 60-10 > window 10
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_allow_all)
    assert delta == 0.0
    assert notes == []


def test_chan_new_lineage_signals_aggregate_into_one_note():
    """T4 遗留展示噪声收敛：strategy_id=None 的新谱系信号不逐条追加，聚合成一条计数。"""
    sigs = [
        {"type": "bsp1p_buy", "idx": 58, "strategy_id": None},
        {"type": "bsp2_sell", "idx": 59, "strategy_id": None},
        {"type": "bsp2s_buy", "idx": 57, "strategy_id": None},
    ]
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_deny_all)
    assert delta == 0.0
    assert lock is None
    assert notes == ["[研究假设]缠论新谱系信号×3(未过闸·0权重)"]


def test_chan_legacy_notes_stay_individual_alongside_aggregated_new_lineage():
    """legacy 四类型（strategy_id 有值）继续逐条备注，新谱系单独聚合成一条，互不干扰。"""
    sigs = [
        {"type": "third_buy", "idx": 59, "strategy_id": "chanlun_third_buy"},
        {"type": "bsp1p_buy", "idx": 58, "strategy_id": None},
    ]
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_deny_all)
    assert delta == 0.0
    assert len(notes) == 2
    assert any("研究假设" in n and "缠论三买" in n for n in notes)
    assert "[研究假设]缠论新谱系信号×1(未过闸·0权重)" in notes


def test_chan_new_lineage_signals_do_not_change_delta_or_lock():
    """未过闸信号不改变 score 的回归用例（chan_adjustment 层面）。"""
    baseline_delta, baseline_lock, baseline_notes = fds.chan_adjustment(
        [], recent_window=10, total_bars=60, allow_fn=_deny_all)
    sigs = [{"type": "bsp2_buy", "idx": 59, "strategy_id": None} for _ in range(5)]
    delta, lock, notes = fds.chan_adjustment(sigs, recent_window=10, total_bars=60, allow_fn=_deny_all)
    assert (delta, lock) == (baseline_delta, baseline_lock) == (0.0, None)
    assert baseline_notes == []
    assert notes == ["[研究假设]缠论新谱系信号×5(未过闸·0权重)"]


def test_score_technical_missing_klines_keeps_output_schema(monkeypatch):
    quote = {"price": 10.0, "change_pct": 1.0}

    result = fds.score_technical("002156", "通富微电", quote=quote, klines=[])

    assert result["score"] == 5
    assert result["price"] == 10.0
    assert result["ma5"] is None
    assert result["atr14"] is None


def test_score_technical_gates_chan_signal(monkeypatch, tmp_path, verified_gate_factory):
    if fds._chan is None:
        pytest.skip("chan_structure unavailable")
    # A_STOCK_STATE_HOME 优先级高于 HERMES_HOME，conftest 为隔离测试状态
    # 无条件设置了它，这里要用 HERMES_HOME 驱动本用例的 tmp_path，
    # 必须先清掉 A_STOCK_STATE_HOME 才能让 HERMES_HOME 生效。
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    klines = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]
    monkeypatch.setattr(fds, "fetch_tencent_realtime",
                        lambda c, m="sz": {"price": 10.0, "change_pct": 1.0, "pe": 20.0})
    monkeypatch.setattr(fds, "fetch_tencent_kline", lambda *a, **k: klines)
    monkeypatch.setattr(fds._chan, "analyze",
                        lambda bars: {"signals": [{"type": "third_buy", "idx": 59,
                                                   "strategy_id": "chanlun_third_buy"}]})

    # 未注册 → display-only（不加分）
    out1 = fds.score_technical("002156", "通富微电")
    assert "研究假设" in out1["detail"]

    # 研究门禁通过仍未完成 shadow/promotion → 继续 0 权重。
    import strategy_registry as sr
    sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
    )
    out2 = fds.score_technical("002156", "通富微电")
    assert "缠论三买" in out2["detail"]
    assert "研究假设" in out2["detail"]
    assert out2["score"] == out1["score"]


def test_score_technical_new_lineage_signals_do_not_change_score(monkeypatch, tmp_path):
    """回归用例（T5 验收）：新谱系信号（strategy_id=None）存在时，four_dim 总分与
    无信号时一致，只有展示文本（聚合备注）不同。"""
    if fds._chan is None:
        pytest.skip("chan_structure unavailable")
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    klines = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]
    monkeypatch.setattr(fds, "fetch_tencent_realtime",
                        lambda c, m="sz": {"price": 10.0, "change_pct": 1.0, "pe": 20.0})
    monkeypatch.setattr(fds, "fetch_tencent_kline", lambda *a, **k: klines)

    monkeypatch.setattr(fds._chan, "analyze", lambda bars: {"signals": []})
    baseline = fds.score_technical("002156", "通富微电")

    monkeypatch.setattr(fds._chan, "analyze", lambda bars: {"signals": [
        {"type": "bsp1p_buy", "idx": 59, "strategy_id": None},
        {"type": "bsp2_sell", "idx": 58, "strategy_id": None},
    ]})
    with_new_lineage = fds.score_technical("002156", "通富微电")

    assert with_new_lineage["score"] == baseline["score"]
    assert "缠论新谱系信号×2(未过闸·0权重)" in with_new_lineage["detail"]
    assert "缠论新谱系信号" not in baseline["detail"]


def test_score_short_term_entry_shows_nested_confirmation_note(monkeypatch):
    """区间套证据（T5）：日线×60m 同向确定买卖点共现时，signals 里出现 0 权重展示备注。"""
    if fds._chan is None or fds._chan_nested is None:
        pytest.skip("chan_nested unavailable")
    bars = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(30)]
    monkeypatch.setattr(fds, "fetch_tencent_realtime", lambda *a, **k: {"price": 10.0, "change_pct": 1.0})
    monkeypatch.setattr(fds, "fetch_tencent_kline", lambda code, market, days, ktype="day": bars)
    monkeypatch.setattr(fds, "calc_ma", lambda values, period: [None] * len(values))
    monkeypatch.setattr(fds, "calc_macd",
                        lambda values, **kwargs: ([None] * len(values), [None] * len(values), [None] * len(values)))
    monkeypatch.setattr(fds, "calc_rsi", lambda values, period: [None] * len(values))
    monkeypatch.setattr(fds, "calc_volume_ratio", lambda values: None)
    monkeypatch.setattr(fds, "calc_atr", lambda *a, **k: [None] * len(bars))

    nested_signals = [
        {"type": "bsp1_buy", "idx": 29, "bsp_type": "1", "is_buy": True, "is_sure": True,
         "strategy_id": None, "date": "2026-07-30"},
        {"type": "bsp2_buy", "idx": 29, "bsp_type": "2", "is_buy": True, "is_sure": True,
         "strategy_id": None, "date": "2026-08-01 14:00"},
    ]
    monkeypatch.setattr(fds._chan, "analyze", lambda bars_arg: {"signals": nested_signals})

    result = fds.score_short_term_entry("600001", "测试股")

    assert any("区间套共振" in item and "0权重" in item for item in result["signals"])
