"""Spikes Q1 (vars across hooks) and Q4 (shape of mapped/unmapped list in code nodes)."""
from common import *

DUMP = """import json
out = []
try:
    for k in ('prods', 'p0_due', 'ok_v', 'whole', 'first_obj', 'acct_response.data.products', 'acct_response.data.ok', 'acct2_response.data.products', 'acct_response.status_code', 'acct_success_count'):
        v = vars.get(k)
        out.append(k + '=' + type(v).__name__ + ':' + repr(v)[:260])
    ks = sorted(k for k in vars if (k.startswith('acct') and 'header' not in k))
    out.append('NKEYS=' + str(len(ks)))
    for k in ks[:14]:
        out.append(k + '=' + type(vars[k]).__name__ + ':' + repr(vars[k])[:60])
except Exception as e:
    out.append('ERR ' + repr(e))
vars['%s'] = ' || '.join(out)
vars['y'] = 'fromcode'
"""
POST_A = "vars['x'] = 'seen'\ntry:\n    result['data']['injected'] = 'hello'\nexcept Exception:\n    pass\n"
PRE_B = "config['body']['text'] = 'x=' + str(vars.get('x')) + ';y=' + str(vars.get('y'))\nconfig['body']['product_id'] = 'p1'\nconfig['body']['reason'] = 'refused'"


def build(ident):
    acct = lambda n, post=None: tool(n, "/v1/get_account", {"customer_id": "{{customer_id}}"}, post=post)
    nodes = {
        "hello": convo("hello", "Say hello in one short sentence. Then wait.", first="Hi there.", transitions=[{"target_node": "v", "description": "always", "condition": {"operator": "always"}}]),
        "v": passthrough("v", verify_actions(ident), "acct"),
        "acct": passthrough("acct", [acct("get_account", POST_A)], "dump1", mapping=[{"path": "data.products", "variable": "prods"}, {"path": "data.products.0.past_due", "variable": "p0_due"}, {"path": "data.ok", "variable": "ok_v"}, {"path": "data", "variable": "whole"}, {"path": "data.products.0", "variable": "first_obj"}]),
        "dump1": code_node("dump1", DUMP % "dump1", "noteb"),
        "noteb": passthrough("noteb", [tool("add_note", "/v1/add_note", {"customer_id": "{{customer_id}}", "product_id": "p9", "reason": "refused", "text": "orig"}, pre=PRE_B)], "acct2"),
        "acct2": passthrough("acct2", [acct("get_account_b")], "dump2"),
        "dump2": code_node("dump2", DUMP % "dump2", "talk"),
        "talk": convo("talk", "Say the single word ready and nothing else.", first="ready"),
    }
    return workflow("hello", nodes)


if __name__ == "__main__":
    v, ident = session("PUB-11")
    s = deploy("spike", build(ident), "spike_1_hooks_data.workflow.json")
    c = Chat(s["workflow_id"], v)
    c.say("Hello?")
    c.say("Ok go on")
    c.end()
    show(c.call_id, ("node_enter", "function_call_start", "function_call_complete", "error"), 1200)
    vt = vtable(c.call_id)
    for k in ("dump1", "x", "y", "prods", "p0_due", "ok_v", "whole", "first_obj"): print("VT", k, "=", json.dumps(vt.get(k))[:1800])
    print([k for k in vt][:80])
    (ROOT / "results").mkdir(exist_ok=True)
    json.dump({"call_id": c.call_id, "vt": vt}, open(ROOT / "results" / "spike1.json", "w"), indent=1)
