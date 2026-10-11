"""Tests for builder/lint.py: each rule has a seeded error and a clean pass."""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from builder.lint import Finding, lint
from builder.tools_gen import build_tool, end_call_node, lifecycle, placeholder
from tests.conftest import REPO_ROOT

ALWAYS: dict[str, Any] = {"operator": "always"}


def _tool(name: str = "do_it", field: str = "item") -> dict[str, Any]:
    openapi = {
        "servers": [{"url": "https://h.test/v1"}],
        "paths": {
            "/" + name: {
                "post": {
                    "summary": "s",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "properties": {field: {"type": "string"}}, "required": [field]}
                            }
                        }
                    },
                }
            }
        },
    }
    return build_tool(openapi, "/" + name, description="CALL it when the caller gives the item.")


def clean() -> dict[str, Any]:
    """A small workflow that passes every rule with no findings."""
    return {
        "initial_node": "prep",
        "system_prompt": "Be brief.",
        "tools": [lifecycle(_tool("finish", "note"))],
        "nodes": {
            "prep": {
                "name": "prep",
                "node_type": "code",
                "passthrough": True,
                "passthrough_default_transition": "talk",
                "on_enter": {"type": "silent"},
                "pre_actions": [
                    {
                        "type": "function",
                        "function": {
                            "name": "code_exec",
                            "handler": "__function__:code_exec",
                            "parameters": {
                                "type": "object",
                                "properties": {"code": {"type": "string", "default": "vars['prepared'] = 'yes'\n"}},
                            },
                        },
                    }
                ],
                "transitions": [{"target_node": "talk", "description": "always", "condition": ALWAYS}],
            },
            "talk": {
                "name": "talk",
                "node_type": "conversation",
                "task_message": "Ask for the item. When given, call the tool do_it.",
                "tools": [_tool()],
                "extraction_schema": [
                    {"name": "when_date", "type": "date", "description": "The date", "min_confidence": 0.7}
                ],
                "max_node_turns": 10,
                "max_turn_transition": "bye",
                "transitions": [
                    {
                        "target_node": "bye",
                        "description": "done",
                        "condition": {
                            "logic": "or",
                            "conditions": [
                                {"variable": "do_it_response.data.ok", "operator": "equals", "operand": "true"},
                                {"variable": "do_it_response.data.ok", "operator": "equals", "operand": "True"},
                            ],
                        },
                    },
                    {"target_node": "talk", "description": "stay", "condition": ALWAYS},
                ],
            },
            "bye": end_call_node("bye"),
        },
    }


def rules(findings: list[Finding], severity: str | None = None) -> set[str]:
    return {f.rule for f in findings if severity is None or f.severity == severity}


def test_clean_workflow_has_no_findings() -> None:
    assert lint(clean()) == []


