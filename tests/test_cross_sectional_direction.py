"""横截面方向检验 —— 回答「高分是否真的比低分好」。

现有闸门只做事件级 T+1/T+3 净收益，回答不了排序方向。trend_score 正是从这个
盲区漏过去的：它从未被要求证明高分优于低分（2026-08-08 实测中窗口 rank IC
-0.34、8/8 队列全负）。
"""

import cross_sectional_direction as csd


def _cohort(src, dst, scores_and_returns, n=120):
    """构造一个队列：scores_and_returns 给出 (score, ret) 的生成规律。"""
    pairs = [scores_and_returns(i, n) for i in range(n)]
    return {"src": src, "dst": dst, "pairs": pairs}


def _aligned(i, n):
    """高分高收益（方向正确）。"""
    return (float(n - i), 0.001 * (n - i))


def _inverted(i, n):
    """高分低收益（方向反了）—— trend_score 的实测形态。"""
    return (float(n - i), 0.001 * i)


def test_small_sample_is_insufficient_not_confirmed():
    """样本不足必须报 insufficient，绝不能因为「碰巧全正」就判通过。"""
    result = csd.evaluate([_cohort("2026-07-16", "2026-07-28", _aligned, n=30)])

    assert result["verdict"] == "insufficient_sample"
    assert result["usable_cohorts"] == 0


def test_aligned_direction_needs_enough_independent_cohorts():
    """方向对但独立队列不够 —— 仍然不能判通过。

    这是我自己在 2026-08-08 那次分析里踩的坑：15 个窗口看着很多，但前瞻窗口
    高度重叠、多数共享同一终点，有效独立观测只有 2~3 个。
    """
    overlapping = [
        _cohort("2026-07-16", "2026-08-07", _aligned),
        _cohort("2026-07-17", "2026-08-07", _aligned),
        _cohort("2026-07-20", "2026-08-07", _aligned),
    ]

    result = csd.evaluate(overlapping)

    assert result["usable_cohorts"] == 3
    assert result["independent_cohorts"] == 1     # 三个窗口互相重叠，只算一个
    assert result["verdict"] == "insufficient_independent_cohorts"


def test_inverted_direction_is_reported_explicitly():
    """方向反了必须单独成一类，不能和「没信号」混为一谈。"""
    cohorts = [
        _cohort("2026-07-01", "2026-07-08", _inverted),
        _cohort("2026-07-09", "2026-07-16", _inverted),
        _cohort("2026-07-17", "2026-07-24", _inverted),
        _cohort("2026-07-25", "2026-08-01", _inverted),
        _cohort("2026-08-02", "2026-08-09", _inverted),
    ]

    result = csd.evaluate(cohorts)

    assert result["verdict"] == "direction_inverted"
    assert result["mean_ic"] < 0
    assert result["positive_ic_ratio"] == 0.0
    assert result["independent_cohorts"] == 5


def test_confirmed_direction_requires_consistency_across_independent_cohorts():
    cohorts = [
        _cohort("2026-07-01", "2026-07-08", _aligned),
        _cohort("2026-07-09", "2026-07-16", _aligned),
        _cohort("2026-07-17", "2026-07-24", _aligned),
        _cohort("2026-07-25", "2026-08-01", _aligned),
        _cohort("2026-08-02", "2026-08-09", _aligned),
    ]

    result = csd.evaluate(cohorts)

    assert result["verdict"] == "direction_confirmed"
    assert result["mean_ic"] > 0
    assert result["positive_ic_ratio"] == 1.0
    assert result["decile_spread"] > 0


def test_mixed_signs_are_no_direction_not_confirmed():
    """正负横跳 = 没信号（daban_score 的实测形态），不得判通过。"""
    cohorts = [
        _cohort("2026-07-01", "2026-07-08", _aligned),
        _cohort("2026-07-09", "2026-07-16", _inverted),
        _cohort("2026-07-17", "2026-07-24", _aligned),
        _cohort("2026-07-25", "2026-08-01", _inverted),
        _cohort("2026-08-02", "2026-08-09", _aligned),
    ]

    result = csd.evaluate(cohorts)

    assert result["verdict"] == "no_direction"


def test_constant_scores_cannot_produce_a_verdict():
    """全常数分数（volume_ratio_5d 恒 0 的形态）秩相关无定义，必须跳过而不是算出 0。"""
    flat = [_cohort("2026-07-01", "2026-07-08", lambda i, n: (1.0, 0.001 * i))]

    result = csd.evaluate(flat)

    assert result["usable_cohorts"] == 0
    assert result["verdict"] == "insufficient_sample"
