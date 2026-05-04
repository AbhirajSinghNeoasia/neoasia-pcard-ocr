"""
Phase 6 pipeline tests — assemble_outputs, helpers, manual override.

NO API CALLS. Synthesises BankRow + OcrTransaction + MetaInvoiceOCR fixtures
and exercises every assembly path:

  1. matched_to_split_row maps a matched simple row correctly
  2. derive_period_string covers single-month / multi-month / cross-year
  3. parse_brand_override_csv accepts well-formed input, rejects malformed
  4. assemble_outputs end-to-end:
       - matched simple -> SplitRow with GL suggestion + description
       - unmatched simple -> SplitRow with no GL, status='Unmatched'
       - Meta bank row + matching invoice -> 6 split rows summing to bank_sgd
       - Orphan Meta bank row -> placeholder SplitRow (no invoice)
       - Orphan invoice -> flagged in orphan_invoice_codes (not in output)
  5. brand_override CSV overrides the default mapping for one campaign
  6. manual_matches override an auto-unmatched simple row
  7. _sort_and_renumber stable-sorts by date and assigns sequential lines
"""

from __future__ import annotations

import io
import sys
from datetime import date
from decimal import Decimal

from models import (
    BankRow,
    MetaCampaign,
    MetaInvoiceOCR,
    OcrTransaction,
    TransactionType,
)
from pipeline import (
    apply_manual_overrides,
    assemble_outputs,
    derive_period_string,
    matched_to_split_row,
    parse_brand_override_csv,
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


def cents(rows) -> int:
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r.sgd_amount))
    return int((total * 100).to_integral_value())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bank(idx, *, simple=True, **kw) -> BankRow:
    base = dict(
        row_index=idx,
        sheet_name="Bank statement - Feb26",
        transaction_id=f"TX{idx}",
        transaction_date=date(2026, 2, 14),
        narrative="Test narrative",
        last_4_digits="9804",
        cardholder="Sharry",
        merchant_amount=42.09,
        merchant_currency="SGD",
        sgd_amount=42.09,
        transaction_type=TransactionType.SIMPLE if simple else TransactionType.META,
        fb_code=None,
        is_refund=False,
    )
    base.update(kw)
    return BankRow(**base)


def _ocr_tx(**kw) -> OcrTransaction:
    base = dict(
        transaction_date=date(2026, 2, 14),
        vendor="Grab",
        nature="Booking#A-XYZ",
        currency="SGD",
        amount=42.09,
    )
    base.update(kw)
    return OcrTransaction(**base)


def _ocr_meta(ref: str, campaigns: list[tuple[str, int]], total: int = 192508,
              subtotal: int = 175007, vat: int = 17501) -> MetaInvoiceOCR:
    return MetaInvoiceOCR(
        reference_number=ref,
        invoice_date=date(2026, 2, 14),
        payment_method_last4="9804",
        total_paid_vnd=total,
        subtotal_vnd=subtotal,
        vat_vnd=vat,
        vat_rate_percent=10.0,
        campaigns=[MetaCampaign(ad_set_name=n, spend_vnd=v) for n, v in campaigns],
        source_file=f"{ref}.pdf",
    )


# ---------------------------------------------------------------------------
# 1. matched_to_split_row
# ---------------------------------------------------------------------------


