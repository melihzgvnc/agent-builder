"""Budget guard, bank adapter and one-simulation-at-a-time batch runner for the online test.

Scoring is reused from the organisers' kit (tools/run_cases.py persona_for, tools/score.py
score_ledger and http); nothing is re-implemented here. No call is ever retried: an error marks
the case or batch `infra` and the caller decides what to do. Does not import builder.pipeline.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Protocol

from builder.online_cases import GenCase
from builder.sim_poll import run_simulation_resilient

BATCH_SIZE: int = 5  # matches the default simulation concurrency
STAGE_SOFT_MARGIN_S: float = 45.0  # the stage returns this long before its hard time limit
REPUBLISH_RESERVE_S: float = 150.0  # kept back while a repaired workflow is live (rollback save + export)
EXPORT_RESERVE_S: float = 30.0  # kept back otherwise
DEFAULT_BATCH_ESTIMATE_S: float = 180.0
MIN_BATCH_ESTIMATE_S: float = 60.0
SIM_MAX_SECONDS: int = 240
KIT_DIR: Path = Path(__file__).resolve().parent.parent


class Budget:
    """Time guard with an injectable clock. `deadline` is the last moment a batch may be running."""

    def __init__(self, stage_budget_s: float, total_deadline: float | None = None,
                 clock: Callable[[], float] = time.monotonic, margin_s: float = STAGE_SOFT_MARGIN_S) -> None:
        self.clock = clock
        self.start = clock()
        hard = self.start + stage_budget_s
        self.deadline = hard - margin_s
        if total_deadline is not None:
            hard = min(hard, total_deadline)
            self.deadline = min(self.deadline, total_deadline)
        self.hard_deadline = hard

    def remaining(self) -> float:
        return self.deadline - self.clock()

    @staticmethod
    def reserve(repaired_live: bool) -> float:
        return REPUBLISH_RESERVE_S if repaired_live else EXPORT_RESERVE_S

    def can_start(self, estimate_s: float, reserve_s: float) -> bool:
        return self.remaining() - reserve_s >= estimate_s

    def can_mutate(self) -> bool:
        """A save is allowed until the hard limit; an abandoned worker must never save late."""
        return self.clock() < self.hard_deadline


def next_estimate(measured_s: float | None) -> float:
    if measured_s is None:
        return DEFAULT_BATCH_ESTIMATE_S
    return max(1.25 * measured_s, MIN_BATCH_ESTIMATE_S)


def load_scoring(require_key: bool = True, kit_dir: Path = KIT_DIR) -> Any | None:
    """persona_for / score_ledger / http from the kit's tools, or None when unavailable."""
    if require_key and not os.environ.get("FREYA_BANK_TEAM_KEY"):
        return None
    tools = kit_dir / "tools"
    if not (tools / "run_cases.py").is_file() or not (tools / "score.py").is_file():
        return None
    for p in (str(tools), str(kit_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        import run_cases  # type: ignore[import-not-found]
        import score  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - the stage degrades instead of crashing
        return None
    return SimpleNamespace(persona_for=run_cases.persona_for, score_ledger=score.score_ledger, http=score.http)


class Bank(Protocol):
    def session(self, case_id: str) -> dict[str, Any]: ...
    def ledger(self, customer_id: str) -> dict[str, Any]: ...


class LedgerSource(Protocol):
    def ledger(self, customer_id: str) -> dict[str, Any]: ...


class OrganizerLedgerSource:
    """The organisers' bank, read through the kit's own http helper."""

    def __init__(self, scoring: Any) -> None:
        self._http = scoring.http

    def ledger(self, customer_id: str) -> dict[str, Any]:
        resp = self._http("/v1/practice/ledger/" + str(customer_id))
        return resp if isinstance(resp, dict) else {}


def server_origin(server_url: str) -> str:
    """scheme://host[:port] of a server url; any path such as /v1 is dropped."""
    parts = urllib.parse.urlsplit(server_url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"not an http(s) url: {server_url!r}")
    return f"{parts.scheme}://{parts.netloc}"


class MockLedgerSource:
    """A self-hosted bank mock: GET <origin>/v1/practice/ledger/<id>. Raises on any network or 5xx error."""

    def __init__(self, server_url: str, auth_header: str | None = None, auth_value: str | None = None,
                 opener: Callable[..., Any] | None = None, timeout: float = 20.0) -> None:
        self.origin = server_origin(server_url)
        self.auth = (auth_header, auth_value) if auth_header and auth_value else None
        self._opener = opener
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if self.auth:
            headers[self.auth[0]] = self.auth[1]
        req = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        opener = self._opener or urllib.request.urlopen
        with opener(req, timeout=self.timeout) as resp:
            status = getattr(resp, "status", 200)
            if status >= 400:
                raise RuntimeError(f"{method} {path}: HTTP {status}")
            return json.loads(resp.read().decode("utf-8"))

    def ledger(self, customer_id: str) -> dict[str, Any]:
        resp = self._call("GET", "/v1/practice/ledger/" + urllib.parse.quote(str(customer_id), safe=""))
        return resp if isinstance(resp, dict) else {}

    def session(self, case_id: str) -> Any:
        return self._call("POST", "/v1/practice/sessions", {"case": case_id})


class HttpBank:
    """The organisers' bank over the kit's own http helper (ledger reads go through a LedgerSource)."""

    def __init__(self, scoring: Any, source: LedgerSource | None = None) -> None:
        self._http = scoring.http
        self._source: LedgerSource = source if source is not None else OrganizerLedgerSource(scoring)

    def session(self, case_id: str) -> dict[str, Any]:
        resp = self._http("/v1/practice/sessions", {"case": case_id})
        variables = resp.get("variables") if isinstance(resp, dict) else None
        if not isinstance(variables, dict) or not variables.get("customer_id"):
            raise RuntimeError(f"no session for case {case_id}: {str(resp)[:200]}")
        return variables

    def ledger(self, customer_id: str) -> dict[str, Any]:
        return self._source.ledger(customer_id)


class MockBank:
    """Session and ledger of a self-hosted mock (selected when a server url override is set)."""

    def __init__(self, source: MockLedgerSource) -> None:
        self._source = source

    def session(self, case_id: str) -> dict[str, Any]:
        resp = self._source.session(case_id)
        variables = resp.get("variables") if isinstance(resp, dict) else None
        if not isinstance(variables, dict) or not variables.get("customer_id"):
            raise RuntimeError(f"no session for case {case_id}: {str(resp)[:200]}")
        return variables

    def ledger(self, customer_id: str) -> dict[str, Any]:
        return self._source.ledger(customer_id)


@dataclass
class CaseResult:
    id: str
    kind: str
    ledger_pass: bool = False
    conversation_pass: bool = False
    judge_verdict: bool = False
    failed: list[str] = field(default_factory=list)
    judge_reason: str = ""
    call_id: str | None = None
    customer_id: str | None = None
    ledger: dict[str, Any] = field(default_factory=dict)
    infra_error: str | None = None

    @property
    def infra(self) -> bool:
        return self.infra_error is not None

    @property
    def passed(self) -> bool:
        return not self.infra and self.ledger_pass and self.conversation_pass

    @property
    def judge_noise(self) -> bool:
        """Ledger fine, but the judge gave no verdict."""
        return not self.infra and self.ledger_pass and not self.judge_verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "passed": self.passed, "ledger_pass": self.ledger_pass,
            "conversation_pass": self.conversation_pass, "judge_verdict": self.judge_verdict,
            "failed": list(self.failed), "judge_reason": self.judge_reason, "call_id": self.call_id,
            "customer_id": self.customer_id, "infra_error": self.infra_error,
            "tools": [str(c.get("tool")) for c in self.ledger.get("calls") or [] if isinstance(c, dict)],
        }


def ensure_personas(freya: Any, scoring: Any, cases: list[GenCase], store: Path) -> dict[str, str]:
    """Persona ids per case id; only personas whose text is new are created (same cache format as run_cases)."""
    try:
        ids = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    except (OSError, ValueError):
        ids = {}
    if not isinstance(ids, dict):
        ids = {}
    spec = {c.id: scoring.persona_for(c.as_case(), {"identity": c.identity}) for c in cases}
    key = {i: i + ":" + hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10] for i, p in spec.items()}
    new = [c for c in cases if key[c.id] not in ids]
    if new:
        made = freya.create_personas([spec[c.id] for c in new])["personas"]
        ids.update({key[c.id]: p["id"] for c, p in zip(new, made)})
        store.write_text(json.dumps(ids, indent=1), encoding="utf-8")
    return {c.id: ids[key[c.id]] for c in cases}


