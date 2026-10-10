"""Thin LLM client over the pinned anthropic SDK, plus a scripted FakeLLM for tests.

Key, endpoint and model all come from the environment (never from files, never logged):
  key      BUILDER_LLM_API_KEY, else OPENROUTER_API_KEY, else ANTHROPIC_API_KEY
  base_url BUILDER_LLM_BASE_URL, else the OpenRouter endpoint for 'sk-or-' keys, else SDK default
  model    BUILDER_LLM_MODEL, else the OpenRouter default for 'sk-or-' keys (native keys must set it)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

OPENROUTER_PREFIX = "sk-or-"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
KEY_VARS: tuple[str, ...] = ("BUILDER_LLM_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_S = 240.0


class LLMError(RuntimeError):
    """A model call failed. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LLMConfigError(LLMError):
    """Key or model configuration is missing or unusable."""


class LLMBudgetExceeded(LLMError):
    """The wall-clock budget for model calls is used up."""


@dataclass
class LLMResult:
    text: str = ""
    tool_inputs: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    model: str = ""
    seconds: float = 0.0
    stop_reason: str | None = None


@dataclass(frozen=True)
class LLMConfig:
    api_key: str = field(repr=False)
    base_url: str | None
    model: str
    openrouter: bool

    def __repr__(self) -> str:  # never show the key
        return f"LLMConfig(base_url={self.base_url!r}, model={self.model!r}, key=<hidden>)"


def resolve_config(env: Mapping[str, str] | None = None) -> LLMConfig:
    """Resolve key, base_url and model from the environment. Raises LLMConfigError."""
    env = os.environ if env is None else env
    key = ""
    for name in KEY_VARS:
        if (env.get(name) or "").strip():
            key = env[name].strip()
            break
    if not key:
        raise LLMConfigError("no LLM key: set one of " + ", ".join(KEY_VARS))
    is_or = key.startswith(OPENROUTER_PREFIX)
    base_url = (env.get("BUILDER_LLM_BASE_URL") or "").strip() or (OPENROUTER_BASE_URL if is_or else None)
    model = (env.get("BUILDER_LLM_MODEL") or "").strip()
    if not model:
        if is_or:
            model = OPENROUTER_DEFAULT_MODEL
        else:
            raise LLMConfigError("BUILDER_LLM_MODEL must be set when the key is not an OpenRouter key")
    return LLMConfig(api_key=key, base_url=base_url, model=model, openrouter=is_or)


def _scrub(text: str, secret: str) -> str:
    return text.replace(secret, "<key>") if secret else text


class _Base:
    """Shared budget and trace logging."""

    model: str = ""

    def __init__(self, trace: Any = None, budget_s: float | None = None) -> None:
        self.trace = trace
        self._deadline: float | None = None
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_seconds = 0.0
        self.calls = 0
        if budget_s is not None:
            self.set_budget(budget_s)

    def set_budget(self, seconds: float) -> None:
        """Start (or restart) the global wall-clock budget for all calls."""
        self._deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return float("inf") if self._deadline is None else self._deadline - time.monotonic()

    def _account(self, res: LLMResult, purpose: str, note: str = "") -> None:
        self.calls += 1
        self.total_input_tokens += res.usage.get("input_tokens", 0)
        self.total_output_tokens += res.usage.get("output_tokens", 0)
        self.total_seconds += res.seconds
        if self.trace is not None:
            self.trace.log_model_call(
                res.model or self.model,
                purpose,
                duration_s=res.seconds,
                input_tokens=res.usage.get("input_tokens"),
                output_tokens=res.usage.get("output_tokens"),
                note=note,
            )


