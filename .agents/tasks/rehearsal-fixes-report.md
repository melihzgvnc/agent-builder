# Rehearsal fixes: final report
HEAD is still 23a45c7. Nothing was committed or pushed. `tools/`, `public-test-set.json`, `freya.py` and the five protected fixtures (`spec_brief_a`, `spec_brief_ab`, `spec_variant_1..3`) have empty diffs.

## Verification
- `uv run pytest -q`: 832 passed, 0 failed (baseline 679).
- `uv run python scripts/check_contamination.py`: 37 files, 0 strict hits, 0 allowlist problems.
- `uv run python scripts/offline_verify.py` prints one line per spec: `<spec>: nodes=N lint=Xe/Yw checks=N failed=0 gaps=<list or -> degraded=<yes|no>`, then `6 spec(s), 0 with errors`.
  - Brief A, Brief A+B: no gaps, not degraded.
  - Variants 1 to 3: degraded on `amount_floor`.
  - Rehearsal spec: degraded on `amount_floor`, `callback_window`, `deadline_formula`, `persuasion_date`.
- Leftovers: no `runs/offline-*` dirs or temp files exist, so nothing was removed. `runs/rehearsal-20261011-132458`, `results/`, `rehearsal/` and `.agents/tasks` are untouched.

## What changed per fix
1. Promise hook rejects instead of rewriting values, so the read-back always matches the recorded value: `builder/hooks.py`, `builder/engine.py`, `builder/snippets.py`.
2. Structured optional spec fields for calendar-day deadlines, business-day deadlines, percent floors and a per-weekday callback window: `builder/spec.py`, `builder/spec_schema.json`, `builder/engine.py`, `builder/compiler.py`.
3. Gap guard marks specs as degraded when key facts are missing: new `builder/gap_guard.py`, wired in `builder/pipeline.py`.
4. Calendar and allowed-date facts in the prompt (weekday names, per-product allowed dates): `builder/node_prompts.py`, `builder/snippets.py`.
5. Mandated introduction before the confirmation question, via `first_sentence.template`: `builder/node_prompts.py`, `builder/compiler.py`.
6. Guaranteed termination and spoken goodbyes on end nodes: `builder/compiler.py`. During the live step, instruction-like goodbye text was ignored in favour of the default farewell (also `builder/extract_prompts/sections.md`).
7. Resilient simulation polling that tolerates 5xx responses: new `builder/sim_poll.py`, `builder/online_runner.py`, `builder/online_cases.py`, `builder/online_repair.py`.
8. Latest-brief-wins extraction and an explicit per-product loop: `builder/extract_prompts/*.md`, `builder/node_prompts.py`. During the live step, loop step 3 was narrowed to "refusal after the persuasion attempts for this product are used up".

Supporting changes: `builder/lint.py`, `builder/selfcheck.py`, `scripts/check_contamination.py`, new `scripts/offline_verify.py`, new tests and fixtures (`test_gap_guard`, `test_sim_poll`, `test_offline_verify`, `test_backward_compat`, `golden_facts.json`, `spec_rehearsal.json`).

## Live step (Sonnet 5.5 extraction)
Confirmed:
- Fix 1 (PROBE-03, 04, 09, 10).
- Fix 2 in extraction and build (0 gaps on the rehearsal rebuild).
- Fix 3 stays silent on a clean extraction. The positive case is offline only.
- Fix 5 (10/10 transcripts).
- Fix 6 (no goodbye loops).
- Fix 8, after the narrowing.
- Brief A is effectively 10/10.
- Rehearsal rebuild: lint 0 errors, selfcheck 0 failed, online test 8/9.

Not confirmed or open:
- PROBE-08: the judge counts the opening question plus 3 offers as 4 persuasion attempts. It failed twice and is left as a finding.
- Brief B: PUB-11 3/3, PUB-12 2/3, PUB-13 2/3, where earlier runs gave 3/3. The remaining PUB-13 miss is the agent offering the card's own later deadline when the customer asks for one date for all products. This looks like model variance and was not changed.
- Fix 7 was not exercised live (no 5xx during polling); it is offline only.
- Fix 4 was not isolated; it is only indirectly supported. The per-weekday window was not exercised by a probe.
- Platform issues seen: Cloudflare 502/530 on the agent LLM, and one "analysis never arrived".
- Review scope was light. Engine/snippet/hook agreement on floors and rounding, weekday names and the spoken-end goodbye were not traced line by line.

## Changed and new files (`git status --short`)
Modified:
- `builder/`: compiler.py, engine.py, extract_prompts/{coverage,sections,system}.md, hooks.py, lint.py, node_prompts.py, online_cases.py, online_repair.py, online_runner.py, pipeline.py, selfcheck.py, snippets.py, spec.py, spec_schema.json
- `scripts/check_contamination.py`
- `tests/`: test_compiler.py, test_compiler_fallback.py, test_contamination.py, test_engine.py, test_extract.py, test_hooks.py, test_lint.py, test_node_prompts.py, test_pipeline_compile.py, test_pipeline_selfcheck.py, test_selfcheck.py, test_snippets.py, test_spec.py

Untracked (new):
- `builder/gap_guard.py`, `builder/sim_poll.py`
- `scripts/offline_verify.py`
- `tests/test_backward_compat.py`, `tests/test_gap_guard.py`, `tests/test_offline_verify.py`, `tests/test_sim_poll.py`
- `tests/fixtures/golden_facts.json`, `tests/fixtures/spec_rehearsal.json`
- `rehearsal/`
- `.agents/tasks/` reports and plans, including this file
