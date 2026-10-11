# Platform notes (Phase 2a spikes)

Generic facts about the workflow platform, found by driving tiny workflows through the chat route. Names are neutral: `tool_a`, `tool_b`, `field_x`, "the lifecycle close tool". Status: confirmed / refuted / partial / untested.

## Q1. Hooks share `vars` within one call: CONFIRMED
- A `post_process_code` of `tool_a` wrote `vars['x'] = 'seen'`. A code node wrote `vars['y'] = 'fromcode'`. The `pre_process_code` of a later `tool_b` (in a later node) built `config['body']['text'] = 'x=' + vars.get('x') + ';y=' + vars.get('y')`.
- The `function_call_start` args of `tool_b` showed the body text `x=seen;y=fromcode`. So pre-hooks read variables written by earlier post-hooks and by code nodes.
- A pre-hook can also overwrite other body fields (for example an id or an enum value). The overwritten value is what was sent.
- A post-hook can add keys to `result['data']` (seen: `injected`). They appear in the `function_call_complete` result and in the flattened `<node>_response.data.<key>` variables, so the flattened variables do follow post-hook changes to `result`.
- Hook-written variables show up in the `route_evaluated` `variable_table`.

## Q2. Lifecycle close tool (`alwaysRunsAt: ["end_of_call"]`): CONFIRMED
- Its `pre_process_code` can rewrite `config['body']` from `vars`. Test: body default outcome was a literal that the bank would reject as wrong; the hook set `outcome` and `reason` from `vars['final_outcome']` / `vars['final_reason']` (written earlier by a code node). The bank ledger then held exactly those values (ground truth, read from the ledger route after the call).
- Ran when the call ended by `/chat/end` in a conversation node: yes.
- Ran when the call ended through an `end_call` node: yes (same ledger evidence; `endedReason` was the assistant-ended value).
- Variables the hook could read (probe: hook listed which of a fixed key list were truthy, and counted keys): every workflow variable. Seen present: `final_outcome`, `final_reason` (code-node variables), `customer_id`, `first_name`, `now` (call variables). The key count was 68 to 69. Not present in `vars`: `call.call_end_reason` (that template exists only in `{{...}}` defaults, not as a key in `vars`). Keys from the `call.*` family that exist at runtime are `call.call_id`, `call.direction`, `call.phone_number`, `call.customer_phone_number`.
- A baked default `{{var}}` in the close tool resolves from variables set by code nodes (the reason default worked).
- What happens when a hook raises was not tested. Wrap hooks in try/except.
- Design consequence: compute the final outcome and reason in code nodes or hooks (`vars['final_outcome']`, `vars['final_reason']`) and let the close tool's pre-hook copy them into the body. The model need not fill them.
- Fallback if this ever fails: close outcome as an enum the model fills from a code-computed reason list written in `task_message`.

## Q3. `code_exec` as a model-called tool: UNTESTED (still; the compiler does not use it)
- Chat service was down (503/504) when this was due. Driver is ready: `scripts/spikes/spike3_model_tools.py`.
- Fallback design: do not use `code_exec` as a model tool. Use post-hooks / pre-hooks on `api_call` tools and `code` nodes only.

## Q4. Shape of a list from an `api_request` node in a code node: CONFIRMED (flattened keys)
- A node-level `response_mapping` with path `data.<list>` leaves the mapped variable UNSET (None in `vars`, absent from the variable table). A mapping to an object path (`data`, `data.<list>.0`) is also unset.
- Mapping to a scalar leaf works: `data.<list>.0.<field>` gave an int; `data.ok` gave the string `'true'`.
- With or without a mapping, the engine writes flattened keys into `vars`, named after the NODE for `pre_actions`: `<node>_response.data.<list>.<i>.<field>` (e.g. `acct_response.data.list.0.field_x`). For a model-called tool the prefix is the tool name.
- Types seen in `vars` inside a code node: numbers are Python `int` (`220` stayed int), strings are `str`, booleans are the STRING `'true'` (`data.ok`), `status_code` is `int`, `<node>_success_count` is `int`. The list itself is never a Python list or a JSON string.
- Code must rebuild the list: loop `i = 0, 1, ...` while `vars.get(prefix + '.%d.id' % i)` is not None. Keep a per-field `int()` / `float()` guard. Only fields present in the response get keys (optional fields are simply absent).
- Also in `vars` after a node: `<node>_response.status`, `.message`, `.status_code`, `.header.<Name>`, `<node>_status_code`, `<node>_success_count`, `<node>.current_turns`, `<node>.total_turns`.

