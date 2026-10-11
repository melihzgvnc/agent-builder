"""Compiler: structure, mutation, gaps, close tool resolution and end-to-end scenarios (no network)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from builder import compiler, hooks, tools_gen
from builder.compiler import CompileError, CompileReport, compile_workflow
from builder.engine import output_names
from builder.snippets import check_snippet
from builder.spec import load_spec, load_spec_file
from builder.tools_gen import placeholder
from tests.conftest import FIXTURES_DIR, REPO_ROOT, load_fixture
from tests.hook_harness import run_hook
from tests.snippet_harness import run_snippet
from tests.test_engine import PUB_PRODUCTS

OPENAPI: dict[str, Any] = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
SPEC_FILES: dict[str, str] = {"A": "spec_brief_a.json", "AB": "spec_brief_ab.json"}
NOW = "2026-10-14T11:00"


def spec_of(key: str) -> Any:
    return load_spec_file(FIXTURES_DIR / SPEC_FILES[key])


def compiled(key: str) -> tuple[dict, CompileReport]:
    return compile_workflow(spec_of(key), copy.deepcopy(OPENAPI))


def spec_dict(key: str) -> dict[str, Any]:
    data = load_fixture(SPEC_FILES[key])
    return data


def compile_dicts(spec_d: dict[str, Any], api: dict[str, Any] | None = None) -> tuple[dict, CompileReport]:
    spec_d = copy.deepcopy(spec_d)
    spec_d.pop("coverage", None)  # coverage paths do not survive edits
    return compile_workflow(load_spec(spec_d), copy.deepcopy(api if api is not None else OPENAPI))


def walk(value: Any, path: str = "$") -> Any:
    yield path, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{path}[{i}]")


def node_tools(wf: dict, node: str) -> dict[str, dict]:
    return {t["function"]["name"]: t for t in wf["nodes"][node].get("tools", [])}


def code_of(node: dict) -> str:
    return node["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]


def create_promise_schema(api: dict[str, Any]) -> dict[str, Any]:
    return api["paths"]["/create_promise"]["post"]["requestBody"]["content"]["application/json"]["schema"]


# ---------------------------------------------------------------------------------------------
# structure (both fixture specs)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["A", "AB"])
def test_zero_lint_errors_no_none_no_agent_config(key: str) -> None:
    wf, report = compiled(key)
    errors = [x for x in report.lint if x.startswith("ERROR")]
    assert not errors, "\n".join(report.lint)
    assert "agent_config" not in wf and "test_variables" not in wf
    assert [p for p, v in walk(wf) if v is None] == []
    assert set(wf) == {"initial_node", "system_prompt", "nodes", "global_nodes", "tools"}


@pytest.mark.parametrize("key", ["A", "AB"])
def test_nodes_and_edges(key: str) -> None:
    wf, _ = compiled(key)
    nodes = wf["nodes"]
    first = nodes[wf["initial_node"]]
    assert first["node_type"] == "conversation" and first["on_enter"]["type"] == "tts"
    assert first["on_enter_first_entry_only"] is True
    for name in ("identity", "get_account", "facts", "negotiate", "finalize", "finalize_forced", "end_final",
                 "exit_backend_failure", "end_backend_failure"):
        assert name in nodes
    for name, node in nodes.items():
        assert node["name"] == name
        if node["node_type"] == "conversation":
            assert isinstance(node["max_node_turns"], int) and node["max_turn_transition"] in nodes
        targets = [t["target_node"] for t in node.get("transitions", [])]
        targets += [node[k] for k in ("passthrough_default_transition", "max_turn_transition") if k in node]
        assert all(t in nodes for t in targets), (name, targets)
        for t in node.get("transitions", []):
            if t.get("is_tool_call"):
                assert t["target_node"] in nodes
    terminals = [n["terminal"]["function"]["name"] for n in nodes.values() if n["node_type"] == "end_call"]
    assert len(terminals) == len(set(terminals)) and "end_final" in terminals
    for name, node in nodes.items():  # exit / end pairs
        if name.startswith("exit_"):
            assert node["node_type"] == "code" and node["passthrough"]
    ends = [n for n, v in nodes.items() if v["node_type"] == "end_call"]
    assert set(ends) == {"end_final", "end_final_single", "end_final_plural", "end_backend_failure"} | {
        "end_" + n[len("exit_"):] for n in nodes if n.startswith("exit_") and n != "exit_backend_failure"
    }


@pytest.mark.parametrize("key", ["A", "AB"])
def test_tool_transitions_are_unique_per_target(key: str) -> None:
    wf, _ = compiled(key)
    for name, node in wf["nodes"].items():
        targets = [t["target_node"] for t in node.get("transitions", []) if t.get("is_tool_call")]
        assert len(targets) == len(set(targets)) or node["node_type"] != "conversation", name


@pytest.mark.parametrize("key", ["A", "AB"])
def test_close_tool_is_workflow_level_lifecycle(key: str) -> None:
    wf, _ = compiled(key)
    assert len(wf["tools"]) == 1
    tool = wf["tools"][0]
    assert tool["function"]["name"] == "close_call"
    assert tool["config"]["alwaysRunsAt"] == ["end_of_call"] and tool["config"]["maxRetries"] == 0
    assert tool["pre_process_code"]
    body = tool["function"]["parameters"]["properties"]["body"]["properties"]
    assert body["outcome"]["default"] == placeholder("final_outcome")
    assert body["reason"]["default"] == placeholder("final_reason")
    assert body["outcome"]["enum"] == OPENAPI["paths"]["/close_call"]["post"]["requestBody"]["content"][
        "application/json"]["schema"]["properties"]["outcome"]["enum"]


def test_note_tool_only_for_the_multi_spec() -> None:
    wf_a, _ = compiled("A")
    wf_ab, _ = compiled("AB")
    assert "add_note" not in node_tools(wf_a, "negotiate") and "add_note" in node_tools(wf_ab, "negotiate")
    assert set(node_tools(wf_a, "negotiate")) == {"create_promise", "transfer_to_agent"}
    assert set(node_tools(wf_ab, "negotiate")) == {"create_promise", "add_note", "transfer_to_agent"}
    assert set(node_tools(wf_a, "identity")) == {"verify_identity", "schedule_callback", "transfer_to_agent"}
    assert "finalize_early" in wf_a["nodes"]
    # the multi spec has no exit for closes that become notes
    assert "exit_paid" in wf_a["nodes"] and "exit_paid" not in wf_ab["nodes"]
    assert "exit_no_promise_dispute" in wf_a["nodes"] and "exit_no_promise_dispute" not in wf_ab["nodes"]


@pytest.mark.parametrize("key", ["A", "AB"])
def test_queue_enum_and_customer_placeholder_follow_inputs(key: str) -> None:
    wf, _ = compiled(key)
    spec = spec_of(key)
    enum = OPENAPI["paths"]["/transfer_to_agent"]["post"]["requestBody"]["content"]["application/json"][
        "schema"]["properties"]["queue"]["enum"]
    for node in ("identity", "negotiate"):
        tool = node_tools(wf, node).get("transfer_to_agent")
        if tool:
            assert tool["function"]["parameters"]["properties"]["body"]["properties"]["queue"]["enum"] == enum
    api_tools = [t for _, t in walk(wf) if isinstance(t, dict) and t.get("function", {}).get("handler") == tools_gen.API_CALL_HANDLER]
    assert api_tools
    for tool in api_tools:
        props = tool["function"]["parameters"]["properties"]["body"]["properties"]
        assert props["customer_id"]["default"] == placeholder(spec.call_variables.customer_id)


@pytest.mark.parametrize("key", ["A", "AB"])
def test_no_default_collisions_and_written_vars(key: str) -> None:
    wf, report = compiled(key)
    spec = spec_of(key)
    for _, tool in walk(wf):
        if isinstance(tool, dict) and tool.get("function", {}).get("handler") == tools_gen.API_CALL_HANDLER:
            assert tools_gen.check_default_collisions(tool, report.written_vars) == []
    from builder.spec import spec_to_dict
    io = {"products_var": "account_products", "products_flat_prefix": "x", "out_prefix": ""}
    assert set(output_names(spec_to_dict(spec), io)) <= set(report.written_vars)
    for var in ("identity_state", "promise_state", "closing_ready", "final_outcome", "final_reason",
                "transfer_reason", "identity_all_verified", "p1_promise", "p1_note"):
        assert var in report.written_vars
    n = 3 if key == "AB" else 1
    assert (f"p{n}_promise" in report.written_vars) and (f"p{n + 1}_promise" not in report.written_vars)
    assert len(report.to_verify) == 13 and report.to_verify[0].startswith("1.")
    assert set(report.to_dict()) == {"warnings", "gaps", "to_verify", "written_vars", "lint", "degraded"}


@pytest.mark.parametrize("key", ["A", "AB"])
def test_identity_transition_tests_both_spellings(key: str) -> None:
    wf, _ = compiled(key)
    first = wf["nodes"]["identity"]["transitions"][1]
    assert first["target_node"] == "get_account"
    leaves = {(c["variable"], c["operand"]) for c in first["condition"]["conditions"]}
    for var in ("verify_identity_response.data.all_verified", "identity_all_verified"):
        assert (var, "true") in leaves and (var, "True") in leaves
    assert ("identity_state", "verified") in leaves
    assert first["condition"]["logic"] == "or"
    verify = node_tools(wf, "identity")["verify_identity"]
    assert verify["response_mapping"] == [{"path": "data.all_verified", "variable": "identity_all_verified"}]


@pytest.mark.parametrize("key", ["A", "AB"])
def test_account_node_has_no_mapping_and_two_retries(key: str) -> None:
    wf, _ = compiled(key)
    account = wf["nodes"]["get_account"]
    assert "response_mapping" not in account and account["node_type"] == "api_request"
    tool = account["pre_actions"][0]
    assert tool["config"]["maxRetries"] == 2 and tool["function"]["name"] == "get_account"
    for path, value in walk(wf):
        if path.endswith("response_mapping") and isinstance(value, list):
            assert all(m["path"] == "data.all_verified" for m in value), path
    assert wf["nodes"]["facts"]["response_mapping"] == []
    failure = account["transitions"][0]["condition"]["conditions"]
    assert any(c["variable"] == "get_account_failure_count" for c in failure)
    assert {c["operand"] for c in failure if c["variable"] == "get_account_response.data.ok"} == {"false", "False"}


@pytest.mark.parametrize("key", ["A", "AB"])
def test_generated_sources_follow_the_platform_rules(key: str) -> None:
    wf, _ = compiled(key)
    sources: list[str] = []
    for _, v in walk(wf):
        if isinstance(v, dict):
            for k in ("pre_process_code", "post_process_code"):
                if isinstance(v.get(k), str):
                    sources.append(v[k])
    for node in wf["nodes"].values():
        if node["node_type"] == "code":
            sources.append(code_of(node))
    assert len(sources) >= 12
    for src in sources:
        hooks.check_source(src) if "def _facts_engine" not in src else check_snippet(src)
    facts = code_of(wf["nodes"]["facts"])
    check_snippet(facts)


def test_closing_sources_and_exit_nodes() -> None:
    wf, _ = compiled("A")
    gated = wf["nodes"]["exit_not_reached_callback_gated"]
    assert [t["target_node"] for t in gated["transitions"]] == ["end_not_reached_callback_gated", "identity"]
    assert "callback_state" in code_of(gated)
    spoken = wf["nodes"]["end_not_verified_failed_say"]["terminal"]["function"]["parameters"]["properties"]
    spec = spec_of("A")
    assert spoken["message_mode"]["default"] == "custom"
    assert spoken["spoken_message"]["default"] == spec.identity.on_failure.say + " " + spec.closing.goodbye_before_close.text
    declined = wf["nodes"]["end_not_verified_declined"]["terminal"]["function"]["parameters"]["properties"]
    assert declined["message_mode"]["default"] == "custom"
    assert declined["spoken_message"]["default"] == spec.closing.goodbye_before_close.text
    assert wf["nodes"]["identity"]["transitions"][0]["target_node"] == "exit_not_verified_failed_say"
    assert wf["nodes"]["negotiate"]["transitions"][1]["target_node"] == "exit_no_promise_service_error_say"
    assert wf["nodes"]["identity"]["max_turn_transition"] == "exit_not_verified_declined"
    assert wf["nodes"]["negotiate"]["max_turn_transition"] == "finalize_forced"


def test_task_messages_name_existing_transition_tools() -> None:
    wf, _ = compiled("A")
    for node in ("identity", "negotiate"):
        names = {t["target_node"] for t in wf["nodes"][node]["transitions"] if t.get("is_tool_call")}
        text = wf["nodes"][node]["task_message"]
        called = {w.strip(".,;()\"") for w in text.split() if w.startswith("transition_to_")}
        assert called, node
        for tool in called:
            assert tool[len("transition_to_"):] in names, (node, tool)
    # the gated callback exit and no-time exit are reachable tools
    ident = wf["nodes"]["identity"]
    tools = {t["target_node"] for t in ident["transitions"] if t.get("is_tool_call")}
    assert {"exit_not_reached_callback_gated", "exit_not_reached_no_callback", "finalize_early"} <= tools


# ---------------------------------------------------------------------------------------------
# switched only by spec flags
# ---------------------------------------------------------------------------------------------


def test_compiler_sources_mention_no_brief() -> None:
    for name in ("compiler.py", "node_prompts.py"):
        text = (REPO_ROOT / "builder" / name).read_text(encoding="utf-8").lower()
        assert "brief" not in text, name


def test_multi_behaviour_is_switched_by_spec_flags_only() -> None:
    a, ab = spec_dict("A"), spec_dict("AB")
    on = copy.deepcopy(a)
    for key in ("multi_product", "debt_statement", "closing"):
        on[key] = copy.deepcopy(ab[key])
    wf_on, _ = compile_dicts(on)
    assert "add_note" in node_tools(wf_on, "negotiate")
    off = copy.deepcopy(ab)
    off["multi_product"] = {"enabled": False, "max_products": 1}
    wf_off, report = compile_dicts(off)
    assert "add_note" not in node_tools(wf_off, "negotiate")
    assert "exit_paid" in wf_off["nodes"]
    assert not [x for x in report.lint if x.startswith("ERROR")]
    # promise tool config follows max_products
    src = node_tools(wf_on, "negotiate")["create_promise"]["pre_process_code"]
    assert "N = 3" in src and "MULTI = True" in src
    assert "N = 1" in node_tools(wf_off, "negotiate")["create_promise"]["pre_process_code"]


# ---------------------------------------------------------------------------------------------
# mutation: names come from the inputs
# ---------------------------------------------------------------------------------------------


def test_mutated_inputs_are_followed() -> None:
    spec_d = spec_dict("AB")
    spec_d["call_variables"]["customer_id"] = "cust_ref"
    spec_d["call_variables"]["last_name"] = "surname"
    spec_d["tools"]["create_promise"]["field_map"]["amount"] = "pay_amount"
    api = copy.deepcopy(OPENAPI)
    schema = create_promise_schema(api)
    schema["properties"]["pay_amount"] = schema["properties"].pop("amount")
    schema["required"] = ["pay_amount" if r == "amount" else r for r in schema["required"]]
    api_text = json.dumps(api).replace('"service_error"', '"svc_fail"')
    spec_text = json.dumps(spec_d).replace('"service_error"', '"svc_fail"')
    wf, report = compile_dicts(json.loads(spec_text), json.loads(api_text))
    dump = json.dumps(wf)
    assert placeholder("cust_ref") in dump and placeholder("customer_id") not in dump
    assert placeholder("surname") in wf["nodes"]["identity"]["on_enter"]["text"]
    assert placeholder("last_name") not in dump
    props = node_tools(wf, "negotiate")["create_promise"]["function"]["parameters"]["properties"]["body"]["properties"]
    assert "pay_amount" in props and "amount" not in props
    pre = node_tools(wf, "negotiate")["create_promise"]["pre_process_code"]
    assert "'pay_amount'" in pre and "F_AMT = 'amount'" not in pre
    assert "svc_fail" in dump and "service_error" not in dump
    enum = node_tools(wf, "negotiate")["add_note"]["function"]["parameters"]["properties"]["body"]["properties"]["reason"]["enum"]
    assert "svc_fail" in enum and "service_error" not in enum
    assert not [x for x in report.lint if x.startswith("ERROR")], report.lint


# ---------------------------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("section, text", [("identity", "identity"), ("amounts", "amounts"), ("call_variables", "call_variables")])
def test_missing_required_sections_raise(section: str, text: str) -> None:
    spec_d = spec_dict("A")
    spec_d.pop(section)
    with pytest.raises(CompileError, match=text):
        compile_dicts(spec_d)


@pytest.mark.parametrize("op", ["verify_identity", "get_account", "create_promise", "close_call"])
def test_missing_required_operation_raises(op: str) -> None:
    api = copy.deepcopy(OPENAPI)
    del api["paths"]["/" + op]
    with pytest.raises(CompileError):
        compile_workflow(spec_of("A"), api)


def test_missing_required_field_raises() -> None:
    api = copy.deepcopy(OPENAPI)
    schema = create_promise_schema(api)
    schema["properties"].pop("date")
    schema["required"].remove("date")
    with pytest.raises(CompileError, match="date"):
        compile_workflow(spec_of("A"), api)


@pytest.mark.parametrize("op, check", [
    ("add_note", lambda wf: "add_note" not in node_tools(wf, "negotiate")),
    ("schedule_callback", lambda wf: "schedule_callback" not in node_tools(wf, "identity")
        and "exit_not_reached_callback_gated" not in wf["nodes"]),
    ("transfer_to_agent", lambda wf: "transfer_to_agent" not in node_tools(wf, "negotiate")
        and "finalize_early" not in wf["nodes"]),
])
def test_missing_optional_operation_drops_the_feature(op: str, check: Any) -> None:
    api = copy.deepcopy(OPENAPI)
    del api["paths"]["/" + op]
    wf, report = compile_workflow(spec_of("AB"), api)
    assert check(wf)
    assert any(op in w for w in report.warnings)
    assert not [x for x in report.lint if x.startswith("ERROR")], report.lint


def test_dropped_notes_turn_note_closes_into_exits() -> None:
    api = copy.deepcopy(OPENAPI)
    del api["paths"]["/add_note"]
    wf, _ = compile_workflow(spec_of("AB"), api)
    assert "exit_paid" in wf["nodes"] and "exit_no_promise_dispute" in wf["nodes"]
    assert "add_note" not in node_tools(wf, "negotiate")


def test_missing_failure_outcomes_are_gaps_not_crashes() -> None:
    spec_d = spec_dict("A")
    spec_d["identity"].pop("on_failure")
    spec_d["promise_rules"].pop("on_failure")
    wf, report = compile_dicts(spec_d)
    assert len([g for g in report.gaps if "on_failure" in g]) == 2
    assert wf["nodes"]["identity"]["transitions"][0]["target_node"] == "finalize_forced"
    assert wf["nodes"]["negotiate"]["transitions"][1]["target_node"] == "finalize_forced"
    assert not [x for x in report.lint if x.startswith("ERROR")], report.lint


def test_missing_opening_falls_back_to_model_opening_with_a_gap() -> None:
    spec_d = spec_dict("A")
    spec_d.pop("opening")
    wf, report = compile_dicts(spec_d)
    assert wf["nodes"]["identity"]["on_enter"] == {"type": "llm"}
    assert any("opening" in g for g in report.gaps)


def test_unmapped_queue_is_a_gap() -> None:
    spec_d = spec_dict("A")
    for intent in spec_d["intents"]:
        intent["actions"] = [a for a in intent["actions"] if not (a["type"] == "close" and a.get("reason") == "restructuring")]
    wf, report = compile_dicts(spec_d)
    assert any("restructuring" in g for g in report.gaps)
    assert "transfer_to_agent" in node_tools(wf, "negotiate")


# ---------------------------------------------------------------------------------------------
# close tool resolution
# ---------------------------------------------------------------------------------------------


def renamed_close_api(new: str) -> dict[str, Any]:
    api = copy.deepcopy(OPENAPI)
    api["paths"]["/" + new] = api["paths"].pop("/close_call")
    return api


def test_close_tool_from_the_tools_mapping() -> None:
    wf, report = compiled("A")
    assert wf["tools"][0]["function"]["name"] == "close_call"
    assert not any("does not name the close tool" in w for w in report.warnings)


def test_close_tool_from_closing_close_tool() -> None:
    spec_d = spec_dict("A")
    spec_d["tools"].pop("close_call")
    spec_d["closing"]["close_tool"] = "end_the_call"
    wf, report = compile_dicts(spec_d, renamed_close_api("end_the_call"))
    assert wf["tools"][0]["function"]["name"] == "end_the_call"
    assert wf["tools"][0]["function"]["parameters"]["properties"]["url"]["default"].endswith("/end_the_call")
    assert not [x for x in report.lint if x.startswith("ERROR")], report.lint


def test_close_tool_tools_key_with_missing_operation_uses_closing_close_tool() -> None:
    spec_d = spec_dict("A")
    spec_d["closing"]["close_tool"] = "end_the_call"
    wf, _ = compile_dicts(spec_d, renamed_close_api("end_the_call"))
    assert wf["tools"][0]["function"]["name"] == "end_the_call"


def test_close_tool_single_fallback_warns() -> None:
    spec_d = spec_dict("A")
    spec_d["tools"].pop("close_call")
    wf, report = compile_dicts(spec_d)
    assert wf["tools"][0]["function"]["name"] == "close_call"
    assert any("does not name the close tool" in w for w in report.warnings)


def test_close_tool_unknown_name_warns_then_falls_back() -> None:
    spec_d = spec_dict("A")
    spec_d["tools"].pop("close_call")
    spec_d["closing"]["close_tool"] = "not_an_operation"
    wf, report = compile_dicts(spec_d)
    assert wf["tools"][0]["function"]["name"] == "close_call"
    assert any("not_an_operation" in w for w in report.warnings)


def test_close_tool_none_raises() -> None:
    spec_d = spec_dict("A")
    spec_d["tools"].pop("close_call")
    with pytest.raises(CompileError, match="no close tool"):
        compile_dicts(spec_d, renamed_close_api("something_else"))


def test_role_constants_match_the_schema() -> None:
    schema = json.loads((REPO_ROOT / "builder" / "spec_schema.json").read_text(encoding="utf-8"))
    assert set(compiler.ROLES) == set(schema["$defs"]["tools"]["properties"])


# ---------------------------------------------------------------------------------------------
# scenarios: facts snippet, then the hooks in order
# ---------------------------------------------------------------------------------------------


class Call:
    """One simulated call: runs the compiled facts code and the compiled hooks over a vars dict."""

    def __init__(self, key: str, products: list[dict[str, Any]], now: str = NOW) -> None:
        self.spec = spec_of(key)
        self.wf, self.report = compiled(key)
        self.vars: dict[str, Any] = {"customer_id": "c1", "now": now}
        self.vars["get_account_response.data.ok"] = "true"
        for i, p in enumerate(products):
            for k, v in p.items():
                self.vars[f"get_account_response.data.products.{i}.{k}"] = v
        run_snippet(code_of(self.wf["nodes"]["facts"]), self.vars)
        assert self.vars["products_state"] == "known" and self.vars["facts_status"] == "ok"

    def tool(self, node: str, name: str) -> dict:
        return node_tools(self.wf, node)[name]

    def hook(self, tool: dict, key: str, **objs: Any) -> None:
        if key in tool:
            run_hook(tool[key], self.vars, **objs)

    def call(self, node: str, name: str, body: dict[str, Any], data: dict[str, Any]) -> tuple[dict, dict]:
        tool = self.tool(node, name)
        config = {"body": dict(body)}
        self.hook(tool, "pre_process_code", config=config)
        result = {"data": dict(data), "header": {}}
        self.hook(tool, "post_process_code", result=result)
        return config["body"], result["data"]

    def verify(self, field: str, answer: str, **data: Any) -> dict:
        base = {"ok": True, "verified": False, "all_verified": False, "wrong_answers_left": 3}
        base.update(data)
        _, out = self.call("identity", "verify_identity", {"customer_id": "c1", "field": field, "answer": answer}, base)
        return out

    def verify_all(self) -> None:
        self.verify("q1", "x", verified=True)
        self.verify("q2", "y", verified=True, all_verified=True)
        assert self.vars["identity_state"] == "verified"

    def promise(self, product_id: str, amount: float, date: str, ok: bool = True) -> tuple[dict, dict]:
        return self.call("negotiate", "create_promise",
                         {"customer_id": "c1", "product_id": product_id, "amount": amount, "date": date}, {"ok": ok})

    def note(self, product_id: str, reason: str) -> tuple[dict, dict]:
        return self.call("negotiate", "add_note", {"customer_id": "c1", "product_id": product_id, "reason": reason}, {"ok": True})

    def transfer(self, queue: str, node: str = "negotiate") -> tuple[dict, dict]:
        return self.call(node, "transfer_to_agent", {"customer_id": "c1", "queue": queue}, {"ok": True})

    def finalize(self, node: str = "finalize") -> None:
        run_hook(code_of(self.wf["nodes"][node]), self.vars)

    def close_body(self) -> dict[str, Any]:
        config = {"body": {"customer_id": "c1", "outcome": "promise", "reason": "{{final_reason}}"}}
        run_hook(self.wf["tools"][0]["pre_process_code"], self.vars, config=config)
        return config["body"]


def test_scenario_single_promise_recorded() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    assert call.vars["p1_id"] == "p1" and call.vars["p1_deadline"] != ""
    call.verify_all()
    body, data = call.promise("zzz", 9999.0, "2026-10-17")  # weekend date, amount above full: rejected, not rewritten
    assert body["product_id"] == "" and body["amount"] == 0 and body["date"] == ""
    assert data["ok"] is False and "read-back" in data["what_to_do_next"] and call.vars.get("p1_promise", "") == ""
    body, data = call.promise("zzz", 540.0, "2026-10-19")  # the id is forced, the confirmed values are kept
    assert body["product_id"] == "p1" and body["amount"] == 540.0 and body["date"] == "2026-10-19"
    assert call.vars["p1_promise"] == "recorded" and call.vars["closing_ready"] == "yes"
    assert call.vars["auto_close"] == "yes" and "automatically" in data["what_to_do_next"]
    assert "text message" not in data["what_to_do_next"]  # the end node speaks the confirmation sentence
    call.finalize()
    assert call.vars["final_outcome"] == "promise" and call.vars["final_reason"] == ""
    assert call.close_body() == {"customer_id": "c1", "outcome": "promise"}


def test_scenario_multi_two_promises_and_a_note() -> None:
    call = Call("AB", PUB_PRODUCTS["PUB-12"])
    call.verify_all()
    call.promise("p1", 510.0, "2026-10-15")
    call.promise("p2", 1320.0, "2026-10-15")
    assert call.vars.get("closing_ready", "") != "yes"
    call.note("p3", "refused")
    assert call.vars["closing_ready"] == "yes"
    call.finalize()
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("promise", "")
    assert "reason" not in call.close_body()


def test_scenario_multi_all_notes_paid() -> None:
    call = Call("AB", PUB_PRODUCTS["PUB-12"])
    call.verify_all()
    for pid in ("p1", "p2", "p3"):
        call.note(pid, "paid")
    assert call.vars["closing_ready"] == "yes"
    call.finalize()
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("paid", "")
    assert call.close_body()["outcome"] == "paid"


def test_scenario_multi_first_note_that_is_not_paid() -> None:
    call = Call("AB", PUB_PRODUCTS["PUB-12"])
    call.verify_all()
    call.note("p1", "paid")
    call.note("p2", "dispute")
    call.note("p3", "refused")
    call.finalize()
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("no_promise", "dispute")
    body = call.close_body()
    assert (body["outcome"], body["reason"]) == ("no_promise", "dispute")


@pytest.mark.parametrize("key, products, queue, reason", [
    ("A", "PUB-07", "restructuring", "restructuring"),
    ("A", "PUB-07", "general", "human"),
    ("AB", "PUB-12", "support", "support"),
    ("AB", "PUB-12", "general", "human"),
])
def test_scenario_transfer_after_a_promise(key: str, products: str, queue: str, reason: str) -> None:
    call = Call(key, PUB_PRODUCTS[products])
    call.verify_all()
    call.promise("p1", 540.0 if key == "A" else 510.0, "2026-10-15")
    _, data = call.transfer(queue)
    assert call.vars["transfer_state"] == "done" and call.vars["transfer_reason"] == reason
    assert call.vars["closing_ready"] == "yes" and "transition_to_finalize" in data["what_to_do_next"]
    assert (call.vars.get("notes_blocked") == "yes") == (key == "AB")
    call.finalize()
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("transferred", reason)
    assert call.close_body()["reason"] == reason


def test_scenario_transfer_before_verification_uses_finalize_early() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    _, data = call.transfer("general", node="identity")
    assert "transition_to_finalize_early" in data["what_to_do_next"]
    call.finalize("finalize_early")
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("transferred", "human")


def test_scenario_early_finalize_is_gated() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    call.finalize("finalize_early")
    assert call.vars.get("final_outcome", "") == "" and call.vars.get("closing_ready", "") == ""
    node = call.wf["nodes"]["finalize_early"]
    assert [t["target_node"] for t in node["transitions"]] == ["end_final_plural", "end_final_single", "end_final", "identity"]


@pytest.mark.parametrize("key", ["A", "AB"])
def test_scenario_identity_failure(key: str) -> None:
    call = Call(key, PUB_PRODUCTS["PUB-07"])
    out: dict = {}
    for left in (2, 1, 0):
        out = call.verify("q1", "bad", wrong_answers_left=left)
        if left > 0:
            assert call.vars["identity_state"] == "pending"
    assert call.vars["identity_state"] == "failed" and call.vars["identity_wrong"] == 3
    assert "Verification failed" in out["what_to_do_next"]
    identity = call.wf["nodes"]["identity"]
    failed = identity["transitions"][0]
    assert failed["condition"]["conditions"][0] == {
        "variable": "identity_state", "operator": "equals", "operand": "failed", "mode": "exact"}
    exit_code = code_of(call.wf["nodes"][failed["target_node"]])
    run_hook(exit_code, call.vars)
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("not_verified", "failed")


def test_scenario_hint_is_not_counted() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    out = call.verify("q1", "swapped", hint="day_month_swapped")
    assert call.vars["identity_wrong"] == 0 and "month as a word" in out["what_to_do_next"]


def test_scenario_promise_failing_twice_single() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    call.verify_all()
    _, data = call.promise("p1", 540.0, "2026-10-15", ok=False)
    assert "same values" in data["what_to_do_next"] and call.vars.get("promise_state", "") == ""
    call.promise("p1", 540.0, "2026-10-15", ok=False)
    assert call.vars["promise_state"] == "write_failed"
    neg = call.wf["nodes"]["negotiate"]["transitions"][1]
    assert neg["condition"]["conditions"][0]["operand"] == "write_failed"
    run_hook(code_of(call.wf["nodes"][neg["target_node"]]), call.vars)
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("no_promise", "service_error")


def test_scenario_promise_failing_twice_multi_goes_to_a_note() -> None:
    call = Call("AB", PUB_PRODUCTS["PUB-12"])
    call.verify_all()
    call.promise("p1", 510.0, "2026-10-15", ok=False)
    _, data = call.promise("p1", 510.0, "2026-10-15", ok=False)
    assert call.vars["p1_promise"] == "failed" and call.vars.get("promise_state", "") == ""
    assert "service_error" in data["what_to_do_next"]
    body, _ = call.note("p1", "refused")
    assert body["reason"] == "service_error" and call.vars["p1_note"] == "service_error"


def test_scenario_hang_up_with_nothing_recorded() -> None:
    verified = Call("A", PUB_PRODUCTS["PUB-07"])
    verified.verify_all()
    assert verified.close_body() == {"customer_id": "c1", "outcome": "no_promise", "reason": "refused"}
    verified.finalize("finalize_forced")
    assert (verified.vars["final_outcome"], verified.vars["final_reason"]) == ("no_promise", "refused")
    assert verified.vars["closing_ready"] == "yes"
    unverified = Call("A", PUB_PRODUCTS["PUB-07"])
    assert unverified.close_body() == {"customer_id": "c1", "outcome": "not_verified", "reason": "declined"}
    multi = Call("AB", PUB_PRODUCTS["PUB-12"])
    multi.verify_all()
    assert multi.close_body() == {"customer_id": "c1", "outcome": "no_promise", "reason": "refused"}


def test_scenario_finalize_waits_until_ready() -> None:
    call = Call("AB", PUB_PRODUCTS["PUB-12"])
    call.verify_all()
    call.note("p1", "paid")
    call.finalize()
    assert call.vars.get("final_outcome", "") == ""
    call.finalize("finalize_forced")
    assert call.vars["final_outcome"] != ""


def test_scenario_callback_valid_and_invalid() -> None:
    call = Call("A", PUB_PRODUCTS["PUB-07"])
    assert call.vars["callback_dates"] == "2026-10-14,2026-10-15"
    body, data = call.call("identity", "schedule_callback", {"customer_id": "c1", "date": "2026-10-14", "time": "15:00"}, {"ok": True})
    assert body["date"] == "2026-10-14" and call.vars["callback_state"] == "scheduled"
    assert "transition_to_exit_not_reached_callback_gated" in data["what_to_do_next"]
    call2 = Call("A", PUB_PRODUCTS["PUB-07"])
    body, data = call2.call("identity", "schedule_callback", {"customer_id": "c1", "date": "2026-10-14", "time": "09:00"}, {"ok": True})
    assert body["date"] == "" and body["time"] == "" and data["ok"] is False
    assert call2.vars["callback_state"] == "invalid"
    body, data = call2.call("identity", "schedule_callback", {"customer_id": "c1", "date": "2026-10-20", "time": "11:00"}, {"ok": True})
    assert call2.vars["callback_state"] == "no_valid_time"
    assert "transition_to_exit_not_reached_no_callback" in data["what_to_do_next"]
    gated = call2.wf["nodes"]["exit_not_reached_callback_gated"]
    run_hook(code_of(gated), call2.vars)
    assert "final_outcome" not in call2.vars  # not scheduled: nothing written, routes back
    run_hook(code_of(call.wf["nodes"]["exit_not_reached_callback_gated"]), call.vars)
    assert (call.vars["final_outcome"], call.vars["final_reason"]) == ("not_reached", "callback")


@pytest.mark.parametrize("key", ["A", "AB"])
def test_failed_identity_wins_and_account_is_reached_only_when_verified(key: str) -> None:
    wf, _ = compiled(key)
    trans = wf["nodes"]["identity"]["transitions"]
    assert [(c["variable"], c["operand"]) for c in trans[0]["condition"]["conditions"]] == [("identity_state", "failed")]
    assert trans[0]["target_node"] != "get_account"
    targets = [t["target_node"] for n in wf["nodes"].values() for t in n.get("transitions", []) if t.get("target_node") == "get_account"]
    assert targets == ["get_account"]
    neg = wf["nodes"]["negotiate"]["transitions"][0]
    assert [(c["variable"], c["operand"]) for c in neg["condition"]["conditions"]] == [("identity_state", "failed")]
    assert neg["target_node"] == trans[0]["target_node"]


@pytest.mark.parametrize("key", ["A", "AB"])
def test_account_tool_has_the_identity_gate_pre_hook(key: str) -> None:
    wf, _ = compiled(key)
    tool = wf["nodes"]["get_account"]["pre_actions"][0]
    assert "identity_state" in tool["pre_process_code"] and "blocked" in tool["pre_process_code"]


def test_composed_opening_and_closing_nodes() -> None:
    data = copy.deepcopy(load_fixture("spec_brief_a.json"))
    data.pop("coverage", None)
    data["opening"]["first_sentence"].pop("template")
    data["opening"]["first_sentence"]["must_include"] = ["item one"]
    data["promise_rules"]["success_sentence_plural"] = "All promises are recorded."
    wf, report = compile_workflow(load_spec(data), copy.deepcopy(OPENAPI))
    ident = wf["nodes"]["identity"]
    assert ident["on_enter"] == {"type": "llm"} and ident["task_message"].startswith("Your very first message")
    assert any("model composes it" in w for w in report.warnings)
    wrap = wf["nodes"]["wrapup"]
    assert wrap["max_node_turns"] == 2 and wrap["max_turn_transition"] == "finalize"
    targets = [t["target_node"] for t in wf["nodes"]["negotiate"]["transitions"]]
    assert targets.index("finalize") < targets.index("wrapup") < len(targets) - 1
    plural = wf["nodes"]["end_final_plural"]["terminal"]["function"]["parameters"]["properties"]["spoken_message"]["default"]
    assert plural.startswith("All promises are recorded.")


def test_default_farewell_only_for_english_and_required() -> None:
    data = copy.deepcopy(load_fixture("spec_brief_a.json"))
    data.pop("coverage", None)
    data["closing"]["goodbye_before_close"] = {"required": True}
    wf, report = compile_workflow(load_spec(data), copy.deepcopy(OPENAPI))
    spoken = wf["nodes"]["end_final"]["terminal"]["function"]["parameters"]["properties"]["spoken_message"]["default"]
    assert spoken == compiler.DEFAULT_FAREWELL_EN and any("default farewell" in w for w in report.warnings)
    data["meta"]["language"] = "xx"
    wf, report = compile_workflow(load_spec(data), copy.deepcopy(OPENAPI))
    assert wf["nodes"]["end_final"]["terminal"]["function"]["parameters"]["properties"]["message_mode"]["default"] == "none"
    assert any("end nodes are silent" in g for g in report.gaps)
