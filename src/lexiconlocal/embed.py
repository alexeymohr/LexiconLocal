"""Local embeddings via Ollama.

localhost only. There is no cloud fallback and there must never be one -- an
absent Ollama is a hard stop, not a reason to send the corpus off the machine.
"""

from __future__ import annotations

import httpx

DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MODEL = "nomic-embed-text"

#: Ollama truncates at the model's context window anyway; clipping here keeps
#: request bodies bounded and avoids pathological single-chunk payloads.
MAX_CHARS_PER_INPUT = 8000


class EmbedError(RuntimeError):
    pass


class Embedder:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = DEFAULT_HOST,
        batch_size: int = 32,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.batch_size = batch_size
        self._client = httpx.Client(timeout=timeout)
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
        names = {m.get("name", "").split(":")[0] for m in r.json().get("models", [])}
        if self.model.split(":")[0] not in names:
            raise EmbedError(
                f"Model {self.model!r} is not available in Ollama (have: {sorted(names)}). "
                f"Run `ollama pull {self.model}`."
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
