"""
Reconciliation engine — pairs OCR results with bank rows.

Two flavours:

  match_simple_transactions(bank_rows, ocr_results) -> list[MatchedRow]
      Two-pass greedy 1:1 match for non-Meta rows.
        Pass 1: only accept score >= 100 (high-confidence "exact").
        Pass 2: only accept score >= 50  (lower-confidence "approximate").
      Each OCR result is consumed once. Unmatched bank rows still appear in
      the output with ocr=None.

  match_meta_transactions(bank_rows, meta_ocrs) -> dict[str, MetaInvoiceOCR]
      Deterministic FB-code join. Logs warnings for orphan codes on either
      side so finance can investigate before posting.

  build_gl_suggestion(bank_row, ocr) -> tuple[str, str] | None
      Vendor-keyword GL suggestion (Section 4.6 of CLAUDE.md). The Excel
      output is finance's source of truth — these are suggestions only.

Score components (max 120):
  amount: 80 pts if |diff| <= 1% of reference amount
          50 pts if |diff| <= 5%
           0 pts otherwise
          (compared in merchant currency when OCR currency matches; in SGD
           when both are SGD; otherwise 0 — a deliberate departure from the
           literal CLAUDE.md text, which assumes both amounts are SGD and
           would always score 0 for VND/PHP/IDR travel receipts.)
  date:   20 / 10 / 5 / 0 for diff of 0 / 1 / <=3 / >3 days
  vendor: 20 if any signature keyword from VENDOR_SIGNATURES appears in BOTH
          the bank narrative AND the OCR vendor or nature; else 0.
"""

from __future__ import annotations

import logging
from typing import Optional

from config import GL_VENDOR_MAP, VENDOR_SIGNATURES
from models import BankRow, MatchedRow, MetaInvoiceOCR, OcrTransaction


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_simple_transactions(
    bank_rows: list[BankRow],
    ocr_results: list[OcrTransaction],
) -> list[MatchedRow]:
    """Pair simple (non-Meta) bank rows with OCR results, greedy two-pass.

    Returns a MatchedRow for every simple bank row in the same order as the
    input. Unmatched rows have ocr=None.
    """
    simple_banks: list[tuple[int, BankRow]] = [
        (i, b) for i, b in enumerate(bank_rows)
        if _is_simple(b)
    ]

    matched: dict[int, tuple[int, int, dict, str]] = {}   # bank_global_idx -> (ocr_idx, score, components, label)
    consumed_ocrs: set[int] = set()

    for threshold, label in [(100, "exact"), (50, "approximate")]:
        candidates: list[tuple[int, int, int, dict]] = []
        for bidx, brow in simple_banks:
            if bidx in matched:
                continue
            for oidx, ocr in enumerate(ocr_results):
                if oidx in consumed_ocrs:
                    continue
                score, comps = score_pair(brow, ocr)
                if score >= threshold:
                    candidates.append((score, bidx, oidx, comps))
        # Sort highest score first; ties break by bank index then ocr index
        # for deterministic behaviour.
        candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
        for score, bidx, oidx, comps in candidates:
            if bidx in matched or oidx in consumed_ocrs:
                continue
            matched[bidx] = (oidx, score, comps, label)
            consumed_ocrs.add(oidx)

    out: list[MatchedRow] = []
    for bidx, brow in simple_banks:
        if bidx in matched:
            oidx, score, comps, label = matched[bidx]
            out.append(MatchedRow(
                bank=brow,
                ocr=ocr_results[oidx],
                match_confidence=label,
                match_reason=f"score={score} amount={comps['amount']} date={comps['date']} vendor={comps['vendor']}",
            ))
        else:
            out.append(MatchedRow(
                bank=brow, ocr=None,
                match_confidence=None, match_reason=None,
            ))
    return out


