"""Typing-only shims.

`pages` needs the shape of a WebConfig but importing `server` would close an
import cycle (server -> pages -> server). A Protocol says what is actually
used -- a URL and the Lexicon root -- without either module owning the other.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class WebConfigLike(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def lexicon_root(self) -> Path: ...
