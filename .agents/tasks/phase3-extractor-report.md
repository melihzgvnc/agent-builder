# Phase 3 extractor report

## Files (new only, nothing committed)
builder/llm.py, builder/extract.py, builder/extract_prompts/{system,sections,coverage,repair,grounding}.md,
tests/test_llm.py, tests/test_extract.py, scripts/extract_live.py, scripts/diff_spec.py.

## Tests
`uv run pytest -q`: 391 passed (includes the compiler workflow's tests at that moment; contamination test green).
No key value found in any file I wrote (grep -rIlF over builder, tests, scripts, results, .agents: 0 files for all three env keys).

## Live round trip (OpenRouter, key from env)
- Auth works; base_url https://openrouter.ai/api with Bearer auth works.
- Sonnet slug `anthropic/claude-sonnet-5.5`: accepted, but a FORCED tool call fails: HTTP 400 `tool_choice: type "tool" and "any" are not supported for this model` (provider Bedrock). `tool_choice: auto` with one tool and an instruction to call it works and returns structured input.
- Haiku slug `anthropic/claude-haiku-5.5`: accepted; forced tool call and auto both work.
- Decision: the extractor never forces the tool. Default mode `auto` (one tool, "call it once" instruction); on a 400 or unusable reply it drops to JSON-object mode (schema in the system prompt, fenced/prose-wrapped output parsed robustly). Code default model is still the sonnet slug (not changed, per instruction).

## Live extractions (results/, gitignored)
| run | model | calls | in tokens | out tokens | seconds | mode | repairs | verbatim_rules |
|---|---|---|---|---|---|---|---|---|
| A | haiku | 3 | 32775 | 24407 | 94.0 | auto | 0 | 49 |
| A+B | haiku | 5 | 67126 | 67064 | 263.8 | json (fell back from auto) | 1 | 55 |
| A | sonnet | 2 | 31622 | 14106 | 76.7 | auto | 0 | 43 |

Haiku A+B used 264 s of the 480 s budget and produced 2x the output tokens of sonnet A: slower and not cheaper per useful result. No full A+B run with sonnet was done (limit).

### Diff summaries (vs fixtures; fixtures are test data only)
Haiku A (grounding bug present in that run, since fixed): 9/14 facts identical. Calendar, identity, persuasion, floors, amounts same. Diffs: bands only differ by explicit `roll: none` (cosmetic); outcome table reasons `declined`/`restructuring` were rewritten to `refused`/`hardship` by a grounding bug (close_call.reason has no enum, the code borrowed the add_note enum). Fixed in `_allowed_for` and covered by a test. Intent names differ (free-form names). Two mandated sentences (the debt question and the confirmation question) were in the fixture but not listed in `mandated_sentences` (they are in templates/verbatim).

Haiku A+B: 11/14 identical. Bands, identity, persuasion, outcome table, multi-product flags and note reasons all same. Diffs: `floor_fields` keys were display names ("car loan") instead of the API product type values (prompt wording fixed once afterwards); intent names differ; same two mandated sentences missing. `account_fields.product_type` was a concept name not in the API; flagged as an unmatched warning.

Sonnet A (after the one prompt fix): 12/14 identical. Only intent names differ (free-form, and one extra intent for changing a promise), plus one extra mandated sentence. 13 warnings, all about facts the brief does not state. This is the better model for the real build.

## Decisions
- Forced tool choice not used (see above). `auto` default, JSON fallback automatic.
- Fallback spec: weekend_days [], one placeholder band (0..0, 0 days, 0 attempts), outcome table from the close tool's outcome enum (else one `unknown` row), call variables `unknown`; all text as global verbatim rules, loud warning.
- Grounding only maps values to enums that exist; fields without an enum (like close reason) are left alone. Only the exact tool's enum is used when the operation exists.
- Mechanical normalisation only: nulls stripped, stringified sub-objects parsed, `schema_version` and `meta.sources` filled when absent.
- Coverage LLM additions are discarded if they would make the spec invalid.

## Proposed schema changes (not made)
- `amounts.floor_fields` / `debt_statement.product_names` keys should be validated against the API product type enum (only the prompt enforces it now).
- `mandated_sentences` vs `opening.confirmation_question` / `debt_statement` templates duplicate the same sentences; consider a rule on which place the compiler reads.
- Optional `intents[].kind` (fixed enum such as ai_question, scam_question, human_request) would make intent names comparable and let the compiler find them without name guessing.

## Known weaknesses
- Number/date coverage check is noisy: example data and overridden numbers show up as "missing" (4 tokens left after the LLM pass in each run). Harmless, listed in extraction_notes.md.
- Account field names and product type keys depend on the model reading the API summary; only account fields are checked deterministically, product type keys are not.
- Haiku is slow on layered briefs and may fall to JSON mode; use sonnet for builds.
- Output ceiling is 20000 tokens per call; a very large layered spec could truncate (then repair, then fallback).

## What the orchestrator must wire into pipeline.py
- In `DEFAULT_STAGES`, set `"extract_spec": builder.extract.stage_extract` (same `(ctx) -> str` signature; it reads `ctx.briefs`, `ctx.brief_texts`, `ctx.openapi_doc`; sets `ctx.spec` and `ctx.results["extraction"]`; writes `run_dir/spec.json` and `run_dir/extraction_notes.md`; logs warnings and model calls to the trace). `validate_spec` rewrites the same spec.json afterwards, which is harmless.
- Remove or replace the `stage_extract_spec` stub (`NotImplementedStage`).
- Env: `ANTHROPIC_API_KEY` (OpenRouter key) is picked up automatically; `BUILDER_LLM_MODEL` overrides the model. Total extraction budget is 480 s (`DEFAULT_BUDGET_S`).
- Tests can monkeypatch `builder.extract.make_llm` to return a `FakeLLM`.
- `--dry` does not skip extraction (it is not in `NETWORK_STAGES`); add it there if dry runs must avoid the LLM.