def match_meta_transactions(
    bank_rows: list[BankRow],
    meta_ocrs: list[MetaInvoiceOCR],
) -> dict[str, MetaInvoiceOCR]:
    """Join Meta bank rows to invoices by FB code (exact string match).

    Returns dict keyed by FB code. Warns on orphans on either side so finance
    can spot missing receipts before exporting.
    """
    bank_codes = {b.fb_code for b in bank_rows if _is_meta(b) and b.fb_code}
    ocr_by_code = {o.reference_number: o for o in meta_ocrs if o.reference_number}

    matched: dict[str, MetaInvoiceOCR] = {}
    for code in bank_codes:
        if code in ocr_by_code:
            matched[code] = ocr_by_code[code]
        else:
            logger.warning("Meta bank row FB#%s has no matching invoice PDF.", code)

    for code in ocr_by_code:
        if code not in bank_codes:
            logger.warning(
                "Meta invoice FB#%s has no matching bank row (orphan invoice).", code,
            )
    return matched


def build_gl_suggestion(
    bank_row: BankRow,
    ocr: Optional[OcrTransaction],
) -> Optional[tuple[str, str]]:
    """Suggest (gl_code, gl_name) by vendor-keyword scan.

    Searches both the bank narrative and the OCR vendor/nature (if present)
    for any keyword in GL_VENDOR_MAP. First match wins. Returns None if
    nothing matches — finance fills in via the Excel dropdown.
    """
    haystack = (bank_row.narrative or "").lower()
    if ocr is not None:
        haystack += " "
        haystack += (ocr.vendor or "").lower()
        haystack += " "
        haystack += (ocr.nature or "").lower()
    for keyword, (gl_code, gl_name) in GL_VENDOR_MAP.items():
        if keyword in haystack:
            return (gl_code, gl_name)
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_pair(bank_row: BankRow, ocr: OcrTransaction) -> tuple[int, dict[str, int]]:
    """Return (total_score, {component: score}) for a (bank, ocr) pair."""
    components = {
        "amount": _amount_score(bank_row, ocr),
        "date":   _date_score(bank_row, ocr),
        "vendor": _vendor_score(bank_row, ocr),
    }
    return sum(components.values()), components


def _amount_score(bank_row: BankRow, ocr: OcrTransaction) -> int:
    """Score the amount agreement, choosing the best applicable currency view."""
    if ocr.amount is None:
        return 0

    # Try comparison in merchant currency first (most reliable for travel),
    # then in SGD if either side is SGD-denominated. Take the max.
    candidates: list[int] = []
    bank_cur = (bank_row.merchant_currency or "").upper()
    ocr_cur = (ocr.currency or "").upper()

    if ocr_cur and ocr_cur == bank_cur:
        candidates.append(_diff_to_score(bank_row.merchant_amount, ocr.amount))
    if ocr_cur == "SGD":
        candidates.append(_diff_to_score(bank_row.sgd_amount, ocr.amount))
    if bank_cur == "SGD" and ocr_cur in {"", "SGD"}:
        # Some receipts omit the currency code; assume SGD locally if both
        # the bank charge and merchant currency are SGD.
        candidates.append(_diff_to_score(bank_row.sgd_amount, ocr.amount))
    return max(candidates) if candidates else 0


def _diff_to_score(reference: float, candidate: float) -> int:
    if reference == 0:
        return 80 if candidate == 0 else 0
    pct = abs(reference - candidate) / abs(reference)
    if pct <= 0.01:
        return 80
    if pct <= 0.05:
        return 50
    return 0


def _date_score(bank_row: BankRow, ocr: OcrTransaction) -> int:
    if ocr.transaction_date is None:
        return 0
    diff = abs((bank_row.transaction_date - ocr.transaction_date).days)
    if diff == 0:
        return 20
    if diff == 1:
        return 10
    if diff <= 3:
        return 5
    return 0


def _vendor_score(bank_row: BankRow, ocr: OcrTransaction) -> int:
    narrative = (bank_row.narrative or "").lower()
    ocr_text = " ".join([
        (ocr.vendor or ""), (ocr.nature or ""),
    ]).lower()
    for _vendor_name, keywords in VENDOR_SIGNATURES.items():
        bank_match = any(kw in narrative for kw in keywords)
        ocr_match = any(kw in ocr_text for kw in keywords)
        if bank_match and ocr_match:
            return 20
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_simple(b: BankRow) -> bool:
    return b.transaction_type == "simple"


def _is_meta(b: BankRow) -> bool:
    return b.transaction_type == "meta"
