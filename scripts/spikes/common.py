"""Shared helpers for the Phase 2a platform spikes. Run with: source env.sh && uv run python scripts/spikes/<file>.py"""
import json, pathlib, sys, time
ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import freya
from score import http

BANK = "https://freya-bank-mock.freyavoice.workers.dev"
FIX = ROOT / "tests" / "fixtures"
CASES = {c["id"]: c for c in json.load(open(ROOT / "public-test-set.json"))}


def session(case="PUB-11"):
    """Seeded practice customer: (call variables, identity answers)."""
    return http("/v1/practice/sessions", {"case": case})["variables"], CASES[case]["identity"]


def ledger(customer_id):
    return http("/v1/practice/ledger/" + customer_id)


def tool(name, path, body_props, *, post=None, pre=None, mapping=None, always_at=None, desc="", timeout=12000, retries=0):
    """api_call tool. body_props: {field: default-string-or-(default, extra schema)}; a default of None means no default (model fills)."""
    props = {}
    for k, v in body_props.items():
        d = {"type": "string"}
        if isinstance(v, tuple): v, extra = v; d.update(extra)
        if v is not None: d["default"] = v
        props[k] = d
    t = {"type": "function", "config": {"timeoutMs": timeout, "maxRetries": retries},
         "function": {"name": name, "handler": "__function__:api_call", "description": desc,
                      "parameters": {"type": "object", "properties": {
                          "url": {"type": "string", "default": BANK + path}, "method": {"type": "string", "default": "POST"},
                          "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                          "body": {"type": "object", "properties": props, "required": [k for k, v in body_props.items() if v is None]}}}}}
    if always_at: t["config"]["alwaysRunsAt"] = always_at
    if post: t["post_process_code"] = post
    if pre: t["pre_process_code"] = pre
    if mapping: t["response_mapping"] = mapping
    return t


def code_tool(name, code, params=None, desc="", model_called=False):
    """code_exec tool. Engine-run (pre_actions): code is a baked default. Model-called: params are top-level w/o default."""
    props = {"code": {"type": "string", "default": code}} if not model_called else {}
    if model_called: props["code"] = {"type": "string", "default": code}
    props.update(params or {})
    return {"type": "function", "function": {"name": name, "handler": "__function__:code_exec", "description": desc,
                                             "parameters": {"type": "object", "properties": props}}}


def passthrough(name, actions, nxt, mapping=None, transitions=None, node_type="api_request"):
    return {"name": name, "node_type": node_type, "passthrough": True, "passthrough_default_transition": nxt,
            "on_enter": {"type": "silent"}, "pre_actions": actions, "response_mapping": mapping or [],
            "transitions": (transitions or []) + [{"target_node": nxt, "description": "always", "condition": {"operator": "always"}}]}


def code_node(name, code, nxt, transitions=None):
    return passthrough(name, [code_tool("code_exec", code)], nxt, transitions=transitions, node_type="code")


def convo(name, task, tools=None, transitions=None, first=None):
    return {"name": name, "node_type": "conversation", "on_enter": {"type": "tts", "text": first} if first else {"type": "silent"},
            "task_message": task, "role_message": "", "tools": tools or [], "transitions": transitions or [], "pre_actions": []}


def end_node(name, msg="Goodbye."):
    return {"name": name, "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True, "transitions": [],
            "terminal": {"type": "function", "function": {"name": name + "_end", "handler": "__function__:end_call",
                         "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "custom"},
                                                                         "spoken_message": {"type": "string", "default": msg}}}}}}


def workflow(initial, nodes, tools=None, test_vars=None):
    return {"initial_node": initial, "nodes": nodes, "global_nodes": {}, "tools": tools or [], "system_prompt": "",
            "test_variables": test_vars or {"first_name": "Sofia", "last_name": "Almeida", "title": "Ms", "now": "2026-10-14T11:00"}}


def cond(var, val):
    return {"logic": "or", "conditions": [{"variable": var, "operator": "equals", "operand": val, "mode": "exact"}]}


def verify_actions(ident):
    return [tool("verify_%s" % f, "/v1/verify_identity", {"customer_id": "{{customer_id}}", "field": f, "answer": a})
            for f, a in ident.items()]


def deploy(name, wf, fixture):
    """Write the fixture, validate, save into the single `spike` slot."""
    FIX.mkdir(parents=True, exist_ok=True)
    (FIX / fixture).write_text(json.dumps(wf, indent=1))
    freya.validate_workflow("spike", wf)
    s = freya.save_workflow("spike", wf)
    print("deployed", fixture, s["workflow_id"], flush=True)
    return s


class Chat:
    def __init__(self, workflow_id, variables):
        self.wid, self.vars, self.call_id, self.log = workflow_id, variables, None, []

    def say(self, msg, retries=1):
        body = {"workflowId": self.wid, "message": msg, "customerVariables": self.vars}
        if self.call_id: body["callId"] = self.call_id
        t0 = time.time()
        for a in range(retries + 1):
            try:
                r = freya.api("POST", "/api/v2/chat/message", body); break
            except RuntimeError as e:
                print("   chat error:", str(e)[:300], f"after {time.time()-t0:.1f}s", flush=True)
                if a == retries: raise
        self.call_id = r.get("callId") or self.call_id
        dt = time.time() - t0
        print(f"> {msg!r}\n< {r.get('response')!r}  ({dt:.1f}s)", flush=True)
        self.log.append((msg, r.get("response"), round(dt, 1)))
        return r

    def end(self):
        try: freya.api("POST", "/api/v2/chat/end", {"callId": self.call_id})
        except RuntimeError as e: print("end error", str(e)[:200])


def events(call_id, kinds=None, retries=3):
    """System events of a call as dicts."""
    for _ in range(retries):
        call = freya.get_call(call_id)
        out = []
        for m in call.get("messages", []):
            if m.get("role") == "system":
                try: e = json.loads(m["message"])
                except Exception: e = {"event": "raw", "message": m.get("message")}
                if not kinds or e.get("event") in kinds: out.append(e)
        if out: return out
        time.sleep(3)
    return []


def show(call_id, kinds=("node_enter", "function_call_start", "function_call_complete", "error"), limit=900):
    for e in events(call_id, kinds):
        e = dict(e); e.pop("variable_table", None)
        r = e.get("result")
        if isinstance(r, dict): e["result"] = {k: v for k, v in r.items() if k != "header"}
        if "branches" in e: e["branches"] = [(b.get("description"), b.get("result")) for b in e["branches"]]
        print("  EVT", json.dumps(e)[:limit])


def vtable(call_id):
    """Latest route_evaluated variable_table (values only)."""
    last = None
    for e in events(call_id, ("route_evaluated",)): last = e
    vt = (last or {}).get("variable_table") or {}
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in vt.items()}
