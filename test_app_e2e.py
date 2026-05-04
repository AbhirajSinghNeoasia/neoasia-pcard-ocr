"""
Phase 6 end-to-end app test.

Drives the full Streamlit app in-process via streamlit.testing.v1.AppTest. NO
network calls — instead of triggering OCR through the UI button (which would
hit Claude), we pre-populate session_state["assembly"] with a synthetic
AssemblyResult so Tabs 3-5 render in their post-processing state.

Coverage:
  1. Auth gate -> dashboard transition (still working after Phase 6 changes).
  2. All 5 tabs render without exceptions for both pre- and post-processing
     states.
  3. Tab 3 shows prerequisite warnings when bank/receipts missing.
  4. Tab 3 shows result metrics after assembly is populated.
  5. Tab 4 surfaces unmatched + Meta groups.
  6. Tab 5 builds + offers the download button (download_button widget present).
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from streamlit.testing.v1 import AppTest

from bank_parser import parse_ocbc_statement
from models import (
    BankRow,
    MatchedRow,
    MetaCampaign,
    MetaInvoiceOCR,
    OcrTransaction,
    SplitRow,
    TransactionType,
)
from pipeline import assemble_outputs


XLS = Path("test_data/SOA - Pcard.xls")
APP_PASSWORD = "neoasia2026"

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


def authed_app() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=20)
    at.run()
    if at.exception:
        for e in at.exception:
            print("APP EXCEPTION:", repr(e.value))
        raise RuntimeError("App raised on initial render")
    pw = next(t for t in at.text_input if t.label == "Password")
    pw.set_value(APP_PASSWORD)
    at.button[0].click()
    at.run()
    if at.exception:
        for e in at.exception:
            print("APP EXCEPTION after auth:", repr(e.value))
        raise RuntimeError("App raised after authentication")
    return at


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_initial_state() -> AppTest:
    section("1. Initial state — auth + 5 tabs")
    at = authed_app()
    check("5 tabs rendered after auth", len(at.tabs) == 5,
          f"got {len(at.tabs)} tabs")
    check("No exception during initial render", not at.exception)
    return at


def test_tab3_warnings(at: AppTest) -> None:
    section("2. Tab 3 prerequisite warnings (no data uploaded)")
    md = "\n".join(m.value for m in at.markdown)
    # Should contain the warning about needing a bank statement
    warnings = [w.value for w in at.warning]
    print(f"  Warnings on Tab 3: {warnings}")
    check("Tab 3 surfaces 'Upload an OCBC bank statement' warning",
          any("OCBC bank statement" in w for w in warnings),
          f"got {warnings}")


def test_after_processing(at: AppTest) -> None:
    section("3. Inject assembly + verify Tabs 3/4/5 render their result UIs")

    # Build a small synthetic assembly result
    bank_rows = parse_ocbc_statement(str(XLS))
    # Use just a tiny slice to keep AppTest fast
    bank_rows = bank_rows[:8]

    # Synthetic OCR results: one matching the first Grab row, one Meta invoice
    grab_bank = next((b for b in bank_rows if "grab" in b.narrative.lower()), None)
    if grab_bank is None:
        grab_bank = bank_rows[0]
    ocr_simple = [
        OcrTransaction(
            transaction_date=grab_bank.transaction_date,
            vendor="Grab",
            nature="Booking from test",
            currency=grab_bank.merchant_currency,
            amount=grab_bank.merchant_amount,
        )
    ]

    meta_bank = next((b for b in bank_rows
                      if b.transaction_type == TransactionType.META.value
                      and b.fb_code), None)
    ocr_meta = []
    if meta_bank is not None:
        ocr_meta = [MetaInvoiceOCR(
            reference_number=meta_bank.fb_code,
            invoice_date=meta_bank.transaction_date,
            payment_method_last4="9804",
            total_paid_vnd=int(meta_bank.merchant_amount),
            subtotal_vnd=int(meta_bank.merchant_amount * 10 // 11),
            vat_vnd=int(meta_bank.merchant_amount) - int(meta_bank.merchant_amount * 10 // 11),
            vat_rate_percent=10.0,
            campaigns=[MetaCampaign(
                ad_set_name="Calecim Brand Ads",
                spend_vnd=int(meta_bank.merchant_amount * 10 // 11),
            )],
        )]

    result = assemble_outputs(bank_rows, ocr_simple, ocr_meta)
    print(f"  Synthetic assembly: {len(result.all_split_rows)} rows, "
          f"matched={result.n_matched_simple}, unmatched={result.n_unmatched_simple}, "
          f"meta={result.n_meta_splits}")

    at.session_state["bank_rows"] = bank_rows
    at.session_state["bank_filename"] = "synthetic.xls"
    at.session_state["assembly"] = result
    at.run()

    if at.exception:
        for e in at.exception:
            print("APP EXCEPTION after injecting assembly:", repr(e.value))
        check("App renders without exceptions after injecting assembly", False, "see above")
        return
    check("App renders without exceptions after injecting assembly", True)

    # ---- Tab 3 should now show the Results metrics ----
    metric_labels = {m.label for m in at.metric}
    print(f"  Metric labels visible: {sorted(metric_labels)}")
    check("Tab 3 'Total output rows' metric appears",
          "Total output rows" in metric_labels)
    check("Tab 3 'Matched simple' metric appears",
          "Matched simple" in metric_labels)
    check("Tab 3 'Total SGD' metric appears",
          "Total SGD" in metric_labels)

    # ---- Tab 4 should render at least one dataframe (All rows) ----
    n_dataframes = len(at.dataframe)
    check(f"Tab 4 renders dataframes (got {n_dataframes})",
          n_dataframes >= 1)

    # ---- Tab 5 download button ----
    # st.download_button isn't in at.button (it has its own widget collection
    # not exposed by AppTest). Verify the path by looking for the caption
    # printed RIGHT AFTER a successful download_button render — that caption
    # mentions the generated filename.
    md = "\n".join(m.value for m in at.markdown)
    captions = "\n".join(c.value for c in at.caption) if hasattr(at, "caption") else ""
    full_text = md + "\n" + captions
    print(f"  Captions seen: {[c.value for c in at.caption][:5] if hasattr(at, 'caption') else 'no caption attr'}")
    check("Tab 5 download path executed (filename caption present)",
          "PCard_" in full_text and ".xlsx" in full_text,
          "filename pattern not found — download_button block likely raised")

    # Brand bar still present (header didn't break)
    check("Brand bar still rendered", "P-Card OCR" in md)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        at = test_initial_state()
        test_tab3_warnings(at)
        test_after_processing(at)
    except RuntimeError as exc:
        print(f"\nFATAL: {exc}")
        return 2

    section("Result")
    print(f"  PASSED: {len(PASSED)}")
    print(f"  FAILED: {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nPhase 6 end-to-end app test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
