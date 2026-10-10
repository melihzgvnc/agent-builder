"""Prompt text helpers: plain-text blocks rendered from a loaded PolicySpec.

Pure functions. Every wording that depends on a brief comes from the spec argument; the only
fixed text is render_tool_rules(), which is brief-independent. Each renderer returns an empty
string when the spec has nothing to render, so callers can join blocks without checks.
"""

from __future__ import annotations

from builder.spec import PolicySpec


def render_verbatim_rules(spec: PolicySpec, stage: str) -> str:
    """Numbered list of the spec's verbatim rules for one stage, under a short heading."""
    rules = [r for r in spec.verbatim_rules if r.stage == stage]
    if not rules:
        return ""
    lines = [f"Rules for this step ({stage}). Follow them exactly:"]
    lines += [f"{i}. {r.text}" for i, r in enumerate(rules, start=1)]
    return "\n".join(lines)


def render_mandated_sentences(spec: PolicySpec, when: str | None = None) -> str:
    """Sentences the agent must say word for word; `when` keeps only that trigger."""
    items = [m for m in spec.mandated_sentences if when is None or m.when == when]
    if not items:
        return ""
    lines = ["Say these sentences word for word:"]
    lines += [f'- {m.when}: "{m.text}"' for m in items]
    return "\n".join(lines)


def render_outcome_table(spec: PolicySpec) -> str:
    """The closing table as plain text rows: outcome, reason, when it applies."""
    rows = spec.closing.outcome_table
    if not rows:
        return ""
    lines = ["Closing outcomes (use the row that matches the call):"]
    for row in rows:
        reason = row.reason if row.reason else "none"
        lines.append(f"- outcome: {row.outcome} | reason: {reason} | when: {row.when}")
    return "\n".join(lines)


def render_tool_rules() -> str:
    """Generic tool-use rules for the phone model. Independent of any brief."""
    return "\n".join(
        [
            "Tool rules:",
            "1. Call one tool per message.",
            "2. After every tool result, speak to the customer or call the next tool. Never end a turn with nothing.",
            "3. When a rule says to use a tool, call the tool. Saying it in words is not enough.",
            "4. Never say that something is recorded or done unless the tool result says ok true.",
            "5. If a tool result carries what_to_do_next, do what it says.",
        ]
    )
