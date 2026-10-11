from __future__ import annotations
import copy
import json
from typing import Any
import pytest
from builder.inputs import apply_server_url, load_openapi_doc, resolve_server_url
from builder.tools_gen import ToolGenError, build_tool, build_tools
from tests.conftest import REPO_ROOT
OPENAPI: dict[str, Any] = json.loads((REPO_ROOT / "tools" / "openapi.json").read_text(encoding="utf-8"))
URL = "https://example.invalid/api"
def test_override_replaces_url_without_mutating_input() -> None:
    before = copy.deepcopy(OPENAPI)
    out = apply_server_url(OPENAPI, URL)
    assert out["servers"][0]["url"] == URL
    assert OPENAPI == before
    assert out is not OPENAPI
def test_falsy_url_returns_doc_unchanged() -> None:
    assert apply_server_url(OPENAPI, None) is OPENAPI
    assert apply_server_url(OPENAPI, "") is OPENAPI
def test_servers_created_when_missing() -> None:
    doc = {k: v for k, v in OPENAPI.items() if k != "servers"}
    assert apply_server_url(doc, URL)["servers"] == [{"url": URL}]
@pytest.mark.parametrize("bad", ["ftp://x.test", "example.invalid", "https://", "file:///etc"])
def test_non_http_urls_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        apply_server_url(OPENAPI, bad)
def test_every_generated_tool_url_starts_with_override() -> None:
    doc = apply_server_url(OPENAPI, URL)
    paths = list(doc["paths"])
    assert paths
    for path in paths:
        tool = build_tool(doc, path)
        assert json.dumps(tool).count(URL) >= 1
        assert "workers.dev" not in json.dumps(tool)
    assert len(build_tools(doc)) == len(paths)
def test_cli_beats_env() -> None:
    env = {"BUILDER_SERVER_URL": "https://env.test"}
    assert resolve_server_url("https://cli.test", env) == "https://cli.test"
    assert resolve_server_url(None, env) == "https://env.test"
    assert resolve_server_url("  ", env) == "https://env.test"
    assert resolve_server_url(None, {}) is None
def test_no_servers_and_no_override_fails_only_at_tool_compile(tmp_path) -> None:
    doc = {k: v for k, v in OPENAPI.items() if k != "servers"}
    p = tmp_path / "c.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    loaded = load_openapi_doc(p)  # loading is fine
    with pytest.raises(ToolGenError, match="--server-url"):
        build_tool(loaded, next(iter(loaded["paths"])))
    build_tool(apply_server_url(loaded, URL), next(iter(loaded["paths"])))
def test_load_openapi_doc_rejects_non_object(tmp_path) -> None:
    p = tmp_path / "c.json"
    p.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_openapi_doc(p)
