# Offline completion report

## Built per task
1. Extractor wiring + fallback workflow + time guard: the LLM extractor is wired into `builder/pipeline.py`. If extraction or compile fails, a generic 2-node fallback workflow is written, so `workflow.json` always exists. A total time guard (25 min, 1500 s) caps the build; extraction has its own stage budget (540 s).
2. Offline self-checks: `builder/selfcheck.py` and `builder/sandbox.py` run the engine and sandboxed hooks in-build. Results go into `compile_report.json`. They are skipped for the fallback workflow.
3. Contamination script: `scripts/check_contamination.py` with the allowlist `scripts/contamination_allow.json`.
4. Rehearsal: `scripts/rehearse.py`, plus README documentation.
5. Live smoke: one haiku run with `--dry`. Exit 0, 234 s, lint clean, but the compile fell back (see [offline-completion-smoke.md](offline-completion-smoke.md)).

## Files
Changed: `README.md`, `build.py`, `builder/compiler.py`, `builder/extract.py`, `builder/extract_prompts/sections.md`, `builder/node_prompts.py`, `builder/pipeline.py`, `tests/conftest.py`.
Created: `builder/sandbox.py`, `builder/selfcheck.py`, `scripts/check_contamination.py`, `scripts/contamination_allow.json`, `scripts/rehearse.py`, `tests/test_check_contamination.py`, `tests/test_compiler_fallback.py`, `tests/test_pipeline_extract.py`, `tests/test_pipeline_selfcheck.py`, `tests/test_rehearse.py`, `tests/test_sandbox.py`, `tests/test_selfcheck.py`, plus task and review files under `.agents/tasks/`.
Task 9 files were not touched.

## Results
- `uv run pytest -q`: 577 passed, 1 skipped.
- `uv run python scripts/check_contamination.py`: exit 0 (30 files scanned, 0 strict hits, 0 allowlist problems; advisory words only).
- `runs/` is empty. No stray temp files; `.pyc` files are gitignored.

## User constraints
- A build always ends with `workflow.json`.
- No network and no Freya calls (the smoke used `--dry`).
- No git commit.

## Design decisions
- The pipeline catches compile errors and degrades to the fallback workflow instead of failing. It prints a degraded warning and records `fallback_workflow: true` in `compile_report.json`.
- Offline checks are skipped on the fallback workflow because there is nothing meaningful to check.
- Hooks run in a sandbox so spec-supplied code cannot touch the host.

## Known risks / verify first when Freya is back
- The fallback workflow has not been tested on the platform.
- Hooks and offline checks were only exercised in the sandbox and in unit tests, not on a live spec (`selfcheck.failed` was never evaluated live).
- The live smoke hit the fallback: the extracted `spec.tools.verify_identity` had no `field_map`, so the compile raised `CompileError`. Candidate fixes: derive the field mapping from the tool API in the compiler, or feed compile errors into the extraction repair loop.
- Extraction took about 4 minutes. Two extract calls hit the 20000 output-token cap.

## git status --short
```
 M README.md
 M build.py
 M builder/compiler.py
 M builder/extract.py
 M builder/extract_prompts/sections.md
 M builder/node_prompts.py
 M builder/pipeline.py
 M tests/conftest.py
?? .agents/tasks/2026-10-10-194752-review.md
?? .agents/tasks/2026-10-10-202852-review.md
?? .agents/tasks/offline-completion-plan.md
?? .agents/tasks/offline-completion-review.json
?? .agents/tasks/offline-completion-smoke.md
?? .agents/tasks/phase2a-report.md
?? .agents/tasks/phase2b-prep-report.md
?? .agents/tasks/plan.md
?? .agents/tasks/review.json
?? .agents/tasks/task-9-variants/plan.md
?? .agents/tasks/task-9-variants/review.json
?? .agents/tasks/task-9-variants/review.md
?? .agents/tasks/task-offline-completion/
?? .agents/tasks/task-phase1-foundations/
?? .agents/tasks/task-phase2b-compiler/
?? builder/sandbox.py
?? builder/selfcheck.py
?? scripts/check_contamination.py
?? scripts/contamination_allow.json
?? scripts/rehearse.py
?? tests/test_check_contamination.py
?? tests/test_compiler_fallback.py
?? tests/test_pipeline_extract.py
?? tests/test_pipeline_selfcheck.py
?? tests/test_rehearse.py
?? tests/test_sandbox.py
?? tests/test_selfcheck.py
```
(Snapshot taken before this report was written; the report file itself is also untracked.)
