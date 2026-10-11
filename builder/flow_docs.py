"""Collect, order and trim the text documents of a folder for the generic flow mode."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from builder import folder_input

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
PER_DOC_CHARS = 40_000
MIN_KEEP_CHARS = 8_000
SUMMARY_CHARS = 6_000
OVERRIDE_WORDS = ("decision", "override", "supersede", "recap")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_COMPACT = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
_DATE_LINE = re.compile(r"^\W*date\s*:.*$", re.IGNORECASE | re.MULTILINE)
CODE_MARKERS = ("package.json", "wrangler.toml", "wrangler.json", "wrangler.jsonc", "pyproject.toml")


@dataclass
class Doc:
    path: Path
    rel: str
    text: str
    date: str = ""
    priority: str = "normal"
    kind: str = "text"


@dataclass
class DocSet:
    docs: list[Doc] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    test_data_summary: str = ""


def find_date(name: str, text: str) -> str:
    for rx in (_ISO, _COMPACT):
        m = rx.search(name)
        if m:
            return "-".join(m.groups())
    for line in _DATE_LINE.findall(text[:3000]):
        m = _ISO.search(line)
        if m:
            return "-".join(m.groups())
    return ""


def trim(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    head = int(cap * 0.7)
    return text[:head] + "\n[trimmed]\n" + text[-(cap - head):]


def _scalars(obj: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in obj.items() if isinstance(v, (str, int, float, bool)))[:160]


def _shape(obj: Any, path: str, out: list[str], depth: int = 0) -> None:
    if depth > 5 or len(out) > 14:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _shape(v, f"{path}/{k}" if path else str(k), out, depth + 1)
            elif k in ("http", "status", "exitState", "code", "returnCode") and isinstance(v, (str, int)):
                out.append(f"{path.rsplit('/', 1)[-1]}:{k}={v}"[:40])
    elif isinstance(obj, list):
        out.append(f"{path.rsplit('/', 1)[-1]}[{len(obj)}]")
        for item in obj[:1]:
            _shape(item, path, out, depth + 1)


def summarise_json(obj: Any, cap: int = SUMMARY_CHARS) -> str:
    """Deterministic compact summary of a JSON test-data file: shape, record count, one line per record."""
    lines: list[str] = []
    records: list[Any] = []
    if isinstance(obj, dict):
        lines.append("top-level keys: " + ", ".join(str(k) for k in obj))
        for k, v in obj.items():
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v):
                lines.append(f"list '{k}': {len(v)} records; record keys: " + ", ".join(v[0]))
                records = v
                break
        if isinstance(obj.get("description"), str):
            lines.append("description: " + obj["description"][:500])
    elif isinstance(obj, list) and all(isinstance(r, dict) for r in obj):
        records = obj
        lines.append(f"list: {len(obj)} records; record keys: " + (", ".join(obj[0]) if obj else ""))
    for r in records:
        sig: list[str] = []
        _shape({k: v for k, v in r.items() if isinstance(v, (dict, list))}, "", sig)
        lines.append("- " + _scalars(r) + " | " + " ".join(sig))
    text = "\n".join(lines)
    return text if len(text) <= cap else text[: cap - 12] + "\n[trimmed]"


def _read_text(path: Path, notes: list[str]) -> tuple[str, str] | None:
    suffix = path.suffix.lower()
    raw = folder_input._read(path)  # noqa: SLF001
    if raw is None:
        notes.append(f"skipped {path.name}: unreadable or too large")
        return None
    if suffix in TEXT_SUFFIXES:
        text = folder_input.sniff_text(raw)
        return (text, "text") if text is not None else None
    if suffix == ".pdf":
        text = folder_input._pdf_to_text(path)  # noqa: SLF001
        if text is None:
            try:
                import io

                import pypdf  # type: ignore[import-not-found]

                text = "\n".join((p.extract_text() or "") for p in pypdf.PdfReader(io.BytesIO(raw)).pages) or None
            except Exception:  # noqa: BLE001
                text = None
        if text is None:
            notes.append(f"pdf skipped (no extractor available or no text): {path.name}")
            return None
        return text, "pdf"
    return None


def collect(folder: Path | str, budget_chars: int = 160_000) -> DocSet:
    folder = Path(folder)
    out = DocSet()
    summaries: list[str] = []
    all_files = folder_input._walk(folder)  # noqa: SLF001
    code_roots = set()
    for p in all_files:
        if p.name.lower() in CODE_MARKERS or p.name.lower().startswith("wrangler."):
            code_roots.add(p.parent)
            code_roots.add(p.parent.parent)
    code_roots.discard(folder)
    for path in all_files:
        rel = path.relative_to(folder).as_posix()
        if any(d == path.parent or d in path.parents for d in code_roots):
            out.notes.append(f"skipped (inside a code project): {rel}")
            continue
        if path.suffix.lower() == ".json":
            raw = folder_input._read(path)  # noqa: SLF001
            text = folder_input.sniff_text(raw) if raw else None
            try:
                obj = json.loads(text) if text else None
            except ValueError:
                obj = None
            if obj is None:
                continue
            if folder_input.classify_json(obj) == "openapi":
                out.docs.append(Doc(path, rel, trim(text or "", PER_DOC_CHARS), find_date(rel, text or ""), kind="contract"))
            else:
                summaries.append(f"### {rel}\n{summarise_json(obj)}")
                out.notes.append(f"json summarised, not sent whole: {rel}")
            continue
        got = _read_text(path, out.notes)
        if got is None:
            continue
        text, kind = got
        doc = Doc(path, rel, trim(text, PER_DOC_CHARS), find_date(rel, text), kind=kind)
        if any(w in rel.lower() for w in OVERRIDE_WORDS):
            doc.priority = "override"
        out.docs.append(doc)
    out.docs.sort(key=lambda d: (d.date != "", d.date, d.rel))
    dated = [d for d in out.docs if d.date]
    if dated:
        dated[-1].priority = "highest"
    total = sum(len(d.text) for d in out.docs)
    if total > budget_chars:
        order = sorted(out.docs, key=lambda d: ({"normal": 0, "override": 1, "highest": 2}[d.priority], -len(d.text)))
        for d in order:
            if total <= budget_chars:
                break
            if len(d.text) <= MIN_KEEP_CHARS:
                continue
            new = trim(d.text, max(MIN_KEEP_CHARS, len(d.text) // 2))
            total -= len(d.text) - len(new)
            d.text = new
        out.notes.append(f"document text trimmed to fit {budget_chars} chars")
    out.test_data_summary = trim("\n\n".join(summaries), SUMMARY_CHARS)
    return out


def render(ds: DocSet) -> str:
    n = len(ds.docs)
    parts = ["Later documents override earlier ones; the newest overrides all."]
    for i, d in enumerate(ds.docs, 1):
        parts.append(f"=== DOC {i}/{n}: {d.rel} (date: {d.date or 'undated'}, priority: {d.priority}) ===\n{d.text}")
    return "\n\n".join(parts)
