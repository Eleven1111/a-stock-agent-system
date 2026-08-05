import os
import sys

import pytest

RADAR_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "announcement-radar", "scripts",
)
sys.path.insert(0, RADAR_SCRIPTS)

import recall  # noqa: E402
from classify import Classifier  # noqa: E402


@pytest.fixture(scope="module")
def rules():
    return recall.load_rules()


def _row(**kwargs):
    base = {
        "code": "000001",
        "name": "平安银行",
        "title": "关于回购股份的公告",
        "l1": ["公司治理"],
        "l2": ["回购股权"],
        "stage": "初始",
        "nss_prior": 7,
        "bayes_nss": 7,
        "industry_group": "其他",
        "polarity_flipped": False,
        "skipped": False,
        "url": "https://static.cninfo.com.cn/x.PDF",
    }
    base.update(kwargs)
    return base


# --------------------------------------------------------------------- 桶规则

def test_divergence_bucket_needs_threshold(rules):
    assert recall.assign_buckets(_row(nss_prior=0, bayes_nss=6), rules) == ["divergence"]
    assert recall.assign_buckets(_row(nss_prior=4, bayes_nss=6), rules) == []


def test_unmatched_bucket_catches_strong_event_without_category(rules):
    row = _row(
        title="中国核电关于辽宁庄河核电、浙江金七门核电项目核准的公告",
        l1=[], l2=[], nss_prior=None, bayes_nss=None, skipped=False,
    )
    assert recall.assign_buckets(row, rules) == ["unmatched"]


def test_unmatched_bucket_ignores_classified_rows(rules):
    row = _row(title="关于中标重大合同的公告", l2=["重大合同"], nss_prior=3, bayes_nss=3)
    assert "unmatched" not in recall.assign_buckets(row, rules)


def test_unmatched_bucket_ignores_weak_titles(rules):
    row = _row(title="关于变更保荐代表人的公告", l1=[], l2=[], nss_prior=None, bayes_nss=None)
    assert recall.assign_buckets(row, rules) == []


def test_extreme_bucket_excludes_routine_noise(rules):
    # 回购/股权激励是每日 ~105 条的常态噪音，先验再高也不进 C 桶
    assert recall.assign_buckets(_row(l2=["回购股权"]), rules) == []
    assert recall.assign_buckets(_row(l2=["股权激励"]), rules) == []
    assert recall.assign_buckets(_row(l2=["资产重组"], nss_prior=6, bayes_nss=6), rules) == ["extreme"]


def test_skipped_periodic_rows_never_recalled(rules):
    row = _row(l2=["年度报告"], nss_prior=None, bayes_nss=None, skipped=True)
    assert recall.assign_buckets(row, rules) == []


def test_row_can_land_in_multiple_buckets_in_stable_order(rules):
    row = _row(l2=["资产重组"], nss_prior=6, bayes_nss=1)
    assert recall.assign_buckets(row, rules) == ["divergence", "extreme"]


# --------------------------------------------------------- 投影 / 护栏 / 简报

def test_projection_drops_everything_but_whitelist(rules):
    result = recall.select([_row(l2=["资产重组"], nss_prior=6, bayes_nss=6, text="正文" * 5000)], rules)
    assert set(result["rows"][0]) == set(recall.PROJECTION_FIELDS)
    assert "text" not in result["rows"][0]
    assert result["rows"][0]["l2"] == "资产重组"


def test_select_counts_per_bucket_and_unique_companies(rules):
    rows = [
        _row(code="000001", l2=["资产重组"], nss_prior=6, bayes_nss=6),
        _row(code="000001", l2=["资产重组"], nss_prior=6, bayes_nss=6),
        _row(code="600519", nss_prior=0, bayes_nss=6),
    ]
    result = recall.select(rows, rules)
    assert result["counts"]["extreme"] == 2
    assert result["counts"]["divergence"] == 1
    assert result["companies"] == 2


def test_guardrail_raises_when_recall_size_explodes(rules):
    with pytest.raises(recall.RecallGuardrailError):
        recall.check_guardrails(fetched=1400, classified_rate=0.92, selected=900, rules=rules)


