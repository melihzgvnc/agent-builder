# final-prep report

Branch: `final-prep` (not pushed, not merged, nothing deployed).

## What was built
- Builder accepts a hidden-case folder (`--folder`) and a mock server override (`--server-url`).
- Cloudflare Worker mock of the bank services in `mock/` (`worker.js`, `gen_mock.py`, tests).
- Pluggable ledger source and customers-file carriers for the online test.
- Trace capture and submission bundling (`scripts/make_submission.py`).
- Contamination scan now covers `mock/`.

## How to use
1. Deploy the mock: follow `mock/DEPLOY.md`. Deploy before running the builder and keep it up until results are out.
2. Run the builder: `uv run python build.py --folder <unzipped folder> --server-url <worker url>`. Keep all stdout as the trace. Multiple runs are allowed; submit one run only.
3. Bundle: `uv run python scripts/make_submission.py` to export the workflow and trace for that run. Do not edit the agent after the run.
4. Commit the export and trace. `*.workflow.json` is gitignored, so use `git add -f <file>`.

## Verification (final run)
- `uv run pytest -q`: 882 passed.
- `uv run python scripts/check_contamination.py`: 0 strict hits, 0 allowlist problems (advisory words only).
- `git status`: source files clean. Only untracked `.agents/tasks/` planning notes remain; `runs/` and `env.sh` are not committed.

## Caveats
- Where to submit is not confirmed. The instructions say to commit the export with the trace; the target repo or platform is unverified.
- `*.workflow.json` and `runs/` are ignored, so force-add only the chosen run's artifacts.
- Never commit `env.sh` or secrets.
- Mock deployment needs a Cloudflare account and may cost money, depending on plan and usage.
