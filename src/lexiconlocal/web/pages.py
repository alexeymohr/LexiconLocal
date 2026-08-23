"""The HTML shell.

One document for all three views, with rendering done client-side against the
JSON API. That is not a fashion choice -- it is what keeps navigation instant.
The API answers in single-digit milliseconds off a warm SQLite page cache, and
a full page round-trip would spend more time on the HTML than on the query.

There is exactly one inline thing on this page: nothing. The
Content-Security-Policy sets ``script-src 'self'`` with no ``unsafe-inline``,
so every byte of script and style is served from ``/static/``. That rule is
what makes it safe to render 4,600 Markdown files nobody has audited.
"""

from __future__ import annotations

import html
from urllib.parse import quote

from .server_types import WebConfigLike

#: A standing link in the header to the plain-English explainer. Rendered from
#: disk through /doc, so editing the Markdown updates the page -- there is no
#: second copy of this text in the UI. Omitted entirely if the file is absent,
#: because a dead link in the header is worse than no link.
EXPLAINER_REL = "topics/what-is-the-lexicon.md"

FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<rect width="16" height="16" rx="3" fill="#2f6fb3"/>'
    b'<path d="M4 3.5h3.2v7.4H12v1.6H4z" fill="#fff"/>'
    b"</svg>"
)

_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lexicon</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="topbar">
  <a class="brand" href="/" data-nav="home">Lexicon</a>
  <form class="searchbar" id="searchform" autocomplete="off">
    <input type="search" id="q" name="q" placeholder="Search everything&hellip;"
           aria-label="Search" spellcheck="false">
    <kbd class="slash-hint">/</kbd>
  </form>
  <nav class="topnav">
    {explainer}
    <span class="health-chip" id="healthchip" title="Index health">&middot;</span>
  </nav>
</header>
<main id="view" tabindex="-1"><div class="loading">Loading&hellip;</div></main>
<footer class="footer">
  <span id="footnote">read-only &middot; localhost only &middot; {url}</span>
</footer>
<script src="/static/app.js"></script>
</body>
</html>
"""

_ERROR = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{code} &mdash; Lexicon</title>
<link rel="stylesheet" href="/static/app.css">
</head>
<body class="errorpage">
<main>
  <h1>{code}</h1>
  <p>{message}</p>
  <p><a href="/">Back to the Lexicon</a></p>
</main>
</body>
</html>
"""


def explainer_link(cfg: WebConfigLike) -> str:
    """The header's 'What is this?' link, or nothing if the doc is missing."""
    path = cfg.lexicon_root / EXPLAINER_REL
    if not path.is_file():
        return ""
    href = f"/doc?path={quote(str(path))}"
    return (f'<a class="explainer-link" href="{html.escape(href)}" data-nav '
            f'title="Plain-English explainer">What is this?</a>')


def shell(cfg: WebConfigLike) -> str:
    return _SHELL.format(url=html.escape(cfg.url), explainer=explainer_link(cfg))


def error_page(code: int, message: str) -> str:
    return _ERROR.format(code=code, message=html.escape(message))
