# Task 8 code report (Part 2, iteration 1)

## Waivers
Waiver (verbatim from the orchestrator, relaying the user's written decision): "The Brief A-on-layered target is WAIVED for PUB-05, PUB-08 and PUB-10." Reasons given: Brief B rule 8 literally applies to one-product no-promise accounts (so the layered agent is correct), and the Brief A rule 1 recording notice is mandated by the brief (so PUB-10 on brief-ab is a brief-internal conflict with the judge). Brief A is scored on the brief-a agent (10/10 at 3/3).
Confirmed by the user, decision date 2026-10-11.
Cases waived (all 0/3 on brief-ab, agent 32204907-48f5-402d-988b-1f224460ea5d):
- PUB-05: results/task8-a-on-layered-stability.json (unexpected hardship note, rule 8)
- PUB-08: results/task8-a-on-layered-stability.json (unexpected hardship note, rule 8)
- PUB-10: results/task8-a-on-layered-stability.json (judge reads the mandated opening recording notice as a premature claim)
Brief A-only evidence (slot brief-a): results/hardening-a-stability*.json.
No code changes were made in this iteration; nothing committed (orchestrator commits Part 1 and Part 2 separately).
brief-ab agent id: 32204907-48f5-402d-988b-1f224460ea5d (slot brief-ab, workflow aae6f3cf-2ef8-43ca-8ba0-bc930659265b). Model: anthropic/claude-haiku-5.5. Nothing committed.
Pinned spec: results/brief-ab.spec.json (first extraction, never hand-edited). Builds after the first one use `--spec results/brief-ab.spec.json --slot brief-ab`.

## Spec check (results/brief-ab.spec.json)
- closing.aggregation: PASS (precedence transfer, any_promise, all_notes_equal, first_note_not_equal; outcomes set). `default` missing: compile warning only, fallback is persuasion.on_exhausted (no_promise/refused). Same as the Brief A spec.
- multi_product: PASS (enabled, max 3, one_date_for_all cap_each_to_own_deadline, combined_readback, say_capped_in_readback, per_product_attempts, notes.*, on_transfer.*, on_promise_failure.note_reason).
- account_fields.products_key = "products": PASS. bands with deadlines per band: PASS.
- Compiled workflow: p<k>_* names are built at runtime in the hooks (`'p'+str(k)+'_'+name`), closing_ready and add_note present: PASS.

## Failure found and fix
PUB-11 failed 2 of 3 on the first build: the model called create_promise for three products in one turn; parallel post-hooks overwrite each other's state, closing_ready never became 'yes', finalize routed back to negotiate and the agent looped on goodbye until the customer hung up (bank records were right, conversation check failed).
Fix (generic, builder/node_prompts.py `_multi_lines`): when the spec is multi-product and a promise tool exists, the negotiate prompt says to call it for one product at a time and wait for each result. Test: tests/test_node_prompts.py::test_negotiate_task_multi_asks_for_sequential_tool_calls.
A first wording that also covered the note tool made PUB-12 fail 2 of 3 (results/task8-b-stability-r2.json, wrong number of persuasion attempts); narrowed to the promise tool only.

## Verification
- `uv run pytest -q`: 616 passed. `uv run python scripts/check_contamination.py`: exit 0.
- Brief B, layered agent: results/task8-b-stability.json 1/3 PUB-11 (before fix), results/task8-b-stability-r2.json (PUB-12 1/3, rejected wording), results/task8-b-stability-r3.json (final build): PUB-11 3/3, PUB-12 3/3, PUB-13 3/3 = 3 of 3 customers.
- Brief A on layered agent: results/task8-a-on-layered-stability.json: 7 of 10 customers 3/3. Fail 0/3: PUB-05, PUB-08, PUB-10.

## Decisions (from the parent session) and outcome
1. PUB-05 and PUB-08 on the layered agent: expected differences, not bugs. Brief B rule 8 gives every product without a promise an add_note, including a single-product account with no promise. No product-count special case added. Brief A scoring uses the Brief A-only agent (slot brief-a, 10/10 stable in Part 1).
2. PUB-10 on the layered agent: judge-sensitive. I read the failing transcript (call in results/task8-a-on-layered-stability.json, all_rows[0], PUB-10). The notice is the first sentence ("This call is recorded."), said at the point Brief A rule 1 specifies, and it is about the call, not about a promise. Everything else in the call is correct: silent retry after the first create_promise failure, one promise recorded, "recorded" about the promise said only after ok:true. The judge counted the word in the opening. No generic wording change is allowed by the brief (rule 1 requires the notice in the first sentence), so nothing was changed.

## Final verification (final build, unchanged since results/task8-b-stability-r3.json)
- `uv run pytest -q`: 616 passed. `uv run python scripts/check_contamination.py`: exit 0.
- Brief B (results/task8-b-stability-r3.json): PUB-11 3/3, PUB-12 3/3, PUB-13 3/3.
- Brief A on layered (results/task8-a-on-layered-stability.json): 7 of 10 at 3/3; PUB-05, PUB-08 (expected notes) and PUB-10 (judge-sensitive) at 0/3.
- runs/ folders deleted; spec copy in results/brief-ab.spec.json. Nothing committed.

## Iteration 2 (review findings) - evidence, no code change
No code or build changed in this iteration; results files are those listed above (brief-ab agent 32204907-48f5-402d-988b-1f224460ea5d). pytest 616 passed, contamination exit 0 (unchanged since the last run).
- PUB-05 / PUB-08 (0/3, all 3 reps, results/task8-a-on-layered-stability.json all_rows[0..2]; calls cde40eb0-f408-4167-8cce-68934bfa9bd0, 290fdaa6-f291-4a2c-9169-85aaf32377e4, bbadf8aa..., d7c58583..., 9e9a46a5..., 0647cd01...). Scorer failure strings: `notes [('p1', 'hardship')], expected []` and `notes [('p1', 'paid')], expected []`. Tools used: verify x2, get_account, add_note, close_call. The agent follows briefs/brief-b.md rule 8 literally ("For each product without a promise call add_note ... This also applies when no product gets a promise"). The Brief A expected ledger has no note. The two briefs conflict for a one-product account; the agent is doing what Brief B says.
- PUB-10 (0/3, calls 699307ee-bfbc-488e-a407-57b47cc19337, e570ca90-d737-4dcc-9e7c-3c3abba11e3e, e23c5b71-0cf0-4693-8985-e972e654077e). All three judge reasons cite the same thing: the opening "This call is recorded" (Brief A rule 1 requires the call-recorded notice) counted as saying "recorded" before create_promise returned ok:true. Ledger part passed (one promise after one silent retry). The Brief A-only agent passes PUB-10 3/3 (results/hardening-a-stability-r3.json) because its opening (call 986a0695-c61b-4973-8d14-20fd4a640657) omits the notice entirely. The layered agent keeps it. Not judge noise: deterministic over 3 reps.
- Finding 4 (commit Part 1 separately): not done, this step is forbidden from committing; the caller must commit Part 1 files first.

## Decisions from the parent session (iteration 2) - final
- PUB-05 / PUB-08 on brief-ab: expected, documented differences. Brief B rule 8 explicitly covers the no-promise case, so notes on one-product accounts are correct. No single-product special case. Brief A is scored on the brief-a agent (10/10 stable).
- PUB-10 on brief-ab: judge-sensitive brief-internal conflict (Brief A rule 1 notice vs the judge's "recorded" criterion), not a bug. The notice stays in the layered opening.
- Extraction fidelity gap for the parent to decide separately: the brief-a (A-only) agent's opening omits the Brief A rule 1 "call is recorded" notice (call 986a0695-c61b-4973-8d14-20fd4a640657).
- No code changed this iteration. Final check: `uv run pytest -q` 616 passed; `uv run python scripts/check_contamination.py` exit 0. Part 1 files to be committed separately by the parent.
## Iteration 3: final user/parent decisions (no code change)
- PUB-05/PUB-08 on brief-ab: Brief B rule 8 kept literally; Brief A scored on the brief-a agent (10/10 stable).
- PUB-10 on brief-ab: notice kept; documented judge-sensitive conflict.
- Brief A-on-layered target WAIVED. No code or build changes. Orchestrator commits (Part 1 separately).
- Evidence unchanged: pytest 616 passed; check_contamination.py exit 0; Brief B 3/3 (results/task8-b-stability-r3.json); Brief A on layered 7/10 (results/task8-a-on-layered-stability.json); brief-ab agent 32204907-48f5-402d-988b-1f224460ea5d.
