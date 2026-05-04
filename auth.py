"""
Password-based authentication gate for the Neoasia P-Card OCR app.

Single shared password sourced from st.secrets["APP_PASSWORD"]. On success the
app stores `authenticated = True` in session state and reruns; the rest of the
app then proceeds to render. On failure we surface a generic error message
(never reveal whether the password is wrong vs. missing).
"""

from __future__ import annotations

import hmac

import streamlit as st

from config import APP_TITLE, COLORS, COMPANY_NAME


_AUTH_KEY = "authenticated"


def check_auth() -> None:
    """Block app rendering until the user enters the correct password.

    Call this immediately after st.set_page_config() and before any other UI.
    If the user is not authenticated, the login form is rendered and st.stop()
    halts the rest of the script.
    """
    if st.session_state.get(_AUTH_KEY):
        return

    _render_login()
    st.stop()


def logout() -> None:
    """Clear the auth flag and rerun. Hooked up via the sidebar logout button."""
    st.session_state[_AUTH_KEY] = False
    st.rerun()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _correct_password() -> str | None:
    """Read APP_PASSWORD from st.secrets. Returns None if not configured.

    Streamlit raises KeyError when the key is missing and a
    FileNotFoundError-derived exception when secrets.toml is absent. Both are
    handled the same way: treat as not-configured.
    """
    try:
        return st.secrets["APP_PASSWORD"]
    except (KeyError, FileNotFoundError):
        return None
    except Exception:
        # Defensive: Streamlit versions vary on the exact exception class
        # raised when secrets.toml is missing in deployment.
        return None


def _render_login() -> None:
    """Render a centered, branded login card."""
    _inject_login_css()

    # Centered column layout — middle column ~33% width on wide screens.
    left, mid, right = st.columns([1, 1.2, 1])
    with mid:
        st.markdown(
            f"""
            <div class="brand-card">
              <div class="brand-card-header">
                <div class="brand-mark">{COMPANY_NAME[0]}</div>
                <div>
                  <div class="brand-name">{COMPANY_NAME}</div>
                  <div class="brand-sub">{APP_TITLE}</div>
                </div>
              </div>
              <div class="brand-card-divider"></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.form("login_form", clear_on_submit=False, border=False):
            password = st.text_input(
                "Password",
                type="password",
                placeholder="Enter access password",
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button(
                "Sign in", width="stretch", type="primary"
            )

        if submitted:
            expected = _correct_password()
            if expected is None:
                st.error(
                    "Authentication is not configured. Set APP_PASSWORD in "
                    ".streamlit/secrets.toml (local) or in the Streamlit Cloud "
                    "secrets panel (deployed)."
                )
            elif _passwords_match(password, expected):
                st.session_state[_AUTH_KEY] = True
                st.rerun()
            else:
                st.error("Invalid password. Please try again.")

        st.markdown(
            f"""
            <div class="brand-footer-mini">
              {COMPANY_NAME} (S) Pte Ltd &middot; Internal use only
            </div>
            """,
            unsafe_allow_html=True,
        )


def _passwords_match(submitted: str, expected: str) -> bool:
    """Constant-time comparison to avoid trivial timing oracles."""
    if not submitted or not expected:
        return False
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def _inject_login_css() -> None:
    """Login-screen-only CSS. Kept inline so the gate is self-contained."""
    primary = COLORS["primary_dark"]
    light_blue = COLORS["secondary_light_blue"]
    very_light = COLORS["very_light_blue"]
    light_gray = COLORS["light_gray"]

    st.markdown(
        f"""
        <style>
          /* Hide the default Streamlit chrome on the login screen */
          #MainMenu, header, footer {{ visibility: hidden; height: 0; }}
          .stDeployButton {{ display: none !important; }}

          .block-container {{
            padding-top: 4rem !important;
            max-width: 100% !important;
          }}

          .brand-card {{
            padding: 2rem 2rem 1rem;
            background: white;
            border: 1px solid {light_gray};
            border-radius: 12px;
            box-shadow: 0 4px 24px rgba(0, 77, 113, 0.06);
            margin-top: 2rem;
          }}
          .brand-card-header {{
            display: flex;
            align-items: center;
            gap: 1rem;
            margin-bottom: 1.25rem;
          }}
          .brand-mark {{
            width: 48px; height: 48px;
            border-radius: 10px;
            background: {primary};
            color: white;
            display: flex; align-items: center; justify-content: center;
            font-weight: 700; font-size: 1.4rem;
            letter-spacing: -0.5px;
          }}
          .brand-name {{
            color: {primary};
            font-size: 1.4rem;
            font-weight: 600;
            line-height: 1.1;
          }}
          .brand-sub {{
            color: #666;
            font-size: 0.9rem;
            margin-top: 2px;
          }}
          .brand-card-divider {{
            height: 1px;
            background: {light_gray};
            margin: 0 -2rem 1rem;
          }}

          /* Login form polish — sits inside the centered column, OUTSIDE the
             card div but visually attached. */
          [data-testid="stForm"] {{
            background: white;
            border: 1px solid {light_gray};
            border-top: none;
            border-radius: 0 0 12px 12px;
            padding: 0 2rem 2rem !important;
            margin-top: -1rem;  /* tuck under brand card */
          }}
          [data-testid="stForm"] input {{
            border-radius: 8px !important;
            border: 1px solid {light_gray} !important;
            padding: 0.65rem 0.85rem !important;
          }}
          [data-testid="stForm"] input:focus {{
            border-color: {primary} !important;
            box-shadow: 0 0 0 3px {very_light} !important;
          }}
          [data-testid="stFormSubmitButton"] button {{
            background: {primary} !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 0.65rem 1.25rem !important;
            font-weight: 600 !important;
            margin-top: 0.5rem !important;
            transition: filter 0.15s ease;
          }}
          [data-testid="stFormSubmitButton"] button:hover {{
            filter: brightness(1.08);
          }}

          .brand-footer-mini {{
            text-align: center;
            color: #999;
            font-size: 0.8rem;
            margin-top: 1.5rem;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )
