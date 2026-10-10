# Task 6 report: iterate Brief A on the live simulation

Agent: `brief-a` slot, agent `11b98cde-c639-48ab-a4b0-1b4f0a46d2ff`, version 3. The `base` agent was never touched. Nothing is committed.
All scores below come from saved `tools/run_cases.py` output under `results/`.

## Score history (Brief A, 10 cases, single run each)

| Step | File | Passed | Simulation |
|---|---|---|---|
| Baseline | `results/task6-baseline.json` | 2 / 10 (PUB-05, PUB-08) | 71517fdb-95eb-4c1b-a60a-17709406b4b1 |
| Iteration 1, after re-extraction only | `results/task6-iter1.json` | 5 / 10 (PUB-01, 04, 05, 08, 09) | f45e7ca5-33bb-4a3b-a697-df75466fa1fb |
| Iteration 1b, after prompt/hook/compiler fixes (final) | `results/task6-iter1b.json` | 10 / 10 | bb8514a1-de3c-440b-804c-7cf675bab633 |
| Stability, `--reps 3` | `results/task6-stability.json` | run 1: 10/10, run 2: 7/10, run 3: 9/10 | 576de6d4-5341-480e-9aa4-5787bc2293f4 |

## Stability result (`--reps 3`)

A customer counts only if all 3 runs pass. Result: 6 / 10 customers passed 3 of 3 (694 s).

- 3/3: PUB-02, 04, 05, 07, 08, 09
- 2/3: PUB-01, 03, 06, 10

The single 10/10 was therefore a lucky draw on a flaky set of four cases, not a stable result. The failures in the three runs:

- PUB-01 (run 2): `conversation: no verdict`. The tool path was correct (verify, get_account, create_promise, close_call). The judge returned no verdict. Judge noise, not an agent fault.
- PUB-10 (run 2): `conversation: no verdict`. The tool path was correct, including the retried create_promise. Judge noise.
- PUB-06 (run 2): the floor amount (637.50) was offered a second time in Message 10, with a different date and without the "rest stays overdue" note. Prompt weakness: the offer-once wording from iteration 1 does not hold every time. This is a real agent failure.
- PUB-03 (run 3): real failure. After four `verify_identity` calls the agent went on to `get_account` and `create_promise` (outcome `promise`, expected `not_verified/failed`). It also disclosed the product and the debt amount (Message 12) and did not send the customer to the bank app or a branch. A wrong-answer persona got through identity verification. Prompt weakness, or the verification gate letting the model proceed after repeated wrong answers. Not yet diagnosed to a single cause.

## Failures and root causes (baseline to iteration 1b)

| Case | Cause | Class |
|---|---|---|
| PUB-01, 04, 06, 07, 09, 10 | `close_call` sent the fallback `no_promise/refused` pair. The extracted spec had no `closing.aggregation`. | Extraction weakness (PUB-01, 04, 09 passed after re-extraction alone) |
| PUB-02 third-party callback | Calendar variables were written only after the account lookup. A third-party caller never reaches that node, so the callback pre-hook blanked every date. The identity prompt also pointed to "date facts" that do not exist in that node. | Compiler/hook bug |
| PUB-03 identity | The post-hook text after a wrong answer said "verified" after one wrong answer, then the model looped to the turn limit. | Prompt |
| PUB-06 floor amount | The floor amount was offered twice. | Prompt |
| PUB-07 named holiday | The model offered the latest promise date instead of the next business day. | Prompt |
| PUB-10 tool failure | After the HTTP error the post-hook did not run, so the model gave up without retrying and looped in `negotiate` until the `finalize` gate let the call end with the fallback. | Prompt |

## Fixes and their tests

- Extraction: `builder/extract_prompts/sections.md` now asks for `closing.aggregation` also for one product. The agent was re-extracted once (spec reused for the second build with `--spec`). No dedicated unit test; verified by the live runs.
- Callback calendar in the hook (`builder/hooks.py`, config passed from `builder/compiler.py`: `now_var`, `weekend`, `holidays`, `days_n`): the pre-hook derives today, time and allowed dates itself when the facts node has not run, with integer arithmetic and no imports. Test: `test_callback_pre_computes_calendar_when_facts_not_run` in `tests/test_hooks.py`, checked against `engine.allowed_callback_dates` for several dates.
- Identity prompt clock (`builder/node_prompts.py`): the callback section gives the date/time placeholder and the business-day rules. Test: `test_identity_task_gives_the_clock_when_a_callback_can_be_booked`.
- Retry after a failed promise call, and the non-business-day rule (named holiday or weekend day gets the next business day; vague-period rule marked as not covering named days): `builder/node_prompts.py`. Test: `test_negotiate_task_retries_a_failed_promise_call_and_holds_dates_rules`.
- Floor offer made once, in one message with its date: `builder/node_prompts.py`. No dedicated test (review finding, low risk). The stability run shows it is not reliable.
- Verification hook texts say the customer is not verified yet (`builder/hooks.py`). No dedicated test; the existing hook tests pass. The PUB-03 stability failure shows this is not enough.

