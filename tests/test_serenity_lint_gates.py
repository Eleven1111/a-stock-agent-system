"""Serenity P2 methodology hard-gate tests.

Covers the new deep industry-chain scan gates added to report_lint.py and the
`red_flag` claim-type added to evidence_ledger.py. These enforce the muxuuu
methodology discipline (dual ranking, downgraded-hot-direction chapter, minimum
source count, red-flag disclosure) without disturbing the existing single_stock
lint contract.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERENITY_SCRIPTS = ROOT / "skills" / "serenity-investment-research" / "scripts"


def load_module(name: str, relpath: Path):
    spec = importlib.util.spec_from_file_location(name, relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report_lint = load_module("serenity_report_lint", SERENITY_SCRIPTS / "report_lint.py")
evidence_ledger = load_module("serenity_evidence_ledger", SERENITY_SCRIPTS / "evidence_ledger.py")


# --- shared fixtures -------------------------------------------------------

BASE_SECTIONS = (
    "## 一句话结论\n判断先行。\n"
    "## 评分\n评分表。\n"
    "## 风险\n风险表。\n"
    "## 反面\n反面证据。\n"
    "## 资料来源\nhttps://a | S |\nhttps://b | A |\nhttps://c | B |\n"
    "本报告仅用于研究和信息整理，不构成任何投资建议。\n"
)


def _evidence(n_sources: int, *, research_type: str = "industry_chain", red_flags: int = 0):
    entries = []
    for i in range(n_sources):
        entries.append(
            {
                "id": f"E{i + 1:03d}",
                "claim": f"claim {i}",
                "claim_type": "fact",
                "source_title": f"src {i}",
                "date": "2026-01-01",
                "grade": "S" if i % 2 == 0 else "A",
                "supports": ["end_demand"],
                "confidence": "high",
                "url": f"https://example.com/{i}",
            }
        )
    for j in range(red_flags):
        entries.append(
            {
                "id": f"R{j + 1:03d}",
                "claim": f"red flag {j}",
                "claim_type": "red_flag",
                "source_title": f"rf {j}",
                "date": "2026-01-01",
                "grade": "B",
                "supports": ["risk"],
                "confidence": "medium",
                "url": f"https://example.com/rf{j}",
            }
        )
    return {"target": "算力链", "research_type": research_type, "entries": entries}


def _industry_report(*, tiers_first=True, downgraded=True, red_flag_section=True):
    tier_block = "## 产业链层级排序\n算力芯片 > 存储互连 > 设备。\n"
    company_block = "## 公司排序\n公司A > 公司B。\n"
    body = BASE_SECTIONS
    if tiers_first:
        body += tier_block + company_block
    else:
        body += company_block + tier_block
    if downgraded:
        body += "## 被降级的热门方向\nPCB 方向排序靠后，因为竞争充分。\n"
    if red_flag_section:
        body += "## 红旗清单\n存货增速高于营收。\n"
    return body


# --- evidence_ledger red_flag ---------------------------------------------


def test_red_flag_is_valid_claim_type():
    assert "red_flag" in evidence_ledger.VALID_CLAIM_TYPES


def test_ledger_accepts_red_flag_entry(tmp_path):
    ledger = tmp_path / "evidence.json"
    ledger.write_text(
        json.dumps({"target": "t", "research_type": "industry_chain", "entries": []}),
        encoding="utf-8",
    )
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data["entries"].append(
        {
            "id": "E001",
            "claim": "存货应收增速>营收",
            "claim_type": "red_flag",
            "source_title": "季报",
            "date": "2026-01-01",
            "grade": "S",
            "supports": ["risk"],
            "confidence": "high",
            "url": "https://x",
        }
    )
    assert evidence_ledger.validate_ledger(data) == []


def test_existing_claim_types_still_valid():
    # backward compatibility: the original four claim types remain valid
    for ct in ("fact", "source-backed inference", "third-party summary", "researcher inference"):
        assert ct in evidence_ledger.VALID_CLAIM_TYPES


# --- report_lint industry_chain gates -------------------------------------


def test_industry_chain_passes_full_gate():
    ev = _evidence(25, red_flags=2)
    report = _industry_report()
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert result["status"] != "fail", result["findings"]


def test_industry_chain_fails_when_sources_below_threshold():
    ev = _evidence(20)
    report = _industry_report()
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert result["status"] == "fail"
    assert any("25" in f["message"] or "source" in f["message"].lower() for f in result["findings"])


def test_industry_chain_fails_without_downgraded_hot_direction():
    ev = _evidence(25)
    report = _industry_report(downgraded=False)
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert result["status"] == "fail"
    assert any("降级" in f["message"] or "downgrad" in f["message"].lower() for f in result["findings"])


def test_industry_chain_accepts_english_downgraded_heading():
    ev = _evidence(25)
    report = _industry_report(downgraded=False).replace(
        "## 资料来源",
        "## Downgraded Hot Directions\nPCB ranked low.\n## 资料来源",
    )
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert not any("降级" in f["message"] or "downgrad" in f["message"].lower() for f in result["findings"])


def test_industry_chain_fails_when_company_ranking_before_tiers():
    ev = _evidence(25)
    report = _industry_report(tiers_first=False)
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert result["status"] == "fail"
    assert any("层级" in f["message"] or "tier" in f["message"].lower() or "order" in f["message"].lower() for f in result["findings"])


def test_industry_chain_fails_when_red_flags_undisclosed():
    ev = _evidence(25, red_flags=2)
    report = _industry_report(red_flag_section=False)
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=25)
    assert result["status"] == "fail"
    assert any("红旗" in f["message"] or "red" in f["message"].lower() for f in result["findings"])


def test_min_sources_override_lower_threshold():
    ev = _evidence(10)
    report = _industry_report()
    result = report_lint.lint_report(report, ev, report_type="industry_chain", min_sources=10)
    assert not any("25" in f["message"] for f in result["findings"])


# --- single_stock exemptions ----------------------------------------------


def test_single_stock_not_subject_to_industry_gates():
    ev = _evidence(3, research_type="single_stock")
    # no tier ranking, no downgraded section, few sources — still fine for single_stock
    report = (
        BASE_SECTIONS
        + "## 公司概况\n主营业务。\n"
    )
    # give it 2 S/A entries for the existing rule
    result = report_lint.lint_report(report, ev, report_type="single_stock", min_sources=25)
    # must NOT contain industry-chain-only failures
    assert not any(
        ("25" in f["message"])
        or ("降级" in f["message"])
        or ("层级" in f["message"])
        for f in result["findings"]
    )


def test_report_type_inferred_from_evidence_research_type():
    ev = _evidence(20, research_type="industry_chain")
    report = _industry_report()
    # report_type auto -> inferred industry_chain -> min source gate applies
    result = report_lint.lint_report(report, ev, report_type="auto", min_sources=25)
    assert result["status"] == "fail"


def test_single_stock_inferred_is_exempt():
    ev = _evidence(3, research_type="single_stock")
    report = BASE_SECTIONS + "## 公司概况\n主营。\n"
    result = report_lint.lint_report(report, ev, report_type="auto", min_sources=25)
    assert not any("25" in f["message"] for f in result["findings"])
