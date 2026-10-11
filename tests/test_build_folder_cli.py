from __future__ import annotations
import json
import shutil
from pathlib import Path
from tests.conftest import REPO_ROOT
from tests.test_build_cli import _run_cli, _run_dirs
SLOT = "folder-cli-test"
def _strings(obj: object):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
def test_folder_dry_build_with_server_url(tmp_path: Path) -> None:
    d = tmp_path / "in"
    d.mkdir()
    shutil.copy(REPO_ROOT / "briefs" / "brief-a.md", d / "BRIEF.md")
    shutil.copy(REPO_ROOT / "tools" / "openapi.json", d / "contract-renamed.json")
    shutil.copy(REPO_ROOT / "public-test-set.json", d / "people.json")
    (d / "broken.json").write_text("{", encoding="utf-8")
    before = _run_dirs(SLOT)
    proc = _run_cli(
        "--folder", str(d), "--dry", "--slot", SLOT,
        "--server-url", "https://example.invalid",
        "--spec", "tests/fixtures/spec_brief_a.json",
    )
    new = _run_dirs(SLOT) - before
    try:
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(new) == 1
        run_dir = next(iter(new))
        wf = json.loads((run_dir / "workflow.json").read_text(encoding="utf-8"))
        urls = [s for s in _strings(wf) if s.startswith(("http://", "https://"))]
        assert urls
        assert all(u.startswith("https://example.invalid") for u in urls), urls
        log = (run_dir / "trace.log").read_text(encoding="utf-8")
        assert "contract-renamed.json" in log
        assert "server_url override: https://example.invalid" in log
    finally:
        for p in new:
            shutil.rmtree(p)
def test_no_briefs_is_an_error() -> None:
    proc = _run_cli("--dry")
    assert proc.returncode == 2
    assert "no briefs" in proc.stderr
