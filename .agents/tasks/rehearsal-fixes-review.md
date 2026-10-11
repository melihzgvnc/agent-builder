# Rehearsal fixes: reject-only promise guard, gap guard, polling resilience and related builder changes

This change set covers eight fixes, all uncommitted in the working tree. The main ones are a promise pre-hook that only rejects on certain rule breaks, a gap guard that marks specs as degraded, resilient simulator polling, and spoken end nodes. Scope was kept to what a successful builder needs. Review depth was deliberately light, at the user's request: a full 12-point audit was not done, and the verification below is what was actually checked.

Watch for: the removed clamp tests were replaced by reject-only behavior, so the old assertions are gone by design (confirmed). The rehearsal spec is degraded on four gaps (amount_floor, callback_window, deadline_formula, persuasion_date), which is the intended signal and not a bug (confirmed).

**Verdict**: APPROVED

## High-level view

The promise pre-hook moved from clamping values to rejecting only on certain rule breaks. Many old clamp assertions were removed, and the new `hooks.py` text says a rule is checked only when the fact it needs is known. Unknown facts pass through.

A gap guard now degrades specs that lack key facts. The offline verifier shows the four earlier specs degraded on `amount_floor`, and the rehearsal spec degraded on four gaps. No spec has lint errors or failed checks.

Contamination stays clean. The only references to the rehearsal and brief fixtures in `scripts/` are source-list paths in the contamination checker and the offline verifier. No brief values appear in `builder/` or `build.py`.

<details>
<summary>Issues (1)</summary>

1. **Light-touch review scope** (possible) — per-fix semantics (engine, snippet and hook agreement on floors and rounding; weekday names from the spec calendar; the spoken-end goodbye) were not traced line by line. If the next online or rehearsal run misbehaves in those areas, start there.

</details>

<details>
<summary>Details</summary>

## Verification run

`uv run pytest -q` gave 832 passed, 0 failed. `check_contamination.py` reported 0 strict hits and 0 allowlist problems over 37 files. `offline_verify.py` reported 6 specs, 0 with errors, 0 failed checks.

## Commit and frozen paths

HEAD is still 23a45c7. `git diff HEAD --stat` on `tools`, `public-test-set.json` and `freya.py` is empty. The diff stat for the five protected fixtures is empty, so they are byte-identical.

## Test changes

Removed assertions are concentrated in the old promise clamp tests (`test_promise_date_clamp_agrees_with_engine`, `test_promise_amount_clamp_agrees_with_classify` and similar) and in a few shape assertions. These match the intended change from clamping to rejecting. New tests exist for the gap guard, simulator polling, offline verification and backward compatibility.

</details>

<details>
<summary>File map</summary>

Builder changes are in `compiler.py`, `engine.py`, `hooks.py`, `lint.py`, `node_prompts.py`, `online_*`, `pipeline.py`, `selfcheck.py`, `snippets.py`, `spec.py` and `spec_schema.json`. New files are `builder/gap_guard.py`, `builder/sim_poll.py` and `scripts/offline_verify.py`. New test fixtures are `golden_facts.json` and `spec_rehearsal.json`. Full diff: `git diff HEAD`.

</details>
