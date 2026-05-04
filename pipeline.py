"""
End-to-end orchestration: OCR -> match -> split -> assemble.

Two entry points:

  process_all(...)
      Full pipeline. Hits the network: calls Claude per uploaded receipt and
      per uploaded Meta invoice, then runs assemble_outputs.

  assemble_outputs(...)
      Post-OCR assembly only. Pure Python, no network. Used by tests and by
      the manual-match re-apply path in the UI (which has the OCR results
      already and only needs to re-run the matching/splitting).

Helpers:
  matched_to_split_row(...)         simple-row MatchedRow -> SplitRow
  derive_period_string(...)         "Feb-Mar-2026" / "Feb-2026" / cross-year
  parse_brand_override_csv(...)     CSV upload -> override dict
  apply_manual_overrides(...)       inject user-picked OCR pairings into the
                                    auto-match result before split/assemble

Design constraint: NO `import streamlit`. The Streamlit app calls into here
with plain file-like objects + callbacks; the pipeline stays test-friendly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

import pandas as pd

from bank_parser import split_by_type
from description_builder import build_simple_description
from matching_engine import (
    build_gl_suggestion,
    match_meta_transactions,
    match_simple_transactions,
)
from meta_splitter import split_meta_transaction
from models import (
    BankRow,
    MatchedRow,
    MatchStatus,
    MetaInvoiceOCR,
    OcrTransaction,
    RowType,
    SplitRow,
)
from ocr_engine import ocr_meta_invoice, ocr_simple_receipt


# Progress callback signature: (message, current, total)
ProgressCB = Callable[[str, int, int], None]


# ---------------------------------------------------------------------------
# Result dataclass — used by both assemble_outputs and process_all
# ---------------------------------------------------------------------------

@dataclass
class AssemblyResult:
    matched_simple:        list[MatchedRow]         = field(default_factory=list)
    simple_split_rows:     list[SplitRow]           = field(default_factory=list)
    meta_split_rows:       list[SplitRow]           = field(default_factory=list)
    all_split_rows:        list[SplitRow]           = field(default_factory=list)
    orphan_meta_codes:     list[str]                = field(default_factory=list)
    orphan_invoice_codes:  list[str]                = field(default_factory=list)
    n_matched_simple:      int                      = 0
    n_unmatched_simple:    int                      = 0
    n_meta_splits:         int                      = 0
    total_sgd:             float                    = 0.0
    ocr_errors:            list[tuple[str, str]]    = field(default_factory=list)
    # Carried for the manual-match re-apply path: the UI needs the OCR pool
    # to offer leftover candidates and to re-assemble after the user picks.
    ocr_simple:            list[OcrTransaction]     = field(default_factory=list)
    ocr_meta:              list[MetaInvoiceOCR]     = field(default_factory=list)


# ---------------------------------------------------------------------------
# Full pipeline (with OCR)
# ---------------------------------------------------------------------------


def process_all(
    bank_rows: list[BankRow],
    simple_files: list[Any],
    meta_files: list[Any],
    *,
    brand_override: Optional[dict[str, dict[str, str]]] = None,
    manual_matches: Optional[dict[int, int]] = None,
    progress_cb: Optional[ProgressCB] = None,
) -> AssemblyResult:
    """Full pipeline: OCR every uploaded file, then assemble outputs.

    Failures during OCR are captured per-file in `result.ocr_errors` rather
    than aborting the whole pipeline — finance can still post the rows that
    DID extract while the operator chases the broken receipts.
    """
    cb: ProgressCB = progress_cb or (lambda *a, **k: None)

    ocr_simple: list[OcrTransaction] = []
    ocr_errors: list[tuple[str, str]] = []

    for i, f in enumerate(simple_files, start=1):
        name = getattr(f, "name", f"<file {i}>")
        cb(f"OCR receipt: {name}", i, len(simple_files))
        try:
            results = ocr_simple_receipt(f, source_name=name)
            ocr_simple.extend(results)
        except Exception as exc:                                        # noqa: BLE001
            ocr_errors.append((name, f"{type(exc).__name__}: {exc}"))

    ocr_meta: list[MetaInvoiceOCR] = []
    for i, f in enumerate(meta_files, start=1):
        name = getattr(f, "name", f"<file {i}>")
        cb(f"OCR Meta invoice: {name}", i, len(meta_files))
        try:
            ocr_meta.append(ocr_meta_invoice(f, source_name=name))
        except Exception as exc:                                        # noqa: BLE001
            ocr_errors.append((name, f"{type(exc).__name__}: {exc}"))

    cb("Assembling outputs", 1, 1)
    result = assemble_outputs(
        bank_rows,
        ocr_simple,
        ocr_meta,
        brand_override=brand_override,
        manual_matches=manual_matches,
    )
    result.ocr_errors = ocr_errors
    return result


# ---------------------------------------------------------------------------
# Pure assembly (no network)
# ---------------------------------------------------------------------------


def assemble_outputs(
    bank_rows: list[BankRow],
    ocr_simple: list[OcrTransaction],
    ocr_meta: list[MetaInvoiceOCR],
    *,
    brand_override: Optional[dict[str, dict[str, str]]] = None,
    manual_matches: Optional[dict[int, int]] = None,
) -> AssemblyResult:
    """Build the full SplitRow list from already-OCR'd inputs.

    `manual_matches` is an optional {simple_bank_idx: ocr_idx} dict — if
    present, those pairings override the auto-matcher and rebuild the
    affected simple SplitRows. Indices are into the SIMPLE-only bank slice
    (the order returned by split_by_type), not into the original bank_rows.
    """
    simple_banks, meta_banks = split_by_type(bank_rows)

    # ----- Simple matching -------------------------------------------
    matched_simple = match_simple_transactions(simple_banks, ocr_simple)
    if manual_matches:
        matched_simple = apply_manual_overrides(matched_simple, ocr_simple, manual_matches)

    simple_split_rows = [matched_to_split_row(m, line_number=0) for m in matched_simple]

    # ----- Meta join + split -----------------------------------------
    meta_join = match_meta_transactions(bank_rows, ocr_meta)
    meta_split_rows: list[SplitRow] = []

    for bank_row in meta_banks:
        invoice = meta_join.get(bank_row.fb_code or "")
        if invoice is not None:
            meta_split_rows.extend(
                split_meta_transaction(
                    bank_row, invoice,
                    start_line=1,
                    brand_override=brand_override,
                )
            )
        else:
            # Orphan Meta bank row — emit a single placeholder so finance
            # sees it in the export instead of it silently disappearing.
            meta_split_rows.append(_orphan_meta_placeholder(bank_row))

    # ----- Combine, sort, renumber -----------------------------------
    all_rows = _sort_and_renumber(simple_split_rows + meta_split_rows)

    # ----- Stats -----------------------------------------------------
    bank_codes = {b.fb_code for b in meta_banks if b.fb_code}
    orphan_meta = sorted([
        b.fb_code for b in meta_banks
        if b.fb_code and b.fb_code not in meta_join
    ])
    orphan_inv = sorted([
        o.reference_number for o in ocr_meta
        if o.reference_number and o.reference_number not in bank_codes
    ])

    n_matched = sum(1 for m in matched_simple if m.ocr is not None)
    n_unmatched = len(matched_simple) - n_matched

    return AssemblyResult(
        matched_simple=matched_simple,
        simple_split_rows=simple_split_rows,
        meta_split_rows=meta_split_rows,
        all_split_rows=all_rows,
        orphan_meta_codes=orphan_meta,
        orphan_invoice_codes=orphan_inv,
        n_matched_simple=n_matched,
        n_unmatched_simple=n_unmatched,
        n_meta_splits=len(meta_split_rows),
        total_sgd=float(sum(r.sgd_amount for r in all_rows)),
        ocr_simple=list(ocr_simple),
        ocr_meta=list(ocr_meta),
    )


# ---------------------------------------------------------------------------
# Conversion: MatchedRow -> SplitRow
# ---------------------------------------------------------------------------


def matched_to_split_row(matched: MatchedRow, line_number: int) -> SplitRow:
    """Project one simple-transaction MatchedRow onto a SplitRow."""
    bank = matched.bank
    ocr = matched.ocr
    gl = build_gl_suggestion(bank, ocr)
    desc = build_simple_description(bank, ocr)
    return SplitRow(
        description=desc,
        gl_account=gl[0] if gl else None,
        gl_account_name=gl[1] if gl else None,
        line_number=line_number,
        brand="0_DIM2",
        country="",
        division="0_DIM4",
        team="0_DIM5",
        tax_code="",
        sgd_amount=bank.sgd_amount,
        bank_date=bank.transaction_date,
        bank_narrative=bank.narrative,
        card_last4=bank.last_4_digits,
        cardholder=bank.cardholder,
        merchant_currency=bank.merchant_currency,
        merchant_amount=bank.merchant_amount,
        bank_sgd=bank.sgd_amount,
        match_status=MatchStatus.MATCHED.value if ocr else MatchStatus.UNMATCHED.value,
        row_type=RowType.SIMPLE.value,
    )


# ---------------------------------------------------------------------------
# Manual-match override
# ---------------------------------------------------------------------------


def apply_manual_overrides(
    matched: list[MatchedRow],
    ocr_simple: list[OcrTransaction],
    manual_matches: dict[int, int],
) -> list[MatchedRow]:
    """Return a new MatchedRow list with user-selected pairings applied."""
    result = list(matched)
    for bidx, oidx in manual_matches.items():
        if not (0 <= bidx < len(result)):
            continue
        if not (0 <= oidx < len(ocr_simple)):
            continue
        old = result[bidx]
        result[bidx] = MatchedRow(
            bank=old.bank,
            ocr=ocr_simple[oidx],
            match_confidence="manual",
            match_reason="Manually assigned by user",
        )
    return result


# ---------------------------------------------------------------------------
# Period string + brand-override CSV parsing
# ---------------------------------------------------------------------------


def derive_period_string(bank_rows: list[BankRow]) -> str:
    """Build a human-readable filename token for the statement period.

      Single month         -> "Feb-2026"
      Multi-month, 1 year  -> "Feb-Mar-2026"
      Cross-year           -> "Dec2025-Jan2026"
    """
    if not bank_rows:
        return "Unknown"
    dates = [b.transaction_date for b in bank_rows]
    dmin, dmax = min(dates), max(dates)
    if dmin.year != dmax.year:
        return f"{dmin.strftime('%b%Y')}-{dmax.strftime('%b%Y')}"
    if dmin.month == dmax.month:
        return f"{dmin.strftime('%b')}-{dmin.year}"
    return f"{dmin.strftime('%b')}-{dmax.strftime('%b')}-{dmax.year}"


def parse_brand_override_csv(file: Any) -> dict[str, dict[str, str]]:
    """Parse a per-session brand-mapping override CSV.

    Required columns: keyword, brand, country, division, team.
    Returns a dict shaped like config.BRAND_KEYWORD_MAP. Keywords are
    lowercased; rows with empty keyword are dropped.
    """
    df = pd.read_csv(file)
    required = {"keyword", "brand", "country", "division", "team"}
    missing = required - {c.lower() for c in df.columns}
    if missing:
        raise ValueError(
            f"CSV must contain columns: {sorted(required)}; missing {sorted(missing)}"
        )

    # Normalise column names to lowercase for resilient lookup
    df.columns = [c.lower() for c in df.columns]

    out: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        kw = str(row["keyword"] or "").strip().lower()
        if not kw or kw == "nan":
            continue
        out[kw] = {
            "brand":    str(row["brand"]).strip(),
            "country":  str(row["country"]).strip(),
            "division": str(row["division"]).strip(),
            "team":     str(row["team"]).strip(),
        }
    return out


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _orphan_meta_placeholder(bank_row: BankRow) -> SplitRow:
    """Single SplitRow for a Meta bank line with no matching invoice PDF."""
    desc = (
        f"OCBC: PCard - {bank_row.cardholder} - Facebook (Meta) - "
        f"Ref#{bank_row.fb_code or '?'} - (no invoice PDF — please supply)"
    )
    return SplitRow(
        description=desc,
        gl_account=None,
        gl_account_name=None,
        line_number=0,
        brand="0_DIM2",
        country="VN",
        division="0_DIM4",
        team="0_DIM5",
        tax_code="",
        sgd_amount=bank_row.sgd_amount,
        bank_date=bank_row.transaction_date,
        bank_narrative=bank_row.narrative,
        card_last4=bank_row.last_4_digits,
        cardholder=bank_row.cardholder,
        merchant_currency=bank_row.merchant_currency,
        merchant_amount=bank_row.merchant_amount,
        bank_sgd=bank_row.sgd_amount,
        match_status=MatchStatus.UNMATCHED.value,
        row_type=RowType.META_SPEND.value,
    )


def _sort_and_renumber(rows: list[SplitRow]) -> list[SplitRow]:
    """Stable-sort by bank_date (rows without dates last), then renumber 1..N."""
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda x: (x[1].bank_date or date.max, x[0]))
    sorted_rows = [r for _, r in indexed]
    for i, r in enumerate(sorted_rows, start=1):
        r.line_number = i
    return sorted_rows