def test_matched_to_split_row() -> None:
    section("1. matched_to_split_row")
    from models import MatchedRow
    bank = _bank(1, narrative="Grab* A-XYZ Singapore")
    ocr = _ocr_tx()
    sr = matched_to_split_row(MatchedRow(bank=bank, ocr=ocr,
                                         match_confidence="exact",
                                         match_reason="..."), 7)
    check("Returns SplitRow with line_number=7", sr.line_number == 7)
    check("GL suggested for Grab vendor",
          sr.gl_account == "6312204" and sr.gl_account_name == "Travelling - Sales Staff")
    check("Description starts with 'OCBC: PCard - Sharry - Grab'",
          sr.description.startswith("OCBC: PCard - Sharry - Grab"),
          f"got {sr.description!r}")
    check("match_status == 'Matched'", sr.match_status == "Matched")
    check("row_type == 'simple'", sr.row_type == "simple")
    check("sgd_amount preserved", sr.sgd_amount == 42.09)

    # When unmatched, GL is still suggested from the bank narrative alone
    # (e.g. "Grab*..." narratives still hit the "grab" keyword). The
    # match_status flips to "Unmatched" but the GL hint is genuinely useful.
    sr_unmatched = matched_to_split_row(
        MatchedRow(bank=bank, ocr=None, match_confidence=None, match_reason=None), 1)
    check("Unmatched: GL still suggested from narrative ('Grab*...' -> 6312204)",
          sr_unmatched.gl_account == "6312204",
          f"got gl={sr_unmatched.gl_account}")
    check("Unmatched: match_status == 'Unmatched'",
          sr_unmatched.match_status == "Unmatched")

    # Genuinely unrecognisable narrative -> no GL suggestion either
    bank_obscure = _bank(2, narrative="MYSTERY VENDOR XYZ")
    sr_obscure = matched_to_split_row(
        MatchedRow(bank=bank_obscure, ocr=None, match_confidence=None, match_reason=None), 1)
    check("Obscure narrative + no OCR -> gl_account is None",
          sr_obscure.gl_account is None,
          f"got gl={sr_obscure.gl_account}")


# ---------------------------------------------------------------------------
# 2. derive_period_string
# ---------------------------------------------------------------------------


def test_period_string() -> None:
    section("2. derive_period_string")
    rows_single = [_bank(1, transaction_date=date(2026, 2, 14)),
                   _bank(2, transaction_date=date(2026, 2, 28))]
    check("Single month: 'Feb-2026'",
          derive_period_string(rows_single) == "Feb-2026",
          f"got {derive_period_string(rows_single)!r}")

    rows_multi = [_bank(1, transaction_date=date(2026, 2, 14)),
                  _bank(2, transaction_date=date(2026, 3, 28))]
    check("Multi month, same year: 'Feb-Mar-2026'",
          derive_period_string(rows_multi) == "Feb-Mar-2026",
          f"got {derive_period_string(rows_multi)!r}")

    rows_cross = [_bank(1, transaction_date=date(2025, 12, 31)),
                  _bank(2, transaction_date=date(2026, 1, 5))]
    check("Cross-year: 'Dec2025-Jan2026'",
          derive_period_string(rows_cross) == "Dec2025-Jan2026",
          f"got {derive_period_string(rows_cross)!r}")

    check("Empty: 'Unknown'", derive_period_string([]) == "Unknown")


# ---------------------------------------------------------------------------
# 3. parse_brand_override_csv
# ---------------------------------------------------------------------------


def test_brand_override_csv() -> None:
    section("3. parse_brand_override_csv")

    good = io.StringIO(
        "keyword,brand,country,division,team\n"
        "calecim,CAL_OVERRIDE,SG,MED-X,T9\n"
        "newbrand,NEW,VN,MED-I,T1\n"
        ",IGNORED,IGNORED,IGNORED,IGNORED\n"  # empty keyword, dropped
    )
    parsed = parse_brand_override_csv(good)
    check("CSV parsed: 2 entries (empty keyword dropped)",
          len(parsed) == 2, f"got {parsed}")
    check("Calecim override has brand=CAL_OVERRIDE",
          parsed.get("calecim", {}).get("brand") == "CAL_OVERRIDE",
          f"got {parsed.get('calecim')}")
    check("New brand entry present",
          parsed.get("newbrand", {}).get("brand") == "NEW")

    # Malformed: missing required column
    bad = io.StringIO("keyword,brand,country\nfoo,X,Y\n")
    try:
        parse_brand_override_csv(bad)
        check("Missing column raises ValueError", False, "no exception raised")
    except ValueError as exc:
        check("Missing column raises ValueError", True)


# ---------------------------------------------------------------------------
# 4. assemble_outputs end-to-end
# ---------------------------------------------------------------------------