## Q5. Tool-level `response_mapping` / counters -> transition: CONFIRMED (first practice run)
- Not reached before the chat outage. Driver ready (`spike3_model_tools.py`, modes a and b).
- Fallback / design: the closing path is "close tool result -> variable -> transition to an `end_call` node". Check it first in the first practice run. Test both the mapped variable and the flattened `<tool>_response.data.<key>` in an OR group, and also `<tool>_success_count` greater than 0.
- Keep a code-node route as a backup: have the pre-hook or a code node set a string flag and route on that.

## Q6. Post-hook `what_to_do_next`, pre-hook override of model fields: CONFIRMED (first practice run)
- Not reached before the outage. (Q1 already shows a pre-hook can overwrite request body fields; only the model-called-tool case is untested.)
- Design: force ids from code in the pre-hook (`config['body']['id_field'] = vars[...]`) and add `result['data']['what_to_do_next']` in the post-hook. Check in the first practice run.

## Chat route and runtime facts the compiler must know
- The chat route SKIPS the pre-actions of an initial `api_request` or `code` node (no events at all; its variables stay unset). Start every workflow on a `conversation` node with an `always` transition into the first engine-run node. The chain then runs after the first user message.
- An `always` transition out of the initial conversation node only fires on the NEXT user message (the first routing is suppressed: "Missing prior assistant message").
- A passthrough chain (several `api_request` / `code` nodes, each with an `always` transition) runs in a single chat turn, about 1 to 4 seconds for a 7-node chain.
- The chat turn in which the call ends through an `end_call` node returns HTTP 504 after about 12 to 18 s, although the engine did hang up and the end-of-call tool ran.
- Chat turns normally take about 1.5 to 5 s. During model trouble they take 60 s and then 504, or fail at once with 503. This happened to every workflow, including the base example, and a new API key in the same workspace did not help.
- Drivers that crashed after a 504 left chat calls open. Whether open calls affect the outage was not determined. Call `/api/v2/chat/end` in a `finally`.
- Flattened result keys for `pre_actions` nodes use the NODE name (`<node>_response.*`), not the tool name.
- `function_call_start` args for `api_call` show the full final request. This is the ground truth for what a pre-hook produced.
- The workspace list route returned 3 workflows after the spikes.

## First practice run (10 simulated calls, built agent; evidence from transcripts and the bank ledger)
Status per item: confirmed / refuted / partial.

