# EXECUTOR PROMPT — Neoasia P-Card OCR & Reconciliation Tool

**Role:** You are a Senior Full-Stack Python Developer building a production-grade Streamlit web application for Neoasia (S) Pte Ltd.

**Model Guidance:** Use your best judgment. Research Streamlit best practices if unsure. When you encounter ambiguity, make the smarter choice and document your decision. Do NOT ask me — just build.

---

## 1. PROJECT OVERVIEW

### What You're Building
A standalone web application that automates OCBC Purchase Card expense reconciliation. It:
1. Imports an OCBC P-Card bank statement (.xls)
2. Accepts uploaded receipt files (PDFs, images) for simple transactions
3. Accepts uploaded Meta/Facebook invoice PDFs for ad transactions
4. OCRs all documents using Claude Sonnet 4.6 Vision API
5. Auto-matches OCR-extracted data to bank statement rows
6. For Meta transactions: extracts ad campaign breakdowns from invoice PDFs, maps campaigns to SAP dimensions (Brand/Country/Division/Team), calculates proportional SGD splits with separate VAT rows
7. Auto-generates SAP-format descriptions for all rows
8. Generates a formatted Excel (.xlsx) SAP journal entry output
9. Has password-based authentication

### Who Uses This
- **Primary uploader:** A designated user (e.g., admin, or any cardholder) uploads the monthly bank statement + receipts + Meta invoice PDFs
- **Primary consumer:** The Finance/Accounts team downloads the generated Excel, reviews it, fills in remaining fields (GL Account, Brand/Country/Division/Team for simple transactions), and posts to SAP

