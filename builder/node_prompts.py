"""Prompt text for the conversation nodes, built from a PolicySpec.

Pure functions. Every sentence is derived from the spec (or is a generic instruction about how
to use the tools); a block whose spec section is missing is left out, nothing is invented.
The compiler passes a PromptCtx with the names it chose (tools, exit transitions, fields).
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field

from builder import prompts
from builder.spec import Action, Intent, OutcomeRef, PolicySpec, spec_to_dict
from builder.tools_gen import placeholder

STAGE_IDENTITY: str = "identity"
STAGE_NEGOTIATE: str = "negotiate"
IDENTITY_RULE_STAGES: tuple[str, ...] = ("opening", "third_party", "identity", "global")
NEGOTIATE_RULE_STAGES: tuple[str, ...] = ("debt", "negotiate", "closing", "global")

DATE_RULE_TEXT: dict[str, str] = {
    "last_business_day_this_week_or_next_if_today": (
        "the last business day of this week, or of next week if today is that day"
    ),
    "deadline": "the product's latest promise date",
    "today": "today",
    "next_business_day": "the next business day",
}
AMOUNT_RULE_TEXT: dict[str, str] = {
    "full_or_floor_if_too_much": (
        "the full amount, or the floor amount if the customer already said the full amount is too much"
    ),
    "floor": "the floor amount",
    "full": "the full amount",
}
READING_ORDER_TEXT: dict[str, str] = {
    "day_month_year": "day, then month, then year",
    "month_day_year": "month, then day, then year",
    "year_month_day": "year, then month, then day",
}


@dataclass
class PromptCtx:
    """Names the compiler chose, as the prompt builders need them."""

    tools: dict[str, str] = field(default_factory=dict)  # role -> tool name available in this node
    fields: dict[str, dict[str, str]] = field(default_factory=dict)  # role -> semantic -> body field
    exits: dict[tuple[str, str], str] = field(default_factory=dict)  # (outcome, reason or '') -> transition tool
    note_map: dict[tuple[str, str], str] = field(default_factory=dict)  # (outcome, reason or '') -> note reason
    queue_reason: dict[str, str] = field(default_factory=dict)  # transfer queue -> closing reason
    finalize_transition: str = ""
    callback_exit: str = ""
    no_time_exit: str = ""
    uses_notes: bool = False
    n_products: int = 1
    note_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------

_SLOT_RE = re.compile(r"\{(\w+)\}")


def _slot_names(spec: PolicySpec) -> dict[str, str]:
    cv = spec.call_variables
    if cv is None:
        return {}
    names: dict[str, str] = {"customer_id": cv.customer_id, "now": cv.now}
    for key in ("first_name", "last_name", "title"):
        value = getattr(cv, key)
        if value:
            names[key] = value
    for extra in cv.extra:
        names.setdefault(extra.name, extra.name)
    return names


def fill_call_slots(template: str, spec: PolicySpec) -> str:
    """Replace `{title}`-style slots by the placeholder of the matching call variable.

    Slots the spec's call variables do not know stay as written.
    """
    names = _slot_names(spec)

    def swap(m: re.Match[str]) -> str:
        name = names.get(m.group(1))
        return placeholder(name) if name else m.group(0)

    return _SLOT_RE.sub(swap, template)


def _fill(text: str | None, spec: PolicySpec) -> str:
    return fill_call_slots(text, spec) if text else ""


def _ref(ref: OutcomeRef | None) -> tuple[str, str] | None:
    return (ref.outcome, ref.reason or "") if ref is not None else None


def _quote(text: str) -> str:
    return '"' + text + '"'


def _goodbye(spec: PolicySpec) -> str:
    g = spec.closing.goodbye_before_close
    return g.text if g is not None and g.text else ""


def _say_goodbye(spec: PolicySpec) -> str:
    text = _goodbye(spec)
    return "say the goodbye text (" + _quote(text) + ")" if text else "say goodbye"


def _sentence(text: str) -> str:
    text = text.rstrip()
    return text if text.endswith((".", "?", "!")) else text + "."


def _times(n: int | None) -> str:
    if n is None:
        return ""
    return "once" if n == 1 else str(n) + " times"


def _section(title: str, lines: list[str]) -> str:
    lines = [x for x in lines if x]
    if not lines:
        return ""
    return title + "\n" + "\n".join("- " + x for x in lines)


def _join(blocks: list[str]) -> str:
    return "\n\n".join(b for b in blocks if b)


def _strings(value: object) -> list[str]:
    """Every string inside nested dict/list data."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def note_reason_for_close(spec: PolicySpec, uses_notes: bool, outcome: str, reason: str | None) -> str:
    """Note reason that replaces a close action in multi-product mode ('' = it is a real exit).

    A close whose outcome is one the note aggregation produces (all notes equal, or the first
    note that differs) is expressed as a note per product instead of an exit.
    """
    mp = spec.multi_product
    if not (uses_notes and mp.enabled and mp.notes is not None and mp.notes.for_products_without_promise):
        return ""
    agg = spec.closing.aggregation
    if agg is None:
        return ""
    reasons = mp.notes.reasons
    if agg.first_note_not_equal is not None and outcome == agg.first_note_not_equal.outcome:
        return reason if reason and (not reasons or reason in reasons) else ""
    if agg.all_notes_equal is not None and outcome == agg.all_notes_equal.outcome and not reason:
        return agg.all_notes_equal.note_reason
    return ""