- Q5 transition after a tool result: CONFIRMED. After the last identity tool result the route fired in the same turn (0.01 s later). All three alternatives were true at once: the hook-written variable (`identity_state == 'verified'`), the flattened `<tool>_response.data.<key> == 'true'`, and the mapped variable. Counters (`<tool>_failure_count`, `<tool>_success_count`) are evaluated; a booleans-as-string compare needs `'true'`.
- Q6 post-hook `what_to_do_next`: CONFIRMED. The post-hook text for the recorded case reached the model and it said the mandated sentence word for word, then called the `transition_to_<node>` tool in the same message.
- Q6 pre-hook override of model-called fields: CONFIRMED. A forced product id and a clamped date/amount were what the bank received; an invalid callback date/time was blanked (`""`) and the bank rejected it. The blanked request still shows in the ledger call log (args `date ""`, `time ""`, `ok false`), so a scorer that counts failed calls sees it.
- Hook order (pre-hook reads variables from earlier post-hooks and code nodes): CONFIRMED. The finalize code node read the per-product state written by the post-hook of the model-called tool (`closing_ready`, `p<k>_promise`, `transfer_reason` all present in the variable table before it ran).
- Product list reconstruction: CONFIRMED. Flattened `<node>_response.data.products.<i>.<field>` keys appeared with the NODE name as prefix; the facts node rebuilt the list and `products_state` became `known`.
- First node is a conversation node: CONFIRMED. The run starts with `node_enter` of the initial conversation node and the opening line, then the chain runs. In the chat route the first user message produced the first assistant question; no pre-action was lost.
- Closing path writes the ledger (Q2 on a real build): CONFIRMED for the transport, with one finding. The lifecycle close tool ran after `end_call` in every call, 0.5 to 1 s after `call_ended`, and the ledger held exactly the values in its body (a no-callback exit gave the no-callback pair; a hardship exit gave the hardship pair). Finding: when the extracted spec has no `closing.aggregation`, the finalize code node falls to the fixed fallback pair (verified: the no-promise/refused pair) even when a promise or a transfer exists (`AGG_SPEC` is `{'closing': {'aggregation': {}}}`). 6 of 8 failing calls lost on this alone. The platform was not at fault. The extraction prompt now asks for `aggregation` also for one product.
- end_call / turn endings: PARTIAL. In simulation no turn hung (slowest turn 5 s). In the chat route the turn that ends the call returned a fast 502 (3 to 5 s) instead of a 504 hang, in 2 of 3 attempts, and one 502 appeared on a normal turn. After a 502 the engine replayed the last user turn: the model-called tool ran again (a second create-promise call came back as a failure), and the close tool fired twice with different pairs. Treat 502/504 on the chat route as unsafe to retry; do not resend the turn.
- Lifecycle close tool in the chat route: the close request is written to the ledger up to 15 s or more after the call ends (a read 8 s after the end showed no outcome; a later read showed it). Wait 30 s before reading the ledger after a chat call.
- Smoke test pollution (placeholder customer id): CONFIRMED harmless. The bank answers "unknown customer_id for this team key" and keeps no record.
- Hooks without imports: CONFIRMED. `float()`, `int()`, string slicing, `str.split` and nested functions ran in hooks and code nodes; no hook fell back to the unchanged request.
- `end_call` node with `message_mode: none` and terminal function names: PARTIAL. `endedReason` was the assistant-ended value on every end. The spoken-message variant was not seen in these runs.
- Gated exit / gated finalize routing back to the source node: UNTESTED (no call hit a gate that blocked).

### Findings that are bugs in the built agent (not platform behaviour)
- Callback before verification: the calendar variables (`callback_dates`) are written by the facts node, which runs only after the account lookup. A third-party caller never reaches it, so the callback pre-hook saw an empty list, blanked every date/time, and the bank rejected both tries ("Allowed dates: ." in the model's text). Needs the calendar computed before the first conversation node can offer a callback (builder change, not yet made).
- `transition_to_*` tool names and a model that says "verified" after one passed question: model behaviour, not platform.

## Online test stage and live variant runs (facts seen in saved traces and results)
- The bank mock is closed-world: practice sessions accept only the case ids of the cases file; custom accounts cannot be created. Generated cases therefore borrow carrier accounts from the cases file (`--cases` or `<root>/public-test-set.json`) and only the customer script and expected result are generated. The stage skips with a DEGRADED line when no carriers, no scoring tools or no bank key exist.
- The mock applies its own rules to its accounts. A spec number that differs from the mock's shows up as class `expectation` and is excluded from the score.
- The chat route and `GET /simulations` returned 504 and 503 several times during the variant runs (smoke probe timeouts at 30 s, a 503 "API key validation temporarily unavailable", a PUT 504 on a rebuild). Nothing on these routes is retried; the stage marks the case `infra` and stops testing. Waiting a few minutes and rebuilding from the saved spec (`--spec`) worked.
- `freya.run_simulation` has no timeout. The stage runs it in a daemon thread; an abandoned simulation can keep the workspace busy for the next run.
- The platform default text substitution rewrites "$" to "dollars" with no space in chat transcripts ("dollars420.00"). It is not emitted by the compiler.
- Ledger call argument key names (`args/request/input/body/params`) are read tolerantly; the real shape was not separately confirmed in these notes.
- Model variance on single cases is real: one variant case failed once and passed on the same compiled workflow in the next run. One run is a hint, not proof.
