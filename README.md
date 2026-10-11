# agent-builder

Reads bank briefs (clean specs or raw material) plus a tool contract and builds, tests and publishes a Freya phone agent with no manual step.

```
briefs + contract  ->  extract (LLM) -> validate -> compile -> lint -> offline checks
                   ->  save -> read back -> export -> smoke test -> online test (self-repair)
```

The LLM only turns the briefs into a typed policy spec (`builder/spec_schema.json`). Everything after that is deterministic: dates, amounts, hooks and the workflow graph are generated from the spec, so nothing in `builder/` is specific to one brief.

## Run

```bash
cp env.example env.sh        # fill in FREYA_API_KEY, ANTHROPIC_API_KEY (model key), FREYA_BANK_TEAM_KEY
set -a && . ./env.sh && set +a
uv sync

# a folder with BRIEF.md, other documents, the tool contract and test customers
BUILDER_LLM_MODEL=anthropic/claude-sonnet-5.5 \
  uv run python build.py --folder <dir> --server-url <bank mock url>/v1

# or explicit files
uv run python build.py briefs/brief-a.md [briefs/brief-b.md] [--dry] [--spec spec.json]
```

Each run writes `runs/<slot>-<timestamp>/` with `workflow.json`, the exported `<name>.workflow.json`, `spec.json`, `compile_report.json`, `trace.log` (everything printed, git commit and source hash included) and `trace.jsonl`. A build always ends with a saved workflow, even when a stage fails (the failure is logged and a degraded flag is set).

Bundle one run for submission: `uv run python scripts/make_submission.py <run dir>`.

## Layout

| Path | What |
| --- | --- |
| `build.py`, `builder/` | the builder (pipeline, extractor, compiler, hooks, online test) |
| `mock/` | data-driven Cloudflare Worker mock of the bank services, see `mock/DEPLOY.md` |
| `scripts/` | contamination check, submission bundle, rehearsal and diff helpers |
| `tests/` | unit tests and fixtures (`uv run pytest -q`) |
| `freya.py`, `tools/`, `public-test-set.json` | kit files from the organizers, used as given |
| `docs/` | organizers' guidelines (`kit-README.md`, API and workflow format) |
| `briefs/`, `rehearsal/`, `tests/fixtures/` | practice briefs and fixtures, test data only |
| `builder/PLATFORM_NOTES.md` | what was verified about the Freya platform |
| `.agents/tasks/` | development logs and reports |

## Checks

```bash
uv run pytest -q
uv run python scripts/check_contamination.py   # no brief value may appear in builder/, build.py, scripts/, mock/
```
