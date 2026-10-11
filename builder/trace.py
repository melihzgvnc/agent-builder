"""Run trace: a plain-text trace.log plus a live JSONL event stream for every build run.

The JSONL file is appended to as events happen, so a crashed run still leaves a record.
trace.log gets a short header at start and is fully rewritten by finish().
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from builder import __version__

REQUIRED_SECTIONS: tuple[str, ...] = (
    "START",
    "INPUTS",
    "BUILDER VERSION",
    "STAGES",
    "MODEL CALLS",
    "FREYA ARTIFACTS",
    "TESTS ON AGENT",
    "WARNINGS",
    "ERRORS",
    "END",
)

SOURCE_SUFFIXES: frozenset[str] = frozenset({".py", ".json", ".md", ".txt", ".tmpl", ".jinja"})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def builder_source_files(root: Path) -> list[Path]:
    """build.py plus every source-like file under builder/ (no __pycache__), sorted."""
    files: list[Path] = []
    entry = root / "build.py"
    if entry.is_file():
        files.append(entry)
    pkg = root / "builder"
    if pkg.is_dir():
        for p in pkg.rglob("*"):
            if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
                continue
            if "__pycache__" in p.relative_to(root).parts:
                continue
            files.append(p)
    return sorted(files)


def builder_source_hash(root: Path, files: list[Path] | None = None) -> str:
    """sha256 over sorted 'relpath NUL file-sha256' lines of the builder source files."""
    if files is None:
        files = builder_source_files(root)
    lines = sorted(
        f"{p.relative_to(root).as_posix()}\0{sha256_file(p)}\n" for p in files if p.is_file()
    )
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def git_commit(root: Path) -> str:
    """Current commit hash, 'no-commit' when there is none, 'no-git' when git is missing."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "no-git"
    except (subprocess.TimeoutExpired, OSError):
        return "no-commit"
    if r.returncode != 0:
        return "no-commit"
    return r.stdout.strip() or "no-commit"


def _fmt_num(value: float | int | None, decimals: int | None = None) -> str:
    if value is None:
        return "-"
    if decimals is not None:
        return f"{float(value):.{decimals}f}"
    return str(value)


