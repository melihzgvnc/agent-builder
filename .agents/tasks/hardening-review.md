# Brief A hardening: identity gate hardening and offer-once guard

Part 1 adds a last-try warning to the identity verify hook. It reorders the compiler so a failed identity wins over a verified one. It gates the account, promise, note and transfer tools on `identity_state == 'failed'`. It also stops the repeated floor offer (PUB-06) through the product facts text and the amounts prompt. The evidence note records pytest at 615 (baseline 602), contamination exit 0, and a final-build stability run of 10/10 customers at 3 of 3. I judged from the report, the diff and the saved result file; I did not re-run any suite.
Watch for: (1) the PUB-03 root cause (a skipped `verify_identity` call) is addressed by injected text and a prompt rule, not a hard code gate; it is a probabilistic guard, 3/3 in the evidence (possible residual risk). (2) PUB-06 is a facts and prompt guard; no hook can see a spoken offer (confirmed, stated by the coder). (3) The 10/10 comes from one 3-rep run of the final build; the two earlier builds scored 9/10 with PUB-06 at 2/3 (confirmed).
**Verdict**: APPROVED

## High-level view

The code-side identity gate is real but narrow. It keys on an explicit `failed` state, which `verify_post_hook` sets once the spec-derived wrong-answer limit is reached. Compiler routing sends `failed` out of identity and negotiate, and the account lookup, promise, note and transfer pre-hooks blank their key field so the bank returns nothing. The case that actually failed before, a model that never submits the third answer, cannot be counted by any hook. The hook text and prompt rule address it.

The offer-once fix is derived from `offer_floor_times` in the spec. It adds a qualifier to repeat-floor attempt lines in the engine and a read-back rule in the amounts prompt. It is not a state flag, and the report says why.

The diff adds no brief wording, and `tools/` and the fixtures are untouched. Tests were added for each fix, and the final stability file shows 10/10 customers at 3 of 3.

<details>
<summary>Issues (3)</summary>

1. **Skipped-submission residual risk (possible)** — a model that skips the verify call is guarded only by text. Keep the contingency item 1.7 on record and watch PUB-03 in Part 2 regressions.
2. **Offer-once is not a code flag (confirmed, accepted)** — it depends on model compliance with the facts qualifier and prompt. The final PUB-06 evidence is 5/5 alone and 3/3 in the stability run; re-check on the layered agent.
3. **Single final-build stability run (likely)** — earlier builds sat at 9/10 with PUB-06 2/3. Treat 10/10 as a lower-confidence number and confirm in Part 2 task 2.5.

</details>

<details>
<summary>Details</summary>

## Identity gate: where it is enforced

`_H_GATE` returns true only for an explicit `identity_state == 'failed'`, so an unset state cannot lock out a customer. When true, `account_pre_hook` blanks the customer id field and the promise, note and transfer pre-hooks blank the product or queue field. Each sets a `*_guard='blocked'` variable. The account hook is attached in `compiler.py` through `tools_gen.attach_hooks`, and the report says `get_account` succeeded live. The compiler now puts the `failed` group ahead of the `verified` group in identity transitions. Negotiate gets a first `failed` group. Together these mean a failed caller cannot reach account data even if both flags were somehow set (confirmed from the diff).

The limit is derived from the spec. `T_LAST` fires when `MAXW - wrong == 1`, taking the minimum with the bank's `wl` when known. `MAXW` is a constant injected from `spec.identity.max_wrong_total`, not a literal. The text fires only when `counted` and the state is still `pending`.

Limitation: the original failure was a skipped tool call, so no hook sees the third answer. The last-try text and the prompt rule ("a message with an answer is submitted first") are the only mitigation. PUB-03 passed 3/3 in all three stability runs, which supports it but does not make it a hard gate (possible residual risk).

## Offer-once guard

`_floor_offered_once` reads the spec. For attempts k >= 2 whose amount equals the floor, the product facts line gets a repeat-offer qualifier. `too_much` was added to the embedded amounts subset in `ENGINE_SPEC_KEYS`, which the snippet needs to see the flag. The prompt gains a rule that a non-accepting, non-refusing answer is not grounds for repeating the amount or date. The rule is emitted only when the flag is 1. The first wording made the agent restate the offer; the coder replaced it and measured 5/5 on PUB-06.

## Contamination, scope and tests

The added strings are generic ("the customer", "the floor offer"). I found no dates, field names or phrases from the briefs in the diff. The evidence reports `check_contamination.py` exit 0. `git status` shows no changes under `tools/`, no changes to the fixtures, and no edited spec. The spec was copied to `results/` and `cmp`-verified. Tests were added for each fix: three hook tests, two compiler tests, one engine test and two prompt tests. Existing compiler tests were re-indexed for the new transition order. Pytest is 615, above the 602 floor.

## Stability evidence

`results/hardening-a-stability-r3.json` (final build, 3 reps) shows passed 10/10, with every customer at 3 of 3. No failure needs a judge-noise defence. The no-verdict cases (PUB-01 and PUB-10) were diagnosed in the plan as platform noise, since the call-level successEvaluation was true, and none occurred in this run.

</details>

<details>
<summary>File map</summary>

- builder/hooks.py: last-try text, `_H_GATE`, gated pre-hooks, new `account_pre_hook`
- builder/compiler.py: failed group ordering, negotiate failed group, account hook attach
- builder/engine.py: repeat-floor qualifier
- builder/snippets.py: `too_much` in the embedded amounts keys
- builder/node_prompts.py: submit-first rule, read-back rule
- tests/test_hooks.py, test_compiler.py, test_engine.py, test_node_prompts.py: new and re-indexed tests

Full diff: `git diff -- builder tests`

</details>
