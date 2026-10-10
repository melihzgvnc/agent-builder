"""Check that no value from the known briefs leaked into builder code.

usage: uv run python scripts/check_contamination.py [--list-tokens] [--include-spikes]
           [--briefs FILE ...] [--fixtures FILE ...]

The banned tokens are derived at run time from the briefs and the spec fixtures built from them;
nothing from a brief is hardcoded here. Two tiers:

* strict (exit 1): ISO dates and datetimes, and every backticked, double-quoted, table-cell or
  JSON-leaf value that contains `_` or a digit or has three or more words. Single identifiers match
  on identifier boundaries, phrases as substrings, both case-insensitively.
* advisory (printed, never fails): single plain words and two-word phrases.

Tool names (from the tool API) and every property name of the spec schema are generic vocabulary and
are never tokens. Other generic hits go in scripts/contamination_allow.json, each with a reason;
an entry that no longer matches anything fails the check.
Scanned: builder/, build.py, scripts/ (without __pycache__, this script, the allowlist and
scripts/spikes/, which --include-spikes reports as notes only).
The concurrently written *_variant_* files are never read.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.dont_write_bytecode = True

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
SCRIPT_PATH: Path = Path(__file__).resolve()
DEFAULT_BRIEFS: tuple[Path, ...] = (
    REPO_ROOT / "briefs" / "brief-a.md",
    REPO_ROOT / "briefs" / "brief-b.md",
)
DEFAULT_FIXTURES: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "fixtures" / "spec_brief_a.json",
    REPO_ROOT / "tests" / "fixtures" / "spec_brief_ab.json",
)
DEFAULT_OPENAPI: Path = REPO_ROOT / "tools" / "openapi.json"
DEFAULT_SCHEMA: Path = REPO_ROOT / "builder" / "spec_schema.json"
DEFAULT_ALLOW: Path = REPO_ROOT / "scripts" / "contamination_allow.json"
DEFAULT_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "builder",
    REPO_ROOT / "build.py",
    REPO_ROOT / "scripts",
)

SPIKES_DIR_NAME = "spikes"
SKIP_DIR_NAMES: frozenset[str] = frozenset({"__pycache__"})
MAX_ADVISORY_LINES = 20

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?")
FULL_DATE_RE = re.compile(DATE_RE.pattern + r"\Z")
BACKTICK_RE = re.compile(r"`([^`\n]+)`")
QUOTE_RE = re.compile(r"[\"\u201c]([^\"\u201c\u201d\n]+)[\"\u201d]")
STRIP_CHARS = " \t'\"`*.,;:!?()\u201c\u201d"


@dataclass(frozen=True)
class Token:
    """One banned value. kind: date, identifier, phrase or word; tier: strict or advisory."""

    value: str
    kind: str
    tier: str
    source: str

    @property
    def key(self) -> str:
        """Case-insensitive matching key."""
        return self.value.casefold()


@dataclass(frozen=True)
class Hit:
    """One occurrence of a token in a scanned file."""

    path: str
    line: int
    token: Token

    def format(self) -> str:
        """path:line: token  [kind, source]"""
        return f"{self.path}:{self.line}: {self.token.value}  [{self.token.kind}, {self.token.source}]"


@dataclass
class ScanResult:
    """Everything one scan found."""

    hits: list[Hit] = field(default_factory=list)  # strict, not allowlisted
    advisory: list[Hit] = field(default_factory=list)
    notes: list[Hit] = field(default_factory=list)  # strict hits in spikes (never fail)
    used_allow: set[str] = field(default_factory=set)  # casefolded allowlist tokens that matched
    files: int = 0


# --------------------------------------------------------------------------- derivation


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = value.strip(STRIP_CHARS).strip()
    return re.sub(r"\.{2,}$", "", value).strip(STRIP_CHARS).strip()


def classify(value: str) -> tuple[str, str] | None:
    """Return (kind, tier) of a cleaned candidate, or None when it cannot be a token."""
    if not re.search(r"[A-Za-z]", value) and not FULL_DATE_RE.match(value):
        return None
    if FULL_DATE_RE.match(value):
        return "date", "strict"
    words = value.split(" ")
    if len(words) >= 3:
        return "phrase", "strict"
    if "_" in value or re.search(r"\d", value):
        return ("phrase" if len(words) > 1 else "identifier"), "strict"
    return ("phrase" if len(words) > 1 else "word"), "advisory"


def extract_candidates(text: str) -> Iterator[str]:
    """Yield raw candidate values from markdown-like text: dates, backticks, quotes, table cells."""
    for m in DATE_RE.finditer(text):
        yield m.group(0)
    for m in BACKTICK_RE.finditer(text):
        yield m.group(1)
    for m in QUOTE_RE.finditer(text):
        yield m.group(1)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        for cell in stripped.strip("|").split("|"):
            cell = cell.strip()
            if not cell or set(cell) <= set("-: "):
                continue
            yield cell.replace("`", "").replace("*", "")


def _json_leaves(data: Any) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for v in data.values():
            yield from _json_leaves(v)
    elif isinstance(data, list):
        for v in data:
            yield from _json_leaves(v)


def _property_names(node: Any) -> Iterator[str]:
    """Every key under a `properties` mapping, anywhere in a JSON schema."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            yield from (str(k) for k in props)
        for v in node.values():
            yield from _property_names(v)
    elif isinstance(node, list):
        for v in node:
            yield from _property_names(v)


