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

    # 注册过闸 → 计权加分
    import strategy_registry as sr
    sr.register_gate_result(
        "chanlun_third_buy",
        verified_gate_factory("chanlun_third_buy"),
    )
    out2 = fds.score_technical("002156", "通富微电")
    assert "缠论三买" in out2["detail"]
    assert out2["score"] > out1["score"]
