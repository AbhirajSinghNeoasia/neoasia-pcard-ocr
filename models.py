"""
Pydantic data models for the P-Card OCR pipeline.

Every component (parser, OCR engine, matcher, splitter, excel generator)
exchanges data through these typed records. Keep them deliberately thin —
business logic belongs in the engines, not in validators.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class TransactionType(str, Enum):
    SIMPLE = "simple"
    META = "meta"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RowType(str, Enum):
    SIMPLE = "simple"
    META_SPEND = "meta_spend"
    META_VAT = "meta_vat"


class MatchStatus(str, Enum):
    MATCHED = "Matched"
    UNMATCHED = "Unmatched"
    META_SPLIT = "Meta Split"


class BankRow(BaseModel):
    """One transaction parsed from the OCBC P-Card .xls statement."""

    model_config = ConfigDict(use_enum_values=True)

    row_index: int                    # Zero-based, unique across all sheets
    sheet_name: str                   # e.g. "Bank statement - Mar26"
    transaction_id: str               # OCBC ID (string — preserves precision)
    transaction_date: date
    narrative: str                    # Raw narrative from SOA
    last_4_digits: str                # Always 4-char zero-padded
    cardholder: str                   # Mapped via CARDHOLDER_MAP
    merchant_amount: float
    merchant_currency: str            # ISO code: VND, SGD, PHP, etc.
    sgd_amount: float                 # Settlement amount in SGD
    transaction_type: TransactionType
    fb_code: Optional[str] = None     # Set for META transactions only
    is_refund: bool = False           # True iff sgd_amount < 0


class OcrTransaction(BaseModel):
    """One transaction extracted from a simple receipt PDF/image."""

    model_config = ConfigDict(use_enum_values=True)

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
    """One ad set inside a Meta invoice with its VND spend."""
    ad_set_name: str
    spend_vnd: float


class MetaInvoiceOCR(BaseModel):
    """Extraction result for one Meta invoice PDF."""
    reference_number: str             # FB Code — join key against bank rows
    invoice_date: Optional[date] = None
    payment_method_last4: Optional[str] = None
    total_paid_vnd: float
    subtotal_vnd: float
    vat_vnd: float
    vat_rate_percent: float = 10.0
    campaigns: List[MetaCampaign] = Field(default_factory=list)
    source_file: str = ""


class SplitRow(BaseModel):
    """One row in the final Excel SAP-journal-entry output."""

    model_config = ConfigDict(use_enum_values=True)

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

    # Bank-row passthrough fields (for the right-hand reference columns)
    bank_date: Optional[date] = None
    bank_narrative: str = ""
    card_last4: str = ""
    cardholder: str = ""
    merchant_currency: str = ""
    merchant_amount: float = 0.0
    bank_sgd: float = 0.0
    match_status: str = ""
    row_type: str = ""


class MatchedRow(BaseModel):
    """Pairing produced by the matching engine for a simple bank row."""
    bank: BankRow
    ocr: Optional[OcrTransaction] = None
    match_confidence: Optional[str] = None
    match_reason: Optional[str] = None