class Trace:
    """Collects run events, streams them to trace.jsonl and renders trace.log."""

    def __init__(self, run_dir: Path, root: Path) -> None:
        self.run_dir: Path = Path(run_dir)
        self.root: Path = Path(root)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.log_path: Path = self.run_dir / "trace.log"
        self.jsonl_path: Path = self.run_dir / "trace.jsonl"
        self.start_time: str = _now()
        self._t0: float = time.monotonic()
        self.inputs: list[str] = []
        self.notes: list[str] = []
        self.stages: list[str] = []
        self.model_calls: list[str] = []
        self.artifacts: list[str] = []
        self.tests: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.source_files: list[Path] = builder_source_files(self.root)
        self.source_sha256_start: str = builder_source_hash(self.root, self.source_files)
        self.source_sha256_end: str | None = None
        self.git_commit: str = git_commit(self.root)
        self.end_time: str | None = None
        self.duration_s: float | None = None
        self.exit_code: int | None = None
        self.result: str | None = None
        self._event(
            "start",
            start_time=self.start_time,
            run_dir=str(self.run_dir),
            version=__version__,
            git_commit=self.git_commit,
            source_files=len(self.source_files),
            source_sha256_start=self.source_sha256_start,
        )
        self.log_path.write_text(
            f"== START ==\nstart: {self.start_time}\nrun in progress\n", encoding="utf-8"
        )

    def _event(self, kind: str, **data: object) -> None:
        record: dict[str, object] = {"ts": _now(), "kind": kind, **data}
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()

    def set_inputs(
        self,
        briefs: list[Path],
        openapi: Path,
        cases: Path | None,
        slot: str,
        flags: dict[str, object],
    ) -> None:
        """Record every input file with its sha256 (or 'missing'), the slot and the flags."""

        def describe(p: Path) -> tuple[str, str]:
            ap = Path(p).resolve()
            digest = sha256_file(ap) if ap.is_file() else "missing"
            return str(ap), digest

        lines: list[str] = []
        brief_records: list[dict[str, str]] = []
        for i, b in enumerate(briefs):
            path, digest = describe(b)
            lines.append(f"brief[{i}]: {path} sha256={digest}")
            brief_records.append({"path": path, "sha256": digest})
        oa_path, oa_digest = describe(openapi)
        lines.append(f"openapi: {oa_path} sha256={oa_digest}")
        cases_record: dict[str, str] | None = None
        if cases is None:
            lines.append("cases: (none)")
        else:
            c_path, c_digest = describe(cases)
            lines.append(f"cases: {c_path} sha256={c_digest}")
            cases_record = {"path": c_path, "sha256": c_digest}
        lines.append(f"slot: {slot}")
        lines.append(f"run_dir: {self.run_dir.resolve()}")
        for key, value in flags.items():
            lines.append(f"flag {key}: {value}")
        self.inputs = lines
        self._event(
            "inputs",
            briefs=brief_records,
            openapi={"path": oa_path, "sha256": oa_digest},
            cases=cases_record,
            slot=slot,
            flags=flags,
        )

    def note(self, msg: str) -> None:
        """Record an informational line in the INPUTS section (and as a note event)."""
        self.notes.append(msg)
        self._event("note", message=msg)

    def log_stage(self, name: str, status: str, detail: str, duration_s: float) -> None:
        """Record one pipeline stage result (status: ok, skipped or failed)."""
        if status not in ("ok", "skipped", "failed"):
            raise ValueError(f"unknown stage status: {status}")
        self.stages.append(f"{name}: {status}: {detail} ({duration_s:.2f}s)")
        self._event("stage", name=name, status=status, detail=detail, duration_s=duration_s)

    def log_model_call(
        self,
        model: str,
        purpose: str,
        duration_s: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        note: str = "",
    ) -> None:
        """Record one model call."""
        line = (
            f"{model} | {purpose} | {_fmt_num(duration_s, 2)}s"
            f" | in={_fmt_num(input_tokens)} out={_fmt_num(output_tokens)}"
        )
        if note:
            line += f" | {note}"
        self.model_calls.append(line)
        self._event(
            "model_call",
            model=model,
            purpose=purpose,
            duration_s=duration_s,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            note=note,
        )

    def log_artifact(
        self,
        action: str,
        workflow_id: str,
        agent_id: str,
        version: int | str | None = None,
        note: str = "",
    ) -> None:
        """Record a platform artifact (workflow/agent created, updated, exported...)."""
        line = (
            f"{action} workflow_id={workflow_id} agent_id={agent_id}"
            f" version={_fmt_num(version)}"
        )
        if note:
            line += f" ({note})"
        self.artifacts.append(line)
        self._event(
            "artifact",
            action=action,
            workflow_id=workflow_id,
            agent_id=agent_id,
            version=version,
            note=note,
        )

    def log_test(
        self,
        kind: str,
        target: str,
        summary: str,
        passed: int | None = None,
        total: int | None = None,
        details: str = "",
    ) -> None:
        """Record a test run against a built agent."""
        line = f"{kind} {target}: {summary}"
        if passed is not None or total is not None:
            line += f" passed={_fmt_num(passed)}/{_fmt_num(total)}"
        if details:
            line += f" | {details}"
        self.tests.append(line)
        self._event(
            "test",
            test_kind=kind,
            target=target,
            summary=summary,
            passed=passed,
            total=total,
            details=details,
        )

    def warn(self, msg: str) -> None:
        """Record a warning."""
        self.warnings.append(msg)
        self._event("warning", message=msg)

    def error(self, msg: str, traceback_text: str | None = None) -> None:
        """Record an error; the traceback (if any) goes to trace.jsonl only."""
        self.errors.append(msg)
        if traceback_text is None:
            self._event("error", message=msg)
        else:
            self._event("error", message=msg, traceback=traceback_text)

    def finish(self, exit_code: int) -> int:
        """Close the run: re-check the builder source hash and write trace.log. Returns the exit code."""
        if self.exit_code is not None:
            self._write_log()
            return self.exit_code
        self.source_sha256_end = builder_source_hash(self.root)
        code = exit_code
        if self.source_sha256_end != self.source_sha256_start:
            self.error(
                "builder source changed during run: "
                f"start={self.source_sha256_start} end={self.source_sha256_end}"
            )
            code = 1
            self.result = "source-changed"
        elif code != 0:
            self.result = "degraded"
        else:
            self.result = "ok"
        self.exit_code = code
        self.end_time = _now()
        self.duration_s = time.monotonic() - self._t0
        self._event(
            "end",
            end_time=self.end_time,
            duration_s=self.duration_s,
            exit_code=code,
            result=self.result,
            source_sha256_end=self.source_sha256_end,
        )
        self._write_log()
        return code

    def _write_log(self) -> None:
        sections: dict[str, list[str]] = {
            "START": [f"start: {self.start_time}"],
            "INPUTS": [*self.inputs, *(f"note: {n}" for n in self.notes)],
            "BUILDER VERSION": [
                f"version: {__version__}",
                f"git_commit: {self.git_commit}",
                f"source_files: {len(self.source_files)}",
                f"source_sha256_start: {self.source_sha256_start}",
                f"source_sha256_end: {self.source_sha256_end or '-'}",
            ],
            "STAGES": self.stages,
            "MODEL CALLS": self.model_calls,
            "FREYA ARTIFACTS": self.artifacts,
            "TESTS ON AGENT": self.tests,
            "WARNINGS": self.warnings,
            "ERRORS": self.errors,
            "END": [
                f"end: {self.end_time or '-'}",
                f"duration: {_fmt_num(self.duration_s, 2)}s",
                f"exit_code: {_fmt_num(self.exit_code)}",
                f"result: {self.result or '-'}",
            ],
        }
        out: list[str] = []
        for name in REQUIRED_SECTIONS:
            out.append(f"== {name} ==")
            body = sections[name]
            out.extend(body if body else ["(none)"])
            out.append("")
        self.log_path.write_text("\n".join(out), encoding="utf-8")
