"""PolicySpec: JSON Schema validation, semantic checks and a typed dataclass loader.

The schema (spec_schema.json, next to this file) is generic: it holds no value from any brief.
Every value comes from the spec document being validated.

CLI: python -m builder.spec validate <spec.json>
    prints OK (exit 0), or one error per line (exit 1); unreadable file or bad JSON -> exit 2.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

SCHEMA_PATH: Path = Path(__file__).with_name("spec_schema.json")


# ---------------------------------------------------------------------------------------------
# Dataclasses (one per schema object). Required fields first, then optional fields with the
# schema default (or None / empty when the schema has no default). Only neutral structural
# fields have a default; every policy-bearing field is None when the spec leaves it out, so a
# gap stays visible instead of silently inheriting some brief's rule.
# ---------------------------------------------------------------------------------------------


@dataclass
class OutcomeRef:
    outcome: str
    reason: str | None = None


@dataclass
class Source:
    path: str
    order: int
    role: str | None = None
    summary: str | None = None


@dataclass
class Meta:
    bank_name: str
    agent_identity: str
    language: str = "en"
    currency: str | None = None
    sources: list[Source] = field(default_factory=list)


@dataclass
class ExtraVariable:
    name: str
    meaning: str | None = None


@dataclass
class CallVariables:
    customer_id: str
    now: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    extra: list[ExtraVariable] = field(default_factory=list)


@dataclass
class AccountFields:
    product_id: str
    product_type: str | None = None
    days_late: str | None = None
    products_key: str | None = None
    due_date: str | None = None


@dataclass
class FirstSentence:
    template: str | None = None
    must_include: list[str] = field(default_factory=list)


@dataclass
class Opening:
    first_sentence: FirstSentence | None = None
    forbidden_before_verification: list[str] = field(default_factory=list)
    applies_to_everyone: bool | None = None
    confirmation_question: str | None = None
    confirmation_accepted: list[str] = field(default_factory=list)
    confirmation_not_accepted: list[str] = field(default_factory=list)
    max_confirmation_asks: int | None = None
    answer_question_first: bool | None = None
    on_unclear: OutcomeRef | None = None
    ai_question_answer: str | None = None
    scam_question_answer: str | None = None


@dataclass
class CallbackDays:
    kind: str
    n: int | None = None


@dataclass
class PerWeekdayWindow:
    weekdays: list[str]
    hour_start: str
    hour_end: str
    inclusive: bool | None = None


@dataclass
class CallbackWindow:
    days: CallbackDays
    hour_start: str
    hour_end: str
    inclusive: bool | None = None
    must_be_after_now: bool | None = None
    per_weekday: list[PerWeekdayWindow] = field(default_factory=list)


@dataclass
class DeceasedRule:
    sentence: str | None = None
    ask_nothing_else: bool | None = None
    outcome: OutcomeRef | None = None


@dataclass
class ThirdParty:
    say_nothing_beyond_opening: bool | None = None
    ask_when_reachable: bool | None = None
    callback_window: CallbackWindow | None = None
    reasks_when_outside: int | None = None
    on_callback_scheduled: OutcomeRef | None = None
    on_no_valid_time: OutcomeRef | None = None
    on_not_customers_number: OutcomeRef | None = None
    on_customer_deceased: DeceasedRule | None = None
    others_may_answer_security: bool | None = None
    others_may_hear_account: bool | None = None


@dataclass
class AnswerFormat:
    kind: str
    output_format: str | None = None
    numeric_reading_order: str | None = None
    digits: int | None = None
    notes: str | None = None


@dataclass
class IdentityQuestion:
    field: str
    prompt: str
    answer_format: AnswerFormat | None = None


@dataclass
class Hint:
    value: str
    instruction: str
    counts_as_wrong: bool


@dataclass
class IdentityFailure:
    say: str | None = None
    disclose_debt: bool | None = None
    outcome: OutcomeRef | None = None


@dataclass
class Decline:
    explain_once: str | None = None
    max_refusals: int | None = None
    asking_purpose_counts_as_decline: bool | None = None
    outcome: OutcomeRef | None = None


@dataclass
class Identity:
    questions: list[IdentityQuestion]
    max_wrong_total: int
    one_at_a_time: bool | None = None
    wrong_may_be_reasked: bool | None = None
    hints: list[Hint] = field(default_factory=list)
    on_failure: IdentityFailure | None = None
    decline: Decline | None = None


@dataclass
class ProductName:
    product_type: str
    template: str


@dataclass
class DebtStatement:
    intro: str | None = None
    product_names: list[ProductName] = field(default_factory=list)
    state_fields: list[str] = field(default_factory=list)
    one_sentence_per_product: bool | None = None


@dataclass
class WeekdaySemantics:
    next: str | None = None
    this: str | None = None


@dataclass
class VaguePeriod:
    ask_exact_day_times: int | None = None
    then: str | None = None


@dataclass
class Calendar:
    weekend_days: list[str]
    holidays: list[str] = field(default_factory=list)
    week_starts_on: str = "monday"
    weekday_semantics: WeekdaySemantics | None = None
    vague_period: VaguePeriod | None = None


@dataclass
class Deadline:
    kind: str
    n: int
    roll: str = "none"
    base: str | None = None


@dataclass
class Band:
    name: str
    days_late_min: int
    days_late_max: int
    deadline: Deadline
    persuasion_attempts: int


@dataclass
class TooMuch:
    offer_floor_times: int | None = None
    say_rest_stays_overdue: bool | None = None
    refusal_is_flat_refusal: bool | None = None


@dataclass
class BelowFloor:
    say: str | None = None
    offer_floor_times: int | None = None
    refusal_is_flat_refusal: bool | None = None


@dataclass
class FloorPercentRule:
    percent: float
    of_field: str


@dataclass
class Amounts:
    full_amount_field: str
    floor_fields: dict[str, str] = field(default_factory=dict)
    floor_default_field: str | None = None
    floor_percent: dict[str, FloorPercentRule] = field(default_factory=dict)
    floor_default_percent: FloorPercentRule | None = None
    rounding_mode: str | None = None
    ask_full_first: bool | None = None
    never_ask_customer_amount: bool | None = None
    forbidden_questions: list[str] = field(default_factory=list)
    too_much: TooMuch | None = None
    accept_between_floor_and_full: bool | None = None
    below_floor: BelowFloor | None = None
    above_full: str = "not_specified"
    rounding_decimals: int = 2


@dataclass
class Readback:
    template: str | None = None
    confirmation_question: str | None = None
    require_clear_yes: bool | None = None
    recheck_on_change: bool | None = None


@dataclass
class PromiseFailure:
    say: str | None = None
    outcome: OutcomeRef | None = None


@dataclass
class PromiseRules:
    earliest: str | None = None
    must_be_business_day: bool | None = None
    latest: str | None = None
    on_non_business_day: str | None = None
    on_after_deadline: str | None = None
    refusing_deadline_is_flat_refusal: bool | None = None
    first_ask: str | None = None
    readback: Readback | None = None
    one_promise_per_product: bool | None = None
    allow_change_or_cancel: bool | None = None
    change_request_answer: str | None = None
    retries_on_failure: int | None = None
    say_recorded_only_after_ok: bool | None = None
    success_sentence: str | None = None
    success_sentence_plural: str | None = None
    on_failure: PromiseFailure | None = None
    forbidden_offers: list[str] = field(default_factory=list)


@dataclass
class Attempt:
    index: int
    date_rule: str
    amount_rule: str
    bands: list[str] | None = None
    note: str | None = None
    date_n: int | None = None


@dataclass
class Persuasion:
    attempts: list[Attempt] = field(default_factory=list)
    flat_refusal_definition: str | None = None
    customer_proposal_is_never_refusal: bool | None = None
    do_not_repeat_refused_offer: bool | None = None
    on_exhausted: OutcomeRef | None = None


@dataclass
class Action:
    type: str
    text: str | None = None
    queue: str | None = None
    reason: str | None = None
    outcome: str | None = None
    question: str | None = None
    on_accept: list[Action] = field(default_factory=list)
    on_decline: list[Action] = field(default_factory=list)
    then: list[Action] = field(default_factory=list)


@dataclass
class Intent:
    name: str
    triggers: list[str]
    actions: list[Action]
    precedence: int = 100
    applies_before_verification: bool = False
    applies_after_verification: bool = True
    occurrences_before_action: int = 1
    notes: str | None = None


@dataclass
class OutcomeRow:
    outcome: str
    when: str
    reason: str | None = None
    source_refs: list[str] = field(default_factory=list)


@dataclass
class Goodbye:
    required: bool | None = None
    text: str | None = None


@dataclass
class AllNotesEqual:
    note_reason: str
    outcome: str


@dataclass
class FirstNoteNotEqual:
    excluded_reason: str
    outcome: str


@dataclass
class Aggregation:
    precedence: list[str]
    transfer_outcome: str | None = None
    promise_outcome: str | None = None
    all_notes_equal: AllNotesEqual | None = None
    first_note_not_equal: FirstNoteNotEqual | None = None
    default: OutcomeRef | None = None


@dataclass
class Closing:
    outcome_table: list[OutcomeRow]
    close_tool: str | None = None
    close_tool_last: bool | None = None
    exactly_one_close: bool | None = None
    goodbye_before_close: Goodbye | None = None
    aggregation: Aggregation | None = None


@dataclass
class NotesRule:
    for_products_without_promise: bool | None = None
    reasons: list[str] = field(default_factory=list)
    also_when_none_promised: bool | None = None


@dataclass
class TransferRule:
    add_no_notes: bool | None = None
    keep_recorded_promises: bool | None = None
    drop_unrecorded_promises: bool | None = None


@dataclass
class PromiseFailureNote:
    note_reason: str | None = None


@dataclass
class MultiProduct:
    enabled: bool = False
    max_products: int = 1
    state_all_before_asking: bool | None = None
    ask_all_today_first: bool | None = None
    order: str = "account_order"
    per_product_attempts: bool | None = None
    one_date_for_all: str | None = None
    say_capped_in_readback: bool | None = None
    combined_readback: bool | None = None
    notes: NotesRule | None = None
    claim_on_one_product_continues: bool | None = None
    on_transfer: TransferRule | None = None
    on_promise_failure: PromiseFailureNote | None = None


@dataclass
class ToolOverride:
    purpose: str | None = None
    field_map: dict[str, str] = field(default_factory=dict)
    allowed_values: dict[str, list[str]] = field(default_factory=dict)
    notes: str | None = None


@dataclass
class MandatedSentence:
    when: str
    text: str
    source_ref: str | None = None


@dataclass
class VerbatimRule:
    stage: str
    text: str
    source_ref: str
    gap_kind: str | None = None


@dataclass
class CoverageEntry:
    source_ref: str
    paths: list[str]
    note: str | None = None


@dataclass
class PolicySpec:
    schema_version: str
    meta: Meta
    account_fields: AccountFields
    calendar: Calendar
    bands: list[Band]
    closing: Closing
    call_variables: CallVariables | None = None
    opening: Opening | None = None
    third_party: ThirdParty | None = None
    identity: Identity | None = None
    debt_statement: DebtStatement | None = None
    amounts: Amounts | None = None
    promise_rules: PromiseRules | None = None
    persuasion: Persuasion | None = None
    intents: list[Intent] = field(default_factory=list)
    multi_product: MultiProduct = field(default_factory=MultiProduct)
    tools: dict[str, ToolOverride] = field(default_factory=dict)
    mandated_sentences: list[MandatedSentence] = field(default_factory=list)
    verbatim_rules: list[VerbatimRule] = field(default_factory=list)
    coverage: list[CoverageEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------
# Schema validation and error formatting
# ---------------------------------------------------------------------------------------------


class SpecError(ValueError):
    """Raised when a spec fails validation; `errors` holds one message per problem."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid spec:\n" + "\n".join(self.errors))


