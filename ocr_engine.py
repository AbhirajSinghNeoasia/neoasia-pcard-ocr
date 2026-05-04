"""
OCR engine — Claude Sonnet 4.6 Vision.

Public API:
    ocr_simple_receipt(file, source_name="") -> list[OcrTransaction]
    ocr_meta_invoice(file, source_name="")   -> MetaInvoiceOCR

Pipeline:
    1. Read input (path | bytes | file-like). PDF -> PyMuPDF rasterises every
       page to PNG at PDF_RENDER_DPI. Images get loaded via PIL and resized so
       longest side <= IMAGE_MAX_DIMENSION.
    2. Encode each PNG as a base64 image block. ALL pages of a single document
       go into ONE Anthropic message — never one call per page.
    3. Call Claude with the appropriate prompt as the SYSTEM message and the
       images as the USER content.
    4. Strip any markdown fences from the response, parse JSON, validate via
       Pydantic, return.

Behaviour:
    - Temperature is 0 (deterministic extraction).
    - Up to OCR_RETRY_COUNT attempts on transient errors (RateLimitError,
      InternalServerError, APIConnectionError, APITimeoutError) with
      exponential backoff per OCR_RETRY_BACKOFF_SECONDS.
    - Authentication, permission and bad-request errors are NOT retried.
    - API key sourced from env ANTHROPIC_API_KEY first, st.secrets fallback.
    - Meta invoice extractions are sanity-checked: a warning is logged if
      the campaigns sum and stated subtotal disagree by more than 1 VND.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from typing import IO, Any, Optional, Union

import anthropic
import fitz  # PyMuPDF
from PIL import Image

from config import (
    CLAUDE_MAX_TOKENS,
    CLAUDE_MODEL,
    IMAGE_MAX_DIMENSION,
    OCR_RETRY_BACKOFF_SECONDS,
    OCR_RETRY_COUNT,
    PDF_RENDER_DPI,
)
from models import MetaInvoiceOCR, OcrTransaction
from prompts import META_INVOICE_PROMPT, SIMPLE_RECEIPT_PROMPT


logger = logging.getLogger(__name__)

FileLike = Union[str, bytes, IO[bytes], "anthropic.types.ImageBlockParam"]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ocr_simple_receipt(file: FileLike, source_name: str = "") -> list[OcrTransaction]:
    """OCR a single receipt/invoice and return one or more OcrTransaction objects.

    Always returns a list — single-transaction receipts return [obj], multi-
    transaction receipts return [obj1, obj2, ...]. Downstream code is simpler
    when the contract is uniform.
    """
    pngs, name = _file_to_pngs(file, source_name)
    raw = _call_with_retry(
        system_prompt=SIMPLE_RECEIPT_PROMPT,
        user_text="Extract the transaction data from this receipt as specified.",
        image_blocks=_build_image_blocks(pngs),
    )
    payload = _parse_json(raw)

    items = payload if isinstance(payload, list) else [payload]
    out: list[OcrTransaction] = []
    for item in items:
        if not isinstance(item, dict):
            logger.warning("Unexpected non-dict item in receipt OCR result: %r", item)
            continue
        item.setdefault("source_file", name)
        out.append(OcrTransaction.model_validate(item))
    return out


def ocr_meta_invoice(file: FileLike, source_name: str = "") -> MetaInvoiceOCR:
    """OCR a Meta (Facebook) invoice PDF and return a MetaInvoiceOCR object."""
    pngs, name = _file_to_pngs(file, source_name)
    raw = _call_with_retry(
        system_prompt=META_INVOICE_PROMPT,
        user_text="Extract the campaign-level Meta invoice data as specified.",
        image_blocks=_build_image_blocks(pngs),
    )
    payload = _parse_json(raw)
    if isinstance(payload, list) and payload:
        # Defensive: should never happen but handle gracefully if model wraps.
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"Meta OCR returned non-object payload: {payload!r}")

    payload.setdefault("source_file", name)
    invoice = MetaInvoiceOCR.model_validate(payload)
    _sanity_check_meta_invoice(invoice, name)
    return invoice


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------


def _resolve_file(file: FileLike, name_hint: str = "") -> tuple[bytes, str]:
    """Return (bytes_payload, filename) for any supported input type."""
    if isinstance(file, str):
        with open(file, "rb") as f:
            return f.read(), name_hint or os.path.basename(file)
    if isinstance(file, (bytes, bytearray)):
        return bytes(file), name_hint or "<bytes>"
    # File-like
    if hasattr(file, "read"):
        data = file.read()
        try:
            file.seek(0)  # be polite — leave the cursor at start
        except Exception:
            pass
        name = name_hint or getattr(file, "name", "<stream>")
        return data, name
    raise TypeError(f"Unsupported file input: {type(file).__name__}")


def _file_to_pngs(file: FileLike, name_hint: str = "") -> tuple[list[bytes], str]:
    """Detect input format and return (PNG bytes per page, filename)."""
    data, name = _resolve_file(file, name_hint)
    if name.lower().endswith(".pdf") or _looks_like_pdf(data):
        return _pdf_to_pngs(data, dpi=PDF_RENDER_DPI), name
    # Treat as a single image
    return [_resize_image_to_png(data, max_dim=IMAGE_MAX_DIMENSION)], name


def _looks_like_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"


def _pdf_to_pngs(pdf_bytes: bytes, dpi: int) -> list[bytes]:
    """Rasterise every page of a PDF to PNG bytes via PyMuPDF."""
    out: list[bytes] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            out.append(pix.tobytes("png"))
    return out


def _resize_image_to_png(img_bytes: bytes, max_dim: int) -> bytes:
    """Open an image, resize so longest side <= max_dim, return PNG bytes."""
    with Image.open(io.BytesIO(img_bytes)) as im:
        im = im.convert("RGB") if im.mode in ("CMYK", "P") else im
        w, h = im.size
        longest = max(w, h)
        if longest > max_dim:
            scale = max_dim / longest
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _build_image_blocks(pngs: list[bytes]) -> list[dict[str, Any]]:
    return [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(p).decode("ascii"),
            },
        }
        for p in pngs
    ]


# ---------------------------------------------------------------------------
# Anthropic call + retry
# ---------------------------------------------------------------------------


_CLIENT: Optional[anthropic.Anthropic] = None


def _get_api_key() -> str:
    """Resolve the Anthropic API key. Env var takes precedence over st.secrets.

    Env-first is intentional: it lets CLI scripts inject the key without
    requiring a Streamlit runtime context. The Streamlit app sets the key in
    secrets.toml which is then surfaced via this fallback.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        return st.secrets["ANTHROPIC_API_KEY"]
    except Exception as exc:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured. Set it in the environment "
            "or in .streamlit/secrets.toml."
        ) from exc


