"""
Phase 3 OCR test — runs ONE real Claude API call against ONE Meta invoice.

The user has explicitly asked us to minimise API usage during testing, so this
script:
  - Calls Claude exactly once (the Meta invoice OCR for TTTW5EZGT2).
  - Prints the full extracted JSON for review.
  - Asserts the documented invariants:
      reference_number == "TTTW5EZGT2"
      subtotal_vnd     == 175007
      vat_vnd          == 17501
      total_paid_vnd   == 192508
      vat_rate_percent ≈ 10
      3 ad sets present, with Calecim ~3568 / Heliocare ~70042 / Profhilo ~101397
      sum(campaigns.spend_vnd) == subtotal_vnd
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path


# ---------------------------------------------------------------------------
# Bootstrap: load the API key from .streamlit/secrets.toml into the env
# BEFORE importing the engine (which reads ANTHROPIC_API_KEY at first call).
# ---------------------------------------------------------------------------

SECRETS_PATH = Path(".streamlit/secrets.toml")
if not SECRETS_PATH.exists():
    print(f"FATAL: {SECRETS_PATH} missing. Cannot run OCR test.", file=sys.stderr)
    sys.exit(2)

with open(SECRETS_PATH, "rb") as f:
    _secrets = tomllib.load(f)

_api_key = _secrets.get("ANTHROPIC_API_KEY", "")
if not _api_key or _api_key.startswith("sk-ant-placeholder"):
    print(f"FATAL: ANTHROPIC_API_KEY in {SECRETS_PATH} is missing or placeholder.",
          file=sys.stderr)
    sys.exit(2)
os.environ["ANTHROPIC_API_KEY"] = _api_key

from ocr_engine import ocr_meta_invoice  # noqa: E402 — env must be set first


# ---------------------------------------------------------------------------
# Test fixture
# ---------------------------------------------------------------------------

# Picked deterministically by transaction-ID prefix per the user's brief.
META_DIR = Path("test_data/meta_invoices")
TX_PREFIX = "25915253954831097"  # TTTW5EZGT2 invoice — Feb 14 2026


def find_target_pdf() -> Path:
    matches = sorted(META_DIR.glob(f"*{TX_PREFIX}*.pdf"))
    if not matches:
        raise FileNotFoundError(
            f"No PDF matching transaction prefix {TX_PREFIX} in {META_DIR}. "
            f"Did you extract Meta Invoices.zip?"
        )
    if len(matches) > 1:
        print(f"Note: multiple PDFs matched {TX_PREFIX}, using {matches[0].name}",
              file=sys.stderr)
    return matches[0]


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(f"{label} :: {detail}")
        print(f"  FAIL  {label}  ({detail})")


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def find_campaign(invoice, keyword: str):
    """Return the first campaign whose ad_set_name contains the keyword (CI)."""
    kl = keyword.lower()
    for c in invoice.campaigns:
        if kl in c.ad_set_name.lower():
            return c
    return None


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        pdf_path = find_target_pdf()
    except FileNotFoundError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    section(f"OCR target: {pdf_path.name}")
    print(f"  size: {pdf_path.stat().st_size / 1024:.1f} KB")

    section("Calling Claude (one API call)…")
    try:
        invoice = ocr_meta_invoice(str(pdf_path))
    except Exception as exc:  # surface the actual error
        print(f"\nOCR call raised: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    section("Extracted JSON")
    payload = invoice.model_dump(mode="json")
    print(json.dumps(payload, indent=2, ensure_ascii=False))

    # --- Invariants per spec ----------------------------------------------
    section("Assertions")

    check("reference_number == 'TTTW5EZGT2'",
          invoice.reference_number == "TTTW5EZGT2",
          f"got {invoice.reference_number!r}")
    check("subtotal_vnd == 175007",
          int(invoice.subtotal_vnd) == 175007,
          f"got {invoice.subtotal_vnd}")
    check("vat_vnd == 17501",
          int(invoice.vat_vnd) == 17501,
          f"got {invoice.vat_vnd}")
    check("total_paid_vnd == 192508",
          int(invoice.total_paid_vnd) == 192508,
          f"got {invoice.total_paid_vnd}")
    check("vat_rate_percent in [9.5, 10.5]",
          9.5 <= float(invoice.vat_rate_percent) <= 10.5,
          f"got {invoice.vat_rate_percent}")

    # subtotal + vat = total identity
    expected_total = invoice.subtotal_vnd + invoice.vat_vnd
    check("subtotal + VAT == total_paid (within 1 VND)",
          abs(expected_total - invoice.total_paid_vnd) <= 1,
          f"{expected_total} vs {invoice.total_paid_vnd}")

    # Campaigns
    check("Exactly 3 ad sets extracted",
          len(invoice.campaigns) == 3,
          f"got {len(invoice.campaigns)}: {[c.ad_set_name for c in invoice.campaigns]}")

    calecim = find_campaign(invoice, "calecim")
    check("Calecim ad set extracted with spend == 3568",
          calecim is not None and int(calecim.spend_vnd) == 3568,
          f"got {calecim}")

    heliocare = find_campaign(invoice, "heliocare")
    check("Heliocare ad set extracted with spend == 70042",
          heliocare is not None and int(heliocare.spend_vnd) == 70042,
          f"got {heliocare}")

    profhilo = find_campaign(invoice, "profhilo")
    check("Profhilo ad set extracted with spend == 101397",
          profhilo is not None and int(profhilo.spend_vnd) == 101397,
          f"got {profhilo}")

    # Sum-of-campaigns identity
    csum = sum(c.spend_vnd for c in invoice.campaigns)
    check("sum(campaigns.spend_vnd) == subtotal_vnd (within 1 VND)",
          abs(csum - invoice.subtotal_vnd) <= 1,
          f"sum={csum} subtotal={invoice.subtotal_vnd}")

    # No campaign group leaked through (e.g. "MED I - Feb")
    leaked = [c.ad_set_name for c in invoice.campaigns
              if c.ad_set_name.strip().lower() in {"med i - feb", "profhilo 64mg 28.02.26 webinar"}]
    check("No campaign group header leaked into campaigns",
          len(leaked) == 0,
          f"leaked={leaked}")

    section("Result")
    print(f"  PASSED: {len(PASSED)}")
    print(f"  FAILED: {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nPhase 3 OCR test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