def load_schema() -> dict[str, Any]:
    """Load the PolicySpec JSON Schema."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _render_path(parts: typing.Iterable[object]) -> str:
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += f".{p}" if out else str(p)
    return out or "$"


def _child(path: str, key: str) -> str:
    return f"{path}.{key}"


_REQUIRED_RE = re.compile(r"^'(.+)' is a required property$")


def format_error(err: jsonschema.ValidationError) -> list[str]:
    """Render one schema error as messages of the form `a.b[1].c: problem` (`$` = root).

    Returns a list because one error may name several properties (missing or unexpected).
    """
    path = _render_path(err.absolute_path)
    if err.validator == "required":
        m = _REQUIRED_RE.match(err.message)
        if m:
            return [f"{_child(path, m.group(1))}: required"]
        instance = err.instance if isinstance(err.instance, dict) else {}
        names = [str(k) for k in (err.validator_value or []) if k not in instance]
        return [f"{_child(path, n)}: required" for n in names] or [f"{path}: {err.message}"]
    if err.validator == "additionalProperties":
        schema = err.schema if isinstance(err.schema, dict) else {}
        known = schema.get("properties", {})
        patterns = [re.compile(p) for p in schema.get("patternProperties", {})]
        instance = err.instance if isinstance(err.instance, dict) else {}
        extras = [
            str(k)
            for k in instance
            if k not in known and not any(p.search(str(k)) for p in patterns)
        ]
        return [f"{_child(path, k)}: unexpected property" for k in extras] or [
            f"{path}: {err.message}"
        ]
    if err.validator == "enum":
        values = ", ".join(str(v) for v in (err.validator_value or []))
        return [f"{path}: must be one of [{values}]"]
    if err.validator == "type":
        expected = err.validator_value
        if isinstance(expected, list):
            expected = " or ".join(str(t) for t in expected)
        return [f"{path}: expected {expected}"]
    return [f"{path}: {err.message}"]


def schema_errors(data: object) -> list[str]:
    """All JSON Schema errors (with format checking), formatted, sorted and de-duplicated."""
    validator = jsonschema.Draft202012Validator(
        load_schema(), format_checker=jsonschema.FormatChecker()
    )
    messages: set[str] = set()
    for err in validator.iter_errors(data):
        messages.update(format_error(err))
    return sorted(messages)


# ---------------------------------------------------------------------------------------------
# Path resolution and semantic checks
# ---------------------------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"^([^.\[\]]+)((?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def resolve_path(data: object, path: str) -> object:
    """Resolve a path like `a.b[1].c` inside `data`. Raises KeyError when it does not resolve."""
    if not isinstance(path, str) or not path.strip():
        raise KeyError(path)
    current: object = data
    for segment in path.split("."):
        m = _SEGMENT_RE.match(segment)
        if not m:
            raise KeyError(path)
        key = m.group(1)
        if not isinstance(current, dict) or key not in current:
            raise KeyError(path)
        current = current[key]
        for idx in _INDEX_RE.findall(m.group(2)):
            i = int(idx)
            if not isinstance(current, list) or i >= len(current):
                raise KeyError(path)
            current = current[i]
    return current


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _hhmm_minutes(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{2}):(\d{2})", value)
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _ref_label(outcome: str, reason: str | None) -> str:
    return f"outcome '{outcome}'/'{reason}'" if reason else f"outcome '{outcome}'"


def _collect_action_refs(
    actions: list[Any], path: str, out: list[tuple[str, str, str | None]]
) -> None:
    for i, action in enumerate(actions):
        a = _as_dict(action)
        here = f"{path}[{i}]"
        if a.get("type") == "close" and isinstance(a.get("outcome"), str):
            out.append((here, a["outcome"], a.get("reason")))
        for key in ("on_accept", "on_decline", "then"):
            _collect_action_refs(_as_list(a.get(key)), f"{here}.{key}", out)


def _outcome_refs(data: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Every (path, outcome, reason) the spec uses outside closing.outcome_table."""
    refs: list[tuple[str, str, str | None]] = []

    def add_ref(path: str, value: object) -> None:
        ref = _as_dict(value)
        if isinstance(ref.get("outcome"), str):
            refs.append((path, ref["outcome"], ref.get("reason")))

    for i, intent in enumerate(_as_list(data.get("intents"))):
        _collect_action_refs(_as_list(_as_dict(intent).get("actions")), f"intents[{i}].actions", refs)

    opening = _as_dict(data.get("opening"))
    add_ref("opening.on_unclear", opening.get("on_unclear"))

    third = _as_dict(data.get("third_party"))
    for key in ("on_callback_scheduled", "on_no_valid_time", "on_not_customers_number"):
        add_ref(f"third_party.{key}", third.get(key))
    add_ref(
        "third_party.on_customer_deceased.outcome",
        _as_dict(third.get("on_customer_deceased")).get("outcome"),
    )

    identity = _as_dict(data.get("identity"))
    add_ref("identity.on_failure.outcome", _as_dict(identity.get("on_failure")).get("outcome"))
    add_ref("identity.decline.outcome", _as_dict(identity.get("decline")).get("outcome"))

    promise = _as_dict(data.get("promise_rules"))
    add_ref("promise_rules.on_failure.outcome", _as_dict(promise.get("on_failure")).get("outcome"))

    add_ref("persuasion.on_exhausted", _as_dict(data.get("persuasion")).get("on_exhausted"))

    agg = _as_dict(_as_dict(data.get("closing")).get("aggregation"))
    for key in ("transfer_outcome", "promise_outcome"):
        if isinstance(agg.get(key), str):
            refs.append((f"closing.aggregation.{key}", agg[key], None))
    for key in ("all_notes_equal", "first_note_not_equal"):
        sub = _as_dict(agg.get(key))
        if isinstance(sub.get("outcome"), str):
            refs.append((f"closing.aggregation.{key}.outcome", sub["outcome"], None))
    add_ref("closing.aggregation.default", agg.get("default"))
    return refs


