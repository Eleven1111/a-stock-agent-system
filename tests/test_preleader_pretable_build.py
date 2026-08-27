"""S4 盘前表构建器的证据纪律：缺证据必须显式退化，不许洗成"干净"。"""

import json

import pytest

from scripts import preleader_pretable_build as builder


def _pool(tmp_path, *, asof="2026-08-07", rows=None):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({
        "asof": asof,
        "candidates": rows if rows is not None else [
            {"code": "600001", "name": "龙头", "sector": "通信设备",
             "leader_role": "sector_leader", "is_st": False},
            {"code": "600002", "name": "跟风一", "sector": "通信设备",
             "leader_role": "sector_follower", "is_st": False},
            {"code": "600003", "name": "跟风二", "sector": "通信设备",
             "leader_role": "sector_follower", "is_st": False},
        ],
    }), encoding="utf-8")
    return str(path)


def test_input_asof_mismatch_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    with pytest.raises(ValueError, match="asof mismatch"):
        builder.build(_pool(tmp_path, asof="2026-08-06"), as_of="2026-08-07")


def test_missing_liquidity_source_degrades_instead_of_excluding_everyone(tmp_path, monkeypatch):
    """缓存整体缺失时不能出一张"所有人都流动性不足"的表。

    那种表和真表在下游长得一模一样，会被当作有效盘前表使用——「空集恒真」的
    另一种形态。必须是 degraded + 命名缺口。
    """
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover", lambda codes, as_of: {})
    result = builder.build(_pool(tmp_path), as_of="2026-08-07")

    assert result["status"] == "degraded"
    assert "avg_turnover_20d_source_unavailable" in result["evidence_gaps"]
    assert result["pretable"] is None


def test_announcement_scan_failure_excludes_the_code_rather_than_assuming_clean(
    tmp_path, monkeypatch
):
    """取数失败的票不进成分股池，并留痕；把失败当成"没有利空"是把未知洗成干净。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover",
                        lambda codes, as_of: {code: 5e7 for code in codes})
    monkeypatch.setattr(builder, "scan_material_bad_news",
                        lambda codes, as_of: ({"600002": False}, ["600003"]))
    result = builder.build(_pool(tmp_path), as_of="2026-08-07")

    assert result["status"] == "ok"
    assert result["announcement_scan_failed"] == ["600003"]
    entry = result["pretable"]["entries"][0]
    assert entry["candidates"] == ["600002"]
    assert "600003" not in entry["candidates"]
    # 也不能被记成"因利空排除"——它根本没被扫到。
    assert all(item["code"] != "600003" for item in entry["excluded"])


def test_material_bad_news_member_is_excluded_with_reason(tmp_path, monkeypatch):
    """正向对照：证据齐全时确实建得出表，且利空票带原因进 excluded。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover",
                        lambda codes, as_of: {code: 5e7 for code in codes})
    monkeypatch.setattr(builder, "scan_material_bad_news",
                        lambda codes, as_of: ({"600002": False, "600003": True}, []))
    result = builder.build(_pool(tmp_path), as_of="2026-08-07")

    entry = result["pretable"]["entries"][0]
    assert entry["leader_code"] == "600001"
    assert entry["candidates"] == ["600002"]
    assert {"code": "600003", "reason": "material_bad_news"} in entry["excluded"]


def test_oversized_member_pool_refuses_to_silently_truncate(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    rows = [{"code": "600001", "sector": "通信设备", "leader_role": "sector_leader"}]
    rows += [{"code": f"6001{i:02d}", "sector": "通信设备", "leader_role": "sector_follower"}
             for i in range(10)]
    result = builder.build(_pool(tmp_path, rows=rows), as_of="2026-08-07", max_scan_codes=3)

    assert result["status"] == "degraded"
    assert "member_pool_exceeds_announcement_scan_budget" in result["evidence_gaps"]


def test_degraded_rerun_does_not_overwrite_a_usable_pretable(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover",
                        lambda codes, as_of: {code: 5e7 for code in codes})
    monkeypatch.setattr(builder, "scan_material_bad_news",
                        lambda codes, as_of: ({"600002": False, "600003": False}, []))
    good = builder.run(_pool(tmp_path), as_of="2026-08-07")
    assert good["status"] == "ok"

    monkeypatch.setattr(builder, "average_turnover", lambda codes, as_of: {})
    again = builder.run(_pool(tmp_path), as_of="2026-08-07")
    assert again["status"] == "ok"
    assert builder.load_pretable("2026-08-07")[0] is not None


def test_load_pretable_refuses_a_degraded_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover", lambda codes, as_of: {})
    builder.run(_pool(tmp_path), as_of="2026-08-07")

    pretable, reason = builder.load_pretable("2026-08-07")
    assert pretable is None
    assert reason.startswith("pretable_degraded:")


def test_previous_trading_asof_walks_back_over_gaps(tmp_path, monkeypatch):
    """按产物实际日期回溯，而不是日历减一天——否则停摆会被误报成表缺失。"""
    monkeypatch.setenv("A_STOCK_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(builder, "average_turnover",
                        lambda codes, as_of: {code: 5e7 for code in codes})
    monkeypatch.setattr(builder, "scan_material_bad_news",
                        lambda codes, as_of: ({"600002": False, "600003": False}, []))
    builder.run(_pool(tmp_path, asof="2026-08-04"), as_of="2026-08-04")

    assert builder.previous_trading_asof("2026-08-07") == "2026-08-04"
    # 严格早于：同日的表不算 D-1 盘前表。
    assert builder.previous_trading_asof("2026-08-04") is None


def test_mandatory_periodic_disclosure_is_not_read_as_material_bad_news(monkeypatch):
    """「非经营性资金占用…汇总表」是定期报告必交件，主题是"不存在该风险"。

    纯子串匹配会把它读成「资金占用」硬风险。2026-08-27 实测：抽样 25 只被判利空的
    成分股全部由这一条触发，定期报告季会把接近全市场判成有利空。
    """
    monkeypatch.setattr(builder.announcement_risk, "scan_many", lambda codes: {
        "600002": [{"date": "2026-08-07",
                    "title": "2026年半年度非经营性资金占用及其他关联资金往来情况汇总表"}],
    })
    flags, failed = builder.scan_material_bad_news(["600002"], "2026-08-07")
    assert failed == []
    assert flags["600002"] is False


def test_a_real_hard_risk_announcement_still_flags(monkeypatch):
    """正向对照：真利空必须照旧判出来，否则上面那条护栏就成了「永不排除」。"""
    monkeypatch.setattr(builder.announcement_risk, "scan_many", lambda codes: {
        "600003": [{"date": "2026-08-07",
                    "title": "关于公司收到中国证监会立案调查通知书的公告"}],
    })
    flags, _ = builder.scan_material_bad_news(["600003"], "2026-08-07")
    assert flags["600003"] is True
