"""FALLBACK probe runner, used only because the platform's simulation queue was stuck.

A stuck, orphaned simulation (98809114-...) kept the workspace's single simulation slot busy for hours
and the platform returned 5xx on many calls, so tools/run_cases.py (rehearsal/probe_runner.py) could not run.
This harness drives the SAME probe cases through POST /api/v2/chat/message instead of a simulation:
 - customer: an LLM playing the exact persona text from run_cases.persona_for (same prompt as the simulator persona);
 - scoring:  the bank ledger through tools/score.py score_ledger (same as the simulation path);
 - judge:    an LLM reading the transcript against the case checks. It is NOT the platform judge; labelled "fallback_judge".
    python rehearsal/chat_probe.py <workflow_id> --out results/x.json [--only A,B] [--reps 1]
"""
import argparse, json, pathlib, sys, threading, time
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import run_cases, freya
from score import http, score_ledger
from builder.llm import LLMClient

CASES = json.load(open(ROOT / "rehearsal" / "probe-cases.json"))
MAX_TURNS = 26
llm = LLMClient(max_attempts=4)

def api_retry(method, path, body=None, tries=3):
    last = None
    for k in range(tries):
        try:
            return freya.api(method, path, body)
        except RuntimeError as e:
            last = e
            if k + 1 < tries: time.sleep(8)
    raise last

def customer_turn(system, history):
    msgs = [{"role": "user" if h[0] == "agent" else "assistant", "content": h[1]} for h in history]
    for _ in range(3):
        try:
            r = llm.call("probe_customer", system, msgs, max_tokens=200, timeout_s=60)
        except Exception:  # noqa: BLE001
            continue
        out = " ".join(r.text.split())
        if out: return out
    return "Sorry, could you repeat that?"

def judge(case, transcript):
    system = "You grade a phone call transcript between a bank agent (Agent) and a customer (Customer). Answer with JSON only: {\"pass\": true|false, \"reason\": \"...\"}. pass is true only if EVERY check below is clearly satisfied by the transcript."
    prompt = "CHECKS:\n- " + "\n- ".join(case["checks"]) + "\n\nTRANSCRIPT:\n" + transcript
    try:
        r = llm.call("probe_judge", system, [{"role": "user", "content": prompt}], max_tokens=600, timeout_s=90).text
        j = json.loads(r[r.find("{"): r.rfind("}") + 1])
        return bool(j.get("pass")), str(j.get("reason"))[:600]
    except Exception as e:  # noqa: BLE001
        return False, f"judge error: {type(e).__name__}: {str(e)[:120]}"

def run_case(workflow_id, case, out, rep):
    row = {"id": case["id"], "carrier": case["carrier"], "rep": rep, "pass": False, "failed": [], "tools": [], "turns": 0}
    out.append(row)
    try:
        variables = http("/v1/practice/sessions", {"case": case["carrier"]})["variables"]
        row["customer_id"] = variables["customer_id"]
        system = run_cases.persona_for(case, {"identity": case["identity"]})["prompt"]
        res = api_retry("POST", "/api/v2/chat/message", {"workflowId": workflow_id, "message": "Hello?", "customerVariables": variables})
        call_id = res.get("callId"); row["call_id"] = call_id
        history = [("customer", "Hello?")]
        agent = res.get("response") or ""
        for turn in range(MAX_TURNS):
            if agent: history.append(("agent", agent))
            row["turns"] = turn + 1
            reply = customer_turn(system, history[1:]) if len(history) > 1 else customer_turn(system, [("agent", agent)])
            if not reply: break
            history.append(("customer", reply))
            if "goodbye" in reply.lower() and turn > 1 and "thank you, goodbye" in reply.lower(): break
            res = api_retry("POST", "/api/v2/chat/message", {"workflowId": workflow_id, "callId": call_id, "message": reply, "customerVariables": variables}, tries=2)
            agent = res.get("response") or ""
            if not agent: break
        try: api_retry("POST", "/api/v2/chat/end", {"callId": call_id}, tries=2)
        except Exception: pass
        row["transcript"] = "\n".join(f"{'Agent' if r == 'agent' else 'Customer'}: {t}" for r, t in history)
        led = None
        for _ in range(8):                      # the closing record can arrive a few seconds after the chat ended
            time.sleep(4)
            led = http("/v1/practice/ledger/" + variables["customer_id"])
            if led.get("calls") and led["calls"][-1]["tool"] == "close_call": break
        sc = score_ledger(case, led)
        row["failed"] = list(sc["failed"]); row["tools"] = [c["tool"] for c in led["calls"]]
        row["ledger"] = {k: led.get(k) for k in ("outcome", "reason", "promises", "notes", "callbacks", "transfers")}
        ok, why = judge(case, row["transcript"])
        row["fallback_judge_pass"], row["fallback_judge_reason"] = ok, why
        if not ok: row["failed"].append("conversation (fallback judge): " + why[:300])
        row["pass"] = not row["failed"]
    except Exception as e:  # noqa: BLE001
        row["infra_error"] = f"{type(e).__name__}: {str(e)[:300]}"; row["failed"].append("infra: " + row["infra_error"])

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("workflow_id"); ap.add_argument("--out", required=True); ap.add_argument("--only"); ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()
    cases = [c for c in CASES if not a.only or c["id"] in a.only.split(",")]
    rows = []
    t0 = time.time()
    for rep in range(a.reps):
        threads = [threading.Thread(target=run_case, args=(a.workflow_id, c, rows, rep + 1)) for c in cases]
        for i in range(0, len(threads), 5):
            for t in threads[i:i + 5]: t.start()
            for t in threads[i:i + 5]: t.join()
    rows.sort(key=lambda r: (r["rep"], r["id"]))
    for r in rows: print("PASS" if r["pass"] else "FAIL", r["rep"], r["id"], "|", " > ".join(r["tools"]) or "(no tools)", *[("\n     - " + f[:240]) for f in r["failed"]])
    final = []
    for c in cases:
        rs = [r for r in rows if r["id"] == c["id"]]
        final.append({"id": c["id"], "carrier": c["carrier"], "passes": sum(r["pass"] for r in rs), "runs": a.reps, "pass": all(r["pass"] for r in rs) and len(rs) == a.reps})
    print(f"{sum(f['pass'] for f in final)} / {len(final)} passed in all {a.reps} run(s), {time.time() - t0:.0f} s")
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"harness": "chat_probe (fallback, no simulation)", "workflow_id": a.workflow_id, "runs": a.reps, "final": final, "rows": rows}, open(a.out, "w"), indent=1)
main()
