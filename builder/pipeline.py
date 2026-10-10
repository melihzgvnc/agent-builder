"""Staged build pipeline.

Every stage runs through run_stage(), which contains crashes: a failing stage is logged and the
run continues with the next stage. Network stages are skipped under --dry. Inputs come only from
the arguments of run_build(); output goes only to <runs>/<slot>-<YYYYmmdd-HHMMSS>/.
"""

from __future__ import annotations

import json
import re
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from builder import health, readback
from builder.compiler import compile_workflow
from builder.freya_extra import get_workflow
from builder.lint import lint
from builder.spec import load_spec, validate_spec
from builder.trace import Trace

STAGE_ORDER: tuple[str, ...] = (
    "load_inputs",
    "extract_spec",
    "validate_spec",
    "compile_workflow",
    "lint",
    "dry_run",
    "save",
    "read_back",
    "export",
    "smoke_test",
    "online_test",
)

NETWORK_STAGES: frozenset[str] = frozenset(
    {"dry_run", "save", "read_back", "smoke_test", "export", "online_test"}
)

REQUIRES: dict[str, tuple[str, ...]] = {
    "load_inputs": (),
    "extract_spec": ("brief_texts", "openapi_doc"),
    "validate_spec": ("spec",),
    "compile_workflow": ("spec", "openapi_doc"),
    "lint": ("workflow",),
    "dry_run": ("workflow",),
    "save": ("workflow",),
    "read_back": ("workflow",),
    "smoke_test": ("workflow",),
    "export": ("workflow",),
    "online_test": ("workflow",),
}

SLOT_PATTERN: re.Pattern[str] = re.compile(r"[A-Za-z0-9._-]+")

StageFn = Callable[["BuildContext"], str]


@dataclass
class BuildContext:
    """Everything a stage may read or produce during one run."""

    root: Path
    briefs: list[Path]
    openapi_path: Path
    slot: str
    dry: bool
    cases: Path | None
    run_dir: Path
    trace: Trace
    spec_path: Path | None = None
    client: Any = None
    spec_obj: Any = None
    brief_texts: list[str] = field(default_factory=list)
    openapi_doc: dict[str, Any] | None = None
    spec: dict[str, Any] | None = None
    workflow: dict[str, Any] | None = None
    results: dict[str, object] = field(default_factory=dict)


class StageSkipped(Exception):
    """Raised by a stage that has nothing to do; logged as 'skipped: <message>'."""


class NotImplementedStage(StageSkipped):
    """Raised by stages that are not implemented yet."""

    def __init__(self, message: str = "not implemented") -> None:
        super().__init__(message)