def _all_infra(cases: list[GenCase], why: str) -> list[CaseResult]:
    return [CaseResult(c.id, c.kind, infra_error=why) for c in cases]


def run_batch(freya: Any, bank: Bank, scoring: Any, cases: list[GenCase], agent_id: str,
              budget: Budget, reserve_s: float, personas_path: Path) -> list[CaseResult]:
    """One simulation with every case once; scored from the ledger plus the judge's reading. No retries."""
    if not cases:
        return []
    try:
        persona_ids = ensure_personas(freya, scoring, cases, personas_path)
    except Exception as e:  # noqa: BLE001
        return _all_infra(cases, f"personas: {type(e).__name__}: {e}")
    results: dict[str, CaseResult] = {}
    sessions: dict[str, dict[str, Any]] = {}
    for c in cases:
        try:
            sessions[c.id] = bank.session(c.carrier_id)
        except Exception as e:  # noqa: BLE001
            results[c.id] = CaseResult(c.id, c.kind, infra_error=f"session: {type(e).__name__}: {e}")
    live = [c for c in cases if c.id in sessions]
    if live:
        out: dict[str, Any] = {}

        def target() -> None:
            try:
                out["value"] = run_simulation_resilient(
                    freya, agent_id, f"online {time.strftime('%H:%M:%S')}",
                    {persona_ids[c.id]: sessions[c.id] for c in live}, SIM_MAX_SECONDS,
                    deadline_s=budget.remaining() - reserve_s)
            except BaseException as e:  # noqa: BLE001
                out["error"] = e

        t = threading.Thread(target=target, name="online-sim", daemon=True)
        t.start()
        t.join(max(0.0, budget.remaining() - reserve_s))
        if t.is_alive():
            why = "simulation did not finish in the time budget; abandoned (it may keep the workspace busy)"
        elif "error" in out:
            e = out["error"]
            why = f"simulation: {type(e).__name__}: {e}"
        else:
            why = ""
        if why:
            for c in live:
                results[c.id] = CaseResult(c.id, c.kind, infra_error=why)
        else:
            sim_id, it_id = out["value"][0], out["value"][1]
            try:
                runs = freya.get_iteration(sim_id, it_id)["runs"]
                judge = {r.get("personaId"): r for r in runs}
            except Exception as e:  # noqa: BLE001
                for c in live:
                    results[c.id] = CaseResult(c.id, c.kind, infra_error=f"iteration: {type(e).__name__}: {e}")
                judge = None
            if judge is not None:
                for c in live:
                    results[c.id] = _score_case(bank, scoring, c, sessions[c.id], judge.get(persona_ids[c.id]) or {})
    return [results[c.id] for c in cases]


def _score_case(bank: Bank, scoring: Any, c: GenCase, session: dict[str, Any], j: dict[str, Any]) -> CaseResult:
    cid = str(session.get("customer_id"))
    try:
        led = bank.ledger(cid)
        if not isinstance(led, dict) or "calls" not in led or led.get("ok") is False:
            raise RuntimeError(f"no usable ledger: {str(led)[:160]}")
        row = scoring.score_ledger(c.as_case(), led)
    except Exception as e:  # noqa: BLE001
        return CaseResult(c.id, c.kind, customer_id=cid, infra_error=f"ledger: {type(e).__name__}: {e}")
    verdict = bool(j.get("outcome"))
    reason = " ".join(str(j.get("reason") or "no verdict").split())[:400]
    failed = list(row["failed"])
    conv_ok = j.get("outcome") == "pass"
    if not conv_ok:
        failed.append("conversation: " + reason)
    return CaseResult(c.id, c.kind, ledger_pass=not row["failed"], conversation_pass=conv_ok, judge_verdict=verdict,
                      failed=failed, judge_reason=reason, call_id=j.get("callId"), customer_id=cid, ledger=led)
