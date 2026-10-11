from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from builder.online_cases import load_carriers
from builder.online_runner import HttpBank, MockBank, MockLedgerSource, OrganizerLedgerSource, server_origin


class Resp:
    def __init__(self, body: Any, status: int = 200) -> None:
        self._raw = json.dumps(body).encode()
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "Resp":
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class Opener:
    def __init__(self, resp: Any = None, exc: Exception | None = None) -> None:
        self.resp, self.exc, self.reqs = resp, exc, []

    def __call__(self, req: Any, timeout: float = 0) -> Any:
        self.reqs.append((req, timeout))
        if self.exc:
            raise self.exc
        return self.resp


@pytest.mark.parametrize("url", ["https://x.example/v1", "https://x.example", "https://x.example:8443/v1/"])
def test_ledger_url_is_built_from_the_origin(url: str) -> None:
    op = Opener(Resp({"calls": []}))
    assert MockLedgerSource(url, opener=op).ledger("c 1") == {"calls": []}
    req, timeout = op.reqs[0]
    assert req.full_url == server_origin(url) + "/v1/practice/ledger/c%201" and req.get_method() == "GET" and timeout == 20.0


def test_auth_header_only_when_configured() -> None:
    op = Opener(Resp({}))
    MockLedgerSource("https://x.example/v1", opener=op).ledger("a")
    assert not op.reqs[0][0].header_items()
    MockLedgerSource("https://x.example/v1", "x-team-key", "secret-value", opener=op).ledger("a")
    assert op.reqs[1][0].get_header("X-team-key") == "secret-value"


def test_errors_raise() -> None:
    err = urllib.error.HTTPError("https://x.example", 503, "down", {}, io.BytesIO(b""))  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        MockLedgerSource("https://x.example", opener=Opener(exc=err)).ledger("a")
    with pytest.raises(urllib.error.URLError):
        MockLedgerSource("https://x.example", opener=Opener(exc=urllib.error.URLError("no route"))).ledger("a")
    with pytest.raises(RuntimeError):
        MockLedgerSource("https://x.example", opener=Opener(Resp({}, status=503))).ledger("a")
    with pytest.raises(ValueError):
        MockLedgerSource("ftp://x.example")


def test_mock_bank_session_posts_and_returns_variables() -> None:
    op = Opener(Resp({"variables": {"customer_id": "k1", "first_name": "A"}}))
    vars_ = MockBank(MockLedgerSource("https://x.example/v1", opener=op)).session("case-1")
    req = op.reqs[0][0]
    assert vars_["customer_id"] == "k1" and req.full_url == "https://x.example/v1/practice/sessions"
    assert req.get_method() == "POST" and json.loads(req.data) == {"case": "case-1"}
    with pytest.raises(RuntimeError):
        MockBank(MockLedgerSource("https://x.example", opener=Opener(Resp({"variables": {}})))).session("c")


def test_http_bank_delegates_ledger_to_its_source() -> None:
    calls: list[str] = []

    def http(path: str, body: Any = None) -> Any:
        calls.append(path)
        return {"calls": [], "ok": True}

    scoring = type("S", (), {"http": staticmethod(http)})()
    assert OrganizerLedgerSource(scoring).ledger("z") == {"calls": [], "ok": True}
    assert HttpBank(scoring).ledger("z") == {"calls": [], "ok": True}
    assert calls == ["/v1/practice/ledger/z"] * 2


def test_simple_customer_shape_is_a_carrier(tmp_path: Path) -> None:
    f = tmp_path / "people.json"
    f.write_text(json.dumps([
        {"customer_id": "u1", "identity": {"a": "1"}, "products": [{"x": 1}, {"x": 2}], "variables": {"now": "2025-01-02T10:00"}},
        {"id": "u2", "identity": {"a": "2"}, "products": [{"x": 3}]},
        {"customer_id": "u3", "products": [{"x": 3}]},
    ]), encoding="utf-8")
    cs = load_carriers(f)
    assert [c.case_id for c in cs] == ["u1", "u2"]
    assert cs[0].product_ids == ["p1", "p2"] and cs[1].variables == {}