def stage_load_inputs(ctx: BuildContext) -> str:
    """Read every brief (in the given layer order) and parse the tool API document."""
    ctx.brief_texts = [Path(b).read_text(encoding="utf-8") for b in ctx.briefs]
    doc = json.loads(Path(ctx.openapi_path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("openapi document is not a JSON object")
    ctx.openapi_doc = doc
    paths = doc.get("paths")
    tools = [str(k).strip("/") for k in paths] if isinstance(paths, dict) else []
    ctx.results["openapi_tools"] = tools
    return f"read {len(ctx.brief_texts)} brief(s); openapi tools={tools}"


def _freya(ctx: BuildContext) -> Any:
    """The Freya client: the injected one (tests) or the freya module next to build.py."""
    if ctx.client is not None:
        return ctx.client
    import freya

    return freya


def _saved(ctx: BuildContext) -> dict[str, Any]:
    info = ctx.results.get("saved")
    if not isinstance(info, dict) or not info.get("workflow_id"):
        raise StageSkipped("no saved workflow")
    return info


def stage_extract_spec(ctx: BuildContext) -> str:
    """Dev option: read a ready PolicySpec from --spec. Without it extraction is not available."""
    if ctx.spec_path is None:
        raise NotImplementedStage()
    data = json.loads(Path(ctx.spec_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("spec file is not a JSON object")
    ctx.spec = data
    return f"spec read from {ctx.spec_path} (dev option, extractor not run)"


def stage_validate_spec(ctx: BuildContext) -> str:
    """Validate ctx.spec against the PolicySpec schema; write run_dir/spec.json when valid."""
    if ctx.spec is None:
        raise StageSkipped("no spec: extract_spec not implemented")
    errors = validate_spec(ctx.spec)
    if errors:
        for err in errors:
            ctx.trace.warn(f"spec: {err}")
        raise RuntimeError(f"spec invalid: {len(errors)} errors")
    (ctx.run_dir / "spec.json").write_text(
        json.dumps(ctx.spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return "spec valid"


def stage_compile_workflow(ctx: BuildContext) -> str:
    """Compile the spec into a workflow; write workflow.json and compile_report.json."""
    assert ctx.spec is not None and ctx.openapi_doc is not None
    ctx.spec_obj = load_spec(ctx.spec)
    workflow, report = compile_workflow(ctx.spec_obj, ctx.openapi_doc)
    ctx.workflow = workflow
    (ctx.run_dir / "workflow.json").write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (ctx.run_dir / "compile_report.json").write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    for w in report.warnings:
        ctx.trace.warn(f"compile: {w}")
    for g in report.gaps:
        ctx.trace.warn(f"compile gap: {g}")
    nodes = workflow.get("nodes")
    count = len(nodes) if isinstance(nodes, (dict, list)) else 0
    return f"{count} nodes, {len(report.warnings)} warning(s), {len(report.gaps)} gap(s)"


def stage_lint(ctx: BuildContext) -> str:
    """Lint the compiled workflow; findings go to the trace and never block the build."""
    if ctx.workflow is None:
        raise StageSkipped("no workflow: compile_workflow not implemented")
    findings = lint(ctx.workflow)
    for f in findings:
        if f.severity == "error":
            ctx.trace.error(f"lint: {f.format()}")
        else:
            ctx.trace.warn(f"lint: {f.format()}")
    errors = sum(1 for f in findings if f.severity == "error")
    ctx.results["lint"] = [f.format() for f in findings]
    return f"{errors} error(s), {len(findings) - errors} warning(s)"


def stage_dry_run(ctx: BuildContext) -> str:
    """Ask the platform to validate the workflow without creating it."""
    result = _freya(ctx).validate_workflow(ctx.slot, ctx.workflow)
    keys = sorted(result) if isinstance(result, dict) else type(result).__name__
    return f"platform validation answered: {keys}"


def stage_save(ctx: BuildContext) -> str:
    """Create or update the workflow in this slot and publish it."""
    info = _freya(ctx).save_workflow(
        ctx.slot, ctx.workflow, state_file=str(ctx.root / ".freya-agents.json")
    )
    ctx.results["saved"] = info
    ctx.trace.log_artifact(
        "saved", str(info["workflow_id"]), str(info.get("agent_id")), version=info.get("version")
    )
    return f"workflow_id={info['workflow_id']}"


def stage_read_back(ctx: BuildContext) -> str:
    """Read the stored workflow and report what the platform changed."""
    info = _saved(ctx)
    stored = get_workflow(str(info["workflow_id"]), client=_freya(ctx))
    stored_wf = stored.get("workflow", stored) if isinstance(stored, dict) else {}
    diffs = readback.diff_workflows(ctx.workflow or {}, stored_wf if isinstance(stored_wf, dict) else {})
    for line in diffs:
        ctx.trace.warn(f"read_back: {line}")
    return f"{len(diffs)} difference(s)"


def stage_smoke_test(ctx: BuildContext) -> str:
    """One chat turn against the saved workflow; an unhealthy model backend is not a build failure."""
    info = _saved(ctx)
    variables: dict[str, str] = {}
    cv = getattr(ctx.spec_obj, "call_variables", None)
    if cv is not None:
        names = [cv.customer_id, cv.first_name, cv.last_name, cv.title, *[e.name for e in cv.extra]]
        for name in names:
            if name:
                variables[name] = "smoke-test"
        variables[cv.now] = datetime.now().isoformat(timespec="minutes")
    res = health.probe_chat(str(info["workflow_id"]), variables, client=_freya(ctx))
    if not res["ok"]:
        ctx.trace.warn(f"model backend unhealthy: {res['error']}")
        ctx.trace.log_test("smoke", ctx.slot, "model backend unhealthy, skipped")
        raise StageSkipped("model backend unhealthy")
    ctx.trace.log_test("smoke", ctx.slot, "ok", details=f"{res['seconds']:.1f}s")
    return f"chat answered in {res['seconds']:.1f}s"


def stage_export(ctx: BuildContext) -> str:
    """Write the workflow as stored on the platform into the run folder."""
    info = _saved(ctx)
    path = _freya(ctx).export_workflow(
        str(info["workflow_id"]), str(ctx.run_dir / f"{ctx.slot}.workflow.json")
    )
    return f"exported to {path}"


def stage_online_test(ctx: BuildContext) -> str:
    raise NotImplementedStage()


DEFAULT_STAGES: dict[str, StageFn] = {
    "load_inputs": stage_load_inputs,
    "extract_spec": stage_extract_spec,
    "validate_spec": stage_validate_spec,
    "compile_workflow": stage_compile_workflow,
    "lint": stage_lint,
    "dry_run": stage_dry_run,
    "save": stage_save,
    "read_back": stage_read_back,
    "smoke_test": stage_smoke_test,
    "export": stage_export,
    "online_test": stage_online_test,
}


def describe_input(ctx: BuildContext, name: str) -> str:
    """Short description of what a stage was working on, for crash messages."""
    if name == "extract_spec" and ctx.spec_path is not None:
        return f"spec={ctx.spec_path}"
    if name in ("load_inputs", "extract_spec"):
        return f"briefs={[str(b) for b in ctx.briefs]} openapi={ctx.openapi_path}"
    if name in ("read_back", "export", "smoke_test"):
        saved = ctx.results.get("saved")
        wid = saved.get("workflow_id") if isinstance(saved, dict) else None
        return f"workflow_id={wid}"
    if name in ("validate_spec", "compile_workflow"):
        if isinstance(ctx.spec, dict):
            return f"spec keys={sorted(ctx.spec)}"
        return "spec=None"
    if isinstance(ctx.workflow, dict):
        nodes = ctx.workflow.get("nodes")
        count = len(nodes) if isinstance(nodes, (dict, list)) else 0
        return f"workflow nodes={count}"
    return "workflow=None"


def _missing_requirement(ctx: BuildContext, name: str) -> str | None:
    for attr in REQUIRES.get(name, ()):
        value = getattr(ctx, attr, None)
        if value is None or (isinstance(value, (list, dict, str)) and not value):
            return attr
    return None


def run_stage(name: str, fn: StageFn, ctx: BuildContext) -> str:
    """Run one stage with crash containment. Returns 'ok', 'skipped' or 'failed'; never raises."""
    t0 = time.monotonic()
    if ctx.dry and name in NETWORK_STAGES:
        ctx.trace.log_stage(name, "skipped", "--dry", time.monotonic() - t0)
        return "skipped"
    missing = _missing_requirement(ctx, name)
    if missing is not None:
        ctx.trace.log_stage(
            name,
            "skipped",
            f"missing {missing} (upstream stage failed or skipped)",
            time.monotonic() - t0,
        )
        return "skipped"
    try:
        detail = fn(ctx)
    except StageSkipped as e:
        ctx.trace.log_stage(name, "skipped", str(e) or "skipped", time.monotonic() - t0)
        return "skipped"
    except Exception as e:  # noqa: BLE001 - every stage crash is contained by design
        msg = (
            f"stage {name} crashed on input {describe_input(ctx, name)}: "
            f"{type(e).__name__}: {e}"
        )
        ctx.trace.error(msg, traceback_text=traceback.format_exc())
        ctx.trace.log_stage(name, "failed", msg, time.monotonic() - t0)
        return "failed"
    ctx.trace.log_stage(name, "ok", str(detail) if detail else "done", time.monotonic() - t0)
    return "ok"


def make_slot(briefs: list[Path], slot: str | None) -> str:
    """Slot name: the given one (validated) or the stem of the LAST brief (sanitized)."""
    if slot is not None:
        if not SLOT_PATTERN.fullmatch(slot):
            raise ValueError(f"invalid slot {slot!r}: use only letters, digits, '.', '_' and '-'")
        return slot
    if not briefs:
        raise ValueError("at least one brief is required")
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(briefs[-1]).stem)
    if not stem.strip("._-"):
        raise ValueError(f"cannot derive a slot from {briefs[-1]}; pass --slot")
    return stem


def _new_run_dir(runs: Path, slot: str) -> Path:
    runs.mkdir(parents=True, exist_ok=True)
    base = f"{slot}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    n = 1
    while True:
        candidate = runs / (base if n == 1 else f"{base}-{n}")
        try:
            candidate.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            n += 1
            continue
        return candidate


def run_build(
    root: Path,
    briefs: list[Path],
    openapi: Path,
    slot: str,
    dry: bool,
    cases: Path | None,
    runs_dir: Path | None = None,
    stages: dict[str, StageFn] | None = None,
    on_run_dir: Callable[[Path], None] | None = None,
    spec_path: Path | None = None,
    client: Any = None,
) -> int:
    """Run every stage in STAGE_ORDER for one build. Returns the process exit code.

    `stages` overrides stage functions by name (tests inject stages this way); `on_run_dir` is
    called with the new run folder as soon as it exists.
    """
    root = Path(root)
    run_dir = _new_run_dir(Path(runs_dir) if runs_dir is not None else root / "runs", slot)
    if on_run_dir is not None:
        on_run_dir(run_dir)
    trace = Trace(run_dir, root)
    exit_code = 0
    try:
        trace.set_inputs(
            briefs,
            openapi,
            cases,
            slot,
            {
                "dry": dry,
                "layer order": " > ".join(str(b) for b in briefs),
                "spec": str(spec_path) if spec_path is not None else "(extraction)",
            },
        )
        ctx = BuildContext(
            root=root,
            briefs=list(briefs),
            openapi_path=openapi,
            slot=slot,
            dry=dry,
            cases=cases,
            run_dir=run_dir,
            trace=trace,
            spec_path=Path(spec_path) if spec_path is not None else None,
            client=client,
        )
        table: dict[str, StageFn] = {**DEFAULT_STAGES, **(stages or {})}
        for name in STAGE_ORDER:
            if run_stage(name, table[name], ctx) == "failed":
                exit_code = 1
    except Exception as e:  # noqa: BLE001 - the trace must always be finished
        trace.error(
            f"build crashed outside a stage: {type(e).__name__}: {e}",
            traceback_text=traceback.format_exc(),
        )
        exit_code = 1
    finally:
        exit_code = trace.finish(exit_code)
    return exit_code
