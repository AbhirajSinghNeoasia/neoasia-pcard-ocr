"""
OCBC P-Card bank-statement parser.

Reads the .xls export, walks every sheet (one per month), and emits a flat
list[BankRow] sorted by transaction date. Handles the format quirks observed
in the real exports:

- Date column is mixed: some rows arrive as Excel datetime serials (xlrd
  ctype=3), others as text in M/D/YYYY (ctype=1). We try both.
- Last-4-digits column arrives as a float for most cards (e.g. 9804.0) and as
  a zero-padded string for cards starting with 0 (e.g. '0594', '0711').
  Normalised to a 4-char zero-padded string in every case.
- Transaction IDs are floats from xlrd; cast to integer-string to avoid the
  trailing ".0".
- "FACEBK" substring (case-insensitive) flags Meta/Facebook ad transactions;
  the reference (FB) code is extracted via FB_CODE_REGEX.
- Negative SGD amounts are kept (they are legitimate refunds).
- Empty/blank rows are skipped silently.

OCBC date-format quirk (CRITICAL — verified against the Pcard - Final output
reviewed file):
  The OCBC export writes dates in Singapore D/M/YYYY format. Excel ingests
  these with US M/D/YYYY locale: when the day numeral is <=12, Excel
  silently treats it as a month and stores a corrupted serial. When the day
  numeral is >=13, M/D parsing fails and Excel keeps the value as text.
  Result, in this export:
    - SERIAL date cells need month <-> day swapped (e.g. xlrd reads
      2026-01-03 but the row is actually 2026-03-01).
    - TEXT date cells are already correct in M/D/YYYY ("3/13/2026" -> Mar 13).
  Cross-checked against the team's reviewed output:
    - Source row 1 of Mar26 (Grab #9XRJTDGGX9SFAV, xlrd Jan 3) -> Mar 1 in
      reviewed.
    - Meta TTTW5EZGT2 (text "2/14/2026") -> Feb 14 in reviewed.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import IO, Iterable, Optional, Union

import xlrd

from config import (
    FB_CODE_REGEX,
    META_NARRATIVE_KEYWORD,
    OCBC_COLUMNS,
    TEXT_DATE_FORMATS,
    lookup_cardholder,
)
from models import BankRow, TransactionType


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

FileLike = Union[str, bytes, IO[bytes]]


def parse_ocbc_statement(file: FileLike) -> list[BankRow]:
    """Parse an OCBC P-Card .xls statement into a list[BankRow].

    Accepts a path, a bytes blob, or a file-like object (so it works with both
    local files and Streamlit UploadedFile objects). Walks every sheet and
    returns rows sorted by date ascending; row_index is unique across sheets.
    """
    wb = _open_workbook(file)

    rows: list[BankRow] = []
    running_index = 0

    for sheet_name in wb.sheet_names():
        sh = wb.sheet_by_name(sheet_name)
        if sh.nrows < 2:
            continue  # No data beyond the header

        for r in range(1, sh.nrows):
            parsed = _parse_row(sh, r, sheet_name, wb.datemode, running_index)
            if parsed is None:
                continue
            rows.append(parsed)
            running_index += 1

    rows.sort(key=lambda b: (b.transaction_date, b.row_index))
    return rows


def extract_fb_code(narrative: str) -> Optional[str]:
    """Extract a Facebook reference code (FB Code) from a bank narrative.

    Returns None if no code is found. Always uppercased on success.
    """
    if not narrative:
        return None
    m = FB_CODE_REGEX.search(narrative)
    if not m:
        return None
    return m.group(1).upper()


def is_meta_narrative(narrative: str) -> bool:
    """True if the bank narrative is a Meta/Facebook ad transaction."""
    return META_NARRATIVE_KEYWORD.lower() in (narrative or "").lower()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _open_workbook(file: FileLike) -> xlrd.book.Book:
    """Open an xlrd workbook from path, bytes, or file-like input."""
    if isinstance(file, str):
        return xlrd.open_workbook(file)
    if isinstance(file, (bytes, bytearray)):
        return xlrd.open_workbook(file_contents=bytes(file))
    # File-like (e.g. Streamlit UploadedFile)
    data = file.read()
    if hasattr(file, "seek"):
        try:
            file.seek(0)
        except Exception:
            pass
    return xlrd.open_workbook(file_contents=data)


def _parse_row(
    sh: xlrd.sheet.Sheet,
    row_idx: int,
    sheet_name: str,
    datemode: int,
    running_index: int,
) -> Optional[BankRow]:
    """Parse a single data row. Returns None if the row is unusable."""
    cols = OCBC_COLUMNS

    # Skip totally empty rows (xlrd reports ctype=0 for empty cells)
    if all(sh.cell(row_idx, c).ctype == 0 for c in range(sh.ncols)):
        return None

    # --- Transaction ID -------------------------------------------------
    tx_id = _to_id_string(sh.cell(row_idx, cols["id"]).value)
    if not tx_id:
        # No ID is a hard signal that the row isn't a real transaction
        return None

    # --- Date ----------------------------------------------------------
    date_cell = sh.cell(row_idx, cols["transaction_date"])
    tx_date = _parse_date_cell(date_cell, datemode)
    if tx_date is None:
        return None

    # --- Narrative -----------------------------------------------------
    narrative = str(sh.cell(row_idx, cols["narrative"]).value or "").strip()

    # --- Last 4 digits (normalise to 4-char zero-padded) ---------------
    last4 = _normalise_last4(sh.cell(row_idx, cols["last_4_digits"]).value)
    cardholder = lookup_cardholder(last4)

    # --- Amounts -------------------------------------------------------
    merchant_amount = _to_float(sh.cell(row_idx, cols["merchant_currency_amount"]).value)
    merchant_currency = str(sh.cell(row_idx, cols["merchant_currency"]).value or "").strip()
    sgd_amount = _to_float(sh.cell(row_idx, cols["transaction_amount"]).value)

    # --- Meta detection + FB code -------------------------------------
    if is_meta_narrative(narrative):
        tx_type = TransactionType.META
        fb_code = extract_fb_code(narrative)
    else:
        tx_type = TransactionType.SIMPLE
        fb_code = None

    return BankRow(
        row_index=running_index,
        sheet_name=sheet_name,
        transaction_id=tx_id,
        transaction_date=tx_date,
        narrative=narrative,
        last_4_digits=last4,
        cardholder=cardholder,
        merchant_amount=merchant_amount,
        merchant_currency=merchant_currency,
        sgd_amount=sgd_amount,
        transaction_type=tx_type,
        fb_code=fb_code,
        is_refund=sgd_amount < 0,
    )


def _to_id_string(value: object) -> str:
    """Convert an OCBC ID cell value to a clean string (no trailing '.0')."""
    if value is None or value == "":
        return ""
    if isinstance(value, float):
        # OCBC IDs are integers represented as floats by xlrd
        if value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def _normalise_last4(value: object) -> str:
    """Coerce the Last-4-Digits cell to a 4-character zero-padded string."""
    if value is None or value == "":
        return "0000"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value)).zfill(4)
        return str(value).zfill(4)
    if isinstance(value, int):
        return str(value).zfill(4)
    s = str(value).strip()
    # Strip any decimal-tail leftovers like "9804.0"
    if s.endswith(".0"):
        s = s[:-2]
    # Pad to 4 chars
    if s.isdigit():
        return s.zfill(4)
    return s


def _to_float(value: object) -> float:
    """Coerce a cell value to float; returns 0.0 for empty/non-numeric."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_date_cell(cell: xlrd.sheet.Cell, datemode: int) -> Optional[date]:
    """Two-pass date parser: handles datetime serials AND text dates.

    SERIAL cells additionally get the OCBC D/M-vs-M/D swap fix applied
    (see module docstring).
    """
    # Pass 1: native Excel date cell (ctype=3 / XL_CELL_DATE)
    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            dt = xlrd.xldate.xldate_as_datetime(cell.value, datemode)
            return _ocbc_swap_md(dt.date())
        except (ValueError, xlrd.xldate.XLDateError):
            pass  # Fall through to text parsing

    # Pass 2: numeric serial stored as a number cell
    if cell.ctype == xlrd.XL_CELL_NUMBER and isinstance(cell.value, (int, float)):
        try:
            dt = xlrd.xldate.xldate_as_datetime(cell.value, datemode)
            return _ocbc_swap_md(dt.date())
        except (ValueError, xlrd.xldate.XLDateError):
            pass

    # Pass 3: text cell (ctype=1) — try each known format. Text dates are
    # already in M/D/YYYY (verified) so no swap is applied.
    text = str(cell.value or "").strip()
    if not text:
        return None
    for fmt in TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    # Pass 4: pandas as a last-resort fuzzy parser (handles ISO with time, etc.)
    try:
        import pandas as pd
        ts = pd.to_datetime(text, errors="raise", dayfirst=False)
        return ts.date()
    except Exception:
        return None