## Final green check (Task 5)

- `uv run pytest -q`: 602 passed.
- `uv run python scripts/check_contamination.py`: exit code 0.
- No Brief A or B values were added under `builder/` (review confirmed on the diff).

## Platform facts (from the `builder/PLATFORM_NOTES.md` update)

Confirmed in the first practice run:
- Q5 transition after a tool result: confirmed. The route fired in the same turn; the hook-written variable, the flattened `<tool>_response.data.<key> == 'true'` and the mapped variable all worked. Counters work.
- Q6 post-hook `what_to_do_next`: confirmed. The model said the mandated sentence and called the transition tool.
- Q6 pre-hook override of model-called fields: confirmed. Forced ids and clamped values reached the bank. An invalid callback date was blanked and the bank rejected it.
- Hook order (pre-hook reads variables from earlier post-hooks and code nodes): confirmed.
- Product list reconstruction from flattened `<node>_response.data.products.<i>.<field>` keys: confirmed.
- A conversation node as the first node: confirmed.
- Closing path writes the ledger: confirmed. The close tool runs after `end_call` and the ledger held the body values.
- Hooks run without imports (`float`, `int`, slicing, `split`, nested functions): confirmed.
- Smoke test with a placeholder customer id is harmless: confirmed.

Partial:
- `end_call` and turn endings: in simulation no turn hung (slowest 5 s). In the chat route the final turn can return a fast 502 instead of hanging. After a 502 the engine replays the last user turn, so the promise tool ran twice and the close tool fired twice with different pairs. Do not retry 502/504 on the chat route.
- `end_call` with `message_mode: none`: the assistant-ended reason was seen on every end. The spoken-message variant was not seen.
- Ledger lag in the chat route: the close request can land 15 s or more after the call ends. Wait 30 s before reading.

Still untested:
- Q3 `code_exec` as a model-called tool (the compiler does not use it).
- A gated exit or gated finalize routing back to its source node (no call hit a blocking gate in the runs; in the stability run PUB-10 earlier looped until the gate released).

Refuted: the earlier assumption that the calendar could be computed by the facts node alone (a third-party caller never reaches it).

## What remains unsolved

- Stability is 6 / 10, not 10 / 10. PUB-03 (identity), PUB-06 (floor offered twice) and the judge `no verdict` outcomes on PUB-01 and PUB-10 are intermittent.
- PUB-03: the agent can proceed to the account lookup and a promise after repeated wrong identity answers, and then leaks product and amount. Needs a transcript read of the failing run (call id in `results/task6-stability.json`, `all_rows[2]`) and probably a hard gate in code rather than prompt wording.
- PUB-06: the offer-once rule needs a code-side guard (a state flag set when the floor offer is made) rather than prompt text alone.
- `no verdict` is a judge failure. A rerun may clear it, but it is counted as a fail. `--rerun-if-slow` is not the same thing and was not used.
- Two prompt changes have no dedicated unit test.
- Work is uncommitted.

## What Task 8 needs (Brief B, multi-product, PUB-11 to PUB-13 with `--brief B`)

- Cases: PUB-11 (three products, one date for all), PUB-12 (promises one product, refuses two), PUB-13 (one date for both, but the deadlines differ).
- A new agent slot for Brief B. Extract Brief B, build it, and keep `base` and `brief-a` untouched. Brief B has not been extracted or run in this task.
- The multi-product aggregation in `closing.aggregation` matters most here. The single-product case already showed that a missing aggregation silently falls back to `no_promise/refused`. Check that the spec carries a per-product promise state and a transfer/aggregation rule for several products.
- Check the per-product state (`p<k>_promise`, `closing_ready`) and the clamping of one shared date against each product's own deadline (PUB-13).
- Run `uv run python tools/run_cases.py <brief-b agent id> --brief B --reps 3` and score by customers passing 3 of 3. Do not trust a single 3/3-looking run.
- Expect the same flaky classes as Brief A: offer-once, identity gate, judge `no verdict`. Fix the identity gate and the offer-once guard generically first, since Brief B will inherit them.
- Run one simulation at a time (about 145 s for 10 cases; 3 cases are quick).
