"""Markdown rendering, and the scrubbing that makes it safe to look at.

The corpus is 4,600 repo Markdown files written over years, plus transcripts of
conversations with three different AI systems. Any of them may contain a
``<script>`` tag -- innocently, as an example in a web-development note, or not.
Rendering that into a page which can talk to this server's own API would turn a
document viewer into a way for indexed content to drive the API.

Two independent defences, because either alone is a single point of failure:

1. **Content-Security-Policy** (set in ``server.py``): ``script-src 'self'``
   with no ``unsafe-inline``. The browser refuses inline and remote scripts
   outright. This is the real control -- it does not depend on this module
   parsing anything correctly.
2. **The scrubber below**, which removes dangerous elements and attributes
   before they reach the page. It is deliberately a blunt instrument and is
   documented as defence in depth, not as a sanitiser to be relied on alone.
"""

from __future__ import annotations

import html
import re

import markdown as _markdown

#: Elements dropped with their contents. `style` is included because a
#: full-page `position:fixed` overlay from a document is a nuisance even
#: without script.
_DANGEROUS_BLOCKS = re.compile(
    r"<\s*(script|style|iframe|object|embed|applet|form|link|meta|base)\b.*?"
    r"(?:</\s*\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
#: Any remaining opening/closing tag of the same set (unbalanced markup).
_DANGEROUS_TAGS = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|applet|form|link|meta|base)\b[^>]*>",
    re.IGNORECASE,
)
#: Event handlers: on<anything>= in any quoting style.
_EVENT_ATTRS = re.compile(r"\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
#: javascript:/vbscript:/data: URLs in href or src.
_BAD_URLS = re.compile(
    r"\s(href|src)\s*=\s*(\"|')\s*(?:javascript|vbscript|data)\s*:[^\"']*\2",
    re.IGNORECASE,
)

_EXTENSIONS = ["fenced_code", "tables", "toc", "sane_lists", "attr_list"]


def scrub(rendered: str) -> str:
    """Remove script-bearing constructs from already-rendered HTML."""
    out = _DANGEROUS_BLOCKS.sub("", rendered)
    out = _DANGEROUS_TAGS.sub("", out)
    out = _EVENT_ATTRS.sub("", out)
    out = _BAD_URLS.sub(r' \1="#"', out)
    return out


def render_markdown(text: str) -> str:
    """Markdown -> scrubbed HTML.

    A fresh converter per call: ``markdown.Markdown`` instances carry state
    between conversions (the ``toc`` extension in particular), and this server
    is threaded, so a shared instance would interleave two documents' state.
    """
    md = _markdown.Markdown(extensions=_EXTENSIONS, output_format="html")
    return scrub(md.convert(text))


def render_plain(text: str) -> str:
    """Non-Markdown text: escaped, in a pre block. Never parsed."""
    return f'<pre class="plain">{html.escape(text)}</pre>'


def render_transcript(chunks: list[dict]) -> str:
    """Transcript documents, which have no file to render.

    A conversation lives in the index as ordered chunks, not as a Markdown
    file, so it is shown as its chunks in order with their locators visible --
    the locator is what makes a claim in the UI checkable against the archive.
    """
    parts: list[str] = []
    for c in chunks:
        kind = c.get("kind", "prose")
        klass = "chunk chunk-tool" if kind != "prose" else "chunk"
        label = "tool event" if kind != "prose" else "prose"
        parts.append(
            f'<section class="{klass}" id="chunk-{c["ord"]}">'
            f'<header class="chunk-meta">'
            f'<span class="chunk-ord">chunk {c["ord"]}</span>'
            f'<span class="chunk-kind">{label}</span>'
            f"</header>"
            f'<pre class="chunk-body">{html.escape(c["text"])}</pre>'
            f"</section>"
        )
    return "\n".join(parts)
