"""四维技术面接入 emotion_cycle 情绪周期特征 — 0权重展示路径 / research_gate 对接。"""

import four_dim_scorer as fds


def test_emotion_cycle_unregistered_zero_weight_notes(monkeypatch):
    """未注册 emotion_cycle:v1 → notes 含 [研究假设]，score 不受影响。"""
    klines = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]
    quote = {"price": 10.0, "change_pct": 1.0}

    monkeypatch.setattr(fds, "_chan_allowed", lambda sid: False)

    result = fds.score_technical("002156", "通富微电", quote=quote, klines=klines)
    assert "[研究假设]情绪周期(未过闸·0权重)" in result["detail"]
    assert "emotion_cycle" in result
    assert result["emotion_cycle"]["volume_percentile_60d"]["available"] is True


def test_emotion_cycle_registered_still_zero_weight_delta(monkeypatch, tmp_path, verified_gate_factory):
    """过闸后：本次不实现计权 delta，只是不再标[研究假设]；score 数值不因过闸而变化。"""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    klines = [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]
    quote = {"price": 10.0, "change_pct": 1.0}

    out_before = fds.score_technical("002156", "通富微电", quote=quote, klines=klines)

    import strategy_registry as sr
    sr.register_gate_result(
        "emotion_cycle:v1",
        verified_gate_factory("emotion_cycle:v1"),
    )

    out_after = fds.score_technical("002156", "通富微电", quote=quote, klines=klines)
    assert "[研究假设]情绪周期" not in out_after["detail"]
    # 过闸不引入计权（本次不实现 delta），分数应保持一致。
    assert out_after["score"] == out_before["score"]


def test_emotion_cycle_empty_klines_regression():
    """kline=[] → 四维评分与现状一致（回归），不新增 emotion_cycle 键。"""
    quote = {"price": 10.0, "change_pct": 1.0}
    result = fds.score_technical("002156", "通富微电", quote=quote, klines=[])
    assert result["score"] == 5
    assert "emotion_cycle" not in result


def test_emotion_cycle_config_missing_block_uses_defaults(monkeypatch, tmp_path):
    """config/scoring.yaml 缺 emotion_cycle 块 → 用模块内默认值，不崩溃。"""
    scoring_yaml = tmp_path / "scoring.yaml"
    scoring_yaml.write_text(
        "scoring:\n"
        "  weights:\n"
        "    default: {technical: 0.30, sentiment: 0.15, catalyst: 0.30, deep: 0.25}\n"
        "  temperature_overlay: {}\n"
        "  grades:\n"
        "    S: {min: 8.0, emoji: \"x\", advice: \"x\"}\n"
        "  confidence:\n"
        "    high: {requires: [realtime]}\n"
        "risk:\n"
        "  stop_loss_atr_mult: 2.0\n",
        encoding="utf-8",
    )
    import emotion_cycle_features as ecf
    monkeypatch.setattr(ecf, "config_path", lambda name: str(scoring_yaml))
    cfg = ecf._load_config()
    assert cfg["volume_percentile"]["window"] == 60
    assert cfg["synthesis"]["bottom_min"] == 3


def test_emotion_cycle_custom_threshold_effective():
    """自定义阈值生效：调低 min_samples 后原本 fail-closed 的样本数变为可用。"""
    import emotion_cycle_features as ecf

    volumes = [float(i) for i in range(1, 11)]  # 10 samples
    klines = [{"close": 10.0, "high": 10.0, "low": 10.0, "volume": v} for v in volumes]

    default_result = ecf.compute_volume_percentile(klines)
    assert default_result["available"] is False

    custom_cfg = {"volume_percentile": {"window": 60, "min_samples": 5,
                                        "buckets": {"cold": 0.20, "active": 0.70,
                                                    "hot": 0.90, "extreme": 0.98}}}
    custom_result = ecf.compute_volume_percentile(klines, config=custom_cfg)
    assert custom_result["available"] is True
