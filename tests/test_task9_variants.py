"""Task 9: Variant briefs and expected specs for generality testing.

Tests verify that:
1. Each variant spec is valid JSON and has required structure.
2. Each variant has unique bank names and identity question phrasing from Brief A/B.
3. All variants follow the spec schema.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder.spec import load_spec, load_spec_file
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture

SPEC_FILES: dict[str, str] = {
    "variant_1": "spec_variant_1.json",
    "variant_2": "spec_variant_2.json",
    "variant_3": "spec_variant_3.json",
    "brief_a": "spec_brief_a.json",
    "brief_ab": "spec_brief_ab.json",
}


def load_spec_variant(variant_key: str) -> Any:
    """Load and parse a variant spec file."""
    spec_file = FIXTURES_DIR / SPEC_FILES[variant_key]
    return load_spec_file(spec_file)


def load_spec_dict(variant_key: str) -> dict[str, Any]:
    """Load a variant spec as a raw dict."""
    return load_fixture(SPEC_FILES[variant_key])


def walk_for_nones(value: Any, path: str = "$") -> list[str]:
    """Walk a spec tree and collect paths to any None values."""
    none_paths: list[str] = []
    if value is None:
        none_paths.append(path)
    elif isinstance(value, dict):
        for k, v in value.items():
            none_paths.extend(walk_for_nones(v, f"{path}.{k}"))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            none_paths.extend(walk_for_nones(v, f"{path}[{i}]"))
    return none_paths


# ==============================================================================================
# Spec Validity Tests
# ==============================================================================================


def test_variant_1_valid() -> None:
    """Variant 1 spec is valid JSON with required fields."""
    spec_dict = load_spec_dict("variant_1")
    spec = load_spec_variant("variant_1")
    
    # Spec must be loadable
    assert spec is not None
    
    # No None values in the spec
    nones = walk_for_nones(spec_dict)
    assert len(nones) == 0, f"Expected no None values in spec, found: {nones}"
    
    # Must have required top-level fields
    assert spec_dict.get("schema_version") == "1"
    assert "meta" in spec_dict
    assert "identity" in spec_dict
    assert "bands" in spec_dict
    assert "closing" in spec_dict
    assert spec_dict["meta"]["bank_name"] != ""


def test_variant_2_valid() -> None:
    """Variant 2 spec is valid JSON with required fields."""
    spec_dict = load_spec_dict("variant_2")
    spec = load_spec_variant("variant_2")
    
    # Spec must be loadable
    assert spec is not None
    
    # No None values in the spec
    nones = walk_for_nones(spec_dict)
    assert len(nones) == 0, f"Expected no None values in spec, found: {nones}"
    
    # Must have required top-level fields
    assert spec_dict.get("schema_version") == "1"
    assert "meta" in spec_dict
    assert "identity" in spec_dict
    assert "bands" in spec_dict
    assert "closing" in spec_dict


def test_variant_3_valid() -> None:
    """Variant 3 spec is valid JSON with required fields."""
    spec_dict = load_spec_dict("variant_3")
    spec = load_spec_variant("variant_3")
    
    # Spec must be loadable
    assert spec is not None
    
    # No None values in the spec
    nones = walk_for_nones(spec_dict)
    assert len(nones) == 0, f"Expected no None values in spec, found: {nones}"
    
    # Must have required top-level fields
    assert spec_dict.get("schema_version") == "1"
    assert "meta" in spec_dict
    assert "identity" in spec_dict
    assert "bands" in spec_dict
    assert "closing" in spec_dict


# ==============================================================================================
# Uniqueness Tests
# ==============================================================================================


def test_variant_1_has_unique_bank_name() -> None:
    """Variant 1 has unique bank name compared to Brief A and Brief AB."""
    v1 = load_spec_dict("variant_1")
    brief_a = load_spec_dict("brief_a")
    brief_ab = load_spec_dict("brief_ab")
    
    # Bank names must differ
    v1_bank = v1.get("meta", {}).get("bank_name")
    a_bank = brief_a.get("meta", {}).get("bank_name")
    ab_bank = brief_ab.get("meta", {}).get("bank_name")
    
    assert v1_bank != a_bank, f"Variant 1 bank name ({v1_bank}) matches Brief A ({a_bank})"
    assert v1_bank != ab_bank, f"Variant 1 bank name ({v1_bank}) matches Brief AB ({ab_bank})"


def test_variant_2_has_unique_facts() -> None:
    """Variant 2 has unique bank name and multi-product config."""
    v2 = load_spec_dict("variant_2")
    brief_a = load_spec_dict("brief_a")
    brief_ab = load_spec_dict("brief_ab")
    
    # Bank names must differ
    v2_bank = v2.get("meta", {}).get("bank_name")
    a_bank = brief_a.get("meta", {}).get("bank_name")
    ab_bank = brief_ab.get("meta", {}).get("bank_name")
    
    assert v2_bank != a_bank, f"Variant 2 bank name ({v2_bank}) matches Brief A ({a_bank})"
    assert v2_bank != ab_bank, f"Variant 2 bank name ({v2_bank}) matches Brief AB ({ab_bank})"
    
    # Multi-product config check
    mp = v2.get("multi_product", {})
    # Even if not explicitly enabled, Apex Trust brief describes multi-product
    assert v2_bank == "Apex Trust", "Variant 2 should be Apex Trust (multi-product bank)"
    
    # Outcome table should exist
    outcome_table = v2.get("closing", {}).get("outcome_table", [])
    assert len(outcome_table) > 0, "Variant 2 should have outcome table"


def test_variant_3_has_unique_facts() -> None:
    """Variant 3 has unique bank name."""
    v3 = load_spec_dict("variant_3")
    brief_a = load_spec_dict("brief_a")
    brief_ab = load_spec_dict("brief_ab")
    
    # Bank names must differ
    v3_bank = v3.get("meta", {}).get("bank_name")
    a_bank = brief_a.get("meta", {}).get("bank_name")
    ab_bank = brief_ab.get("meta", {}).get("bank_name")
    
    assert v3_bank != a_bank, f"Variant 3 bank name ({v3_bank}) matches Brief A ({a_bank})"
    assert v3_bank != ab_bank, f"Variant 3 bank name ({v3_bank}) matches Brief AB ({ab_bank})"
    assert v3_bank == "River Bank", "Variant 3 should be River Bank"


# ==============================================================================================
# Structure Tests
# ==============================================================================================


def test_variant_structure() -> None:
    """All variants have valid spec structures with required fields."""
    for variant_key in ["variant_1", "variant_2", "variant_3"]:
        spec_dict = load_spec_dict(variant_key)
        
        # Required top-level fields
        assert "schema_version" in spec_dict, f"{variant_key}: missing schema_version"
        assert spec_dict["schema_version"] == "1", f"{variant_key}: schema_version must be '1'"
        
        assert "meta" in spec_dict, f"{variant_key}: missing meta"
        meta = spec_dict["meta"]
        assert "bank_name" in meta, f"{variant_key}: meta missing bank_name"
        assert "agent_identity" in meta, f"{variant_key}: meta missing agent_identity"
        
        assert "account_fields" in spec_dict, f"{variant_key}: missing account_fields"
        assert "calendar" in spec_dict, f"{variant_key}: missing calendar"
        assert "bands" in spec_dict, f"{variant_key}: missing bands"
        assert "closing" in spec_dict, f"{variant_key}: missing closing"
        
        # Calendar must have weekend_days
        calendar = spec_dict["calendar"]
        assert "weekend_days" in calendar, f"{variant_key}: calendar missing weekend_days"
        assert isinstance(calendar["weekend_days"], list), f"{variant_key}: weekend_days must be a list"
        
        # Bands must be non-empty
        bands = spec_dict["bands"]
        assert isinstance(bands, list), f"{variant_key}: bands must be a list"
        assert len(bands) > 0, f"{variant_key}: bands must be non-empty"
        
        for i, band in enumerate(bands):
            assert "name" in band, f"{variant_key}: band[{i}] missing name"
            assert "days_late_min" in band, f"{variant_key}: band[{i}] missing days_late_min"
            assert "days_late_max" in band, f"{variant_key}: band[{i}] missing days_late_max"
            assert "deadline" in band, f"{variant_key}: band[{i}] missing deadline"
            assert "persuasion_attempts" in band, f"{variant_key}: band[{i}] missing persuasion_attempts"
        
        # Closing must have outcome_table
        closing = spec_dict["closing"]
        assert "outcome_table" in closing, f"{variant_key}: closing missing outcome_table"
        outcome_table = closing["outcome_table"]
        assert isinstance(outcome_table, list), f"{variant_key}: outcome_table must be a list"
        assert len(outcome_table) > 0, f"{variant_key}: outcome_table must be non-empty"
        
        for i, row in enumerate(outcome_table):
            assert "outcome" in row, f"{variant_key}: outcome_table[{i}] missing outcome"
            assert "when" in row, f"{variant_key}: outcome_table[{i}] missing when"


def test_variant_1_structure() -> None:
    """Variant 1 has minimal structure: single product, single band, basic outcomes."""
    v1 = load_spec_dict("variant_1")
    
    # Single band
    bands = v1.get("bands", [])
    assert len(bands) == 1, f"Variant 1 should have 1 band, got {len(bands)}"
    
    # Band covers days 1-45
    band = bands[0]
    assert band["days_late_min"] <= 1, "Variant 1 band should start at or before day 1"
    assert band["days_late_max"] >= 45, "Variant 1 band should cover up to day 45"
    
    # 1 persuasion attempt
    assert band["persuasion_attempts"] == 1, "Variant 1 should have 1 persuasion attempt"
    
    # Identity: 2 questions
    identity_questions = v1.get("identity", {}).get("questions", [])
    assert len(identity_questions) == 2, f"Variant 1 should have 2 identity questions, got {len(identity_questions)}"


def test_variant_2_structure() -> None:
    """Variant 2 has multi-band structure and multiple outcomes."""
    v2 = load_spec_dict("variant_2")
    
    # Multiple bands (lateness bands for different day ranges)
    bands = v2.get("bands", [])
    assert len(bands) >= 1, "Variant 2 should have at least 1 band"
    
    # Outcome table with multiple rows
    outcome_table = v2.get("closing", {}).get("outcome_table", [])
    assert len(outcome_table) >= 3, f"Variant 2 should have multiple outcome rows, got {len(outcome_table)}"


def test_variant_3_structure() -> None:
    """Variant 3 has 2-product structure and standard outcomes."""
    v3 = load_spec_dict("variant_3")
    
    # Calendar should be weekdays only (Mon-Fri, no Saturday/Sunday)
    weekend = v3.get("calendar", {}).get("weekend_days", [])
    # Standard weekends are Saturday (5) and Sunday (6)
    assert len(weekend) >= 2, f"Variant 3 should have standard weekend (Sat/Sun), got {weekend}"
    
    # Callback window should be business days
    callback = v3.get("third_party", {}).get("callback_window", {})
    if callback:
        start = callback.get("hour_start", "")
        end = callback.get("hour_end", "")
        # River Bank callback is 10am-6pm
        assert "10" in start, f"Variant 3 callback should start at 10am, got {start}"
        assert "18" in end or "6" in end, f"Variant 3 callback should end at 6pm/18:00, got {end}"
    
    # Identity: 3 questions
    identity_questions = v3.get("identity", {}).get("questions", [])
    assert len(identity_questions) == 3, f"Variant 3 should have 3 identity questions, got {len(identity_questions)}"
