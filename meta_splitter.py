"""
Meta-transaction splitter.

For each Meta bank row + its corresponding OCR'd invoice, produces a list of
SplitRow records suitable for the SAP journal entry export. Each campaign
yields TWO output rows:
  - "meta_spend" — GL 6210101 (Advertisement), SGD = bank_sgd * spend_vnd / total_vnd
  - "meta_vat"   — GL 6312701 (VAT expenses), SGD = bank_sgd * (spend_vnd * 10%) / total_vnd

Penny-perfect rounding (CRITICAL):
  All monetary computation runs in Decimal with ROUND_HALF_UP (the accounting
  convention, not Python's default ROUND_HALF_EVEN). Each SGD component is
  rounded to 2 decimals, then the sum is reconciled against bank_sgd. Any
  residual cent is added to the LARGEST spend row. The post-adjustment
  Decimal sum is asserted to equal bank_sgd exactly — so the SAP journal
  reconciles with the bank statement to the cent every time.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from config import (
    BRAND_KEYWORD_MAP,
    DEFAULT_BRAND_MAP,
    GL_META_AD,
    GL_META_VAT,
    VAT_RATE_DEFAULT,
)
from description_builder import (
    build_meta_spend_description,
    build_meta_vat_description,
    derive_month_year,
)
from models import BankRow, MatchStatus, MetaInvoiceOCR, RowType, SplitRow


# ---------------------------------------------------------------------------
# Brand keyword lookup
# ---------------------------------------------------------------------------


def lookup_brand_dimensions(ad_set_name: Optional[str]) -> dict[str, str]:
    """Return SAP dimensions for an ad set name via case-insensitive keyword scan.

    First match wins (insertion order of BRAND_KEYWORD_MAP). Falls back to
    DEFAULT_BRAND_MAP when no keyword matches or the input is empty.
    """
    name = (ad_set_name or "").lower()
    if not name:
        return dict(DEFAULT_BRAND_MAP)
    for keyword, dims in BRAND_KEYWORD_MAP.items():
        if keyword in name:
            return dict(dims)
    return dict(DEFAULT_BRAND_MAP)


# ---------------------------------------------------------------------------
# Public splitter
# ---------------------------------------------------------------------------


def split_meta_transaction(
    bank_row: BankRow,
    meta_ocr: MetaInvoiceOCR,
    start_line: int = 1,
) -> list[SplitRow]:
    """Split one Meta bank row into 2N SplitRow records (N = number of campaigns).

    Layout: spend rows for every campaign first (in OCR order), then the
    matching VAT rows in the same order. This mirrors the team's existing
    reviewed output and keeps the Excel readable.

    Penny-perfect: the sum of all returned SplitRow.sgd_amount values equals
    bank_row.sgd_amount EXACTLY (no float drift) when both are converted back
    through Decimal(str(...)). Any residual cent is absorbed by the largest
    spend row.
    """
    if not meta_ocr.campaigns:
        raise ValueError(
            f"Cannot split Meta transaction {bank_row.fb_code}: no campaigns "
            f"in OCR result for {meta_ocr.source_file or '<unknown invoice>'}."
        )
    if meta_ocr.total_paid_vnd <= 0:
        raise ValueError(
            f"Cannot split Meta transaction {bank_row.fb_code}: "
            f"total_paid_vnd is {meta_ocr.total_paid_vnd}."
        )

    bank_sgd_d = _D(bank_row.sgd_amount)
    total_vnd_d = _D(meta_ocr.total_paid_vnd)
    vat_rate_d = _D(VAT_RATE_DEFAULT)

    # --- Compute Decimal SGD amounts per campaign ----------------------
    spend_sgds: list[Decimal] = []
    vat_sgds:   list[Decimal] = []
    for c in meta_ocr.campaigns:
        spend_vnd_d = _D(c.spend_vnd)
        # spend SGD: bank * spend_vnd / total_vnd
        raw_spend = (bank_sgd_d * spend_vnd_d) / total_vnd_d
        spend_sgds.append(_quantize(raw_spend))
        # campaign VAT VND: spend_vnd * 10%
        # vat SGD: bank * vat_vnd / total_vnd
        raw_vat = (bank_sgd_d * spend_vnd_d * vat_rate_d) / total_vnd_d
        vat_sgds.append(_quantize(raw_vat))

    # --- Penny-perfect adjustment --------------------------------------
    current_total = sum(spend_sgds, Decimal("0")) + sum(vat_sgds, Decimal("0"))
    diff = bank_sgd_d - current_total
    if diff != 0:
        # Absorb residual into the largest spend row. argmax breaks ties
        # by lower index (deterministic).
        max_idx = max(range(len(spend_sgds)), key=lambda i: spend_sgds[i])
        spend_sgds[max_idx] = _quantize(spend_sgds[max_idx] + diff)

    # Internal sanity check — never expected to trip, but if it does we
    # want a loud failure rather than a silent off-by-cent in production.
    final_total = sum(spend_sgds, Decimal("0")) + sum(vat_sgds, Decimal("0"))
    assert final_total == bank_sgd_d, (
        f"Penny-perfect failed: sum {final_total} != bank {bank_sgd_d}"
    )

    # --- Build SplitRow records ----------------------------------------
    month_year = derive_month_year(meta_ocr.invoice_date, bank_row.transaction_date)
    fb_code = bank_row.fb_code or meta_ocr.reference_number
    line = start_line
    rows: list[SplitRow] = []

    # Spend rows first
    for c, spend_sgd_d in zip(meta_ocr.campaigns, spend_sgds):
        dims = lookup_brand_dimensions(c.ad_set_name)
        rows.append(
            SplitRow(
                description=build_meta_spend_description(
                    bank_row, fb_code, month_year, c.spend_vnd
                ),
                gl_account=GL_META_AD[0],
                gl_account_name=GL_META_AD[1],
                line_number=line,
                brand=dims["brand"],
                country=dims["country"],
                division=dims["division"],
                team=dims["team"],
                tax_code="",
                sgd_amount=float(spend_sgd_d),
                bank_date=bank_row.transaction_date,
                bank_narrative=bank_row.narrative,
                card_last4=bank_row.last_4_digits,
                cardholder=bank_row.cardholder,
                merchant_currency=bank_row.merchant_currency,
                merchant_amount=bank_row.merchant_amount,
                bank_sgd=bank_row.sgd_amount,
                match_status=MatchStatus.META_SPLIT.value,
                row_type=RowType.META_SPEND.value,
            )
        )
        line += 1

    # VAT rows in the same order
    for c, vat_sgd_d in zip(meta_ocr.campaigns, vat_sgds):
        dims = lookup_brand_dimensions(c.ad_set_name)
        # Per-campaign VAT in VND, displayed in the description as a rounded
        # integer (matches the reviewed-output convention, e.g. 356.8 -> 357).
        campaign_vat_vnd_int = int(
            _quantize(_D(c.spend_vnd) * _D(VAT_RATE_DEFAULT), Decimal("1"))
        )
        rows.append(
            SplitRow(
                description=build_meta_vat_description(
                    bank_row, fb_code, month_year, campaign_vat_vnd_int
                ),
                gl_account=GL_META_VAT[0],
                gl_account_name=GL_META_VAT[1],
                line_number=line,
                brand=dims["brand"],
                country=dims["country"],
                division=dims["division"],
                team=dims["team"],
                tax_code="",
                sgd_amount=float(vat_sgd_d),
                bank_date=bank_row.transaction_date,
                bank_narrative=bank_row.narrative,
                card_last4=bank_row.last_4_digits,
                cardholder=bank_row.cardholder,
                merchant_currency=bank_row.merchant_currency,
                merchant_amount=bank_row.merchant_amount,
                bank_sgd=bank_row.sgd_amount,
                match_status=MatchStatus.META_SPLIT.value,
                row_type=RowType.META_VAT.value,
            )
        )
        line += 1

    return rows


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------


def _D(x: float | int | Decimal | str) -> Decimal:
    """Decimal coercion that goes through str() to avoid float-binary noise."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _quantize(value: Decimal, exp: Decimal = Decimal("0.01")) -> Decimal:
    """Round to the given exponent using ROUND_HALF_UP (accounting convention)."""
    return value.quantize(exp, rounding=ROUND_HALF_UP)
