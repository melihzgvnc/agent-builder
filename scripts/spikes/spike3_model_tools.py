"""Spikes Q3 (code_exec as model tool), Q5 (tool response_mapping/counters -> transitions), Q6 (post-hook what_to_do_next, pre-hook override).
Run: spike3_model_tools.py a|b   (a: calc, account, note -> note_ok transition; b: calc, then failing tool -> failure_count transition)"""
from common import *

CALC = """out = []
try:
    out.append('n=' + repr(vars.get('n')))
    out.append('calc_args.n=' + repr(vars.get('calc_args.n')))
    out.append('keys=' + ','.join(sorted(k for k in vars if 'calc' in k or k in ('n', 'args'))))
    try:
        out.append('args=' + repr(args))
    except NameError:
        out.append('args=NameError')
    n = vars.get('n') or vars.get('calc_args.n')
    vars['calc_out'] = str(int(float(n)) * 2)
except Exception as e:
    out.append('ERR ' + repr(e))
vars['calc_seen'] = ' | '.join(out)
"""
NOTE_PRE = "try:\n    config['body']['product_id'] = str(vars.get('forced_pid'))\nexcept Exception as e:\n    vars['pre_err'] = repr(e)"
ACC_POST = "try:\n    result['data']['what_to_do_next'] = 'Say exactly the single word PRODUCTS_OK and nothing else.'\n    result['data'].pop('products', None)\n    result.pop('header', None)\nexcept Exception:\n    pass\n"
NOTE_POST = "try:\n    result.pop('header', None)\nexcept Exception:\n    pass\n"
GO = lambda t: [{"target_node": t, "description": "always", "condition": {"operator": "always"}}]


def build(ident):
    calc = code_tool("calc", CALC, {"n": {"type": "number", "description": "the number to double"}}, desc="CALL it when the caller asks to double a number. Pass n.", model_called=True)
    calc["function"]["parameters"]["properties"]["code"]["default"] = CALC
    acc = tool("get_account", "/v1/get_account", {"customer_id": "{{customer_id}}"}, post=ACC_POST, desc="CALL it when the caller asks for the account.")
    note = tool("add_note", "/v1/add_note", {"customer_id": "{{customer_id}}", "product_id": (None, {"description": "product id"}), "reason": ("{{reason}}", {"enum": ["refused", "hardship"], "description": "reason"})},
                pre=NOTE_PRE, post=NOTE_POST, mapping=[{"path": "data.ok", "variable": "note_ok"}], desc="CALL it when the caller asks to add a note. product_id: use p1.")
    nope = tool("nope", "/v1/does_not_exist", {"customer_id": "{{customer_id}}"}, desc="CALL it when the caller asks to run nope.")
    tr = [{"target_node": "failed_path", "description": "nope failed", "condition": {"logic": "or", "conditions": [{"variable": "nope_failure_count", "operator": "greater_than", "operand": 0}]}},
          {"target_node": "bye", "description": "note ok", "condition": cond("note_ok", "true")},
          {"target_node": "bye2", "description": "note success_count", "condition": {"logic": "or", "conditions": [{"variable": "add_note_success_count", "operator": "greater_than", "operand": 0}]}}]
    nodes = {
        "hello": convo("hello", "Say hello briefly then wait.", first="Hi there.", transitions=GO("v")),
        "v": passthrough("v", verify_actions(ident), "pick"),
        "pick": code_node("pick", "vars['forced_pid'] = 'p2'\n", "work"),
        "work": convo("work", "You are a test harness. Do exactly what the user asks. Tools: calc, get_account, add_note, nope. When a tool is asked for, CALL it. After a tool result, reply with one short sentence.", tools=[calc, acc, note, nope], transitions=tr, first="Ready."),
        "failed_path": convo("failed_path", "Say the single word FAILEDPATH and nothing else.", first="FAILEDPATH"),
        "bye": end_node("bye", "Goodbye A."), "bye2": end_node("bye2", "Goodbye B."),
    }
    return workflow("hello", nodes)


if __name__ == "__main__":
    mode = sys.argv[1]
    v, ident = session("PUB-11")
    s = deploy("spike", build(ident), "spike_3_model_tools.workflow.json")
    c = Chat(s["workflow_id"], v)
    msgs = ["Hello", "Start", "Please call calc with n equal to 21.", "Now tell me what calc_seen is: just call get_account please.", "Add a note with reason refused for product p1."] if mode == "a" \
        else ["Hello", "Start", "Please call calc with n equal to 7.", "Please run nope now."]
    for m in msgs:
        try: c.say(m, retries=0)
        except RuntimeError: print("TURN FAILED"); break
    time.sleep(4)
    show(c.call_id, ("node_enter", "function_call_start", "function_call_complete", "error", "route_evaluated"), 1300)
    vt = vtable(c.call_id)
    for k in ("calc_seen", "calc_out", "note_ok", "pre_err", "add_note_success_count", "nope_failure_count", "nope_success_count", "add_note_response.data.ok", "get_account_response.data.what_to_do_next"):
        print("VT", k, "=", json.dumps(vt.get(k))[:900])
    print("LEDGER", json.dumps(ledger(v["customer_id"]).get("notes")))
