"""Deterministic workflow assembly from policy + tool plan, local checks and the validation repair loop."""
from __future__ import annotations

import json
import re
from typing import Any

from builder import flow_llm, flow_policy, flow_tools, tools_gen

MAIN = "main"
END_LIMIT = "end_limit"
MAIN_TURNS = 80
_BRACES = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
SNAKE = re.compile(r"[a-z][a-z0-9_]*")


def tool_transition(node: str, description: str) -> dict[str, Any]:
    return {"target_node": node, "description": description, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": description}}


def assemble(policy: dict[str, Any], plan: flow_tools.ToolPlan) -> tuple[dict[str, Any], dict[str, Any]]:
    """(workflow, info). `info` has prompt sizes and the hand-over mapping."""
    var_names = plan.var_names() or [str(v.get("name")) for v in policy.get("call_variables") or [] if isinstance(v, dict)]
    prompts = flow_policy.assemble_prompts(policy, var_names, plan.names())
    tools = flow_tools.make_tools(plan)
    flow_tools.finalize_descriptions(tools, policy)
    hands = prompts["handovers"]
    nodes: dict[str, Any] = {}
    transitions = [tool_transition(h["node"], (h["when"] or h["destination"] or h["id"])[:300]) for h in hands]
    opening = policy.get("opening") if isinstance(policy.get("opening"), dict) else {}
    text = flow_policy.clean(opening.get("text"), set(var_names))
    on_enter: dict[str, Any] = {"type": "tts", "text": text} if opening.get("mode") == "fixed" and text else {"type": "llm"}
    nodes[MAIN] = {
        "name": MAIN, "node_type": "conversation", "on_enter": on_enter, "role_message": "",
        "task_message": prompts["task_message"], "tools": tools, "transitions": transitions,
        "max_node_turns": MAIN_TURNS, "max_turn_transition": END_LIMIT,
    }
    for h in hands:
        nodes[h["node"]] = tools_gen.end_call_node(h["node"], spoken_message=flow_policy.clean(h["text"], set(var_names)) or None,
                                                   terminal_name=h["terminal"])
    nodes[END_LIMIT] = tools_gen.end_call_node(END_LIMIT, None, "limit_reached")
    wf: dict[str, Any] = {"initial_node": MAIN, "system_prompt": prompts["system_prompt"], "nodes": nodes,
                          "global_nodes": {}, "tools": []}
    examples = {v["name"]: v["example_from_docs"] for v in plan.call_variables if v.get("example_from_docs")}
    if examples:
        wf["test_variables"] = examples
    info = {"system_prompt_chars": len(wf["system_prompt"]), "task_message_chars": len(prompts["task_message"]),
            "handovers": [{k: h[k] for k in ("id", "node", "terminal", "ends_call")} for h in hands],
            "tool_names": plan.names()}
    return wf, info


def local_checks(wf: dict[str, Any], plan: flow_tools.ToolPlan) -> list[str]:
    errs: list[str] = []
    try:
        json.dumps(wf, allow_nan=False)
    except (TypeError, ValueError) as e:
        errs.append(f"workflow is not JSON-serialisable: {e}")
        return errs
    allowed = set(plan.var_names())
    for t in plan.tools:
        allowed.update(f["name"] for f in t["fields"])

    def walk(o: Any, path: str) -> None:
        if o is None:
            errs.append(f"null value at {path}")
        elif isinstance(o, dict):
            for k, v in o.items():
                if k in ("pre_process_code", "post_process_code"):
                    if tools_gen.has_double_braces(str(v)):
                        errs.append(f"doubled brace in {path}.{k}")
                    continue
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str):
            for m in _BRACES.finditer(o):
                if m.group(1) not in allowed:
                    errs.append(f"unknown variable reference {m.group(0)} at {path}")
            rest = _BRACES.sub("", o)
            if "{{" in rest or "}}" in rest:
                errs.append(f"stray double brace at {path}")

    walk(wf, "$")
    nodes = wf.get("nodes", {})
    if nodes.get(wf.get("initial_node"), {}).get("node_type") != "conversation":
        errs.append("initial node is not a conversation node")
    for name, n in nodes.items():
        if not SNAKE.fullmatch(name) or n.get("name") != name:
            errs.append(f"node name not snake_case or not equal to key: {name}")
        for tr in n.get("transitions", []):
            if tr["target_node"] not in nodes:
                errs.append(f"{name}: transition to unknown node {tr['target_node']}")
    names = [t["function"]["name"] for t in nodes.get(MAIN, {}).get("tools", [])]
    if len(names) != len(set(names)):
        errs.append("duplicate tool names")
    return errs


def validate_with_repair(policy: dict[str, Any], plan: flow_tools.ToolPlan, slot: str, llm: Any, freya: Any, trace: Any,
                         dry: bool, max_rounds: int = 3) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(workflow, policy, report). Feeds validator errors back to the LLM for up to `max_rounds` repairs,
    then falls back to the last workflow that validated (or the first one when none did)."""
    rounds: list[dict[str, Any]] = []
    first: tuple[dict, dict, dict] | None = None
    last_valid: tuple[dict, dict, dict] | None = None
    current = policy
    for rnd in range(max_rounds + 1):
        errors = flow_policy.check_policy(current, plan.names())
        wf: dict[str, Any] = {}
        info: dict[str, Any] = {}
        if not errors:
            try:
                wf, info = assemble(current, plan)
                errors = local_checks(wf, plan)
            except Exception as e:  # noqa: BLE001 - a malformed policy is a repairable error
                errors = [f"assembly failed: {type(e).__name__}: {e}"]
        source = "local"
        if not errors and not dry:
            source = "platform"
            try:
                freya.validate_workflow(slot, wf)
            except Exception as e:  # noqa: BLE001 - validator message goes back to the LLM
                errors = [f"{type(e).__name__}: {str(e)[:1500]}"]
        rounds.append({"round": rnd, "ok": not errors, "source": source, "errors": errors[:10]})
        if first is None and wf:
            first = (wf, current, info)
        if not errors:
            last_valid = (wf, current, info)
            break
        trace.warn(f"validation round {rnd} failed ({source}): {errors[0][:300]}")
        if rnd == max_rounds or llm is None:
            break
        try:
            fixed = flow_llm.ask_json(llm, f"flow repair {rnd + 1}", flow_llm.REPAIR_SYSTEM,
                                      "POLICY:\n" + json.dumps(current, ensure_ascii=False) + "\n\nERRORS:\n" + "\n".join(errors[:10]))
        except Exception as e:  # noqa: BLE001
            trace.warn(f"repair call failed: {type(e).__name__}: {e}")
            break
        if isinstance(fixed, dict):
            current = fixed
    chosen = last_valid or first
    if chosen is None:
        raise ValueError("no policy could be assembled: " + "; ".join(rounds[-1]["errors"][:3]))
    wf, pol, info = chosen
    if last_valid is None:
        trace.warn("DEGRADED: validation never passed; using the first assembled workflow")
    return wf, pol, {"validation_rounds": rounds, **info}
