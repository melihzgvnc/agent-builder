"""Cheap health probe: one chat turn against the published workflow, always ended afterwards.

`freya` is imported lazily so importing this module never needs credentials. Nothing here raises.
"""

from __future__ import annotations

import threading
import time
from typing import Any

DEFAULT_TIMEOUT_S: float = 30.0


def probe_chat(
    workflow_id: str,
    variables: dict[str, str],
    timeout: float = DEFAULT_TIMEOUT_S,
    client: Any = None,
) -> dict[str, Any]:
    """Send one 'Hello?' turn and return {ok, seconds, error}.

    The chat is ended in a finally block whenever a callId was obtained. `client` needs an
    `api(method, path, body=None, **params)` method; it defaults to the freya module.
    """
    t0 = time.monotonic()
    state: dict[str, Any] = {"call_id": None, "error": "", "done": False}

    def work() -> None:
        try:
            api = (client if client is not None else __import__("freya")).api
            result = api(
                "POST",
                "/api/v2/chat/message",
                {"workflowId": workflow_id, "message": "Hello?", "customerVariables": variables},
            )
            if isinstance(result, dict) and result.get("callId"):
                state["call_id"] = str(result["callId"])
            if not isinstance(result, dict) or not result.get("response"):
                state["error"] = "chat returned no response"
        except Exception as e:  # noqa: BLE001 - the probe never raises
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            if state["call_id"] is not None:
                try:
                    api_end = (client if client is not None else __import__("freya")).api
                    api_end("POST", "/api/v2/chat/end", {"callId": state["call_id"]})
                except Exception:  # noqa: BLE001 - ending is best effort
                    pass
            state["done"] = True

    try:
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(timeout)
        if not state["done"]:
            return {"ok": False, "seconds": time.monotonic() - t0, "error": f"timeout after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": time.monotonic() - t0, "error": f"{type(e).__name__}: {e}"}
    return {"ok": not state["error"], "seconds": time.monotonic() - t0, "error": state["error"]}
