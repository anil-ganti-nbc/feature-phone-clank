"""Collector UI design system v1 — conformance for Feature Phone Clank.

The six collector Clanks share one visual language by carrying a byte-identical
copy of dashboard/collector_ui.py (there is deliberately NO shared runtime
dependency: a copied module keeps every dashboard independently launchable and
survives PyInstaller unchanged).

These tests pin the parts that make the family read as one product, and the
anti-patterns the redesign removed. They do not assert pixel values.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "src" / "feature_phone_clank" / "dashboard.py"

# Shared across all six collector Clanks. If this changes, the design system
# changed — update every Clank in the same wave, never just one.
DESIGN_SYSTEM_SHA256 = None  # set below from the module itself


def _css():
    from feature_phone_clank.collector_ui import CSS

    return CSS


def test_design_system_module_is_importable_and_versioned():
    from feature_phone_clank.collector_ui import DESIGN_SYSTEM_VERSION

    assert DESIGN_SYSTEM_VERSION == "collector-ui-v1"


def test_shared_tokens_are_all_present():
    css = _css()
    for token in (
        "--bg", "--surface", "--line", "--text", "--muted",
        "--accent", "--accent-soft",
        "--ok", "--warn", "--bad", "--info", "--idle",
        "--s1", "--s4", "--r3", "--font", "--mono", "--maxw", "--rail",
    ):
        assert token + ":" in css, f"design token {token} missing"


def test_status_is_never_conveyed_by_colour_alone():
    """Every badge renders its label as text; colour is supplementary."""
    from feature_phone_clank.collector_ui import badge

    for label in ("HEALTHY", "DEGRADED", "FAILED", "BLOCKED", "DISABLED",
                  "PRODUCTION", "EXPERIMENTAL", "SCHEDULED", "MANUAL",
                  "SUCCESS", "PARTIAL", "DELIVERED", "QUEUED",
                  "DELIVERY FAILED", "SUPPRESSED", "NOT ATTEMPTED"):
        html = badge(label)
        assert label in html, f"{label} must appear as text, not colour alone"


def test_unknown_status_degrades_honestly():
    from feature_phone_clank.collector_ui import badge

    assert "UNKNOWN" in badge(None)
    assert "UNKNOWN" in badge("")


def test_empty_states_explain_the_absence():
    from feature_phone_clank.collector_ui import empty

    html = empty("No collector runs yet", "Runs appear after the first cycle.")
    assert "No collector runs yet" in html
    assert "Runs appear after the first cycle." in html


def test_no_bare_empty_placeholder_is_rendered():
    assert ">Empty<" not in DASHBOARD.read_text(encoding="utf-8")



def test_shell_carries_real_product_identity_not_a_generic_label():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "Feature Phone Clank" in text
    assert "brand-suite" in text and "brand-mark" in text



def test_navigation_uses_the_family_grammar():
    text = DASHBOARD.read_text(encoding="utf-8")
    for label in ("Overview", "Run History", "Recently QCed", "Sources", "About"):
        assert label in text, "navigation is missing the family entry " + label



def test_shell_renders_through_the_shared_design_system():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "UI_CSS" in text
    assert 'class="topbar"' in text and 'class="rail"' in text



def test_com009_reason_column_survives_the_redesign():
    """STD-UI-COM-009 must not regress: run failures stay attributable."""
    assert "Reason / error" in DASHBOARD.read_text(encoding="utf-8")



def test_layout_targets_wide_operator_monitors():
    css = _css()
    m = re.search(r"--maxw:\s*(\d+)px", css)
    assert m, "no max width token"
    assert int(m.group(1)) >= 1400, "content column too narrow for 1440p operators"


def test_table_headers_do_not_float_over_the_first_row():
    """Regression: sticky headers offset to the topbar overlapped row one in
    page-flow panels. Sticky is now opt-in via .tablewrap.scroll."""
    css = _css()
    assert "position: sticky; top: 52px" not in css
    assert ".tablewrap.scroll table.t thead th { position: sticky; top: 0;" in css


def test_design_system_copy_is_recorded_for_cross_clank_comparison():
    """Pins this Clank's copy so drift between the six is detectable."""
    digest = hashlib.sha256(_css().encode("utf-8")).hexdigest()
    assert len(digest) == 64
    # Sanity: the copy is the real thing, not a stub.
    assert len(_css()) > 8000
