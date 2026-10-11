"""Online test for the flow mode: personas (stage C), simulations, judge verdicts, policy patches (stage D),
keep-best and rollback. Every failure degrades loudly; the saved agent is always kept."""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

from builder import flow_assemble, flow_docs, flow_llm, flow_policy, sim_poll

MAX_PERSONAS = 10
SCENARIO_WORDS = ("scenario", "example", "brief")
_EXAMPLE_HEAD = re.compile(r"^#{1,4}\s*(?:example|scenario)\s*#?\d+\b", re.IGNORECASE | re.MULTILINE)
SIM_MAX_S = 600.0
CALL_MAX_S = 300
MAX_PATCH_ROUNDS = 3
RESERVE_S = 90.0
MAX_FAILED_PER_PATCH = 3


class Skipped(Exception):
    pass


class Infra(Exception):
    """The platform or the mock is down; stop testing, keep the saved agent."""


def transcript(call: dict[str, Any], cap: int = 7000) -> str:
    lines: list[str] = []
    for m in call.get("messages") or []:
        role, msg = m.get("role"), m.get("message")
        if role in ("user", "assistant"):
            lines.append(("CALLER: " if role == "user" else "ASSISTANT: ") + str(msg))
        elif role == "system":
            try:
                ev = json.loads(msg) if isinstance(msg, str) else msg
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("event")
            if kind == "function_call_start":
                lines.append(f"  [tool call {ev.get('function')} {json.dumps(ev.get('args'), ensure_ascii=False)[:500]}]")
            elif kind == "function_call_complete":
                lines.append(f"  [tool result {ev.get('function')} {json.dumps(ev.get('result'), ensure_ascii=False)[:500]}]")
            elif kind == "error":
                lines.append(f"  [error {str(ev.get('message'))[:200]}]")
    text = "\n".join(lines) or str(call.get("transcript") or "")
    return flow_docs.trim(text, cap)


def make_personas(ctx: Any, llm: Any) -> list[dict[str, Any]]:
    var_names = ctx.plan.var_names()
    picked = [d for d in ctx.docset.docs if any(w in d.rel.lower() for w in SCENARIO_WORDS)] or ctx.docset.docs
    ds = flow_docs.DocSet(docs=picked)
    n_examples = max(0, sum(len(_EXAMPLE_HEAD.findall(d.text)) for d in picked))
    base = (flow_docs.render(ds) + "\n\n=== EXPECTED BEHAVIOUR (the assistant's policy) ===\n"
            + json.dumps({k: ctx.policy.get(k) for k in ("rules", "never", "handovers", "silence", "out_of_scope", "tries", "anger")}, ensure_ascii=False)[:14000]
            + "\n\n=== TEST DATA SUMMARY ===\n" + ctx.docset.test_data_summary
            + "\n\n=== CALL VARIABLE NAMES ===\n" + ", ".join(var_names))
    system = flow_llm.STAGE_C_SYSTEM.replace("{n}", str(MAX_PERSONAS)).replace("{{", "{").replace("}}", "}")
    raw_all: list[Any] = []
    if n_examples:
        ex = flow_llm.ask_json(llm, "flow stage C: example personas", system + f"\nThis call: write ONLY the personas for the example scenarios of the documents: exactly one per example scenario ({n_examples} of them), none skipped, source=example.", base, max_tokens=9000)
        raw_all += (ex.get("personas") or []) if isinstance(ex, dict) else ex
    room = MAX_PERSONAS - len(raw_all)
    if room > 0:
        taken = ", ".join(str(p.get("id")) for p in raw_all if isinstance(p, dict))
        gen = flow_llm.ask_json(llm, "flow stage C: generated personas", system + f"\nThis call: write {room} NEW personas (source=generated) that cover behaviours the example scenarios do not. Already written, do not repeat: {taken or 'none'}.", base, max_tokens=9000)
        raw_all += (gen.get("personas") or []) if isinstance(gen, dict) else gen
    raw: Any = raw_all
    if isinstance(raw, dict):
        raw = raw.get("personas") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not str(p.get("prompt") or "").strip():
            continue
        pid = flow_policy.snake(p.get("id") or p.get("name") or f"p{len(out)}")
        if pid in seen:
            pid = f"{pid}_{len(out)}"
        seen.add(pid)
        crit = p.get("successCriteria")
        crit_text = "\n".join(f"- {c}" for c in crit) if isinstance(crit, list) else str(crit or "")
        vars_ = p.get("variables") if isinstance(p.get("variables"), dict) else {}
        out.append({"id": pid, "kind": str(p.get("kind") or "edge"), "name": str(p.get("name") or pid)[:80],
                    "prompt": str(p["prompt"]), "successCriteria": crit_text,
                    "variables": {k: str(v) for k, v in vars_.items() if k in var_names}, "source": str(p.get("source") or "generated")})
        if len(out) >= MAX_PERSONAS:
            break
    return out


