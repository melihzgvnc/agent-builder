# Compile-fix report

## Changes
- builder/compiler.py: `resolve_fields` now uses `_derive_fields` / `_cue_match` (+ `SEMANTIC_CUES`). Explicit field_map wins; alias keys (a key that is an API field), name/type/enum cues, and single-gap elimination fill the rest. Ambiguity for a required field of verify/promise/close raises `CompileError("tool X: cannot derive field for 'sem'; candidates [...]; set spec.tools.<role>.field_map")`. Every derived mapping is a `CompileReport` warning ("derived field_map ..."); enums not in the spec log "allowed_values for 'f' taken from the openapi enum". The openapi enum already drives tool schemas (spec allowed_values only produced a warning), so no schema change was needed.
- builder/extract.py: `_fill_tool_fields` (drops non-API field_map values/allowed_values keys, never invents); compile-feedback loop `_compile_repair_loop` (`COMPILE_REPAIRS = 2`, purpose `extract_compile_repair_N`, only while `asker.remaining() > 30`, patch merged by top-level section, invalid patch discarded, never raises); `report["compile_repairs"]`/`compile_error`; shown in notes and `ctx.results["extraction"]`.
- builder/extract_prompts/sections.md (semantic names per tool role, exact API field names, compact output), new compile_repair.md.
- Token cap decision: sectioned generation NOT done (cross-section references make it risky). Done instead: compactness prompt line, and a truncated reply (`stop_reason == max_tokens`) is retried with a "cut off, be more compact" hint instead of the identical request. `SPEC_MAX_TOKENS` stays 20000 (comment documents this).
- builder/selfcheck.py: one generic false positive fixed. Variant 2's spec has a documented gap ("no outcome for a verified call that ends with nothing recorded"); finalize then writes nothing and the close hook intentionally leaves the model's own values, which the check compared with a placeholder. The comparison is now skipped when finalize decided no outcome.
- Tests: new tests/test_compile_fix.py (derivation clear/partial/alias/explicit-wins/ambiguous/wrong-value/enums, `_fill_tool_fields`, repair loop with FakeLLM: success, invalid patch + cap of 2, LLMError, no call when compilable, no call on low budget, truncation hint); tests/test_task9_variants.py gains `test_variant_compiles_to_real_workflow` over variant_1..3, brief_a, brief_ab (no CompileError so never fallback, 0 lint errors, selfcheck failed == []). Before the compiler change variants 1-3 raised `CompileError: tool 'verify_identity' has no field for 'question'`.

## Verification (run in this step, no live LLM, no Freya)
- `uv run pytest -q` -> 599 passed.
- `uv run python scripts/check_contamination.py` -> exit 0 ("0 strict hit(s), 0 allowlist problem(s)"). tests/test_contamination.py is part of the full pytest run (passed).
- Not verified here: live extraction with the real model (finalize step).

## Live verification (model anthropic/claude-haiku-5.5 via OpenRouter, `--dry`, Freya backend not called)
- Step 1: `build.py briefs/brief-a.md --dry` -> exit 0, wall 89 s, 2 model calls (in 30246 / out 22243 tokens), compile_repairs 0. compile_report.json: fallback_workflow false, lint [] (0 errors), selfcheck 116 checks, failed [].
- Step 2: `build.py briefs/brief-a.md briefs/brief-b.md --dry` -> exit 0, wall 178 s, 3 model calls (in 46755 / out 42900 tokens), compile_repairs 0. fallback_workflow false, lint [] (0 errors), selfcheck 124 checks, failed [].
- Offline re-check on final code: `uv run pytest -q` -> 599 passed; `scripts/check_contamination.py` -> exit 0 (0 strict hits, 0 allowlist problems).
- Remaining warnings: compile warning "closing.aggregation.default is missing: a verified call that ends with nothing recorded falls back to no_promise/refused" (both runs). Brief A selfcheck warnings: outcome rows promise/, transferred/restructuring, transferred/human, transferred/support are not reachable by a compiled code path (model-only). Extraction notes still list brief tokens not found in the spec (4 for Brief A, 10 for the combined run), advisory only.
- Still blocked on the Freya backend (down): non-dry deploy/smoke test, and any real-conversation scoring. Nothing here exercised the API.
- Cleanup: run folders from this verification (runs/brief-a-20261011-000111, runs/brief-b-20261011-000252) deleted; runs/ is empty.
