"""Run rehearsal/probe-cases.json against an agent with the kit's own helpers (tools/run_cases.py).

Same machinery as run_cases.py (persona_for, run_once, score_ledger, judge), except:
 - probe ids (PROBE-xx) are mapped to their carrier account (PUB-xx) when a session is opened,
   because the bank mock only serves known case accounts;
 - the persona cache lives in rehearsal/ (not the repo-root .personas.json);
 - no write to results/log.jsonl.
    python rehearsal/probe_runner.py <agent_id> --out results/rehearsal-probes.json [--reps 2] [--only A,B]
"""
import argparse, hashlib, json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import run_cases, freya

import time
_orig_api = freya.api
def api(method, path, body=None, **params):
    """Same as freya.api, but GET requests are retried on 5xx (the platform returned intermittent 503/504 while polling)."""
    for k in range(12):
        try:
            return _orig_api(method, path, body, **params)
        except RuntimeError as e:
            if method != "GET" or not any(f"-> {c}" in str(e) for c in (500, 502, 503, 504)) or k == 11: raise
            print(f"transient {str(e)[:60]!r}, retry {k + 1}", flush=True); time.sleep(15)
freya.api = api

CASES = json.load(open(ROOT / "rehearsal" / "probe-cases.json"))
CARRIER = {c["id"]: c["carrier"] for c in CASES}
_orig_http = run_cases.http
def http(path, body=None):
    if path == "/v1/practice/sessions" and body and body.get("case") in CARRIER:
        body = {"case": CARRIER[body["case"]]}
    return _orig_http(path, body)
run_cases.http = http

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("agent_id"); ap.add_argument("--out", required=True); ap.add_argument("--reps", type=int, default=1); ap.add_argument("--only")
    a = ap.parse_args()
    cases = [c for c in CASES if not a.only or c["id"] in a.only.split(",")]
    store = ROOT / "rehearsal" / ".personas-probes.json"; ids = json.load(open(store)) if store.exists() else {}
    spec = {c["id"]: run_cases.persona_for(c, {"identity": c["identity"]}) for c in cases}
    key = {i: i + ":" + hashlib.sha1(json.dumps(p, sort_keys=True).encode()).hexdigest()[:10] for i, p in spec.items()}
    new = [c for c in cases if key[c["id"]] not in ids]
    if new:
        made = freya.create_personas([spec[c["id"]] for c in new])["personas"]
        ids.update({key[c["id"]]: p["id"] for c, p in zip(new, made)}); json.dump(ids, open(store, "w"), indent=1)
    pid = {c["id"]: ids[key[c["id"]]] for c in cases}
    reps, total = [], 0
    for r in range(a.reps):
        rows, sim_id, it_id, status, took, med = run_cases.run_once(a.agent_id, cases, pid)
        for row, c in zip(rows, cases): row["carrier"] = c["carrier"]
        reps.append({"rows": rows, "simulation_id": sim_id, "iteration_id": it_id, "status": status, "seconds": round(took)}); total += took
        print(f"-- run {r + 1} of {a.reps}: {sum(x['pass'] for x in rows)} / {len(rows)} passed, {took:.0f} s\n", flush=True)
    final = [{"id": c["id"], "carrier": c["carrier"], "passes": sum(rp["rows"][k]["pass"] for rp in reps), "runs": a.reps} for k, c in enumerate(cases)]
    for f in final: f["pass"] = f["passes"] == a.reps
    print(f"{sum(f['pass'] for f in final)} / {len(final)} passed in all {a.reps} run(s)")
    pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    json.dump({"agent_id": a.agent_id, "runs": a.reps, "seconds": round(total), "final": final, "reps": reps}, open(a.out, "w"), indent=1)
main()
