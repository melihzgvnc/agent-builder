"""Rehearse a full offline build and check it against the time budgets.

usage: uv run python scripts/rehearse.py <brief.md> [<brief.md> ...] [--spec FILE] [--openapi PATH]
           [--budget 1500] [--keep] [--require-extraction]

Runs `build.py <briefs> --dry --budget N` as a subprocess (hard kill after 30 minutes), reads the
stage events of the run's trace.jsonl and prints a table: stage, status, seconds, budget, verdict.
Exit 0 only when every stage is within pipeline.STAGE_BUDGETS, the whole build is within
pipeline.TOTAL_BUDGET_S (25 minutes), workflow.json exists, compile_report.json lint has no ERROR
line and the build exited 0. An extraction fallback is only a WARN unless --require-extraction.
The run folder is deleted unless --keep is given.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

sys.dont_write_bytecode = True

ROOT: Path = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder.pipeline import STAGE_BUDGETS, STAGE_ORDER, TOTAL_BUDGET_S  # noqa: E402

KILL_AFTER_S: float = 1800.0
RUN_DIR_RE = re.compile(r"^run dir:\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class StageRow:
    """One stage result from trace.jsonl."""

    name: str
    status: str
    seconds: float
    detail: str = ""


@dataclass
class Evaluation:
    """Verdict of a rehearsal: failures make it fail, warnings are printed only."""

    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing failed."""
        return not self.failures


def parse_stages(events: Iterable[Mapping[str, Any]]) -> list[StageRow]:
    """Stage rows from trace.jsonl events, in the order the stages were recorded."""
    rows: list[StageRow] = []
    for ev in events:
        if ev.get("kind") != "stage":
            continue
        rows.append(
            StageRow(
                name=str(ev.get("name", "?")),
                status=str(ev.get("status", "?")),
                seconds=float(ev.get("duration_s") or 0.0),
                detail=str(ev.get("detail", "")),
            )
        )
    return rows


def read_events(path: Path) -> list[dict[str, Any]]:
    """Read a trace.jsonl file; unreadable lines are skipped."""
    events: list[dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return events
    for line in text.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def evaluate(
    rows: Sequence[StageRow],
    total_s: float,
    *,
    workflow_exists: bool = True,
    lint_errors: Sequence[str] = (),
    killed: bool = False,
    exit_code: int = 0,
    fallback: bool = False,
    require_extraction: bool = False,
    budgets: Mapping[str, float] | None = None,
    total_budget: float | None = None,
) -> Evaluation:
    """Judge a rehearsal against the stage budgets and the total budget."""
    budgets = STAGE_BUDGETS if budgets is None else budgets
    total_budget = TOTAL_BUDGET_S if total_budget is None else total_budget
    ev = Evaluation()
    if killed:
        ev.failures.append(f"build killed after {KILL_AFTER_S:g} s wall clock")
    for row in rows:
        limit = budgets.get(row.name)
        if limit is not None and row.seconds > limit:
            ev.failures.append(f"stage {row.name} took {row.seconds:.1f} s, budget {limit:g} s")
    if total_s > total_budget:
        ev.failures.append(f"total {total_s:.1f} s is over the {total_budget:g} s budget")
    if not workflow_exists:
        ev.failures.append("workflow.json is missing")
    for line in lint_errors:
        ev.failures.append(f"lint: {line}")
    if exit_code != 0 and not killed:
        ev.failures.append(f"build exited with code {exit_code}")
    if fallback:
        msg = "extraction fell back to the minimal spec (degraded build)"
        (ev.failures if require_extraction else ev.warnings).append(msg)
    return ev


def render_table(
    rows: Sequence[StageRow],
    total_s: float,
    budgets: Mapping[str, float] | None = None,
    total_budget: float | None = None,
) -> str:
    """Aligned table: stage, status, seconds, budget, verdict, plus a total row."""
    budgets = STAGE_BUDGETS if budgets is None else budgets
    total_budget = TOTAL_BUDGET_S if total_budget is None else total_budget
    order = {name: i for i, name in enumerate(STAGE_ORDER)}
    body: list[tuple[str, str, str, str, str]] = []
    for row in sorted(rows, key=lambda r: order.get(r.name, len(order))):
        limit = budgets.get(row.name)
        verdict = "OVER" if limit is not None and row.seconds > limit else "OK"
        body.append(
            (row.name, row.status, f"{row.seconds:.2f}", f"{limit:g}" if limit is not None else "-", verdict)
        )
    body.append(("TOTAL", "", f"{total_s:.2f}", f"{total_budget:g}", "OVER" if total_s > total_budget else "OK"))
    header = ("stage", "status", "seconds", "budget", "verdict")
    widths = [max(len(r[i]) for r in [header, *body]) for i in range(5)]

    def fmt(r: tuple[str, ...]) -> str:
        cells = [r[0].ljust(widths[0]), r[1].ljust(widths[1])]
        cells += [r[2].rjust(widths[2]), r[3].rjust(widths[3]), r[4].ljust(widths[4])]
        return "  ".join(cells).rstrip()

    rule = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(header), rule, *map(fmt, body[:-1]), rule, fmt(body[-1])])


