"""Cover page check: the title, page title and version that the migration sheet (metadata/*.xlsx) gives for a
manual must be printed on its cover, in prod and in stage.

The sheet row is found from the two PDF file names (the same matching as the metadata check). The cover is
the first page of each PDF. A field passes when its expected text is on that page, whitespace and line breaks
ignored.
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

# (field, the sheet's expected text for it)
FIELDS = [("Document Title", "exp_doc"), ("Page Title", "exp_page"), ("Version", "version")]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _cover_text(path: str | Path | None) -> str | None:
    if not path or not Path(path).exists():
        return None
    with pymupdf.open(str(path)) as doc:
        return doc[0].get_text() if doc.page_count else ""


def check(result: dict, rows: list[dict] | None = None) -> dict | None:
    """The cover check of the report: one entry per field with the expected text and what the prod and stage
    covers print. None when the sheet is not there or no sheet row belongs to the two PDFs."""
    from . import metadata
    if rows is None:
        sheet = metadata.default_sheet()
        if sheet is None:
            return None
        rows = metadata.load_sheet(sheet)
    meta = result.get("meta") or {}
    prod, stage = (meta.get("baseline") or {}).get("path"), (meta.get("candidate") or {}).get("path")
    row = None
    for path in (stage, prod):
        if path:
            stem = Path(path).stem
            row = metadata.match("", stem, rows, stem)
            if row:
                break
    if row is None:
        return {"row": None, "fields": []}
    covers = {"prod": _cover_text(prod), "stage": _cover_text(stage)}
    fields = []
    for name, key in FIELDS:
        expected = row.get(key) or ""
        if not expected:
            continue
        cells = {}
        for side, text in covers.items():
            if text is None:
                cells[side] = "no file"
            else:
                cells[side] = "ok" if _norm(expected) in _norm(text) else "missing"
        fields.append({"field": name, "expected": expected, **cells,
                       "status": "pass" if cells["stage"] == "ok" else "fail"})  # the stage cover is what is validated
    return {"row": row.get("row"), "model": row.get("model"), "fields": fields}
