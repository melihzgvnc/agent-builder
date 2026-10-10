"""Build a phone agent from one or more briefs.

Usage:
    python build.py <brief.md> [<brief.md> ...] [--openapi PATH] [--slot NAME] [--dry] [--cases FILE]

Briefs are layered in the order given: each later brief builds on the earlier ones.
Each run writes only to runs/<slot>-<YYYYmmdd-HHMMSS>/ and prints that folder.
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

from builder.pipeline import make_slot, run_build  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; an invalid slot is an argparse error."""
    parser = argparse.ArgumentParser(
        prog="build.py",
        usage="%(prog)s <brief.md> [<brief.md> ...] [--openapi PATH] [--slot NAME] [--dry] [--cases FILE]",
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
    args = parser.parse_args(argv)
    try:
        args.slot = make_slot(args.briefs, args.slot)
    except ValueError as e:
        parser.error(str(e))
    return args


def _print_run_dir(run_dir: Path) -> None:
    print(f"run dir: {run_dir}", flush=True)


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
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
