"""情绪周期确定性特征（emotion_cycle_features）— P1-C

F1 volume_percentile_60d / F2 volume_spike_ratio / F3 ma_coil_ratio /
F4 atr_contraction_pct / F5 emotion_extreme（合成判定）。

全部为研究信号：fail-closed，不返回中性数值；四维评分对接见
test_four_dim_scorer_emotion.py（若存在）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "common"))

import emotion_cycle_features as ecf


def _kline(close, high=None, low=None, volume=None):
    return {
        "close": close,
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "volume": volume,
    }


def _klines_from_volumes(volumes, close=10.0):
    return [_kline(close, volume=v) for v in volumes]


# ---------------------------------------------------------------------------
# F1: volume_percentile_60d
# ---------------------------------------------------------------------------

class TestVolumePercentile:
    def test_monotonic_increase_today_is_max_extreme(self):
        volumes = [float(i) for i in range(1, 61)]  # today = 60, strictly max
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is True
        assert result["pct"] == 1.0
        assert result["bucket"] == "extreme"

    def test_today_is_min_cold(self):
        volumes = [float(i) for i in range(2, 62)]
        volumes[-1] = 1.0  # today smallest
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is True
        assert result["pct"] == 0.0
        assert result["bucket"] == "cold"

    def test_min_samples_20_available(self):
        volumes = [float(i) for i in range(1, 21)]  # exactly 20 samples
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is True

    def test_min_samples_19_fail_closed(self):
        volumes = [float(i) for i in range(1, 20)]  # 19 samples
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is False
        assert result["value"] is None

    def test_all_volumes_none_fail_closed(self):
        klines = [_kline(10.0, volume=None) for _ in range(30)]
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is False

    def test_window_35_reports_window_used(self):
        volumes = [float(i) for i in range(1, 36)]  # 35 samples, window default 60
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_percentile(klines)
        assert result["available"] is True
        assert result["window_used"] == 35


# ---------------------------------------------------------------------------
# F2: volume_spike_ratio
# ---------------------------------------------------------------------------

class TestVolumeSpike:
    def test_ratio_5x_boundary_included_distribution_suspect(self):
        avg20 = [100.0] * 20
        volumes = avg20 + [500.0]  # today = 5.0x avg20
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_spike(klines)
        assert result["available"] is True
        assert result["ratio"] == 5.0
        assert result["label"] == "distribution_suspect"

    def test_ratio_4_9x_not_included(self):
        avg20 = [100.0] * 20
        volumes = avg20 + [490.0]  # 4.9x
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_spike(klines)
        assert result["available"] is True
        assert result["ratio"] == 4.9
        assert result["label"] != "distribution_suspect"
        assert result["label"] == "heavy_volume"

    def test_avg20_zero_fail_closed(self):
        volumes = [0.0] * 20 + [100.0]
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_spike(klines)
        assert result["available"] is False

    def test_sample_missing_one_bar_fail_closed(self):
        # need >=21 bars total (20 for avg + 1 today); provide only 20
        volumes = [100.0] * 20
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_spike(klines)
        assert result["available"] is False

    def test_ratio_0_5x_shrink(self):
        avg20 = [100.0] * 20
        volumes = avg20 + [50.0]
        klines = _klines_from_volumes(volumes)
        result = ecf.compute_volume_spike(klines)
        assert result["available"] is True
        assert result["ratio"] == 0.5
        assert result["label"] == "shrink"


# ---------------------------------------------------------------------------
# F3: ma_coil_ratio
# ---------------------------------------------------------------------------

class TestMaCoil:
    def test_three_ma_equal_coil_zero_coiled(self):
        closes = [10.0] * 20
        klines = [_kline(c) for c in closes]
        result = ecf.compute_ma_coil(klines)
        assert result["available"] is True
        assert result["coil"] == 0.0
        assert result["coiled"] is True

    def test_coil_exactly_threshold_included(self):
        # Exact construction: MA20=99, MA10=100, MA5=101 -> spread=2, median=100
        # -> coil = 2/100 = 0.02 exactly (clean integers, no float noise).
        closes = [98.0] * 10 + [99.0] * 5 + [101.0] * 5
        klines = [_kline(c) for c in closes]
        result = ecf.compute_ma_coil(klines)
        assert result["available"] is True
        assert result["coil"] == 0.02
        assert result["coiled"] is True

    def test_closes_19_fail_closed(self):
        closes = [10.0] * 19
        klines = [_kline(c) for c in closes]
        result = ecf.compute_ma_coil(klines)
        assert result["available"] is False

    def test_median_non_positive_fail_closed(self):
        closes = [0.0] * 20
        klines = [_kline(c) for c in closes]
        result = ecf.compute_ma_coil(klines)
        assert result["available"] is False


# ---------------------------------------------------------------------------
# F4: atr_contraction_pct
# ---------------------------------------------------------------------------

class TestAtrContraction:
    def _rising_range_klines(self, n):
        # Increasing daily range -> increasing ATR series.
        klines = []
        close = 10.0
        for i in range(n):
            rng = 0.1 + i * 0.05
            high = close + rng / 2
            low = close - rng / 2
            klines.append(_kline(close, high=high, low=low))
        return klines

    def test_atr_today_max_expanding(self):
        # Strictly increasing daily range -> ATR series strictly increasing ->
        # today's ATR is the series max -> pct == 1.0 -> expanding.
        klines = self._rising_range_klines(40)
        result = ecf.compute_atr_contraction(klines)
        assert result["available"] is True
        assert result["label"] == "expanding"
        assert result["pct"] == 1.0

    def test_atr_today_min_contracting_explicit(self):
        # Decreasing ranges -> ATR series decreasing -> most recent ATR is min -> contracting
        klines = []
        close = 10.0
        n = 40
        for i in range(n):
            rng = 2.0 - i * 0.04
            rng = max(rng, 0.05)
            high = close + rng / 2
            low = close - rng / 2
            klines.append(_kline(close, high=high, low=low))
        result = ecf.compute_atr_contraction(klines)
        assert result["available"] is True
        assert result["label"] == "contracting"
        assert result["pct"] == 0.0

    def test_series_length_9_fail_closed(self):
        # ATR needs period=14, so series len = n-13; need series>=10 -> n>=23
        klines = self._rising_range_klines(22)  # series len = 9
        result = ecf.compute_atr_contraction(klines)
        assert result["available"] is False

    def test_series_length_10_available(self):
        klines = self._rising_range_klines(23)  # series len = 10
        result = ecf.compute_atr_contraction(klines)
        assert result["available"] is True

    def test_high_less_than_low_does_not_crash(self):
        klines = []
        close = 10.0
        for _ in range(30):
            klines.append(_kline(close, high=close - 1.0, low=close + 1.0))  # abnormal
        result = ecf.compute_atr_contraction(klines)
        assert result is not None
        assert "available" in result


# ---------------------------------------------------------------------------
# F5: emotion_extreme synthesis
# ---------------------------------------------------------------------------

class TestEmotionExtreme:
    def test_three_bottom_hits_emotion_bottom(self):
        sub = {
            "volume_percentile_60d": {"available": True, "bucket": "cold"},
            "volume_spike_ratio": {"available": True, "label": "shrink"},
            "ma_coil_ratio": {"available": True, "coiled": True},
            "atr_contraction_pct": {"available": True, "label": "contracting"},
        }
        result = ecf.synthesize_emotion_extreme(sub)
        assert result["label"] == "emotion_bottom"
        assert result["available"] is True

    def test_two_top_hits_emotion_top(self):
        sub = {
            "volume_percentile_60d": {"available": True, "bucket": "hot"},
            "volume_spike_ratio": {"available": True, "label": "distribution_suspect"},
            "ma_coil_ratio": {"available": True, "coiled": False},
            "atr_contraction_pct": {"available": True, "label": "expanding"},
        }
        result = ecf.synthesize_emotion_extreme(sub)
        assert result["label"] == "emotion_top"

    def test_two_bottom_hits_neutral(self):
        sub = {
            "volume_percentile_60d": {"available": True, "bucket": "cold"},
            "volume_spike_ratio": {"available": True, "label": "heavy_volume"},
            "ma_coil_ratio": {"available": True, "coiled": True},
            "atr_contraction_pct": {"available": True, "label": "expanding"},
        }
        result = ecf.synthesize_emotion_extreme(sub)
        assert result["label"] == "neutral"

    def test_degraded_subfeature_recorded(self):
        sub = {
            "volume_percentile_60d": {"available": False, "value": None, "reason": "insufficient samples"},
            "volume_spike_ratio": {"available": True, "label": "shrink"},
            "ma_coil_ratio": {"available": True, "coiled": True},
            "atr_contraction_pct": {"available": True, "label": "contracting"},
        }
        result = ecf.synthesize_emotion_extreme(sub)
        assert "volume_percentile_60d" in result["degraded_features"]
        # 3 usable bottom-relevant hits remain (spike shrink, coil, atr contracting)
        assert result["label"] == "emotion_bottom"

    def test_all_fail_closed_available_false(self):
        sub = {
            "volume_percentile_60d": {"available": False, "value": None},
            "volume_spike_ratio": {"available": False, "value": None},
            "ma_coil_ratio": {"available": False, "value": None},
            "atr_contraction_pct": {"available": False, "value": None},
        }
        result = ecf.synthesize_emotion_extreme(sub)
        assert result["available"] is False
        assert result["label"] == "neutral"


# ---------------------------------------------------------------------------
# compute_emotion_features (top-level aggregator)
# ---------------------------------------------------------------------------

class TestComputeEmotionFeatures:
    def test_empty_klines_all_fail_closed(self):
        result = ecf.compute_emotion_features([])
        assert result["volume_percentile_60d"]["available"] is False
        assert result["volume_spike_ratio"]["available"] is False
        assert result["ma_coil_ratio"]["available"] is False
        assert result["atr_contraction_pct"]["available"] is False
        assert result["emotion_extreme"]["available"] is False