# ---------------------------------------------------------------------------------------------
# System prompt, opening
# ---------------------------------------------------------------------------------------------


def system_prompt(spec: PolicySpec) -> str:
    """Workflow-level prompt: who speaks and the facts-only rule."""
    m = spec.meta
    lines = [f"You are {m.agent_identity}, speaking on the phone for {m.bank_name}."]
    lines.append(f"Speak in the language with code {m.language}.")
    if m.currency:
        lines.append(f"Amounts are in {m.currency}.")
    lines.append("Keep every message short and ask one thing at a time.")
    lines.append(
        "Use only values that appear in the facts of the current step or in tool results. "
        "Never invent figures, dates or reasons."
    )
    lines.append("Follow the instructions of the current step exactly.")
    return " ".join(lines)


def opening_text(spec: PolicySpec) -> str:
    """The fixed first line: first sentence plus the confirmation question ('' when neither)."""
    op = spec.opening
    if op is None:
        return ""
    parts = []
    if op.first_sentence is not None and op.first_sentence.template:
        parts.append(_fill(op.first_sentence.template, spec))
    if op.confirmation_question:
        parts.append(_fill(op.confirmation_question, spec))
    return " ".join(parts)


# ---------------------------------------------------------------------------------------------
# Actions and intents
# ---------------------------------------------------------------------------------------------


def _tool(ctx: PromptCtx, role: str) -> str:
    return ctx.tools.get(role, "")


def _exit_line(spec: PolicySpec, ctx: PromptCtx, outcome: str, reason: str | None) -> str:
    note = ctx.note_map.get((outcome, reason or ""))
    note_tool = _tool(ctx, "add_note")
    if note and note_tool:
        return (
            "for the current product call " + note_tool + " with reason " + _quote(note)
            + "; do the same for every product that has no promise yet; when all products are handled, "
            + _say_goodbye(spec) + " and call " + ctx.finalize_transition
        )
    exit_tool = ctx.exits.get((outcome, reason or ""))
    if exit_tool:
        return _say_goodbye(spec) + ", then call " + exit_tool
    return "end the call as the instructions say"


