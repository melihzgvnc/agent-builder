from __future__ import annotations

from typing import Any

from builder.freya_extra import (
    WORKSPACE_WORKFLOW_LIMIT,
    extract_workflow_list,
    summarize_slot_hygiene,
)


def _items(n: int) -> list[dict[str, Any]]:
    return [{"id": f"wf_{i}", "name": f"w{i}"} for i in range(n)]


def test_extract_workflow_list_shapes() -> None:
    items = _items(2)
    assert extract_workflow_list(items) == items
    assert extract_workflow_list({"workflows": items}) == items
    assert extract_workflow_list({"items": items}) == items
    assert extract_workflow_list({"data": items}) == items
    assert extract_workflow_list({"results": items}) == items
    assert extract_workflow_list([{"id": "a"}, "junk", 3]) == [{"id": "a"}]
    assert extract_workflow_list("garbage") == []
    assert extract_workflow_list(None) == []
    assert extract_workflow_list({"workflows": "nope"}) == []
    assert extract_workflow_list({"other": items}) == []


def test_summarize_slot_hygiene_counts() -> None:
    state = {
        "slot-b": {"workflow_id": "wf_1", "agent_id": "ag_1"},
        "slot-a": {"workflow_id": "wf_3", "agent_id": "ag_3"},
    }
    s5 = summarize_slot_hygiene(_items(5), state)
    assert s5 == {
        "count": 5,
        "limit": WORKSPACE_WORKFLOW_LIMIT,
        "remaining": 195,
        "near_limit": False,
        "at_limit": False,
        "tracked_slots": ["slot-a", "slot-b"],
        "untracked_count": 3,
    }

    s199 = summarize_slot_hygiene(_items(199), state)
    assert s199["remaining"] == 1
    assert s199["near_limit"] is True
    assert s199["at_limit"] is False
    assert s199["untracked_count"] == 197

    s200 = summarize_slot_hygiene(_items(200), {}, limit=200)
    assert s200["remaining"] == 0
    assert s200["near_limit"] is True
    assert s200["at_limit"] is True
    assert s200["tracked_slots"] == []
    assert s200["untracked_count"] == 200
