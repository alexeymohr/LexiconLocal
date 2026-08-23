"""`lexicon web` — a read-only, localhost-only front door to the Lexicon.

Everything hard already exists elsewhere in this package: hybrid search with
confidence, project attribution and alias resolution, coverage reporting. This
subpackage is a thin skin over them plus the one thing they cannot provide --
a way to look at all of it at once.

Four standing decisions govern it (D-2026-08-19-05..08):

* **Read-only.** Every route is a GET. Editing stays in editors and agents,
  where git records it.
* **Localhost, on demand.** Binds 127.0.0.1, started by hand, stopped with
  Ctrl-C. Not a daemon, not a launch agent.
* **Two dependencies at most, no build step.** `markdown` is the only one spent.
  Routing is stdlib; CSS and JS are vendored.
* **Never-serve is enforced here**, at the request layer, before the index is
  consulted -- see `paths.py`.
"""

from .server import WebConfig, serve  # noqa: F401
