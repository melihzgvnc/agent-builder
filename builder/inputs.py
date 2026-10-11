"""Input helpers: the server URL override and tolerant loading of the tool API document."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SERVER_URL_ENV = "BUILDER_SERVER_URL"


def resolve_server_url(cli: str | None, env: Mapping[str, str]) -> str | None:
    """The command line value wins over BUILDER_SERVER_URL; blank values count as unset."""
    for value in (cli, env.get(SERVER_URL_ENV)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def apply_server_url(doc: dict[str, Any], url: str | None) -> dict[str, Any]:
    """A deep copy of `doc` whose servers[0].url is `url` (servers is created when missing).

    A falsy url returns `doc` unchanged. Anything but an http(s) URL with a host is a ValueError.
    """
    if not url:
        return doc
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid server url {url!r}: expected http:// or https:// with a host")
    out = copy.deepcopy(doc)
    servers = out.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        servers[0]["url"] = url
    else:
        out["servers"] = [{"url": url}]
    return out


def load_openapi_doc(path: Path) -> dict[str, Any]:
    """Parse the tool API document: JSON, or YAML when a yaml module happens to be installed."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except ValueError as json_error:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            raise json_error from None
        doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError("openapi document is not a JSON object")
    return doc