def _enum_values(node: Any) -> Iterator[str]:
    """Every string in an `enum` list, anywhere in a JSON schema."""
    if isinstance(node, dict):
        enum = node.get("enum")
        if isinstance(enum, list):
            yield from (v for v in enum if isinstance(v, str))
        for v in node.values():
            yield from _enum_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _enum_values(v)


def allowed_vocabulary(openapi_path: Path, schema_path: Path) -> set[str]:
    """Generic vocabulary: tool names from the API paths, plus property names and enum values of the schema."""
    vocab: set[str] = set()
    if Path(openapi_path).is_file():
        paths = json.loads(Path(openapi_path).read_text(encoding="utf-8")).get("paths", {})
        vocab.update(p.strip("/").casefold() for p in paths)
    if Path(schema_path).is_file():
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        vocab.update(n.casefold() for n in _property_names(schema))
        vocab.update(n.casefold() for n in _enum_values(schema))
    return vocab


def is_spec_path(value: str, vocab: set[str]) -> bool:
    """True for a dotted reference into the spec (a.b[0].c) whose names are all schema vocabulary."""
    if not re.fullmatch(r"[A-Za-z_][\w]*(?:\[\d+\]|\.[A-Za-z_]\w*)+", value):
        return False
    parts = [p for p in re.split(r"[.\[\]]+", value.casefold()) if p and not p.isdigit()]
    return all(p in vocab for p in parts)


def _refuse_variant(paths: Iterable[Path]) -> None:
    for p in paths:
        if "_variant_" in Path(p).name:
            raise SystemExit(f"refusing to read {p}: *_variant_* files are off limits")


def derive_tokens(
    brief_paths: Iterable[Path],
    fixture_paths: Iterable[Path],
    openapi_path: Path,
    schema_path: Path,
) -> list[Token]:
    """Build the banned token list from the briefs and spec fixtures (nothing is hardcoded)."""
    brief_paths = [Path(p) for p in brief_paths]
    fixture_paths = [Path(p) for p in fixture_paths]
    _refuse_variant(brief_paths + fixture_paths)
    vocab = allowed_vocabulary(openapi_path, schema_path)
    tokens: dict[str, Token] = {}

    def add(raw: str, source: str) -> None:
        value = _clean(raw)
        cls = classify(value)
        if cls is None or value.casefold() in vocab or is_spec_path(value, vocab):
            return
        kind, tier = cls
        tokens.setdefault(value.casefold(), Token(value, kind, tier, source))

    for path in brief_paths:
        for raw in extract_candidates(path.read_text(encoding="utf-8")):
            add(raw, path.name)
    for path in fixture_paths:
        for leaf in _json_leaves(json.loads(path.read_text(encoding="utf-8"))):
            add(leaf, path.name)
            for raw in extract_candidates(leaf):
                add(raw, path.name)
    return sorted(tokens.values(), key=lambda t: (t.tier != "strict", t.kind, t.key))