def ensure_personas(freya: Any, personas: list[dict[str, Any]], store: Path) -> dict[str, str]:
    try:
        ids = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    except (OSError, ValueError):
        ids = {}
    key = {p["id"]: hashlib.sha1(json.dumps([p["name"], p["prompt"], p["successCriteria"]], sort_keys=True).encode()).hexdigest()[:16]
           for p in personas}
    new = [p for p in personas if key[p["id"]] not in ids]
    if new:
        made = freya.create_personas([{"name": p["name"], "prompt": p["prompt"], "successCriteria": p["successCriteria"],
                                       "tags": ["flow", p["kind"]]} for p in new])["personas"]
        for p, m in zip(new, made):
            ids[key[p["id"]]] = m["id"]
        store.write_text(json.dumps(ids, indent=1), encoding="utf-8")
    return {p["id"]: ids[key[p["id"]]] for p in personas}


def run_round(freya: Any, agent_id: str, name: str, personas: list[dict[str, Any]], pmap: dict[str, str], deadline_s: float) -> dict[str, dict[str, Any]]:
    """One simulation with the given personas; {persona id: {outcome, reason, call_id}}. Raises Infra."""
    variables = {pmap[p["id"]]: p["variables"] for p in personas}
    try:
        sim_id, it_id, status = sim_poll.run_simulation_resilient(freya, agent_id, name, variables, CALL_MAX_S, deadline_s=deadline_s)
        runs = freya.get_iteration(sim_id, it_id)["runs"]
    except Exception as e:  # noqa: BLE001 - never retried here
        raise Infra(f"{type(e).__name__}: {str(e)[:300]}") from e
    by_persona = {r.get("personaId"): r for r in runs}
    out: dict[str, dict[str, Any]] = {}
    for p in personas:
        r = by_persona.get(pmap[p["id"]]) or {}
        out[p["id"]] = {"outcome": str(r.get("outcome") or "none"), "reason": " ".join(str(r.get("reason") or "").split())[:500],
                        "call_id": r.get("callId"), "passed": r.get("outcome") == "pass"}
    return out


def score(results: dict[str, dict[str, Any]]) -> int:
    return sum(1 for r in results.values() if r.get("passed"))


def _save(ctx: Any, freya: Any, wf: dict[str, Any]) -> None:
    info = freya.save_workflow(ctx.slot, wf, state_file=str(ctx.root / ".freya-agents.json"))
    ctx.saved = info
    ctx.trace.log_artifact("updated", str(info["workflow_id"]), str(info.get("agent_id")), version=info.get("version"),
                           note=f"slot={ctx.slot} online test")


