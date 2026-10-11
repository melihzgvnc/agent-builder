"""Mode selection: the PolicySpec pipeline ('policy') or the generic document-driven pipeline ('flow')."""
from __future__ import annotations

from typing import Any

from builder import tools_gen
from builder.compiler import ROLE_ACCOUNT, ROLE_PROMISE, ROLE_VERIFY

MODES: tuple[str, ...] = ("auto", "flow", "policy")
REQUIRED_ROLES: tuple[str, ...] = (ROLE_VERIFY, ROLE_ACCOUNT, ROLE_PROMISE)


def contract_serves_policy(doc: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """True when the OpenAPI-like document has an operation for every role the old compiler requires."""
    if not isinstance(doc, dict):
        return False, list(REQUIRED_ROLES)
    have = set(tools_gen.tool_names(doc))
    missing = [r for r in REQUIRED_ROLES if r not in have]
    return not missing, missing


def decide_mode(folder_inputs: Any, requested: str = "auto", openapi_doc: dict[str, Any] | None = None) -> tuple[str, str]:
    """(mode, why). `folder_inputs` is a folder_input.FolderInputs or None (positional briefs)."""
    if requested == "flow":
        return "flow", "forced by --mode flow"
    if requested == "policy":
        return "policy", "forced by --mode policy"
    if folder_inputs is None:
        return "policy", "briefs given without --folder"
    if getattr(folder_inputs, "openapi_path", None) is None:
        return "flow", "the folder has no OpenAPI-like tool contract the compiler can serve"
    ok, missing = contract_serves_policy(openapi_doc)
    if ok:
        return "policy", "the folder's tool contract provides every role the policy compiler needs"
    return "flow", "the folder's tool contract lacks compiler roles: " + ", ".join(missing)
