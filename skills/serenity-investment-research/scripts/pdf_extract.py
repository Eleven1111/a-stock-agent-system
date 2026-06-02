#!/usr/bin/env python3
"""Extract text from a PDF for research workflows."""

from __future__ import annotations

import argparse
from pathlib import Path


def extract_with_pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("pypdf is not installed") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        chunks.append(f"\n---PAGE {i}---\n{text}")
    return "\n".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="input PDF path")
    parser.add_argument("--out", help="output text path; default: <pdf>.txt")
    parser.add_argument("--max-pages", type=int, default=0, help="reserved for compatibility")
    args = parser.parse_args()

    pdf = Path(args.pdf)
    if not pdf.exists():
        raise SystemExit(f"missing PDF: {pdf}")
    out = Path(args.out) if args.out else pdf.with_suffix(".txt")
    text = extract_with_pypdf(pdf)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"extracted {len(text)} chars -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
