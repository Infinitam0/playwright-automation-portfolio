"""Export scored companies to the human review CSV (the send surface).

UTF-8 BOM so Excel renders accented characters correctly. Suppressed companies are
excluded by default — the operator sends from this file, so it must never carry
a do-not-contact row.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..models import CSV_HEADERS, Company

# Excel and LibreOffice evaluate a cell beginning with one of these as a formula,
# and this file is written with a BOM precisely so the operator opens it in Excel.
# Most of the values are scraped from third-party websites, and anyone can list a
# business, so a company "name" of =HYPERLINK(...) or @SUM(...) is attacker-supplied
# input landing in a spreadsheet on the operator's machine. The benign case is just
# as real: every Places phone starts with "+".
_FORMULA_LEAD = ("=", "+", "-", "@")


def _defuse(cell: str) -> str:
    """Force a spreadsheet to read the cell as text, not as a formula.

    A leading apostrophe is the standard Excel/LibreOffice text marker: it is
    consumed on display, so the operator still sees a phone number starting with `+`. It does
    survive into the raw file, so any program reading the export must strip
    exactly this prefix back off.
    """
    return "'" + cell if cell.startswith(_FORMULA_LEAD) else cell


def export_csv(companies: list[Company], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADERS)
        for c in companies:
            writer.writerow([_defuse(v) for v in c.to_row()])
    return len(companies)