def render_actions(actions: list[Action], ctx: PromptCtx, spec: PolicySpec, depth: int = 0) -> list[str]:
    """One line per action (nested actions are indented). A close right after a transfer is
    implied by the transfer tool and is not repeated."""
    out: list[str] = []
    pad = "  " * depth
    skip_close = False
    for a in actions:
        t = a.type
        if t == "close" and skip_close:
            skip_close = False
            continue
        skip_close = False
        if t in ("say", "say_once", "explain_once"):
            once = " once" if t != "say" else ""
            out.append(pad + "Say" + once + ": " + _quote(_fill(a.text, spec)))
        elif t == "sympathy":
            out.append(pad + "Express sympathy: " + _quote(_fill(a.text, spec)))
        elif t == "no_persuasion":
            out.append(pad + "Do not use persuasion attempts.")
        elif t == "skip_promise":
            out.append(pad + "Do not ask for a promise.")
        elif t == "continue":
            out.append(pad + "Then continue where you were.")
        elif t == "record_customer_proposal":
            out.append(pad + "If the customer names a date or an amount, record it under the date and amount rules.")
        elif t == "ask_once":
            out.append(pad + "Ask once: " + _quote(_fill(a.question, spec)))
            if a.then:
                out.append(pad + "If the customer still wants this after your question:")
                out.extend(render_actions(a.then, ctx, spec, depth + 1))
        elif t == "offer_transfer":
            out.append(pad + "Ask: " + _quote(_fill(a.question, spec)))
            if a.on_accept:
                out.append(pad + "If the customer accepts:")
                out.extend(render_actions(a.on_accept, ctx, spec, depth + 1))
            if a.on_decline:
                out.append(pad + "If the customer declines:")
                out.extend(render_actions(a.on_decline, ctx, spec, depth + 1))
        elif t == "transfer":
            tool = _tool(ctx, "transfer_to_agent")
            if tool:
                qf = ctx.fields.get("transfer_to_agent", {}).get("queue", "queue")
                out.append(
                    pad + "Call " + tool + " with " + qf + " " + _quote(a.queue or "")
                    + ". After the tool result says ok, " + _say_goodbye(spec)
                    + " and call " + ctx.finalize_transition + "."
                )
                skip_close = True
            else:
                out.append(pad + "The transfer is not available; tell the customer you cannot do that.")
        elif t == "add_note":
            tool = _tool(ctx, "add_note")
            if tool:
                out.append(pad + "Call " + tool + " with reason " + _quote(a.reason or "") + " for the current product.")
        elif t == "close":
            out.append(pad + "Then " + _exit_line(spec, ctx, a.outcome or "", a.reason) + ".")
    return out


def _intent_block(intent: Intent, ctx: PromptCtx, spec: PolicySpec) -> str:
    head = "Situation " + _quote(intent.name) + ". It applies when the customer says or means: " + _sentence("; ".join(intent.triggers))
    if intent.occurrences_before_action > 1:
        head += " Act on it only when the customer has said it " + str(intent.occurrences_before_action) + " times."
    lines = [head]
    lines += ["  " + x for x in render_actions(intent.actions, ctx, spec)]
    if intent.notes:
        lines.append("  Note: " + intent.notes)
    return "\n".join(lines)


def _intents_text(spec: PolicySpec, ctx: PromptCtx, stage: str) -> str:
    if stage == STAGE_IDENTITY:
        chosen = [i for i in spec.intents if i.applies_before_verification]
    else:
        chosen = [i for i in spec.intents if i.applies_after_verification]
    if not chosen:
        return ""
    chosen = sorted(chosen, key=lambda i: i.precedence)
    blocks = [_intent_block(i, ctx, spec) for i in chosen]
    head = "Situations that can come up. When several apply, the one listed first wins:"
    return head + "\n" + "\n".join(blocks)


# ---------------------------------------------------------------------------------------------
# Mandated sentences and verbatim rules
# ---------------------------------------------------------------------------------------------


def _stage_strings(spec: PolicySpec, stage: str) -> list[str]:
    data = spec_to_dict(spec)
    if stage == STAGE_IDENTITY:
        parts: list[object] = [data.get("opening"), data.get("third_party"), data.get("identity")]
        parts.append([i for i in data.get("intents", []) if i.get("applies_before_verification")])
    else:
        parts = [
            data.get("debt_statement"), data.get("amounts"), data.get("promise_rules"),
            data.get("persuasion"), data.get("multi_product"), data.get("calendar"),
            [i for i in data.get("intents", []) if i.get("applies_after_verification", True)],
        ]
    return _strings(parts)


def _mandated(spec: PolicySpec, stage: str) -> str:
    """Mandated sentences that occur in this stage's spec sections; unmatched ones go to negotiate."""
    mine = _stage_strings(spec, stage)
    other = _stage_strings(spec, STAGE_NEGOTIATE if stage == STAGE_IDENTITY else STAGE_IDENTITY)
    picked = []
    for m in spec.mandated_sentences:
        here = any(m.text in s for s in mine)
        there = any(m.text in s for s in other)
        if here or (stage == STAGE_NEGOTIATE and not there):
            picked.append(m)
    if not picked:
        return ""
    return prompts.render_mandated_sentences(dataclasses.replace(spec, mandated_sentences=picked))


