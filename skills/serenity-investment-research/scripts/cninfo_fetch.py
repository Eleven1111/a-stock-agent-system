#!/usr/bin/env python3
"""Best-effort CNINFO announcement fetcher for A-share research.

This script uses CNINFO's public announcement endpoint. The endpoint may change
or rate-limit; when it fails, fall back to browser/web search and still record
sources in the evidence ledger.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
PDF_BASE = "https://static.cninfo.com.cn/"


def post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 serenity-investment-research",
            "Referer": "https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def safe_name(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    return re.sub(r"\s+", "_", name).strip("_")[:120]


def infer_org_id(stock_code: str) -> str:
    prefix = "gssz" if stock_code.startswith(("0", "3")) else "gssh"
    digits = f"{int(stock_code):07d}" if stock_code.isdigit() else stock_code
    return prefix + digits


def download(url: str, out: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 serenity-investment-research"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        out.write_bytes(resp.read())


def millis_to_date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-code", required=True, help="e.g. 002050")
    parser.add_argument("--org-id", help="CNINFO orgId, e.g. gssz0002050. Inferred when omitted.")
    parser.add_argument("--query", default="", help="announcement title keyword")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--page-size", type=int, default=10)
    parser.add_argument("--download", action="store_true", help="download PDFs")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    org_id = args.org_id or infer_org_id(args.stock_code)
    stock_value = f"{args.stock_code},{org_id}"
    payload = {
        "stock": stock_value,
        "searchkey": args.query,
        "plate": "szse" if args.stock_code.startswith(("0", "3")) else "sse",
        "category": "",
        "trade": "",
        "column": "szse" if args.stock_code.startswith(("0", "3")) else "sse",
        "columnTitle": "历史公告查询",
        "pageNum": "1",
        "pageSize": str(args.page_size),
        "tabName": "fulltext",
        "sortName": "",
        "sortType": "",
        "limit": "",
        "seDate": "",
        "isHLtitle": "true",
    }
    data = post_form(QUERY_URL, payload)
    announcements = data.get("announcements") or []
    manifest = []
    for item in announcements:
        adjunct = item.get("adjunctUrl") or ""
        pdf_url = urllib.parse.urljoin(PDF_BASE, adjunct)
        title = re.sub(r"<[^>]+>", "", item.get("announcementTitle", "announcement"))
        row = {
            "title": title,
            "announcementTime": item.get("announcementTime"),
            "date": millis_to_date(item.get("announcementTime")),
            "url": pdf_url,
            "downloaded": None,
        }
        if args.download and adjunct:
            filename = safe_name(f"{args.stock_code}_{title}.pdf")
            path = out_dir / filename
            download(pdf_url, path)
            row["downloaded"] = str(path)
        manifest.append(row)

    manifest_path = out_dir / "cninfo_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"found {len(manifest)} announcements -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
