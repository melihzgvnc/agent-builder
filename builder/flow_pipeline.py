"""Generic document-driven build: documents -> tools (stage A) -> policy (stage B) -> workflow -> save -> test."""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from builder import flow_assemble, flow_docs, flow_llm, flow_policy, flow_tools, folder_input, health, readback
from builder.flow_online import Skipped
from builder.freya_extra import get_workflow
from builder.llm import LLMClient, LLMConfigError
from builder.pipeline import TOTAL_BUDGET_S, _new_run_dir
from builder.trace import Trace

ONLINE_MAX_S = 600.0


class Ctx:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)
        self.docset: flow_docs.DocSet | None = None
        self.plan: flow_tools.ToolPlan | None = None
        self.policy: dict[str, Any] | None = None
        self.workflow: dict[str, Any] | None = None
        self.saved: dict[str, Any] | None = None
        self.llm: Any = None
        self.report: dict[str, Any] = {}
        self.backend_unhealthy = False


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _freya(ctx: Ctx) -> Any:
    if ctx.client is not None:
        return ctx.client
    import freya
    return freya


def degrade(ctx: Ctx, msg: str) -> None:
    ctx.trace.warn(f"DEGRADED: {msg}")
    print(f"WARNING: DEGRADED: {msg}", file=sys.stderr, flush=True)


