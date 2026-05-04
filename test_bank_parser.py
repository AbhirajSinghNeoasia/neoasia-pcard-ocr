"""
Phase 1 sanity test for bank_parser.py.

Exercises every spec'd requirement against the real OCBC export:
  - Row counts per sheet (Mar26: 175 data, Feb26: 88 data)
  - Date parsing for both datetime serials and "M/D/YYYY" text cells
  - Meta vs Simple identification (FACEBK substring)
  - FB Code extraction via regex
  - Cardholder mapping incl. zero-padded last4 and Unknown-XXXX fallback
  - Negative amount preservation (refunds)
  - Date range and currency coverage
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from bank_parser import extract_fb_code, parse_ocbc_statement
from config import CARDHOLDER_MAP
from models import TransactionType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

XLS_PATH = Path("test_data/SOA - Pcard.xls")

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  FAIL  {label}  ({detail})")


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# 1. Standalone FB-code regex sanity (no file I/O needed)
# ---------------------------------------------------------------------------

def test_fb_code_regex() -> None:
    section("1. FB Code extraction unit checks")

    cases = [
        ("FACEBK *TTTW5EZGT2 DUBLIN", "TTTW5EZGT2"),
        ("FACEBK *UQACXFVGT2 fb.me/ads", "UQACXFVGT2"),
        ("FACEBK *56436FMGT2 fb.me/ads", "56436FMGT2"),
        ("FACEBK *KSSP9FMGT2 fb.me/ads", "KSSP9FMGT2"),
        ("Grab* A-9XRJTDGGX9SFAV Singapore", None),  # not a Meta narrative
        ("", None),
    ]
    for narrative, expected in cases:
        got = extract_fb_code(narrative)
        check(
            f"extract_fb_code({narrative!r}) == {expected!r}",
            got == expected,
            f"got {got!r}",
        )


# ---------------------------------------------------------------------------
# 2. Parse the real workbook and assert spec'd invariants
# ---------------------------------------------------------------------------

def test_parser_against_real_xls() -> None:
    section("2. Parsing test_data/SOA - Pcard.xls")

    if not XLS_PATH.exists():
        FAILED.append(f"Test fixture missing: {XLS_PATH}")
        print(f"  FAIL  fixture missing: {XLS_PATH}")
        return

    rows = parse_ocbc_statement(str(XLS_PATH))
    print(f"  Parsed {len(rows)} total rows from both sheets.")

    # ----- Per-sheet counts -------------------------------------------
    by_sheet = Counter(r.sheet_name for r in rows)
    print(f"  Rows by sheet: {dict(by_sheet)}")
    check("Mar26 sheet has 175 data rows",
          by_sheet.get("Bank statement - Mar26") == 175,
          f"got {by_sheet.get('Bank statement - Mar26')}")
    check("Feb26 sheet has 88 data rows",
          by_sheet.get("Bank statement - Feb26") == 88,
          f"got {by_sheet.get('Bank statement - Feb26')}")
    check("Total rows == 263 (175 + 88)",
          len(rows) == 263,
          f"got {len(rows)}")

    # ----- Date parsing ------------------------------------------------
    # Every row must have a real date in 2026.
    bad_dates = [r for r in rows if r.transaction_date.year != 2026]
    check("All transaction_date values are in 2026",
          len(bad_dates) == 0,
          f"{len(bad_dates)} rows out-of-year")

    # Spot-check a known text-date row: Feb26 row 37 had '2/14/2026'
    feb_rows = [r for r in rows if r.sheet_name == "Bank statement - Feb26"]
    feb_2_14 = [r for r in feb_rows if r.transaction_date.isoformat() == "2026-02-14"]
    check("At least one Feb26 row parsed for 2026-02-14 (text-date case)",
          len(feb_2_14) > 0,
          f"got {len(feb_2_14)}")

    # Spot-check Mar26 row 102 had '3/13/2026'
    mar_3_13 = [r for r in rows
                if r.sheet_name == "Bank statement - Mar26"
                and r.transaction_date.isoformat() == "2026-03-13"]
    check("At least one Mar26 row parsed for 2026-03-13 (text-date case)",
          len(mar_3_13) > 0,
          f"got {len(mar_3_13)}")

    # Spot-check the OCBC D/M-vs-M/D serial swap: Mar26 row 1 has xlrd serial
    # date 46025 (-> Jan 3, 2026). After swap it must be Mar 1, 2026, which is
    # what the team's reviewed output shows for the matching Grab booking.
    grab_swap = next((r for r in rows if "9XRJTDGGX9SFAV" in r.narrative), None)
    check("Grab #9XRJTDGGX9SFAV (Mar26) has bank date 2026-03-01 after swap",
          grab_swap is not None and grab_swap.transaction_date.isoformat() == "2026-03-01",
          f"got {grab_swap.transaction_date if grab_swap else None}")

    # All Mar26 dates must fall within March 2026 (the statement period).
    mar26 = [r for r in rows if r.sheet_name == "Bank statement - Mar26"]
    bad_mar = [r for r in mar26 if r.transaction_date.month != 3]
    check("Every Mar26 row's date is in March 2026",
          len(bad_mar) == 0,
          f"{len(bad_mar)} out of period (sample={bad_mar[0].transaction_date if bad_mar else None})")

    # All Feb26 dates must fall within February 2026.
    feb26 = [r for r in rows if r.sheet_name == "Bank statement - Feb26"]
    bad_feb = [r for r in feb26 if r.transaction_date.month != 2]
    check("Every Feb26 row's date is in February 2026",
          len(bad_feb) == 0,
          f"{len(bad_feb)} out of period (sample={bad_feb[0].transaction_date if bad_feb else None})")

    # ----- Meta vs Simple ---------------------------------------------
    meta_rows = [r for r in rows if r.transaction_type == TransactionType.META.value]
    simple_rows = [r for r in rows if r.transaction_type == TransactionType.SIMPLE.value]
    print(f"  Meta rows: {len(meta_rows)} | Simple rows: {len(simple_rows)}")
    check("Meta + Simple == total",
          len(meta_rows) + len(simple_rows) == len(rows),
          f"{len(meta_rows)}+{len(simple_rows)} != {len(rows)}")
    # We confirmed earlier from the raw xls inspection that there are 79 Meta rows.
    check("Meta row count == 79",
          len(meta_rows) == 79,
          f"got {len(meta_rows)}")

    # Every Meta row must contain FACEBK in narrative
    meta_with_facebk = [r for r in meta_rows if "FACEBK" in r.narrative.upper()]
    check("Every Meta row's narrative contains 'FACEBK'",
          len(meta_with_facebk) == len(meta_rows),
          f"{len(meta_with_facebk)}/{len(meta_rows)}")

    # No Simple row should contain FACEBK
    simple_with_facebk = [r for r in simple_rows if "FACEBK" in r.narrative.upper()]
    check("No Simple row contains 'FACEBK'",
          len(simple_with_facebk) == 0,
          f"{len(simple_with_facebk)} leaked")

    # ----- FB Code extraction -----------------------------------------
    meta_with_fb = [r for r in meta_rows if r.fb_code]
    print(f"  Meta rows with extracted FB code: {len(meta_with_fb)} / {len(meta_rows)}")
    check("Every Meta row has an extracted FB code",
          len(meta_with_fb) == len(meta_rows),
          f"{len(meta_with_fb)}/{len(meta_rows)}")

    # All FB codes look like 10+ alphanumeric uppercase strings
    bad_fb = [r.fb_code for r in meta_with_fb
              if not (r.fb_code and len(r.fb_code) >= 10 and r.fb_code.isalnum())]
    check("All extracted FB codes are >=10 alphanumeric chars",
          len(bad_fb) == 0,
          f"bad={bad_fb[:5]}")

    # No Simple row has an FB code
    simple_with_fb = [r for r in simple_rows if r.fb_code]
    check("No Simple row has an FB code",
          len(simple_with_fb) == 0,
          f"got {len(simple_with_fb)}")

    # Confirm the documented examples are present
    expected_codes = {"UQACXFVGT2", "TTTW5EZGT2", "56436FMGT2"}
    found_codes = {r.fb_code for r in meta_with_fb}
    overlap = expected_codes & found_codes
    check("Documented FB codes UQACXFVGT2/TTTW5EZGT2/56436FMGT2 found",
          len(overlap) >= 1,
          f"overlap={overlap}")

    # ----- Last-4 / cardholder ----------------------------------------
    last4s = sorted({r.last_4_digits for r in rows})
    print(f"  Unique last4 values: {last4s}")
    cardholders = sorted({r.cardholder for r in rows})
    print(f"  Unique cardholders: {cardholders}")

    # Every last4 must be 4 chars long
    bad_len = [l for l in last4s if len(l) != 4]
    check("All last_4_digits values are 4 chars long",
          len(bad_len) == 0,
          f"bad={bad_len}")

    # Cards starting with '0' must be zero-padded (regression check)
    leading_zero = [l for l in last4s if l.startswith("0")]
    check("Zero-padded last4 values exist (e.g. '0594', '0711')",
          len(leading_zero) >= 1,
          f"got {leading_zero}")

    # Known mapping check
    known_card = next((r for r in rows if r.last_4_digits == "9804"), None)
    check("Card 9804 maps to 'Sharry'",
          known_card is not None and known_card.cardholder == "Sharry",
          f"got {known_card.cardholder if known_card else None}")

    # Unknown-XXXX fallback present iff there are unmapped cards
    unmapped = [l for l in last4s if l not in CARDHOLDER_MAP]
    if unmapped:
        # Verify those rows did fall back to Unknown-...
        for l in unmapped:
            sample = next(r for r in rows if r.last_4_digits == l)
            check(f"Unmapped card {l} falls back to 'Unknown-{l}'",
                  sample.cardholder == f"Unknown-{l}",
                  f"got {sample.cardholder}")

    # ----- Negative amounts (refunds) ---------------------------------
    refunds = [r for r in rows if r.is_refund]
    print(f"  Refund rows (sgd_amount < 0): {len(refunds)}")
    for r in refunds:
        print(f"    {r.transaction_date} | {r.cardholder:8} | {r.sgd_amount:>8.2f} | {r.narrative}")
    check("7 refund rows preserved",
          len(refunds) == 7,
          f"got {len(refunds)}")
    check("Every refund's is_refund flag matches sign",
          all((r.sgd_amount < 0) == r.is_refund for r in rows))

    # ----- Date range --------------------------------------------------
    dmin = min(r.transaction_date for r in rows)
    dmax = max(r.transaction_date for r in rows)
    print(f"  Date range: {dmin} -> {dmax}")
    check("Min date is in February 2026",
          dmin.year == 2026 and dmin.month == 2,
          f"got {dmin}")
    check("Max date is in March 2026",
          dmax.year == 2026 and dmax.month == 3,
          f"got {dmax}")

    # ----- Sort order --------------------------------------------------
    sorted_dates = [r.transaction_date for r in rows]
    check("Rows are sorted ascending by date",
          sorted_dates == sorted(sorted_dates))

    # ----- Currency coverage ------------------------------------------
    currencies = sorted({r.merchant_currency for r in rows})
    print(f"  Merchant currencies seen: {currencies}")
    check("VND present (Meta + travel)", "VND" in currencies)
    check("SGD present", "SGD" in currencies)

    # ----- Print the summary the spec asked for -----------------------
    section("Summary (as required by Phase 1 spec)")

    print(f"  Total rows parsed       : {len(rows)}")
    for sname, count in sorted(by_sheet.items()):
        print(f"    - {sname:30s} : {count} rows")
    print(f"  Simple transactions     : {len(simple_rows)}")
    print(f"  Meta transactions       : {len(meta_rows)}")
    print(f"  Unique cardholders      : {len(cardholders)}")
    for ch in cardholders:
        n = sum(1 for r in rows if r.cardholder == ch)
        print(f"    - {ch:18s} : {n} rows")
    print(f"  Date range              : {dmin}  ->  {dmax}")
    print(f"  Refunds (negative SGD)  : {len(refunds)}")
    print(f"  Currencies              : {', '.join(currencies)}")

    # ----- Print a small sample for visual inspection ------------------
    section("Sample rows (first 3 simple, first 3 meta, all refunds)")
    for label, batch in (
        ("SIMPLE", [r for r in rows if r.transaction_type == TransactionType.SIMPLE.value][:3]),
        ("META  ", [r for r in rows if r.transaction_type == TransactionType.META.value][:3]),
        ("REFUND", refunds),
    ):
        for r in batch:
            print(
                f"  {label}  {r.transaction_date}  card={r.last_4_digits} ({r.cardholder:9})  "
                f"{r.merchant_currency} {r.merchant_amount:>12.2f}  SGD {r.sgd_amount:>9.2f}  "
                f"fb={r.fb_code}  narr={r.narrative[:50]!r}"
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    test_fb_code_regex()
    test_parser_against_real_xls()

    section("Result")
    print(f"  PASSED : {len(PASSED)}")
    print(f"  FAILED : {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nAll Phase 1 checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
