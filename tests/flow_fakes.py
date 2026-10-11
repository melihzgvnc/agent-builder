"""Small invented fixtures (a pet clinic) for the generic flow mode tests."""
from __future__ import annotations

from typing import Any

STAGE_A: dict[str, Any] = {
    "call_variables": [{"name": "ownerId", "description": "owner id", "example_from_docs": "123"},
                       {"name": "stamp", "description": "call time", "example_from_docs": "2030-01-02T09:00:00+01:00"}],
    "base_address": "https://clinic.example/api/v2/",
    "request_envelope": {"wrapper_key": "data", "extra": {"call": {"lang": "xx"}}},
    "response_convention": {"success": "code ok", "error_shape": "code field"},
    "tools": [
        {"name": "pets.list", "description_for_model": "lists the pets of the owner", "method": "post", "path": "pets.list",
         "fields": [{"name": "owner", "type": "string", "description": "", "source": "call_variable:ownerId", "required": True},
                    {"name": "flow", "type": "string", "source": "constant:Visit"}],
         "errors": [{"status": "404", "code": "NOPET", "meaning": "no pets"}], "side_effects": "none"},
        {"name": "visit.book", "description_for_model": "books a visit", "method": "POST", "path": "/api/v2/visit.book",
         "fields": [{"name": "petId", "type": "string", "source": "previous_tool_output"},
                    {"name": "slot", "type": "string", "source": "bogus"}],
         "side_effects": "changes state"},
        {"name": "visit.book", "path": "dup"},
        {"name": "nopath"},
    ],
}


def policy() -> dict[str, Any]:
    return {
        "language": "xx", "persona": "You are the clinic assistant.",
        "opening": {"mode": "fixed", "text": "Welcome to the clinic."},
        "flow_steps": [{"id": "s1", "goal": "list pets", "do": ["greet"], "call_tool": "pets_list",
                        "preconditions": ["the owner is known"], "then": "s2"},
                       {"id": "s2", "goal": "book", "do": [], "call_tool": "visit_book", "preconditions": ["caller said yes"], "then": "end"}],
        "rules": [{"id": "r1", "text": "Be brief {{ownerId}} {{junk}}.", "source_doc": "a"}],
        "mandated_sentences": [{"situation": "goodbye", "text": "Bye now."}],
        "reading_rules": ["Say dates in words."], "security_rules": ["Never read more than two digits."],
        "tool_errors": [{"tool": "pets_list", "condition": "no pets", "say": "No pets found.", "next": "hand over"}],
        "silence": {"max_repeats": 2, "repeat_text": "Hello?", "final_text": "Goodbye, no sound.", "ends_call": True},
        "out_of_scope": {"ask_text": "Want the menu?", "if_yes": "hand over", "if_no": "continue", "unclear_tries": 2},
        "tries": {"max": 3, "text_on_exhaust": "Sorry, ending."},
        "anger": {"calming_replies": 3, "calming_text_style": "calm", "then": "to_human"},
        "handovers": [{"id": "to_human", "destination": "a person", "spoken_text": "Passing you to a person.", "when": "the caller asks for a person", "ends_call": True}],
        "never": ["Never invent prices"],
        "call_variables": [{"name": "ownerId", "meaning": "owner"}],
        "conflicts_resolved": [{"topic": "SECRETTOPIC", "kept": "new", "dropped": "old", "reason": "newer"}],
    }