def _run_build(cmd: list[str]) -> tuple[str, int, bool]:
    """Run build.py; returns (stdout, exit code, killed)."""
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=KILL_AFTER_S, check=False
        )
        return proc.stdout, proc.returncode, False
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return out, -1, True


def _lint_errors(run_dir: Path) -> list[str]:
    try:
        report = json.loads((run_dir / "compile_report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(x) for x in report.get("lint", []) if str(x).lstrip().upper().startswith("ERROR")]


def _fallback(run_dir: Path) -> bool:
    try:
        return "EXTRACTION FELL BACK" in (run_dir / "trace.log").read_text(encoding="utf-8")
    except OSError:
        return False


def build_parser() -> argparse.ArgumentParser:
    """Command line of the rehearsal."""
    p = argparse.ArgumentParser(prog="rehearse.py", description="Rehearse a --dry build under the time budgets.")
    p.add_argument("briefs", nargs="+", type=Path, metavar="<brief.md>")
    p.add_argument("--spec", type=Path, default=None, help="use this PolicySpec JSON instead of extraction")
    p.add_argument("--openapi", type=Path, default=None, help="tool API description")
    p.add_argument("--budget", type=float, default=TOTAL_BUDGET_S, help="total build budget passed to build.py")
    p.add_argument("--keep", action="store_true", help="keep the run folder")
    p.add_argument("--require-extraction", action="store_true", help="fail when extraction fell back")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the rehearsal; 0 when within budget, 1 otherwise."""
    args = build_parser().parse_args(argv)
    cmd = [sys.executable, str(ROOT / "build.py"), *map(str, args.briefs), "--dry", "--budget", f"{args.budget:g}"]
    if args.spec is not None:
        cmd += ["--spec", str(args.spec)]
    if args.openapi is not None:
        cmd += ["--openapi", str(args.openapi)]
    started = time.monotonic()
    stdout, code, killed = _run_build(cmd)
    total_s = time.monotonic() - started
    m = RUN_DIR_RE.search(stdout)
    run_dir = Path(m.group(1)) if m else None
    rows = parse_stages(read_events(run_dir / "trace.jsonl")) if run_dir else []
    ev = evaluate(
        rows,
        total_s,
        workflow_exists=bool(run_dir and (run_dir / "workflow.json").is_file()),
        lint_errors=_lint_errors(run_dir) if run_dir else [],
        killed=killed,
        exit_code=code,
        fallback=bool(run_dir and _fallback(run_dir)),
        require_extraction=args.require_extraction,
    )
    if run_dir is None:
        ev.failures.append("build printed no run dir")
    print(f"run dir: {run_dir}")
    print(render_table(rows, total_s))
    for w in ev.warnings:
        print(f"WARN: {w}")
    for f in ev.failures:
        print(f"FAIL: {f}")
    print("REHEARSAL " + ("OK" if ev.ok else "FAILED"))
    runs = (ROOT / "runs").resolve()
    if run_dir is not None and not args.keep and run_dir.is_dir() and runs in run_dir.resolve().parents:
        shutil.rmtree(run_dir)
    return 0 if ev.ok else 1


if __name__ == "__main__":
    sys.exit(main())
