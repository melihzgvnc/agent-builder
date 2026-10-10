from __future__ import annotations

from builder.readback import diff_workflows


def test_identical_and_stored_extras_ignored() -> None:
    sent = {"a": 1, "n": {"x": "y"}, "l": [1, 2]}
    stored = {"a": 1, "n": {"x": "y", "default": True}, "l": [1, 2], "extra": 5}
    assert diff_workflows(sent, stored) == []


def test_stripped_changed_length() -> None:
    sent = {"a": 1, "gone": 2, "n": {"x": "y"}, "l": [1, 2, 3]}
    stored = {"a": 9, "n": {"x": "z"}, "l": [1, 2]}
    out = diff_workflows(sent, stored)
    assert "stripped gone" in out
    assert "changed a: 1 -> 9" in out
    assert "changed n.x: 'y' -> 'z'" in out
    assert "length l: 3 -> 2" in out


def test_values_truncated_and_limit() -> None:
    out = diff_workflows({"a": "x" * 200}, {"a": "y"})
    assert len(out[0]) < 160
    sent = {f"k{i}": i for i in range(10)}
    out = diff_workflows(sent, {}, limit=3)
    assert len(out) == 4 and out[-1] == "... 7 more"
