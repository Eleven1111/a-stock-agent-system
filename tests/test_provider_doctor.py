"""Provider canary reports health per dataset instead of per library."""

from __future__ import annotations

from scripts import provider_doctor


def test_optional_dataset_failure_degrades_without_hiding_healthy_datasets(monkeypatch):
    monkeypatch.setattr(
        provider_doctor,
        "PROBES",
        {
            "tencent_quote": {
                "provider": "tencent",
                "required": True,
                "call": lambda: {"sh000001": {"price": 3000}},
            },
            "eastmoney_fund_flow": {
                "provider": "eastmoney",
                "required": False,
                "call": lambda: (_ for _ in ()).throw(ConnectionError("blocked")),
            },
            "akshare_limitup": {
                "provider": "akshare_push2ex",
                "required": False,
                "call": lambda: [{"code": "600001"}],
            },
        },
    )

    report = provider_doctor.run_probes()

    assert report["status"] == "degraded"
    assert report["datasets"]["tencent_quote"]["status"] == "ok"
    assert report["datasets"]["eastmoney_fund_flow"]["status"] == "error"
    assert report["datasets"]["akshare_limitup"]["status"] == "ok"


def test_required_dataset_failure_is_error(monkeypatch):
    monkeypatch.setattr(
        provider_doctor,
        "PROBES",
        {
            "tencent_quote": {
                "provider": "tencent",
                "required": True,
                "call": lambda: {},
            }
        },
    )

    assert provider_doctor.run_probes()["status"] == "error"
