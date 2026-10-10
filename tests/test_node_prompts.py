"""Prompt builders: every spec fact lands in the right node, nothing is invented."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from builder import node_prompts, prompts
from builder.node_prompts import PromptCtx
from builder.spec import load_spec, spec_to_dict
from builder.tools_gen import placeholder
from tests.conftest import load_fixture

SPEC_A_DICT: dict[str, Any] = load_fixture("spec_brief_a.json")
SPEC_AB_DICT: dict[str, Any] = load_fixture("spec_brief_ab.json")
SPEC_A = load_spec(SPEC_A_DICT)
SPEC_AB = load_spec(SPEC_AB_DICT)


def id_ctx(spec: Any) -> PromptCtx:
    return PromptCtx(
        tools={"verify_identity": "verify_identity", "schedule_callback": "schedule_callback",
               "transfer_to_agent": "transfer_to_agent"},
        fields={"verify_identity": {"question": "field", "answer": "answer"},
                "schedule_callback": {"date": "date", "time": "time"},
                "transfer_to_agent": {"queue": "queue"}},
        exits={("not_verified", "declined"): "transition_to_x_declined",
               ("not_reached", "unclear"): "transition_to_x_unclear",
               ("not_reached", "deceased"): "transition_to_x_deceased",
               ("not_reached", "wrong_number"): "transition_to_x_wrong"},
        finalize_transition="transition_to_early", callback_exit="transition_to_x_cb",
        no_time_exit="transition_to_x_none",
    )


def neg_ctx(spec: Any, notes: bool = False) -> PromptCtx:
    tools = {"create_promise": "create_promise", "transfer_to_agent": "transfer_to_agent"}
    if notes:
        tools["add_note"] = "add_note"
    return PromptCtx(
        tools=tools,
        fields={"create_promise": {"product": "product_id", "amount": "amount", "date": "date"},
                "add_note": {"product": "product_id", "reason": "reason"},
                "transfer_to_agent": {"queue": "queue"}},
        exits={("no_promise", "refused"): "transition_to_x_refused", ("paid", ""): "transition_to_x_paid",
               ("no_promise", "dispute"): "transition_to_x_dispute"},
        note_map={("no_promise", "refused"): "refused", ("paid", ""): "paid"} if notes else {},
        finalize_transition="transition_to_fin", uses_notes=notes, n_products=3 if notes else 1,
        note_reasons=list(spec.multi_product.notes.reasons) if spec.multi_product.notes else [],
    )


def mutated(base: dict[str, Any], edit: Any) -> Any:
    data = copy.deepcopy(base)
    data.pop("coverage", None)  # coverage paths would not resolve after an edit
    edit(data)
    return load_spec(data)


# ---------------------------------------------------------------------------------------------
# slots and opening
# ---------------------------------------------------------------------------------------------


def test_fill_call_slots_maps_known_and_keeps_unknown() -> None:
    text = node_prompts.fill_call_slots("Hi {title} {last_name} {first_name} {customer_id} {product_name}", SPEC_A)
    assert text == (
        f"Hi {placeholder('title')} {placeholder('last_name')} {placeholder('first_name')} "
        f"{placeholder('customer_id')} {{product_name}}"
    )


def test_fill_call_slots_follows_renamed_variables() -> None:
    spec = mutated(SPEC_A_DICT, lambda d: d["call_variables"].update(title="salutation", last_name="surname"))
    text = node_prompts.fill_call_slots("{title} {last_name}", spec)
    assert text == f"{placeholder('salutation')} {placeholder('surname')}"


def test_opening_text_has_sentence_and_question() -> None:
    text = node_prompts.opening_text(SPEC_A)
    assert SPEC_A.opening.first_sentence.template in text
    assert f"Am I speaking with {placeholder('title')} {placeholder('last_name')}?" in text


def test_opening_text_empty_without_opening() -> None:
    spec = mutated(SPEC_A_DICT, lambda d: d.pop("opening") and None)
    assert node_prompts.opening_text(spec) == ""


def test_system_prompt_uses_meta() -> None:
    text = node_prompts.system_prompt(SPEC_A)
    assert SPEC_A.meta.agent_identity in text and SPEC_A.meta.bank_name in text


# ---------------------------------------------------------------------------------------------
# identity task
# ---------------------------------------------------------------------------------------------


def test_identity_task_carries_the_spec_facts() -> None:
    text = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    op, tp, idn = SPEC_A.opening, SPEC_A.third_party, SPEC_A.identity
    for word in op.forbidden_before_verification:
        assert word in text
    for item in op.confirmation_accepted + op.confirmation_not_accepted:
        assert item in text
    assert "at most 2 times" in text and "transition_to_x_unclear" in text
    assert op.ai_question_answer in text and op.scam_question_answer in text
    cw = tp.callback_window
    assert cw.hour_start in text and cw.hour_end in text and "at most once" in text
    assert "transition_to_x_cb" in text and "transition_to_x_none" in text
    assert tp.on_customer_deceased.sentence in text and "transition_to_x_deceased" in text
    assert "transition_to_x_wrong" in text
    for q in idn.questions:
        assert q.prompt in text and q.field in text
    assert "day, then month, then year" in text
    assert "After 3 wrong answers" in text
    assert idn.hints[0].value in text and idn.hints[0].instruction in text and "does not count" in text
    assert idn.on_failure.say in text
    assert idn.decline.explain_once in text and "transition_to_x_declined" in text


def test_identity_task_ends_with_rules_and_has_stage_rules_only() -> None:
    text = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    assert text.endswith(prompts.render_tool_rules())
    for rule in SPEC_A.verbatim_rules:
        if rule.stage in node_prompts.IDENTITY_RULE_STAGES:
            assert rule.text in text
        else:
            assert rule.text not in text


def test_identity_task_mandated_sentences_split_by_stage() -> None:
    ident = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    neg = node_prompts.negotiate_task(SPEC_A, neg_ctx(SPEC_A))
    deceased = SPEC_A.third_party.on_customer_deceased.sentence
    assert f'"{deceased}"' in ident.split("Say these sentences word for word:")[1]
    assert deceased not in neg.split("Say these sentences word for word:")[1]
    success = SPEC_A.promise_rules.success_sentence
    assert f'"{success}"' in neg.split("Say these sentences word for word:")[1]
    assert success not in ident


def test_identity_task_only_pre_verification_intents() -> None:
    text = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    assert '"human_request"' in text and '"hardship"' not in text and '"already_paid"' not in text
    assert "transition_to_early" in text  # transfer then finalize_early


def test_identity_task_omits_what_the_spec_omits() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["opening"].pop("forbidden_before_verification")
        d["opening"].pop("ai_question_answer")
        d["identity"].pop("hints")
        d["identity"].pop("decline")
        d["third_party"].pop("callback_window")
    spec = mutated(SPEC_A_DICT, edit)
    text = node_prompts.identity_task(spec, id_ctx(spec))
    assert "never say these words" not in text and "AI or a robot" not in text
    assert "hint" not in text and "refuses the security questions" not in text
    assert "A callback can be booked" not in text


def test_identity_task_follows_mutated_strings() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["identity"]["questions"][0]["prompt"] = "Zorbl your birth date?"
        d["third_party"]["callback_window"]["hour_end"] = "21:30"
        d["identity"]["max_wrong_total"] = 5
    spec = mutated(SPEC_A_DICT, edit)
    text = node_prompts.identity_task(spec, id_ctx(spec))
    assert "Zorbl your birth date?" in text and "21:30" in text and "After 5 wrong answers" in text
    assert "19:00" not in text and "After 3 wrong answers" not in text


# ---------------------------------------------------------------------------------------------
# negotiate task
# ---------------------------------------------------------------------------------------------


def test_negotiate_task_carries_the_spec_facts() -> None:
    text = node_prompts.negotiate_task(SPEC_A, neg_ctx(SPEC_A))
    pr, am, ds = SPEC_A.promise_rules, SPEC_A.amounts, SPEC_A.debt_statement
    assert placeholder("date_facts") in text and placeholder("product_facts") in text
    assert ds.intro in text and "the full amount" in text and "the days late" in text
    assert pr.first_ask in text and pr.readback.template in text and pr.readback.confirmation_question in text
    assert pr.change_request_answer in text and pr.success_sentence in text
    for offer in pr.forbidden_offers:
        assert offer in text
    for q in am.forbidden_questions:
        assert q in text
    assert am.below_floor.say in text
    assert "Attempt 1" in text and "Attempt 2" in text and "only for bands: late" in text
    assert SPEC_A.persuasion.flat_refusal_definition in text
    assert "transition_to_x_refused" in text  # persuasion exhausted -> exit
    assert "transition_to_fin" in text
    assert text.endswith(prompts.render_tool_rules())


def test_negotiate_task_single_has_no_multi_block() -> None:
    text = node_prompts.negotiate_task(SPEC_A, neg_ctx(SPEC_A))
    assert "Several products" not in text and "up to 3 products" not in text


def test_negotiate_task_multi_block_and_note_path() -> None:
    text = node_prompts.negotiate_task(SPEC_AB, neg_ctx(SPEC_AB, notes=True))
    mp = SPEC_AB.multi_product
    assert "up to 3 products" in text and "State all products first" in text
    assert "one date" in text.lower() and "add no notes" in text.lower()
    for reason in mp.notes.reasons:
        assert reason in text
    # exhausted attempts become a note, not an exit
    assert 'call add_note with reason "refused"' in text
    assert "transition_to_x_refused" not in text


def test_negotiate_task_multi_asks_for_sequential_tool_calls() -> None:
    text = node_prompts.negotiate_task(SPEC_AB, neg_ctx(SPEC_AB, notes=True))
    assert "one product at a time" in text and "same turn" in text
    assert "Call create_promise for one product at a time" in text
    single = node_prompts.negotiate_task(SPEC_A, neg_ctx(SPEC_A))
    assert "one product at a time" not in single
def test_negotiate_task_omits_what_the_spec_omits() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["promise_rules"].pop("success_sentence")
        d["mandated_sentences"] = [m for m in d["mandated_sentences"] if "text message" not in m["text"]]
        d["amounts"].pop("below_floor")
        d["amounts"].pop("forbidden_questions")
        d["persuasion"].pop("flat_refusal_definition")
        d["calendar"].pop("vague_period")
    spec = mutated(SPEC_A_DICT, edit)
    text = node_prompts.negotiate_task(spec, neg_ctx(spec))
    assert SPEC_A.promise_rules.success_sentence not in text
    assert SPEC_A.amounts.below_floor.say not in text and "How much can you pay" not in text
    assert "vague period" not in text


def test_negotiate_task_follows_mutated_strings() -> None:
    def edit(d: dict[str, Any]) -> None:
        d["promise_rules"]["first_ask"] = "Qwerty today?"
        d["mandated_sentences"] = [m for m in d["mandated_sentences"] if m["text"] != "Can you make this payment today?"]
        d["debt_statement"]["intro"] = "Intro zzz."
        d["calendar"]["week_starts_on"] = "sunday"
    spec = mutated(SPEC_A_DICT, edit)
    text = node_prompts.negotiate_task(spec, neg_ctx(spec))
    assert "Qwerty today?" in text and "Intro zzz." in text and "start on sunday" in text
    assert SPEC_A.promise_rules.first_ask not in text.replace("Qwerty today?", "")


# ---------------------------------------------------------------------------------------------
# render_actions
# ---------------------------------------------------------------------------------------------


def test_render_actions_exit_and_note_paths() -> None:
    dispute = next(i for i in SPEC_AB.intents if i.name == "dispute")
    lines = node_prompts.render_actions(dispute.actions, neg_ctx(SPEC_AB), SPEC_AB)
    assert any("transition_to_x_dispute" in x for x in lines)
    ctx = neg_ctx(SPEC_AB, notes=True)
    ctx.note_map[("no_promise", "dispute")] = "dispute"
    lines = node_prompts.render_actions(dispute.actions, ctx, SPEC_AB)
    assert any('add_note with reason "dispute"' in x for x in lines)
    assert not any("transition_to_x_dispute" in x for x in lines)


def test_render_actions_close_after_transfer_is_implied() -> None:
    plan = next(i for i in SPEC_A.intents if i.name == "payment_plan_request")
    lines = node_prompts.render_actions(plan.actions, neg_ctx(SPEC_A), SPEC_A)
    assert len(lines) == 1 and "transfer_to_agent" in lines[0] and "transition_to_fin" in lines[0]


def test_render_actions_without_transfer_tool() -> None:
    plan = next(i for i in SPEC_A.intents if i.name == "payment_plan_request")
    ctx = neg_ctx(SPEC_A)
    ctx.tools.pop("transfer_to_agent")
    lines = node_prompts.render_actions(plan.actions, ctx, SPEC_A)
    assert "not available" in " ".join(lines)


def test_note_reason_for_close_rules() -> None:
    f = node_prompts.note_reason_for_close
    assert f(SPEC_AB, True, "paid", None) == "paid"
    assert f(SPEC_AB, True, "no_promise", "dispute") == "dispute"
    assert f(SPEC_AB, True, "no_promise", "unknown_reason") == ""
    assert f(SPEC_AB, False, "paid", None) == ""
    assert f(SPEC_A, True, "paid", None) == ""
    assert f(SPEC_AB, True, "transferred", "support") == ""


@pytest.mark.parametrize("spec", [SPEC_A, SPEC_AB], ids=["A", "AB"])
def test_spec_roundtrip_unchanged_by_prompt_builders(spec: Any) -> None:
    before = spec_to_dict(spec)
    node_prompts.identity_task(spec, id_ctx(spec))
    node_prompts.negotiate_task(spec, neg_ctx(spec))
    assert spec_to_dict(spec) == before
def test_identity_task_gives_the_clock_when_a_callback_can_be_booked() -> None:
    text = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    assert placeholder(SPEC_A.call_variables.now) in text
    assert "date facts" not in text
def test_negotiate_task_retries_a_failed_promise_call_and_holds_dates_rules() -> None:
    text = node_prompts.negotiate_task(SPEC_A, neg_ctx(SPEC_A))
    assert "call the tool again with exactly the same values" in text
    assert "Do not offer the latest promise date instead" in text


def test_identity_task_has_submit_first_rule() -> None:
    text = node_prompts.identity_task(SPEC_A, id_ctx(SPEC_A))
    assert "must be submitted to" in text and "even when it also contains a question" in text


def test_amounts_read_back_rule_only_when_floor_offered_once() -> None:
    marker = "do not repeat its amount or date"
    assert marker in "\n".join(node_prompts._amount_lines(SPEC_A))
    assert "ask only the confirmation question" in "\n".join(node_prompts._amount_lines(SPEC_A))
    for value in (2,):
        def edit(d: dict[str, Any], v: Any = value) -> None:
            d["amounts"]["too_much"]["offer_floor_times"] = v
        assert marker not in "\n".join(node_prompts._amount_lines(mutated(SPEC_A_DICT, edit)))