def _rules(spec: PolicySpec, stages: tuple[str, ...]) -> str:
    return _join([prompts.render_verbatim_rules(spec, s) for s in stages])


# ---------------------------------------------------------------------------------------------
# Identity step
# ---------------------------------------------------------------------------------------------


def _exit_call(ctx: PromptCtx, ref: OutcomeRef | None) -> str:
    key = _ref(ref)
    name = ctx.exits.get(key, "") if key else ""
    return name


def _opening_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    op = spec.opening
    if op is None:
        return []
    lines: list[str] = []
    if op.first_sentence is not None and op.first_sentence.template:
        lines.append("The first sentence has already been said: " + _quote(_fill(op.first_sentence.template, spec)))
    if op.confirmation_question:
        lines.append("The confirmation question has already been asked: " + _quote(_fill(op.confirmation_question, spec)))
    if op.forbidden_before_verification:
        scope = " to anyone" if op.applies_to_everyone else ""
        lines.append(
            "Until the customer is verified, never say these words" + scope + ": "
            + ", ".join(op.forbidden_before_verification) + "."
        )
    if op.confirmation_accepted:
        lines.append("These answers confirm the person is the customer: " + "; ".join(op.confirmation_accepted) + ".")
    if op.confirmation_not_accepted:
        lines.append("These answers do not confirm it: " + "; ".join(op.confirmation_not_accepted) + ".")
    if op.max_confirmation_asks is not None:
        text = "Ask the confirmation question at most " + _times(op.max_confirmation_asks) + "."
        unclear = _exit_call(ctx, op.on_unclear)
        if unclear:
            text += " If it is still unclear, " + _say_goodbye(spec) + " and call " + unclear + "."
        lines.append(text)
    if op.answer_question_first:
        lines.append("If the customer asks a question, answer it first, then ask the confirmation question again.")
    if op.ai_question_answer:
        lines.append("If asked whether you are an AI or a robot, say: " + _quote(_fill(op.ai_question_answer, spec)))
    if op.scam_question_answer:
        lines.append("If asked whether this call is a scam, say: " + _quote(_fill(op.scam_question_answer, spec)))
    return lines


def _third_party_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    tp = spec.third_party
    if tp is None:
        return []
    lines: list[str] = []
    if tp.say_nothing_beyond_opening:
        lines.append("If the person is not the customer, say nothing beyond the opening sentence.")
    if tp.ask_when_reachable:
        lines.append("Ask whether the customer can come to the phone.")
    cw = tp.callback_window
    cb_tool = _tool(ctx, "schedule_callback")
    if cw is not None:
        days = cw.days
        if days.n is None:
            span = "today or the next business days"
        elif days.n == 1:
            span = "today or the next business day"
        else:
            span = "today or one of the next " + str(days.n) + " business days"
        text = "A callback can be booked for " + span + ", between " + cw.hour_start + " and " + cw.hour_end
        if cw.inclusive is True:
            text += " (both times included)"
        elif cw.inclusive is False:
            text += " (the end time is not included)"
        if cw.must_be_after_now:
            text += "; the time must be later than now"
        lines.append(text + ". The exact dates are in the date facts.")
        if cb_tool:
            f = ctx.fields.get("schedule_callback", {})
            lines.append(
                "To book it, call " + cb_tool + " with " + f.get("date", "date") + " as YYYY-MM-DD and "
                + f.get("time", "time") + " as HH:MM."
            )
            if tp.reasks_when_outside is not None:
                lines.append(
                    "If the time is outside the window, ask for another time, at most " + _times(tp.reasks_when_outside) + "."
                )
            if ctx.callback_exit:
                lines.append(
                    "When the tool result says the callback is recorded, tell the contact, "
                    + _say_goodbye(spec) + " and call " + ctx.callback_exit + "."
                )
            if ctx.no_time_exit:
                lines.append(
                    "If no valid time is found, " + _say_goodbye(spec) + " and call " + ctx.no_time_exit + "."
                )
    wrong = _exit_call(ctx, tp.on_not_customers_number)
    if wrong:
        lines.append("If the person says this number is not the customer's, " + _say_goodbye(spec) + " and call " + wrong + ".")
    dec = tp.on_customer_deceased
    if dec is not None:
        text = "If the person reports that the customer is deceased"
        if dec.sentence:
            text += ", say " + _quote(_fill(dec.sentence, spec))
        if dec.ask_nothing_else:
            text += " and ask nothing else"
        name = _exit_call(ctx, dec.outcome)
        if name:
            text += ", then " + _say_goodbye(spec) + " and call " + name
        lines.append(text + ".")
    if tp.others_may_answer_security is False:
        lines.append("Another person may not answer the security questions.")
    if tp.others_may_hear_account is False:
        lines.append("Another person may not hear anything about the account.")
    return lines


