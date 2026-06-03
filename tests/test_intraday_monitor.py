"""盘中异动监控 — 新一天首次运行不得清空刚生成的告警（顺序 bug 回归）。"""

import intraday_monitor as im


def _stub_market(monkeypatch):
    monkeypatch.setattr(im, "TRACKED_CODES", ["600001"])
    monkeypatch.setattr(im, "TRACKED_NAMES", {"600001": "测试股"})
    monkeypatch.setattr(
        im, "fetch_realtime",
        lambda code: {"price": 11.0, "change_pct": 9.8, "turnover": 5.0, "amount": 1e8},
    )


def test_new_day_first_run_keeps_alerts(tmp_path, monkeypatch):
    """昨日缓存残留时，新一天首次运行应正常产出告警，而非循环后被一并清空。"""
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    _stub_market(monkeypatch)

    # 注入陈旧缓存（昨天 + 已记录的告警键）
    im.save_alert_cache({"_date": "20200101", "zt_600001": "09:30"})

    data = im.check_intraday()
    assert data["has_alerts"] is True, "新一天首次运行不应吞掉刚生成的告警"
    assert any(a["type"] == "涨停" for a in data["alerts"])


def test_same_day_dedup_still_works(tmp_path, monkeypatch):
    """同一天内重复触发应去重：第二次运行不再重复报同一涨停。"""
    monkeypatch.setattr(im, "ALERT_CACHE", str(tmp_path / "intraday_alerts.json"))
    _stub_market(monkeypatch)

    first = im.check_intraday()
    assert first["has_alerts"] is True

    second = im.check_intraday()
    assert not any(a["type"] == "涨停" for a in second["alerts"]), "同日涨停应已去重"
