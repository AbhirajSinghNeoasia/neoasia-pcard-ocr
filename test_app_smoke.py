"""
Phase 2 smoke test for the Streamlit app.

Drives app.py end-to-end via streamlit.testing.v1.AppTest:
  1. Initial render shows the auth gate (password input, no tabs)
  2. Wrong password produces an error and stays on the gate
  3. Right password unlocks the dashboard (no error, 5 tabs present)
  4. Injecting parsed BankRow data into session_state renders the right
     summary metrics (263 total, 79 Meta, 184 Simple, 7 cardholders, 7 refunds)

The file_uploader widget itself is not driven through AppTest (its testing
plumbing varies across streamlit versions); the parser was already verified
end-to-end in test_bank_parser.py. Here we verify the app rendering logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

from bank_parser import parse_ocbc_statement


XLS = Path("test_data/SOA - Pcard.xls")
APP_PASSWORD = "neoasia2026"  # mirrors .streamlit/secrets.toml

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


def boot() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=20)
    at.run()
    if at.exception:
        for e in at.exception:
            print("APP EXCEPTION:", repr(e.value))
        raise RuntimeError("App raised on initial render")
    return at


def is_on_login(at: AppTest) -> bool:
    """We're on the login screen iff the password input is rendered.

    AppTest reports widget-type='text_input' for both plain and password
    inputs, so we identify the login input by label ("Password"). The
    dashboard has zero text_input widgets, so any text_input here means we
    haven't authenticated yet.
    """
    return any(t.label == "Password" for t in at.text_input)


# ---------------------------------------------------------------------------
# 1. Auth gate
# ---------------------------------------------------------------------------

def test_auth_gate() -> AppTest:
    section("1. Auth gate")
    at = boot()

    check("Initial render: password input is visible (login screen)",
          is_on_login(at),
          f"text_inputs={[(t.label, t.type) for t in at.text_input]}")
    check("Initial render: no tabs are exposed",
          len(at.tabs) == 0,
          f"got {len(at.tabs)} tabs")
    check("Initial render: no app exception",
          not at.exception,
          str([repr(e.value) for e in at.exception]))

    # ----- Wrong password ---------------------------------------------
    pw_input = next(t for t in at.text_input if t.label == "Password")
    pw_input.set_value("not-the-password")
    # The login form has a single button ("Sign in")
    submit = at.button[0]
    submit.click()
    at.run()

    check("Wrong password produces an 'Invalid password' error",
          any("Invalid password" in e.value for e in at.error),
          f"errors={[e.value for e in at.error]}")
    check("Still on login screen after wrong password",
          is_on_login(at),
          "expected password input still rendered")

    # ----- Correct password -------------------------------------------
    pw_input = next(t for t in at.text_input if t.label == "Password")
    pw_input.set_value(APP_PASSWORD)
    at.button[0].click()
    at.run()

    check("Correct password: no error rendered",
          len(at.error) == 0,
          f"errors={[e.value for e in at.error]}")
    check("Correct password: login screen replaced by dashboard (no password input)",
          not is_on_login(at),
          f"text_inputs still present={[(t.label, t.type) for t in at.text_input]}")
    check("Correct password: 5 tabs are exposed",
          len(at.tabs) == 5,
          f"got {len(at.tabs)} tabs")
    check("Correct password: no app exception",
          not at.exception,
          str([repr(e.value) for e in at.exception]))
    auth_val = at.session_state["authenticated"] if "authenticated" in at.session_state else None
    check("session_state.authenticated == True",
          auth_val is True,
          f"got {auth_val!r}")
    return at


# ---------------------------------------------------------------------------
# 2. Bank-statement summary rendering
# ---------------------------------------------------------------------------

def test_bank_summary_rendering(at: AppTest) -> None:
    section("2. Bank statement summary widgets (Tab 1)")

    if not XLS.exists():
        check("Fixture exists", False, str(XLS))
        return

    # Drive the parser ourselves, then prime session_state as if the upload
    # had just completed. The app reads from session_state to render.
    rows = parse_ocbc_statement(str(XLS))
    at.session_state["bank_rows"] = rows
    at.session_state["bank_filename"] = XLS.name
    at.session_state["bank_parse_error"] = None
    at.run()

    if at.exception:
        for e in at.exception:
            print("APP EXCEPTION on render:", repr(e.value))
        check("No exceptions while rendering populated dashboard", False, "see above")
        return
    check("No exceptions while rendering populated dashboard", True)

    # Sanity: parsed row count
    n_rows = len(at.session_state["bank_rows"]) if "bank_rows" in at.session_state else 0
    check("session_state.bank_rows has 263 rows",
          n_rows == 263,
          f"got {n_rows}")

    # Metrics
    metric_values = {m.label: m.value for m in at.metric}
    print(f"  Metric labels: {list(metric_values.keys())}")
    expected = {
        "Total transactions": "263",
        "Simple": "184",
        "Meta (Facebook)": "79",
        "Cardholders": "7",
        "Refunds": "7",
    }
    for label, want in expected.items():
        check(f"Metric '{label}' == {want!r}",
              metric_values.get(label) == want,
              f"got {metric_values.get(label)!r}")

    # The transactions DataFrame should be rendered
    check("At least one DataFrame is rendered (transactions table)",
          len(at.dataframe) >= 1,
          f"got {len(at.dataframe)} dataframe(s)")

    # Footer marker should appear in the rendered markdown
    md_blob = "\n".join(m.value for m in at.markdown)
    check("Footer line includes 'Confidential'",
          "Confidential" in md_blob,
          "Footer marker missing")
    check("Brand bar includes 'P-Card OCR'",
          "P-Card OCR" in md_blob,
          "Brand bar missing")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        at = test_auth_gate()
        test_bank_summary_rendering(at)
    except RuntimeError as exc:
        print(f"\nFATAL: {exc}")
        return 2

    section("Result")
    print(f"  PASSED : {len(PASSED)}")
    print(f"  FAILED : {len(FAILED)}")
    if FAILED:
        print("\nFailures:")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("\nPhase 2 smoke test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