def patch_once(ctx: Any, llm: Any, failed: list[dict[str, Any]], results: dict[str, dict[str, Any]], freya: Any) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    """Ask stage D for one patch; return (workflow, policy, diagnosis) or None when nothing usable came back."""
    parts = []
    for p in failed[:MAX_FAILED_PER_PATCH]:
        r = results[p["id"]]
        try:
            tr = transcript(freya.get_call(r["call_id"])) if r.get("call_id") else "(no call id)"
        except Exception as e:  # noqa: BLE001
            tr = f"(transcript unavailable: {type(e).__name__})"
        parts.append(f"--- FAILED PERSONA {p['id']} ({p['kind']}) ---\nScript: {p['prompt']}\nCriteria:\n{p['successCriteria']}\n"
                     f"Variables: {json.dumps(p['variables'], ensure_ascii=False)}\nJudge reason: {r['reason']}\nTranscript:\n{tr}")
    user = ("POLICY:\n" + json.dumps(ctx.policy, ensure_ascii=False) + "\n\nRESOLVED CONFLICTS (do not undo):\n"
            + json.dumps(ctx.policy.get("conflicts_resolved", []), ensure_ascii=False) + "\n\n" + "\n\n".join(parts))
    patch = flow_llm.ask_json(llm, "flow stage D: patch", flow_llm.STAGE_D_SYSTEM, user, max_tokens=4000)
    ops = patch.get("ops") if isinstance(patch, dict) else None
    if not isinstance(ops, list) or not ops:
        ctx.trace.note(f"stage D: no change proposed ({(patch or {}).get('diagnosis') if isinstance(patch, dict) else ''})")
        return None
    new_policy = flow_policy.apply_patch(ctx.policy, [o for o in ops if isinstance(o, dict)])
    wf, pol, _rep = flow_assemble.validate_with_repair(new_policy, ctx.plan, ctx.slot, None, freya, ctx.trace, False, max_rounds=0)
    return wf, pol, str(patch.get("diagnosis") or "")


