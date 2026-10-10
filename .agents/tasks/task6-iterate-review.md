# Task 6 iteration 1: Brief A at 10/10 after generic compiler, hook and prompt fixes

Brief A scored 10/10 on the live simulation (`results/task6-iter1b.json`: passed 10, total 10, simulation bb8514a1-de3c-440b-804c-7cf675bab633). Stop criterion 1 (all 10 cases pass) is met. The fixes in the diff are spec-driven and generic. I did not re-run tests, the contamination scan or any simulation. pytest (602 green) and contamination (exit 0) are taken from the coder's report. I did not independently confirm them.

Watch for: the floor-offer-once wording and the verification hook texts have no dedicated tests (possible, low risk). The hook date arithmetic is hand-rolled but has a test against `engine.allowed_callback_dates`.

**Verdict**: APPROVED

## High-level view

The 5/10 after re-extraction alone came from the `closing.aggregation` extraction gap. The remaining failures were a compiler bug (callback calendar only written after the account lookup) and prompt wording gaps (verification claims, holiday handling, retry after a failed call, floor offer repeated). Each fix reads its values from the spec (`now_var`, weekend, holidays, `days_n`, retry count, `offer_floor_times`) rather than from Brief A. Protected files do not appear in the diff.

<details>
<summary>Issues (1)</summary>

1. **Untested prompt wording** — the floor offer-once text and the verification post-hook messages have no dedicated test. Optionally add a prompt-text assertion in a later pass.

</details>

<details><summary>Details</summary>

## Callback calendar computed in the pre-hook

The calendar variables used to be set only by the facts node, which runs after the account lookup. A third-party caller never reaches that node, so the callback pre-hook blanked every date. The hook now derives today, the time and the allowed dates from the now-variable when the facts node has not run. Weekend, holiday and window size come from the spec through `compiler.py`. The date arithmetic uses integers with no imports, which the platform notes confirm hooks need. A test compares the result with `engine.allowed_callback_dates` across several dates. The diagnosis matches the ledger evidence (blanked `date ""` calls) recorded in PLATFORM_NOTES. Confidence: confirmed.

## Prompt fixes tied to transcripts

The post-hook texts after a wrong identity answer now say the customer is not yet verified. This addresses PUB-03, where the model said "verified" and looped. The promise section retries the call with identical values up to the spec's retry count (PUB-10). A named non-business day gets the next business day (PUB-07). The floor offer is made once (PUB-06). All wording is built from spec fields.

## Extraction

The sections prompt now asks for `closing.aggregation` even for a single product. This was the root cause of 6 of the failing cases, and 3 of those passed after re-extraction alone. The prompt names no brief values.

## Constraints

No Brief A or B values appear in the added lines of builder/. The `base` agent was untouched and only the brief-a slot was rebuilt. Nothing is committed yet.

</details>

<details><summary>File map</summary>

builder/compiler.py (calendar config passed to hook), builder/hooks.py (calendar and messages), builder/node_prompts.py (clock, retry, holiday, floor wording), builder/extract_prompts/sections.md (aggregation), builder/PLATFORM_NOTES.md (practice-run findings), tests/test_hooks.py and tests/test_node_prompts.py (new tests). Full diff: `git diff`.

</details>
