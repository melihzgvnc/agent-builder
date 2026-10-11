"""Bundle one build run for submission.

usage: uv run python scripts/make_submission.py RUN_DIR [--out DIR]

Copies the exported workflow (<slot>.workflow.json), trace.log, trace.jsonl, compile_report.json
and spec.json of a run into DIR (default: submission/<run folder name>/) and writes MANIFEST.md
with UTC time, ids and a sha256 per file. If the run folder has no export and FREYA_API_KEY is
set, the workflow is exported from the platform first. No git operation is performed.

Exit codes: 0 ok, 2 the workflow export or trace.log is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.dont_write_bytecode = True

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OPTIONAL_FILES: tuple[str, ...] = ("trace.jsonl", "compile_report.json", "spec.json")

Exporter = Callable[[str, str], object]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_export(run_dir: Path) -> Path | None:
    with os.scandir(run_dir) as it:
        names = sorted(e.name for e in it if e.is_file() and e.name.endswith(".workflow.json"))
    return run_dir / names[0] if names else None


def _read_events(run_dir: Path) -> list[dict]:
    events: list[dict] = []
    try:
        text = (run_dir / "trace.jsonl").read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            events.append(rec)
    return events


def _ids(events: list[dict]) -> dict[str, str]:
    """workflow_id, agent_id and version of the last saved or exported artifact; the slot."""
    out = {"workflow_id": "-", "agent_id": "-", "version": "-", "slot": "-"}
    for ev in events:
        if ev.get("kind") == "artifact":
            for key in ("workflow_id", "agent_id", "version"):
                if ev.get(key) not in (None, "", "None"):
                    out[key] = str(ev[key])
        elif ev.get("kind") == "inputs" and ev.get("slot"):
            out["slot"] = str(ev["slot"])
    return out


def _default_exporter() -> Exporter | None:
    if not os.environ.get("FREYA_API_KEY"):
        return None
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import freya  # lazy: needs the key and the network

    return freya.export_workflow


def make_submission(run_dir: Path, out: Path | None = None, export: Exporter | None = None) -> int:
    """Build the bundle. Returns the exit code; messages go to stderr/stdout, never a traceback."""
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        print(f"ERROR: {run_dir} is not a directory", file=sys.stderr)
        return 2
    out = Path(out) if out is not None else REPO_ROOT / "submission" / run_dir.name
    events = _read_events(run_dir)
    ids = _ids(events)
    workflow_file = _find_export(run_dir)
    exported_now = False
    if workflow_file is None:
        if export is not None and ids["workflow_id"] != "-":
            slot = ids["slot"] if ids["slot"] != "-" else run_dir.name
            target = run_dir / f"{slot}.workflow.json"
            try:
                export(ids["workflow_id"], str(target))
            except Exception as e:  # noqa: BLE001 - reported, not a traceback
                print(f"ERROR: workflow export failed: {type(e).__name__}", file=sys.stderr)
                return 2
            workflow_file = target if target.is_file() else None
            exported_now = workflow_file is not None
        if workflow_file is None:
            print(
                f"ERROR: no <slot>.workflow.json in {run_dir} and it could not be exported "
                "(set FREYA_API_KEY and make sure the run saved a workflow, or re-run without --dry)",
                file=sys.stderr,
            )
            return 2
    trace_log = run_dir / "trace.log"
    if not trace_log.is_file():
        print(f"ERROR: {trace_log} is missing", file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for src in (workflow_file, trace_log):
        shutil.copy2(src, out / src.name)
        copied.append(out / src.name)
    for name in OPTIONAL_FILES:
        src = run_dir / name
        if src.is_file():
            shutil.copy2(src, out / name)
            copied.append(out / name)
        else:
            print(f"WARNING: optional file {name} not found in {run_dir}", file=sys.stderr)

    lines = [
        "# Submission bundle",
        "",
        f"- created (UTC): {datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds')}Z",
        f"- run dir: {run_dir}",
        f"- workflow_id: {ids['workflow_id']}",
        f"- agent_id: {ids['agent_id']}",
        f"- version: {ids['version']}",
        f"- export: {workflow_file.name}" + (" (exported from the platform now)" if exported_now else ""),
        "",
        "## Files (sha256)",
        "",
        *(f"- {p.name}  {_sha256(p)}" for p in copied),
        "",
        "## Committing",
        "",
        "`*.workflow.json` is ignored by .gitignore, so add it explicitly:",
        "",
        f"    git add -f {out / workflow_file.name}",
        f"    git add {' '.join(str(p) for p in copied if p.name != workflow_file.name)}",
        "",
        "This script performs no git operation.",
        "",
    ]
    (out / "MANIFEST.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"submission bundle: {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="make_submission.py", description="Bundle one run for submission.")
    p.add_argument("run_dir", type=Path)
    p.add_argument("--out", type=Path, default=None, help="bundle folder (default: submission/<run name>)")
    args = p.parse_args(argv)
    try:
        exporter = _default_exporter()
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: cannot load the export client: {type(e).__name__}", file=sys.stderr)
        exporter = None
    return make_submission(args.run_dir, args.out, exporter)


if __name__ == "__main__":
    sys.exit(main())
