"""
Neoasia P-Card OCR & Reconciliation — Streamlit entrypoint.

Phase 2 scope:
  - Auth gate (auth.check_auth)
  - 5-tab layout: Upload Bank SOA | Upload Receipts | Process & Match
                  | Review | Export
  - Tab 1: parse OCBC .xls on upload, persist in session_state, show metrics
  - Tab 2: collect simple receipt files + Meta invoice PDFs
  - Tabs 3-5: placeholder (wired up in later phases)

UI deliberately uses Streamlit's native theming via .streamlit/config.toml
plus a small dose of CSS injection for the navy header bar, refined tabs,
metric cards, and footer. No further unsafe_allow_html beyond that.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st

from auth import check_auth, logout
from bank_parser import date_range, parse_ocbc_statement, split_by_type
from config import APP_TITLE, COLORS, COMPANY_LEGAL_NAME, COMPANY_NAME
from models import BankRow


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
    "bank_rows": [],          # list[BankRow]
    "bank_filename": None,    # str
    "bank_parse_error": None, # str
    "simple_receipts": [],    # list[UploadedFile]
    "meta_invoices": [],      # list[UploadedFile]
    "active_tab": 0,
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
            max-width: 1400px;
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


# --- Tab 3: Process & Match (placeholder) ---------------------------------

with tab_process:
    section_title("Process & Match", "OCR pipeline + reconciliation.")
    st.info("Coming in Phase 3 — wires up the OCR engine, simple-receipt matcher and Meta splitter.")


# --- Tab 4: Review (placeholder) ------------------------------------------

with tab_review:
    section_title("Review", "Inspect matched/unmatched rows and Meta splits.")
    st.info("Coming in Phase 4 — row-level review with manual override controls.")


# --- Tab 5: Export (placeholder) ------------------------------------------

with tab_export:
    section_title("Export", "Generate the formatted SAP journal entry workbook.")
    st.info("Coming in Phase 5 — formatted .xlsx with dropdowns and Lookups sheet.")


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