def _answer_format_text(q) -> str:  # noqa: ANN001 - IdentityQuestion
    f = q.answer_format
    if f is None:
        return ""
    parts = []
    if f.kind == "date":
        parts.append("a date" + (" as " + f.output_format if f.output_format else ""))
    elif f.kind == "digits":
        parts.append("exactly " + str(f.digits) + " digits" if f.digits else "digits only")
    elif f.kind == "number":
        parts.append("a number")
    if f.numeric_reading_order:
        parts.append(
            "a date said only in numbers is read " + READING_ORDER_TEXT.get(f.numeric_reading_order, f.numeric_reading_order)
        )
    if f.notes:
        parts.append(f.notes.rstrip().rstrip("."))
    return "; ".join(parts)


def _identity_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    idn = spec.identity
    if idn is None:
        return []
    tool = _tool(ctx, "verify_identity")
    f = ctx.fields.get("verify_identity", {})
    qf, af = f.get("question", "field"), f.get("answer", "answer")
    lines = []
    for i, q in enumerate(idn.questions, start=1):
        text = "Question " + str(i) + ", asked after the customer is confirmed: " + _quote(_fill(q.prompt, spec))
        text += ". Check the answer by calling " + tool + " with " + qf + " " + _quote(q.field) + " and " + af + " the customer's answer"
        fmt = _answer_format_text(q)
        if fmt:
            text += " (format: " + fmt + ")"
        lines.append(text + ".")
    if idn.one_at_a_time:
        lines.append("Ask one question at a time. Call " + tool + " for each answer before you ask the next question.")
    if idn.wrong_may_be_reasked:
        lines.append("A wrong answer may be asked again.")
    lines.append(
        "After " + str(idn.max_wrong_total) + " wrong answers in total the check fails. "
        "The tool result tells you what to do next; the call moves on by itself when the check ends."
    )
    for h in idn.hints:
        counts = "counts as a wrong answer" if h.counts_as_wrong else "does not count as a wrong answer"
        lines.append("If the tool result hint is " + _quote(h.value) + ": " + h.instruction + " This " + counts + ".")
    fail = idn.on_failure
    if fail is not None:
        text = "If the check fails"
        if fail.say:
            text += " the call says " + _quote(_fill(fail.say, spec)) + " by itself"
        if fail.disclose_debt is False:
            text += "; never mention the debt"
        lines.append(text + ".")
    dec = idn.decline
    if dec is not None:
        text = "If the customer refuses the security questions"
        if dec.explain_once:
            text += ", explain once: " + _quote(_fill(dec.explain_once, spec))
        if dec.asking_purpose_counts_as_decline:
            text += ". Asking why the questions are needed counts as a refusal"
        if dec.max_refusals is not None:
            text += ". After " + str(dec.max_refusals) + " refusals"
            name = _exit_call(ctx, dec.outcome)
            if name:
                text += ", " + _say_goodbye(spec) + " and call " + name
        lines.append(text + ".")
    return lines


def identity_task(spec: PolicySpec, ctx: PromptCtx) -> str:
    """Task message of the identity node."""
    m = spec.meta
    intro = (
        "You are " + m.agent_identity + ". This step confirms who is on the phone and verifies the customer. "
        "Do not talk about the account in this step."
    )
    opening = _section("Opening:", _opening_lines(spec, ctx))
    third = _section("If the person is not the customer:", _third_party_lines(spec, ctx))
    return _join(
        [
            intro,
            opening,
            third,
            _section("Security questions (ask them in this order):", _identity_lines(spec, ctx)),
            _intents_text(spec, ctx, STAGE_IDENTITY),
            _mandated(spec, STAGE_IDENTITY),
            _rules(spec, IDENTITY_RULE_STAGES),
            prompts.render_tool_rules(),
        ]
    )


