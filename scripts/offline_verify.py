"""Offline check of the compiler on the spec fixtures (no network, no model).

usage: uv run python scripts/offline_verify.py [--spec FILE ...]

For every spec: load -> compile with tools/openapi.json -> lint -> self-checks, then one line:
nodes, lint errors/warnings, checks, failed, gaps (kinds of unstructured rules), degraded.
Exit code 1 only on lint errors, failed self-checks or a compile exception.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from builder import selfcheck  # noqa: E402
from builder.compiler import compile_workflow  # noqa: E402
from builder.lint import lint  # noqa: E402
from builder.spec import load_spec, spec_to_dict  # noqa: E402

FIXTURES: Path = REPO_ROOT / "tests" / "fixtures"
OPENAPI: Path = REPO_ROOT / "tools" / "openapi.json"
DEFAULT_SPECS: tuple[Path, ...] = tuple(FIXTURES / n for n in (
    "spec_brief_a.json", "spec_brief_ab.json",
    "spec_variant_1.json", "spec_variant_2.json", "spec_variant_3.json",
    "spec_rehearsal.json",
))
KIND_RE = re.compile(r"unstructured rule \(([a-z_]+)\)")


def verify(path: Path, openapi: dict[str, Any]) -> dict[str, Any]:
    """Compile and check one spec file. Never raises; a crash is reported in `error`."""
    row: dict[str, Any] = {"name": path.stem, "error": "", "nodes": 0, "lint_errors": 0, "lint_warnings": 0,
                           "checks": 0, "failed": 0, "gaps": [], "degraded": False}
    try:
        spec_dict = json.loads(path.read_text(encoding="utf-8"))
        spec = load_spec(spec_dict)
        workflow, report = compile_workflow(spec, openapi)
        findings = lint(workflow)
        result = selfcheck.run_selfchecks(workflow, spec_to_dict(spec), openapi, report.context)
    except Exception as e:  # noqa: BLE001 - reported per spec
        row["error"] = f"{type(e).__name__}: {e}"
        row["trace"] = traceback.format_exc()
        return row
    nodes = workflow.get("nodes")
    row["nodes"] = len(nodes) if isinstance(nodes, (dict, list)) else 0
    row["lint_errors"] = sum(1 for f in findings if f.severity == "error")
    row["lint_warnings"] = len(findings) - row["lint_errors"]
    row["checks"] = result.ran
    row["failed"] = len(result.failed)
    row["failed_names"] = [c.name for c in result.failed]
    row["gaps"] = sorted({m.group(1) for g in report.gaps for m in [KIND_RE.search(g)] if m})
    row["degraded"] = bool(report.degraded)
    return row


def format_row(row: dict[str, Any]) -> str:
    if row["error"]:
        return f"{row['name']}: COMPILE ERROR {row['error']}"
    gaps = ",".join(row["gaps"]) or "-"
    return (f"{row['name']}: nodes={row['nodes']} lint={row['lint_errors']}e/{row['lint_warnings']}w "
            f"checks={row['checks']} failed={row['failed']} gaps={gaps} degraded={'yes' if row['degraded'] else 'no'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="offline_verify.py", description="Compile, lint and self-check the spec fixtures offline.")
    ap.add_argument("--spec", action="append", default=[], type=Path, help="extra spec file to check (repeatable)")
    args = ap.parse_args(argv)
    openapi = json.loads(OPENAPI.read_text(encoding="utf-8"))
    paths = [p for p in DEFAULT_SPECS if p.is_file()] + list(args.spec)
    bad = 0
    for p in paths:
        row = verify(p, openapi)
        print(format_row(row))
        if row["error"] or row["lint_errors"] or row["failed"]:
            bad += 1
            if row.get("failed_names"):
                print("  failed checks: " + "; ".join(row["failed_names"][:10]))
    print(f"{len(paths)} spec(s), {bad} with errors")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
