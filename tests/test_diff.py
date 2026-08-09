from __future__ import annotations

from feature_phone_clank.core.diff import (
    diff_meaningful_fields, normalize_field_value, severity_for_changes,
)
from feature_phone_clank.core.models import AlertLevel


def test_only_meaningful_fields_are_diffed():
    old = {"usb-connection": {"values": ["micro-usb"], "category": "connectivity"},
           "buttons": {"values": ["physical"], "category": "design"}}
    new = {"usb-connection": {"values": ["micro-usb"], "category": "connectivity"},
           "buttons": {"values": ["capacitive"], "category": "design"}}  # not meaningful
    changes = diff_meaningful_fields(old, new)
    assert changes == []


def test_meaningful_field_value_change_detected():
    old = {"usb-connection": {"values": ["micro-usb"], "category": "connectivity"}}
    new = {"usb-connection": {"values": ["usb-type-c"], "category": "connectivity"}}
    changes = diff_meaningful_fields(old, new)
    assert len(changes) == 1
    assert changes[0].field == "usb-connection"
    assert changes[0].old_value == {"values": ["micro-usb"], "category": "connectivity"}
    assert changes[0].new_value == {"values": ["usb-type-c"], "category": "connectivity"}


def test_field_appearance_is_meaningful():
    old = {}
    new = {"nfc": {"values": ["yes"], "category": "connectivity"}}
    changes = diff_meaningful_fields(old, new)
    assert len(changes) == 1
    assert changes[0].field == "nfc"
    assert changes[0].old_value is None


def test_field_disappearance_is_not_a_change():
    """brief section 8: a single field vanishing must not be treated as a
    real removal — only whole-page spec_completeness transitions are."""
    old = {"bluetooth": {"values": ["5-0"], "category": "connectivity"}}
    new = {}
    changes = diff_meaningful_fields(old, new)
    assert changes == []


def test_value_list_order_is_not_meaningful():
    old = {"networks": {"values": ["gsm", "lte"], "category": "networks"}}
    new = {"networks": {"values": ["lte", "gsm"], "category": "networks"}}
    assert diff_meaningful_fields(old, new) == []


def test_whitespace_and_case_are_not_meaningful():
    old = {"os": {"values": ["S30+"], "category": "operating-system"}}
    new = {"os": {"values": [" s30+ "], "category": "operating-system"}}
    assert diff_meaningful_fields(old, new) == []


def test_unit_case_is_not_meaningful_but_numeric_value_is():
    old = {"battery-capacity": {"value": 1450, "unit": "mAh"}}
    same_unit_diff_case = {"battery-capacity": {"value": 1450, "unit": "mah"}}
    assert diff_meaningful_fields(old, same_unit_diff_case) == []

    changed = {"battery-capacity": {"value": 1500, "unit": "mAh"}}
    changes = diff_meaningful_fields(old, changed)
    assert len(changes) == 1
    assert changes[0].field == "battery-capacity"


def test_normalize_does_not_erase_a_real_change():
    """Conservative normalization (brief section 7): different tokens must
    never collapse to equal, even if they're 'similar'."""
    assert normalize_field_value({"values": ["usb-type-c"], "category": "x"}) != \
           normalize_field_value({"values": ["micro-usb"], "category": "x"})


def test_severity_high_for_connectivity_platform_fields():
    old = {"max-network-speed": {"values": ["2g"], "category": "networks"}}
    new = {"max-network-speed": {"values": ["4g"], "category": "networks"}}
    changes = diff_meaningful_fields(old, new)
    assert severity_for_changes(changes) == AlertLevel.HIGH


def test_severity_medium_for_other_meaningful_fields():
    old = {"headphone-jack": {"values": ["no"], "category": "audio"}}
    new = {"headphone-jack": {"values": ["yes"], "category": "audio"}}
    changes = diff_meaningful_fields(old, new)
    assert severity_for_changes(changes) == AlertLevel.MEDIUM


def test_multiple_simultaneous_meaningful_changes_all_captured():
    old = {
        "usb-connection": {"values": ["micro-usb"], "category": "connectivity"},
        "max-network-speed": {"values": ["2g"], "category": "networks"},
    }
    new = {
        "usb-connection": {"values": ["usb-type-c"], "category": "connectivity"},
        "max-network-speed": {"values": ["4g"], "category": "networks"},
    }
    changes = diff_meaningful_fields(old, new)
    assert {c.field for c in changes} == {"usb-connection", "max-network-speed"}