# ---------------------------------------------------------------------------------------------
# Negotiate step
# ---------------------------------------------------------------------------------------------


def _debt_lines(spec: PolicySpec) -> list[str]:
    d = spec.debt_statement
    if d is None:
        return []
    lines = []
    if d.intro:
        lines.append("Start by saying: " + _quote(_fill(d.intro, spec)))
    if d.state_fields:
        names = []
        for f in d.state_fields:
            if spec.amounts is not None and f == spec.amounts.full_amount_field:
                names.append("the full amount")
            elif f == spec.account_fields.days_late:
                names.append("the days late")
            else:
                names.append(f)
        who = "each product" if d.one_sentence_per_product else "the products"
        text = "For " + who + ", state its spoken name and " + ", ".join(names) + " (values are in the product facts)"
        if d.one_sentence_per_product:
            text += ", one sentence per product"
        lines.append(text + ".")
    return lines


def _ask_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    pr, mp = spec.promise_rules, spec.multi_product
    lines = []
    if mp.enabled:
        if mp.state_all_before_asking:
            lines.append("State all products first, then ask.")
        if mp.ask_all_today_first:
            lines.append("Ask for all products to be paid today first, in account order. Do not ask again for a product that already got an answer about today.")
        if mp.order == "account_order":
            lines.append("Handle products in the order of the product facts.")
        if mp.per_product_attempts:
            lines.append("Persuasion attempts are counted per product.")
    if pr is not None and pr.first_ask:
        lines.append("First ask: " + _quote(_fill(pr.first_ask, spec)))
    return lines


def _amount_lines(spec: PolicySpec) -> list[str]:
    am = spec.amounts
    if am is None:
        return []
    lines = []
    if am.ask_full_first:
        lines.append("Offer the full amount first.")
    if am.never_ask_customer_amount:
        lines.append("Never ask the customer how much they can pay.")
    for q in am.forbidden_questions:
        lines.append("Never ask: " + _quote(q))
    tm = am.too_much
    if tm is not None:
        text = "If the customer says the full amount is too much, offer the floor amount"
        if tm.offer_floor_times is not None:
            text += " (" + _times(tm.offer_floor_times) + ")"
        if tm.say_rest_stays_overdue:
            text += " and say the rest stays overdue"
        if tm.refusal_is_flat_refusal:
            text += ". A refusal of the floor amount is a flat refusal"
        lines.append(text + ".")
    if am.accept_between_floor_and_full:
        lines.append("If the customer names an amount between the floor amount and the full amount, accept it.")
    bf = am.below_floor
    if bf is not None:
        text = "If the customer names an amount below the floor amount"
        if bf.say:
            text += ", say " + _quote(_fill(bf.say, spec))
        if bf.offer_floor_times is not None:
            text += " and offer the floor amount (" + _times(bf.offer_floor_times) + ")"
        if bf.refusal_is_flat_refusal:
            text += ". A refusal is a flat refusal"
        lines.append(text + ".")
    if am.above_full == "offer_full":
        lines.append("If the customer names an amount above the full amount, offer the full amount.")
    elif am.above_full == "accept_full":
        lines.append("If the customer names an amount above the full amount, accept only the full amount.")
    return lines


