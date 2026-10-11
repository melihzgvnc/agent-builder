"""Fakes for the online test stage: a world with a fake Freya client and a fake bank (no network)."""
from __future__ import annotations

import copy
import json
import time
from typing import Any

from builder.online_cases import GenCase
from builder.online_repair import MARKER


def ledger_for(case: GenCase, mode: str) -> dict[str, Any]:
    """A ledger built from the case's expectation, then bent by `mode`."""
    exp = copy.deepcopy(case.expected)
    calls: list[dict[str, Any]] = []
    if exp["outcome"] not in ("not_reached", "no_contact", "unreached"):
        calls.append({"tool": "verify_identity", "args": {}, "result": {"ok": True}})
    first = len(calls)  # index of the first promise call
    for p in exp["promises"]:
        calls.append({"tool": "create_promise", "args": {"product_id": p["product_id"], "amount": p["amount"], "date": p["date"]},
                      "result": {"ok": True}})
    led = {"ok": True, "outcome": exp["outcome"], "reason": exp["reason"], "promises": exp["promises"], "notes": exp["notes"],
           "callbacks": exp["callbacks"], "transfers": exp["transfers"], "failures": []}
    if mode == "wrong_outcome":
        led["outcome"] = "zz_wrong"
    elif mode == "behavior":
        led["promises"], calls = [], calls[:first]
        led["notes"] = [{"product_id": "p1", "reason": "x"}]
    elif mode == "bad_date" and exp["promises"]:
        bad = dict(exp["promises"][0], date="2099-01-01")
        led["promises"] = [bad]
        calls[first]["args"]["date"] = "2099-01-01"
    elif mode == "rejected" and exp["promises"]:
        calls[first]["result"] = {"ok": False, "error": "mock rule"}
        led["promises"] = []
    calls.append({"tool": "close_call", "args": {}, "result": {"ok": True}})
    led["calls"] = calls
    return led


class World:
    """Shared state: which ledger mode and judge verdict each case id gets, and the live workflow."""

    def __init__(self, cases: list[GenCase], modes: dict[str, str] | None = None) -> None:
        self.cases = {c.id: c for c in cases}
        self.modes = modes or {}
        self.judge: dict[str, str] = {}
        self.fixed_by_marker: set[str] = set()  # case ids that pass once a reminder is in the live workflow
        self.broken_by_marker: set[str] = set()  # case ids that fail once a reminder is in the live workflow
        self.live: dict[str, Any] = {}
        self.customer_case: dict[str, str] = {}
        self.persona_case: dict[str, str] = {}

    def marker_live(self) -> bool:
        return MARKER in json.dumps(self.live)

    def mode(self, case_id: str) -> str:
        if self.marker_live():
            if case_id in self.fixed_by_marker:
                return "ok"
            if case_id in self.broken_by_marker:
                return "behavior"
        return self.modes.get(case_id, "ok")


class FakeSimFreya:
    """The slice of the Freya client the online test uses, plus the calls of the other stages."""

    def __init__(self, world: World, sim_delay: float = 0.0, raise_on_sim: Exception | None = None,
                 fail_save_after: int | None = None) -> None:
        self.world = world
        self.calls: list[str] = []
        self.saved: list[dict[str, Any]] = []
        self.sim_delay = sim_delay
        self.raise_on_sim = raise_on_sim
        self.fail_save_after = fail_save_after
        self.persona_batches: list[int] = []
        self.sim_count = 0
        self._n = 0

    def create_personas(self, personas: list[dict[str, Any]]) -> dict[str, Any]:
        self.calls.append("create_personas")
        self.persona_batches.append(len(personas))
        out = []
        for p in personas:
            self._n += 1
            pid = f"persona-{self._n}"
            self.world.persona_case[pid] = str(p["name"]).split(" ")[0]
            out.append({"id": pid})
        return {"personas": out}

    def run_simulation(self, agent_id: str, name: str, variables: dict[str, Any], max_seconds: int = 420) -> tuple[str, str, str]:
        self.calls.append("run_simulation")
        self.sim_count += 1
        if self.raise_on_sim is not None:
            raise self.raise_on_sim
        if self.sim_delay:
            time.sleep(self.sim_delay)
        self.last_variables = variables
        for pid, v in variables.items():
            self.world.customer_case[v["customer_id"]] = self.world.persona_case.get(pid, "")
        self.last_personas = list(variables)
        return "sim-1", "it-1", "completed"

    def get_iteration(self, sim_id: str, it_id: str) -> dict[str, Any]:
        self.calls.append("get_iteration")
        runs = []
        for pid in self.last_personas:
            cid = self.world.persona_case.get(pid, "")
            verdict = self.world.judge.get(cid, "pass")
            run: dict[str, Any] = {"personaId": pid, "callId": f"call-{cid}", "reason": f"judge says {verdict}"}
            if verdict != "none":
                run["outcome"] = verdict
            runs.append(run)
        return {"runs": runs}

    def save_workflow(self, slot: str, workflow: dict[str, Any], state_file: str = "") -> dict[str, Any]:
        self.calls.append("save")
        if self.fail_save_after is not None and len(self.saved) >= self.fail_save_after:
            raise RuntimeError("save failed")
        self.saved.append(workflow)
        self.world.live = workflow
        return {"workflow_id": "wf_1", "agent_id": "ag_1", "version": 1 + len(self.saved)}

    def export_workflow(self, workflow_id: str, path: str) -> str:
        self.calls.append("export")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.world.live or {}, f)
        return path


class FakeBank:
    def __init__(self, world: World, fail_sessions: bool = False, fail_ledger: bool = False) -> None:
        self.world = world
        self.fail_sessions = fail_sessions
        self.fail_ledger = fail_ledger
        self.n = 0
        self.session_calls: list[str] = []

    def session(self, case_id: str) -> dict[str, Any]:
        self.session_calls.append(case_id)
        if self.fail_sessions:
            raise RuntimeError("no session")
        self.n += 1
        return {"customer_id": f"cust-{self.n}", "first_name": "A"}

    def ledger(self, customer_id: str) -> dict[str, Any]:
        if self.fail_ledger:
            raise RuntimeError("ledger down")
        cid = self.world.customer_case[customer_id]
        return ledger_for(self.world.cases[cid], self.world.mode(cid))
