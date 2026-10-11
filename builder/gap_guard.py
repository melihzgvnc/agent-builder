"""Gap guard: finds policy rules that exist only as prose although the schema has a field for them.

The extraction model sometimes cannot (or does not) map a formula into the structured fields of the
spec and parks it in `verbatim_rules` or admits it in `warnings`. The engine, the facts and the
hooks only see the structured fields, so such a rule would be silently ignored at run time.
find_unstructured(spec_dict) reports each kind of such rule once. It never raises and never fails
a build: the compiler records the result as gaps and as degraded.

Two signals, strongest first:
  1. `verbatim_rules[].gap_kind` set by the extractor, while the structural field of that kind is unused.
  2. A cue-based safety net over `warnings` and `verbatim_rules` text. A warning counts only when it
     contains an admission cue (the extractor says it could not express something), a kind cue and a
     detail cue of the same kind. Words are schema vocabulary and generic English only.

Imports only `re`.
"""

from __future__ import annotations

import re

KIND_DEADLINE: str = "deadline_formula"
KIND_FLOOR: str = "amount_floor"
KIND_CALLBACK: str = "callback_window"
KIND_PERSUASION: str = "persuasion_date"

EVIDENCE_CHARS: int = 200
KIND_SUFFIX_BASE: str = "_after_base"
RULE_BUSINESS_DAYS_AFTER_TODAY: str = "business_days_after_today"

# Phrases with which an extractor admits that it could not express a rule in the schema.
ADMISSION_CUES: tuple[str, ...] = (
    "cannot express", "cannot be expressed", "can not express", "not representable", "no structured field",
    "no matching", "placeholder", "only in verbatim", "schema cannot", "schema has no", "schema holds one",
    "not an account field", "cannot go in",
)

WEEKDAY_NAMES: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
CLOCK_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\b")
PERCENT_PATTERN = re.compile(r"%|\bpercent")
FLOOR_WORD_PATTERN = re.compile(r"\b(floor|minimum)\b")


class Gap:
    """One rule kind that exists only as prose: kind, short evidence text, where it was found."""

    __slots__ = ("kind", "evidence", "source")

    def __init__(self, kind: str, evidence: str, source: str) -> None:
        self.kind = kind
        self.evidence = evidence
        self.source = source

    def __repr__(self) -> str:
        return f"Gap({self.kind!r}, {self.evidence!r}, {self.source!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Gap) and (self.kind, self.evidence, self.source) == (
            other.kind, other.evidence, other.source)

    def __hash__(self) -> int:
        return hash((self.kind, self.evidence, self.source))


# ---------------------------------------------------------------------------------------------
# Spec access (plain dicts; every access tolerates a missing or malformed part)
# ---------------------------------------------------------------------------------------------


def _dict(x: object) -> dict:
    return x if isinstance(x, dict) else {}


def _list(x: object) -> list:
    return x if isinstance(x, list) else []


def _words(text: str, cue: str) -> bool:
    """True when `cue` occurs in `text` (both lower case) starting at a word boundary."""
    return re.search(r"(?<![a-z0-9_])" + re.escape(cue), text) is not None


def _any_word(text: str, cues: tuple[str, ...]) -> bool:
    return any(_words(text, c) for c in cues)


def product_universe(spec: dict) -> list[str]:
    """Product types known to the spec: the debt statement names, floor_fields keys, floor_percent keys."""
    out: list[str] = []
    for pn in _list(_dict(spec.get("debt_statement")).get("product_names")):
        t = _dict(pn).get("product_type")
        if isinstance(t, str) and t and t not in out:
            out.append(t)
    am = _dict(spec.get("amounts"))
    for key in list(_dict(am.get("floor_fields"))) + list(_dict(am.get("floor_percent"))):
        if isinstance(key, str) and key and key not in out:
            out.append(key)
    return out


def floor_covered(spec: dict) -> bool:
    """Every product type has a floor source (or a default source exists). An empty universe is not covered."""
    am = _dict(spec.get("amounts"))
    if am.get("floor_default_field") or am.get("floor_default_percent"):
        return True
    universe = product_universe(spec)
    if not universe:
        return False
    sources = set(_dict(am.get("floor_fields"))) | set(_dict(am.get("floor_percent")))
    return all(t in sources for t in universe)


