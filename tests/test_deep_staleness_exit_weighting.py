"""深研过期治理阶梯 — fail-closed 退出加权（P2 补丁）。

阶梯语义（config/scoring.yaml: scoring.deep_staleness.exclude_after_extra_days）：
  新鲜（age <= max_age_days=90）：满权重，现状不变。
  过渡衰减（90 < age <= 90+exclude_after_extra_days）：decay_stale_score 向 PE 快照
    回归，仍以满权重参与四维合成——过渡带。
  重度过期（age > 90+exclude_after_extra_days）：深度面退出合成加权，其余三维权重
    按比例归一化后合成总分。
"""

import subprocess
import sys
import textwrap
from datetime import date, timedelta
from pathlib import Path

import deep_research_cache as drc
import four_dim_scorer as fds

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _quote(pe=12.0, cap=100.0):
    return {
        "price": 10.0,
        "change_pct": 1.0,
        "turnover": 5.0,
        "amount": 1e8,
        "pe": pe,
        "market_cap": cap,
    }


def _klines():
    return [{"close": 10.0, "high": 10.2, "low": 9.8, "volume": 1000} for _ in range(60)]


def _write_deep(age_days, total=90.0):
    old = (date.today() - timedelta(days=age_days)).isoformat()
    drc.write_deep_research(
        "000021", "深科技",
        {"total": total, "rating": "强烈看多（非投资建议）", "dimensions": {}},
        asof=old,
    )


def _mock_non_deep_dims(monkeypatch, deep_score=None):
    """把 technical/sentiment/catalyst 钉死为已知值，只让 deep 走真实 score_deep。"""
    monkeypatch.setattr(
        fds, "score_technical",
        lambda *a, **k: {"score": 6.0, "ma5": 10.0, "price": 10.0, "detail": "t"},
    )
    monkeypatch.setattr(
        fds, "score_sentiment",
        lambda *a, **k: {"score": 6.0, "change_pct": 1.0, "detail": "s"},
    )
    monkeypatch.setattr(
        fds, "score_catalyst",
        lambda *a, **k: {"score": 6.0, "available": True, "news_count": 1, "detail": "c"},
    )


# ========== 三档阶梯（score_stock 合成层） ==========


