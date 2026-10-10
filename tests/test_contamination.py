"""Guard: no value from the known briefs may appear in builder code (builder/ and build.py)."""

from __future__ import annotations

from pathlib import Path

from tests.conftest import REPO_ROOT

BANNED_TOKENS: tuple[str, ...] = (
    "2026-10-26",
    "2026-11-11",
    "2026-11-26",
    "2026-12-25",
    "2027-01-01",
    "collections team",
    "restructuring",
    "minimum_payment",
    "instalment_amount",
    "phone_last4",
    "date_of_birth",
    "not_reached",
    "not_verified",
    "no_promise",
    "no_callback",
    "wrong_number",
    "service_error",
    "day_month_swapped",
    "credit_card",
    "personal_loan",
    "car_loan",
    "card_last4",
    "past_due",
    "freya bank",
    "brief-a",
    "brief-b",
    "briefs/",
    "tests/fixtures",
    "do you confirm",
    "can you make this payment today",
    "i am sorry for your loss",
    "you will receive a text message",
)


def _builder_files() -> list[Path]:
    files = [REPO_ROOT / "build.py"]
    for p in sorted((REPO_ROOT / "builder").rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            files.append(p)
    return files


def test_no_brief_tokens_in_builder() -> None:
    hits: list[str] = []
    for p in _builder_files():
        text = p.read_text(encoding="utf-8", errors="replace").lower()
        for token in BANNED_TOKENS:
            if token in text:
                hits.append(f"{p.relative_to(REPO_ROOT).as_posix()}: {token}")
    assert not hits, "brief values in builder code:\n" + "\n".join(hits)


def test_no_directory_scanning_outside_trace() -> None:
    allowed = REPO_ROOT / "builder" / "trace.py"
    hits: list[str] = []
    for p in _builder_files():
        if p.suffix != ".py" or p == allowed:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for needle in ("glob(", "iterdir("):
            if needle in text:
                hits.append(f"{p.relative_to(REPO_ROOT).as_posix()}: {needle}")
    assert not hits, "directory scanning in builder code:\n" + "\n".join(hits)