def _client() -> anthropic.Anthropic:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = anthropic.Anthropic(api_key=_get_api_key())
    return _CLIENT


# Errors that are worth retrying — transient/server-side conditions.
_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
)


def _call_with_retry(
    *,
    system_prompt: str,
    user_text: str,
    image_blocks: list[dict[str, Any]],
) -> str:
    """Call Claude with retry on transient errors, return the text body."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, OCR_RETRY_COUNT + 1):
        try:
            return _call_claude_once(system_prompt, user_text, image_blocks)
        except _RETRYABLE as exc:
            last_exc = exc
            if attempt >= OCR_RETRY_COUNT:
                break
            backoff = OCR_RETRY_BACKOFF_SECONDS[
                min(attempt - 1, len(OCR_RETRY_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "Claude call failed (attempt %d/%d): %s. Retrying in %ds.",
                attempt, OCR_RETRY_COUNT, exc.__class__.__name__, backoff,
            )
            time.sleep(backoff)
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.BadRequestError,
        ) as exc:
            # Non-retryable. Surface immediately with context.
            raise RuntimeError(f"Claude rejected the request: {exc}") from exc
    assert last_exc is not None
    raise RuntimeError(
        f"Claude call failed after {OCR_RETRY_COUNT} attempts: {last_exc}"
    ) from last_exc


def _call_claude_once(
    system_prompt: str,
    user_text: str,
    image_blocks: list[dict[str, Any]],
) -> str:
    response = _client().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        temperature=0,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": [*image_blocks, {"type": "text", "text": user_text}],
            }
        ],
    )
    # Concatenate every text block in the response (usually exactly one).
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# JSON parsing + validation
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?|\n?```\s*$", re.IGNORECASE)


def _strip_json_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences."""
    s = text.strip()
    # Trim opening fence
    s = re.sub(r"^```(?:json|JSON)?\s*\n?", "", s)
    # Trim closing fence
    s = re.sub(r"\n?```\s*$", "", s)
    return s.strip()


def _parse_json(raw: str) -> Any:
    """Strip markdown fences and parse the response as JSON."""
    cleaned = _strip_json_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # Surface the first ~200 chars to help debug prompt regressions
        head = cleaned[:200].replace("\n", " ")
        raise ValueError(
            f"Claude returned non-JSON content (parse error: {exc}). "
            f"First 200 chars: {head!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------


def _sanity_check_meta_invoice(inv: MetaInvoiceOCR, source: str) -> None:
    """Log warnings if a Meta invoice OCR result fails internal consistency."""
    campaigns_sum = sum(c.spend_vnd for c in inv.campaigns)
    if abs(campaigns_sum - inv.subtotal_vnd) > 1:
        logger.warning(
            "Meta OCR (%s): campaigns sum %s != subtotal %s (diff %s).",
            source, campaigns_sum, inv.subtotal_vnd, campaigns_sum - inv.subtotal_vnd,
        )

    expected_total = inv.subtotal_vnd + inv.vat_vnd
    if abs(expected_total - inv.total_paid_vnd) > 1:
        logger.warning(
            "Meta OCR (%s): subtotal+VAT %s != total_paid %s (diff %s).",
            source, expected_total, inv.total_paid_vnd, expected_total - inv.total_paid_vnd,
        )

    if not inv.reference_number or len(inv.reference_number) < 8:
        logger.warning(
            "Meta OCR (%s): reference_number looks suspicious: %r",
            source, inv.reference_number,
        )

    if not inv.campaigns:
        logger.warning("Meta OCR (%s): no campaigns extracted.", source)
