"""Terminal output guard: crawled text never reaches the terminal as control sequences.

Text is cleaned at ingest, but anything printed may still hold words a remote server chose (titles, hostnames, error
details). An ESC there can do more than garble the screen: OSC 52 writes the clipboard, OSC 8 plants a link under
harmless-looking text, CSI sequences move the cursor to overwrite what was printed. Every line the CLI prints passes
through ``term_safe``, which replaces C0 and C1 controls (newline and tab kept) and bidi overrides with ``?``.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

# C0 except \t and \n; DEL; C1 (U+0080-U+009F holds the 8-bit CSI and OSC introducers); bidi embeddings, overrides
# and isolates, which reorder what is shown.
_UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def term_safe(text: str) -> str:
    return _UNSAFE.sub("?", text)


class GuardedStream:
    """A text stream that writes only terminal-safe text to the stream it wraps."""

    def __init__(self, inner: TextIO):
        self._inner = inner

    def write(self, s: str) -> int:
        self._inner.write(term_safe(s))
        return len(s)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@contextmanager
def guarded_output() -> Iterator[None]:
    """For the length of the block, stdout and stderr only carry terminal-safe text."""
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = GuardedStream(out), GuardedStream(err)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = out, err
