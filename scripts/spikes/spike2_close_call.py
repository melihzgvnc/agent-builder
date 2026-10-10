"""Spike Q2: lifecycle close tool (alwaysRunsAt end_of_call) with a pre_process_code hook that rewrites the body from vars.
Run: spike2_close_call.py node|chatend   (node: call ends through an end_call node; chatend: /chat/end while in a conversation node)"""
from common import *

PRE = """sys_argv_flag = 'positive'
try:
    b = config.get('body')
    avail = [k for k in ('final_outcome', 'final_reason', 'customer_id', 'first_name', 'now', 'call.call_end_reason') if vars.get(k)]
    b['outcome'] = str(vars.get('final_outcome') or 'NOVARS')
    b['reason'] = str(vars.get('final_reason') or 'NOVARS') if len(sys_argv_flag) else 'seen:' + ','.join(avail) + '|n=' + str(len(vars))
except Exception as e:
    config['body']['reason'] = 'ERR ' + repr(e)[:80]
"""
SET = "vars['final_outcome'] = 'no_promise'\nvars['final_reason'] = 'refused'\n"


def build(ident):
    close = tool("close_call", "/v1/close_call", {"customer_id": "{{customer_id}}", "outcome": "promise", "reason": "{{final_reason}}"},
                 pre=PRE, always_at=["end_of_call"], desc="Record how the call ended.")
    nodes = {
        "hello": convo("hello", "Say hello briefly then wait.", first="Hi there.", transitions=[{"target_node": "setc", "description": "always", "condition": {"operator": "always"}}]),
        "setc": code_node("setc", SET, "wait", transitions=[{"target_node": "bye", "description": "mode node", "condition": cond("mode", "node")}]),
        "wait": convo("wait", "Say the word waiting and nothing else.", first="waiting"),
        "bye": end_node("bye", "Goodbye."),
    }
    return workflow("hello", nodes, tools=[close])


if __name__ == "__main__":
    mode = sys.argv[1]
    v, ident = session("PUB-11")
    s = deploy("spike", build(ident), "spike_2_close_call.workflow.json")
    c = Chat(s["workflow_id"], {**v, "mode": mode})
    c.say("Hello?")
    try: c.say("Go on", retries=0)
    except RuntimeError: print("TURN 2 FAILED (see chat error above)")
    if mode == "chatend": c.end()
    time.sleep(6)
    show(c.call_id, ("node_enter", "function_call_start", "function_call_complete", "error"), 700)
    led = ledger(v["customer_id"])
    print("LEDGER outcome/reason:", led.get("outcome"), led.get("reason")); print("LEDGER calls:", json.dumps(led["calls"])[:900])
    call = freya.get_call(c.call_id); print("endedReason", call.get("endedReason"), "status", call.get("status"))