def _date_lines(spec: PolicySpec) -> list[str]:
    pr, cal = spec.promise_rules, spec.calendar
    lines = []
    if pr is not None:
        text = "A promise date is"
        text += " today or later" if pr.earliest == "today" else ""
        if pr.must_be_business_day:
            text += ", a business day"
        if pr.latest == "deadline":
            text += ", and not after the product's latest promise date"
        lines.append(text + ". Business days exclude " + ", ".join(cal.weekend_days) + " and the holidays in the date facts.")
        if pr.on_non_business_day == "offer_next_business_day":
            lines.append("If the customer names a day that is not a business day, offer the next business day.")
        elif pr.on_non_business_day == "reject":
            lines.append("If the customer names a day that is not a business day, do not accept it and ask for another day.")
        if pr.on_after_deadline == "offer_deadline":
            lines.append("If the customer names a day after the latest promise date, offer the latest promise date.")
        elif pr.on_after_deadline == "reject":
            lines.append("If the customer names a day after the latest promise date, do not accept it.")
        if pr.refusing_deadline_is_flat_refusal:
            lines.append("Refusing the latest promise date is a flat refusal.")
    ws = cal.weekday_semantics
    if ws is not None:
        if ws.next == "following_calendar_week":
            lines.append("'Next <weekday>' means that weekday in the calendar week after the current one.")
        elif ws.next == "next_occurrence":
            lines.append("'Next <weekday>' means the next time that weekday comes after today.")
        if ws.this == "current_calendar_week":
            lines.append("'This <weekday>' means that weekday in the current calendar week.")
        elif ws.this == "next_occurrence_inclusive":
            lines.append("'This <weekday>' means the next time that weekday comes, today included.")
        lines.append("The weeks start on " + cal.week_starts_on + ". The date facts list what each of these means today.")
    vp = cal.vague_period
    if vp is not None:
        text = "If the customer gives a vague period such as 'next month'"
        if vp.ask_exact_day_times is not None:
            text += ", ask for an exact day (" + _times(vp.ask_exact_day_times) + ")"
        then = {
            "propose_deadline": "then propose the latest promise date",
            "propose_attempt_date": "then propose the date of the current attempt",
            "treat_as_refusal": "then treat it as a refusal",
        }.get(vp.then or "", "")
        if then:
            text += ", " + then
        lines.append(text + ".")
    return lines


def _persuasion_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    p = spec.persuasion
    if p is None:
        return []
    lines = []
    if p.attempts:
        lines.append("The number of attempts for each product is 'persuasion attempts' in its product facts. The offer of attempt N is its 'attempt N offer' line; use exactly those values.")
    for a in sorted(p.attempts, key=lambda x: x.index):
        text = "Attempt " + str(a.index) + ": date " + DATE_RULE_TEXT.get(a.date_rule, a.date_rule) + "; amount " + AMOUNT_RULE_TEXT.get(a.amount_rule, a.amount_rule)
        if a.bands:
            text += " (only for bands: " + ", ".join(a.bands) + ")"
        if a.note:
            text += ". " + a.note
        lines.append(_sentence(text))
    if p.flat_refusal_definition:
        lines.append(p.flat_refusal_definition)
    if p.customer_proposal_is_never_refusal:
        lines.append("A date or amount the customer proposes is never a refusal.")
    if p.do_not_repeat_refused_offer:
        lines.append("Do not repeat an offer the customer already refused.")
    ex = p.on_exhausted
    if ex is not None:
        note = ctx.note_map.get((ex.outcome, ex.reason or ""))
        note_tool = _tool(ctx, "add_note")
        if note and note_tool:
            lines.append(
                "When the attempts are used up, call " + note_tool + " with reason " + _quote(note)
                + " for the current product and do not repeat an offer."
            )
        else:
            name = _exit_call(ctx, ex)
            if name:
                lines.append("When the attempts are used up, " + _say_goodbye(spec) + " and call " + name + ".")
    return lines


