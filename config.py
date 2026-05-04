"""
Configuration constants for Neoasia P-Card OCR & Reconciliation Tool.

All lookups, mappings, branding, regex patterns and dropdown seeds live here so
the rest of the codebase stays declarative. Anything finance/operations may want
to tweak without touching engine code belongs in this module.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------

COMPANY_NAME: Final[str] = "Neoasia"
COMPANY_LEGAL_NAME: Final[str] = "Neoasia (S) Pte Ltd"
APP_TITLE: Final[str] = "Neoasia P-Card OCR & Reconciliation"

COLORS: Final[dict[str, str]] = {
    "primary_dark": "#004d71",
    "primary_gray": "#b1b1b1",
    "secondary_blue": "#66a2c5",
    "secondary_light_blue": "#cde4f5",
    "very_light_blue": "#e7eff8",
    "light_gray": "#eaeaea",
    "white": "#ffffff",
    "text": "#333333",
    "background": "#f6f8fb",
}

EXCEL_FONT: Final[str] = "Calibri"

# ---------------------------------------------------------------------------
# Bank statement format / regex
# ---------------------------------------------------------------------------

# OCBC P-Card export column layout (zero-indexed, header on row 0).
OCBC_COLUMNS: Final[dict[str, int]] = {
    "id": 0,
    "transaction_date": 1,
    "company_name": 2,
    "narrative": 3,
    "last_4_digits": 4,
    "merchant_currency_amount": 5,
    "merchant_currency": 6,
    "transaction_amount": 7,
    "transaction_currency": 8,
    "unreconciled_amount": 9,
    "currency": 10,
}

# A row is a Meta/Facebook ad spend if its narrative contains this keyword
# (case-insensitive substring match).
META_NARRATIVE_KEYWORD: Final[str] = "FACEBK"

# FB reference codes are 10+ uppercase-alphanumeric tokens that follow
# "FACEBK *". We keep the regex liberal (\w{10,}) per CLAUDE.md spec but the
# observed format is exactly 10 chars, e.g. TTTW5EZGT2, UQACXFVGT2, 56436FMGT2.
FB_CODE_REGEX: Final[re.Pattern[str]] = re.compile(
    r"FACEBK\s*\*?\s*(\w{10,})", re.IGNORECASE
)

# Date string format observed in the OCBC export (Mar26 sheet rows 102+, Feb26
# rows 37+). Two-pass parsing in bank_parser.py falls back to this format when
# xlrd reports a TEXT cell instead of a DATE cell.
TEXT_DATE_FORMATS: Final[tuple[str, ...]] = (
    "%m/%d/%Y",   # 3/13/2026
    "%d/%m/%Y",   # defensive — in case locale flips
    "%Y-%m-%d",
)

# ---------------------------------------------------------------------------
# Cardholder mapping
# ---------------------------------------------------------------------------
# Maps last-4-digits of card to cardholder name. Unknown cards fall back to
# "Unknown-{last4}" so the row is still processable.

CARDHOLDER_MAP: Final[dict[str, str]] = {
    "9804": "Sharry",
    "1099": "Sharry",     # Also used by Sharry for flights
    "9415": "KC",
    "0711": "KC",
    "9794": "Joey",       # Tiket.com bookings
    "9671": "Jaslyn",
    "8913": "Jaslyn",
    "0594": "Joey",       # Blue Bird taxi
    "9622": "Joey",
    "5051": "Joey",
    "2099": "Sharry",
    "3869": "Sharry",
    "1396": "Jaslyn",
    "0745": "Sharry",
    "2673": "Jaslyn",
    "0690": "Sharry",
}

UNKNOWN_CARDHOLDER_PREFIX: Final[str] = "Unknown-"


def lookup_cardholder(last4: str) -> str:
    """Return cardholder name for a last-4-digits string, or Unknown-XXXX."""
    return CARDHOLDER_MAP.get(last4, f"{UNKNOWN_CARDHOLDER_PREFIX}{last4}")


# ---------------------------------------------------------------------------
# Brand keyword mapping (Meta ad set names → SAP dimensions)
# ---------------------------------------------------------------------------

BRAND_KEYWORD_MAP: Final[dict[str, dict[str, str]]] = {
    "calecim":   {"brand": "CAL",    "country": "VN", "division": "MED-I",  "team": "T1"},
    "heliocare": {"brand": "HEL",    "country": "VN", "division": "MED-I",  "team": "T1"},
    "profhilo":  {"brand": "PRO",    "country": "VN", "division": "MED-II", "team": "T1"},
    "revalene":  {"brand": "REV",    "country": "VN", "division": "MED-I",  "team": "T1"},
    "nourkrin":  {"brand": "NOU",    "country": "VN", "division": "MED-I",  "team": "T1"},
    "sessions":  {"brand": "0_DIM2", "country": "VN", "division": "MED-I",  "team": "T1"},
}

DEFAULT_BRAND_MAP: Final[dict[str, str]] = {
    "brand": "0_DIM2", "country": "VN", "division": "MED-I", "team": "T1",
}

# ---------------------------------------------------------------------------
# GL account suggestions (vendor keyword → GL code & name)
# ---------------------------------------------------------------------------

GL_VENDOR_MAP: Final[dict[str, tuple[str, str]]] = {
    "grab":              ("6312204", "Travelling - Sales Staff"),
    "scoot":             ("6312204", "Travelling - Sales Staff"),
    "flyscoot":          ("6312204", "Travelling - Sales Staff"),
    "singapore air":     ("6312204", "Travelling - Sales Staff"),
    "tiket.com":         ("6312204", "Travelling - Sales Staff"),
    "novotel":           ("6312204", "Travelling - Sales Staff"),
    "mercure":           ("6312204", "Travelling - Sales Staff"),
    "shopee":            ("6311501", "Printing and stationery"),
    "taobao":            ("6210104", "Promotional expense"),
    "malaysia airlines": ("6312204", "Travelling - Sales Staff"),
    "blue bird":         ("6312204", "Travelling - Sales Staff"),
    "mom*":              ("6110116", "Staff welfare"),
}

GL_META_AD: Final[tuple[str, str]] = ("6210101", "Advertisement")
GL_META_VAT: Final[tuple[str, str]] = ("6312701", "VAT expenses")

# ---------------------------------------------------------------------------
# Dropdown seeds for the generated Excel workbook
# ---------------------------------------------------------------------------

DROPDOWN_SEED: Final[dict[str, list[str]]] = {
    "GL_Account": [
        "6312204", "6210101", "6312701", "6210104",
        "6311001", "6311501", "6110116", "1130104", "1130105",
    ],
    "Brand":    ["0_DIM2", "CAL", "HEL", "PRO", "NOU", "REV", "GTP", "ACM", "NST"],
    "Country":  ["SG", "VN", "MY", "ID", "PH", "HK", "Shared"],
    "Division": ["0_DIM4", "CORP", "MED-I", "MED-II", "MED-III", "OMNI"],
    "Team":     ["0_DIM5", "T1", "T2", "T3", "T4", "OT"],
    "Tax_Code": ["", "ZP", "TX7", "OP"],
}

# ---------------------------------------------------------------------------
# Vendor signatures used by the matching engine
# ---------------------------------------------------------------------------

VENDOR_SIGNATURES: Final[dict[str, list[str]]] = {
    "Grab":              ["grab"],
    "Shopee":            ["shopee"],
    "Scoot":             ["scoot", "flyscoot"],
    "Singapore Airlines": ["singapore air", "sq "],
    "Taobao":            ["taobao"],
    "Tiket.com":         ["tiket"],
    "Trip.com":          ["trip.com"],
    "Novotel":           ["novotel"],
    "Mercure":           ["mercure"],
    "Malaysia Airlines": ["malaysia airlines", "malaysiaair"],
    "Blue Bird":         ["blue bird", "bluebird"],
    "Facebook":          ["facebk", "facebook", "meta"],
}

# ---------------------------------------------------------------------------
# Misc behaviour knobs
# ---------------------------------------------------------------------------

VAT_RATE_DEFAULT: Final[float] = 0.10  # 10% Vietnam VAT on Meta invoices
PDF_RENDER_DPI: Final[int] = 200
IMAGE_MAX_DIMENSION: Final[int] = 3000
CLAUDE_MODEL: Final[str] = "claude-sonnet-4-6"   # NO date suffix
CLAUDE_MAX_TOKENS: Final[int] = 8192
OCR_RETRY_COUNT: Final[int] = 3
OCR_RETRY_BACKOFF_SECONDS: Final[tuple[int, ...]] = (2, 4, 8)