def _deadline_used(spec: dict) -> bool:
    return any(
        str(_dict(_dict(b).get("deadline")).get("kind", "")).endswith(KIND_SUFFIX_BASE)
        for b in _list(spec.get("bands"))
    )


def _callback_used(spec: dict) -> bool:
    window = _dict(_dict(spec.get("third_party")).get("callback_window"))
    return bool(_list(window.get("per_weekday")))


def _persuasion_used(spec: dict) -> bool:
    return any(
        _dict(a).get("date_rule") == RULE_BUSINESS_DAYS_AFTER_TODAY
        for a in _list(_dict(spec.get("persuasion")).get("attempts"))
    )


def _account_cues(spec: dict) -> tuple[str, ...]:
    af = _dict(spec.get("account_fields"))
    return tuple(v.lower() for v in (af.get("due_date"), af.get("days_late")) if isinstance(v, str) and v)


def _detail_deadline(text: str, spec: dict) -> bool:
    cues = ("due date", "due_date", "minus", "before today", "since the due") + _account_cues(spec)
    return any(c in text for c in cues)


def _detail_floor(text: str, spec: dict) -> bool:
    return PERCENT_PATTERN.search(text) is not None


def _detail_callback(text: str, spec: dict) -> bool:
    return CLOCK_PATTERN.search(text) is not None and _any_word(text, WEEKDAY_NAMES)


def _detail_persuasion(text: str, spec: dict) -> bool:
    return "business day" in text


# kind -> (is the structural field in use, kind cues, detail cue test). Order = report order.
KINDS: dict[str, tuple] = {
    KIND_DEADLINE: (_deadline_used, ("deadline", "latest promise date"), _detail_deadline),
    KIND_FLOOR: (floor_covered, ("floor", "minimum"), _detail_floor),
    KIND_CALLBACK: (_callback_used, ("callback", "window"), _detail_callback),
    KIND_PERSUASION: (_persuasion_used, ("attempt", "persuasion"), _detail_persuasion),
}


def satisfied(kind: str, spec: dict) -> bool:
    """True when the structured field of this kind is in use in the spec."""
    entry = KINDS.get(kind)
    return entry is None or bool(entry[0](spec))


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= EVIDENCE_CHARS else text[:EVIDENCE_CHARS]


def _warning_kinds(text: str, spec: dict) -> list[str]:
    """Kinds whose admission + kind cue + detail cue all occur in one warning."""
    low = text.lower()
    if not any(c in low for c in ADMISSION_CUES):
        return []
    return [k for k, (_used, cues, detail) in KINDS.items() if _any_word(low, cues) and detail(low, spec)]


def find_unstructured(spec: dict) -> list[Gap]:
    """One Gap per rule kind that is described in prose while its structural field is unused."""
    spec = _dict(spec)
    found: dict[str, Gap] = {}

    def add(kind: str, evidence: str, source: str) -> None:
        if kind in KINDS and kind not in found and not satisfied(kind, spec):
            found[kind] = Gap(kind, _cut(evidence), source)

    rules = [r for r in _list(spec.get("verbatim_rules")) if isinstance(r, dict)]
    for r in rules:
        kind = r.get("gap_kind")
        if isinstance(kind, str):
            add(kind, str(r.get("text", "")), "verbatim_rules.gap_kind")
    for w in _list(spec.get("warnings")):
        if not isinstance(w, str):
            continue
        for kind in _warning_kinds(w, spec):
            add(kind, w, "warnings")
    # Percent floors written only as prose: the floor wording and a percentage in one rule text.
    for r in rules:
        low = str(r.get("text", "")).lower()
        if FLOOR_WORD_PATTERN.search(low) and PERCENT_PATTERN.search(low):
            add(KIND_FLOOR, str(r.get("text", "")), "verbatim_rules")
    return [found[k] for k in KINDS if k in found]
