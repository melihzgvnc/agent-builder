"""Build a phone agent from one or more briefs.

Usage:
    python build.py <brief.md> [<brief.md> ...] [--openapi PATH] [--slot NAME] [--dry] [--cases FILE] [--spec FILE] [--budget SECONDS]

Briefs are layered in the order given: each later brief builds on the earlier ones.
Each run writes only to runs/<slot>-<YYYYmmdd-HHMMSS>/ and prints that folder.
--budget is the total time budget in seconds (default 1500); a build always ends with workflow.json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# A run never writes into its own source tree, so no builder/__pycache__ either.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder.pipeline import TOTAL_BUDGET_S, make_slot, run_build  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; an invalid slot is an argparse error."""
    parser = argparse.ArgumentParser(
        prog="build.py",
        usage="%(prog)s <brief.md> [<brief.md> ...] [--openapi PATH] [--slot NAME] [--dry] [--cases FILE] [--spec FILE] [--budget SECONDS]",
        description="Build a phone agent from layered briefs (later briefs build on earlier ones).",
    )
    parser.add_argument("briefs", nargs="+", type=Path, metavar="<brief.md>")
    parser.add_argument(
        "--openapi",
        type=Path,
        default=ROOT / "tools" / "openapi.json",
        help="tool API description (default: tools/openapi.json next to build.py)",
    )
    parser.add_argument("--slot", default=None, help="slot name (default: stem of the last brief)")
    parser.add_argument("--dry", action="store_true", help="skip every stage that needs the network")
    parser.add_argument("--cases", type=Path, default=None, help="optional test cases file")
    parser.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="dev option: use this PolicySpec JSON instead of extraction",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=TOTAL_BUDGET_S,
        metavar="SECONDS",
        help=f"total time budget of the build in seconds (default: {TOTAL_BUDGET_S:g})",
    )
    args = parser.parse_args(argv)
    try:
        args.slot = make_slot(args.briefs, args.slot)
    except ValueError as e:
        parser.error(str(e))
    return args


_last_run_dir: list[Path] = []


def _print_run_dir(run_dir: Path) -> None:
    _last_run_dir.append(run_dir)
    print(f"run dir: {run_dir}", flush=True)


def _trace_is_degraded(run_dirs: list[Path]) -> bool:
    """True when the finished trace.log of the last run contains a DEGRADED: entry."""
    if not run_dirs:
        return False
    try:
        return "DEGRADED:" in (run_dirs[-1] / "trace.log").read_text(encoding="utf-8")
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    """Run one build and return its exit code."""
    args = parse_args(argv)
    code = run_build(
        root=ROOT,
        briefs=[Path(b) for b in args.briefs],
        openapi=Path(args.openapi),
        slot=args.slot,
        dry=args.dry,
        cases=Path(args.cases) if args.cases is not None else None,
        on_run_dir=_print_run_dir,
        spec_path=Path(args.spec) if args.spec is not None else None,
        budget_s=args.budget,
    )
    if _trace_is_degraded(_last_run_dir):
        print("WARNING: degraded build, see trace.log", file=sys.stderr, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
