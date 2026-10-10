# Implementation Plan: harden Brief A (Part 1), then build and test the layered A+B agent (Part 2)

Workspace root: `/Users/melih/Desktop/build-the-builder` (all paths below are absolute or relative to it).
Python 3.11, uv. Tests: `uv run pytest -q` (baseline at HEAD 40c270a: 602 passed). Do NOT git commit anything.

## 0. Hard constraints (restate to yourself before every edit)

- No Brief A or Brief B value (dates, field names, enums, sentences, numbers, quoted phrases such as the persona's words) in `builder/`, `build.py` or `scripts/`. Wording you add must be generic ("the customer", "the floor amount", "an offer").
  `uv run pytest -q tests/test_contamination.py tests/test_check_contamination.py` and `uv run python scripts/check_contamination.py` (exit 0) must stay green after EVERY code item.
- All fixes are generic and driven by the spec (limits from `spec.identity.max_wrong_total`, floor rule from `spec.amounts.too_much.offer_floor_times`). Never a constant lifted from the brief.
- Never hand-edit an extracted spec to make a case pass. If the spec is wrong, fix the extraction prompts (`builder/extract_prompts/*.md`) or the compiler, then re-extract/rebuild.
- Do not touch: `tests/fixtures/spec_variant_*.json`, `brief_variant_*.md`, anything under `tools/` (given files), the `base` slot, `.freya-agents.json` entry `base`.
- Keep pytest >= 602 passing and add at least one unit test per fix (named in each item).
- Never retry 502/504 on the chat route (a retry replays the last turn and double-fires tools). Prefer the simulation route (`tools/run_cases.py`) for everything.
- Run ONE simulation at a time (a second one queues behind the first). Save every result JSON under `results/`.
- Credentials: run live commands from the workspace root as `set -a && . ./env.sh && set +a && ...`. Never print key values.
- Model for every extraction/build: `BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5` (user cost decision, do NOT switch to Sonnet).
- Delete unneeded `runs/<slot>-<stamp>/` folders when done, but only after item 1.1 has copied the Brief A spec to `results/` and `cmp` confirmed it.

## A. Diagnosis (what I read, so the coder need not redo it)

### A1. PUB-03 (call `f14de7f8-3a4c-4993-9652-c1fd4fb618f5`, `results/task6-stability.json` `all_rows[2]`)

Transcript sequence in the `identity` node (customer's DOB answers: wrong, wrong, wrong; real phone digits):

1. Customer answer 1 (wrong DOB) -> `verify_identity` called -> `wrong_answers_left` 2.
2. Customer answer 2 (wrong DOB) -> `verify_identity` called -> `wrong_answers_left` 1. Hook state: `identity_wrong = 2`, max is 3.
3. Customer answer 3 (wrong DOB) was bundled with a complaint and a question ("how much do I owe?"). The model did NOT call `verify_identity`. It replied "I cannot provide that information until your identity is verified. What is your date of birth?" So the third wrong answer was never submitted. Bank count stayed at 2 wrong; `identity_wrong` stayed 2; `identity_state` stayed `pending`.
4. The simulated customer, asked the same question again, gave the real DOB. `verify_identity` -> `verified: true`. Then the phone digits -> `all_verified: true`. The `identity` route to `get_account` fired legitimately (`identity_state == 'verified'` and bank `all_verified`). From there `get_account`, `facts`, `negotiate` (debt amount and product disclosed), `create_promise`, `close_call(promise)` all behaved as designed.

ROOT CAUSE: the identity gate in code was NOT bypassed. It opened because the bank genuinely verified the caller. The lockout never triggered because the third wrong answer never reached any hook: hooks only run on tool calls, and the model skipped the call on a message that mixed an answer with a question. Every later disclosure is a consequence of that one skipped tool call.

Consequences for the design:
- The "failed / wrong-count reached max -> block" gate the task asked for already exists in structure (`identity_state` set to `failed` by `verify_post_hook` in `builder/hooks.py`; compiler routes `failed` to the failed exit; `get_account` node is only reachable through the `verified` group, see `builder/compiler.py` around lines 950-960). Hardening those is cheap and worth doing (items 1.2c-d) but it would NOT have changed this run.
- No code can count an answer the model never submitted. I evaluated and REJECTED these code-only counters: (i) compare `identity.total_turns` with the number of verify calls (benign non-answer turns, such as the customer asking a question back or chatting, look identical to a skipped submission, so legitimate customers would be failed); (ii) a turn cap derived from `max_wrong_total + questions` (must have slack for chatty customers, so it never bites in this trace); (iii) a deterministic extract-then-verify node chain (large redesign, untested platform behaviour: re-entry of conversation nodes after an api node, extraction typing). (iii) is the only true hard gate; keep it as a contingency (item 1.7) and do not start it unless item 1.6 shows PUB-03 still below 3 of 3 AND you tell the user.
- The fix that works at the moment of risk is code-injected text: the verify post-hook is the only code that talks to the model at that moment, so its result text must warn the model, derived from the spec's limit, that the next answer must be submitted first (item 1.2a). Plus the identity prompt rule (item 1.2b).

### A2. PUB-06 (call `d6746855-5704-48af-be08-a79244296eaf`)

After the customer said the full amount was too much, the agent offered the floor with its date and the "rest stays overdue" note (correct). The customer replied with a non-committal "you tell me". The agent then offered the floor again with a DIFFERENT date (the deadline) and without the note. That second offer is exactly the product facts line "attempt 2 offer: <floor> on <deadline>" produced by `builder/engine.py` `_product_block` (line ~917), and the spec's `persuasion.flat_refusal_definition` says a non-answer is a flat refusal, so the model took attempt 2. The prompt's "never offer the floor again" (`builder/node_prompts.py` `_amount_lines`) loses against a ready-made attempt-2 offer line. No hook can see a spoken offer (hooks run only on tool calls), so a state flag "set in a hook when the floor offer is made" is not possible. What IS code-controlled and spec-derived: the facts text the model is given (`engine.py` runs inside the facts code node and is embedded by `builder/snippets.py`).

### A3. PUB-01 (call `7313c2d6-6df6-4d87-9046-79b728b2d862`) and PUB-10 (call `cbfc357f-2987-4bf9-bbcc-c71259b0b623`)

Read both transcripts: same tool path as the passing calls, a clean last message ("...Thank you and goodbye."), `endedReason` assistant-ended-call, no extra turn after the goodbye, no trailing text, 14 s and 18 s long. The platform's own call-level `analysis.successEvaluation.success` is `true` for BOTH calls. The ledger scored them fine; the only failure string is `conversation: no verdict`, i.e. the simulation run record had no `outcome` (iteration runs expose `attempts`/`maxAttempts` = 0/4). That is platform judge noise, not an agent fault. No agent change is planned for it. Observed rate: 2 of 30 case-runs in the stability run.

Retry options in the given tools (read `tools/run_cases.py`, `tools/score.py`; do NOT edit): `--only PUB-01,PUB-10` re-runs chosen customers; `--reps N` repeats the whole selection; `--rerun-if-slow N` only repeats a run whose median slowest turn is above 10 s (waits 5 minutes) and is unrelated to missing verdicts; there is no no-verdict retry. Therefore: report the official number as printed, and separately report a "no-verdict adjusted" number only with the call-level evidence (item 1.5).

### A4. Saved Brief A spec (use it for every Part 1 rebuild to avoid extraction variance)

`/Users/melih/Desktop/build-the-builder/runs/brief-a-20261011-005410/spec.json` (26659 bytes; byte-identical to `/Users/melih/Desktop/build-the-builder/runs/brief-a-20261011-003227/spec.json`, checked with `cmp`). It contains `closing.aggregation` (precedence transfer, any_promise), `identity.max_wrong_total: 3`, `amounts.too_much.offer_floor_times: 1`, `multi_product.enabled: false`. Agent: slot `brief-a`, agent `11b98cde-c639-48ab-a4b0-1b4f0a46d2ff` (workflow id in `.freya-agents.json`). Never touch slot `base`.

### A5. Layered build semantics (README + `build.py`)

`build.py <brief1.md> <brief2.md> ...` layers briefs in the order given (later builds on earlier). `--slot NAME` picks the Freya slot (default: stem of last brief; the slot is the key in `.freya-agents.json`, the workflow is updated in place on a rebuild and published). `--spec FILE` skips extraction. Each run writes `runs/<slot>-<stamp>/` (`spec.json`, `workflow.json`, `compile_report.json`, `extraction_notes.md`, `trace.log`, `<slot>.workflow.json`). Part 2 uses slot `brief-ab`.

---

# PART 1: harden Brief A (loop stops when `.agents/tasks/hardening-review.json` has `verdict == APPROVED`)

- [ ] 1.1 Pin the Brief A spec and the baseline.
      Copy the saved spec to a stable path and verify: `cp runs/brief-a-20261011-005410/spec.json results/brief-a.spec.json && cmp runs/brief-a-20261011-005410/spec.json results/brief-a.spec.json`. Confirm the baseline tree is green: `uv run pytest -q` (602 passed) and `uv run python scripts/check_contamination.py` (exit 0). Do not edit the copied spec.
      Files: `results/brief-a.spec.json` (new, copy only)
      Verify: `cmp` prints nothing; pytest 602 passed; contamination script exit 0.

- [ ] 1.2 Identity hardening (PUB-03). Four sub-changes, generic and spec-derived.
      a. `builder/hooks.py` `_VERIFY_BODY` / `verify_post_hook`: after a wrong answer, compute `left` = `MAXW - wrong` (when `MAXW` is not None; also take `min` with the bank's `wl` when both are known). When `left == 1`, append a new text `last_try` (add to `DEFAULT_TEXTS`, pass as const `T_LAST`) saying: only one wrong answer is left before the check ends; if the customer's next message contains an answer, submit it to the tool first even when the message also asks something; answer no question before the tool result is in. When `left >= 2`, keep the existing text. Use no brief wording, no doubled braces, no imports (see module docstring rules). Add `last_try` to the cfg `texts` override mechanism like the others. Update the module docstring only if you add a variable (avoid new variables).
      b. `builder/node_prompts.py` `_identity_lines`: add one generic line next to "Call <tool> for each answer before you ask the next question": a message that contains an answer must be submitted to the tool first even if it also contains a question or complaint; do not discuss anything else until the tool result is in.
      c. `builder/compiler.py` `build()` (identity transitions, ~line 957): put the "security check failed" group BEFORE the "security questions are passed" group, so `identity_state == 'failed'` always wins.
      d. `builder/compiler.py` negotiate transitions (`neg_trans`, ~line 990): add a first group `identity_state == 'failed'` -> `failed_exit.code if failed_exit else NODE_FINALIZE_FORCED` ("route out of negotiate"). In `builder/hooks.py` add a shared helper `_H_GATE` (returns True when `_s(vars.get('identity_state')) == 'failed'`) and use it at the top of `_PROMISE_PRE_BODY`, `_NOTE_PRE_BODY` and `transfer_pre_hook`: when failed, blank the product/queue body field and set `vars['promise_guard']` / `vars['note_guard']` to `'blocked'` (the bank then rejects; nothing is recorded). Add a new `account_pre_hook(cfg)` in `hooks.py` with the same gate that blanks the customer-id body field, and attach it to the account tool in `builder/compiler.py` (`account_tool = self.tool(ROLE_ACCOUNT, ...)`, via `tools_gen.attach_hooks(tool, pre=...)`; cfg gets the customer field name from `self.fields[ROLE_ACCOUNT]["customer"]`). Gate ONLY on explicit `failed`, never on "not verified", because identity_state can be unset if the verify hook ever falls back. Hooks stay inside the existing try/except `_assemble` shape.
      Files: `builder/hooks.py`, `builder/node_prompts.py`, `builder/compiler.py`
      Tests (add): in `tests/test_hooks.py` next to `test_verify_third_wrong_fails_and_never_downgrades`: last-try text appears exactly when one wrong answer is left (MAXW 3 -> after the 2nd wrong; MAXW 2 -> after the 1st; MAXW None and bank `wrong_left` 1 -> appears) and never after the first of three; promise/note/transfer/account pre-hooks blank the field when `identity_state == 'failed'` and leave the request untouched when `pending`/`verified`/unset (use `tests/hook_harness.run_hook`, both layouts). In `tests/test_compiler.py`: compiled identity node lists the failed group before the verified group, the negotiate node's first transition tests `identity_state == 'failed'`, and `get_account` is targeted only by the verified group. In `tests/test_node_prompts.py`: the identity task contains the submit-first rule.
      Verify: `uv run pytest -q tests/test_hooks.py tests/test_compiler.py tests/test_node_prompts.py tests/test_selfcheck.py tests/test_pipeline_compile.py` pass; full `uv run pytest -q` >= 602 + new passing; contamination checks green.

- [ ] 1.3 Offer-once hardening (PUB-06). Spec-derived facts and prompt, no wording from the brief.
      a. `builder/engine.py` `_product_block` (the "attempt k offer" lines, ~line 913-921): when `spec["amounts"]["too_much"]["offer_floor_times"] == 1` and attempt k (k >= 2) has the same amount as the floor offered under the too-much rule (`attempt_offer(..., True)` amount of attempt 1, or `floor_for`), append a spec-neutral qualifier to that line: it is a repeat of the floor offer, use it only after the customer has clearly refused the floor offer, never in reply to an answer that is not a refusal. Keep the function import-free and sandbox-safe (it is embedded by `builder/snippets.py`; `tests/test_snippets.py`/`tests/snippet_harness.py` prove it runs without imports).
      b. `builder/node_prompts.py` `_amount_lines` (the `offer_floor_times == 1` branch) : add that an answer to the floor offer that neither accepts nor clearly refuses (the customer leaves the choice to you, asks something, or is unsure) is not a reason for a new offer: restate nothing new, read the offer already made back (same amount, same date) and ask the confirmation question. Only emit this when `offer_floor_times == 1` so it stays spec-driven. Check `_persuasion_lines` does not contradict it: the persuasion block still says a non-answer is a flat refusal when the spec says so; word the new rule as "an answer that leaves the choice to you accepts your offer for read-back purposes" only if it does not clash with the spec's own `flat_refusal_definition`; if a clash is unavoidable, keep (a) and the read-back rule narrow (only for the floor offer) and say so in the report.
      Files: `builder/engine.py`, `builder/node_prompts.py`
      Tests (add): `tests/test_engine.py` product block with `offer_floor_times` 1 shows the qualifier on the repeating attempt and not on attempt 1; with `offer_floor_times` None/2 the block is byte-identical to before (guard against regression). `tests/test_node_prompts.py`: the amounts text contains the read-back rule only when the flag is 1. Re-run `tests/test_task9_variants.py` (variants must still compile; do not edit the variant fixtures).
      Verify: `uv run pytest -q` all green; contamination checks green.
      Limitation to state in the report: a hook cannot see a spoken offer, so this is a facts and prompt guard, not a hook flag. Measure it live in 1.6.

- [ ] 1.4 Rebuild Brief A from the pinned spec (no extraction variance).
      Command (from workspace root): `set -a && . ./env.sh && set +a && BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5 uv run python build.py briefs/brief-a.md --slot brief-a --spec results/brief-a.spec.json`.
      Read the printed run dir: check `compile_report.json` lint has no errors, `trace.log` has no `DEGRADED:` line, and the smoke test line is ok. The `brief-a` slot is updated in place (same agent id `11b98cde-c639-48ab-a4b0-1b4f0a46d2ff`, version bumps). Confirm the account node with the new pre-hook still works: the first live run's `get_account` call must succeed (platform honoured hooks on `pre_actions` tools only if this works; if it does not, remove the account pre-hook and keep the route-order guard, and say so).
      Verify: build exit code 0, no `DEGRADED:` in `trace.log`.

- [ ] 1.5 Live run of the 10 Brief A cases, one at a time.
      `uv run python tools/run_cases.py 11b98cde-c639-48ab-a4b0-1b4f0a46d2ff --brief A --out results/hardening-a-run1.json` (about 145 s). Diagnose each fail by reading `freya.get_call(<call_id>)["messages"]` for the call id in the `rows` of the results file (small helper via `PYTHONPATH=. uv run python - <<'E' ... E`, nothing committed). For any row whose only failure is `conversation: no verdict`, read `get_call(call_id)["analysis"]["successEvaluation"]` and record it as the call-level verdict in the report (this is evidence for the "judge noise" claim, not a replacement for the official score).
      Verify: results file exists; every failure has a written cause.

- [ ] 1.6 Stability run and decision.
      `uv run python tools/run_cases.py 11b98cde-c639-48ab-a4b0-1b4f0a46d2ff --brief A --reps 3 --out results/hardening-a-stability.json` (about 12 minutes). Target: >= 9 of 10 customers pass 3 of 3, and every remaining failure is demonstrable judge noise (`no verdict` with call-level `successEvaluation.success == true` and a ledger that scored clean). If PUB-03 or PUB-06 still fail: first run the single case 5 times to measure its rate (`--only PUB-03 --reps 5`, then `--only PUB-06 --reps 5`, results under `results/`), read the failing transcripts, and fix generically (up to 2 more iterations, rebuilding from the pinned spec each time). Do not exceed that; report the honest rate.
      Verify: results file `results/hardening-a-stability.json`; the report states the exact 3-of-3 count and the list of failures with causes.

- [ ] 1.7 Contingency, NOT to be started without telling the user: deterministic answer submission.
      Only if PUB-03 is still below 3 of 3 after 1.6: write up (do not implement) the extraction-driven identity chain (extract answer into a typed variable per question, route to a verify `api_request` node, return to the conversation node) with its untested-platform risks, and ask the user. Record this in the report.

- [ ] 1.8 Close out Part 1.
      Run `uv run pytest -q` (>= 602 + new, all pass), `uv run python scripts/check_contamination.py` (exit 0), `uv run python scripts/rehearse.py briefs/brief-a.md --spec results/brief-a.spec.json` (exit 0, no stage over budget; add `--keep` only if you need the folder). Write `.agents/tasks/hardening-report.md` (root causes, changes, test names, the stability numbers, noise rate, anything unsolved). Delete the two `runs/brief-a-*` folders and any rehearsal leftovers once the spec copy exists. Do not commit.
      Verify: commands above; `git status --short` shows only intended source, test, results and report files.

---

# PART 2: Task 8, Brief B layered on Brief A (loop stops when `.agents/tasks/task8-review.json` has `verdict == APPROVED`)

Part 2 starts after Part 1 is approved, because the identity guard and the offer-once fix are inherited.

- [ ] 2.1 Extract and build the layered agent.
      `set -a && . ./env.sh && set +a && BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5 uv run python build.py briefs/brief-a.md briefs/brief-b.md --slot brief-ab` (briefs layered A then B; creates the new slot `brief-ab` in `.freya-agents.json`; never `base` or `brief-a`). Note the run dir printed. Immediately copy its spec: `cp runs/brief-ab-<stamp>/spec.json results/brief-ab.spec.json`.
      Read `compile_report.json`, `extraction_notes.md`, `trace.log` (no `DEGRADED:`), lint errors empty. Reference only (older extraction, same briefs): `results/extract-haiku-ab.json`; compare with `uv run python scripts/diff_spec.py` if it helps (check its usage first).
      Verify: exit code 0, `workflow.json` present, `brief-ab` key in `.freya-agents.json` with an agent id (print only the id, no keys).

- [ ] 2.2 Check the extracted spec for the multi-product essentials (read `results/brief-ab.spec.json`, nothing hand-edited).
      Required: (a) `closing.aggregation` present and complete for several products: precedence containing transfer and any_promise, `all_notes_equal` (paid), `first_note_not_equal` (no_promise with the first non-paid product's reason), `transfer_outcome`, `promise_outcome` (a missing aggregation silently falls back to the fixed no_promise/refused pair in `hooks._final_pair`); (b) `multi_product.enabled == true`, `max_products == 3`, `one_date_for_all` set to the cap-to-own-deadline rule, `combined_readback`, `say_capped_in_readback`, `per_product_attempts`, `state_all_before_asking`, `ask_all_today_first`, `notes.for_products_without_promise`, `notes.reasons`, `on_transfer` (no notes, keep recorded, drop unrecorded), `on_promise_failure.note_reason`; (c) `account_fields.products_key` equals the real list key of the get_account response in `tools/openapi.json` (an older extraction omitted it; the compiler falls back to the schema array with a warning, which is acceptable only if `compile_report.json` shows the fallback found the list); (d) `bands` with deadlines per band (per-product deadline is derived from each product's `days_late`); (e) compiled `workflow.json` carries per-product state names (`p1_promise`..`p3_promise`, `closing_ready`) in the promise/note post-hooks and the finalize code node, and the `add_note` tool is present with its reason enum from the openapi.
      If anything is missing or wrong: fix it GENERICALLY in `builder/extract_prompts/*.md` (extraction) or `builder/compiler.py`/`hooks.py` (compile), add a unit test for the fix (e.g. `tests/test_extract.py` for prompt text expectations, `tests/test_compiler.py` for compile behaviour), re-run extraction once, save the new spec to `results/brief-ab.spec.json`, and from then on rebuild with `--spec results/brief-ab.spec.json --slot brief-ab`. Never patch the JSON.
      Verify: a short checklist (each item PASS/FAIL with the spec key) in `.agents/tasks/task8-report.md`; after any fix `uv run pytest -q` and `uv run python scripts/check_contamination.py` stay green.

- [ ] 2.3 Live Brief B single run, then stability (one simulation at a time).
      `uv run python tools/run_cases.py <brief-ab agent id from .freya-agents.json> --brief B --out results/task8-b-run1.json` (3 cases, quick). Read every failing transcript. Expected pitfalls to look at: PUB-11 (three products stated before any ask, one read-back and one confirmation, three `create_promise` calls with forced product ids, one date for all); PUB-12 (phone question re-asked once and DOB not; personal loan gets 2 attempts (late band), car loan 1 (early band); two `add_note` calls with the refused reason; outcome is `promise`); PUB-13 (one shared date clamped per product to its own deadline by `promise_pre_hook` using `business_days` and `p<k>_deadline`; the read-back names both dates). Then: `... --brief B --reps 3 --out results/task8-b-stability.json`. A customer counts only with 3 of 3.
      Verify: result files exist; each failing case has a cause and a generic fix or an honest note.

- [ ] 2.4 Fix, rebuild, re-run (max 2 rounds).
      For each generic fix: unit test first (`tests/test_hooks.py`, `tests/test_compiler.py`, `tests/test_node_prompts.py`, `tests/test_engine.py` as appropriate), contamination checks, then `BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5 uv run python build.py briefs/brief-a.md briefs/brief-b.md --slot brief-ab --spec results/brief-ab.spec.json`, then re-run `--brief B --reps 3 --out results/task8-b-stability-r2.json`. Apply the same flaky-class rules as Part 1: identity submit-first, offer-once, judge `no verdict` (verify with the call-level `successEvaluation`, report it separately).
      Verify: pytest green; stability file saved.

- [ ] 2.5 Regression: Brief A cases on the layered agent.
      `uv run python tools/run_cases.py <brief-ab agent id> --brief A --out results/task8-a-on-layered.json`, then `--brief A --reps 3 --out results/task8-a-on-layered-stability.json`. Single-product calls on the layered agent must keep passing (expect the same noise classes as Part 1). Any new regression is fixed generically (the layered agent compiles with `multi_product.enabled`, so single-product paths use the multi paths of the hooks).
      Verify: results files saved; report states Brief A 3-of-3 count on the layered agent next to Part 1's.

- [ ] 2.6 Close out Part 2.
      `uv run pytest -q` (>= 602 + new), `uv run python scripts/check_contamination.py` (exit 0), `uv run python scripts/rehearse.py briefs/brief-a.md briefs/brief-b.md --spec results/brief-ab.spec.json` (exit 0). Write `.agents/tasks/task8-report.md` (spec checklist from 2.2, scores per run file, fixes with test names, honest unsolved items). Delete unneeded `runs/` folders (keep nothing that is not referenced; the spec copies live under `results/`). Do not commit.
      Verify: commands above; `git status --short` clean of stray files.

## Decomposition note

I did not restructure the workflow tail: Part 1 and Part 2 are already the two sequential loops, both mutate the same worktree, build the same agent compiler, and Part 2 depends on Part 1's hooks and prompts. FEAT artifacts would add handoff cost without enabling any parallelism.