def _promise_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    pr = spec.promise_rules
    tool = _tool(ctx, "create_promise")
    if pr is None or not tool:
        return []
    f = ctx.fields.get("create_promise", {})
    lines = []
    rb = pr.readback
    if rb is not None:
        if rb.template:
            lines.append("Read the promise back with this template, replacing each slot with its value: " + _quote(rb.template))
        if rb.confirmation_question:
            lines.append("Then ask: " + _quote(_fill(rb.confirmation_question, spec)))
        if rb.require_clear_yes:
            lines.append("Record the promise only after a clear yes.")
        if rb.recheck_on_change:
            lines.append("If the customer changes anything after the read-back, read it back again.")
    lines.append(
        "To record a promise call " + tool + " with " + f.get("product", "product_id") + " (the product id from the product facts), "
        + f.get("amount", "amount") + " and " + f.get("date", "date") + " as YYYY-MM-DD."
    )
    if pr.one_promise_per_product:
        lines.append("Record one promise per product.")
    if pr.allow_change_or_cancel is False:
        text = "A recorded promise cannot be changed or cancelled."
        if pr.change_request_answer:
            text += " If asked, say: " + _quote(_fill(pr.change_request_answer, spec))
        lines.append(text)
    if pr.say_recorded_only_after_ok:
        lines.append("Say a promise is recorded only after the tool result says ok true.")
    if pr.retries_on_failure is not None:
        lines.append("If the tool result says the promise was not recorded, do what it says.")
    if pr.success_sentence:
        lines.append("After the promise is recorded, say: " + _quote(_fill(pr.success_sentence, spec)))
    if pr.on_failure is not None and pr.on_failure.say:
        lines.append("If the promise cannot be recorded in the end, the call says " + _quote(_fill(pr.on_failure.say, spec)) + " by itself.")
    if pr.forbidden_offers:
        lines.append("Never offer: " + ", ".join(pr.forbidden_offers) + ".")
    return lines


def _multi_lines(spec: PolicySpec, ctx: PromptCtx) -> list[str]:
    mp = spec.multi_product
    if not mp.enabled:
        return []
    lines = ["The account can have up to " + str(mp.max_products) + " products; handle each product."]
    if mp.one_date_for_all == "cap_each_to_own_deadline":
        lines.append("One date can be used for all products. A product whose latest promise date is earlier gets its own latest promise date.")
    elif mp.one_date_for_all == "reject_if_any_after_deadline":
        lines.append("One date can be used for all products only if no product has an earlier latest promise date; otherwise do not accept it.")
    if mp.say_capped_in_readback:
        lines.append("In the read-back, say when a date was capped to a product's own latest promise date.")
    if mp.combined_readback:
        lines.append("Read all promises back together and ask for one confirmation.")
    note_tool = _tool(ctx, "add_note")
    if mp.notes is not None and note_tool:
        f = ctx.fields.get("add_note", {})
        if mp.notes.for_products_without_promise:
            text = "For every product that ends without a promise, call " + note_tool + " with " + f.get("product", "product_id") + " and " + f.get("reason", "reason") + " (one of: " + ", ".join(ctx.note_reasons or mp.notes.reasons) + ")"
            lines.append(text + ".")
        if mp.notes.also_when_none_promised:
            lines.append("Notes are also needed when no product got a promise.")
    if mp.claim_on_one_product_continues:
        lines.append("If the customer makes a claim about one product, continue with the other products.")
    ot = mp.on_transfer
    if ot is not None:
        if ot.add_no_notes:
            lines.append("After a transfer, add no notes.")
        if ot.keep_recorded_promises:
            lines.append("Promises already recorded stay.")
        if ot.drop_unrecorded_promises:
            lines.append("Promises not yet recorded are dropped when the call is transferred.")
    return lines


def negotiate_task(spec: PolicySpec, ctx: PromptCtx) -> str:
    """Task message of the negotiate node."""
    m = spec.meta
    intro = (
        "You are " + m.agent_identity + ". The customer is verified. This step states the debt and agrees on promises."
    )
    facts = (
        "Facts for this call. Use only these values:\n"
        + placeholder("date_facts") + "\n\n" + placeholder("product_facts")
    )
    finish_bits = []
    if ctx.finalize_transition:
        finish_bits.append(
            "When every product is handled (a promise or a note) or the call is transferred, "
            + _say_goodbye(spec) + " and call " + ctx.finalize_transition + "."
        )
    return _join(
        [
            intro,
            _section("Debt statement:", _debt_lines(spec)),
            facts,
            _section("Asking:", _ask_lines(spec, ctx)),
            _section("Amounts:", _amount_lines(spec)),
            _section("Dates:", _date_lines(spec)),
            _section("Persuasion:", _persuasion_lines(spec, ctx)),
            _section("Promises:", _promise_lines(spec, ctx)),
            _section("Several products:", _multi_lines(spec, ctx)),
            _intents_text(spec, ctx, STAGE_NEGOTIATE),
            "\n".join(finish_bits),
            _mandated(spec, STAGE_NEGOTIATE),
            _rules(spec, NEGOTIATE_RULE_STAGES),
            prompts.render_tool_rules(),
        ]
    )
