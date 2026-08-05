import urllib.parse

import pytest

import announcement_risk
import cninfo_client
from http_client import HttpResult
from pdf_fixture import build_pdf


PDF_URL = "https://static.cninfo.com.cn/finalpage/2026-07-31/1225448830.PDF"


def _result(payload):
    return HttpResult(payload, "2026-06-12T06:00:00+00:00", 1)


def _stub_pdf_download(monkeypatch, payload, seen=None):
    """Stub only the network hop; PDF parsing stays on the real pypdf path."""

    def fake_request_bytes(request, **kwargs):
        if seen is not None:
            seen.append(request.full_url)
        return _result(payload)

    monkeypatch.setattr(announcement_risk, "request_bytes", fake_request_bytes)


def test_fetch_announcements_uses_resolved_org_id(monkeypatch):
    monkeypatch.setattr(
        cninfo_client,
        "lookup_org_id",
        lambda code, timeout=8: "9900003427",
    )
    _stub_pdf_download(monkeypatch, build_pdf(["ABNORMAL FLUCTUATION BODY TEXT"]))

    def fake_request_json(request, **kwargs):
        assert kwargs == {
            "source": "cninfo",
            "timeout": 8,
            "encoding": "utf-8",
        }
        assert request.full_url == cninfo_client.QUERY_URL
        assert request.get_method() == "POST"
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        assert form["stock"] == ["002156,9900003427"]
        return _result({
            "announcements": [{
                "announcementTitle": "关于股票交易异常波动的公告",
                "announcementTime": 1781136000000,
                "adjunctUrl": "finalpage/2026-07-31/1225448830.PDF",
            }]
        })

    monkeypatch.setattr(cninfo_client, "request_json", fake_request_json)

    rows = announcement_risk.fetch_announcements("002156")

    assert rows[0]["title"] == "关于股票交易异常波动的公告"
    # The risk-word row must carry real body text pulled through PdfReader, not "".
    assert rows[0]["text_status"] == "ok"
    assert "ABNORMAL FLUCTUATION BODY TEXT" in rows[0]["text"]


def test_pdf_backend_is_installed():
    """Regression guard for the extra-only pypdf bug: a deploy without pypdf
    silently produced empty announcement bodies. If pypdf ever drops out of the
    install set again, this fails instead of degrading quietly."""
    assert announcement_risk.pdf_text_available() is True


def test_extract_pdf_text_parses_a_real_pdf(monkeypatch):
    _stub_pdf_download(monkeypatch, build_pdf(["RISK ALERT BODY", "SECOND PAGE BODY"]))

    text, status = announcement_risk.extract_pdf_text_with_status(PDF_URL)

    assert status == "ok"
    assert "RISK ALERT BODY" in text
    assert "SECOND PAGE BODY" in text
    assert announcement_risk.extract_pdf_text(PDF_URL) == text


def test_extract_pdf_text_honours_max_pages(monkeypatch):
    _stub_pdf_download(monkeypatch, build_pdf(["PAGE ONE", "PAGE TWO", "PAGE THREE"]))

    text, status = announcement_risk.extract_pdf_text_with_status(PDF_URL, max_pages=2)

    assert status == "ok"
    assert "PAGE ONE" in text and "PAGE TWO" in text
    assert "PAGE THREE" not in text


def test_extract_pdf_text_reports_missing_backend(monkeypatch):
    seen: list[str] = []
    _stub_pdf_download(monkeypatch, b"", seen=seen)
    monkeypatch.setattr(announcement_risk, "pdf_text_available", lambda: False)

    text, status = announcement_risk.extract_pdf_text_with_status(PDF_URL)

    assert (text, status) == ("", "pdf_backend_missing")
    assert seen == []  # no pointless download when the backend cannot parse it


def test_extract_pdf_text_reports_unparsable_payload(monkeypatch):
    _stub_pdf_download(monkeypatch, b"not a pdf at all")

    assert announcement_risk.extract_pdf_text_with_status(PDF_URL) == ("", "parse_failed")


def test_extract_pdf_text_reports_download_failure(monkeypatch):
    def boom(request, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(announcement_risk, "request_bytes", boom)

    assert announcement_risk.extract_pdf_text_with_status(PDF_URL) == ("", "fetch_failed")


@pytest.mark.parametrize("url", ["", None])
def test_extract_pdf_text_without_url(url):
    assert announcement_risk.extract_pdf_text_with_status(url) == ("", "no_url")