# --------------------------------------------------------------------------- scanning


def load_allow(path: Path) -> tuple[dict[str, str], list[str]]:
    """Read the allowlist: ({casefolded token: reason}, problems). Missing file = empty."""
    path = Path(path)
    if not path.is_file():
        return {}, []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", []) if isinstance(data, dict) else data
    allow: dict[str, str] = {}
    problems: list[str] = []
    for i, entry in enumerate(entries):
        token = entry.get("token") if isinstance(entry, dict) else None
        reason = entry.get("reason") if isinstance(entry, dict) else None
        if not isinstance(token, str) or not token.strip():
            problems.append(f"allowlist entry {i}: missing token")
        elif not isinstance(reason, str) or not reason.strip():
            problems.append(f"allowlist entry {i} ({token!r}): a reason is required")
        else:
            allow[token.casefold()] = reason.strip()
    return allow, problems


def _word_regex(keys: list[str], date: bool = False) -> re.Pattern[str] | None:
    if not keys:
        return None
    alt = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
    right = r"(?![0-9])" if date else r"(?![a-z0-9_])"
    return re.compile(rf"(?<![a-z0-9_])(?:{alt}){right}")


class _Matcher:
    """Finds the tokens of one tier on a lowercased, whitespace-collapsed line."""

    def __init__(self, tokens: list[Token]) -> None:
        self.by_key = {t.key: t for t in tokens}
        singles = [t.key for t in tokens if t.kind in ("identifier", "word")]
        dates = [t.key for t in tokens if t.kind == "date"]
        self.phrases = [t.key for t in tokens if t.kind == "phrase"]
        self.single_re = _word_regex(singles)
        self.date_re = _word_regex(dates, date=True)

    def find(self, line: str) -> list[Token]:
        found: dict[str, Token] = {}
        for rx in (self.single_re, self.date_re):
            if rx is not None:
                for m in rx.finditer(line):
                    found[m.group(0)] = self.by_key[m.group(0)]
        for key in self.phrases:
            if key in line:
                found[key] = self.by_key[key]
        return list(found.values())


def _iter_files(
    roots: Iterable[Path], exclude: set[Path], include_spikes: bool
) -> Iterator[tuple[Path, bool]]:
    """Yield (file, in_spikes) for every candidate file under the roots."""
    for root in roots:
        root = Path(root)
        if root.is_file():
            yield root, False
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in SKIP_DIR_NAMES and (include_spikes or d != SPIKES_DIR_NAME)
            )
            in_spikes = SPIKES_DIR_NAME in Path(dirpath).relative_to(root).parts
            for name in sorted(filenames):
                yield Path(dirpath) / name, in_spikes


