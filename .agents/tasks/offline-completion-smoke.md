# Live smoke record (FEAT-003 step 8)

One live run, no retry needed (no HTTP error). Freya was not called (`--dry`). No key value appears here.

Command: `BUILDER_LLM_MODEL=anthropic/claude-haiku-5.5 uv run python build.py briefs/brief-a.md --dry` (key loaded from env.sh, not printed).

## Result

- Exit code 0, wall time 234 s (3 min 54 s), far below the 25 min guard (1500 s).
- Files present: `workflow.json`, `compile_report.json`, `trace.log` (plus `spec.json`, `trace.jsonl`, `extraction_notes.md`).
- `python -m builder.lint workflow.json`: 0 errors, 0 warnings.
- Extraction did NOT fall back (`fallback: False`, mode json, 1 repair). Spec valid, 45 verbatim rules, 35 warnings, 4 brief tokens not found in the spec (a time, a sample id, two sample names; these are example values, not rules).
- **`compile_report.json` `fallback_workflow` = true.** Compile of the extracted spec raised `CompileError: tool 'verify_identity' has no field for 'question'; check spec.tools.verify_identity.field_map`. The pipeline contained it: generic fallback workflow (2 nodes) written, lint clean, trace line `DEGRADED: FALLBACK WORKFLOW ...`, `build.py` printed the degraded warning, exit 0. This is the "always a workflow.json" invariant working, but the workflow is the last-resort one.
- `selfcheck`: no block in `compile_report.json`; `offline_checks` was skipped (fallback workflow, nothing to check). So selfcheck.failed was not evaluated on a live spec.

## Finding (not fixed here)

The extracted `spec.tools.verify_identity` had only a `purpose`; no `field_map` and no `allowed_values`. The API names the question parameter `field`, and the compiler requires the spec to map the semantic name `question` to it (the fixture spec has `field_map: {customer: customer_id, question: field, answer: answer}`). So a real extraction can still fail to compile when the model leaves out a tool's `field_map`. The spec also carries a warning that no tool API summary was provided, which suggests the extractor prompt input lacked the API field names for this tool. Candidate fixes for a later step: let the compiler derive an unambiguous field mapping from the tool API when `field_map` is missing, or make the extraction repair loop feed compile errors back. The same CompileError text appears for the other workflow's `spec_variant_1.json`.

## Stage timings (trace.jsonl)

| stage | status | seconds |
|---|---|---|
| load_inputs | ok | 0.00 |
| extract_spec | ok | 233.82 |
| validate_spec | ok | 0.01 |
| compile_workflow | ok (fallback workflow) | 0.01 |
| lint | ok | 0.00 |
| offline_checks | skipped (fallback workflow) | 0.00 |
| dry_run, save, read_back, export, smoke_test, online_test | skipped (--dry) | 0.00 |

Extraction is 99.9 % of the wall time and within its 540 s stage budget.

## Model calls (extraction_notes.md and trace.log)

Model `anthropic/claude-haiku-5.5`, 5 calls, tokens in=61767 out=60631 in total.

| purpose | seconds | in | out |
|---|---|---|---|
| extract_spec | 76.02 | 16611 | 20000 |
| extract_spec | 79.67 | 16157 | 20000 |
| extract_repair_1 | 58.45 | 15798 | 15797 |
| extract_coverage | 17.49 | 12642 | 4604 |
| extract_grounding | 2.15 | 559 | 230 |

Both extract_spec calls ended at exactly 20000 output tokens (likely the output cap; the trace does not say why there were two calls), followed by one repair call. That is why extraction took about 4 minutes. The run folder was deleted afterwards.