### Sister App
This is a companion to the existing CC OCR tool (https://neoasia-cc-ocr.streamlit.app) which handles UOB credit card reconciliation. The P-Card tool follows the same architectural patterns but handles OCBC format and adds Meta splitting capability.

### Company Branding (STRICTLY ENFORCED)
- Company name: **"Neoasia"** — capital N, lowercase a. NEVER "NeoAsia" or "NEOASIA"
- Primary Dark: **#004d71** (headers, primary color)
- Primary Gray: **#b1b1b1**
- Secondary Blue: **#66a2c5**
- Secondary Light Blue: **#cde4f5**
- Very Light Blue: **#e7eff8**
- Light Gray: **#eaeaea**
- Font in Excel: **Calibri** throughout
- App must look professional, slim, sleek — enterprise-grade aesthetics

---

## 2. SYSTEM ARCHITECTURE

```
app.py (Streamlit orchestration — main entry point)
├── auth.py                → Password-based authentication gate
├── config.py              → Constants, cardholder map, brand lookup, GL mappings
├── bank_parser.py         → Parse OCBC .xls statement → list[BankRow]
├── ocr_engine.py          → PDF/image → Claude Vision API → JSON extraction
│       ↓ uses
│   prompts.py             → OCR extraction prompts (simple + Meta)
├── matching_engine.py     → Pairs OCR results to bank rows
├── meta_splitter.py       → Split Meta transactions by campaign + VAT
├── description_builder.py → Auto-generate SAP descriptions
├── excel_generator.py     → Generates formatted .xlsx output
└── models.py              → Pydantic data models

.streamlit/
├── config.toml            → Neoasia theme configuration
└── secrets.toml           → API key + app password (local dev only)

requirements.txt
```

### Key Design Decisions

1. **Separate app from CC OCR** — Different bank format (OCBC vs UOB), different output format (SAP journal entry vs simple reconciliation), Meta splitting is unique. Independent codebase, independent deployment.

2. **Dynamic cardholder detection** — The system does NOT hardcode a fixed number of cardholders. It reads the "Last 4 Digits" column from the bank statement and groups transactions by card. A configurable lookup maps last-4-digits to cardholder names, but unknown cards still get processed (just with "Unknown-XXXX" as the name).

3. **Meta splitting from invoice PDFs** — No dependency on Sharry's pre-processed spreadsheet. The tool OCRs the Meta invoice PDFs directly to extract campaign breakdowns. Each PDF contains: Reference Number (FB Code), campaign names, spend amounts, and VAT.

4. **Ad Set → Brand mapping via keyword matching** — Brand names (Calecim, Heliocare, Profhilo, etc.) appear in the ad set names. The tool scans for these keywords. If no match, defaults to "Corporate". This lookup is configurable in config.py AND can be overridden via a CSV upload in the app.

5. **GL Account suggestion for simple transactions** — Based on vendor name patterns (e.g., "Grab" → 6312204 Travelling). These are suggestions only — Finance overrides in Excel via dropdowns.

6. **Penny-perfect rounding** — When splitting amounts, the sum of parts must equal the bank amount exactly. Remainder is absorbed by the largest row.

7. **Claude Sonnet 4.6 for OCR** — Model string: `claude-sonnet-4-6` (NO date suffix). Best cost/accuracy ratio.

---

## 3. INPUT DATA SPECIFICATIONS

### 3.1 OCBC Bank Statement (.xls)

The file contains one or more sheets, each named like "Bank statement - Mar26", "Bank statement - Feb26".

**Column layout (row 0 is header):**

| Col Index | Header | Example | Notes |
|-----------|--------|---------|-------|
| 0 | ID | 193061652 | Unique transaction ID from OCBC |
| 1 | Transaction Date | 2026-01-03 00:00:00 OR "2/14/2026" | Mixed format — sometimes datetime, sometimes string |
| 2 | Company Name | Neoasia (S) Pte Ltd (9378) | Always Neoasia — can ignore |
| 3 | Narrative | Grab* A-9XRJTDGGX9SFAV Singapore | Transaction description — key for matching |
| 4 | Last 4 Digits | 9804 | Card identifier — maps to cardholder |
| 5 | Merchant Currency Amount | 364838 or 42.09 | Amount in merchant's currency |
| 6 | Merchant Currency | VND, SGD, PHP, IDR, HKD, MYR | ISO currency code |
| 7 | Transaction Amount | 18.29 or 42.09 | Amount in SGD (settlement currency) |
| 8 | Transaction Currency | SGD | Always SGD |
| 9 | Unreconciled Amount | same as col 5 | Can ignore — same as merchant amount |
| 10 | Currency | same as col 6 | Can ignore — same as merchant currency |

**CRITICAL DATE PARSING:** Column 1 has mixed formats:
- Some rows: `2026-01-03 00:00:00` (datetime object from xlrd)
- Some rows: `"2/14/2026"` or `"3/26/2026"` (string in M/D/YYYY)
- Use two-pass parsing: try pd.to_datetime first, fallback for string dates

**Identifying Meta transactions:** Check if Narrative contains "FACEBK" (case-insensitive). If yes → Meta transaction. Everything else → Simple transaction.

**Extracting FB Code from Meta narrative:**
Pattern: `FACEBK *XXXXXXXX` or `FACEBK *XXXXXXXXXX` followed by space and location
- "FACEBK *TTTW5EZGT2 DUBLIN" → FB Code = "TTTW5EZGT2"
- "FACEBK *UQACXFVGT2 fb.me/ads" → FB Code = "UQACXFVGT2"
- "FACEBK *56436FMGT2 fb.me/ads" → FB Code = "56436FMGT2"
- Regex: `FACEBK\s*\*?\s*(\w{10,})`

**Negative amounts:** Refunds appear as negative values (e.g., -77.27 SGD). These are valid transactions — keep them.

**Cardholder Mapping (default, configurable):**

```python
CARDHOLDER_MAP = {
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
```

If a last-4-digits value is not in the map, use "Unknown-{last4}" as the cardholder name.

### 3.2 Receipt Files (Simple Transactions)

Supported formats: PDF, PNG, JPG, JPEG, TIFF, BMP, WebP
- PDFs rendered at 200 DPI via PyMuPDF
- Images resized if >3000px on longest side
- All converted to PNG base64 for Claude Vision API calls
- Multi-page PDFs: ALL pages sent in a single API call

### 3.3 Meta Invoice PDFs

These are PDF receipts from Meta Platforms. Each contains a single daily transaction. Structure extracted via OCR:

```
Tax invoice for Neoasia Vietnam
Account ID: 1368485137966251
Invoice/Payment Date: Feb 14, 2026, 6:00 PM
Payment method: Visa ···· 9804
Reference Number: TTTW5EZGT2                    ← FB Code (join key)
Transaction ID: 25915253954831097-...
Product Type: Meta ads

Paid: ₫192,508                                  ← Total including VAT
Subtotal: 175,007 VND
VAT: ₫17,501 (Rate: 10%)

Campaigns:
  MED I - Feb
    Calecim Brand Ads: ₫3,568                   ← Ad Set + Spend
    Heliocare Brands Campaign: ₫70,042
  Profhilo 64MG 28.02.26 Webinar
    64MG Profhilo Webinar 28 Feb: ₫101,397
```

**What to extract from Meta invoice PDFs:**
1. Reference Number (FB Code) — e.g., "TTTW5EZGT2"
2. Total paid amount (VND)
3. Subtotal (VND) — amount before VAT
4. VAT amount (VND) and rate (should be 10%)
5. Campaign breakdown: list of {ad_set_name, spend_vnd}

---

## 4. PROCESSING PIPELINE

### 4.1 Bank Statement Parsing (bank_parser.py)

```python
def parse_ocbc_statement(file) -> list[BankRow]:
    """
    Parse OCBC .xls bank statement.
    Handles multiple sheets (one per month).
    Returns list of BankRow sorted by date.
    """
    # Read all sheets
    # For each sheet: row 0 = headers, rows 1+ = data
    # Parse dates (two-pass: datetime objects + string fallback)
    # Identify Meta vs Simple via Narrative
    # Extract FB Code for Meta transactions
    # Map Last 4 Digits → cardholder name
    # Return list[BankRow]
```

### 4.2 OCR Engine (ocr_engine.py)

Two OCR modes:

**Simple Receipt OCR** — Extract: date, vendor name, nature/description, currency, amount, GST/tax, invoice number. Same approach as CC OCR.

**Meta Invoice OCR** — Extract: reference_number (FB Code), total_paid_vnd, subtotal_vnd, vat_vnd, vat_rate, campaigns: [{name, spend_vnd}]. Use a specialized prompt (see prompts.py section).

API call pattern:
- Model: `claude-sonnet-4-6`
- Max tokens: 8192
- Retry: 3 attempts with exponential backoff (2s, 4s, 8s)
- PDF → images via PyMuPDF at 200 DPI
- Images resized to max 3000px
- Response: JSON (strip markdown fences before parsing)

### 4.3 Matching Engine (matching_engine.py)

**For Simple transactions:**
- Two-pass: exact match (score ≥ 100) then approximate (score ≥ 50)
- Scoring: amount match (±1% = 80pts, ±5% = 50pts), date match (same day = 20pts, ±1 day = 10pts), vendor signature match (+20pts)
- Vendor signatures: {"Grab": ["grab"], "Shopee": ["shopee"], "Scoot": ["scoot", "flyscoot"], ...}

**For Meta transactions:**
- Match by FB Code. Extract FB Code from bank row narrative AND from OCR'd Meta PDF reference number. Exact string match.
- This is a deterministic join — no fuzzy matching needed.

### 4.4 Meta Splitting Engine (meta_splitter.py)

This is the core new component. For each Meta bank row:

```python
def split_meta_transaction(bank_row: BankRow, meta_ocr: MetaInvoiceOCR) -> list[SplitRow]:
    """
    bank_row.sgd_amount = 9.65 (from bank statement)
    meta_ocr.campaigns = [
        {"name": "Calecim Brand Ads", "spend_vnd": 3568},
        {"name": "Heliocare Brands Campaign", "spend_vnd": 70042},
        {"name": "64MG Profhilo Webinar 28 Feb", "spend_vnd": 101397},
    ]
    meta_ocr.subtotal_vnd = 175007
    meta_ocr.vat_vnd = 17501
    meta_ocr.total_vnd = 192508
    
    Algorithm:
    1. For each campaign, calculate its proportion:
       proportion = campaign.spend_vnd / meta_ocr.subtotal_vnd
    
    2. Calculate VAT for this campaign:
       campaign_vat_vnd = campaign.spend_vnd * 0.10  (VAT rate)
    
    3. Calculate SGD amounts:
       campaign_sgd = bank_row.sgd_amount * campaign.spend_vnd / meta_ocr.total_vnd
       vat_sgd = bank_row.sgd_amount * campaign_vat_vnd / meta_ocr.total_vnd
    
    4. Look up brand/country/division/team from campaign name
    
    5. Generate two rows per campaign:
       - Amount row: GL 6210101 (Advertisement), amount = campaign_sgd
       - VAT row: GL 6312701 (VAT expenses), amount = vat_sgd
    
    6. Apply penny-perfect rounding:
       - Sum all SGD amounts (both spend + VAT rows)
       - Calculate difference from bank_row.sgd_amount
       - Absorb remainder into the largest amount row
    """
```

**Brand Keyword Mapping (config.py):**

```python
BRAND_KEYWORD_MAP = {
    # keyword (case-insensitive) → SAP dimensions
    "calecim": {"brand": "CAL", "country": "VN", "division": "MED-I", "team": "T1"},
    "heliocare": {"brand": "HEL", "country": "VN", "division": "MED-I", "team": "T1"},
    "profhilo": {"brand": "PRO", "country": "VN", "division": "MED-II", "team": "T1"},
    "revalene": {"brand": "REV", "country": "VN", "division": "MED-I", "team": "T1"},
    "nourkrin": {"brand": "NOU", "country": "VN", "division": "MED-I", "team": "T1"},
    "sessions": {"brand": "0_DIM2", "country": "VN", "division": "MED-I", "team": "T1"},
    # ... more as needed
}
DEFAULT_BRAND_MAP = {"brand": "0_DIM2", "country": "VN", "division": "MED-I", "team": "T1"}
# Used for ad sets that don't match any keyword (e.g., "Announcement Post")
```

The user can optionally upload a CSV override with columns: `keyword,brand,country,division,team` to add/modify mappings for a given run.

### 4.5 Description Builder (description_builder.py)

**Simple transactions:**
```
OCBC: PCard - {cardholder} - {vendor} - {nature} ({currency}{amount})
```
Examples:
- `OCBC: PCard - Sharry - Singapore Airlines - Booking#DDOWAT - Air-ticket 05/03/26, SGN/SIN (VND3,681,000)`
- `OCBC: PCard - Sharry - Grab - Booking#A-8UG6Q62WWHTCAV - TPT in Ho Chi Minh (VND95,680)`
- `OCBC: PCard - Jaslyn - Shopee - Order ID#260204SMA46GDX - CNY Goodies (SGD41.60)`

**Meta transactions (amount row):**
```
OCBC: PCard - {cardholder} - Facebook (Meta) - Ref#{fb_code} - {month_year} - VND {spend_vnd}
```
Example: `OCBC: PCard - Sharry - Facebook (Meta) - Ref#TTTW5EZGT2 - Feb26 - VND 3568`

**Meta transactions (VAT row):**
```
OCBC: PCard - {cardholder} - Facebook (Meta) - Ref#{fb_code} - {month_year} - VND {vat_vnd}
```
Example: `OCBC: PCard - Sharry - Facebook (Meta) - Ref#TTTW5EZGT2 - Feb26 - VND 357`

### 4.6 GL Account Suggestion (config.py)

```python
GL_VENDOR_MAP = {
    # Vendor keyword → (GL code, GL name)
    "grab": ("6312204", "Travelling - Sales Staff"),
    "scoot": ("6312204", "Travelling - Sales Staff"),
    "flyscoot": ("6312204", "Travelling - Sales Staff"),
    "singapore air": ("6312204", "Travelling - Sales Staff"),
    "tiket.com": ("6312204", "Travelling - Sales Staff"),
    "novotel": ("6312204", "Travelling - Sales Staff"),
    "mercure": ("6312204", "Travelling - Sales Staff"),
    "shopee": ("6311501", "Printing and stationery"),  # Default, varies
    "taobao": ("6210104", "Promotional expense"),
    "malaysia airlines": ("6312204", "Travelling - Sales Staff"),
    "blue bird": ("6312204", "Travelling - Sales Staff"),
    "mom*": ("6110116", "Staff welfare"),  # MOM levy
}
GL_META_AD = ("6210101", "Advertisement")
GL_META_VAT = ("6312701", "VAT expenses")
```

These are SUGGESTIONS only. The Excel output includes dropdown validation lists for GL Account so Finance can override.

---

## 5. OUTPUT FORMAT

### Excel Structure

Single sheet: "PCard-{month}-{year}"

**Columns (SAP journal entry format):**

| Col | Header | Source | Width |
|-----|--------|--------|-------|
| A | # | Sequential line number | 5 |
| B | Description | Auto-generated (see 4.5) | 60 |
| C | G/L Account | Suggested or blank | 12 |
| D | G/L Account Name | Looked up from GL code | 25 |
| E | Line Number | Sequential | 8 |
| F | Brand | From Meta split or blank | 10 |
| G | Country | From Meta split or blank | 8 |
| H | Division | From Meta split or blank | 10 |
| I | Sales Team | From Meta split or blank | 10 |
| J | Tax Code | ZP for flights, blank otherwise | 8 |
| K | Unit Price | SGD amount (from bank or split) | 14 |
| L | (spacer) | — | 2 |
| M | Bank Date | Transaction date from SOA | 12 |
| N | Bank Narrative | Raw narrative from SOA | 35 |
| O | Card | Last 4 digits | 6 |
| P | Cardholder | Mapped name | 12 |
| Q | Merchant Currency | VND, SGD, etc. | 8 |
| R | Merchant Amount | Amount in merchant currency | 14 |
| S | SGD Amount | Transaction amount from bank | 14 |
| T | Match Status | "Matched"/"Unmatched"/"Meta Split" | 12 |

**Formatting:**
- Header row: Navy (#004D71) background, white text, Calibri 11 bold
- Data rows: Alternating white / very light blue (#e7eff8)
- Borders: Thin, light gray (#eaeaea)
- Number format: "#,##0.00" for amounts, "#,##0" for VND amounts
- Freeze panes: Row 1 + Col B (description)
- Auto-filter on header row

**Lookups sheet (for dropdown validation):**

```python
DROPDOWN_SEED = {
    "GL_Account": ["6312204", "6210101", "6312701", "6210104", "6311001", "6311501", "6110116", "1130104", "1130105"],
    "Brand": ["0_DIM2", "CAL", "HEL", "PRO", "NOU", "REV", "GTP", "ACM", "NST"],
    "Country": ["SG", "VN", "MY", "ID", "PH", "HK", "Shared"],
    "Division": ["0_DIM4", "CORP", "MED-I", "MED-II", "MED-III", "OMNI"],
    "Team": ["0_DIM5", "T1", "T2", "T3", "T4", "OT"],
    "Tax_Code": ["", "ZP", "TX7", "OP"],
}
```

Apply data validation (dropdown lists) to columns C, F, G, H, I, J using values from Lookups sheet.

---

## 6. STREAMLIT APP (app.py)

### Theme Configuration (.streamlit/config.toml)

```toml
[theme]
primaryColor = "#004d71"
backgroundColor = "#f6f8fb"
secondaryBackgroundColor = "#e7eff8"
textColor = "#333333"
font = "sans serif"
```

### Authentication (auth.py)

Simple password gate. On app load, check if authenticated in session_state. If not, show a centered login form. Password stored in st.secrets["app_password"].

```python
def check_auth():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False
    if not st.session_state.authenticated:
        show_login()
        st.stop()

def show_login():
    # Centered, branded login form
    # Logo + "Neoasia P-Card OCR" title
    # Password input
    # Login button
    # On success: st.session_state.authenticated = True; st.rerun()
```

### App Flow (5 Steps)

Use `st.tabs` or step-based navigation:

**Step 1: Upload Bank Statement**
- st.file_uploader for .xls file
- On upload: parse immediately, show summary (# transactions, date range, # cards, # Meta vs Simple)
- Display parsed transactions in st.dataframe (sortable, filterable)

**Step 2: Upload Receipts**
- st.file_uploader (accept_multiple_files=True) for PDFs/images
- Separate uploader for Meta invoice PDFs (or detect automatically from filename/content)
- Show upload count + file list

**Step 3: Process & Match**
- "Process All" button
- st.status or st.progress showing:
  - "Parsing bank statement..."
  - "OCR-ing receipt 1 of N..."
  - "OCR-ing Meta invoice 1 of M..."
  - "Matching receipts to bank rows..."
  - "Splitting Meta transactions..."
  - "Generating descriptions..."
- Show results summary: X matched, Y unmatched, Z Meta splits generated

**Step 4: Review**
- Tabbed view: "All Rows" | "Matched" | "Unmatched" | "Meta Splits"
- st.dataframe showing the generated output
- For unmatched bank rows: manual assignment UI (selectbox to pick an OCR result)
- For Meta splits: expandable view showing the campaign breakdown per transaction

**Step 5: Export**
- st.download_button for the generated Excel
- Summary stats: total rows, total SGD amount, match rate

### UI Requirements
- Professional, clean, enterprise-grade
- Use st.columns for layout
- Use custom CSS to inject Neoasia branding (navy header, clean typography)
- Use st.metric for key stats
- Use st.badge (if available) or colored st.markdown for status indicators
- Mobile-responsive (Streamlit handles this)
- Show a Neoasia disclaimer footer: "Neoasia (S) Pte Ltd — P-Card OCR & Reconciliation Tool — Confidential"

---

## 7. OCR PROMPTS (prompts.py)

### Simple Receipt Prompt

```python
SIMPLE_RECEIPT_PROMPT = """You are an expert receipt and invoice data extractor for a Singapore-based company called Neoasia.

Extract the following fields from this receipt/invoice image. Return ONLY valid JSON with no markdown formatting.

{
  "transaction_date": "YYYY-MM-DD",
  "vendor": "Vendor/merchant name (clean, standardized)",
  "nature": "Concise description of what was purchased/paid for",
  "currency": "ISO 4217 currency code (SGD, VND, IDR, PHP, HKD, MYR, EUR, USD)",
  "amount": 0.00,
  "gst_amount": null,
  "invoice_number": null,
  "confidence": "high|medium|low",
  "document_notes": null
}

Rules:
- For amounts: use the TOTAL amount including tax. No currency symbols.
- For VND amounts: do NOT include commas or periods as thousand separators. Example: 364838 not 364,838.
- For vendor names: clean up abbreviations. "SQ" = "Singapore Airlines". "FLYSCOOT" = "Scoot".
- For nature: be concise but specific. Include booking references, order IDs, room dates.
- For dates: use the transaction/payment date, not the document creation date.
- If multiple transactions exist on one receipt, return an array of objects.
- If you cannot read a field, set it to null. Never guess.
"""
```

### Meta Invoice Prompt

```python
META_INVOICE_PROMPT = """You are an expert at extracting structured data from Facebook/Meta advertising invoice PDFs for a company called Neoasia.

Extract the following from this Meta ads invoice. Return ONLY valid JSON with no markdown formatting.

{
  "reference_number": "The Reference Number (also called FB Code) — e.g., TTTW5EZGT2",
  "invoice_date": "YYYY-MM-DD",
  "payment_method_last4": "Last 4 digits of card used",
  "total_paid_vnd": 0,
  "subtotal_vnd": 0,
  "vat_vnd": 0,
  "vat_rate_percent": 10,
  "campaigns": [
    {
      "ad_set_name": "Name of the ad set/campaign",
      "spend_vnd": 0
    }
  ]
}

Rules:
- The Reference Number is the unique code shown near the top of the invoice.
- total_paid_vnd should include VAT. It equals subtotal_vnd + vat_vnd.
- campaigns should list EVERY individual ad set with its spend amount.
- Campaign groups (like "MED I - Feb") may contain multiple ad sets — list each ad set separately.
- Amounts are in VND (Vietnamese Dong). No decimals for VND.
- If the invoice shows impressions, you can ignore those — we only need amounts.
- CRITICAL: The sum of all campaign spend_vnd values should equal subtotal_vnd (before VAT).
"""
```

---

## 8. DATA MODELS (models.py)

```python
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
from enum import Enum

class TransactionType(str, Enum):
    SIMPLE = "simple"
    META = "meta"

class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

class BankRow(BaseModel):
    row_index: int
    transaction_id: str              # OCBC ID
    transaction_date: date
    narrative: str                   # Raw narrative
    last_4_digits: str               # Card identifier
    cardholder: str                  # Mapped from last_4_digits
    merchant_amount: float
    merchant_currency: str           # VND, SGD, PHP, etc.
    sgd_amount: float                # Settlement amount in SGD
    transaction_type: TransactionType  # SIMPLE or META
    fb_code: Optional[str] = None    # Extracted FB Code for Meta transactions
    is_refund: bool = False          # True if sgd_amount < 0

class OcrTransaction(BaseModel):
    transaction_date: Optional[date] = None
    vendor: str
    nature: str
    currency: str
    amount: float
    gst_amount: Optional[float] = None
    invoice_number: Optional[str] = None
    confidence: Confidence = Confidence.HIGH
    source_file: str = ""
    document_notes: Optional[str] = None

class MetaCampaign(BaseModel):
    ad_set_name: str
    spend_vnd: float

class MetaInvoiceOCR(BaseModel):
    reference_number: str            # FB Code
    invoice_date: Optional[date] = None
    payment_method_last4: Optional[str] = None
    total_paid_vnd: float
    subtotal_vnd: float
    vat_vnd: float
    vat_rate_percent: float = 10.0
    campaigns: List[MetaCampaign]
    source_file: str = ""

class SplitRow(BaseModel):
    """One output row in the final Excel"""
    description: str
    gl_account: Optional[str] = None
    gl_account_name: Optional[str] = None
    line_number: int
    brand: str = "0_DIM2"
    country: str = ""
    division: str = "0_DIM4"
    team: str = "0_DIM5"
    tax_code: str = ""
    sgd_amount: float
    # Bank reference fields
    bank_date: Optional[date] = None
    bank_narrative: str = ""
    card_last4: str = ""
    cardholder: str = ""
    merchant_currency: str = ""
    merchant_amount: float = 0.0
    bank_sgd: float = 0.0
    match_status: str = ""          # "Matched", "Unmatched", "Meta Split"
    row_type: str = ""              # "simple", "meta_spend", "meta_vat"

class MatchedRow(BaseModel):
    bank: BankRow
    ocr: Optional[OcrTransaction] = None
    match_confidence: Optional[str] = None
    match_reason: Optional[str] = None
```

---

## 9. REQUIREMENTS

```
streamlit>=1.45.0
anthropic>=0.52.0
openpyxl>=3.1.2
xlrd>=2.0.1
PyMuPDF>=1.24.0
pandas>=2.2.0
pydantic>=2.0.0
Pillow>=10.0.0
```

---

## 10. TESTING CHECKLIST

Use the uploaded test data (Feb/Mar 2026):

1. **Bank parser:** Parse `SOA_-_Pcard.xls` → verify 89 rows from Feb sheet, 176 from Mar sheet
2. **Date parsing:** Verify both datetime objects and string dates parse correctly
3. **Meta identification:** Verify all "FACEBK" rows flagged as Meta type
4. **FB Code extraction:** Verify "FACEBK *TTTW5EZGT2 DUBLIN" → "TTTW5EZGT2"
5. **Meta OCR:** OCR one Meta invoice PDF, verify Reference Number + campaign breakdown extracted
6. **Meta splitting:** For TTTW5EZGT2 (SGD 9.65, 3 campaigns): verify 6 output rows, verify sum = 9.65
7. **Brand mapping:** "Calecim Brand Ads" → CAL, "Heliocare Brands Campaign" → HEL, "64MG Profhilo Webinar 28 Feb" → PRO
8. **Description format:** Verify matches the pattern "OCBC: PCard - Sharry - Facebook (Meta) - Ref#TTTW5EZGT2 - Feb26 - VND 3568"
9. **Excel output:** Open in Excel, verify dropdowns work, formatting correct, amounts sum correctly
10. **Auth:** Verify password gate blocks without correct password

---

## 11. DEPLOYMENT

### Streamlit Community Cloud
1. Push to GitHub (private repo)
2. Deploy on Streamlit Community Cloud
3. Set secrets: `ANTHROPIC_API_KEY`, `APP_PASSWORD`
4. URL pattern: `https://neoasia-pcard-ocr.streamlit.app`

### Secrets structure (.streamlit/secrets.toml for local dev)
```toml
ANTHROPIC_API_KEY = "sk-ant-..."
APP_PASSWORD = "neoasia2026"
```

---

## 12. DEVELOPMENT APPROACH

Build iteratively in this order:
1. **config.py + models.py** — Constants, data models
2. **bank_parser.py** — Parse OCBC .xls (test with uploaded file)
3. **auth.py** — Password gate
4. **app.py (skeleton)** — Basic Streamlit layout with upload + auth
5. **ocr_engine.py + prompts.py** — OCR pipeline for simple + Meta
6. **matching_engine.py** — Match simple receipts to bank rows
7. **meta_splitter.py** — The core Meta splitting engine
8. **description_builder.py** — Auto-generate SAP descriptions
9. **excel_generator.py** — Generate formatted Excel output
10. **app.py (full)** — Wire everything together, add review UI

At each step, test with the real uploaded data before proceeding.

---

## 13. CRITICAL ANTI-PATTERNS

- **Do NOT hardcode a fixed number of cardholders.** Dynamic detection from bank statement.
- **Do NOT create a database.** This is a processing pipeline, not a data store.
- **Do NOT use `claude-sonnet-4-6-XXXXXXXX`** — no date suffix on the model string.
- **Do NOT use `unsafe_allow_html=True` excessively** — use Streamlit's native theming first, CSS injection only for polish.
- **Do NOT truncate any amounts.** Penny-perfect rounding is critical.
- **Do NOT assume VND amounts have decimals.** VND is an integer currency.
- **Do NOT skip the penny-perfect rounding step** in Meta splitting. The sum of all split rows MUST equal the bank SGD amount.
- **Do NOT process files client-side.** All processing happens server-side.
