"""Folder ingestion: find the briefs, the tool API document and the customers file by content.

File names are not trusted (a single documented name, BRIEF.md, sorts first). Nothing here
raises on an unreadable, binary or unparsable file: it is skipped and the reason is logged.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRIMARY_BRIEF = "brief.md"
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
YAML_SUFFIXES = {".yaml", ".yml"}
MAX_FILE_BYTES = 4_000_000
EXTRA_MAX_CHARS = 20_000  # per extra file appended to the extractor input
SKIP_DIRS = {".git", "node_modules", "__MACOSX", "__pycache__", ".venv"}


@dataclass
class FolderInputs:
    briefs: list[Path] = field(default_factory=list)
    openapi_path: Path | None = None
    customers_path: Path | None = None
    extras: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def classify_json(obj: Any) -> str:
    """'openapi', 'customers' or 'data' for a parsed JSON/YAML value."""
    if isinstance(obj, dict):
        if isinstance(obj.get("paths"), dict) and ("openapi" in obj or "swagger" in obj or obj["paths"]):
            return "openapi"
        for value in obj.values():
            if isinstance(value, list) and value and classify_json(value) == "customers":
                return "customers"
        return "data"
    if isinstance(obj, list) and obj and all(isinstance(r, dict) for r in obj):
        hits = 0
        for row in obj:
            has_id = isinstance(row.get("id"), str) or isinstance(row.get("customer_id"), str)
            if has_id and (isinstance(row.get("identity"), dict) or isinstance(row.get("products"), list)):
                hits += 1
        if hits == len(obj):
            return "customers"
    return "data"


def sniff_text(raw: bytes) -> str | None:
    """Decoded text, or None when the bytes look binary or are not UTF-8."""
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _walk(folder: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            if not name.startswith("."):
                found.append(Path(dirpath) / name)
    return found


def _read(path: Path) -> bytes | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _pdf_to_text(path: Path) -> str | None:
    tool = shutil.which("pdftotext")
    if tool is None:
        return None
    try:
        proc = subprocess.run([tool, "-layout", str(path), "-"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout.strip() else None


def _parse_structured(path: Path, text: str) -> Any:
    """Parsed JSON, or YAML when a yaml module is importable; raises ValueError otherwise."""
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        raise ValueError("yaml module not installed") from None
    try:
        return yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001 - any parser failure means skip
        raise ValueError(f"unparsable yaml: {type(e).__name__}") from None


def discover(folder: Path) -> FolderInputs:
    """Classify every file under `folder` by content."""
    folder = Path(folder)
    out = FolderInputs()
    if not folder.is_dir():
        out.log.append(f"folder {folder}: not a directory")
        return out
    primary: list[Path] = []
    briefs: list[Path] = []
    openapis: list[Path] = []
    customers: list[Path] = []

    def skip(path: Path, reason: str) -> None:
        out.skipped.append((path, reason))
        out.log.append(f"skipped {path.relative_to(folder)}: {reason}")

    def extra(path: Path, why: str) -> None:
        out.extras.append(path)
        out.log.append(f"extra {path.relative_to(folder)}: {why}")

    for path in _walk(folder):
        rel = path.relative_to(folder)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            if _pdf_to_text(path) is None:
                skip(path, "pdf not readable (pdftotext missing or no text)")
            else:
                extra(path, "pdf text")
            continue
        if suffix == ".docx":
            skip(path, "docx not supported")
            continue
        raw = _read(path)
        if raw is None:
            skip(path, "unreadable or too large")
            continue
        text = sniff_text(raw)
        if text is None:
            skip(path, "binary or not utf-8")
            continue
        if suffix in TEXT_SUFFIXES:
            name = path.name.lower()
            if not text.strip():
                skip(path, "empty")
            elif name == PRIMARY_BRIEF:
                primary.append(path)
                out.log.append(f"brief {rel}: primary BRIEF.md")
            elif name.startswith("readme") or suffix == ".txt":
                extra(path, "documentation / text material")
            else:
                briefs.append(path)
                out.log.append(f"brief {rel}: markdown")
            continue
        if suffix == ".json" or suffix in YAML_SUFFIXES:
            try:
                kind = classify_json(_parse_structured(path, text))
            except ValueError as e:
                skip(path, f"not parsable ({e})")
                continue
            if kind == "openapi":
                openapis.append(path)
                out.log.append(f"contract candidate {rel}")
            elif kind == "customers":
                customers.append(path)
                out.log.append(f"customers candidate {rel}")
            else:
                extra(path, "structured data of unknown shape")
            continue
        skip(path, f"unknown file type {suffix or '(none)'}")

    out.briefs = sorted(primary, key=lambda p: str(p).lower()) + sorted(briefs, key=lambda p: str(p).lower())
    if openapis:
        out.openapi_path = openapis[0]
        out.log.append(f"contract: {openapis[0].relative_to(folder)}")
        for other in openapis[1:]:
            extra(other, "additional contract candidate (first one used)")
    else:
        out.log.append("contract: none found in folder")
    if customers:
        out.customers_path = customers[0]
        out.log.append(f"customers: {customers[0].relative_to(folder)}")
        for other in customers[1:]:
            extra(other, "additional customers candidate (first one used)")
    else:
        out.log.append("customers: none found in folder")
    if not out.briefs:
        out.log.append("briefs: none found in folder")
    return out


def extra_text(path: Path) -> str | None:
    """The text of an extra file with a header line, truncated; None when it cannot be read."""
    if path.suffix.lower() == ".pdf":
        text = _pdf_to_text(path)
    else:
        raw = _read(path)
        text = sniff_text(raw) if raw is not None else None
    if text is None:
        return None
    if len(text) > EXTRA_MAX_CHARS:
        text = text[:EXTRA_MAX_CHARS] + "\n[truncated]\n"
    return f"# EXTRA MATERIAL: {path.name}\n\n{text}"