def test_assemble_e2e() -> None:
    section("4. assemble_outputs end-to-end")

    # 2 simple bank rows: one will match, one won't
    bank_simple_match = _bank(1, narrative="Grab* A-MATCH Singapore",
                              transaction_date=date(2026, 2, 14))
    bank_simple_orphan = _bank(2, narrative="Some weird vendor",
                               sgd_amount=999.99, transaction_date=date(2026, 2, 15))
    # 1 Meta bank row matched + 1 orphan (no invoice)
    bank_meta_match = _bank(3, simple=False, narrative="FACEBK *TTTW5EZGT2 DUBLIN",
                            sgd_amount=9.65, merchant_amount=192508.0,
                            merchant_currency="VND", fb_code="TTTW5EZGT2",
                            transaction_date=date(2026, 2, 14))
    bank_meta_orphan = _bank(4, simple=False, narrative="FACEBK *NOMATCHCD1 fb.me/ads",
                             sgd_amount=5.00, merchant_amount=100000.0,
                             merchant_currency="VND", fb_code="NOMATCHCD1",
                             transaction_date=date(2026, 2, 16))

    bank_rows = [bank_simple_match, bank_simple_orphan, bank_meta_match, bank_meta_orphan]

    # OCR results
    ocr_match = _ocr_tx(vendor="Grab", nature="Booking#A-MATCH",
                        currency="SGD", amount=42.09,
                        transaction_date=date(2026, 2, 14))
    ocr_simple = [ocr_match]

    # Meta invoices: one matching the bank row, one orphan invoice
    invoice_match = _ocr_meta("TTTW5EZGT2", campaigns=[
        ("Calecim Brand Ads", 3568),
        ("Heliocare Brands Campaign", 70042),
        ("64MG Profhilo Webinar 28 Feb", 101397),
    ])
    invoice_orphan = _ocr_meta("ORPHAN1234",
                                campaigns=[("Calecim Brand Ads", 100)],
                                total=110, subtotal=100, vat=10)
    ocr_meta = [invoice_match, invoice_orphan]

    result = assemble_outputs(bank_rows, ocr_simple, ocr_meta)

    # Counts
    check("matched_simple has 2 entries (1 matched + 1 unmatched)",
          len(result.matched_simple) == 2,
          f"got {len(result.matched_simple)}")
    check("n_matched_simple == 1", result.n_matched_simple == 1,
          f"got {result.n_matched_simple}")
    check("n_unmatched_simple == 1", result.n_unmatched_simple == 1,
          f"got {result.n_unmatched_simple}")
    # Meta: 6 from matched invoice + 1 placeholder for orphan = 7
    check("meta_split_rows has 7 entries (6 splits + 1 orphan placeholder)",
          len(result.meta_split_rows) == 7,
          f"got {len(result.meta_split_rows)}")

    # Orphans
    check("orphan_meta_codes contains NOMATCHCD1",
          "NOMATCHCD1" in result.orphan_meta_codes,
          f"got {result.orphan_meta_codes}")
    check("orphan_invoice_codes contains ORPHAN1234",
          "ORPHAN1234" in result.orphan_invoice_codes,
          f"got {result.orphan_invoice_codes}")

    # Penny-perfect check on the matched Meta transaction (6 split rows)
    tttw_rows = [r for r in result.meta_split_rows if "TTTW5EZGT2" in r.description]
    check("TTTW: 6 rows in meta_split_rows", len(tttw_rows) == 6,
          f"got {len(tttw_rows)}")
    check("TTTW: rows sum to 9.65 SGD exactly",
          cents(tttw_rows) == 965, f"got {cents(tttw_rows)} cents")

    # all_split_rows should contain everything (2 simple + 7 meta = 9)
    check("all_split_rows has 9 entries", len(result.all_split_rows) == 9,
          f"got {len(result.all_split_rows)}")

    # Sequential line numbers 1..9
    line_nums = [r.line_number for r in result.all_split_rows]
    check("all_split_rows line_numbers are sequential 1..9",
          line_nums == list(range(1, 10)),
          f"got {line_nums}")

    # Sorted by bank_date ascending (rows on the same date keep relative order)
    dates_ordered = [r.bank_date for r in result.all_split_rows]
    check("all_split_rows sorted by bank_date ascending",
          dates_ordered == sorted(dates_ordered),
          f"got {dates_ordered}")

    # Total SGD = simple matched (42.09) + simple unmatched (999.99) + meta matched (9.65) + meta orphan (5.00)
    expected_total = 42.09 + 999.99 + 9.65 + 5.00
    check(f"total_sgd == {expected_total}",
          abs(result.total_sgd - expected_total) < 0.005,
          f"got {result.total_sgd}")