def run_online(ctx: Any, mock_health: Callable[[str | None], tuple[bool, str]], stage_export: Callable[[Any], str],
               clock: Callable[[], float] = time.monotonic) -> str:
    freya = ctx.client if ctx.client is not None else __import__("freya")
    trace = ctx.trace
    ok, msg = mock_health(ctx.server_url)
    trace.note(f"mock health: {msg}")
    if not ok:
        trace.warn(f"DEGRADED: mock unreachable ({msg}); online test skipped, the saved agent is kept")
        print(f"WARNING: DEGRADED: mock unreachable ({msg}); online test skipped", file=sys.stderr, flush=True)
        raise Skipped("mock unreachable")
    t0 = clock()
    limit = float(getattr(ctx, "online_limit_s", 900.0))
    llm = ctx.llm
    left = lambda: limit - (clock() - t0)  # noqa: E731
    log: dict[str, Any] = {"personas": [], "rounds": [], "patches": [], "decision": "", "seconds": 0.0}
    personas = make_personas(ctx, llm)
    if not personas:
        raise Skipped("stage C produced no personas")
    log["personas"] = personas
    try:
        pmap = ensure_personas(freya, personas, ctx.root / ".personas_flow.json")
        agent_id = str(ctx.saved["agent_id"])
        results = run_round(freya, agent_id, f"{ctx.slot} flow r0", personas, pmap, min(SIM_MAX_S, left()))
    except Infra as e:
        return _finish(ctx, log, t0, clock, f"DEGRADED: infra ({e}); agent kept", degraded=True)
    except Exception as e:  # noqa: BLE001
        return _finish(ctx, log, t0, clock, f"DEGRADED: personas could not be created ({type(e).__name__}: {e}); agent kept", degraded=True)
    log["rounds"].append({"round": 0, "which": "all", "score": score(results), "results": results})
    trace.log_test("simulation", ctx.slot, "round 0 (all personas)", passed=score(results), total=len(personas))
    best = {"score": score(results), "workflow": ctx.workflow, "policy": ctx.policy, "results": results}
    cur, cur_results = dict(best), results
    live = "best"
    for rnd in range(1, MAX_PATCH_ROUNDS + 1):
        failed = [p for p in personas if not cur_results[p["id"]]["passed"]]
        if not failed or left() < RESERVE_S + 60:
            break
        try:
            got = patch_once(ctx, llm, failed, cur_results, freya)
        except Exception as e:  # noqa: BLE001
            trace.warn(f"stage D failed in round {rnd}: {type(e).__name__}: {e}")
            break
        if got is None:
            break
        wf, pol, diag = got
        try:
            _save(ctx, freya, wf)
            live = "patched"
            sub = run_round(freya, str(ctx.saved["agent_id"]), f"{ctx.slot} flow r{rnd}", failed, pmap, min(SIM_MAX_S, left()))
        except Infra as e:
            log["decision"] = f"infra during round {rnd}: {e}"
            break  # live is "patched" here: the best workflow is re-saved below
        merged = {**cur_results, **sub}
        log["patches"].append({"round": rnd, "diagnosis": diag, "reran": [p["id"] for p in failed], "rerun_passes": score(sub)})
        log["rounds"].append({"round": rnd, "which": [p["id"] for p in failed], "score": score(merged), "results": sub})
        trace.log_test("simulation", ctx.slot, f"round {rnd} (re-run failed only) patch: {diag[:120]}", passed=score(sub), total=len(failed))
        if score(sub) == 0:  # no improvement on the failures: roll back
            log["patches"][-1]["decision"] = "rolled back (no failed persona improved)"
            try:
                _save(ctx, freya, best["workflow"])
                live = "best"
            except Exception as e:  # noqa: BLE001
                trace.warn(f"DEGRADED: rollback save failed: {e}")
            break
        ctx.workflow, ctx.policy = wf, pol
        cur = {"score": score(merged), "workflow": wf, "policy": pol, "results": merged}
        cur_results = merged
        if left() > RESERVE_S + 120:  # confirm with a full run
            try:
                full = run_round(freya, str(ctx.saved["agent_id"]), f"{ctx.slot} flow confirm{rnd}", personas, pmap, min(SIM_MAX_S, left()))
            except Infra as e:
                log["decision"] = f"infra during confirm {rnd}: {e}"
                if cur["score"] > best["score"]:  # the failures improved and nothing else was re-run: keep, unconfirmed
                    best, live = dict(cur), "best"
                    log["patches"][-1]["decision"] = "kept (unconfirmed: the confirming run did not finish)"
                    trace.warn(f"DEGRADED: round {rnd} patch kept without a confirming full run (estimated {cur['score']}/{len(personas)})")
                break
            log["rounds"].append({"round": rnd, "which": "all (confirm)", "score": score(full), "results": full})
            trace.log_test("simulation", ctx.slot, f"round {rnd} confirm (all personas)", passed=score(full), total=len(personas))
            cur_results = full
            cur = {"score": score(full), "workflow": wf, "policy": pol, "results": full}
        if cur["score"] > best["score"]:
            best, live = dict(cur), "best"
            log["patches"][-1]["decision"] = "kept"
        else:
            log["patches"][-1]["decision"] = "rolled back (score did not improve)"
            try:
                _save(ctx, freya, best["workflow"])
                live = "best"
            except Exception as e:  # noqa: BLE001
                trace.warn(f"DEGRADED: rollback save failed: {e}")
            break
        if best["score"] == len(personas):
            break
    if live != "best":
        try:
            _save(ctx, freya, best["workflow"])
        except Exception as e:  # noqa: BLE001
            trace.warn(f"DEGRADED: final save of the best workflow failed: {e}")
    ctx.workflow, ctx.policy = best["workflow"], best["policy"]
    (ctx.run_dir / "workflow.json").write_text(json.dumps(ctx.workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (ctx.run_dir / "spec.json").write_text(json.dumps(ctx.policy, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        stage_export(ctx)
    except Exception as e:  # noqa: BLE001
        trace.warn(f"re-export after the online test failed: {type(e).__name__}: {e}")
    log["best_score"], log["best_results"] = best["score"], best["results"]
    log["decision"] = log["decision"] or f"kept best score {best['score']}/{len(personas)}"
    return _finish(ctx, log, t0, clock, f"best {best['score']}/{len(personas)} personas passed; {log['decision']}")


def _finish(ctx: Any, log: dict[str, Any], t0: float, clock: Callable[[], float], detail: str, degraded: bool = False) -> str:
    log["seconds"] = round(clock() - t0, 1)
    log["detail"] = detail
    (ctx.run_dir / "online_test.json").write_text(json.dumps(log, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    if degraded:
        ctx.trace.warn(detail)
        print("WARNING: " + detail, file=sys.stderr, flush=True)
    return detail
