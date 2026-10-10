# Phase 2b report: compiler core

## What was built
- `builder/hooks.py`, `builder/node_prompts.py`, `builder/compiler.py` (FEAT-001/002): `compile_workflow(spec, openapi) -> (workflow, CompileReport)`, import-free hooks, spec-derived prompts, N-product support switched only by spec flags.
- FEAT-003 (pipeline and CLI):
  - `builder/health.py`: `probe_chat` (one chat turn in a worker thread, chat/end in `finally`, never raises).
  - `builder/readback.py`: `diff_workflows` (stripped / changed / length, stored-only keys ignored, values cut to 60 chars, limit with "... N more").
  - `builder/freya_extra.py`: `get_workflow(workflow_id, client=None)`.
  - `builder/pipeline.py`: `BuildContext.spec_path/client/spec_obj`, `run_build(..., spec_path, client)`, `export` now runs before `smoke_test`, stages `extract_spec` (`--spec`), `compile_workflow`, `dry_run`, `save`, `read_back`, `export`, `smoke_test` implemented. Network stages stay skipped under `--dry`. State file is `<root>/.freya-agents.json`.
  - `build.py`: `--spec FILE` and usage string.

## Files
New: builder/hooks.py, node_prompts.py, compiler.py, health.py, readback.py; tests/hook_harness.py, test_hooks.py, test_node_prompts.py, test_compiler.py, test_health.py, test_readback.py, test_pipeline_compile.py.
Changed: build.py, builder/pipeline.py, builder/freya_extra.py, builder/engine.py, builder/spec.py, builder/spec_schema.json, tests/test_build_cli.py, test_engine.py, test_snippets.py, test_spec.py.
No commits were made. Protected files were not touched.

## Tests
- Full suite: 491 passed (baseline 253; after FEAT-002 477).
- FEAT-003 targeted run (health, readback, pipeline_compile, build_cli, trace, freya_extra): 26 passed.
- Both CLI dry runs exit 0; run dir holds workflow.json, compile_report.json, spec.json, trace.log, trace.jsonl. `builder.lint`: 0 error(s), 0 warning(s) for both (34 nodes for spec A, 26 for spec AB). Run folders deleted.

## Lint / warnings
- Lint: none on either fixture.
- Compile warning (both specs): "closing.aggregation.default is missing". Acceptable: neither fixture spec defines it, so the compiler uses persuasion.on_exhausted (else promise_rules.on_failure) as the fallback pair for a verified hang-up. Nothing is invented. It is traced as `compile: ...`.

## To verify in the first practice run
1. Same-turn transition after a tool result when the variable was written by a post-hook (`identity_state`, `promise_state`), not only by `response_mapping`. The identity transition also tests the flattened and mapped keys.
2. Post-hook `what_to_do_next` makes the model speak (actual recorded values and success sentence). Texts live in `hooks.py` constants.
3. Pre-hook overwrite of body fields on model-called tools (forced product id, clamped date and amount, blanked callback fields).
4. `is_tool_call` transitions: tool name is `transition_to_<exit code node>`; model speaks and moves in one message; gated exit and gated `finalize` route back to the source node.
5. `end_call` node with `spoken_message` on state-driven exits, and terminal function names as ended reasons.
6. Close pre-hook sees `p<k>_*`, `transfer_reason` and other vars written by hooks of model-called tools.
7. The blanked callback request still appears in the ledger call log; if the scorer counts it, switch to redirecting the URL or accept clamping.
8. Hooks without imports: if a sandbox forbids string slicing or `float()`, hooks fall back to unchanged requests (all wrapped).
9. Product list flattened keys appear for the account node with the node name as prefix; otherwise `products_state=unknown` and the call ends on the backend-failure path.
10. The non-dry smoke test (`probe_chat`) sends the placeholder customer id `smoke-test` and always calls `/api/v2/chat/end`; PLATFORM_NOTES says the lifecycle close tool runs on chat end, so each smoke test may fire `close_call` at the bank API with a bogus id and the unverified fallback outcome (a junk or rejected ledger record a scorer might count). Easy change: build the smoke variables from a real customer id taken from the spec or cases, or accept the extra record. No behaviour change made.
Also new in this step: the real `/api/v2/chat/message` response shape (probe expects `response` and `callId`), and that `save_workflow` returns `version` (taken with `.get`).

## Deviations
- From FEAT-002: extra `finalize_early` node and transfer tool on identity for pre-verification transfers; exit node names carry `_say`/`_gated` suffixes; field resolution fallbacks with warnings; `matches_intent` condition shape; optional roles dropped with a warning (see FEAT-002 findings).
- FEAT-003: `probe_chat` treats an empty `response` as unhealthy. The compile stage also writes `spec.json` via the existing validate stage. Compile warnings and gaps are traced as `compile: ...` / `compile gap: ...`.
- The `runs/` folder did not exist before and was removed after verification.