def scan(
    roots: Iterable[Path],
    tokens: list[Token],
    allow: dict[str, str],
    *,
    exclude: Iterable[Path] = (),
    include_spikes: bool = False,
    base: Path = REPO_ROOT,
) -> ScanResult:
    """Scan the roots for tokens. Allowlisted strict tokens are suppressed and recorded as used."""
    strict = _Matcher([t for t in tokens if t.tier == "strict"])
    advisory = _Matcher([t for t in tokens if t.tier == "advisory"])
    skip = {Path(p).resolve() for p in exclude}
    result = ScanResult()
    for path, in_spikes in _iter_files(roots, skip, include_spikes):
        if path.resolve() in skip or "_variant_" in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        result.files += 1
        try:
            rel = path.resolve().relative_to(Path(base).resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        for no, raw in enumerate(text.splitlines(), start=1):
            line = re.sub(r"\s+", " ", raw.casefold())
            if not line.strip():
                continue
            for tok in strict.find(line):
                hit = Hit(rel, no, tok)
                if in_spikes:
                    result.notes.append(hit)
                elif tok.key in allow:
                    result.used_allow.add(tok.key)
                else:
                    result.hits.append(hit)
            for tok in advisory.find(line):
                result.advisory.append(Hit(rel, no, tok))
    return result


# --------------------------------------------------------------------------- CLI


def _advisory_summary(hits: list[Hit]) -> list[str]:
    counts: dict[str, list[Hit]] = {}
    for h in hits:
        counts.setdefault(h.token.key, []).append(h)
    ranked = sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    lines = [
        f"  {len(v):4d}x {v[0].token.value}  (first: {v[0].path}:{v[0].line})"
        for _, v in ranked[:MAX_ADVISORY_LINES]
    ]
    if len(ranked) > MAX_ADVISORY_LINES:
        lines.append(f"  ... and {len(ranked) - MAX_ADVISORY_LINES} more advisory words")
    return lines


def build_parser() -> argparse.ArgumentParser:
    """Command line of the check."""
    p = argparse.ArgumentParser(
        prog="check_contamination.py",
        description="Fail when brief values appear in builder/, build.py or scripts/.",
    )
    p.add_argument("--list-tokens", action="store_true", help="print the derived tokens and exit")
    p.add_argument("--include-spikes", action="store_true", help="also scan scripts/spikes/ (notes only)")
    p.add_argument("--briefs", nargs="+", type=Path, default=list(DEFAULT_BRIEFS), metavar="FILE")
    p.add_argument("--fixtures", nargs="+", type=Path, default=list(DEFAULT_FIXTURES), metavar="FILE")
    p.add_argument("--roots", nargs="+", type=Path, default=list(DEFAULT_ROOTS), metavar="PATH", help="paths to scan")
    p.add_argument("--openapi", type=Path, default=DEFAULT_OPENAPI)
    p.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    p.add_argument("--allow", type=Path, default=DEFAULT_ALLOW, help="allowlist JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    """Run the check; 0 when clean, 1 on strict hits or allowlist problems."""
    args = build_parser().parse_args(argv)
    tokens = derive_tokens(args.briefs, args.fixtures, args.openapi, args.schema)
    if args.list_tokens:
        for t in tokens:
            print(f"{t.tier:8s} {t.kind:10s} {t.value}  ({t.source})")
        return 0
    allow, problems = load_allow(args.allow)
    result = scan(
        args.roots,
        tokens,
        allow,
        exclude=[SCRIPT_PATH, args.allow],
        include_spikes=args.include_spikes,
    )
    problems += [
        f"unused allowlist entry: {k!r} matches nothing (remove it)"
        for k in sorted(allow)
        if k not in result.used_allow
    ]
    seen: set[tuple[str, int, str]] = set()
    for h in result.hits:
        ident = (h.path, h.line, h.token.key)
        if ident not in seen:
            seen.add(ident)
            print(h.format())
    for problem in problems:
        print(f"ALLOWLIST: {problem}")
    if result.notes:
        print(f"NOTE: {len(result.notes)} strict hit(s) in {SPIKES_DIR_NAME}/ (not failing):")
        for h in result.notes:
            print(f"  NOTE {h.format()}")
    if result.advisory:
        print("advisory (never fails), most frequent plain words:")
        print("\n".join(_advisory_summary(result.advisory)))
    strict_n = sum(1 for t in tokens if t.tier == "strict")
    print(
        f"scanned {result.files} files, {len(tokens)} tokens ({strict_n} strict): "
        f"{len(seen)} strict hit(s), {len(problems)} allowlist problem(s)"
    )
    return 1 if seen or problems else 0


if __name__ == "__main__":
    sys.exit(main())
