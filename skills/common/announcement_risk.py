"""Best-effort CNINFO announcement title scan for recommendation gating.

Transport, orgId resolution and title cleaning live in
:mod:`cninfo_client`; this module only adds the risk-word gating policy
and the PDF text enrichment used by recommendation review.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Iterable

try:
    from .http_client import DataSourceError, build_request, request_bytes
    from .cninfo_client import (
        SOURCE_STOCK,
        clean_title,
        millis_to_date,
        pdf_url,
        query_announcements,
    )
except ImportError:  # pragma: no cover - exercised via PYTHONPATH=skills/common
    from http_client import DataSourceError, build_request, request_bytes
    from cninfo_client import (
        SOURCE_STOCK,
        clean_title,
        millis_to_date,
        pdf_url,
        query_announcements,
    )


LOGGER = logging.getLogger(__name__)

RISK_TITLE_TERMS = ("澄清", "异常波动", "风险提示", "问询", "监管", "更正")
PDF_BACKEND_MISSING_HINT = (
    "pypdf 未安装，公告正文深读不可用（announcement-radar 第四步将只有标题）；"
    "修复：pip install -c constraints.txt -e ."
)

# http_client wraps transport failures in DataSourceError; urllib/socket errors
# (URLError, timeout) surface as OSError.
_DOWNLOAD_ERRORS = (DataSourceError, OSError)


@lru_cache(maxsize=1)
def pdf_text_available() -> bool:
    """Whether the PDF text backend is importable in this environment.

    ``pypdf`` is a hard dependency (see ``pyproject.toml``); this probe exists so a
    broken deployment surfaces as an explicit ``pdf_backend_missing`` status instead
    of an empty announcement body that looks like "the PDF had no text".
    """
    try:
        import pypdf  # noqa: F401
    except ImportError:
        LOGGER.warning(PDF_BACKEND_MISSING_HINT)
        return False
    return True


def extract_pdf_text_with_status(
    url: str,
    timeout: int = 8,
    max_pages: int = 5,
) -> tuple[str, str]:
    """Return ``(text, status)`` for a CNINFO announcement PDF.

    ``status`` is one of ``no_url`` / ``pdf_backend_missing`` / ``fetch_failed`` /
    ``parse_failed`` / ``empty`` / ``ok`` so callers can tell "no text" apart from
    "never tried".
    """
    if not url:
        return "", "no_url"
    if not pdf_text_available():
        return "", "pdf_backend_missing"
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    # Corrupt PDFs escape pypdf's own hierarchy often enough (truncated xref tables,
    # bad object types) that the structural builtins have to be caught too.
    _PDF_PARSE_ERRORS = (PyPdfError, ValueError, KeyError, TypeError, IndexError, RecursionError)

    request = build_request(
        url,
        headers={"User-Agent": "Mozilla/5.0 A-Stock-Agent"},
    )
    try:
        payload = request_bytes(
            request,
            source=SOURCE_STOCK,
            timeout=timeout,
        ).data[:6 * 1024 * 1024]
    except _DOWNLOAD_ERRORS as exc:  # network/HTTP failures stay best-effort
        LOGGER.warning("公告 PDF 下载失败 url=%s err=%s", url, exc)
        return "", "fetch_failed"
    try:
        reader = PdfReader(io.BytesIO(payload))
        text = "\n".join(
            (page.extract_text() or "")
            for page in reader.pages[:max_pages]
        )[:20000]
    except _PDF_PARSE_ERRORS as exc:  # malformed/encrypted/scanned PDFs
        LOGGER.warning("公告 PDF 解析失败 url=%s err=%s", url, exc)
        return "", "parse_failed"
    return text, "ok" if text.strip() else "empty"


def extract_pdf_text(url: str, timeout: int = 8, max_pages: int = 5) -> str:
    """Best-effort announcement body text; empty string when unavailable.

    Use :func:`extract_pdf_text_with_status` when the caller must distinguish a
    genuinely empty PDF from a missing backend or a failed download.
    """
    return extract_pdf_text_with_status(url, timeout=timeout, max_pages=max_pages)[0]


# Backwards-compatible alias: existing tests and callers monkeypatch this name.
_extract_pdf_text = extract_pdf_text


def fetch_announcements(stock_code: str, page_size: int = 30, timeout: int = 8) -> list[dict[str, Any]]:
    items = query_announcements(stock_code, page_size=page_size, timeout=timeout)
    result = []
    enriched = 0
    for item in items:
        title = clean_title(item.get("announcementTitle"))
        url = pdf_url(item.get("adjunctUrl"))
        text = ""
        text_status = "not_attempted"
        if enriched < 1 and any(term in title for term in RISK_TITLE_TERMS):
            text, text_status = extract_pdf_text_with_status(url, timeout=min(timeout, 4))
            enriched += 1
        result.append({
            "title": title,
            "date": millis_to_date(item.get("announcementTime")),
            "source": "CNINFO",
            "url": url,
            "text": text,
            "text_status": text_status,
        })
    return result


def scan_many(codes: Iterable[str], timeout: int = 8) -> dict[str, list[dict[str, Any]] | None]:
    unique = list(dict.fromkeys(str(code)[-6:].zfill(6) for code in codes if code))

    def _fetch(code: str) -> tuple[str, list[dict[str, Any]] | None]:
        try:
            return code, fetch_announcements(code, timeout=timeout)
        except Exception:
            return code, None

    with ThreadPoolExecutor(max_workers=min(5, len(unique) or 1)) as pool:
        return dict(pool.map(_fetch, unique))
