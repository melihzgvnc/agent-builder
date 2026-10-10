from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from builder.pipeline import NETWORK_STAGES, STAGE_ORDER
from builder.trace import REQUIRED_SECTIONS
from tests.conftest import REPO_ROOT

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", "runs", ".agents", ".pytest_cache", "__pycache__"}
)


def _snapshot(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _porcelain(root: Path) -> str:
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=30
    )
    return r.stdout


def _run_dirs(prefix: str) -> set[Path]:
    runs = REPO_ROOT / "runs"
    if not runs.is_dir():
        return set()
    return {p for p in runs.iterdir() if p.is_dir() and p.name.startswith(prefix)}


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run build.py like a user does (bytecode writing left at the interpreter default)."""
    base = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    return subprocess.run(
        [sys.executable, "build.py", *args],
        cwd=REPO_ROOT,
        env={**base, **(env or {})},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_dry_run_isolation(tmp_path: Path) -> None:
    before = _snapshot(REPO_ROOT)
    porcelain_before = _porcelain(REPO_ROOT)
    runs_before = _run_dirs("brief-a-")
    # Any bytecode the run writes lands under this prefix, where it can be seen.
    pycache = tmp_path / "pycache"
    proc = _run_cli("briefs/brief-a.md", "--dry", env={"PYTHONPYCACHEPREFIX": str(pycache)})
    new = _run_dirs("brief-a-") - runs_before
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        written = sorted(pycache.rglob("*.pyc")) if pycache.exists() else []
        repo_rel = REPO_ROOT.resolve().relative_to(REPO_ROOT.resolve().anchor)
        own = [
            p for p in written
            if p.relative_to(pycache).is_relative_to(repo_rel)
            and not p.relative_to(pycache).is_relative_to(repo_rel / ".venv")
        ]
        assert own == [], own
        assert len(new) == 1, new
        run_dir = next(iter(new))
        assert str(run_dir) in proc.stdout
        text = (run_dir / "trace.log").read_text(encoding="utf-8")
        for name in REQUIRED_SECTIONS:
            assert f"== {name} ==" in text, name
        for name in STAGE_ORDER:
            assert f"\n{name}: " in text, name
        for name in NETWORK_STAGES:
            assert f"{name}: skipped: --dry" in text, name
        assert "load_inputs: ok" in text
        brief_hash = hashlib.sha256((REPO_ROOT / "briefs" / "brief-a.md").read_bytes()).hexdigest()
        assert brief_hash in text
        assert (run_dir / "trace.jsonl").is_file()
        assert _snapshot(REPO_ROOT) == before
        assert _porcelain(REPO_ROOT) == porcelain_before
    finally:
        for d in new:
            shutil.rmtree(d)


def test_two_briefs_layered() -> None:
    runs_before = _run_dirs("brief-b-")
    proc = _run_cli("briefs/brief-a.md", "briefs/brief-b.md", "--dry")
    new = _run_dirs("brief-b-") - runs_before
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(new) == 1, new
        text = (next(iter(new)) / "trace.log").read_text(encoding="utf-8")
        a = REPO_ROOT / "briefs" / "brief-a.md"
        b = REPO_ROOT / "briefs" / "brief-b.md"
        ia = text.index(f"brief[0]: {a}")
        ib = text.index(f"brief[1]: {b}")
        assert ia < ib
        assert "slot: brief-b" in text
    finally:
        for d in new:
            shutil.rmtree(d)
