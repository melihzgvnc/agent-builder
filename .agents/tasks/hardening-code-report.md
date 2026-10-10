# Hardening Part 1: code report (first iteration)
Model for all builds: anthropic/claude-haiku-5.5. Nothing committed. Slot `brief-a` (agent 11b98cde-...), rebuilt from `results/brief-a.spec.json` (cmp-identical copy of the saved spec; not edited). Slot `base` untouched.

## Verification run (final tree)
- `uv run pytest -q`: 615 passed (baseline 602).
- `uv run python scripts/check_contamination.py`: exit 0.
- Builds: `BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5 uv run python build.py briefs/brief-a.md --slot brief-a --spec results/brief-a.spec.json` x3, each exit 0, no `DEGRADED:`, lint `[]`, smoke ok.
- Live results (saved files, scores read from them):
  - `results/hardening-a-run1.json`: 10/10 (first build).
  - `results/hardening-a-stability.json`: --reps 3, 9/10 customers 3/3; PUB-06 2/3 (offer repeated; judge text: offered twice).
  - `results/hardening-a-stability-r2.json`: --reps 3, 9/10; PUB-06 2/3 (my first read-back wording made the agent restate the offer: counterproductive, replaced).
  - `results/hardening-pub06-r5.json`: PUB-06 alone, 5/5 after the final wording.
  - `results/hardening-a-stability-r3.json`: FINAL build, --reps 3: 10/10 customers pass 3 of 3.

## Changes (all generic, spec-driven)
1. PUB-03 identity (`builder/hooks.py`, `builder/compiler.py`, `builder/node_prompts.py`):
   - Verify post-hook appends a `last_try` text when one wrong answer is left (limit = spec `max_wrong_total`, also min with bank `wrong_left`): submit the next answer to the tool first, answer nothing before the tool result.
   - Identity prompt: a message containing an answer is submitted to the tool first even with a question/complaint.
   - Compiler: `identity_state == failed` group is now first in identity transitions; negotiate has a first `failed` group routing to the failed exit.
   - New `_H_GATE`; promise/note/transfer pre-hooks blank the product/queue field and set `*_guard='blocked'` when `identity_state == 'failed'`; new `account_pre_hook` (blanks customer id) attached to the account tool. Gate only on explicit `failed`.
   - Limitation (root cause, from plan): the original failure was a skipped tool call; no hook can count an answer never submitted. The text + prompt guard is what addresses it; item 1.7 (deterministic extraction chain) was NOT needed since PUB-03 was 3/3 in all 3 stability runs.
2. PUB-06 offer-once (`builder/engine.py`, `builder/snippets.py`, `builder/node_prompts.py`): attempt k>=2 product-fact lines that repeat the floor get a qualifier (only when `offer_floor_times == 1`); `too_much` added to the embedded amounts subset in `ENGINE_SPEC_KEYS` (required for the snippet to see the flag); amounts prompt says a non-accepting/non-refusing answer is no reason to repeat the amount or date, answer any question briefly and ask only the confirmation question. A hook cannot see a spoken offer, so this is a facts+prompt guard.
3. PUB-01/PUB-10 "no verdict": no code change. Per plan A3 (transcripts read: clean ending, no extra turn, platform successEvaluation true); in the 3 stability runs of the final build both passed 3/3, so no no-verdict occurred; earlier rate 2 of 30 case-runs. Nothing under `tools/` touched.

## Tests added
- tests/test_hooks.py: test_verify_last_try_text_when_one_wrong_answer_is_left, test_verify_last_try_text_follows_max_wrong_and_bank_count, test_pre_hooks_block_when_identity_failed.
- tests/test_compiler.py: test_failed_identity_wins_and_account_is_reached_only_when_verified, test_account_tool_has_the_identity_gate_pre_hook; existing tests re-indexed for the new transition order.
- tests/test_engine.py: test_product_block_marks_repeat_floor_offer_only_when_floor_is_offered_once.
- tests/test_node_prompts.py: test_identity_task_has_submit_first_rule, test_amounts_read_back_rule_only_when_floor_offered_once (rule text only when flag is 1).

## Cleanup
`runs/brief-a-*` deleted after `cmp` of the pinned spec copy. `results/brief-a.spec.json` kept (needed for rebuilds). Account pre-hook worked live (get_account succeeded in all runs).