def _m_const(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["tools"][0]["function"]["parameters"]["properties"]["body"]["properties"]["item"]["const"] = "x"


def _m_tools_on_api_node(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["node_type"] = "api_request"
    w["nodes"]["prep"]["tools"] = [_tool("other")]


def _m_pre_actions_on_conversation(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["pre_actions"] = copy.deepcopy(w["nodes"]["prep"]["pre_actions"])


def _m_mapping_in_pre_action(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["pre_actions"][0]["response_mapping"] = [{"path": "data.a", "variable": "a"}]


def _m_mapping_path(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["tools"][0]["response_mapping"] = [{"path": "ok", "variable": "a"}]


def _m_one_spelling(w: dict[str, Any]) -> None:
    del w["nodes"]["talk"]["transitions"][0]["condition"]["conditions"][1]


def _m_and_group(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["transitions"][0]["condition"]["conditions"] = [
        {"logic": "and", "conditions": [{"variable": "v", "operator": "equals", "operand": "true"}]}
    ]


def _m_passthrough_no_default(w: dict[str, Any]) -> None:
    del w["nodes"]["prep"]["passthrough_default_transition"]


def _m_passthrough_no_always(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["transitions"][0]["condition"] = {"operator": "matches_intent", "description": "x"}


def _m_code_braces(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"] = "x = {'a': {'b': 1}}"


def _m_hook_braces(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["tools"][0]["pre_process_code"] = "x = '" + placeholder("v") + "'"


def _m_null(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["role_message"] = None


def _m_no_min_conf(w: dict[str, Any]) -> None:
    del w["nodes"]["talk"]["extraction_schema"][0]["min_confidence"]


def _m_date_as_string(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["extraction_schema"][0]["type"] = "string"


def _m_bad_target(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["transitions"][1]["target_node"] = "nowhere"


def _m_no_initial(w: dict[str, Any]) -> None:
    w["initial_node"] = "missing"


def _m_trapped(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["transitions"] = [{"target_node": "talk", "description": "stay", "condition": ALWAYS}]
    del w["nodes"]["talk"]["max_turn_transition"]


def _m_agent_config(w: dict[str, Any]) -> None:
    w["agent_config"] = {}


def _m_end_handler(w: dict[str, Any]) -> None:
    w["nodes"]["bye"]["terminal"]["function"]["handler"] = "__function__:api_call"


def _m_end_transitions(w: dict[str, Any]) -> None:
    w["nodes"]["bye"]["transitions"] = [{"target_node": "talk", "description": "x", "condition": ALWAYS}]


def _m_hook_on_code_exec(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["pre_actions"][0]["post_process_code"] = "pass"


def _m_template_body(w: dict[str, Any]) -> None:
    props = w["nodes"]["talk"]["tools"][0]["function"]["parameters"]["properties"]
    props["body"] = {"type": "object", "default": {"item": "x"}}
    props["item"] = {"type": "string", "enum": ["a"]}


def _m_leaf_no_default(w: dict[str, Any]) -> None:
    del w["nodes"]["talk"]["tools"][0]["function"]["parameters"]["properties"]["body"]["properties"]["item"]["default"]


def _m_duplicate_tool(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["tools"].append(_tool())


def _m_empty_lifecycle(w: dict[str, Any]) -> None:
    w["tools"][0]["config"]["alwaysRunsAt"] = []


def _m_dead_workflow_tool(w: dict[str, Any]) -> None:
    del w["tools"][0]["config"]["alwaysRunsAt"]


def _m_collision(w: dict[str, Any]) -> None:
    w["nodes"]["prep"]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"] = "vars['item'] = 'x'\n"


def _m_no_tool_mention(w: dict[str, Any]) -> None:
    w["nodes"]["talk"]["task_message"] = "Ask for the thing."


def _m_no_turn_transition(w: dict[str, Any]) -> None:
    del w["nodes"]["talk"]["max_turn_transition"]


MUTATIONS: list[tuple[str, str, str, Callable[[dict[str, Any]], None]]] = [
    ("no_const", "no_const", "error", _m_const),
    ("placement", "placement", "error", _m_tools_on_api_node),
    ("placement", "placement", "error", _m_pre_actions_on_conversation),
    ("response_mapping", "response_mapping", "error", _m_mapping_in_pre_action),
    ("response_mapping", "response_mapping", "error", _m_mapping_path),
    ("bool_spellings", "bool_spellings", "error", _m_one_spelling),
    ("bool_spellings", "bool_spellings", "error", _m_and_group),
    ("passthrough", "passthrough", "error", _m_passthrough_no_default),
    ("passthrough", "passthrough", "error", _m_passthrough_no_always),
    ("code_braces", "code_braces", "error", _m_code_braces),
    ("code_braces", "code_braces", "error", _m_hook_braces),
    ("no_null", "no_null", "error", _m_null),
    ("extraction", "extraction", "error", _m_no_min_conf),
    ("extraction", "extraction", "warn", _m_date_as_string),
    ("graph", "graph", "error", _m_bad_target),
    ("graph", "graph", "error", _m_no_initial),
    ("graph", "graph", "error", _m_trapped),
    ("agent_config", "agent_config", "error", _m_agent_config),
    ("end_call", "end_call", "error", _m_end_handler),
    ("end_call", "end_call", "error", _m_end_transitions),
    ("hooks_handler", "hooks_handler", "error", _m_hook_on_code_exec),
    ("body_fields", "body_fields", "error", _m_template_body),
    ("body_fields", "body_fields", "error", _m_leaf_no_default),
    ("tool_prompt", "tool_prompt", "warn", _m_no_tool_mention),
    ("turn_cap", "turn_cap", "warn", _m_no_turn_transition),
    ("tool_scope", "tool_scope", "error", _m_duplicate_tool),
    ("tool_scope", "tool_scope", "error", _m_empty_lifecycle),
    ("tool_scope", "tool_scope", "error", _m_dead_workflow_tool),
    ("placeholder_collision", "placeholder_collision", "warn", _m_collision),
]


@pytest.mark.parametrize(
    ("rule", "expected_rule", "severity", "mutate"), MUTATIONS, ids=[f"{r}-{m.__name__[3:]}" for r, _, _, m in MUTATIONS]
)
def test_seeded_error_is_found(rule: str, expected_rule: str, severity: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    w = clean()
    mutate(w)
    assert expected_rule in rules(lint(w), severity), [f.format() for f in lint(w)]


def test_rule_ids_cover_all_seventeen() -> None:
    assert len({r for r, _, _, _ in MUTATIONS}) == 17


def test_lifecycle_in_node_is_a_warning() -> None:
    w = clean()
    w["nodes"]["talk"]["tools"].append(lifecycle(_tool("late", "note")))
    found = lint(w)
    assert "tool_scope" in rules(found, "warn") and "tool_scope" not in rules(found, "error")


def test_single_true_leaf_condition_is_flagged() -> None:
    w = clean()
    w["nodes"]["talk"]["transitions"][0]["condition"] = {"variable": "v", "operator": "equals", "operand": "True"}
    assert "bool_spellings" in rules(lint(w), "error")


def test_global_node_previous_target_is_fine() -> None:
    w = clean()
    w["global_nodes"] = {
        "off": {
            "name": "off",
            "node_type": "conversation",
            "task_message": "Redirect.",
            "passthrough": True,
            "passthrough_default_transition": "__previous__",
            "condition": {"operator": "matches_intent", "description": "x"},
            "transitions": [{"target_node": "__previous__", "description": "always", "condition": ALWAYS}],
        }
    }
    assert lint(w) == []


def test_malformed_input_does_not_raise() -> None:
    assert lint([]) and lint([])[0].severity == "error"  # type: ignore[arg-type]
    findings = lint({"nodes": {"a": "text", "b": {"transitions": "x", "tools": 5}}, "tools": [1, None]})
    assert all(isinstance(f, Finding) for f in findings)


# ---------------------------------------------------------------------------------------------
# Real documents
# ---------------------------------------------------------------------------------------------

# examples/base-agent.json is an export of the platform's own template. Judged by the docs
# checklist it has null fields (an export artifact) and no end_call node (it hangs up through
# a lifecycle tool). Anything else would be a linter regression.
BASE_AGENT_EXPECTED_ERRORS: set[tuple[str, str]] = {
    ("no_null", "$.nodes.identity.max_turn_transition"),
    ("no_null", "$.nodes.identity.passthrough_default_transition"),
    ("no_null", "$.nodes.collection.max_turn_transition"),
    ("no_null", "$.nodes.collection.passthrough_default_transition"),
    ("graph", "nodes"),
}


def test_base_agent_errors_are_the_known_ones() -> None:
    wf = json.loads((REPO_ROOT / "examples" / "base-agent.json").read_text(encoding="utf-8"))
    errors = {(f.rule, f.path) for f in lint(wf) if f.severity == "error"}
    assert errors == BASE_AGENT_EXPECTED_ERRORS


def test_base_agent_without_export_artifacts_has_no_other_errors() -> None:
    wf = json.loads((REPO_ROOT / "examples" / "base-agent.json").read_text(encoding="utf-8"))
    for node in wf["nodes"].values():
        for key in [k for k, v in node.items() if v is None]:
            del node[key]
    wf["nodes"]["bye"] = end_call_node("bye")
    wf["nodes"]["collection"]["transitions"].append(
        {"target_node": "bye", "description": "end", "condition": {"operator": "always"}}
    )
    wf["nodes"]["identity"]["transitions"].append(
        {"target_node": "bye", "description": "end", "condition": {"operator": "always"}}
    )
    assert [f.format() for f in lint(wf) if f.severity == "error"] == []


def _complete_example() -> dict[str, Any] | None:
    text = (REPO_ROOT / "docs" / "workflow-format.md").read_text(encoding="utf-8")
    m = re.search(r"^## Complete example\s*\n.*?```json\n(.*?)\n```", text, re.DOTALL | re.MULTILINE)
    if m is None:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def test_docs_complete_example_has_no_errors() -> None:
    wf = _complete_example()
    if wf is None:
        pytest.skip("complete example could not be parsed")
    assert [f.format() for f in lint(wf) if f.severity == "error"] == []


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------


def _cli(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "builder.lint", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_exit_codes_and_export_wrapper(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "x", "workflow": clean()}), encoding="utf-8")
    r = _cli(good)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 error(s)" in r.stdout
    bad_wf = clean()
    bad_wf["agent_config"] = {}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(bad_wf), encoding="utf-8")
    r = _cli(bad)
    assert r.returncode == 1
    assert "agent_config" in r.stdout
    assert _cli(tmp_path / "missing.json").returncode == 2


# ---------------------------------------------------------------------------------------------
# Pipeline stage
# ---------------------------------------------------------------------------------------------


def _run_with_workflow(tmp_path: Path, workflow: dict[str, Any] | None) -> tuple[int, str]:
    from builder.pipeline import BuildContext, run_build

    brief = tmp_path / "b.md"
    brief.write_text("x", encoding="utf-8")
    api = tmp_path / "api.json"
    api.write_text('{"paths": {}}', encoding="utf-8")

    def stub_extract(ctx: BuildContext) -> str:
        ctx.spec = {"stub": True}
        return "stub"

    def stub_compile(ctx: BuildContext) -> str:
        ctx.workflow = workflow
        return "stub"

    code = run_build(
        REPO_ROOT, [brief], api, "lint-stage", True, None,
        runs_dir=tmp_path / "runs", stages={"extract_spec": stub_extract, "validate_spec": lambda ctx: "stub", "compile_workflow": stub_compile},
    )
    logs = sorted((tmp_path / "runs").iterdir())
    return code, (logs[0] / "trace.log").read_text(encoding="utf-8")


def test_lint_stage_logs_findings_without_blocking(tmp_path: Path) -> None:
    bad = clean()
    bad["agent_config"] = {}
    bad["nodes"]["talk"]["task_message"] = "Ask."
    code, text = _run_with_workflow(tmp_path, bad)
    assert code == 0
    assert "lint: ok: 1 error(s), 1 warning(s)" in text
    assert "lint: ERROR agent_config" in text
    assert "lint: WARN  tool_prompt" in text


def test_lint_stage_clean_workflow(tmp_path: Path) -> None:
    code, text = _run_with_workflow(tmp_path, clean())
    assert code == 0
    assert "lint: ok: 0 error(s), 0 warning(s)" in text


def test_lint_stage_without_workflow_is_skipped(tmp_path: Path) -> None:
    code, text = _run_with_workflow(tmp_path, None)
    assert code == 0
    assert "lint: skipped" in text


def _with_task(text: str) -> list[Finding]:
    w = clean()
    w["nodes"]["talk"]["task_message"] = text + " Use the tool."
    return lint(w)


def test_repeat_counts_conflict_is_warned() -> None:
    found = _with_task("Offer the reduced amount once. Offer the reduced amount to the customer twice.")
    assert rules(found, "warn") == {"repeat_counts"}


def test_repeat_counts_same_count_or_other_topic_is_quiet() -> None:
    assert _with_task("Offer the reduced amount twice. Offer the reduced amount twice.") == []
    assert _with_task("Offer the reduced amount once. Ask for the security answer twice.") == []
