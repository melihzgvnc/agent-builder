from __future__ import annotations

from typing import Any

import pytest

from builder import sim_poll
from builder.sim_poll import SimPollError, run_simulation_resilient


class Api:
    AGENT_LLM = "m"
    SIM_LLM = "m"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def api(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


E504 = RuntimeError("GET /x -> 504: timeout")
CREATE = {"simulation": {"id": "s1"}}
DONE = {"simulation": {"status": "completed"}}
ITS = {"iterations": [{"id": "i1"}]}


def run(api: Api, **kw: Any) -> tuple:
    run.sleeps = []  # type: ignore[attr-defined]
    return run_simulation_resilient(api, "ag", "n", {"p": {}}, 60, sleep=run.sleeps.append, **kw)  # type: ignore[attr-defined]


def test_one_504_resumes_same_simulation() -> None:
    api = Api([CREATE, E504, DONE, ITS])
    assert run(api) == ("s1", "i1", "completed")
    assert sum(1 for m, _ in api.calls if m == "POST") == 1


def test_three_consecutive_5xx_stop_with_backoff() -> None:
    with pytest.raises(SimPollError):
        run(Api([CREATE, E504, E504, E504]))
    assert run.sleeps == [sim_poll.POLL_INTERVAL_S, 5.0, 10.0]  # type: ignore[attr-defined]


def test_post_5xx_and_4xx_are_not_retried() -> None:
    with pytest.raises(RuntimeError):
        run(Api([RuntimeError("POST /x -> 502: bad")]))
    with pytest.raises(RuntimeError):
        run(Api([CREATE, RuntimeError("GET /x -> 404: no")]))


def test_fallback_for_fakes() -> None:
    class Fake:
        def run_simulation(self, a, n, v, max_seconds=0):
            return ("s", "i", "completed")

    assert run_simulation_resilient(Fake(), "a", "n", {}, 10) == ("s", "i", "completed")
