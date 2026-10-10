from __future__ import annotations

import time
from typing import Any

from builder.health import probe_chat


class FakeClient:
    def __init__(self, result: Any = None, raises: Exception | None = None, delay: float = 0.0) -> None:
        self.result = result
        self.raises = raises
        self.delay = delay
        self.calls: list[tuple[str, str, Any]] = []

    def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
        self.calls.append((method, path, body))
        if path.endswith("/end"):
            return {}
        if self.delay:
            time.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.result


def test_ok_path_ends_chat() -> None:
    c = FakeClient({"response": "Hi", "callId": "c1"})
    r = probe_chat("wf", {"a": "b"}, client=c)
    assert r["ok"] is True and r["error"] == ""
    assert c.calls[0][2]["customerVariables"] == {"a": "b"}
    assert c.calls[-1][1] == "/api/v2/chat/end" and c.calls[-1][2] == {"callId": "c1"}


def test_message_error_is_contained() -> None:
    c = FakeClient(raises=RuntimeError("500"))
    r = probe_chat("wf", {}, client=c)
    assert r["ok"] is False and "500" in r["error"]
    assert all(not p.endswith("/end") for _, p, _ in c.calls)


def test_end_called_even_when_response_empty() -> None:
    c = FakeClient({"response": "", "callId": "c2"})
    r = probe_chat("wf", {}, client=c)
    assert r["ok"] is False
    assert c.calls[-1][1] == "/api/v2/chat/end"


def test_end_failure_swallowed() -> None:
    class Bad(FakeClient):
        def api(self, method: str, path: str, body: Any = None, **params: Any) -> Any:
            if path.endswith("/end"):
                raise RuntimeError("boom")
            return super().api(method, path, body)

    r = probe_chat("wf", {}, client=Bad({"response": "x", "callId": "c"}))
    assert r["ok"] is True


def test_timeout() -> None:
    c = FakeClient({"response": "x"}, delay=1.0)
    r = probe_chat("wf", {}, timeout=0.05, client=c)
    assert r["ok"] is False and "timeout" in r["error"]