def semantic_errors(data: dict[str, Any]) -> list[str]:
    """Cross-field checks the JSON Schema cannot express. Assumes the schema already passed."""
    errors: list[str] = []

    bands = [_as_dict(b) for b in _as_list(data.get("bands"))]
    for i, band in enumerate(bands):
        lo, hi = band.get("days_late_min"), band.get("days_late_max")
        if isinstance(lo, int) and isinstance(hi, int) and hi < lo:
            errors.append(f"bands[{i}].days_late_max: below days_late_min")
    for i, band in enumerate(bands):
        for j in range(i):
            a, b = bands[j], band
            if all(isinstance(x, int) for x in (a.get("days_late_min"), a.get("days_late_max"),
                                                 b.get("days_late_min"), b.get("days_late_max"))):
                if b["days_late_min"] <= a["days_late_max"] and a["days_late_min"] <= b["days_late_max"]:
                    errors.append(f"bands[{i}]: overlaps bands[{j}]")
    band_names = {b.get("name") for b in bands}

    account = _as_dict(data.get("account_fields"))
    for i, band in enumerate(bands):
        dl = _as_dict(band.get("deadline"))
        kind, base = dl.get("kind"), dl.get("base")
        based = kind in ("business_days_after_base", "calendar_days_after_base")
        if based and base is None:
            errors.append(f"bands[{i}].deadline.base: required for kind '{kind}'")
        elif not based and base is not None:
            errors.append(f"bands[{i}].deadline.base: only allowed with a *_after_base kind")
        if base == "due_date_field" and not account.get("due_date"):
            errors.append(f"bands[{i}].deadline.base: due_date_field needs account_fields.due_date")
        if base == "days_late_field" and not account.get("days_late"):
            errors.append(f"bands[{i}].deadline.base: days_late_field needs account_fields.days_late")

    seen_index: dict[int, int] = {}
    attempts = _as_list(_as_dict(data.get("persuasion")).get("attempts"))
    for i, attempt in enumerate(attempts):
        att = _as_dict(attempt)
        for k, name in enumerate(_as_list(att.get("bands"))):
            if name not in band_names:
                errors.append(f"persuasion.attempts[{i}].bands[{k}]: unknown band '{name}'")
        rule, date_n = att.get("date_rule"), att.get("date_n")
        if rule == "business_days_after_today" and date_n is None:
            errors.append(f"persuasion.attempts[{i}].date_n: required with date_rule business_days_after_today")
        elif rule != "business_days_after_today" and date_n is not None:
            errors.append(f"persuasion.attempts[{i}].date_n: only allowed with date_rule business_days_after_today")
        idx = att.get("index")
        if isinstance(idx, int):
            if idx in seen_index:
                errors.append(
                    f"persuasion.attempts[{i}].index: duplicate index {idx} "
                    f"(also persuasion.attempts[{seen_index[idx]}])"
                )
            else:
                seen_index[idx] = i

    window = _as_dict(_as_dict(data.get("third_party")).get("callback_window"))
    start, end = _hhmm_minutes(window.get("hour_start")), _hhmm_minutes(window.get("hour_end"))
    if start is not None and end is not None and end < start:
        errors.append("third_party.callback_window.hour_end: before hour_start")
    listed: dict[str, int] = {}
    for i, entry in enumerate(_as_list(window.get("per_weekday"))):
        e = _as_dict(entry)
        here = f"third_party.callback_window.per_weekday[{i}]"
        for day in _as_list(e.get("weekdays")):
            if day in listed:
                errors.append(f"{here}.weekdays: weekday '{day}' listed twice (also per_weekday[{listed[day]}])")
            else:
                listed[day] = i
        s, t = _hhmm_minutes(e.get("hour_start")), _hhmm_minutes(e.get("hour_end"))
        if s is not None and t is not None and t < s:
            errors.append(f"{here}.hour_end: before hour_start")

    rows = [_as_dict(r) for r in _as_list(_as_dict(data.get("closing")).get("outcome_table"))]
    outcomes = {r.get("outcome") for r in rows}
    pairs = {(r.get("outcome"), r.get("reason")) for r in rows}
    for path, outcome, reason in _outcome_refs(data):
        known = (outcome, reason) in pairs if reason else outcome in outcomes
        if not known:
            errors.append(f"{path}: {_ref_label(outcome, reason)} not in closing.outcome_table")

    for i, entry in enumerate(_as_list(data.get("coverage"))):
        for k, p in enumerate(_as_list(_as_dict(entry).get("paths"))):
            try:
                resolve_path(data, p)
            except KeyError:
                errors.append(f"coverage[{i}].paths[{k}]: does not resolve")

    return errors


