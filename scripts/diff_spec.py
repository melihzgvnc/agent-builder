"""Semantic diff of key facts between an expected and an extracted PolicySpec.

usage: uv run python scripts/diff_spec.py <expected_spec.json> <extracted_spec.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def get(d: Any, *path: Any) -> Any:
    for p in path:
        try:
            d = d[p]
        except (KeyError, IndexError, TypeError):
            return None
    return d


def _bands(s: dict) -> Any:
    return [
        (b.get("name"), b.get("days_late_min"), b.get("days_late_max"), b.get("deadline"), b.get("persuasion_attempts"))
        for b in s.get("bands", [])
    ]


def _identity(s: dict) -> Any:
    return [q.get("field") for q in get(s, "identity", "questions") or []]


def _persuasion(s: dict) -> Any:
    return [(a.get("index"), a.get("date_rule"), a.get("amount_rule"), a.get("bands")) for a in get(s, "persuasion", "attempts") or []]


def _outcomes(s: dict) -> Any:
    return sorted((r.get("outcome"), r.get("reason")) for r in get(s, "closing", "outcome_table") or [])


def _intents(s: dict) -> Any:
    return sorted(i.get("name") for i in s.get("intents", []))


def _mandated(s: dict) -> Any:
    return sorted(m.get("text", "").strip().lower() for m in s.get("mandated_sentences", []))


FACTS: list[tuple[str, Any]] = [
    ("calendar.weekend_days", lambda s: sorted(get(s, "calendar", "weekend_days") or [])),
    ("calendar.holidays", lambda s: sorted(get(s, "calendar", "holidays") or [])),
    ("calendar.weekday_semantics", lambda s: get(s, "calendar", "weekday_semantics")),
    ("bands (name,min,max,deadline,attempts)", _bands),
    ("identity question order (fields)", _identity),
    ("identity.max_wrong_total", lambda s: get(s, "identity", "max_wrong_total")),
    ("amounts.full_amount_field", lambda s: get(s, "amounts", "full_amount_field")),
    ("amounts.floor_fields", lambda s: get(s, "amounts", "floor_fields")),
    ("persuasion sequence", _persuasion),
    ("outcome table rows", _outcomes),
    ("intents (names)", _intents),
    ("multi_product.enabled/max", lambda s: (get(s, "multi_product", "enabled"), get(s, "multi_product", "max_products"))),
    ("multi_product.notes.reasons", lambda s: sorted(get(s, "multi_product", "notes", "reasons") or [])),
    ("mandated sentences", _mandated),
]


def _fmt(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/diff_spec.py <expected_spec.json> <extracted_spec.json>", file=sys.stderr)
        return 2
    exp = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    got = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    same = 0
    for name, fn in FACTS:
        a, b = fn(exp), fn(got)
        if a == b:
            same += 1
            print(f"[same] {name}")
            continue
        print(f"[DIFF] {name}")
        if isinstance(a, list) and isinstance(b, list):
            only_a = [x for x in a if x not in b]
            only_b = [x for x in b if x not in a]
            if only_a or only_b:
                for x in only_a:
                    print(f"    expected only: {_fmt(x)}")
                for x in only_b:
                    print(f"    extracted only: {_fmt(x)}")
            else:
                print(f"    order differs\n    expected:  {_fmt(a)}\n    extracted: {_fmt(b)}")
        else:
            print(f"    expected:  {_fmt(a)}\n    extracted: {_fmt(b)}")
    print(
        f"\n{same}/{len(FACTS)} key facts identical; "
        f"verbatim_rules expected={len(exp.get('verbatim_rules', []))} extracted={len(got.get('verbatim_rules', []))}; "
        f"warnings extracted={len(got.get('warnings', []))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