def _ocbc_swap_md(d: date) -> date:
    """Swap month and day for OCBC serial date cells.

    OCBC writes dates in D/M/YYYY (Singapore) but Excel ingests them with
    M/D/YYYY locale, swapping the two whenever the day numeral is <=12. The
    fix is to swap them back. If the swapped result would be an invalid
    calendar date (e.g. day=30 -> month=30), the original is returned and the
    swap is skipped — that case has not been observed in real exports but the
    guard keeps the parser resilient.
    """
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return d


# ---------------------------------------------------------------------------
# Convenience helpers for downstream modules
# ---------------------------------------------------------------------------

def split_by_type(rows: Iterable[BankRow]) -> tuple[list[BankRow], list[BankRow]]:
    """Split a flat list[BankRow] into (simple_rows, meta_rows)."""
    simple, meta = [], []
    for r in rows:
        if r.transaction_type == TransactionType.META.value or r.transaction_type == TransactionType.META:
            meta.append(r)
        else:
            simple.append(r)
    return simple, meta


def date_range(rows: Iterable[BankRow]) -> Optional[tuple[date, date]]:
    """Return (min_date, max_date) for a list of rows, or None if empty."""
    rows = list(rows)
    if not rows:
        return None
    dates = [r.transaction_date for r in rows]
    return (min(dates), max(dates))
