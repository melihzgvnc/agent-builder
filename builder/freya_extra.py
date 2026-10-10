"""Extra Freya helpers: list/read workflows and workspace slot hygiene.

`freya` is imported lazily inside the network functions so importing this module never needs
credentials or the network. The pure helpers are what the tests exercise.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

WORKSPACE_WORKFLOW_LIMIT: int = 200
MAX_LIST_PAGES: int = 20
_LIST_KEYS: tuple[str, ...] = ("workflows", "items", "data", "results")


def extract_workflow_list(result: object) -> list[dict[str, Any]]:
    """Pull the list of workflow dicts out of a list-route result (list or wrapped list)."""
    items: object = result
    if isinstance(result, dict):
        items = None
        for key in _LIST_KEYS:
            if isinstance(result.get(key), list):
                items = result[key]
                break
    if not isinstance(items, list):
        return []
    return [w for w in items if isinstance(w, dict)]


def list_workflows(page_size: int = 100) -> list[dict[str, Any]]:
    """Every workflow in the workspace, read page by page (network)."""
    import freya

    out: list[dict[str, Any]] = []
    first_ids: list[str] | None = None
    offset = 0
    for _ in range(MAX_LIST_PAGES):
        page = extract_workflow_list(
            freya.api(
                "GET",
                "/api/v2/workflow",
                workspaceId=freya.workspace_id(),
                limit=page_size,
                offset=offset,
            )
        )
        ids = [str(w.get("id")) for w in page]
        if first_ids is None:
            first_ids = ids
        elif ids and ids == first_ids:
            break  # the route ignored offset and returned the first page again
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


def get_workflow(workflow_id: str, client: Any = None) -> dict[str, Any]:
    """One workflow as stored on the platform (network). `client` defaults to the freya module."""
    if client is None:
        import freya as client

    freya = client
    r = freya.api("GET", f"/api/v2/workflow/{workflow_id}", workspaceId=freya.workspace_id())
    if isinstance(r, dict):
        inner = r.get("workflow", r)
        return inner if isinstance(inner, dict) else r
    return {}


def summarize_slot_hygiene(
    items: list[dict[str, Any]], state: dict[str, Any], limit: int = WORKSPACE_WORKFLOW_LIMIT
) -> dict[str, Any]:
    """How full the workspace is and how many workflows are not tracked in the slot state."""
    count = len(items)
    tracked_ids = {
        str(v.get("workflow_id"))
        for v in state.values()
        if isinstance(v, dict) and v.get("workflow_id") is not None
    }
    untracked = sum(1 for w in items if str(w.get("id")) not in tracked_ids)
    return {
        "count": count,
        "limit": limit,
        "remaining": max(limit - count, 0),
        "near_limit": count >= 0.9 * limit,
        "at_limit": count >= limit,
        "tracked_slots": sorted(state),
        "untracked_count": untracked,
    }


def check_slot_hygiene(state_file: Path, limit: int = WORKSPACE_WORKFLOW_LIMIT) -> dict[str, Any]:
    """Read the slot state file (if any), list the workspace and summarize (network)."""
    path = Path(state_file)
    state: dict[str, Any] = {}
    if path.is_file():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    return summarize_slot_hygiene(list_workflows(), state, limit)
