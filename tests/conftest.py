"""Shared test helpers. Fixtures under tests/fixtures are for tests only; builder code never reads them."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
FIXTURES_DIR: Path = REPO_ROOT / "tests" / "fixtures"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture
def repo_root() -> Path:
    """Absolute path of the repository root."""
    return REPO_ROOT


def load_fixture(name: str) -> dict[str, Any]:
    """Load tests/fixtures/<name> as JSON."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def copy_builder_tree(dst: Path) -> Path:
    """Copy build.py and builder/ (without __pycache__) into dst; returns dst as a fake repo root."""
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_ROOT / "build.py", dst / "build.py")
    shutil.copytree(
        REPO_ROOT / "builder", dst / "builder", ignore=shutil.ignore_patterns("__pycache__")
    )
    return dst
