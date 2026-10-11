"""Trace capture: console tee, UTC+local times, RUN SUMMARY and secret redaction."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from builder.tee import capture_console, redact
from builder.trace import Trace, format_run_summary
from tests.conftest import copy_builder_tree
from tests.online_fakes import FakeBank
from tests.test_pipeline_online import StageFreya, build, make_world

SECRETS = {
    "FREYA_API_KEY": "fk-live-AAAA1111secret",
    "FREYA_BANK_TEAM_KEY": "team-BBBB2222secret",
    "ANTHROPIC_API_KEY": "sk-ant-CCCC3333secret",
}


@pytest.fixture(autouse=True)
def fake_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)


def test_redact_masks_each_secret_and_ignores_short_values() -> None:
    text = " ".join(SECRETS.values()) + " abc"
    out = redact(text)
    assert not any(v in out for v in SECRETS.values())
    assert redact("abc", {"FREYA_API_KEY": "abc"}) == "abc"


def test_full_run_trace_has_everything_and_no_secret(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    client = StageFreya(world)
    with capture_console(tmp_path / "console.log"):
        print("hello console", SECRETS["FREYA_API_KEY"])
        print("err " + SECRETS["ANTHROPIC_API_KEY"], file=sys.stderr)
        code, run_dir = build(tmp_path, client, FakeBank(world))
    assert code == 0
    log = (run_dir / "trace.log").read_text(encoding="utf-8")
    jsonl = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    for v in SECRETS.values():
        assert v not in log and v not in jsonl
        assert v not in (tmp_path / "console.log").read_text(encoding="utf-8")
    assert "== CONSOLE ==" in log and "hello console ***" in log and "err ***" in log
    assert "(utc | local):" in log and "Z | local " in log
    assert log.count("RUN SUMMARY") == 1
    tail = log[log.index("== RUN SUMMARY =="):log.index("== CONSOLE ==")]
    for name in ("workflow.json", "trace.log", "trace.jsonl", "slot-a.workflow.json"):
        assert name in tail
    assert "elapsed: " in tail
    assert " | " in log and "workflow_id=wf_1 agent_id=ag_1" in log and "slot=slot-a" in log
    assert "exported workflow_id=wf_1" in log and "path=" in log
    assert "case " in log and "ledger=True conversation=True" in log


def test_events_are_redacted(tmp_path: Path) -> None:
    root = copy_builder_tree(tmp_path / "root")
    t = Trace(tmp_path / "run", root)
    t.warn("leaked " + SECRETS["FREYA_BANK_TEAM_KEY"])
    t.finish(0)
    assert SECRETS["FREYA_BANK_TEAM_KEY"] not in (tmp_path / "run" / "trace.jsonl").read_text(encoding="utf-8")
    assert SECRETS["FREYA_BANK_TEAM_KEY"] not in (tmp_path / "run" / "trace.log").read_text(encoding="utf-8")


def test_format_run_summary_without_export(tmp_path: Path) -> None:
    lines = format_run_summary(tmp_path, 1.5, 0, "ok")
    assert any(l == "export: (not exported)" for l in lines)
    assert any(l == "elapsed: 1.5s" for l in lines)
    assert json.dumps(lines)