class LLMClient(_Base):
    """Anthropic-style messages client with bounded retries and a global budget."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        trace: Any = None,
        budget_s: float | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        sdk_client: Any = None,
    ) -> None:
        super().__init__(trace, budget_s)
        self.config = resolve_config(env)
        self.model = self.config.model
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._sdk = sdk_client

    def _client(self) -> Any:
        if self._sdk is None:
            import anthropic

            kwargs: dict[str, Any] = {"max_retries": 0}
            if self.config.openrouter:
                kwargs["auth_token"] = self.config.api_key
                kwargs["api_key"] = None
            else:
                kwargs["api_key"] = self.config.api_key
            if self.config.base_url:
                kwargs["base_url"] = self.config.base_url
            self._sdk = anthropic.Anthropic(**kwargs)
        return self._sdk

    def _classify(self, exc: Exception) -> tuple[bool, int | None]:
        """(retryable, status) for an SDK exception."""
        import anthropic

        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code in RETRY_STATUSES or exc.status_code >= 500, exc.status_code
        if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
            return True, None
        return False, None

    def call(
        self,
        purpose: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        secret = self.config.api_key
        last: LLMError | None = None
        for attempt in range(1, self.max_attempts + 1):
            left = self.remaining()
            if left <= 1:
                raise LLMBudgetExceeded(f"LLM budget exhausted before call '{purpose}'")
            t0 = time.monotonic()
            try:
                resp = self._client().messages.create(timeout=min(timeout_s, left), **kwargs)
            except Exception as exc:  # noqa: BLE001 - classified below
                retryable, status = self._classify(exc)
                msg = _scrub(f"{type(exc).__name__}: {exc}", secret)
                secs = time.monotonic() - t0
                if self.trace is not None:
                    self.trace.log_model_call(
                        self.model, purpose, duration_s=secs, note=f"failed attempt {attempt}: {msg[:300]}"
                    )
                last = LLMError(f"call '{purpose}' failed (status={status}): {msg[:600]}", status)
                if not retryable or attempt == self.max_attempts:
                    raise last from None
                delay = min(2.0 ** attempt, 20.0)
                if delay >= self.remaining():
                    raise LLMBudgetExceeded(f"LLM budget exhausted while retrying '{purpose}'") from None
                self._sleep(delay)
                continue
            res = self._to_result(resp, time.monotonic() - t0)
            self._account(res, purpose)
            return res
        raise last or LLMError(f"call '{purpose}' failed")

    def _to_result(self, resp: Any, seconds: float) -> LLMResult:
        texts: list[str] = []
        tool_inputs: list[dict[str, Any]] = []
        for block in getattr(resp, "content", []) or []:
            btype = getattr(block, "type", "")
            if btype == "text":
                texts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                inp = getattr(block, "input", None)
                if isinstance(inp, dict):
                    tool_inputs.append(inp)
        usage = getattr(resp, "usage", None)
        return LLMResult(
            text="".join(texts),
            tool_inputs=tool_inputs,
            usage={
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            },
            model=str(getattr(resp, "model", "") or self.model),
            seconds=seconds,
            stop_reason=getattr(resp, "stop_reason", None),
        )


class FakeLLM(_Base):
    """Scripted test double with the LLMClient interface.

    `script` items are consumed in order. Each is an LLMResult, a dict (keys text, tool_inputs,
    usage), a str (text), an Exception (raised), or a callable(request_dict) returning any of those.
    Every request is kept in `requests`.
    """

    def __init__(self, script: list[Any] | None = None, trace: Any = None, budget_s: float | None = None) -> None:
        super().__init__(trace, budget_s)
        self.script: list[Any] = list(script or [])
        self.requests: list[dict[str, Any]] = []
        self.model = "fake-model"

    def call(
        self,
        purpose: str,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> LLMResult:
        if self.remaining() <= 0:
            raise LLMBudgetExceeded(f"LLM budget exhausted before call '{purpose}'")
        request = {
            "purpose": purpose,
            "system": system,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "max_tokens": max_tokens,
            "timeout_s": timeout_s,
        }
        self.requests.append(request)
        if not self.script:
            raise LLMError(f"FakeLLM script exhausted at call '{purpose}'")
        item = self.script.pop(0)
        if callable(item):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = {"text": item}
        if isinstance(item, dict):
            item = LLMResult(
                text=item.get("text", ""),
                tool_inputs=list(item.get("tool_inputs", [])),
                usage=item.get("usage", {"input_tokens": 10, "output_tokens": 5}),
                model=self.model,
                seconds=0.0,
            )
        self._account(item, purpose)
        return item
