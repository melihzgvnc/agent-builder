"""Tests for builder/tools_gen.py: tools follow the given openapi, whatever it says."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from builder.tools_gen import (
    ToolGenError,
    attach_hooks,
    attach_response_mapping,
    build_tool,
    build_tools,
    check_default_collisions,
    end_call_node,
    lifecycle,
    path_for_tool,
    placeholder,
    post_hook_source,
    tool_names,
)
from tests.conftest import REPO_ROOT

OPENAPI: dict[str, Any] = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))


def _schema(doc: dict[str, Any], path: str) -> dict[str, Any]:
    post = doc["paths"][path]["post"]
    return post["requestBody"]["content"]["application/json"]["schema"]


def _first_with_enum(doc: dict[str, Any]) -> tuple[str, str]:
    """(path, field) of a body field that has an enum."""
    for path in doc["paths"]:
        for name, spec in _schema(doc, path).get("properties", {}).items():
            if "enum" in spec:
                return path, name
    raise AssertionError("fixture openapi has no enum body field")


def _mutated() -> dict[str, Any]:
    doc = copy.deepcopy(OPENAPI)
    enum_path, enum_field = _first_with_enum(doc)
    schema = _schema(doc, enum_path)
    schema["properties"][enum_field]["enum"] = ["alpha", "beta", "gamma"]
    renamed = schema["properties"].pop(enum_field)
    schema["properties"]["renamed_" + enum_field] = renamed
    schema["required"] = [("renamed_" + enum_field) if r == enum_field else r for r in schema["required"]]
    schema["properties"]["extra_flag"] = {"type": "boolean", "description": "new field"}
    schema["required"].append("extra_flag")
    doc["servers"] = [{"url": "https://example.test/api/v9/"}]
    dropped = [p for p in doc["paths"] if p != enum_path][-1]
    del doc["paths"][dropped]
    doc["_mutation"] = {"enum_path": enum_path, "old": enum_field, "new": "renamed_" + enum_field, "dropped": dropped}
    return doc


def _walk(value: Any) -> Any:
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def _assert_follows(doc: dict[str, Any], tool: dict[str, Any], path: str) -> None:
    schema = _schema(doc, path)
    fn = tool["function"]
    props = fn["parameters"]["properties"]
    base = doc["servers"][0]["url"].rstrip("/")
    assert fn["name"] == path.strip("/").split("/")[-1]
    assert fn["handler"] == "__function__:api_call"
    assert props["url"]["default"] == base + path
    assert props["method"]["default"] == "POST"
    assert props["headers"]["properties"]["Content-Type"]["default"] == "application/json"
    body = props["body"]
    assert list(body["properties"]) == list(schema["properties"])
    assert body["required"] == schema.get("required", [])
    for name, spec in schema["properties"].items():
        leaf = body["properties"][name]
        assert leaf["type"] == spec["type"]
        assert "default" in leaf
        assert "const" not in leaf
        assert leaf.get("enum") == spec.get("enum")
        assert leaf.get("description") == spec.get("description")
        assert leaf.get("format") == spec.get("format")
    assert not any(isinstance(n, dict) and "const" in n for n in _walk(tool))
    assert "default" not in body


@pytest.mark.parametrize("path", list(OPENAPI["paths"]))
def test_every_example_operation_follows_the_openapi(path: str) -> None:
    tool = build_tool(OPENAPI, path)
    _assert_follows(OPENAPI, tool, path)
    assert tool["config"] == {"timeoutMs": 10000, "maxRetries": 0}
    for name, leaf in tool["function"]["parameters"]["properties"]["body"]["properties"].items():
        assert leaf["default"] == placeholder(name)


def test_names_come_from_the_paths() -> None:
    assert tool_names(OPENAPI) == [p.strip("/").split("/")[-1] for p in OPENAPI["paths"]]
    assert build_tool(OPENAPI, "/v1/anything" if "/v1/anything" in OPENAPI["paths"] else next(iter(OPENAPI["paths"])))
    with pytest.raises(ToolGenError):
        path_for_tool(OPENAPI, "no_such_tool")
    with pytest.raises(ToolGenError):
        build_tool(OPENAPI, "/missing")


def test_mutated_openapi_is_followed() -> None:
    doc = _mutated()
    m = doc["_mutation"]
    tools = build_tools(doc)
    assert m["dropped"].strip("/") not in tools
    assert set(tools) == set(tool_names(doc))
    assert len(tools) == len(OPENAPI["paths"]) - 1
    for path in doc["paths"]:
        _assert_follows(doc, tools[path.strip("/").split("/")[-1]], path)
    name = m["enum_path"].strip("/").split("/")[-1]
    body = tools[name]["function"]["parameters"]["properties"]["body"]
    assert m["old"] not in body["properties"]
    assert body["properties"][m["new"]]["enum"] == ["alpha", "beta", "gamma"]
    assert body["properties"]["extra_flag"]["type"] == "boolean"
    assert "extra_flag" in body["required"]
    assert body["properties"]["extra_flag"]["default"] == placeholder("extra_flag")
    assert tools[name]["function"]["parameters"]["properties"]["url"]["default"].startswith(
        "https://example.test/api/v9/"
    )
    assert "freya" not in json.dumps(tools).lower()


def test_baked_fields_and_unknown_baked_keys() -> None:
    path = next(p for p in OPENAPI["paths"] if "customer_id" in _schema(OPENAPI, p)["properties"])
    tool = build_tool(OPENAPI, path, baked={"customer_id": placeholder("cid"), "not_a_field": "x"})
    props = tool["function"]["parameters"]["properties"]["body"]["properties"]
    assert props["customer_id"]["default"] == placeholder("cid")
    assert "not_a_field" not in props
    others = [n for n in props if n != "customer_id"]
    assert all(props[n]["default"] == placeholder(n) for n in others)


def test_options_and_description() -> None:
    path = next(iter(OPENAPI["paths"]))
    summary = OPENAPI["paths"][path]["post"]["summary"]
    default = build_tool(OPENAPI, path, max_retries=2, timeout_ms=5000)
    assert default["config"] == {"timeoutMs": 5000, "maxRetries": 2}
    assert default["function"]["description"] == "CALL it when the instructions say so: " + summary
    custom = build_tool(OPENAPI, path, description="CALL it when the caller answers.")
    assert custom["function"]["description"] == "CALL it when the caller answers."


def test_no_servers_is_an_error() -> None:
    doc = copy.deepcopy(OPENAPI)
    del doc["servers"]
    with pytest.raises(ToolGenError):
        build_tool(doc, next(iter(doc["paths"])))


def test_ref_body_schema_is_resolved() -> None:
    doc = {
        "servers": [{"url": "https://h.test/v2"}],
        "components": {"schemas": {"Body": {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}}},
        "paths": {"/v2/do_it": {"put": {"summary": "s", "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Body"}}}}}}},
    }
    tool = build_tool(doc, "/v2/do_it")
    props = tool["function"]["parameters"]["properties"]
    assert props["body"]["properties"]["a"] == {"type": "integer", "default": placeholder("a")}
    assert props["body"]["required"] == ["a"]
    assert props["method"]["default"] == "PUT"


def test_collision_check() -> None:
    path = next(p for p in OPENAPI["paths"] if "customer_id" in _schema(OPENAPI, p)["properties"])
    tool = build_tool(OPENAPI, path, baked={"customer_id": placeholder("cid")})
    fields = list(_schema(OPENAPI, path)["properties"])
    model_filled = [f for f in fields if f != "customer_id"]
    assert check_default_collisions(tool, {"unrelated"}) == []
    if model_filled:
        assert check_default_collisions(tool, {model_filled[0], "unrelated"}) == [model_filled[0]]
    # A baked field is not a collision, even when its name is reserved.
    assert check_default_collisions(tool, {"customer_id"}) == []


def test_hooks_reject_double_braces_and_non_api_call() -> None:
    tool = build_tool(OPENAPI, next(iter(OPENAPI["paths"])))
    attach_hooks(tool, pre="vars['x'] = 1", post=post_hook_source("go on", "try again"))
    assert tool["pre_process_code"] == "vars['x'] = 1"
    assert "post_process_code" in tool
    for bad in ("x = {'a': {'b': 1}}", "x = '" + placeholder("v") + "'"):
        with pytest.raises(ToolGenError):
            attach_hooks(tool, pre=bad)
        with pytest.raises(ToolGenError):
            attach_hooks(tool, post=bad)
    other = {"function": {"handler": "__function__:code_exec"}}
    with pytest.raises(ToolGenError):
        attach_hooks(other, pre="pass")
    empty = build_tool(OPENAPI, next(iter(OPENAPI["paths"])))
    attach_hooks(empty, pre="  ", post=None)
    assert "pre_process_code" not in empty


def test_post_hook_source_behaviour() -> None:
    source = post_hook_source("Say it worked.", "Say it failed; try again.")
    compile(source, "<hook>", "exec")
    assert "{{" not in source and "}}" not in source

    def run(result: Any) -> Any:
        exec(source, {"result": result})
        return result

    ok = run({"data": {"ok": True}, "header": {"a": "b"}})
    assert ok["data"]["what_to_do_next"] == "Say it worked."
    assert "header" not in ok
    assert run({"data": {"ok": "true"}})["data"]["what_to_do_next"] == "Say it worked."
    assert run({"data": {"ok": False}})["data"]["what_to_do_next"] == "Say it failed; try again."
    assert run({"data": {}})["data"]["what_to_do_next"] == "Say it failed; try again."
    assert run({"data": None}) == {"data": None}
    assert run("not a dict") == "not a dict"
    with pytest.raises(ToolGenError):
        post_hook_source("fine", "bad " + placeholder("x"))


def test_response_mapping_and_lifecycle() -> None:
    tool = build_tool(OPENAPI, next(iter(OPENAPI["paths"])))
    attach_response_mapping(tool, [{"path": "data.ok", "variable": "ok_flag"}])
    assert tool["response_mapping"] == [{"path": "data.ok", "variable": "ok_flag"}]
    with pytest.raises(ToolGenError):
        attach_response_mapping(tool, [{"path": "ok", "variable": "ok_flag"}])
    assert lifecycle(tool)["config"]["alwaysRunsAt"] == ["end_of_call"]
    assert lifecycle(tool, when=["start_of_call", "end_of_call"])["config"]["alwaysRunsAt"] == [
        "start_of_call",
        "end_of_call",
    ]
    with pytest.raises(ToolGenError):
        lifecycle(tool, when=[])
    with pytest.raises(ToolGenError):
        lifecycle(tool, when=["each_turn"])


def test_end_call_node_shape() -> None:
    silent = end_call_node("bye")
    assert silent == {
        "name": "bye",
        "node_type": "end_call",
        "on_enter": {"type": "silent"},
        "disable_inactivity_monitor": True,
        "transitions": [],
        "terminal": {
            "type": "function",
            "function": {
                "name": "bye",
                "handler": "__function__:end_call",
                "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}},
            },
        },
    }
    spoken = end_call_node("bye", "See you.", terminal_name="done_ok")
    props = spoken["terminal"]["function"]["parameters"]["properties"]
    assert spoken["terminal"]["function"]["name"] == "done_ok"
    assert props["message_mode"]["default"] == "custom"
    assert props["spoken_message"]["default"] == "See you."