def validate_spec(data: object) -> list[str]:
    """Schema errors first; semantic errors only when the schema passed. [] means valid."""
    errors = schema_errors(data)
    if errors:
        return errors
    assert isinstance(data, dict)
    return semantic_errors(data)


# ---------------------------------------------------------------------------------------------
# Typed loading and dumping
# ---------------------------------------------------------------------------------------------


def _strip_optional(tp: Any) -> Any:
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return tp


def _convert(tp: Any, value: Any, path: str) -> Any:
    tp = _strip_optional(tp)
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return _build(tp, value, path)
    origin = typing.get_origin(tp)
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if not isinstance(value, list):
            raise SpecError([f"{path}: expected array"])
        return [_convert(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        _, val_tp = typing.get_args(tp)
        if not isinstance(value, dict):
            raise SpecError([f"{path}: expected object"])
        return {str(k): _convert(val_tp, v, f"{path}.{k}") for k, v in value.items()}
    return value


def _build(cls: type, value: Any, path: str) -> Any:
    """Construct dataclass `cls` from a JSON object, recursing through nested types."""
    if not isinstance(value, dict):
        raise SpecError([f"{path}: expected object"])
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    names = set()
    for f in dataclasses.fields(cls):
        names.add(f.name)
        if f.name in value:
            sub = f.name if path == "$" else f"{path}.{f.name}"
            kwargs[f.name] = _convert(hints[f.name], value[f.name], sub)
    unexpected = sorted(set(value) - names)
    if unexpected:
        base = "" if path == "$" else path
        raise SpecError([f"{base + '.' if base else '$.'}{k}: unexpected property" for k in unexpected])
    try:
        return cls(**kwargs)
    except TypeError as e:
        raise SpecError([f"{path}: {e}"]) from e


def load_spec(data: dict[str, Any]) -> PolicySpec:
    """Validate and load a spec dict. Raises SpecError listing every problem."""
    errors = validate_spec(data)
    if errors:
        raise SpecError(errors)
    return _build(PolicySpec, data, "$")


def load_spec_file(path: Path) -> PolicySpec:
    """Read, validate and load a spec JSON file."""
    return load_spec(json.loads(Path(path).read_text(encoding="utf-8")))


def _has_empty_default(f: dataclasses.Field[Any]) -> bool:
    if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
        default = f.default_factory()  # type: ignore[misc]
        return isinstance(default, (list, dict)) and not default
    return False


def _dump(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            if v is None:
                continue
            if isinstance(v, (list, dict)) and not v and _has_empty_default(f):
                continue
            out[f.name] = _dump(v)
        return out
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items() if v is not None}
    return value


def spec_to_dict(spec: PolicySpec) -> dict[str, Any]:
    """Dump a PolicySpec to a JSON-ready dict; None and empty optional fields are omitted."""
    return _dump(spec)


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------

USAGE = "usage: python -m builder.spec validate <spec.json>"


def main(argv: list[str]) -> int:
    """Entry point for `python -m builder.spec validate <spec.json>`."""
    if len(argv) != 2 or argv[0] != "validate":
        print(USAGE, file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 2
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"bad JSON in {path}: {e}", file=sys.stderr)
        return 2
    errors = validate_spec(data)
    if errors:
        for line in errors:
            print(line)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