# ---------------------------------------------------------------------------
# 5. Brand override actually overrides
# ---------------------------------------------------------------------------


def test_brand_override_in_assembly() -> None:
    section("5. brand_override flows through to Meta SplitRows")

    bank = _bank(1, simple=False, narrative="FACEBK *TTTW5EZGT2 DUBLIN",
                 sgd_amount=9.65, merchant_amount=192508.0,
                 merchant_currency="VND", fb_code="TTTW5EZGT2")
    invoice = _ocr_meta("TTTW5EZGT2", campaigns=[
        ("Calecim Brand Ads", 3568),
        ("Heliocare Brands Campaign", 70042),
        ("64MG Profhilo Webinar 28 Feb", 101397),
    ])
    override = {
        "calecim": {"brand": "CAL_OVERRIDE", "country": "SG", "division": "MED-X", "team": "T9"},
    }
    result = assemble_outputs([bank], [], [invoice], brand_override=override)
    spend_rows = [r for r in result.meta_split_rows if r.row_type == "meta_spend"]
    cal_row = next(r for r in spend_rows if "VND 3568" in r.description)
    check("Override brand applied to Calecim row",
          cal_row.brand == "CAL_OVERRIDE",
          f"got brand={cal_row.brand}")
    check("Override division applied",
          cal_row.division == "MED-X",
          f"got division={cal_row.division}")
    # Other brands should NOT be overridden (use defaults)
    hel_row = next(r for r in spend_rows if "VND 70042" in r.description)
    check("Heliocare row uses default mapping (not override)",
          hel_row.brand == "HEL", f"got {hel_row.brand}")


# ---------------------------------------------------------------------------
# 6. apply_manual_overrides
# ---------------------------------------------------------------------------


def test_manual_override() -> None:
    section("6. manual_matches re-pair a previously-unmatched simple row")
    from models import MatchedRow
    bank = _bank(1, narrative="Mystery Vendor narrative",
                 sgd_amount=10.00, transaction_date=date(2026, 2, 20))
    # OCR result that wouldn't auto-match (different vendor, different date,
    # but same SGD amount — in a different currency to defeat amount scoring)
    ocr = _ocr_tx(vendor="Manual Pick", nature="Custom assignment",
                  currency="USD", amount=999.99,
                  transaction_date=date(2026, 1, 1))

    # Auto-matcher leaves bank unmatched (score < 50)
    result = assemble_outputs([bank], [ocr], [])
    check("Bank initially unmatched by auto-matcher",
          result.matched_simple[0].ocr is None)

    # Now manually override: bank index 0 -> ocr index 0
    result2 = assemble_outputs([bank], [ocr], [], manual_matches={0: 0})
    check("After manual_matches, bank is matched",
          result2.matched_simple[0].ocr is ocr)
    check("Manual match has confidence='manual'",
          result2.matched_simple[0].match_confidence == "manual")
    # The simple_split_row should have status="Matched" and the manual OCR's vendor in description
    sr = result2.simple_split_rows[0]
    check("SplitRow reflects manual match: status='Matched'",
          sr.match_status == "Matched")
    check("SplitRow description includes manually-picked vendor",
          "Manual Pick" in sr.description,
          f"got {sr.description!r}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    test_matched_to_split_row()
    test_period_string()
    test_brand_override_csv()
    test_assemble_e2e()
    test_brand_override_in_assembly()
    test_manual_override()

    section("Result")
    print(f"  PASSED: {len(PASSED)}")
    print(f"  FAILED: {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nPhase 6 pipeline test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
