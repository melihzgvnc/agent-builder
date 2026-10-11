"""Build a phone agent from one or more briefs.

Usage:
    python build.py <brief.md> [<brief.md> ...] [--openapi PATH] [--slot NAME] [--dry] [--cases FILE] [--spec FILE] [--budget SECONDS]
    python build.py --folder DIR [--server-url URL] [...]    (briefs, contract and customers are found by content)

Briefs are layered in the order given: each later brief builds on the earlier ones.
Each run writes only to runs/<slot>-<YYYYmmdd-HHMMSS>/ and prints that folder.
--budget is the total time budget in seconds (default 1500); a build always ends with workflow.json.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

# A run never writes into its own source tree, so no builder/__pycache__ either.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from builder import flow_mode, folder_input  # noqa: E402
from builder.inputs import apply_server_url, resolve_server_url  # noqa: E402
from builder.pipeline import TOTAL_BUDGET_S, make_slot, run_build  # noqa: E402
from builder.tee import capture_console  # noqa: E402
from builder.trace import format_run_summary  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; an invalid slot is an argparse error."""
    parser = argparse.ArgumentParser(
        prog="build.py",
        usage="%(prog)s [<brief.md> ...] [--folder DIR] [--openapi PATH] [--server-url URL] [--slot NAME] [--dry] [--cases FILE] [--spec FILE] [--budget SECONDS]",
        description="Build a phone agent from layered briefs (later briefs build on earlier ones).",
    )
    parser.add_argument("briefs", nargs="*", type=Path, metavar="<brief.md>")
    parser.add_argument("--folder", type=Path, default=None, help="input folder: briefs, contract and customers are found by content")
    parser.add_argument(
        "--openapi",
        type=Path,
        default=None,
        help="tool API description (default: the folder's contract, else tools/openapi.json next to build.py)",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        metavar="URL",
        help="replace servers[0].url of the tool API (default: BUILDER_SERVER_URL if set)",
    )
    parser.add_argument(
        "--mode",
        choices=flow_mode.MODES,
        default="auto",
        help="auto (default): the policy compiler when the folder's tool contract fits it, else the generic document-driven flow mode",
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
    if args.folder is None and len(args.briefs) == 1 and args.briefs[0].is_dir():
        args.folder = args.briefs.pop()
    args.folder_inputs = None
    if args.folder is not None:
        if not args.folder.is_dir():
            parser.error(f"--folder {args.folder} is not a directory")
        args.folder_inputs = folder_input.discover(args.folder)
        args.briefs = [*args.briefs, *args.folder_inputs.briefs]
    args.mode_resolved, args.mode_why = _resolve_mode(args)
    if args.mode_resolved == "flow":
        if args.folder is None:
            parser.error("--mode flow needs --folder")
        args.openapi = args.openapi or ROOT / "tools" / "openapi.json"
        args.server_url = resolve_server_url(args.server_url, os.environ)
        try:
            apply_server_url({}, args.server_url)
        except ValueError as e:
            parser.error(str(e))
        try:
            args.slot = make_slot(args.briefs or [args.folder.resolve()], args.slot)
        except ValueError as e:
            parser.error(str(e))
        return args
    if not args.briefs:
        parser.error("no briefs given (pass brief files or a --folder that contains one)")
    if args.openapi is None:
        found = args.folder_inputs.openapi_path if args.folder_inputs is not None else None
        args.openapi = found if found is not None else ROOT / "tools" / "openapi.json"
        if args.folder_inputs is not None and found is None:
            note = f"no tool API document found in {args.folder}; using {args.openapi}"
            args.folder_inputs.log.append(f"WARNING: {note}")
            print(f"WARNING: {note}", file=sys.stderr, flush=True)
    if args.cases is None and args.folder_inputs is not None:
        args.cases = args.folder_inputs.customers_path
    args.server_url = resolve_server_url(args.server_url, os.environ)
    try:
        apply_server_url({}, args.server_url)
    except ValueError as e:
        parser.error(str(e))
    try:
        args.slot = make_slot(args.briefs, args.slot)
    except ValueError as e:
        parser.error(str(e))
    return args


def _resolve_mode(args: argparse.Namespace) -> tuple[str, str]:
    """(mode, why) for this command line; loads the folder's contract only to check it fits the policy compiler."""
    doc = None
    fi = args.folder_inputs
    if args.mode == "auto" and fi is not None and fi.openapi_path is not None:
        try:
            from builder.inputs import load_openapi_doc

            doc = load_openapi_doc(Path(args.openapi) if args.openapi else fi.openapi_path)
        except (OSError, ValueError):
            doc = None
    return flow_mode.decide_mode(fi, args.mode, doc)


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
    t0 = time.monotonic()
    stack = contextlib.ExitStack()

    def on_run_dir(run_dir: Path) -> None:
        # capture the console from the moment the run folder exists; trace.log embeds it
        stack.enter_context(capture_console(run_dir / "console.log"))
        _print_run_dir(run_dir)

    with stack:
        code = _run(args, on_run_dir)
        if _last_run_dir:
            print("RUN SUMMARY", flush=True)
            for line in format_run_summary(
                _last_run_dir[-1], time.monotonic() - t0, code, "ok" if code == 0 else "degraded"
            ):
                print(f"  {line}", flush=True)
    return code


def _run(args: argparse.Namespace, on_run_dir: Callable[[Path], None]) -> int:
    if args.mode_resolved == "flow":
        from builder.flow_pipeline import run_flow_build

        code = run_flow_build(
            root=ROOT, folder=args.folder, slot=args.slot, dry=args.dry, server_url=args.server_url,
            mode_why=args.mode_why, budget_s=args.budget, on_run_dir=on_run_dir,
            reuse_dir=Path(args.spec).parent if args.spec is not None else None,
        )
        if _trace_is_degraded(_last_run_dir):
            print("WARNING: degraded build, see trace.log", file=sys.stderr, flush=True)
        return code
    code = run_build(
        root=ROOT,
        briefs=[Path(b) for b in args.briefs],
        openapi=Path(args.openapi),
        slot=args.slot,
        dry=args.dry,
        cases=Path(args.cases) if args.cases is not None else None,
        on_run_dir=on_run_dir,
        spec_path=Path(args.spec) if args.spec is not None else None,
        budget_s=args.budget,
        server_url=args.server_url,
        folder_inputs=args.folder_inputs,
    )
    if _trace_is_degraded(_last_run_dir):
        print("WARNING: degraded build, see trace.log", file=sys.stderr, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