def run_stage(ctx: Ctx, name: str, fn: Callable[[Ctx], str]) -> str:
    t0 = time.monotonic()
    try:
        detail = fn(ctx)
        ctx.trace.log_stage(name, "ok", detail, time.monotonic() - t0)
        print(f"[{name}] {detail}", flush=True)
        return "ok"
    except Skipped as e:
        ctx.trace.log_stage(name, "skipped", str(e), time.monotonic() - t0)
        print(f"[{name}] skipped: {e}", flush=True)
        return "skipped"
    except Exception as e:  # noqa: BLE001
        ctx.trace.error(f"stage {name} failed: {type(e).__name__}: {e}", traceback_text=traceback.format_exc())
        ctx.trace.log_stage(name, "failed", f"{type(e).__name__}: {str(e)[:300]}", time.monotonic() - t0)
        print(f"[{name}] FAILED: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr, flush=True)
        return "failed"


def stage_select_mode(ctx: Ctx) -> str:
    ctx.trace.note(f"mode: flow because {ctx.mode_why}")
    return f"flow ({ctx.mode_why})"


def stage_load_docs(ctx: Ctx) -> str:
    ctx.docset = flow_docs.collect(ctx.folder)
    for n in ctx.docset.notes:
        ctx.trace.note(f"docs: {n}")
    ctx.trace.note("docs order (oldest first): " + " > ".join(f"{d.rel}[{d.date or '?'}]" for d in ctx.docset.docs))
    _write(ctx.run_dir / "docs_manifest.json", [
        {"rel": d.rel, "date": d.date, "priority": d.priority, "chars": len(d.text), "kind": d.kind} for d in ctx.docset.docs])
    return f"{len(ctx.docset.docs)} documents, {sum(len(d.text) for d in ctx.docset.docs)} chars"


def _make_llm(ctx: Ctx) -> Any:
    if ctx.llm is None:
        ctx.llm = LLMClient(trace=ctx.trace, budget_s=ctx.budget_s)
    return ctx.llm


def stage_extract_tools(ctx: Ctx) -> str:
    assert ctx.docset is not None
    cached = ctx.reuse_dir / "stage_a_tools.json" if ctx.reuse_dir else None
    if cached is not None and cached.is_file():
        raw = json.loads(cached.read_text(encoding="utf-8"))
        ctx.trace.note(f"stage A reused from {cached} (development shortcut)")
    else:
        raw = flow_llm.ask_json(_make_llm(ctx), "flow stage A: tools", flow_llm.STAGE_A_SYSTEM, flow_docs.render(ctx.docset), max_tokens=12000)
    _write(ctx.run_dir / "stage_a_tools.json", raw)
    ctx.plan = flow_tools.normalise(raw, ctx.server_url)
    for w in ctx.plan.warnings:
        ctx.trace.warn(("DEGRADED: " if w.startswith("WARNING") else "") + w)
        if w.startswith("WARNING"):
            print(w, file=sys.stderr, flush=True)
    if not ctx.plan.tools:
        raise ValueError("stage A found no tools in the documents")
    if not ctx.plan.call_variables:
        degrade(ctx, "stage A found no call variables")
    return f"{len(ctx.plan.tools)} tools: " + ", ".join(f"{t['name']}={t['url']}" for t in ctx.plan.tools)


def stage_author_policy(ctx: Ctx) -> str:
    assert ctx.docset is not None and ctx.plan is not None
    cached = ctx.reuse_dir / "spec.json" if ctx.reuse_dir else None
    if cached is not None and cached.is_file():
        ctx.policy = json.loads(cached.read_text(encoding="utf-8"))
        ctx.trace.note(f"stage B reused from {cached} (development shortcut)")
    else:
        tools_view = [{"name": t["name"], "description": t["description_for_model"], "fields": [
            {k: f[k] for k in ("name", "source", "required")} for f in t["fields"]],
            "response_fields_that_matter": t["response_fields_that_matter"], "errors": t["errors"],
            "side_effects": t["side_effects"], "notes": t["notes"]} for t in ctx.plan.tools]
        user = (flow_docs.render(ctx.docset) + "\n\n=== TOOLS (from stage A; the assistant can call exactly these) ===\n"
                + json.dumps(tools_view, ensure_ascii=False) + "\n\n=== CALL VARIABLES ===\n"
                + json.dumps(ctx.plan.call_variables, ensure_ascii=False))
        ctx.policy = flow_llm.ask_json(_make_llm(ctx), "flow stage B: policy", flow_llm.STAGE_B_SYSTEM, user, max_tokens=30000, timeout_s=480)
        errs = flow_policy.check_policy(ctx.policy, ctx.plan.names())
        if errs:
            ctx.trace.warn("policy check: " + "; ".join(errs[:5]))
            ctx.policy = flow_llm.ask_json(ctx.llm, "flow stage B: policy fix", flow_llm.REPAIR_SYSTEM,
                                           "POLICY:\n" + json.dumps(ctx.policy, ensure_ascii=False) + "\n\nERRORS:\n" + "\n".join(errs),
                                           max_tokens=30000, timeout_s=480)
    if not isinstance(ctx.policy, dict):
        raise ValueError("stage B did not return a JSON object")
    _write(ctx.run_dir / "spec.json", ctx.policy)
    _write(ctx.run_dir / "conflicts_report.json", ctx.policy.get("conflicts_resolved", []))
    return (f"policy with {len(ctx.policy.get('rules', []))} rules, {len(ctx.policy.get('handovers', []))} hand-overs, "
            f"{len(ctx.policy.get('conflicts_resolved', []))} resolved conflicts")


def stage_assemble(ctx: Ctx) -> str:
    assert ctx.plan is not None and ctx.policy is not None
    wf, pol, report = flow_assemble.validate_with_repair(
        ctx.policy, ctx.plan, ctx.slot, ctx.llm, None if ctx.dry else _freya(ctx), ctx.trace, ctx.dry)
    ctx.workflow, ctx.policy, ctx.report = wf, pol, report
    _write(ctx.run_dir / "workflow.json", wf)
    _write(ctx.run_dir / "spec.json", pol)
    ctx.trace.note("hand-over modelled as end_call nodes with distinct terminal names (no transfer node type is documented): "
                   + ", ".join(f"{h['node']}->{h['terminal']}" for h in report.get("handovers", [])))
    _write(ctx.run_dir / "compile_report.json", {
        "mode": "flow", "mode_why": ctx.mode_why, "slot": ctx.slot, "server_url": ctx.server_url,
        "tools": [{"name": t["name"], "url": t["url"], "method": t["method"]} for t in ctx.plan.tools],
        "call_variables": ctx.plan.call_variables, "warnings": ctx.plan.warnings,
        "conflicts_resolved": pol.get("conflicts_resolved", []), **{k: v for k, v in report.items() if k != "tools"}})
    rounds = report["validation_rounds"]
    print(f"assembled prompt size: system={report['system_prompt_chars']} task={report['task_message_chars']} chars", flush=True)
    print("tools: " + " ".join(f"{t['name']}={t['url']}" for t in ctx.plan.tools), flush=True)
    return f"{len(rounds)} validation round(s), last ok={rounds[-1]['ok']}"


def stage_save(ctx: Ctx) -> str:
    if ctx.dry:
        raise Skipped("dry run")
    info = _freya(ctx).save_workflow(ctx.slot, ctx.workflow, state_file=str(ctx.root / ".freya-agents.json"))
    ctx.saved = info
    ctx.trace.log_artifact("saved", str(info["workflow_id"]), str(info.get("agent_id")), version=info.get("version"), note=f"slot={ctx.slot}")
    return f"workflow_id={info['workflow_id']}"


def stage_read_back(ctx: Ctx) -> str:
    if ctx.saved is None:
        raise Skipped("nothing saved")
    stored = get_workflow(str(ctx.saved["workflow_id"]), client=_freya(ctx))
    swf = stored.get("workflow", stored) if isinstance(stored, dict) else {}
    diffs = readback.diff_workflows(ctx.workflow or {}, swf if isinstance(swf, dict) else {})
    for line in diffs:
        ctx.trace.warn(f"read_back: {line}")
    return f"{len(diffs)} difference(s)"


def stage_export(ctx: Ctx) -> str:
    if ctx.saved is None:
        raise Skipped("nothing saved")
    path = _freya(ctx).export_workflow(str(ctx.saved["workflow_id"]), str(ctx.run_dir / f"{ctx.slot}.workflow.json"))
    ctx.trace.log_artifact("exported", str(ctx.saved["workflow_id"]), str(ctx.saved.get("agent_id")),
                           version=ctx.saved.get("version"), note=f"slot={ctx.slot} path={path}")
    return f"exported to {path}"


def mock_health(server_url: str | None, timeout: float = 10.0) -> tuple[bool, str]:
    if not server_url:
        return False, "no server url"
    import urllib.request
    url = flow_tools.origin_of(server_url) + "/health"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (build-the-builder)"}), timeout=timeout) as r:
            return 200 <= r.status < 300, f"GET {url} -> {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"GET {url} failed: {type(e).__name__}: {e}"


def stage_smoke(ctx: Ctx) -> str:
    if ctx.saved is None:
        raise Skipped("nothing saved")
    assert ctx.plan is not None
    variables = {v["name"]: (v["example_from_docs"] or "smoke-test") for v in ctx.plan.call_variables}
    res = health.probe_chat(str(ctx.saved["workflow_id"]), variables, client=_freya(ctx))
    if not res["ok"]:
        ctx.trace.warn(f"model backend unhealthy: {res['error']}")
        ctx.trace.log_test("smoke", ctx.slot, "model backend unhealthy, skipped")
        ctx.backend_unhealthy = True
        raise Skipped("model backend unhealthy")
    ctx.trace.log_test("smoke", ctx.slot, "ok", details=f"{res['seconds']:.1f}s")
    return f"chat answered in {res['seconds']:.1f}s"


def stage_online(ctx: Ctx) -> str:
    if ctx.dry:
        raise Skipped("dry run")
    if ctx.saved is None:
        raise Skipped("nothing saved")
    if ctx.backend_unhealthy:
        raise Skipped("model backend unhealthy")
    from builder import flow_online
    ctx.online_limit_s = max(120.0, min(ONLINE_MAX_S + 300, ctx.deadline - time.monotonic() - 60))
    try:
        return flow_online.run_online(ctx, mock_health, stage_export)
    except flow_online.Skipped as e:
        raise Skipped(str(e)) from e


def emergency_workflow(ctx: Ctx) -> dict[str, Any]:
    """Generic fallback: one conversation node that follows the documents' text, one end node, no tools."""
    from builder import tools_gen
    texts = ""
    if ctx.docset is not None and ctx.docset.docs:
        texts = "\n\n".join(f"[{d.rel}]\n{d.text}" for d in ctx.docset.docs)[:9000]
    return {"initial_node": "main",
            "system_prompt": "You are a phone assistant. Reply briefly. Follow these documents; the newest wins:\n" + flow_policy.clean(texts, set()),
            "nodes": {"main": {"name": "main", "node_type": "conversation", "on_enter": {"type": "llm"}, "role_message": "",
                               "task_message": "Help the caller as the documents describe. When the call is finished call transition_to_end_fallback.",
                               "tools": [], "transitions": [flow_assemble.tool_transition("end_fallback", "The call is finished")],
                               "max_node_turns": 40, "max_turn_transition": "end_fallback"},
                      "end_fallback": tools_gen.end_call_node("end_fallback")},
            "global_nodes": {}, "tools": []}


STAGES: list[tuple[str, Callable[[Ctx], str]]] = [
    ("select_mode", stage_select_mode), ("load_docs", stage_load_docs), ("extract_tools", stage_extract_tools),
    ("author_policy", stage_author_policy), ("assemble", stage_assemble), ("save", stage_save),
    ("read_back", stage_read_back), ("export", stage_export), ("smoke_test", stage_smoke), ("online_test", stage_online),
]
ESSENTIAL = {"select_mode", "load_docs"}


def run_flow_build(root: Path, folder: Path, slot: str, dry: bool, server_url: str | None, mode_why: str,
                   budget_s: float = TOTAL_BUDGET_S, runs_dir: Path | None = None, on_run_dir: Callable[[Path], None] | None = None,
                   llm: Any = None, client: Any = None, reuse_dir: Path | None = None, stages: list | None = None) -> int:
    root, folder = Path(root), Path(folder)
    run_dir = _new_run_dir(Path(runs_dir) if runs_dir is not None else root / "runs", slot)
    if on_run_dir is not None:
        on_run_dir(run_dir)
    trace = Trace(run_dir, root)
    ctx = Ctx(root=root, folder=folder, slot=slot, dry=dry, server_url=server_url, mode_why=mode_why, run_dir=run_dir,
              trace=trace, budget_s=budget_s, client=client, reuse_dir=reuse_dir)
    ctx.llm = llm
    code = 0
    try:
        files = [d for d in folder_input._walk(folder) if d.suffix.lower() in (".md", ".txt", ".pdf", ".json")]  # noqa: SLF001
        trace.set_inputs(files[:60], folder, None, slot, {
            "mode": "flow", "dry": dry, "time budget": f"{budget_s:g} s", "server url": server_url or "(contract address)",
            "stage reuse": str(reuse_dir) if reuse_dir else "(none)"})
        deadline = ctx.deadline = time.monotonic() + budget_s
        for name, fn in (stages or STAGES):
            if name not in ESSENTIAL and time.monotonic() > deadline:
                degrade(ctx, f"stage {name} skipped: total time budget exhausted")
                trace.log_stage(name, "skipped", "budget exhausted", 0.0)
                continue
            if name == "extract_tools":
                try:
                    _make_llm(ctx)
                except LLMConfigError as e:
                    degrade(ctx, f"no LLM available ({e}); writing the generic fallback workflow")
                    trace.log_stage("extract_tools", "failed", str(e), 0.0)
                    code = 1
                    break
            if run_stage(ctx, name, fn) == "failed":
                code = 1
                if name in ("extract_tools", "author_policy", "assemble"):
                    degrade(ctx, f"stage {name} failed; the generic fallback workflow is used if nothing else was assembled")
                    if name != "assemble":
                        break
    except Exception as e:  # noqa: BLE001
        trace.error(f"flow build crashed: {type(e).__name__}: {e}", traceback_text=traceback.format_exc())
        code = 1
    finally:
        try:
            if not (run_dir / "workflow.json").is_file():
                trace.error("workflow.json missing after all stages; writing the generic fallback workflow")
                _write(run_dir / "workflow.json", emergency_workflow(ctx))
                trace.warn("DEGRADED: workflow.json is the generic fallback (no tools, no policy)")
        except Exception as e:  # noqa: BLE001
            trace.error(f"fallback workflow could not be written: {e}")
        if ctx.llm is not None and getattr(ctx.llm, "calls", 0):
            line = f"llm calls={ctx.llm.calls} tokens in={ctx.llm.total_input_tokens} out={ctx.llm.total_output_tokens}"
            trace.note(line)
            print(line, flush=True)
        code = trace.finish(code)
    return code