def test_fresh_deep_full_weight_in_synthesis(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_non_deep_dims(monkeypatch)
    _write_deep(age_days=10, total=90.0)  # deep_score=9.0, fresh

    result = fds.score_stock("000021", "深科技", quote=_quote(), klines=_klines(), market_ctx={})

    assert result["scores"]["deep"]["source"] == "serenity_deep"
    assert "deep" not in result["excluded_dims"]
    assert result["deep_excluded"] is False
    # 满权重：0.30*6 + 0.15*6 + 0.30*6 + 0.25*9 = 6.75（+coherence delta 若触发）
    expected = round(0.30 * 6.0 + 0.15 * 6.0 + 0.30 * 6.0 + 0.25 * 9.0, 1)
    assert result["weighted"] == expected


def test_transition_band_decays_but_full_weight(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_non_deep_dims(monkeypatch)
    _write_deep(age_days=105, total=90.0)  # extra=15 → decay 8.7 (pe_score=7.0 from pe=12)

    result = fds.score_stock("000021", "深科技", quote=_quote(pe=12.0), klines=_klines(), market_ctx={})

    deep = result["scores"]["deep"]
    assert deep["source"] == "serenity_deep_stale"
    assert deep["score"] == 8.7
    assert "deep" not in result["excluded_dims"]
    assert result["deep_excluded"] is False
    expected = round(0.30 * 6.0 + 0.15 * 6.0 + 0.30 * 6.0 + 0.25 * 8.7, 1)
    assert result["weighted"] == expected


def test_severe_stale_excludes_deep_and_renormalizes_three_dims(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_non_deep_dims(monkeypatch)
    _write_deep(age_days=135, total=90.0)  # extra=45 > 30 → severe exclusion

    result = fds.score_stock("000021", "深科技", quote=_quote(pe=12.0), klines=_klines(), market_ctx={})

    deep = result["scores"]["deep"]
    assert deep["source"] == "serenity_deep_excluded"
    assert deep["excluded"] is True
    assert "deep" in result["excluded_dims"]
    assert result["deep_excluded"] is True

    # 三维归一化手算：base weights technical=0.30 sentiment=0.15 catalyst=0.30 deep=0.25
    # 归一化后 technical/sentiment/catalyst 比例不变，总和=1。
    remaining = {"technical": 0.30, "sentiment": 0.15, "catalyst": 0.30}
    total = sum(remaining.values())
    normed = {k: v / total for k, v in remaining.items()}
    expected_weighted = round(
        6.0 * normed["technical"] + 6.0 * normed["sentiment"] + 6.0 * normed["catalyst"], 1
    )
    assert result["weighted"] == expected_weighted
    # effective_weights 应反映归一化后的三维权重、且不含 deep 的贡献
    eff = result["effective_weights"]
    assert eff["technical"] == f"{normed['technical']*100:.0f}%"
    assert eff["sentiment"] == f"{normed['sentiment']*100:.0f}%"
    assert eff["catalyst"] == f"{normed['catalyst']*100:.0f}%"


# ========== 配置缺失/损坏 fail-closed 回退默认 30 ==========


_SUBPROCESS_PRELUDE = """
import sys
sys.path.insert(0, {stock_triage!r})
sys.path.insert(0, {common!r})
sys.path.insert(0, {root!r})
import config_registry
config_registry.config_path = lambda name: {config_path!r}
import four_dim_scorer as fds
print(fds.DEEP_STALENESS_EXCLUDE_AFTER_EXTRA_DAYS)
"""


def _run_in_subprocess(config_path_value: str) -> int:
    """在独立子进程里以打了补丁的 config_path 重新导入 four_dim_scorer，
    避免 importlib.reload 与其他测试对 sys.modules['four_dim_scorer'] 的
    污染（scripts/four_dim_scorer.py 兼容 wrapper 会通过 runpy 抢占模块名）。"""
    script = _SUBPROCESS_PRELUDE.format(
        stock_triage=str(_REPO_ROOT / "skills" / "stock-triage" / "scripts"),
        common=str(_REPO_ROOT / "skills" / "common"),
        root=str(_REPO_ROOT),
        config_path=config_path_value,
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(_REPO_ROOT), timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return int(result.stdout.strip().splitlines()[-1])


def test_config_missing_falls_back_to_default_30():
    assert _run_in_subprocess("/nonexistent/scoring.yaml") == 30


def test_config_corrupt_value_falls_back_to_default_30(tmp_path):
    bad_yaml = tmp_path / "scoring.yaml"
    bad_yaml.write_text(
        textwrap.dedent("""\
            scoring:
              weights:
                default: {technical: 0.30, sentiment: 0.15, catalyst: 0.30, deep: 0.25}
              deep_staleness:
                exclude_after_extra_days: not_a_number
            risk: {}
        """),
        encoding="utf-8",
    )
    assert _run_in_subprocess(str(bad_yaml)) == 30


# ========== 温度 overlay 交互：deep 退出加权时 overlay 的 deep 调整不生效 ==========


def test_extreme_hot_overlay_deep_adjustment_voided_when_excluded(tmp_path, monkeypatch):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_non_deep_dims(monkeypatch)
    _write_deep(age_days=135, total=90.0)  # severe exclusion

    result = fds.score_stock(
        "000021", "深科技", quote=_quote(pe=12.0), klines=_klines(),
        market_ctx={}, temperature_tier="极热",
    )

    assert result["deep_excluded"] is True
    assert "deep" in result["excluded_dims"]

    # 极热 overlay: {sentiment: +0.10, deep: -0.10}；deep 退出后其自身权重被整体丢弃，
    # sentiment 的 +0.10 仍应保留在归一化前的技术/情绪/催化比例中生效。
    base = {"technical": 0.30, "sentiment": 0.15, "catalyst": 0.30, "deep": 0.25}
    overlay = {"sentiment": 0.10, "deep": -0.10}
    adjusted = dict(base)
    for dim, delta in overlay.items():
        adjusted[dim] = max(0.05, adjusted[dim] + delta)
    total = sum(adjusted.values())
    adjusted = {k: v / total for k, v in adjusted.items()}

    remaining = {k: v for k, v in adjusted.items() if k != "deep"}
    rtotal = sum(remaining.values())
    normed = {k: v / rtotal for k, v in remaining.items()}

    expected_weighted = round(
        6.0 * normed["technical"] + 6.0 * normed["sentiment"] + 6.0 * normed["catalyst"], 1
    )
    assert result["weighted"] == expected_weighted
    # sentiment 份额应高于纯三维等比归一化(0.30/0.15/0.30归一)下的情绪份额，
    # 证明 overlay 的 sentiment 加成仍然生效，未被 deep 排除连带清空。
    pure_normed_sentiment = 0.15 / (0.30 + 0.15 + 0.30)
    assert normed["sentiment"] > pure_normed_sentiment


# ========== 作废证据不得经由旁路继续投票（coherence / S 档闸门） ==========
#
# 退出加权合成只堵住了一条路。deep 被判 excluded 后，它的分值仍留在
# scores["deep"]["score"]（而且是**未衰减的原始值**），下面两条路径原本还在读它：
#   1. _detect_coherence 的多空维度计数与「技术强但基本面差」冲突判定；
#   2. S 档闸门 `deep["score"] < 6.0`——一份数月前的高分能替 S 档背书。
# 两处都实测复现过，这里锁死。

def _mock_dims(monkeypatch, tech, sent, cata):
    for name, payload in (
        ("score_technical", {"score": tech, "ma5": 10.0, "price": 10.0, "detail": "t"}),
        ("score_sentiment", {"score": sent, "change_pct": 1.0, "detail": "s"}),
        ("score_catalyst", {"score": cata, "available": True, "news_count": 1, "detail": "c"}),
    ):
        monkeypatch.setattr(fds, name, (lambda p: (lambda *a, **k: p))(payload))


def _score_with(monkeypatch, tmp_path, *, tech, sent, cata, age_days, total):
    monkeypatch.delenv("A_STOCK_STATE_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _mock_dims(monkeypatch, tech, sent, cata)
    _write_deep(age_days=age_days, total=total)
    return fds.score_stock("000021", "深科技", quote=_quote(), klines=_klines(), market_ctx={})


def test_excluded_deep_does_not_count_toward_resonance(tmp_path, monkeypatch):
    """两维看多 + 作废的高分 deep：不得凑成「多维共振看多」(>=3) 拿 +0.5。"""
    result = _score_with(monkeypatch, tmp_path,
                         tech=7.0, sent=7.0, cata=5.0, age_days=135, total=90.0)

    assert result["scores"]["deep"]["excluded"] is True
    assert result["scores"]["deep"]["score"] >= 6.5, "前提：作废分值本身达到看多档"
    assert result["coherence"]["bullish_dims"] == 2
    assert "多维共振看多" not in result["coherence"]["tags"]
    assert result["coherence"]["delta"] == 0.0


def test_non_excluded_deep_still_counts_toward_resonance(tmp_path, monkeypatch):
    """对照组：同样两维看多，deep 新鲜时应当照常凑成共振——证明上一条不是恒真。"""
    result = _score_with(monkeypatch, tmp_path,
                         tech=7.0, sent=7.0, cata=5.0, age_days=10, total=90.0)

    assert result["scores"]["deep"].get("excluded") is not True
    assert result["coherence"]["bullish_dims"] == 3
    assert "多维共振看多" in result["coherence"]["tags"]
    assert result["coherence"]["delta"] == 0.5


def test_excluded_deep_does_not_trigger_fundamental_conflict(tmp_path, monkeypatch):
    """技术强 + 作废的低分 deep：不得打「投机性反弹风险」标签。"""
    result = _score_with(monkeypatch, tmp_path,
                         tech=8.0, sent=5.0, cata=5.0, age_days=135, total=20.0)

    assert result["scores"]["deep"]["excluded"] is True
    assert result["scores"]["deep"]["score"] <= 3.0, "前提：作废分值本身在看空档"
    assert "投机性反弹风险" not in result["coherence"]["tags"]
    assert "技术走强但基本面差" not in result["coherence"]["conflicts"]


def test_s_grade_gate_fails_closed_when_deep_excluded(tmp_path, monkeypatch):
    """没有有效深研证据就不能认定 S 档，无论作废分值多高。"""
    high = _score_with(monkeypatch, tmp_path,
                       tech=9.5, sent=9.5, cata=9.5, age_days=135, total=90.0)
    assert high["scores"]["deep"]["excluded"] is True
    assert high["scores"]["deep"]["score"] >= 6.0, "前提：作废分值高于闸门阈值"
    assert "insufficient_catalyst_or_deep_for_s" in high["score_gates"]
    assert high["grade"] != "S"
