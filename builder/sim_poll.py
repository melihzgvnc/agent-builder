"""Resilient simulation polling for the online test.

A status poll that answers 5xx is retried with a short backoff on the same simulation id; the
create call is never retried on 5xx (the simulation may exist) and the chat route is not used.
A client without `api` / `AGENT_LLM` (a test fake) falls back to `freya.run_simulation`.
Does not import builder.pipeline; the client module is passed in.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

POLL_INTERVAL_S: float = 10.0
BACKOFF_BASE_S: float = 5.0
BACKOFF_CAP_S: float = 30.0
MAX_CONSECUTIVE_POLL_FAILURES: int = 3
BUSY_WAIT_S: float = 30.0
FINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "paused")
_SERVER_ERROR = re.compile(r" -> 5\d\d")


class SimPollError(RuntimeError):
    """Polling gave up (repeated 5xx or no time left). The message keeps the last HTTP error."""


def is_server_error(e: BaseException) -> bool:
    return isinstance(e, RuntimeError) and _SERVER_ERROR.search(str(e)) is not None


def backoff_s(failures: int) -> float:
    return min(BACKOFF_BASE_S * 2 ** max(failures - 1, 0), BACKOFF_CAP_S)


def run_simulation_resilient(freya: Any, agent_id: str, name: str, variables: dict[str, Any], max_seconds: int,
                             deadline_s: float | None = None, sleep: Callable[[float], Any] = time.sleep,
                             clock: Callable[[], float] = time.monotonic) -> tuple[Any, ...]:
    """Start a simulation, wait for it, return (simulation_id, iteration_id, status)."""
    if not hasattr(freya, "api") or not hasattr(freya, "AGENT_LLM"):
        return freya.run_simulation(agent_id, name, variables, max_seconds=max_seconds)
    end = None if deadline_s is None else clock() + deadline_s

    def left() -> float:
        return float("inf") if end is None else end - clock()

    body = {
        "agentId": agent_id, "name": name, "mode": "chat", "selectedPersonaIds": list(variables),
        "customerVariablesMap": variables, "maxConcurrentCalls": int(os.environ.get("FREYA_SIM_CONCURRENCY", "5")),
        "runsPerPersona": 1, "maxCallDurationSeconds": max_seconds,
        "assistantLlmOverride": freya.AGENT_LLM, "routingLlmOverride": freya.AGENT_LLM,
        "extractionLlmOverride": freya.AGENT_LLM, "personaLlmOverride": getattr(freya, "SIM_LLM", freya.AGENT_LLM),
        "analysisLlmOverride": getattr(freya, "SIM_LLM", freya.AGENT_LLM),
    }
    while True:
        try:
            sim = freya.api("POST", "/api/v2/simulations", body)
            break
        except RuntimeError as e:
            if "concurrent simulations" not in str(e):
                raise
            if left() <= BUSY_WAIT_S:
                raise SimPollError(f"another simulation kept the workspace busy until the time limit: {e}") from e
            sleep(BUSY_WAIT_S)
    sim_id = (sim.get("simulation") or sim)["id"]

    def get(path: str) -> Any:
        failures = 0
        while True:
            try:
                return freya.api("GET", path)
            except RuntimeError as e:
                if not is_server_error(e):
                    raise
                failures += 1
                wait = backoff_s(failures)
                if failures >= MAX_CONSECUTIVE_POLL_FAILURES or left() <= wait:
                    raise SimPollError(f"polling stopped after {failures} consecutive server error(s): {e}") from e
                sleep(wait)

    while True:
        if left() <= 0:
            raise SimPollError(f"simulation {sim_id} did not finish before the time limit")
        sleep(POLL_INTERVAL_S)
        s = get(f"/api/v2/simulations/{sim_id}")
        s = s.get("simulation") or s
        if s.get("status") in FINAL_STATUSES:
            break
    its = get(f"/api/v2/simulations/{sim_id}/iterations")
    its = its.get("iterations") or its.get("items") or its
    return sim_id, its[0]["id"], s.get("status")
