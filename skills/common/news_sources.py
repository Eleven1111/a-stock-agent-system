"""Primary-source RSS/HTML polling layer for the news pipeline.

Reads the source catalog from ``config/news_pipeline.json`` (or an injected
path), fetches each source through ``http_client`` (so every request is
recorded by the provider-health ledger), and normalizes results into a flat
list of ``{title, url, published_hint, source_id, source_name, source_rank,
source_type}`` items. Parsing is stdlib-only: ``xml.etree.ElementTree`` for
RSS/Atom, and a small regex-based anchor scan (matching the technique already
used by ``watch_official_policy.py``) for plain HTML listing pages.

Per-source isolation: a single source failing (network error, malformed XML,
unexpected HTML) never aborts the scan of other sources. Each source result
carries an explicit ``status`` so callers can distinguish "fetched, zero
items" from "fetch failed" without guessing.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

try:
    from .http_client import DataSourceError, request_text
except ImportError:  # pragma: no cover - script-style sys.path imports
    from http_client import DataSourceError, request_text  # type: ignore


BJ = timezone(timedelta(hours=8))
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_CATALOG_PATH = os.path.join(_REPO_ROOT, "config", "news_pipeline.json")

UA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

ANCHOR_RE = re.compile(
    r"<a\b[^>]*?href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
DATE_PATTERNS = (
    re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)
GENERIC_NAV_TITLES_DEFAULT = {
    "更多", "加载更多", "公告通知", "新闻发布", "工作动态", "意见征集",
}


def now_bj_iso() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def load_catalog(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CATALOG_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("news pipeline source catalog has no sources")
    return payload


def _normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = TAG_RE.sub(" ", value)
    return SPACE_RE.sub(" ", value).strip()


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def _parse_date_hint(*values: str) -> str | None:
    text = " ".join(value or "" for value in values)
    for pattern in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3)) if len(match.groups()) >= 3 else 1
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


def _entry_date(pub_raw: str, title: str) -> str | None:
    """Date hint from an RSS/Atom entry: RFC822 first, ISO regex fallback."""
    if pub_raw:
        try:
            return datetime(*_parse_rfc822(pub_raw)).date().isoformat()
        except Exception:  # noqa: BLE001 - fall through to regex hint
            hint = _parse_date_hint(pub_raw, title)
            if hint:
                return hint
    return _parse_date_hint(title)


def _parse_rfc822(value: str) -> tuple[int, int, int, int, int, int]:
    from email.utils import parsedate

    parsed = parsedate(value)
    if not parsed:
        raise ValueError(f"unparseable date: {value}")
    return parsed[:6]  # type: ignore[return-value]


def parse_rss(document: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse an RSS 2.0 or Atom feed using only ``xml.etree.ElementTree``."""
    items: list[dict[str, Any]] = []
    try:
        root = ElementTree.fromstring(document)
    except ElementTree.ParseError as exc:
        raise DataSourceError(
            str(source.get("id")), f"RSS parse failed: {exc}", exc, error_type="decode",
        ) from exc

    tag = root.tag.rsplit("}", 1)[-1].lower()
    entries: list[Any]
    if tag == "feed":
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = root.findall("a:entry", ns) or root.findall("entry")
    else:
        entries = root.findall(".//item")

    for entry in entries:
        children: dict[str, Any] = {}
        for child in entry:
            local = child.tag.rsplit("}", 1)[-1].lower()
            children.setdefault(local, child)
        title_el = children.get("title")
        title = _normalize_text(title_el.text or "") if title_el is not None else ""
        link = ""
        link_el = children.get("link")
        if link_el is not None:
            link = (link_el.text or link_el.attrib.get("href") or "").strip()
        if not title or not link:
            continue
        pub_raw = ""
        for date_key in ("pubdate", "published", "updated", "date"):
            el = children.get(date_key)
            if el is not None and el.text:
                pub_raw = el.text.strip()
                break
        published_hint = _entry_date(pub_raw, title)
        items.append({
            "title": title,
            "url": _canonical_url(urljoin(str(source["url"]), link)),
            "published_hint": published_hint,
            "source_id": source["id"],
            "source_name": source["name"],
            "source_rank": source["source_rank"],
            "source_type": source.get("source_type"),
        })
    return items


def parse_html_anchors(
    document: str,
    source: dict[str, Any],
    *,
    generic_titles: set[str] | None = None,
    min_title_len: int = 4,
) -> list[dict[str, Any]]:
    """Extract candidate news links from a plain listing page.

    Same technique as ``policy-intent-decoder``'s watcher: scan anchor tags,
    drop navigation chrome, resolve relative URLs, and harvest a date hint
    from nearby text when the page does not expose structured dates.
    """
    generic = generic_titles or GENERIC_NAV_TITLES_DEFAULT
    base_url = str(source["url"])
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in ANCHOR_RE.finditer(document):
        title = _normalize_text(match.group("label"))
        href = html.unescape(match.group("href") or "").strip()
        if (
            not title
            or len(title) < min_title_len
            or title in generic
            or href.startswith(("javascript:", "#"))
            or "{" in href
            or "}" in href
        ):
            continue
        url = _canonical_url(urljoin(base_url, href))
        if url in seen:
            continue
        seen.add(url)
        nearby = document[max(0, match.start() - 80): match.end() + 120]
        items.append({
            "title": title,
            "url": url,
            "published_hint": _parse_date_hint(title, url, nearby),
            "source_id": source["id"],
            "source_name": source["name"],
            "source_rank": source["source_rank"],
            "source_type": source.get("source_type"),
        })
    return items


def fetch_source(
    source: dict[str, Any],
    *,
    timeout: float = 8.0,
    max_items: int = 30,
) -> dict[str, Any]:
    """Fetch and parse a single source. Never raises: failures are contract data."""
    fetched_at = now_bj_iso()
    kind = str(source.get("kind") or "html")
    try:
        result = request_text(
            source["url"],
            source=source["id"],
            timeout=timeout,
            headers=UA_HEADERS,
        )
        if kind == "rss":
            items = parse_rss(result.data, source)
        else:
            items = parse_html_anchors(result.data, source)
        return {
            "source_id": source["id"],
            "status": "ok",
            "fetched_at": result.fetched_at,
            "items": items[:max_items],
        }
    except DataSourceError as exc:
        return {
            "source_id": source["id"],
            "status": "error",
            "fetched_at": fetched_at,
            "error": exc.to_dict(),
            "items": [],
        }
    except Exception as exc:  # noqa: BLE001 - one bad source must not sink the scan
        return {
            "source_id": source["id"],
            "status": "error",
            "fetched_at": fetched_at,
            "error": {"error_type": type(exc).__name__, "error": str(exc)},
            "items": [],
        }


def fetch_all(
    catalog: dict[str, Any],
    *,
    timeout: float = 8.0,
    max_items_per_source: int = 30,
) -> list[dict[str, Any]]:
    """Fetch every source in the catalog. Returns per-source result contracts."""
    return [
        fetch_source(source, timeout=timeout, max_items=max_items_per_source)
        for source in catalog["sources"]
    ]


if __name__ == "__main__":  # pragma: no cover - manual smoke aid
    COMMON_DIR = os.path.dirname(os.path.abspath(__file__))
    if COMMON_DIR not in sys.path:
        sys.path.insert(0, COMMON_DIR)
    catalog_payload = load_catalog()
    results = fetch_all(catalog_payload)
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
