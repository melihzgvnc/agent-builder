"""Compare the workflow that was sent with the one the platform stored."""

from __future__ import annotations

from typing import Any

VALUE_LIMIT: int = 60


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= VALUE_LIMIT else text[: VALUE_LIMIT - 3] + "..."


def _walk(sent: Any, stored: Any, path: str, out: list[str]) -> None:
    if isinstance(sent, dict):
        if not isinstance(stored, dict):
            out.append(f"changed {path or '<root>'}: dict -> {_short(stored)}")
            return
        for key, value in sent.items():
            sub = f"{path}.{key}" if path else str(key)
            if key not in stored:
                out.append(f"stripped {sub}")
            else:
                _walk(value, stored[key], sub, out)
    elif isinstance(sent, list):
        if not isinstance(stored, list):
            out.append(f"changed {path}: list -> {_short(stored)}")
            return
        if len(sent) != len(stored):
            out.append(f"length {path}: {len(sent)} -> {len(stored)}")
        for i, (a, b) in enumerate(zip(sent, stored)):
            _walk(a, b, f"{path}[{i}]", out)
    elif sent != stored or type(sent) is not type(stored):
        out.append(f"changed {path}: {_short(sent)} -> {_short(stored)}")


def diff_workflows(sent: dict[str, Any], stored: dict[str, Any], limit: int = 50) -> list[str]:
    """Differences from `sent` to `stored`; keys only present in `stored` are platform defaults."""
    found: list[str] = []
    _walk(sent, stored, "", found)
    if len(found) <= limit:
        return found
    return found[:limit] + [f"... {len(found) - limit} more"]
