"""compile_fallback / emergency_workflow: spec-independent workflows that always lint clean."""

from __future__ import annotations

import json

from builder import lint, tools_gen
from builder.compiler import compile_fallback, emergency_workflow
from tests.conftest import REPO_ROOT

OPENAPI = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
SOURCES = [("a.md", "first paragraph\n\nsecond paragraph\n"), ("b.md", "layer paragraph {" + "{x}" + "}")]


def _errors(wf: dict) -> list[str]:
    return [f.format() for f in lint.lint(wf) if f.severity == "error"]


def _no_none(wf: dict) -> bool:
    return all(v is not None for _p, v in lint._walk(wf, "$"))


def test_fallback_lints_clean_and_has_no_none() -> None:
    wf, report = compile_fallback(SOURCES, OPENAPI)
    assert _errors(wf) == []
    assert _no_none(wf)
    assert "agent_config" not in wf
    assert any(w.startswith("FALLBACK") for w in report.warnings)
    assert report.to_dict().keys() == {"warnings", "gaps", "to_verify", "written_vars", "lint"}


def test_fallback_shape_and_brief_paragraphs() -> None:
    wf, _ = compile_fallback(SOURCES, OPENAPI)
    node = wf["nodes"][wf["initial_node"]]
    assert node["node_type"] == "conversation"
    task = node["task_message"]
    for para in ("first paragraph", "second paragraph", "layer paragraph"):
        assert para in task
    assert "{" + "{" not in task
    assert any(t["target_node"] == "end_fallback" and t["is_tool_call"] for t in node["transitions"])
    assert wf["nodes"]["end_fallback"]["node_type"] == "end_call"


def test_fallback_has_every_operation_as_a_node_tool() -> None:
    wf, _ = compile_fallback(SOURCES, OPENAPI)
    names = {t["function"]["name"] for t in wf["nodes"][wf["initial_node"]]["tools"]}
    assert names == set(tools_gen.tool_names(OPENAPI))
    assert len(names) == 7
    assert "close_call" in names


def test_fallback_without_openapi_has_no_tools() -> None:
    wf, _ = compile_fallback(SOURCES, {})
    assert wf["nodes"][wf["initial_node"]]["tools"] == []
    assert _errors(wf) == []


def test_emergency_workflow_lints_clean() -> None:
    wf = emergency_workflow()
    assert _errors(wf) == []
    assert _no_none(wf)
    assert wf["initial_node"] in wf["nodes"]
    assert wf["nodes"][wf["initial_node"]]["tools"] == []
