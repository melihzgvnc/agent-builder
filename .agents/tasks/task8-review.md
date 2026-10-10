# Layered Brief A+B agent (slot brief-ab): sequential promise calls for multi-product accounts

Task 8 builds the layered agent from `briefs/brief-a.md` + `briefs/brief-b.md` into slot `brief-ab`. It needed one generic fix: the multi-product negotiate prompt now tells the model to call the promise tool one product at a time, because parallel calls overwrote each other's post-hook state and closing never became ready (PUB-11 looped on goodbye). Brief B passes 3 of 3 customers at 3/3 per the saved results. Brief A on the layered agent is 7 of 10 customers at 3/3; PUB-05, PUB-08 and PUB-10 are 0/3, and the only authority for waiving them is the coder's report.

Watch for: (1) the Brief A-on-layered target is not met and the waiver does not appear in the user's messages or in the plan (confirmed); (2) PUB-10 is plausibly an extraction-fidelity problem, not only judge sensitivity (likely); (3) uncommitted tree mixes Part 1 and Task 8 code (confirmed).

**Verdict**: NEEDS_CHANGES

## High-level view

The layered fix is small and generic: one prompt line in `_multi_lines` plus a unit test. The extracted spec is complete for multi-product closing, and Brief B is stable. The open issue is Brief A regression on the layered agent, which the coder resolved by a quoted waiver rather than by passing.

Brief A cases on the layered agent fail for two reasons: Brief B rule 8 puts a note on one-product no-promise accounts (scorer expects none), and the mandated opening recording notice trips the judge on PUB-10. Both are brief-level conflicts, but the target as written in this gate is 3/3, so the waiver needs to be recorded where the gate can see it.

<details>
<summary>Issues (4)</summary>

1. **Brief A on layered not met (confirmed)** — results/task8-a-on-layered-stability.json: 7/10 at 3/3, PUB-05/08/10 at 0/3. Either pass them or record a user waiver.
2. **Waiver unverifiable (confirmed)** — "WAIVED for PUB-05, PUB-08 and PUB-10" appears only in task8-code-report.md as relayed text; it is not in the user's messages or the plan. Put it in the gate or plan, then the verdict can flip.
3. **PUB-10 may be extraction fidelity (likely)** — the brief-a agent passes 3/3 with an opening that omits the Brief A rule 1 notice, while the layered agent keeps it and fails deterministically. The report itself lists this as an open fidelity gap; decide which behaviour is correct.
4. **Part 1 and Task 8 uncommitted together (confirmed)** — the working tree holds Part 1 hooks/compiler/engine changes alongside the Task 8 prompt change; commit Part 1 first so Task 8 is reviewable alone.

</details>

<details>
<summary>Details</summary>

## Build and slot isolation

`.freya-agents.json` has distinct ids for base, spike, brief-a and brief-ab (32204907-...), so the layered build did not overwrite base or brief-a. The report states the build used both briefs in order A then B with `--slot brief-ab`, and later builds with `--spec results/brief-ab.spec.json`. I did not see the command line itself; the slot and spec evidence are consistent with it (likely).

## Extracted spec

`results/brief-ab.spec.json` carries `closing.aggregation` with precedence transfer, any_promise, all_notes_equal, first_note_not_equal and both outcomes, so the no_promise/refused fallback is not in play. The only gap is a missing `default`, a compile warning that falls back to persuasion.on_exhausted, and the same warning exists for Brief A. `multi_product` is enabled, max 3, one date capped to each product's own deadline, with combined read-back, per-product attempts, per-product notes with reasons, and transfer and failure rules. `account_fields.products_key` is `products`. Per-product `p<k>_promise` and `closing_ready` are built at runtime in the hooks. The report says the spec was never hand-edited; nothing contradicts that, and the fix touched only builder code.

## The fix and its generality

`_multi_lines` in `builder/node_prompts.py` adds a line only when the spec is multi-product and a promise tool exists, using the resolved tool name. No brief value is present. Test: `test_negotiate_task_multi_asks_for_sequential_tool_calls`. The rejected wording that also covered the note tool broke PUB-12; narrowing it was the right call and is documented with a saved result.

The rest of the uncommitted diff (identity last-try text, account/promise/note/transfer gating, offer-once qualifier, route order) is Part 1 and out of scope here. Those changes include tests in tests/test_hooks.py, test_compiler.py, test_engine.py and test_node_prompts.py.

## Evidence for tests and contamination

Report: pytest 616 passed (>= 602), `check_contamination.py` exit 0. I did not re-run them as instructed. `git diff` shows no change to build.py, scripts/, tools/ or tests/fixtures.

## Stability results

Brief B final build (r3): PUB-11, PUB-12, PUB-13 each 3/3, matching the saved JSON. Brief A on the layered agent: PUB-01,02,03,04,06,07,09 3/3; PUB-05 and PUB-08 fail on an unexpected note (`notes [('p1','hardship')], expected []`), PUB-10 fails the judge on the opening "This call is recorded". PUB-05/08 follow Brief B rule 8 literally, so they are a genuine brief conflict. PUB-10 is deterministic over 3 reps, not noise, and the Brief A-only agent passes because its opening drops the notice, which suggests the A-only extraction lost a Brief A rule.

## Waiver

The report quotes "The Brief A-on-layered target is WAIVED for PUB-05, PUB-08 and PUB-10", dated 2026-10-11. The same claim was raised in the previous review pass and nothing new backs it: it is absent from the user's verbatim messages and from the plan, whose Part 2.5 requires single-product calls to keep passing. This review cannot treat a self-reported waiver as meeting the target.

</details>

<details>
<summary>File map</summary>

- builder/node_prompts.py: sequential promise-call line (Task 8) plus Part 1 identity/offer lines
- tests/test_node_prompts.py: test for sequential-call line
- builder/compiler.py, engine.py, hooks.py, snippets.py and their tests: Part 1, not Task 8
- results/brief-ab.spec.json, task8-*.json: saved spec and stability runs

Full diff: `git diff HEAD`.

</details>
