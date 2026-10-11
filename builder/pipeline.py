"""Staged build pipeline.

Every stage runs through run_stage(), which contains crashes: a failing stage is logged and the
run continues with the next stage. Network stages are skipped under --dry. Inputs come only from
the arguments of run_build(); output goes only to <runs>/<slot>-<YYYYmmdd-HHMMSS>/.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from builder import health, online_cases, online_repair, online_runner, readback, selfcheck
from builder.compiler import compile_fallback, compile_workflow, emergency_workflow
from builder.extract import stage_extract
from builder.freya_extra import get_workflow
from builder.lint import lint
from builder.spec import load_spec, spec_to_dict, validate_spec
from builder.trace import Trace

STAGE_ORDER: tuple[str, ...] = (
    "load_inputs",
    "extract_spec",
    "validate_spec",
    "compile_workflow",
    "lint",
    "offline_checks",
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
    "compile_workflow": ("openapi_doc",),
    "lint": ("workflow",),
    "offline_checks": ("workflow", "spec"),
    "dry_run": ("workflow",),
    "save": ("workflow",),
    "read_back": ("workflow",),
    "smoke_test": ("workflow",),
    "export": ("workflow",),
    "online_test": ("workflow",),
}

# Total wall-clock budget of one build (seconds): 5 minutes under the 30 minute rule.
TOTAL_BUDGET_S: float = 1500.0

# Per-stage time limits (seconds). Only extract_spec and the network stages are cut off by a
# thread time-out; CPU-only stages run inline and these numbers are targets there.
STAGE_BUDGETS: dict[str, float] = {
    "load_inputs": 10.0,
    "extract_spec": 540.0,
    "validate_spec": 10.0,
    "compile_workflow": 60.0,
    "lint": 30.0,
    "offline_checks": 120.0,
    "dry_run": 120.0,
    "save": 120.0,
    "read_back": 60.0,
    "export": 60.0,
    "smoke_test": 180.0,
    "online_test": 600.0,
}

# Stages that still run when the total budget is gone (they are fast and local).
ESSENTIAL_STAGES: frozenset[str] = frozenset(
    {"load_inputs", "validate_spec", "compile_workflow", "lint", "offline_checks"}
)

TIMED_STAGES: frozenset[str] = frozenset({"extract_spec"}) | NETWORK_STAGES
MIN_EXTRACT_S: float = 60.0  # below this much remaining time extraction is skipped
EXTRACT_RESERVE_S: float = 240.0  # time kept for everything after extraction
EXTRACT_MAX_S: float = 480.0
EXTRACT_MIN_S: float = 30.0

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
    deadline: float | None = None  # time.monotonic() value at which the total budget ends
    extract_budget_s: float | None = None
    bank: Any = None  # bank mock adapter for the online test (tests inject a fake)


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


def _degrade(ctx: BuildContext, message: str) -> None:
    """Record a loud DEGRADED warning in the trace and in ctx.results['degraded']."""
    ctx.trace.warn(f"DEGRADED: {message}")
    ctx.results.setdefault("degraded", [])
    degraded = ctx.results["degraded"]
    if isinstance(degraded, list):
        degraded.append(message)


def stage_spec_file(ctx: BuildContext) -> str:
    """Dev option: read a ready PolicySpec from --spec instead of running the extractor."""
    if ctx.spec_path is None:
        raise StageSkipped("no --spec file given")
    data = json.loads(Path(ctx.spec_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("spec file is not a JSON object")
    ctx.spec = data
    return f"spec read from {ctx.spec_path} (dev option, extractor not run)"


def stage_validate_spec(ctx: BuildContext) -> str:
    """Validate ctx.spec against the PolicySpec schema; write run_dir/spec.json when valid."""
    if ctx.spec is None:
        raise StageSkipped("no spec: extract_spec produced none")
    errors = validate_spec(ctx.spec)
    if errors:
        ctx.results["spec_invalid"] = True
        for err in errors:
            ctx.trace.warn(f"spec: {err}")
        raise RuntimeError(f"spec invalid: {len(errors)} errors")
    (ctx.run_dir / "spec.json").write_text(
        json.dumps(ctx.spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return "spec valid"


def _read_sources(
    briefs: list[Path], texts: list[str] | None = None
) -> list[tuple[str, str]]:
    """(file name, text) per brief; uses loaded texts when complete, else reads each file safely."""
    if texts is not None and len(texts) == len(briefs):
        return [(Path(b).name, t) for b, t in zip(briefs, texts)]
    out: list[tuple[str, str]] = []
    for b in briefs:
        try:
            out.append((Path(b).name, Path(b).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _compile_with_fallback(ctx: BuildContext, reason: str) -> str:
    """Write the generic fallback workflow and a report that says so."""
    assert ctx.openapi_doc is not None
    sources = _read_sources(ctx.briefs, ctx.brief_texts)
    workflow, report = compile_fallback(sources, ctx.openapi_doc)
    ctx.workflow = workflow
    ctx.results["fallback_workflow"] = reason
    _write_json(ctx.run_dir / "workflow.json", workflow)
    _write_json(
        ctx.run_dir / "compile_report.json",
        {**report.to_dict(), "fallback_workflow": True, "fallback_reason": reason},
    )
    _degrade(ctx, f"FALLBACK WORKFLOW built from the raw briefs and the tool API: {reason}")
    nodes = workflow.get("nodes")
    count = len(nodes) if isinstance(nodes, (dict, list)) else 0
    return f"FALLBACK WORKFLOW ({reason}); {count} nodes"


def stage_compile_workflow(ctx: BuildContext) -> str:
    """Compile the spec into a workflow; write workflow.json and compile_report.json.

    Without a usable spec (none, invalid, or a compile error) a generic fallback workflow is
    written instead, so a build always ends with workflow.json.
    """
    assert ctx.openapi_doc is not None
    if ctx.spec is None:
        return _compile_with_fallback(ctx, "no spec (extraction failed or was skipped)")
    if ctx.results.get("spec_invalid"):
        return _compile_with_fallback(ctx, "the spec did not pass validation")
    try:
        ctx.spec_obj = load_spec(ctx.spec)
        workflow, report = compile_workflow(ctx.spec_obj, ctx.openapi_doc)
    except Exception as e:  # noqa: BLE001 - any compile problem degrades to the fallback workflow
        ctx.spec_obj = None
        reason = f"compile failed: {type(e).__name__}: {e}"
        ctx.trace.error(reason, traceback_text=traceback.format_exc())
        return _compile_with_fallback(ctx, reason)
    ctx.workflow = workflow
    ctx.results["compile_context"] = report.context
    _write_json(ctx.run_dir / "workflow.json", workflow)
    _write_json(
        ctx.run_dir / "compile_report.json",
        {**report.to_dict(), "fallback_workflow": False, "fallback_reason": ""},
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


def _selfcheck_block(result: selfcheck.SelfCheckResult) -> dict[str, Any]:
    """The compile_report.json['selfcheck'] entry."""
    return {
        "checks": result.ran,
        "failed": [c.format() for c in result.failed],
        "warnings": [c.format() for c in result.warnings],
        "repairs": list(result.repairs),
        "skipped": list(result.skipped),
    }


def _try_repair(
    ctx: BuildContext, spec_dict: dict[str, Any], result: selfcheck.SelfCheckResult
) -> selfcheck.SelfCheckResult:
    """One deterministic repair pass; the repaired workflow is kept only if it is clearly better.

    Accepted only when the recompiled workflow has no lint error and strictly fewer failed checks;
    otherwise the original workflow stays and a loud warning says why.
    """
    assert ctx.openapi_doc is not None
    repaired = selfcheck.repair_spec(spec_dict, result)
    if repaired is None:
        ctx.trace.warn("selfcheck: no deterministic repair applies to the failed checks; workflow kept as compiled")
        return result
    fixed, names = repaired
    label = ", ".join(names)
    try:
        spec_obj = load_spec(fixed)
        workflow, report = compile_workflow(spec_obj, ctx.openapi_doc)
        lint_errors = [f for f in lint(workflow) if f.severity == "error"]
        again = selfcheck.run_selfchecks(workflow, spec_to_dict(spec_obj), ctx.openapi_doc, report.context)
    except Exception as e:  # noqa: BLE001 - a failed repair must never lose the original workflow
        ctx.trace.warn(f"selfcheck: repair [{label}] could not be built ({type(e).__name__}: {e}); original workflow kept")
        return result
    if lint_errors or len(again.failed) >= len(result.failed):
        why = f"{len(lint_errors)} lint error(s)" if lint_errors else f"failed checks {len(result.failed)} -> {len(again.failed)}"
        ctx.trace.warn(f"selfcheck: repair [{label}] rejected ({why}); original workflow kept")
        return result
    ctx.workflow = workflow
    ctx.spec_obj = spec_obj
    ctx.results["compile_context"] = report.context
    _write_json(ctx.run_dir / "workflow.json", workflow)
    _write_json(
        ctx.run_dir / "compile_report.json",
        {**report.to_dict(), "fallback_workflow": False, "fallback_reason": ""},
    )
    again.repairs = list(names)
    ctx.trace.warn(
        f"REPAIRED: [{label}] changed the workflow (failed checks {len(result.failed)} -> {len(again.failed)}); "
        "spec.json is unchanged, see compile_report.json"
    )
    return again


def stage_offline_checks(ctx: BuildContext) -> str:
    """Run the spec-derived scenario checks on the compiled workflow (no network, no model).

    Failed checks are reported (trace errors, compile_report.json['selfcheck']) and never raise;
    at most one deterministic repair is tried (see selfcheck.REPAIRS).
    """
    if ctx.results.get("fallback_workflow"):
        raise StageSkipped("fallback workflow: there is no spec to check it against")
    context = ctx.results.get("compile_context")
    if ctx.workflow is None or ctx.spec is None or ctx.openapi_doc is None or not isinstance(context, dict) or not context:
        raise StageSkipped("no compile context (the workflow was not compiled from a spec)")
    spec_dict = spec_to_dict(ctx.spec_obj) if ctx.spec_obj is not None else ctx.spec
    result = selfcheck.run_selfchecks(ctx.workflow, spec_dict, ctx.openapi_doc, context)
    if result.failed:
        result = _try_repair(ctx, spec_dict, result)
    for c in result.failed:
        ctx.trace.error(f"selfcheck FAILED: {c.format()}")
    for c in result.warnings:
        ctx.trace.warn(f"selfcheck: {c.format()}")
    ctx.results["selfcheck"] = _selfcheck_block(result)
    path = ctx.run_dir / "compile_report.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    if isinstance(report, dict):
        report["selfcheck"] = _selfcheck_block(result)
        _write_json(path, report)
    return (
        f"{result.ran} checks, {len(result.failed)} failed, {len(result.warnings)} warning(s), "
        f"{len(result.repairs)} repair(s), {len(result.skipped)} skipped"
    )


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
        ctx.results["backend_unhealthy"] = True
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


def _cases_file(ctx: BuildContext) -> Path | None:
    """The carrier accounts for the online test: --cases, else <root>/public-test-set.json."""
    for p in (ctx.cases, ctx.root / "public-test-set.json"):
        if p is not None and Path(p).is_file():
            return Path(p)
    return None


def _online_recompile(ctx: BuildContext) -> Callable[[dict[str, Any]], dict[str, Any] | None]:
    """Gate for a spec-level repair: recompile a copy of the spec, accept only a clean, not worse workflow."""
    sc = ctx.results.get("selfcheck")
    before = len(sc.get("failed", [])) if isinstance(sc, dict) else 0

    def recompile(fixed: dict[str, Any]) -> dict[str, Any] | None:
        try:
            spec_obj = load_spec(fixed)
            workflow, report = compile_workflow(spec_obj, ctx.openapi_doc or {})
            if any(f.severity == "error" for f in lint(workflow)):
                return None
            again = selfcheck.run_selfchecks(workflow, spec_to_dict(spec_obj), ctx.openapi_doc or {}, report.context)
            return workflow if len(again.failed) <= before else None
        except Exception:  # noqa: BLE001 - a repair that cannot be built is simply not used
            return None

    return recompile


def stage_online_test(ctx: BuildContext) -> str:
    """Generate cases from the spec, run them against the saved agent, repair what is deterministic.

    Every network call goes through the budget guard; the live agent is never left worse than the
    saved workflow (a repair is kept only when strictly better, else the best version is re-saved).
    """
    t0 = time.monotonic()
    if ctx.results.get("fallback_workflow"):
        raise StageSkipped("fallback workflow: there is no spec to derive test cases from")
    if ctx.spec is None:
        raise StageSkipped("no spec")
    info = _saved(ctx)
    if ctx.results.get("backend_unhealthy"):
        raise StageSkipped("model backend unhealthy")
    path = _cases_file(ctx)
    carriers = online_cases.load_carriers(path) if path is not None else []
    if not carriers:
        _degrade(ctx, "online_test skipped: no carrier accounts (give --cases or put public-test-set.json in the project root)")
        raise StageSkipped("no carrier accounts")
    scoring = online_runner.load_scoring(require_key=ctx.bank is None)
    if scoring is None:
        _degrade(ctx, "online_test skipped: scoring tools or FREYA_BANK_TEAM_KEY missing")
        raise StageSkipped("no scoring tools or bank key")
    bank = ctx.bank if ctx.bank is not None else online_runner.HttpBank(scoring)
    spec = spec_to_dict(ctx.spec_obj) if ctx.spec_obj is not None else ctx.spec
    cases, skipped = online_cases.generate_cases(spec, carriers)
    if not cases:
        _degrade(ctx, "online_test skipped: the spec yields no test case (" + "; ".join(skipped)[:300] + ")")
        raise StageSkipped("no test cases")
    assert ctx.workflow is not None
    baseline = ctx.workflow
    client = _freya(ctx)
    budget = online_runner.Budget(STAGE_BUDGETS["online_test"], ctx.deadline)
    personas = ctx.root / ".personas.json"
    agent_id = str(info.get("agent_id"))
    saves: list[dict[str, Any]] = []

    def run_batch(batch: list[online_cases.GenCase], reserve: float) -> list[online_runner.CaseResult]:
        return online_runner.run_batch(client, bank, scoring, batch, agent_id, budget, reserve, personas)

    def save(workflow: dict[str, Any]) -> Any:
        if not budget.can_mutate():
            raise RuntimeError("time budget exhausted before the save")
        saved = client.save_workflow(ctx.slot, workflow, state_file=str(ctx.root / ".freya-agents.json"))
        saves.append(saved)
        return saved

    out = online_repair.run_loop(
        cases=cases, baseline=baseline, spec=spec, budget=budget, run_batch=run_batch, save=save,
        recompile=_online_recompile(ctx), log=ctx.trace.warn,
    )
    for msg in out.degraded:
        _degrade(ctx, f"online_test: {msg}")
    for cid, cls in out.classes.items():
        if cls.klass in ("hook", "closing", "expectation"):
            ctx.trace.warn(f"online_test {cid} [{cls.klass}]: {cls.evidence}")
    if saves:
        ctx.results["saved"] = {**info, **saves[-1]}
    if out.live_workflow is not baseline:
        ctx.workflow = out.live_workflow
        _write_json(ctx.run_dir / "workflow.json", out.live_workflow)
        try:
            _freya(ctx).export_workflow(str(info["workflow_id"]), str(ctx.run_dir / f"{ctx.slot}.workflow.json"))
        except Exception as e:  # noqa: BLE001 - the saved agent is what counts; the export is a copy
            ctx.trace.warn(f"online_test: re-export failed: {type(e).__name__}: {e}")
    scored = [r for i, r in out.results.items() if not r.infra and not (i in out.classes and out.classes[i].klass == "expectation")]
    passed = sum(1 for r in scored if r.passed or r.judge_noise)
    report = {
        "slot": ctx.slot, "carriers_file": str(path), "agent_id": agent_id,
        "cases": [{"id": c.id, "kind": c.kind, "carrier": c.carrier_id, "title": c.title, "script": c.script,
                   "expected": c.expected} for c in cases],
        "skipped_kinds": skipped,
        "rounds": out.rounds, "repairs_tried": out.repairs, "decision": out.decision,
        "classes": {i: {"class": c.klass, "evidence": c.evidence} for i, c in out.classes.items()},
        "degraded": out.degraded, "passed": passed, "scored": len(scored),
        "excluded": len(out.results) - len(scored),
        "seconds": round(time.monotonic() - t0, 1), "stage_budget_s": STAGE_BUDGETS["online_test"],
    }
    _write_json(ctx.run_dir / "online_test.json", report)
    ctx.trace.log_test("online", ctx.slot, f"{out.decision.get('kept')} workflow live", passed, len(scored),
                       details=f"{len(out.repairs)} repair(s) tried, {report['excluded']} case(s) excluded")
    return (f"{passed}/{len(scored)} cases pass, {len(out.repairs)} repair(s) tried, kept {out.decision.get('kept')}, "
            f"{report['excluded']} excluded, {len(out.degraded)} degraded note(s)")


DEFAULT_STAGES: dict[str, StageFn] = {
    "load_inputs": stage_load_inputs,
    "extract_spec": stage_extract,
    "validate_spec": stage_validate_spec,
    "compile_workflow": stage_compile_workflow,
    "lint": stage_lint,
    "offline_checks": stage_offline_checks,
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
    if name in ("read_back", "export", "smoke_test", "online_test"):
        saved = ctx.results.get("saved")
        wid = saved.get("workflow_id") if isinstance(saved, dict) else None
        return f"workflow_id={wid}"
    if name in ("validate_spec", "compile_workflow"):
        if isinstance(ctx.spec, dict):
            return f"spec keys={sorted(ctx.spec)}"
        return "spec=None"
    if name == "offline_checks":
        nodes = ctx.workflow.get("nodes") if isinstance(ctx.workflow, dict) else None
        count = len(nodes) if isinstance(nodes, (dict, list)) else 0
        return f"workflow nodes={count} spec={'set' if isinstance(ctx.spec, dict) else 'None'}"
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


class StageTimeout(Exception):
    """A stage ran longer than its time limit and was abandoned."""


def _call_with_timeout(fn: StageFn, ctx: BuildContext, timeout_s: float) -> str:
    """Run fn on a shadow copy of ctx in a daemon thread; merge the shadow back only on completion.

    On time-out the thread is abandoned and the shadow discarded, so a late finish cannot
    change the real context. Exceptions of the stage are re-raised here.
    """
    shadow = copy.copy(ctx)
    shadow.results = dict(ctx.results)
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["detail"] = fn(shadow)
        except BaseException as e:  # noqa: BLE001 - handed to the caller thread
            outcome["error"] = e

    thread = threading.Thread(target=target, name="stage-worker", daemon=True)
    thread.start()
    thread.join(max(0.0, timeout_s))
    if thread.is_alive():
        raise StageTimeout(f"{timeout_s:g} s")
    for f in dataclasses.fields(ctx):
        if f.name != "results":
            setattr(ctx, f.name, getattr(shadow, f.name))
    ctx.results.update(shadow.results)
    error = outcome.get("error")
    if error is not None:
        raise error
    return str(outcome.get("detail") or "")


def run_stage(name: str, fn: StageFn, ctx: BuildContext, timeout_s: float | None = None) -> str:
    """Run one stage with crash containment. Returns 'ok', 'skipped' or 'failed'; never raises.

    With timeout_s the stage runs in a worker thread and is abandoned when it takes longer.
    """
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
        detail = fn(ctx) if timeout_s is None else _call_with_timeout(fn, ctx, timeout_s)
    except StageSkipped as e:
        ctx.trace.log_stage(name, "skipped", str(e) or "skipped", time.monotonic() - t0)
        return "skipped"
    except StageTimeout as e:
        msg = f"stage {name} exceeded {e}, abandoned (input {describe_input(ctx, name)})"
        ctx.trace.error(msg)
        ctx.trace.log_stage(name, "failed", msg, time.monotonic() - t0)
        return "failed"
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


def ensure_workflow_on_disk(
    run_dir: Path,
    briefs: list[Path],
    openapi_path: Path,
    trace: Trace,
    ctx: BuildContext | None = None,
) -> str:
    """Guarantee run_dir/workflow.json. Returns 'present', 'fallback' or 'emergency'.

    Called after all stages. When the file is missing it is built from the briefs and the tool API
    (each read guarded); if even that fails, the static emergency workflow is written. Never raises.
    """
    run_dir = Path(run_dir)
    path = run_dir / "workflow.json"
    try:
        if path.is_file():
            return "present"
        trace.error("workflow.json missing after all stages; writing a fallback workflow")
        texts = ctx.brief_texts if ctx is not None else None
        sources = _read_sources([Path(b) for b in briefs], texts)
        doc: dict[str, Any] = {}
        try:
            loaded = json.loads(Path(openapi_path).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                doc = loaded
        except (OSError, ValueError) as e:
            trace.error(f"ensure_workflow_on_disk: tool API unreadable: {type(e).__name__}: {e}")
        kind = "fallback"
        report: dict[str, Any] | None = None
        try:
            workflow, rep = compile_fallback(sources, doc)
            report = {**rep.to_dict(), "fallback_workflow": True, "fallback_reason": "workflow.json missing after all stages"}
        except Exception as e:  # noqa: BLE001
            trace.error(
                f"ensure_workflow_on_disk: fallback compile failed: {type(e).__name__}: {e}",
                traceback_text=traceback.format_exc(),
            )
            workflow = emergency_workflow()
            kind = "emergency"
        _write_json(path, workflow)
        if report is not None and not (run_dir / "compile_report.json").exists():
            _write_json(run_dir / "compile_report.json", report)
        trace.warn(f"DEGRADED: workflow.json was missing; wrote the {kind} workflow")
        return kind
    except Exception as e:  # noqa: BLE001 - last line of defence
        trace.error(f"ensure_workflow_on_disk failed: {type(e).__name__}: {e}")
        try:
            _write_json(path, emergency_workflow())
            return "emergency"
        except Exception as e2:  # noqa: BLE001
            trace.error(f"emergency workflow could not be written: {type(e2).__name__}: {e2}")
            return "missing"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
    budget_s: float = TOTAL_BUDGET_S,
    stage_timeouts: dict[str, float] | None = None,
    bank: Any = None,
) -> int:
    """Run every stage in STAGE_ORDER for one build. Returns the process exit code.

    `stages` overrides stage functions by name (tests inject stages this way); `on_run_dir` is
    called with the new run folder as soon as it exists. `budget_s` is the total time budget;
    `stage_timeouts` overrides STAGE_BUDGETS per stage (seconds). Whatever happens, the run
    folder ends with a workflow.json.
    """
    root = Path(root)
    run_dir = _new_run_dir(Path(runs_dir) if runs_dir is not None else root / "runs", slot)
    if on_run_dir is not None:
        on_run_dir(run_dir)
    trace = Trace(run_dir, root)
    exit_code = 0
    ctx: BuildContext | None = None
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
                "time budget": f"{budget_s:g} s",
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
            bank=bank,
            deadline=time.monotonic() + budget_s,
        )
        overrides: dict[str, StageFn] = {"extract_spec": stage_spec_file} if spec_path is not None else {}
        table: dict[str, StageFn] = {**DEFAULT_STAGES, **overrides, **(stages or {})}
        limits = {**STAGE_BUDGETS, **(stage_timeouts or {})}
        for name in STAGE_ORDER:
            remaining = (ctx.deadline or 0.0) - time.monotonic()
            runs_for_real = not (dry and name in NETWORK_STAGES)
            if runs_for_real and name not in ESSENTIAL_STAGES and remaining <= 0:
                msg = f"skipped: total time budget exhausted ({budget_s:g} s)"
                trace.warn(f"DEGRADED: stage {name} {msg}")
                trace.log_stage(name, "skipped", msg, 0.0)
                continue
            if runs_for_real and table[name] is stage_extract:
                if remaining < MIN_EXTRACT_S:
                    msg = f"skipped: total time budget too low for extraction ({max(remaining, 0.0):.0f} s left)"
                    trace.warn(f"DEGRADED: stage {name} {msg}")
                    trace.log_stage(name, "skipped", msg, 0.0)
                    continue
                ctx.extract_budget_s = _clamp(
                    min(EXTRACT_MAX_S, remaining - EXTRACT_RESERVE_S), EXTRACT_MIN_S, EXTRACT_MAX_S
                )
            timeout_s: float | None = None
            if name in TIMED_STAGES or (stage_timeouts is not None and name in stage_timeouts):
                timeout_s = min(limits.get(name, remaining), max(remaining, 0.0))
            status = run_stage(name, table[name], ctx, timeout_s)
            if status == "failed":
                exit_code = 1
            elif status == "ok" and name == "extract_spec":
                extraction = ctx.results.get("extraction")
                if isinstance(extraction, dict) and extraction.get("fallback"):
                    _degrade(ctx, "extraction fell back to the minimal spec; the workflow will not follow the briefs' policy")
    except Exception as e:  # noqa: BLE001 - the trace must always be finished
        trace.error(
            f"build crashed outside a stage: {type(e).__name__}: {e}",
            traceback_text=traceback.format_exc(),
        )
        exit_code = 1
    finally:
        ensure_workflow_on_disk(run_dir, briefs, openapi, trace, ctx)
        exit_code = trace.finish(exit_code)
    return exit_code
