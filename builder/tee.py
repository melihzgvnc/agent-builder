"""Console capture and secret redaction for the run trace.

capture_console(path) tees stdout and stderr into a file and an in-memory buffer (both redacted)
while still writing to the real streams. Trace reads the buffer through captured_text().
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, TextIO

SECRET_ENV_NAMES: tuple[str, ...] = (
    "FREYA_API_KEY",
    "FREYA_BANK_TEAM_KEY",
    "ANTHROPIC_API_KEY",
    "BUILDER_MOCK_AUTH_VALUE",
)
MIN_SECRET_LEN = 6
MASK = "***"

_buffer: list[str] = []


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Replace the current value of every known secret variable (length >= 6) with ***."""
    source = os.environ if env is None else env
    # longest first so a value that contains another one is masked completely
    values = sorted(
        {v for v in (source.get(n, "") for n in SECRET_ENV_NAMES) if len(v) >= MIN_SECRET_LEN},
        key=len,
        reverse=True,
    )
    for v in values:
        text = text.replace(v, MASK)
    return text


class TeeStream:
    """File-like object that forwards to the original stream and records redacted text."""

    def __init__(self, stream: TextIO, sink: TextIO, label: str) -> None:
        self._stream = stream
        self._sink = sink
        self._label = label

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        clean = redact(text)
        self._stream.write(clean)
        try:
            self._sink.write(clean)
            self._sink.flush()
        except (OSError, ValueError):
            pass
        _buffer.append(clean)
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


@contextmanager
def capture_console(path: Path) -> Iterator[None]:
    """Tee sys.stdout and sys.stderr into `path` (and the in-memory buffer) until the block ends."""
    old_out, old_err = sys.stdout, sys.stderr
    _buffer.clear()
    with open(path, "w", encoding="utf-8") as sink:
        sys.stdout = TeeStream(old_out, sink, "stdout")  # type: ignore[assignment]
        sys.stderr = TeeStream(old_err, sink, "stderr")  # type: ignore[assignment]
        try:
            yield
        finally:
            sys.stdout, sys.stderr = old_out, old_err


def captured_text() -> str:
    """Everything captured so far by the active (or last) capture_console block."""
    return "".join(_buffer)