def test_guardrail_raises_when_recall_collapses(rules):
    with pytest.raises(recall.RecallGuardrailError):
        recall.check_guardrails(fetched=1400, classified_rate=0.92, selected=3, rules=rules)


def test_guardrail_only_warns_on_thin_fetch(rules):
    # 非交易日/接口未放量：规模检查不适用，只告警
    warnings = recall.check_guardrails(fetched=50, classified_rate=0.92, selected=4, rules=rules)
    assert any("抓取量" in w for w in warnings)


def test_guardrail_warns_on_low_classification_rate(rules):
    warnings = recall.check_guardrails(fetched=1400, classified_rate=0.70, selected=160, rules=rules)
    assert any("命中率" in w for w in warnings)


def test_brief_is_bounded_and_omits_urls(rules):
    rows = [_row(code=f"{i:06d}", nss_prior=0, bayes_nss=6) for i in range(50)]
    result = recall.select(rows, rules)
    brief = recall.build_brief(result, rules, day="2026-08-01")
    assert "https://" not in brief
    assert "另 45 条见 artifact" in brief
    assert len(brief) < 2400


# ------------------------------------------------------------- 分类 bug 回归

def test_level1_labels_are_deduped():
    """两个不同二级分类可能同属一个大类，拼接前必须去重。

    2026-08-01 实测有 92 条形如 `公司治理|公司治理` 的记录。
    """
    classifier = Classifier()
    for title in (
        "关于变更公司注册资本及修订公司章程并办理工商变更登记的公告",
        "关于董事会、监事会换届选举及聘任高级管理人员的公告",
    ):
        labels = classifier.classify(title)["l1"]
        assert len(labels) == len(set(labels)), (title, labels)


# ----------------------------------------------------------- 行业映射口径

@pytest.fixture(scope="module")
def scorer():
    from score import Scorer
    return Scorer()


def test_industry_tables_are_internally_consistent(scorer):
    groups = {"科技", "消费", "周期", "其他"}
    assert set(scorer.ind_map.values()) <= groups
    assert set(scorer.ind_map_fallback.values()) <= groups
    # 两表键若重叠且结论不同，industry_group 的优先级就成了隐性行为
    for name in set(scorer.ind_map) & set(scorer.ind_map_fallback):
        assert scorer.ind_map[name] == scorer.ind_map_fallback[name], name


def test_fallback_table_covers_the_live_cache_taxonomy(scorer):
    """兜底表不能悄悄缩水。

    2026-08-04 缓存有 127 个行业名，只查 sw_to_group 时识别率仅 7.9%。
    """
    assert len(scorer.ind_map_fallback) >= 120


@pytest.mark.parametrize("name,group", [
    # 申万一级仍走原表
    ("医药生物", "科技"), ("机械设备", "周期"), ("银行", "其他"),
    # 东财/国民经济分类走兜底表，语义与申万一致
    ("生物制药", "科技"), ("医药制造业", "科技"), ("医疗器械", "科技"),
    ("软件和信息技术服务业", "科技"), ("发电设备", "科技"), ("船舶制造", "科技"),
    ("食品行业", "消费"), ("酿酒行业", "消费"), ("商业百货", "消费"),
    ("玻璃行业", "周期"), ("专用设备制造业", "周期"), ("煤炭行业", "周期"),
    # 仪器仪表属申万「机械设备」，不是电子——最容易归错的一类
    ("仪器仪表", "周期"), ("仪器仪表制造业", "周期"),
    ("金融行业", "其他"), ("次新股", "其他"), ("综合行业", "其他"),
])
def test_industry_group_maps_both_taxonomies(scorer, name, group):
    assert scorer.industry_group(name) == group


def test_unknown_industry_falls_back_to_other(scorer):
    assert scorer.industry_group("这不是一个行业名") == "其他"
    assert scorer.industry_group("") == "其他"
    assert scorer.industry_group(None) == "其他"


def test_knows_industry_separates_recognition_from_grouping(scorer):
    """「金融行业」归其他但**是**已识别的；用 group != 其他 度量覆盖率会误判。"""
    assert scorer.industry_group("金融行业") == "其他"
    assert scorer.knows_industry("金融行业") is True
    assert scorer.knows_industry("这不是一个行业名") is False
    assert scorer.knows_industry(None) is False
