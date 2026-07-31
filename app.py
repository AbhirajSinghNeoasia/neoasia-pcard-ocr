"""
Neoasia P-Card OCR & Reconciliation — Streamlit entrypoint.

Full app surface:
  - Auth gate (auth.check_auth)
  - 5-tab layout: Upload Bank SOA | Upload Receipts | Process & Match
                  | Review | Export
  - Tab 1: parse OCBC .xls on upload, persist in session_state, show metrics
  - Tab 2: collect simple receipt files + Meta invoice PDFs + optional
           brand-mapping CSV override
  - Tab 3: orchestration hub — runs the OCR + matching + splitter pipeline
           with live progress and surfaces orphan / failed-OCR warnings
  - Tab 4: row-level review (all rows / unmatched / Meta splits) with
           manual-match override
  - Tab 5: download the formatted SAP-journal Excel + preview the first 20
           rows

UI uses Streamlit's native theming via .streamlit/config.toml plus a small
dose of CSS injection for the navy header bar, refined tabs, metric cards,
status badges and footer.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd
import streamlit as st

from auth import check_auth, logout
from bank_parser import date_range, parse_ocbc_statement, split_by_type
from config import APP_TITLE, COLORS, COMPANY_LEGAL_NAME, COMPANY_NAME
from excel_generator import generate_output_excel
from models import BankRow, MatchedRow, SplitRow
from pipeline import (
    AssemblyResult,
    assemble_outputs,
    derive_period_string,
    parse_brand_override_csv,
    process_all,
)


# ---------------------------------------------------------------------------
# Page setup — must be the first Streamlit call.
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title=f"{COMPANY_NAME} P-Card OCR",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Auth gate. If not authenticated, the login screen renders here and st.stop()
# halts the rest of this script.
check_auth()


# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "bank_rows":         [],     # list[BankRow]
    "bank_filename":     None,   # str
    "bank_parse_error":  None,   # str
    "simple_receipts":   [],     # list[UploadedFile]
    "meta_invoices":     [],     # list[UploadedFile]
    "brand_override":    None,   # dict[str, dict[str, str]] | None
    "brand_override_filename": None,
    "assembly":          None,   # AssemblyResult | None — populated by Tab 3
    "manual_matches":    {},     # dict[int, int] — simple_bank_idx -> ocr_idx
    "active_tab":        0,
}
for k, v in _DEFAULTS.items():
    st.session_state.setdefault(k, v)


# ---------------------------------------------------------------------------
# Custom CSS — enterprise-grade polish on top of the native theme.
# ---------------------------------------------------------------------------

def inject_chrome_css() -> None:
    primary = COLORS["primary_dark"]
    accent = COLORS["secondary_blue"]
    very_light = COLORS["very_light_blue"]
    light_blue = COLORS["secondary_light_blue"]
    light_gray = COLORS["light_gray"]

    st.markdown(
        f"""
        <style>
          /* Hide default Streamlit chrome we don't want */
          #MainMenu {{ visibility: hidden; }}
          footer {{ visibility: hidden; }}
          header[data-testid="stHeader"] {{ background: transparent; }}
          .stDeployButton {{ display: none !important; }}

          /* Pull content up close to the top of the viewport */
          .block-container {{
            padding-top: 1.25rem !important;
            padding-bottom: 4rem !important;
            max-width: 98%;
          }}

          /* ---- Brand header bar ---- */
          .brand-bar {{
            background: linear-gradient(135deg, {primary} 0%, #003d5c 100%);
            color: white;
            padding: 1.25rem 1.75rem;
            border-radius: 10px;
            margin-bottom: 1.5rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            box-shadow: 0 4px 16px rgba(0, 77, 113, 0.10);
          }}
          .brand-bar-left {{ display: flex; align-items: center; gap: 0.95rem; }}
          .brand-bar-mark {{
            width: 42px; height: 42px;
            border-radius: 8px;
            background: rgba(255, 255, 255, 0.15);
            border: 1px solid rgba(255, 255, 255, 0.25);
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 1.2rem;
          }}
          .brand-bar-title {{ font-size: 1.25rem; font-weight: 600; line-height: 1.1; }}
          .brand-bar-sub   {{ font-size: 0.85rem; opacity: 0.8; margin-top: 2px; }}
          .brand-bar-right {{
            font-size: 0.8rem;
            opacity: 0.85;
            text-align: right;
            line-height: 1.3;
          }}

          /* ---- Tabs ---- */
          .stTabs [data-baseweb="tab-list"] {{
            gap: 0;
            background: white;
            border: 1px solid {light_gray};
            border-radius: 10px;
            padding: 0.35rem;
          }}
          .stTabs [data-baseweb="tab"] {{
            background: transparent;
            color: #555;
            font-weight: 500;
            padding: 0.55rem 1.2rem;
            border-radius: 7px;
            border: none;
          }}
          .stTabs [aria-selected="true"] {{
            background: {primary} !important;
            color: white !important;
          }}
          .stTabs [data-baseweb="tab-highlight"] {{ display: none; }}
          .stTabs [data-baseweb="tab-border"]    {{ display: none; }}

          /* ---- Metric cards ---- */
          [data-testid="stMetric"] {{
            background: white;
            padding: 1rem 1.1rem;
            border-radius: 10px;
            border: 1px solid {light_gray};
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
          }}
          [data-testid="stMetricLabel"] {{ color: #777; font-weight: 500; font-size: 0.8rem; }}
          [data-testid="stMetricValue"] {{ color: {primary}; font-weight: 700; }}

          /* ---- Section heading ---- */
          .section-title {{
            color: {primary};
            font-size: 1.05rem;
            font-weight: 600;
            margin: 1.5rem 0 0.5rem;
            padding-bottom: 0.4rem;
            border-bottom: 2px solid {very_light};
          }}
          .section-help {{
            color: #777;
            font-size: 0.88rem;
            margin-bottom: 1rem;
          }}

          /* ---- File uploader ---- */
          [data-testid="stFileUploader"] section {{
            background: {very_light};
            border: 1.5px dashed {accent};
            border-radius: 10px;
          }}
          [data-testid="stFileUploader"] section:hover {{
            background: {light_blue};
          }}

          /* ---- Buttons ---- */
          .stButton > button[kind="primary"],
          .stDownloadButton > button[kind="primary"] {{
            background: {primary};
            border: none;
            color: white;
            font-weight: 600;
          }}

          /* ---- Footer ---- */
          .brand-footer {{
            text-align: center;
            color: #999;
            font-size: 0.8rem;
            padding: 1.5rem 0 0;
            margin-top: 3rem;
            border-top: 1px solid {light_gray};
          }}

          /* ---- Sidebar logout button ---- */
          [data-testid="stSidebar"] {{ background: {very_light}; }}

          /* ---- Status badges (used in Review tab) ---- */
          .badge {{
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.78rem;
            font-weight: 600;
            line-height: 1.4;
          }}
          .badge-matched   {{ background: #d4edda; color: #1d6c31; }}
          .badge-approx    {{ background: #fff3cd; color: #856404; }}
          .badge-unmatched {{ background: #f8d7da; color: #842029; }}
          .badge-meta      {{ background: {light_blue}; color: {primary}; }}
          .badge-manual    {{ background: #e2d9f3; color: #5a3d99; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_chrome_css()


# ---------------------------------------------------------------------------
# Header bar
# ---------------------------------------------------------------------------

def render_header() -> None:
    today = date.today().strftime("%d %b %Y")
    st.markdown(
        f"""
        <div class="brand-bar">
          <div class="brand-bar-left">
            <div class="brand-bar-mark">{COMPANY_NAME[0]}</div>
            <div>
              <div class="brand-bar-title">{COMPANY_NAME} &middot; P-Card OCR</div>
              <div class="brand-bar-sub">{APP_TITLE}</div>
            </div>
          </div>
          <div class="brand-bar-right">
            {today}<br>
            <span style="opacity: 0.7;">Internal use only</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


render_header()


# Sidebar — logout + small status panel.
with st.sidebar:
    st.markdown(f"### {COMPANY_NAME}")
    st.caption("P-Card OCR & Reconciliation")
    st.divider()

    bank_rows: list[BankRow] = st.session_state["bank_rows"]
    if bank_rows:
        st.success(f"Bank SOA loaded · {len(bank_rows)} rows")
    else:
        st.info("No bank statement loaded yet")

    n_simple = len(st.session_state["simple_receipts"])
    n_meta = len(st.session_state["meta_invoices"])
    st.caption(f"Receipts: **{n_simple}**   Meta PDFs: **{n_meta}**")

    st.divider()
    if st.button("Sign out", width="stretch"):
        logout()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def section_title(text: str, help_text: str = "") -> None:
    st.markdown(f'<div class="section-title">{text}</div>', unsafe_allow_html=True)
    if help_text:
        st.markdown(f'<div class="section-help">{help_text}</div>', unsafe_allow_html=True)


def bank_rows_to_dataframe(rows: list[BankRow]) -> pd.DataFrame:
    """Project BankRow records onto a display-friendly DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(
        [
            {
                "Date": pd.Timestamp(r.transaction_date),
                "Card": r.last_4_digits,
                "Cardholder": r.cardholder,
                "Type": "Meta" if r.transaction_type == "meta" else "Simple",
                "Narrative": r.narrative,
                "Currency": r.merchant_currency,
                "Merchant Amt": r.merchant_amount,
                "SGD": r.sgd_amount,
                "FB Code": r.fb_code or "",
                "Refund": r.is_refund,
            }
            for r in rows
        ]
    )


def format_date_range(rng: Optional[tuple[date, date]]) -> str:
    if not rng:
        return "—"
    a, b = rng
    if a == b:
        return a.strftime("%d %b %Y")
    return f"{a.strftime('%d %b %Y')}  →  {b.strftime('%d %b %Y')}"


def split_rows_to_dataframe(rows: list[SplitRow]) -> pd.DataFrame:
    """Project SplitRow records onto a display-friendly DataFrame."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([
        {
            "#":           r.line_number,
            "Status":      r.match_status or "",
            "Date":        pd.Timestamp(r.bank_date) if r.bank_date else None,
            "Cardholder":  r.cardholder,
            "Card":        r.card_last4,
            "Description": r.description,
            "GL":          r.gl_account or "",
            "GL Name":     r.gl_account_name or "",
            "Brand":       r.brand,
            "Country":     r.country,
            "Division":    r.division,
            "Team":        r.team,
            "Tax":         r.tax_code,
            "Unit Price":  r.sgd_amount,
            "Bank SGD":    r.bank_sgd,
            "Mer Cur":     r.merchant_currency,
            "Mer Amount":  r.merchant_amount,
        }
        for r in rows
    ])


_BADGE_CLASS = {
    "Matched":    "badge-matched",
    "Unmatched":  "badge-unmatched",
    "Meta Split": "badge-meta",
    "manual":     "badge-manual",
    "exact":      "badge-matched",
    "approximate": "badge-approx",
}


def status_badge(status: str) -> str:
    cls = _BADGE_CLASS.get(status or "", "badge-unmatched")
    return f'<span class="badge {cls}">{status or "—"}</span>'


def cents_sum(rows: list[SplitRow]) -> int:
    """Sum SGD amounts as integer cents — bypasses float drift."""
    total = Decimal("0")
    for r in rows:
        total += Decimal(str(r.sgd_amount))
    return int((total * 100).to_integral_value())


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

TAB_LABELS = [
    "1 · Upload Bank SOA",
    "2 · Upload Receipts",
    "3 · Process & Match",
    "4 · Review",
    "5 · Export",
]
tab_soa, tab_receipts, tab_process, tab_review, tab_export = st.tabs(TAB_LABELS)


# --- Tab 1: Bank SOA -------------------------------------------------------

with tab_soa:
    section_title(
        "Upload OCBC P-Card statement",
        "Drop the monthly OCBC P-Card SOA (.xls). All sheets within the workbook "
        "are parsed automatically. Dates are normalised, last-4 card digits "
        "zero-padded, and Meta vs simple transactions auto-classified.",
    )

    uploaded = st.file_uploader(
        "Bank statement (.xls)",
        type=["xls"],
        accept_multiple_files=False,
        label_visibility="collapsed",
        key="soa_uploader",
    )

    # Parse on upload — only re-parse if the filename changed (cheap signal,
    # and avoids burning CPU on every rerun).
    if uploaded is not None:
        if st.session_state["bank_filename"] != uploaded.name:
            try:
                with st.spinner("Parsing OCBC statement…"):
                    rows = parse_ocbc_statement(io.BytesIO(uploaded.getvalue()))
                st.session_state["bank_rows"] = rows
                st.session_state["bank_filename"] = uploaded.name
                st.session_state["bank_parse_error"] = None
            except Exception as exc:  # surface the real error to the user
                st.session_state["bank_rows"] = []
                st.session_state["bank_filename"] = None
                st.session_state["bank_parse_error"] = str(exc)

    if st.session_state["bank_parse_error"]:
        st.error(f"Failed to parse statement: {st.session_state['bank_parse_error']}")

    bank_rows: list[BankRow] = st.session_state["bank_rows"]

    if bank_rows:
        simple_rows, meta_rows = split_by_type(bank_rows)
        cardholders = sorted({r.cardholder for r in bank_rows})
        rng = date_range(bank_rows)
        n_refunds = sum(1 for r in bank_rows if r.is_refund)

        section_title("Statement summary")

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total transactions", f"{len(bank_rows):,}")
        m2.metric("Simple", f"{len(simple_rows):,}")
        m3.metric("Meta (Facebook)", f"{len(meta_rows):,}")
        m4.metric("Cardholders", f"{len(cardholders)}")
        m5.metric("Refunds", f"{n_refunds}")

        st.markdown(
            f"<div style='margin: 0.5rem 0 1rem; color:#555;'>"
            f"Statement period: <b>{format_date_range(rng)}</b> · "
            f"file: <code>{st.session_state['bank_filename']}</code>"
            f"</div>",
            unsafe_allow_html=True,
        )

        # Per-cardholder breakdown
        with st.expander("Breakdown by cardholder", expanded=False):
            ch_rows = []
            for ch in cardholders:
                cr = [r for r in bank_rows if r.cardholder == ch]
                ch_rows.append({
                    "Cardholder": ch,
                    "Transactions": len(cr),
                    "Meta": sum(1 for r in cr if r.transaction_type == "meta"),
                    "Simple": sum(1 for r in cr if r.transaction_type == "simple"),
                    "SGD Total": sum(r.sgd_amount for r in cr),
                })
            ch_df = pd.DataFrame(ch_rows)
            st.dataframe(
                ch_df,
                hide_index=True,
                width="stretch",
                column_config={
                    "SGD Total": st.column_config.NumberColumn("SGD Total", format="%.2f"),
                },
            )

        section_title("Parsed transactions")
        df = bank_rows_to_dataframe(bank_rows)
        st.dataframe(
            df,
            hide_index=True,
            width="stretch",
            height=520,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
                "Card": st.column_config.TextColumn("Card", width="small"),
                "Type": st.column_config.TextColumn("Type", width="small"),
                "Narrative": st.column_config.TextColumn("Narrative", width="large"),
                "Currency": st.column_config.TextColumn("Cur", width="small"),
                "Merchant Amt": st.column_config.NumberColumn("Merchant Amt", format="%.2f"),
                "SGD": st.column_config.NumberColumn("SGD", format="%.2f"),
                "FB Code": st.column_config.TextColumn("FB Code", width="small"),
                "Refund": st.column_config.CheckboxColumn("Refund", width="small"),
            },
        )
    else:
        st.info("Upload an OCBC P-Card .xls file above to begin.")


# --- Tab 2: Receipts -------------------------------------------------------

with tab_receipts:
    section_title(
        "Receipts and Meta invoices",
        "Upload supporting documents. Simple-transaction receipts (PDFs / images) "
        "match by amount, date and vendor. Meta invoice PDFs match by FB "
        "reference code and drive the campaign-level split.",
    )

    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**Simple receipts**")
        st.caption("PDF, PNG, JPG, JPEG, TIFF, BMP, WebP. Multi-file.")
        uploads_simple = st.file_uploader(
            "Simple receipts",
            type=["pdf", "png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"],
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="receipts_uploader",
        )
        if uploads_simple is not None:
            st.session_state["simple_receipts"] = uploads_simple
        n = len(st.session_state["simple_receipts"])
        if n:
            st.success(f"{n} receipt file(s) staged")
            with st.expander("File list", expanded=False):
                for f in st.session_state["simple_receipts"]:
                    st.markdown(f"• `{f.name}`  &mdash;  {f.size/1024:,.0f} KB", unsafe_allow_html=True)
        else:
            st.info("No receipts uploaded yet")

    with col2:
        st.markdown("**Meta (Facebook) invoice PDFs**")
        st.caption("One PDF per daily transaction. The reference number on each PDF is the join key.")
        uploads_meta = st.file_uploader(
            "Meta invoices",
            type=["pdf"],
            accept_multiple_files=True,
            label_visibility="collapsed",
            key="meta_uploader",
        )
        if uploads_meta is not None:
            st.session_state["meta_invoices"] = uploads_meta
        n = len(st.session_state["meta_invoices"])
        if n:
            st.success(f"{n} Meta invoice(s) staged")
            with st.expander("File list", expanded=False):
                for f in st.session_state["meta_invoices"]:
                    st.markdown(f"• `{f.name}`  &mdash;  {f.size/1024:,.0f} KB", unsafe_allow_html=True)
        else:
            st.info("No Meta invoices uploaded yet")

    # Combined upload counter
    n_simple = len(st.session_state["simple_receipts"])
    n_meta = len(st.session_state["meta_invoices"])
    if n_simple or n_meta:
        st.markdown(
            f"<div style='margin-top:0.75rem; color:#555;'>"
            f"<b>{n_simple}</b> simple receipt(s) + <b>{n_meta}</b> Meta invoice(s) staged."
            f"</div>",
            unsafe_allow_html=True,
        )

    # Optional brand override CSV
    section_title(
        "Brand mapping override (optional)",
        "Upload a CSV with columns `keyword,brand,country,division,team` to add "
        "or override the default brand keyword mapping for this run only. "
        "Existing mappings stay in effect for keywords you don't override.",
    )
    brand_csv = st.file_uploader(
        "Brand override CSV",
        type=["csv"],
        accept_multiple_files=False,
        label_visibility="collapsed",
        key="brand_override_uploader",
    )
    if brand_csv is not None:
        if st.session_state.get("brand_override_filename") != brand_csv.name:
            try:
                override = parse_brand_override_csv(brand_csv)
                st.session_state["brand_override"] = override
                st.session_state["brand_override_filename"] = brand_csv.name
                st.success(f"Loaded {len(override)} brand override(s) from `{brand_csv.name}`")
            except Exception as exc:
                st.session_state["brand_override"] = None
                st.error(f"Could not parse CSV: {exc}")
        else:
            n = len(st.session_state["brand_override"] or {})
            st.success(f"Brand override active · {n} entries from `{brand_csv.name}`")
    elif st.session_state["brand_override"]:
        st.info(
            f"Brand override still active "
            f"({len(st.session_state['brand_override'])} entries). "
            f"Re-upload to change."
        )


# --- Tab 3: Process & Match -----------------------------------------------

with tab_process:
    section_title(
        "Process & Match",
        "Run the OCR engine over every uploaded receipt and Meta invoice, then "
        "match them to the bank statement and generate the SAP journal rows.",
    )

    bank_rows: list[BankRow] = st.session_state["bank_rows"]
    n_simple = len(st.session_state["simple_receipts"])
    n_meta = len(st.session_state["meta_invoices"])

    # ---- Prerequisites ------------------------------------------------
    if not bank_rows:
        st.warning("Upload an OCBC bank statement on **Tab 1** before processing.")
    elif not (n_simple or n_meta):
        st.warning("Upload at least one receipt or Meta invoice on **Tab 2** before processing.")
    else:
        # Pre-flight summary
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Bank rows", f"{len(bank_rows):,}")
        c2.metric("Simple receipts", f"{n_simple}")
        c3.metric("Meta invoices", f"{n_meta}")
        c4.metric(
            "Brand override",
            f"{len(st.session_state['brand_override'])} entries"
            if st.session_state["brand_override"] else "Off",
        )

        st.caption(
            "Each receipt and each Meta invoice will trigger one Claude Sonnet 4.6 "
            "API call. Processing is sequential — for ~80 receipts expect a few minutes."
        )

        if st.button("🚀 Process all", type="primary", key="process_all_btn"):
            st.session_state["assembly"] = None  # clear any previous result
            st.session_state["manual_matches"] = {}

            with st.status("Running pipeline…", expanded=True) as status:
                def progress_cb(message: str, current: int, total: int) -> None:
                    status.write(f"({current}/{total}) {message}")

                try:
                    result = process_all(
                        bank_rows=bank_rows,
                        simple_files=st.session_state["simple_receipts"],
                        meta_files=st.session_state["meta_invoices"],
                        brand_override=st.session_state["brand_override"],
                        progress_cb=progress_cb,
                    )
                    st.session_state["assembly"] = result
                    label = (
                        f"Done · {len(result.all_split_rows)} output row(s) · "
                        f"{result.n_matched_simple} matched · "
                        f"{result.n_unmatched_simple} unmatched · "
                        f"{result.n_meta_splits} Meta split rows"
                    )
                    status.update(label=label, state="complete", expanded=False)
                except Exception as exc:                                    # noqa: BLE001
                    status.update(
                        label=f"Pipeline failed: {type(exc).__name__}: {exc}",
                        state="error", expanded=True,
                    )

    # ---- Result summary (persisted across reruns) --------------------
    result: Optional[AssemblyResult] = st.session_state["assembly"]
    if result is not None:
        section_title("Results")

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total output rows", f"{len(result.all_split_rows):,}")
        m2.metric("Matched simple", f"{result.n_matched_simple}")
        m3.metric("Unmatched simple", f"{result.n_unmatched_simple}")
        m4.metric("Meta split rows", f"{result.n_meta_splits}")
        m5.metric("Total SGD", f"{result.total_sgd:,.2f}")

        # Warnings
        if result.ocr_errors:
            st.warning(
                f"OCR failed on {len(result.ocr_errors)} file(s). The rest were processed."
            )
            with st.expander("Failed file details", expanded=False):
                for filename, err in result.ocr_errors:
                    st.markdown(f"- `{filename}` — {err}")

        if result.orphan_meta_codes:
            st.warning(
                f"{len(result.orphan_meta_codes)} Meta bank row(s) have no matching invoice "
                f"PDF. They appear as 'Unmatched (Meta)' placeholders in the export."
            )
            with st.expander("Orphan Meta bank codes", expanded=False):
                st.markdown(", ".join(f"`{c}`" for c in result.orphan_meta_codes))

        if result.orphan_invoice_codes:
            st.info(
                f"{len(result.orphan_invoice_codes)} Meta invoice(s) have no matching bank "
                f"row. These will not appear in the export."
            )
            with st.expander("Orphan invoice codes", expanded=False):
                st.markdown(", ".join(f"`{c}`" for c in result.orphan_invoice_codes))

        st.success("Processing complete. Review on **Tab 4** or download on **Tab 5**.")


# --- Tab 4: Review (placeholder) ------------------------------------------

with tab_review:
    section_title(
        "Review",
        "Inspect every output row, manually re-pair unmatched bank rows with "
        "leftover OCR results, and verify Meta splits reconcile to the cent.",
    )

    result = st.session_state["assembly"]
    if result is None:
        st.info("Run processing on **Tab 3** first to populate the review.")
    else:
        sub_all, sub_unmatched, sub_meta = st.tabs(
            ["All rows", "Unmatched simple", "Meta splits"]
        )

        # ---- All rows -------------------------------------------------
        with sub_all:
            df = split_rows_to_dataframe(result.all_split_rows)
            st.dataframe(
                df, hide_index=True, width="stretch", height=540,
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
                    "Description": st.column_config.TextColumn("Description", width="large"),
                    "Unit Price": st.column_config.NumberColumn("Unit Price", format="%.2f"),
                    "Bank SGD":   st.column_config.NumberColumn("Bank SGD",   format="%.2f"),
                    "Mer Amount": st.column_config.NumberColumn("Mer Amount", format="%.2f"),
                },
            )
            st.caption(
                f"{len(result.all_split_rows)} rows · "
                f"sum = {result.total_sgd:,.2f} SGD"
            )

        # ---- Unmatched simple — manual override ----------------------
        with sub_unmatched:
            unmatched_pairs: list[tuple[int, MatchedRow]] = [
                (i, m) for i, m in enumerate(result.matched_simple) if m.ocr is None
            ]
            if not unmatched_pairs:
                st.success("Every simple bank row was matched automatically.")
            else:
                # Build the pool of OCR results not yet consumed by an
                # auto-match. Manual selections can only pick from this pool.
                ocr_pool = result.ocr_simple
                consumed = {
                    id(m.ocr) for m in result.matched_simple if m.ocr is not None
                }
                if not ocr_pool:
                    st.info(
                        "No OCR results were extracted from the uploaded receipts, so "
                        "manual matching has nothing to offer. Re-upload receipts on Tab 2."
                    )
                else:
                    unused_indices = [
                        i for i, o in enumerate(ocr_pool) if id(o) not in consumed
                    ]
                    if not unused_indices:
                        st.info(
                            "Every OCR result was consumed by the auto-matcher. "
                            "There is nothing left to assign to these unmatched bank rows."
                        )
                    else:
                        st.markdown(
                            f"**{len(unmatched_pairs)} unmatched bank row(s).** Pick a "
                            f"leftover OCR result to assign manually, then click "
                            f"**Apply manual matches**."
                        )

                        new_matches: dict[int, int] = {}
                        with st.form("manual_match_form"):
                            for bidx, mrow in unmatched_pairs:
                                br = mrow.bank
                                st.markdown(
                                    f"**Bank row {bidx}** — {br.transaction_date} · "
                                    f"{br.cardholder} · {br.merchant_currency} "
                                    f"{br.merchant_amount:,.2f} (SGD {br.sgd_amount:,.2f})"
                                )
                                st.caption(f"Narrative: `{br.narrative}`")
                                options = [None] + unused_indices
                                choice = st.selectbox(
                                    f"Pick OCR for row {bidx}",
                                    options=options,
                                    format_func=lambda i, pool=ocr_pool: (
                                        "— skip —" if i is None else
                                        f"{pool[i].vendor} · {pool[i].currency} "
                                        f"{pool[i].amount:,.2f} · {pool[i].transaction_date}"
                                    ),
                                    key=f"manual_pick_{bidx}",
                                    label_visibility="collapsed",
                                )
                                if choice is not None:
                                    new_matches[bidx] = choice
                                st.markdown("---")

                            applied = st.form_submit_button(
                                "Apply manual matches", type="primary",
                            )

                        if applied:
                            st.session_state["manual_matches"] = new_matches
                            try:
                                new_result = assemble_outputs(
                                    bank_rows=st.session_state["bank_rows"],
                                    ocr_simple=ocr_pool,
                                    ocr_meta=result.ocr_meta,
                                    brand_override=st.session_state["brand_override"],
                                    manual_matches=new_matches,
                                )
                                new_result.ocr_errors = result.ocr_errors
                                st.session_state["assembly"] = new_result
                                st.success(
                                    f"Applied {len(new_matches)} manual match(es). "
                                    f"Refreshing…"
                                )
                                st.rerun()
                            except Exception as exc:                        # noqa: BLE001
                                st.error(f"Re-assembly failed: {exc}")

        # ---- Meta splits — grouped view + reconciliation -------------
        with sub_meta:
            meta_rows = result.meta_split_rows
            if not meta_rows:
                st.info("No Meta transactions in this period.")
            else:
                # Group by FB code from description (e.g. "Ref#TTTW5EZGT2 - ...")
                groups: dict[str, list[SplitRow]] = defaultdict(list)
                for r in meta_rows:
                    code = "Unknown"
                    if "Ref#" in r.description:
                        code = r.description.split("Ref#", 1)[1].split(" ", 1)[0]
                    groups[code].append(r)

                st.caption(f"{len(groups)} Meta transaction(s) · {len(meta_rows)} split rows")

                for code, rows in sorted(groups.items()):
                    bank_sgd = rows[0].bank_sgd if rows else 0.0
                    sum_cents = cents_sum(rows)
                    bank_cents = int(round(bank_sgd * 100))
                    reconciled = sum_cents == bank_cents
                    status_icon = "✅" if reconciled else "❌"
                    title = (
                        f"{status_icon} **Ref#{code}** · "
                        f"bank SGD {bank_sgd:,.2f} · "
                        f"split sum SGD {sum_cents/100:,.2f} · "
                        f"{len(rows)} rows"
                    )
                    with st.expander(title, expanded=False):
                        first = rows[0]
                        st.markdown(
                            f"Bank narrative: `{first.bank_narrative}` · "
                            f"Card {first.card_last4} ({first.cardholder}) · "
                            f"{first.bank_date}"
                        )
                        if not reconciled:
                            st.error(
                                f"Off by {(sum_cents - bank_cents)/100:+.2f} SGD — "
                                f"penny-perfect rounding regression."
                            )
                        df = split_rows_to_dataframe(rows)
                        st.dataframe(
                            df, hide_index=True, width="stretch",
                            column_config={
                                "Date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
                                "Description": st.column_config.TextColumn("Description", width="large"),
                                "Unit Price": st.column_config.NumberColumn("Unit Price", format="%.2f"),
                            },
                        )


# --- Tab 5: Export (placeholder) ------------------------------------------

with tab_export:
    section_title(
        "Export",
        "Download the formatted SAP journal entry workbook. The Excel includes "
        "navy-bordered headers, dropdowns on every editable column (GL, Brand, "
        "Country, Division, Team, Tax) and a Lookups sheet for the dropdown sources.",
    )

    result = st.session_state["assembly"]
    if result is None:
        st.info("Run processing on **Tab 3** first to generate exportable rows.")
    elif not result.all_split_rows:
        st.warning("Processing produced zero output rows — nothing to export.")
    else:
        all_rows = result.all_split_rows
        n_total = len(all_rows)
        n_matched = sum(1 for r in all_rows if r.match_status == "Matched")
        n_meta = sum(1 for r in all_rows
                     if r.match_status == "Meta Split" and r.row_type == "meta_spend")
        # Match rate = matched simple / (matched + unmatched simple). Meta
        # rows are always 'Meta Split' so excluded from the rate.
        denom = result.n_matched_simple + result.n_unmatched_simple
        match_rate = (result.n_matched_simple / denom) if denom else 1.0

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total rows", f"{n_total:,}")
        c2.metric("Total SGD", f"{result.total_sgd:,.2f}")
        c3.metric("Match rate (simple)", f"{match_rate:.0%}")
        c4.metric("Meta transactions", f"{n_meta}")

        # Build the Excel + offer download
        period = derive_period_string(st.session_state["bank_rows"])
        today_str = date.today().strftime("%Y%m%d")
        filename = f"PCard_{period}_{today_str}.xlsx"

        try:
            with st.spinner("Building Excel…"):
                excel_bytes = generate_output_excel(all_rows, period)
            st.download_button(
                label="📥 Download SAP Journal Entry (.xlsx)",
                data=excel_bytes,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                type="primary",
                key="download_xlsx",
            )
            st.caption(
                f"File: `{filename}` · {len(excel_bytes):,} bytes · "
                f"sheet: `PCard-{period}` · Lookups sheet included for dropdowns."
            )
        except Exception as exc:                                           # noqa: BLE001
            st.error(f"Excel generation failed: {exc}")

        # Preview
        section_title("Preview (first 20 rows)")
        preview_df = split_rows_to_dataframe(all_rows[:20])
        st.dataframe(
            preview_df, hide_index=True, width="stretch", height=420,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="DD MMM YYYY"),
                "Description": st.column_config.TextColumn("Description", width="large"),
                "Unit Price": st.column_config.NumberColumn("Unit Price", format="%.2f"),
                "Bank SGD":   st.column_config.NumberColumn("Bank SGD",   format="%.2f"),
                "Mer Amount": st.column_config.NumberColumn("Mer Amount", format="%.2f"),
            },
        )


# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------

st.markdown(
    f"""
    <div class="brand-footer">
      {COMPANY_LEGAL_NAME} &mdash; P-Card OCR &amp; Reconciliation Tool &mdash; Confidential
    </div>
    """,
    unsafe_allow_html=True,
)
