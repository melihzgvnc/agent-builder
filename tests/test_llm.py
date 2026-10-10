"""LLM client tests: no network."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from builder.llm import (
    FakeLLM,
    LLMBudgetExceeded,
    LLMClient,
    LLMConfigError,
    LLMError,
    resolve_config,
)

OR_KEY = "sk-or-v1-SECRETVALUE123"


def test_resolution_order_and_defaults() -> None:
    cfg = resolve_config({"ANTHROPIC_API_KEY": OR_KEY})
    assert cfg.base_url == "https://openrouter.ai/api"
    assert cfg.model == "anthropic/claude-sonnet-5.5"
    assert cfg.openrouter
    cfg = resolve_config({"ANTHROPIC_API_KEY": "sk-or-a", "OPENROUTER_API_KEY": "sk-or-b", "BUILDER_LLM_API_KEY": "sk-or-c"})
    assert cfg.api_key == "sk-or-c"
    cfg = resolve_config({"ANTHROPIC_API_KEY": "sk-or-a", "OPENROUTER_API_KEY": "sk-or-b"})
    assert cfg.api_key == "sk-or-b"


def test_overrides() -> None:
    cfg = resolve_config(
        {"ANTHROPIC_API_KEY": OR_KEY, "BUILDER_LLM_MODEL": "m/x", "BUILDER_LLM_BASE_URL": "http://h"}
    )
    assert (cfg.model, cfg.base_url) == ("m/x", "http://h")


def test_native_key_requires_model() -> None:
    with pytest.raises(LLMConfigError, match="BUILDER_LLM_MODEL"):
        resolve_config({"ANTHROPIC_API_KEY": "sk-ant-zzz"})
    cfg = resolve_config({"ANTHROPIC_API_KEY": "sk-ant-zzz", "BUILDER_LLM_MODEL": "claude-x"})
    assert cfg.base_url is None and not cfg.openrouter


def test_missing_key() -> None:
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        resolve_config({})


def test_key_never_in_repr_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    cfg = resolve_config({"ANTHROPIC_API_KEY": OR_KEY})
    assert "SECRETVALUE" not in repr(cfg) and "SECRETVALUE" not in str(cfg)

    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw: object) -> None:
                raise ValueError(f"bad header Bearer {OR_KEY}")

    caplog.set_level(logging.DEBUG)
    client = LLMClient(env={"ANTHROPIC_API_KEY": OR_KEY}, sdk_client=Boom(), sleep=lambda s: None)
    with pytest.raises(LLMError) as ei:
        client.call("p", "s", [{"role": "user", "content": "x"}])
    assert "SECRETVALUE" not in str(ei.value)
    assert "SECRETVALUE" not in caplog.text


class _Resp(SimpleNamespace):
    pass


def _ok_resp() -> _Resp:
    return _Resp(
        content=[SimpleNamespace(type="tool_use", input={"a": 1}), SimpleNamespace(type="text", text="hi")],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
        model="m",
        stop_reason="tool_use",
    )


def test_call_parses_and_logs_to_trace() -> None:
    logged: list[dict] = []
    trace = SimpleNamespace(log_model_call=lambda model, purpose, **kw: logged.append({"model": model, "purpose": purpose, **kw}))
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: _ok_resp()))
    c = LLMClient(env={"ANTHROPIC_API_KEY": OR_KEY}, trace=trace, sdk_client=sdk)
    r = c.call("purp", "sys", [{"role": "user", "content": "x"}])
    assert r.tool_inputs == [{"a": 1}] and r.text == "hi"
    assert r.usage == {"input_tokens": 7, "output_tokens": 3}
    assert logged[0]["purpose"] == "purp" and logged[0]["input_tokens"] == 7
    assert c.total_input_tokens == 7


def _status_error(status: int) -> Exception:
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://x")
    return anthropic.APIStatusError("boom", response=httpx.Response(status, request=req), body=None)


def test_retries_on_429_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def create(**kw: object) -> _Resp:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(429)
        return _ok_resp()

    c = LLMClient(env={"ANTHROPIC_API_KEY": OR_KEY}, sdk_client=SimpleNamespace(messages=SimpleNamespace(create=create)), sleep=sleeps.append)
    r = c.call("p", "s", [{"role": "user", "content": "x"}])
    assert r.tool_inputs and calls["n"] == 3 and len(sleeps) == 2


def test_no_retry_on_400() -> None:
    calls = {"n": 0}

    def create(**kw: object) -> _Resp:
        calls["n"] += 1
        raise _status_error(400)

    c = LLMClient(env={"ANTHROPIC_API_KEY": OR_KEY}, sdk_client=SimpleNamespace(messages=SimpleNamespace(create=create)), sleep=lambda s: None)
    with pytest.raises(LLMError) as ei:
        c.call("p", "s", [{"role": "user", "content": "x"}])
    assert ei.value.status == 400 and calls["n"] == 1


def test_budget_exhausted() -> None:
    c = LLMClient(env={"ANTHROPIC_API_KEY": OR_KEY}, sdk_client=SimpleNamespace(), budget_s=0)
    with pytest.raises(LLMBudgetExceeded):
        c.call("p", "s", [{"role": "user", "content": "x"}])


def test_fake_llm_script_and_requests() -> None:
    f = FakeLLM([{"text": "a"}, RuntimeError("x"), "plain"])
    assert f.call("p", "s", []).text == "a"
    with pytest.raises(RuntimeError):
        f.call("p", "s", [])
    assert f.call("p", "s", []).text == "plain"
    assert len(f.requests) == 3
    with pytest.raises(LLMError):
        f.call("p", "s", [])
