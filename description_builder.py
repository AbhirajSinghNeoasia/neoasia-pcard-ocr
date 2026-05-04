"""
Auto-generated SAP descriptions for the journal-entry export.

Three flavours, exact formats per CLAUDE.md Section 4.5:

  Simple (matched to OCR):
    OCBC: PCard - {cardholder} - {vendor} - {nature} ({currency}{amount})
    e.g. "OCBC: PCard - Sharry - Singapore Airlines - Booking#DDOWAT - "
         "Air-ticket 05/03/26, SGN/SIN (VND3,681,000)"

  Simple (no OCR match — falls back to bank narrative):
    OCBC: PCard - {cardholder} - {bank_narrative} ({currency}{merchant_amount})

  Meta spend row:
    OCBC: PCard - {cardholder} - Facebook (Meta) - Ref#{fb_code} - {month_year} - VND {spend_vnd}
    e.g. "OCBC: PCard - Sharry - Facebook (Meta) - Ref#TTTW5EZGT2 - Feb26 - VND 3568"

  Meta VAT row:
    OCBC: PCard - {cardholder} - Facebook (Meta) - Ref#{fb_code} - {month_year} - VND {vat_vnd}
    e.g. "OCBC: PCard - Sharry - Facebook (Meta) - Ref#TTTW5EZGT2 - Feb26 - VND 357"

Helper:
    derive_month_year(invoice_date | None, bank_date) -> "Feb26", "Mar26", ...
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from models import BankRow, OcrTransaction


_PREFIX = "OCBC: PCard - "


# ---------------------------------------------------------------------------
# Simple-transaction descriptions
# ---------------------------------------------------------------------------


def build_simple_description(
    bank_row: BankRow,
    ocr: Optional[OcrTransaction] = None,
) -> str:
    """Build the description for a non-Meta transaction.

    With an OCR match: cardholder + vendor + nature + (currency+amount).
    Without an OCR match: cardholder + raw bank narrative + (merchant currency
    & amount as charged). The fallback ensures the row still has enough
    context for finance to triage manually.
    """
    cardholder = bank_row.cardholder

    if ocr is not None:
        vendor = (ocr.vendor or "").strip() or bank_row.narrative.strip()
        nature = (ocr.nature or "").strip()
        currency = (ocr.currency or bank_row.merchant_currency or "").strip()
        amount = ocr.amount if ocr.amount is not None else bank_row.merchant_amount
        money = _format_currency_amount(currency, float(amount))

        if nature:
            return f"{_PREFIX}{cardholder} - {vendor} - {nature} ({money})"
        return f"{_PREFIX}{cardholder} - {vendor} ({money})"

    # No OCR — fall back to bank narrative + merchant currency/amount
    money = _format_currency_amount(bank_row.merchant_currency, bank_row.merchant_amount)
    narrative = bank_row.narrative.strip() or "(no narrative)"
    return f"{_PREFIX}{cardholder} - {narrative} ({money})"


# ---------------------------------------------------------------------------
# Meta-transaction descriptions
# ---------------------------------------------------------------------------


def build_meta_spend_description(
    bank_row: BankRow,
    fb_code: str,
    month_year: str,
    spend_vnd: float | int,
) -> str:
    """Description for a Meta spend (Advertisement) row.

    Format:
      OCBC: PCard - {cardholder} - Facebook (Meta) - Ref#{fb_code} - {month_year} - VND {spend_vnd_int}
    spend_vnd is shown as an integer (no separators) — matches CLAUDE.md
    examples like "VND 3568" / "VND 70042".
    """
    return (
        f"{_PREFIX}{bank_row.cardholder} - Facebook (Meta) - "
        f"Ref#{fb_code} - {month_year} - VND {int(round(float(spend_vnd)))}"
    )


def build_meta_vat_description(
    bank_row: BankRow,
    fb_code: str,
    month_year: str,
    vat_vnd: float | int,
) -> str:
    """Description for a Meta VAT row.

    Same shape as the spend description; the caller is responsible for
    passing the per-campaign VAT VND (rounded to integer — e.g. 356.8 -> 357).
    """
    return (
        f"{_PREFIX}{bank_row.cardholder} - Facebook (Meta) - "
        f"Ref#{fb_code} - {month_year} - VND {int(round(float(vat_vnd)))}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def derive_month_year(invoice_date: Optional[date], bank_date: date) -> str:
    """Return abbreviated month + 2-digit year, e.g. 'Feb26', 'Mar26'.

    Uses invoice_date when available (the Meta invoice's own period is the
    truth) and falls back to bank_date otherwise.
    """
    d = invoice_date if invoice_date is not None else bank_date
    return d.strftime("%b%y")


def _format_currency_amount(currency: str, amount: float) -> str:
    """Format an amount with its ISO currency prefix.

    VND is integer with thousand separators (e.g. "VND3,681,000"). Other
    currencies use 2 decimals with thousand separators (e.g. "SGD41.60",
    "SGD1,234.56"). No space between currency and number — matches the
    documented examples in CLAUDE.md.
    """
    cur = (currency or "").strip().upper() or "?"
    if cur == "VND":
        return f"{cur}{int(round(amount)):,}"
    return f"{cur}{amount:,.2f}"
