"""Tests for builder/prompts.py with the Brief A and A+B spec fixtures."""

from __future__ import annotations

import dataclasses

from builder.prompts import (
    render_mandated_sentences,
    render_outcome_table,
    render_tool_rules,
    render_verbatim_rules,
)
from builder.spec import PolicySpec, load_spec
from tests.conftest import load_fixture

SPEC_A: PolicySpec = load_spec(load_fixture("spec_brief_a.json"))
SPEC_AB: PolicySpec = load_spec(load_fixture("spec_brief_ab.json"))


def _bare(spec: PolicySpec) -> PolicySpec:
    closing = dataclasses.replace(spec.closing, outcome_table=[])
    return dataclasses.replace(spec, verbatim_rules=[], mandated_sentences=[], closing=closing)


def test_verbatim_rules_numbered_per_stage() -> None:
    for spec in (SPEC_A, SPEC_AB):
        stage = "negotiate"
        want = [r.text for r in spec.verbatim_rules if r.stage == stage]
        assert len(want) >= 2
        lines = render_verbatim_rules(spec, stage).splitlines()
        assert stage in lines[0]
        assert lines[1:] == [f"{i}. {t}" for i, t in enumerate(want, start=1)]


def test_verbatim_rules_differ_between_layers_and_unknown_stage_is_empty() -> None:
    assert render_verbatim_rules(SPEC_AB, "negotiate") != render_verbatim_rules(SPEC_A, "negotiate")
    assert render_verbatim_rules(SPEC_A, "no_such_stage") == ""


def test_mandated_sentences_all_and_filtered() -> None:
    text = render_mandated_sentences(SPEC_A)
    for m in SPEC_A.mandated_sentences:
        assert m.text in text and m.when in text
    one = SPEC_A.mandated_sentences[0]
    filtered = render_mandated_sentences(SPEC_A, when=one.when)
    assert one.text in filtered
    assert all(m.text not in filtered for m in SPEC_A.mandated_sentences if m.when != one.when)
    assert render_mandated_sentences(SPEC_A, when="never happens") == ""


def test_outcome_table_has_one_row_per_entry() -> None:
    for spec in (SPEC_A, SPEC_AB):
        lines = render_outcome_table(spec).splitlines()
        rows = spec.closing.outcome_table
        assert len(lines) == len(rows) + 1
        for line, row in zip(lines[1:], rows):
            assert row.outcome in line and row.when in line
            assert (row.reason or "none") in line


def test_empty_spec_renders_nothing() -> None:
    bare = _bare(SPEC_A)
    assert render_verbatim_rules(bare, "negotiate") == ""
    assert render_mandated_sentences(bare) == ""
    assert render_outcome_table(bare) == ""


def test_tool_rules_are_generic() -> None:
    text = render_tool_rules()
    low = text.lower()
    assert "one tool per message" in low
    assert "after every tool result" in low
    assert "call the tool" in low
    assert "ok true" in low
    assert text == render_tool_rules()
