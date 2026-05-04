"""
Phase 4 tests for meta_splitter + description_builder.

NO API CALLS. Constructs synthetic BankRow + MetaInvoiceOCR fixtures from
known-good values (verified against the Phase 3 OCR result and the team's
reviewed output) and asserts the splitter produces correct line items, brand
dimensions, descriptions, GL accounts, and — most importantly — a sum that
equals the bank charge to the cent.

Coverage:
  1. Brand keyword lookup edge cases.
  2. derive_month_year + currency formatting.
  3. Description builders match CLAUDE.md examples character-for-character.
  4. TTTW5EZGT2 split (real data, bank=9.65 SGD): 6 rows, brand mapping,
     GL accounts, exact sum, descriptions.
  5. PPTW5EZGT2 split (real data, bank=3.94 SGD): generalisation check.
  6. SYNTHETIC penny-perfect case where naive rounding misses by 0.02 —
     forces the adjustment branch to execute and verifies it lands on the
     largest spend row.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

from description_builder import (
    build_meta_spend_description,
    build_meta_vat_description,
    build_simple_description,
    derive_month_year,
)
from meta_splitter import lookup_brand_dimensions, split_meta_transaction
from models import (
    BankRow,
    MetaCampaign,
    MetaInvoiceOCR,
    OcrTransaction,
    TransactionType,
)


PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  FAIL  {label}  ({detail})")


def section(t: str) -> None:
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def cents_sum(rows) -> int:
    """Sum SGD amounts as integer cents — bypasses float drift entirely."""
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r.sgd_amount))
    return int((total * 100).to_integral_value())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bank_row(
    *,
    fb_code: str,
    sgd: float,
    vnd: float,
    txdate: date,
    cardholder: str = "Sharry",
    last4: str = "9804",
) -> BankRow:
    return BankRow(
        row_index=0,
        sheet_name="Bank statement - Feb26",
        transaction_id=f"TX-{fb_code}",
        transaction_date=txdate,
        narrative=f"FACEBK *{fb_code} fb.me/ads",
        last_4_digits=last4,
        cardholder=cardholder,
        merchant_amount=vnd,
        merchant_currency="VND",
        sgd_amount=sgd,
        transaction_type=TransactionType.META,
        fb_code=fb_code,
        is_refund=False,
    )


def _meta_ocr(
    *,
    ref: str,
    subtotal: int,
    vat: int,
    total: int,
    campaigns: list[tuple[str, int]],
    invoice_date: date | None = None,
) -> MetaInvoiceOCR:
    return MetaInvoiceOCR(
        reference_number=ref,
        invoice_date=invoice_date,
        payment_method_last4="9804",
        total_paid_vnd=total,
        subtotal_vnd=subtotal,
        vat_vnd=vat,
        vat_rate_percent=10.0,
        campaigns=[MetaCampaign(ad_set_name=n, spend_vnd=v) for n, v in campaigns],
        source_file=f"{ref}.pdf",
    )


# ---------------------------------------------------------------------------
# 1. Brand keyword lookup
# ---------------------------------------------------------------------------


def test_brand_lookup() -> None:
    section("1. lookup_brand_dimensions")
    cases = [
        ("Calecim Brand Ads",            "CAL", "MED-I",  "T1"),
        ("CALECIM Boosted",              "CAL", "MED-I",  "T1"),  # case-insensitive
        ("Heliocare Brands Campaign",    "HEL", "MED-I",  "T1"),
        ("64MG Profhilo Webinar 28 Feb", "PRO", "MED-II", "T1"),
        ("Profhilo intro",               "PRO", "MED-II", "T1"),
        ("Revalene push",                "REV", "MED-I",  "T1"),
        ("Nourkrin awareness",           "NOU", "MED-I",  "T1"),
        ("Sessions deck",                "0_DIM2", "MED-I", "T1"),
        ("Announcement Post",            "0_DIM2", "MED-I", "T1"),  # default
        ("",                             "0_DIM2", "MED-I", "T1"),  # empty default
        (None,                           "0_DIM2", "MED-I", "T1"),
    ]
    for name, brand, division, team in cases:
        d = lookup_brand_dimensions(name)
        ok = (d["brand"] == brand and d["division"] == division and d["team"] == team)
        check(f"lookup_brand_dimensions({name!r}) -> brand={brand}, div={division}",
              ok, f"got {d}")


# ---------------------------------------------------------------------------
# 2. derive_month_year + simple-description format
# ---------------------------------------------------------------------------


def test_helpers() -> None:
    section("2. Helpers (derive_month_year + simple description)")

    check("derive_month_year(invoice=Feb 14 2026, bank=anything) == 'Feb26'",
          derive_month_year(date(2026, 2, 14), date(2026, 3, 1)) == "Feb26")
    check("derive_month_year(invoice=None, bank=Mar 1 2026) == 'Mar26'",
          derive_month_year(None, date(2026, 3, 1)) == "Mar26")

    # Simple description with OCR — mirror the Singapore Airlines DDOWAT example
    bank = _bank_row(fb_code="N/A", sgd=186.10, vnd=3681000.0,
                     txdate=date(2026, 2, 4), cardholder="Sharry")
    bank.transaction_type = TransactionType.SIMPLE.value
    bank.merchant_currency = "VND"
    bank.narrative = "SQ INTERNET PURCHASES"
    ocr = OcrTransaction(
        transaction_date=date(2026, 2, 4),
        vendor="Singapore Airlines",
        nature="Booking#DDOWAT - Air-ticket 05/03/26, SGN/SIN",
        currency="VND",
        amount=3681000,
    )
    desc = build_simple_description(bank, ocr)
    expected = ("OCBC: PCard - Sharry - Singapore Airlines - "
                "Booking#DDOWAT - Air-ticket 05/03/26, SGN/SIN (VND3,681,000)")
    check("build_simple_description matches CLAUDE.md DDOWAT example",
          desc == expected,
          f"\n    expected: {expected}\n    got     : {desc}")

    # Simple description without OCR — falls back to bank narrative
    desc2 = build_simple_description(bank, None)
    check("build_simple_description fallback uses bank narrative",
          "SQ INTERNET PURCHASES" in desc2 and "(VND3,681,000)" in desc2,
          f"got {desc2!r}")

    # SGD example (Shopee, with 2 decimals)
    sgd_bank = _bank_row(fb_code="N/A", sgd=41.60, vnd=41.60, txdate=date(2026, 2, 4),
                         cardholder="Jaslyn", last4="9671")
    sgd_bank.transaction_type = TransactionType.SIMPLE.value
    sgd_bank.merchant_currency = "SGD"
    sgd_ocr = OcrTransaction(
        transaction_date=date(2026, 2, 4),
        vendor="Shopee",
        nature="Order ID#260204SMA46GDX - CNY Goodies",
        currency="SGD",
        amount=41.60,
    )
    desc3 = build_simple_description(sgd_bank, sgd_ocr)
    expected3 = ("OCBC: PCard - Jaslyn - Shopee - "
                 "Order ID#260204SMA46GDX - CNY Goodies (SGD41.60)")
    check("build_simple_description matches CLAUDE.md Shopee example",
          desc3 == expected3,
          f"\n    expected: {expected3}\n    got     : {desc3}")


# ---------------------------------------------------------------------------
# 3. Meta description builders
# ---------------------------------------------------------------------------


def test_meta_descriptions() -> None:
    section("3. Meta spend/VAT description builders")

    bank = _bank_row(fb_code="TTTW5EZGT2", sgd=9.65, vnd=192508.0,
                     txdate=date(2026, 2, 14))

    spend = build_meta_spend_description(bank, "TTTW5EZGT2", "Feb26", 3568)
    expected_spend = ("OCBC: PCard - Sharry - Facebook (Meta) - "
                      "Ref#TTTW5EZGT2 - Feb26 - VND 3568")
    check("Meta spend description matches CLAUDE.md example",
          spend == expected_spend,
          f"\n    expected: {expected_spend}\n    got     : {spend}")

    vat = build_meta_vat_description(bank, "TTTW5EZGT2", "Feb26", 357)
    expected_vat = ("OCBC: PCard - Sharry - Facebook (Meta) - "
                    "Ref#TTTW5EZGT2 - Feb26 - VND 357")
    check("Meta VAT description matches CLAUDE.md example",
          vat == expected_vat,
          f"\n    expected: {expected_vat}\n    got     : {vat}")


# ---------------------------------------------------------------------------
# 4. Real-data split: TTTW5EZGT2
# ---------------------------------------------------------------------------


def test_split_tttw() -> None:
    section("4. Split TTTW5EZGT2 (bank 9.65 SGD, 3 campaigns)")

    bank = _bank_row(fb_code="TTTW5EZGT2", sgd=9.65, vnd=192508.0,
                     txdate=date(2026, 2, 14))
    ocr = _meta_ocr(
        ref="TTTW5EZGT2",
        subtotal=175007, vat=17501, total=192508,
        campaigns=[
            ("Calecim Brand Ads", 3568),
            ("Heliocare Brands Campaign", 70042),
            ("64MG Profhilo Webinar 28 Feb", 101397),
        ],
        invoice_date=date(2026, 2, 14),
    )
    rows = split_meta_transaction(bank, ocr, start_line=1)

    check("Exactly 6 SplitRows generated", len(rows) == 6, f"got {len(rows)}")
    check("Sum equals 9.65 SGD to the cent (965 cents)",
          cents_sum(rows) == 965,
          f"got {cents_sum(rows)} cents")

    # Layout: 3 spend rows then 3 VAT rows
    spend_rows = [r for r in rows if r.row_type == "meta_spend"]
    vat_rows   = [r for r in rows if r.row_type == "meta_vat"]
    check("3 spend rows + 3 VAT rows",
          len(spend_rows) == 3 and len(vat_rows) == 3,
          f"spend={len(spend_rows)}, vat={len(vat_rows)}")
    check("All spend rows use GL 6210101 / Advertisement",
          all(r.gl_account == "6210101" and r.gl_account_name == "Advertisement"
              for r in spend_rows))
    check("All VAT rows use GL 6312701 / VAT expenses",
          all(r.gl_account == "6312701" and r.gl_account_name == "VAT expenses"
              for r in vat_rows))

    # Brand dimensions per spend row (matched by ad set name)
    by_name = {r.description: r for r in spend_rows}
    calecim = next(r for r in spend_rows if "VND 3568" in r.description)
    heliocare = next(r for r in spend_rows if "VND 70042" in r.description)
    profhilo = next(r for r in spend_rows if "VND 101397" in r.description)

    check("Calecim spend row: brand=CAL, division=MED-I, team=T1",
          calecim.brand == "CAL" and calecim.division == "MED-I" and calecim.team == "T1",
          f"got brand={calecim.brand}, div={calecim.division}, team={calecim.team}")
    check("Heliocare spend row: brand=HEL, division=MED-I, team=T1",
          heliocare.brand == "HEL" and heliocare.division == "MED-I" and heliocare.team == "T1",
          f"got brand={heliocare.brand}, div={heliocare.division}, team={heliocare.team}")
    check("Profhilo spend row: brand=PRO, division=MED-II, team=T1",
          profhilo.brand == "PRO" and profhilo.division == "MED-II" and profhilo.team == "T1",
          f"got brand={profhilo.brand}, div={profhilo.division}, team={profhilo.team}")

    # Spend SGD amounts (rounded to cents)
    cents = {r.description: round(r.sgd_amount * 100) for r in spend_rows}
    check("Calecim spend SGD ~ 0.18", cents.get(calecim.description) == 18,
          f"got {cents.get(calecim.description)} cents")
    check("Heliocare spend SGD ~ 3.51", cents.get(heliocare.description) == 351,
          f"got {cents.get(heliocare.description)} cents")
    check("Profhilo spend SGD ~ 5.08", cents.get(profhilo.description) == 508,
          f"got {cents.get(profhilo.description)} cents")

    # VAT descriptions use rounded per-campaign VAT VND (356.8 -> 357 etc.)
    vat_descs = [r.description for r in vat_rows]
    check("Calecim VAT row description shows 'VND 357'",
          any("VND 357" == d.split(" - ")[-1] for d in vat_descs),
          f"got {vat_descs}")
    check("Heliocare VAT row description shows 'VND 7004'",
          any("VND 7004" == d.split(" - ")[-1] for d in vat_descs),
          f"got {vat_descs}")
    check("Profhilo VAT row description shows 'VND 10140'",
          any("VND 10140" == d.split(" - ")[-1] for d in vat_descs),
          f"got {vat_descs}")

    # Match status + line numbers
    check("All rows have match_status='Meta Split'",
          all(r.match_status == "Meta Split" for r in rows))
    check("Line numbers are sequential 1..6",
          [r.line_number for r in rows] == [1, 2, 3, 4, 5, 6],
          f"got {[r.line_number for r in rows]}")

    # First spend description must match CLAUDE.md exactly
    expected_first = ("OCBC: PCard - Sharry - Facebook (Meta) - "
                      "Ref#TTTW5EZGT2 - Feb26 - VND 3568")
    check("First spend description matches CLAUDE.md exactly",
          calecim.description == expected_first,
          f"\n    expected: {expected_first}\n    got     : {calecim.description}")


# ---------------------------------------------------------------------------
# 5. Real-data split: PPTW5EZGT2 (generalisation)
# ---------------------------------------------------------------------------


def test_split_pptw() -> None:
    section("5. Split PPTW5EZGT2 (bank 3.94 SGD, 3 campaigns) — generalisation")

    bank = _bank_row(fb_code="PPTW5EZGT2", sgd=3.94, vnd=78582.0,
                     txdate=date(2026, 2, 14))
    ocr = _meta_ocr(
        ref="PPTW5EZGT2",
        subtotal=71438, vat=7144, total=78582,
        campaigns=[
            ("Calecim Brand Ads", 25210),
            ("Heliocare Brands Campaign", 44577),
            ("64MG Profhilo Webinar 28 Feb", 1651),
        ],
        invoice_date=date(2026, 2, 14),
    )
    rows = split_meta_transaction(bank, ocr, start_line=10)

    check("PPTW: 6 rows generated", len(rows) == 6, f"got {len(rows)}")
    check("PPTW: sum equals 3.94 SGD to the cent (394 cents)",
          cents_sum(rows) == 394,
          f"got {cents_sum(rows)} cents")
    check("PPTW: line numbers continue from start_line=10",
          [r.line_number for r in rows] == [10, 11, 12, 13, 14, 15],
          f"got {[r.line_number for r in rows]}")
    check("PPTW: descriptions reference Ref#PPTW5EZGT2",
          all("Ref#PPTW5EZGT2" in r.description for r in rows))
    # Brand dims still flow through correctly
    spend_rows = [r for r in rows if r.row_type == "meta_spend"]
    brands = sorted({r.brand for r in spend_rows})
    check("PPTW: brands across spend rows = {CAL, HEL, PRO}",
          brands == ["CAL", "HEL", "PRO"], f"got {brands}")


# ---------------------------------------------------------------------------
# 6. SYNTHETIC penny-perfect adjustment (residual cent path)
# ---------------------------------------------------------------------------


def test_penny_perfect_adjustment() -> None:
    section("6. Penny-perfect: synthetic residual case (forces adjustment)")

    # Engineered so naive rounding overshoots by 0.02 SGD.
    # 3 campaigns of 33/33/34 VND (sum 100), VAT 10 VND, total 110, bank 0.55 SGD.
    # Naive rounding: 0.17 + 0.17 + 0.17 + 0.02 + 0.02 + 0.02 = 0.57 (over by 0.02).
    # Algorithm should subtract 0.02 from the largest spend (first 0.17 -> 0.15).
    bank = _bank_row(fb_code="SYNTH00001", sgd=0.55, vnd=100.0,
                     txdate=date(2026, 2, 1))
    ocr = _meta_ocr(
        ref="SYNTH00001",
        subtotal=100, vat=10, total=110,
        campaigns=[
            ("Synthetic Calecim", 33),
            ("Synthetic Heliocare", 33),
            ("Synthetic Profhilo", 34),
        ],
        invoice_date=date(2026, 2, 1),
    )
    rows = split_meta_transaction(bank, ocr, start_line=1)

    check("Synthetic: 6 rows", len(rows) == 6, f"got {len(rows)}")
    check("Synthetic: sum equals 0.55 SGD to the cent (55 cents)",
          cents_sum(rows) == 55,
          f"got {cents_sum(rows)} cents")

    # The adjustment must land on a spend row (not VAT)
    spend_cents = sorted(round(r.sgd_amount * 100)
                         for r in rows if r.row_type == "meta_spend")
    vat_cents = sorted(round(r.sgd_amount * 100)
                       for r in rows if r.row_type == "meta_vat")
    print(f"  spend cents (sorted): {spend_cents}")
    print(f"  vat   cents (sorted): {vat_cents}")
    check("Synthetic: VAT rows untouched (3 x 0.02 cents)",
          vat_cents == [2, 2, 2],
          f"got {vat_cents}")
    check("Synthetic: spend rows are [15, 17, 17] after adjustment",
          spend_cents == [15, 17, 17],
          f"got {spend_cents}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    test_brand_lookup()
    test_helpers()
    test_meta_descriptions()
    test_split_tttw()
    test_split_pptw()
    test_penny_perfect_adjustment()

    section("Result")
    print(f"  PASSED: {len(PASSED)}")
    print(f"  FAILED: {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nPhase 4 splitter test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
