"""The HTTP server: routing, security headers, and a socket that only listens
to this machine.

Stdlib ``http.server`` rather than a framework, because six endpoints do not
justify one and because every dependency has to survive a 7-day publication
hold forever (D-2026-08-19-07). The parts that would normally justify a
framework -- request parsing, threading, keep-alive -- are already in the
stdlib and are adequate for a single operator on loopback.

Three things here are not incidental:

* **The bind is validated, not merely defaulted.** A default can be overridden
  by a flag; a validated bind cannot (D-2026-08-19-06).
* **Only GET and HEAD exist.** Not "no write routes are registered" -- the
  dispatcher refuses the method outright, so adding a handler by accident
  cannot create one (D-2026-08-19-05).
* **Connections are per-thread.** ``ThreadingHTTPServer`` runs handlers on
  different threads and a sqlite3 connection belongs to the thread that made
  it, so each thread gets its own reader.
"""

from __future__ import annotations

import ipaddress
import json
import mimetypes
import threading
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from ..config import Config
from ..embed import EmbedError, Embedder
from ..search import Searcher
from . import api, pages

STATIC_DIR = Path(__file__).parent / "static"

#: The only addresses this server may bind. Not a default -- a rule.
LOOPBACK_ONLY = True

#: `script-src 'self'` with no `unsafe-inline` is the real defence against a
#: script that arrives inside an indexed document; the scrubber in render.py is
#: the second layer. `default-src 'none'` means anything not named below is
#: refused, so a future addition has to be declared rather than inherited.
CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # The corpus is private. Nothing about it should reach a shared cache, and
    # there is no shared cache to reach on loopback -- this is belt and braces.
    "Cache-Control": "no-store",
}


class BindRefused(RuntimeError):
    pass


@dataclass
class WebConfig:
    cfg: Config
    port: int = 8377
    bind: str = "127.0.0.1"
    open_browser: bool = True

    @property
    def url(self) -> str:
        return f"http://{self.bind}:{self.port}/"

    @property
    def lexicon_root(self) -> Path:
        """Exposed for `pages`, which links to a doc under the Lexicon root."""
        return self.cfg.lexicon_root

    def validate(self) -> None:
        """Refuse to listen anywhere but loopback.

        Checked here rather than trusted from the default so that a flag, an
        environment variable, or a future caller cannot widen it by accident.
        """
        try:
            addr = ipaddress.ip_address(self.bind)
        except ValueError as e:
            raise BindRefused(f"{self.bind!r} is not an IP address") from e
        if LOOPBACK_ONLY and not addr.is_loopback:
            raise BindRefused(
                f"refusing to bind {self.bind}: the Lexicon web UI is loopback-only "
                f"(D-2026-08-19-06). Remote access is deliberately not implemented."
            )


class _Readers:
    """One Searcher per thread, one Embedder shared across all of them.

    sqlite3 connections are thread-affine; ``httpx.Client`` is thread-safe, and
    one embedder means one warm connection to Ollama instead of one per request.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._local = threading.local()
        self._all: list[Searcher] = []
        self._lock = threading.Lock()
        self.embedder: Embedder | None = None
        self.embed_error: str | None = None
        try:
            emb = Embedder()
            emb.preflight()
            self.embedder = emb
        except EmbedError as e:
            # Lexical search still works. Degrade loudly: the UI shows it.
            self.embed_error = str(e)

    def searcher(self) -> Searcher:
        s = getattr(self._local, "searcher", None)
        if s is None:
            s = Searcher(self.cfg, self.embedder)
            self._local.searcher = s
            with self._lock:
                self._all.append(s)
        return s

    def close(self) -> None:
        with self._lock:
            for s in self._all:
                try:
                    s.close()
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
            self._all.clear()
        if self.embedder is not None:
            self.embedder.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "lexicon-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        if self.server.web.quiet:  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _html(self, status: int, html: str) -> None:
        self._send(status, html.encode("utf-8"), "text/html; charset=utf-8")

    def _not_found(self) -> None:
        if self.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
        else:
            self._html(404, pages.error_page(404, "Not found"))

    # -- methods -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def _unsupported(self) -> None:
        """Every mutating method, refused in one place.

        The point is not that no write handler is registered -- it is that
        there is no path by which one could be reached (D-2026-08-19-05).
        """
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()

    do_POST = do_PUT = do_DELETE = do_PATCH = _unsupported
    do_OPTIONS = do_CONNECT = do_TRACE = _unsupported

    # -- routing -----------------------------------------------------------

    def _dispatch(self) -> None:
        try:
            parsed = urlparse(self.path)
            route = unquote(parsed.path)
            q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            web = self.server.web  # type: ignore[attr-defined]
            cfg = web.cfg.cfg

            if route.startswith("/static/"):
                return self._static(route)

            if route == "/api/search":
                return self._json(*api.search(cfg, web.readers.searcher(), q))
            if route == "/api/doc":
                return self._json(*api.doc(cfg, web.readers.searcher(), q))
            if route == "/api/dashboard":
                return self._json(*api.dashboard(cfg, web.readers.searcher(), q))
            if route.startswith("/api/project/"):
                name = route[len("/api/project/"):].strip("/")
                return self._json(*api.project(cfg, web.readers.searcher(), name))
            if route == "/api/health":
                # Deliberately tiny and dependency-free: the one endpoint that
                # must answer even if the index is unreadable.
                return self._json(200, {
                    "ok": True,
                    "url": web.cfg.url,
                    "vector_leg": web.readers.embedder is not None,
                    "embed_error": web.readers.embed_error,
                })

            if route in ("/", "/search", "/project", "/doc"):
                return self._html(200, pages.shell(web.cfg))
            if route == "/favicon.ico":
                return self._send(200, pages.FAVICON, "image/svg+xml")

            return self._not_found()
        except BrokenPipeError:
            return  # the browser navigated away mid-response
        except Exception as e:  # noqa: BLE001
            # A stack trace must never reach the page: it would name paths the
            # never-serve rules exist to keep quiet.
            self.log_message("handler error on %s: %s", self.path, e)
            if self.path.startswith("/api/"):
                return self._json(500, {"error": "internal error"})
            return self._html(500, pages.error_page(500, "Internal error"))

    def _static(self, route: str) -> None:
        name = route[len("/static/"):]
        # Static assets are vendored files with known names; anything with a
        # separator in it is not one of them.
        if not name or "/" in name or "\\" in name or ".." in name:
            return self._not_found()
        target = STATIC_DIR / name
        if not target.is_file():
            return self._not_found()
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), ctype or "application/octet-stream")


class WebServer:
    def __init__(self, cfg: WebConfig, *, quiet: bool = False) -> None:
        cfg.validate()
        self.cfg = cfg
        self.quiet = quiet
        self.readers = _Readers(cfg.cfg)
        self.httpd = ThreadingHTTPServer((cfg.bind, cfg.port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.web = self  # type: ignore[attr-defined]
        # Port 0 means "pick one" -- tests use it so they never collide with a
        # real server or with each other.
        self.cfg.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return self.cfg.url

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return t

    def shutdown(self) -> None:
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.readers.close()

    def __enter__(self) -> "WebServer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()


def serve(cfg: WebConfig, *, quiet: bool = False) -> WebServer:
    """Build a server. The caller decides how to run it."""
    return WebServer(cfg, quiet=quiet)


def open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - not being able to open a browser is not an error
        pass
