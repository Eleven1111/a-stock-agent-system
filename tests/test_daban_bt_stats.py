"""打板回测统计层 — 合成数据单测（期望值独立手推）"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "chanlun-backtest" / "scripts" / "daban_bt_stats.py"
SPEC = importlib.util.spec_from_file_location("daban_bt_stats", SCRIPT)
st = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(st)


def test_summarize_known():
    s = st.summarize([0.1, -0.1, 0.2, 0.0])
    assert s["n"] == 4
    assert s["mean"] == pytest.approx(0.05)
    assert s["win_rate"] == pytest.approx(0.5)  # 0.1,0.2 >0 → 2/4


def test_summarize_empty():
    s = st.summarize([])
    assert s["n"] == 0 and s["mean"] == 0.0


def test_t_test_strong_mean_is_significant():
    t, p = st.t_test_vs_zero([0.05] * 30)  # 近零方差正均值 → t 极大, p≈0
    assert t > 1e6 and p < 1e-6


def test_t_test_exact_zero_std_returns_inf():
    # 构造精确零方差（整数 0.0），命中 sd==0 分支
    t, p = st.t_test_vs_zero([0.0, 0.0, 0.0])
    assert t == 0.0 and p == 1.0


def test_t_test_zero_mean_not_significant():
    t, p = st.t_test_vs_zero([0.1, -0.1, 0.1, -0.1, 0.1, -0.1])
    assert abs(t) < 1e-9
    assert p == pytest.approx(1.0)


def test_t_test_needs_two_points():
    assert st.t_test_vs_zero([0.05]) == (0.0, 1.0)


def test_bootstrap_ci_brackets_mean():
    data = [0.02] * 50 + [0.0] * 50  # 均值 0.01
    lo, hi = st.bootstrap_ci_mean(data, n_boot=2000, seed=1)
    assert lo <= 0.01 <= hi
    assert lo < hi


def test_permutation_identical_groups_high_p():
    res = st.permutation_test_diff([0.01, 0.02, 0.0, 0.01], [0.01, 0.02, 0.0, 0.01],
                                   n_perm=2000, seed=7)
    assert res["observed_diff"] == pytest.approx(0.0)
    assert res["p_value"] > 0.5


def test_permutation_separated_groups_low_p():
    signal = [0.05, 0.06, 0.055, 0.052, 0.058, 0.05, 0.06, 0.054]
    control = [-0.05, -0.06, -0.055, -0.052, -0.058, -0.05, -0.06, -0.054]
    res = st.permutation_test_diff(signal, control, n_perm=5000, seed=3)
    assert res["p_value"] < 0.05


def test_benjamini_hochberg_known_case():
    # p=[0.01,0.04,0.03,0.20], m=4, q=0.10 → adjusted=[0.04,0.0533,0.0533,0.20], reject=[T,T,T,F]
    out = st.benjamini_hochberg([0.01, 0.04, 0.03, 0.20], q=0.10)
    adj = [o["adjusted"] for o in out]
    rej = [o["reject"] for o in out]
    assert adj[0] == pytest.approx(0.04)
    assert adj[1] == pytest.approx(0.04 * 4 / 3)   # 0.0533
    assert adj[2] == pytest.approx(0.04 * 4 / 3)
    assert adj[3] == pytest.approx(0.20)
    assert rej == [True, True, True, False]


def test_benjamini_hochberg_empty():
    assert st.benjamini_hochberg([]) == []
