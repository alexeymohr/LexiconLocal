"""Local embeddings via Ollama.

localhost only. There is no cloud fallback and there must never be one -- an
absent Ollama is a hard stop, not a reason to send the corpus off the machine.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

import httpx

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"

#: The embedding target may only ever be this machine. Checked rather than
#: defaulted, so that a flag, an environment variable, or a future caller cannot
#: widen it by accident -- the same rule, and deliberately the same shape, as the
#: web UI's loopback bind in `web/server.py`. Every chunk of the corpus passes
#: through this transport, so it is the one place the local-only promise is
#: either kept or quietly broken.
LOOPBACK_ONLY = True

#: Ollama tags hosted models with a `:cloud` suffix. Refusing those is
#: **defence in depth, not the guarantee**: it is a string match against one
#: vendor's current naming convention and would stop working silently if that
#: convention changed. The host check is the invariant; this is a second line.
CLOUD_TAG = ":cloud"

#: Ollama truncates at the model's context window anyway; clipping here keeps
#: request bodies bounded and avoids pathological single-chunk payloads.
MAX_CHARS_PER_INPUT = 8000


class EmbedError(RuntimeError):
    pass


class EmbedTargetRefused(EmbedError):
    """The requested embedding target is not on this machine.

    Deliberately raised *before* any HTTP request: a refusal that happens after
    the connection attempt has already leaked the fact of the corpus to whatever
    was listening.
    """


def is_cloud_model(name: str) -> bool:
    """Whether *name* is one of Ollama's hosted model tags."""
    return name.strip().lower().endswith(CLOUD_TAG)


def require_local_model(model: str) -> str:
    if is_cloud_model(model):
        raise EmbedTargetRefused(
            f"refusing model {model!r}: {CLOUD_TAG} models run on the vendor's "
            f"servers, so embedding with one would send the corpus off this "
            f"machine. Pull a local model and use that instead."
        )
    return model


def ollama_client(timeout: float = 60.0) -> httpx.Client:
    """The only way this package talks to Ollama.

    ``trust_env=False`` is the point. httpx honours ``HTTP_PROXY``,
    ``HTTPS_PROXY`` and ``ALL_PROXY`` by default, and it applies them to
    loopback URLs unless ``NO_PROXY`` happens to exclude them -- verified:
    with ``ALL_PROXY`` set, a request to ``http://localhost:11434`` is routed
    through ``httpcore.HTTPProxy`` rather than a direct ``ConnectionPool``.

    Validating the *address* therefore did not establish that the request stays
    on this machine: it checked where the request was pointed, not where it
    would travel. An environment variable could send corpus text to a proxy
    host while every host check still passed. One factory, so the invariant
    cannot be forgotten at a call site.
    """
    return httpx.Client(timeout=timeout, trust_env=False)


def require_local_host(host: str) -> str:
    """Return *host* normalised, or refuse it before a request is ever made.

    Accepts `localhost` and any literal loopback address (IPv4 or IPv6, any
    port). Everything else -- a LAN address, a public address, an ordinary
    hostname -- is refused. There is deliberately no override: an escape hatch
    would change the product's contract rather than enforce it.
    """
    raw = (host or "").strip().rstrip("/")
    parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    if parts.scheme not in ("http", "https"):
        raise EmbedTargetRefused(
            f"refusing embedding host {host!r}: only http and https are understood."
        )
    hostname = parts.hostname
    if not hostname:
        raise EmbedTargetRefused(
            f"refusing embedding host {host!r}: no host could be read from it."
        )
    if hostname.lower() != "localhost":
        try:
            addr = ipaddress.ip_address(hostname)
        except ValueError as e:
            raise EmbedTargetRefused(
                f"refusing embedding host {host!r}: {hostname!r} is neither "
                f"'localhost' nor a literal loopback address. The Lexicon embeds "
                f"on this machine only."
            ) from e
        if LOOPBACK_ONLY and not addr.is_loopback:
            raise EmbedTargetRefused(
                f"refusing embedding host {host!r}: {hostname} is not a loopback "
                f"address. Embedding is local-only; there is no remote override."
            )
    return f"{parts.scheme}://{parts.netloc}"


class Embedder:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        batch_size: int = 32,
        timeout: float = 300.0,
    ) -> None:
        # Validated here, not at the call sites: `Embedder` is what indexing and
        # search actually instantiate, so this is the single authoritative gate.
        self.model = require_local_model(model)
        self.host = require_local_host(host)
        self.batch_size = batch_size
        self._client = ollama_client(timeout)
        self._dims: int | None = None

    # ---- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Embedder":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- checks ------------------------------------------------------------

    def preflight(self) -> int:
        """Verify Ollama is reachable and the model is present. Returns dims."""
        try:
            r = self._client.get(f"{self.host}/api/tags", timeout=15.0)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - surfaced verbatim to the user
            raise EmbedError(
                f"Cannot reach Ollama at {self.host}: {e}\n"
                f"Start it with `ollama serve`. Never substitute a cloud API."
            ) from e
        # A `:cloud` entry must never satisfy availability. Stripping the tag
        # before comparing -- which this did -- let `nomic-embed-text:cloud`
        # answer for `nomic-embed-text`, so a machine with only the hosted model
        # would have embedded the whole corpus through the vendor.
        entries = [m.get("name", "") for m in r.json().get("models", [])]
        local = [n for n in entries if not is_cloud_model(n)]
        names = {n.split(":")[0] for n in local}
        if self.model.split(":")[0] not in names:
            cloud_only = [n for n in entries if is_cloud_model(n)]
            extra = (
                f" Only hosted models match: {sorted(cloud_only)} — those run off this "
                f"machine and must never embed the corpus." if cloud_only else ""
            )
            raise EmbedError(
                f"Model {self.model!r} is not available locally in Ollama "
                f"(local models: {sorted(names)}).{extra} Run `ollama pull {self.model}`."
            )
        self._dims = len(self.embed(["dimension probe"])[0])
        return self._dims

    @property
    def dims(self) -> int:
        if self._dims is None:
            self._dims = len(self.embed(["dimension probe"])[0])
        return self._dims

    # ---- embedding ---------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {
            "model": self.model,
            "input": [t[:MAX_CHARS_PER_INPUT] for t in texts],
        }
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = self._client.post(f"{self.host}/api/embed", json=payload)
                r.raise_for_status()
                data = r.json()
                embeddings = data.get("embeddings")
                if embeddings is None or len(embeddings) != len(texts):
                    raise EmbedError(
                        f"Ollama returned {0 if embeddings is None else len(embeddings)} "
                        f"embeddings for {len(texts)} inputs"
                    )
                return embeddings
            except Exception as e:  # noqa: BLE001 - retried, then surfaced
                last = e
        raise EmbedError(f"Embedding failed after 3 attempts: {last}") from last
