"""Write rows of records as CSV, Excel or JSON (for `ghost-api fetch` and the dashboard).

Rows come from `runtime.flatten`, so nested fields are already dotted columns
("venue.name"). Columns keep the order they first appear in.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Literal

from ghost_api.runtime import flatten

Format = Literal["csv", "xlsx", "json"]
FORMATS: tuple[Format, ...] = ("csv", "xlsx", "json")
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
}
EXCEL_MAX_CELL = 32_767  # Excel refuses longer cell text


def columns(rows: list[dict[str, Any]]) -> list[str]:
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key, None)
    return list(seen)


def to_bytes(records: list[Any], fmt: Format) -> bytes:
    """The records as a file's contents. JSON keeps the original nesting."""
    if fmt == "json":
        return json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
    rows = [flatten(r) for r in records]
    cols = columns(rows)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({k: _safe_text(v) for k, v in row.items()} for row in rows)
        return buffer.getvalue().encode("utf-8-sig")  # the BOM makes Excel read UTF-8
    return _xlsx(rows, cols)


def write(records: list[Any], path: Path, fmt: Format) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_bytes(records, fmt))
    return path


def _xlsx(rows: list[dict[str, Any]], cols: list[str]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "data"
    sheet.append(cols)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in rows:
        sheet.append([_cell(row.get(c)) for c in cols])
    sheet.freeze_panes = "A2"
    for i, col in enumerate(cols, start=1):
        width = max([len(col)] + [len(str(r.get(col) or "")) for r in rows[:200]])
        sheet.column_dimensions[sheet.cell(1, i).column_letter].width = min(max(width, 8), 60)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _cell(value: Any) -> Any:
    safe = _safe_text(value)
    return safe[:EXCEL_MAX_CELL] if isinstance(safe, str) else safe


def _safe_text(value: Any) -> Any:
    """Site data must never become a spreadsheet formula ("=HYPERLINK(...)")."""
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
