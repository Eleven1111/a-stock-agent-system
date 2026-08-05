"""Hand-assembled minimal PDF bytes for tests.

Deliberately built from raw PDF syntax rather than with ``pypdf`` so a test that
parses the result exercises the real ``PdfReader`` path end to end and cannot pass
because writer and reader share a bug.
"""

from __future__ import annotations


def build_pdf(pages: list[str]) -> bytes:
    """Return a valid single-font PDF, one page per entry in ``pages`` (ASCII text)."""
    if not pages:
        raise ValueError("pages must not be empty")

    objects: list[bytes] = []
    page_count = len(pages)
    # Object numbering: 1=Catalog, 2=Pages, 3=Font, then per page (Page, Contents).
    page_obj_ids = [4 + 2 * index for index in range(page_count)]

    kids = " ".join(f"{obj_id} 0 R" for obj_id in page_obj_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, text in enumerate(pages):
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_obj_ids[index] + 1} 0 R >>".encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)
