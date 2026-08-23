"""Path admission: the single gate every filesystem read passes through.

This module exists because of D-2026-08-19-08: access control must not be
derived from the index. The index is a disposable artifact rebuilt by code that
changes; if "is it indexed?" were the test, one indexing bug would become a
disclosure bug. So a path is admitted on its own merits -- canonicalised,
proven to be inside a configured root, and checked against the never-serve
rules -- whether or not any document exists for it.

Everything that fails is a **404**, never a 403. A 403 confirms the file is
there, which is exactly the fact `private/` exists to withhold.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from ..config import Config

#: Filename patterns refused regardless of configuration or location. These
#: duplicate `exclude_files` on purpose: config is editable and this is not, and
#: two independent lists are what keeps an editing mistake from becoming a leak.
NEVER_SERVE_GLOBS: tuple[str, ...] = (
    ".env", ".env.*", "*.env",
    "*.pem", "*.key", "*.cer", "*.crt", "*.p12", "*.pfx", "*.jks",
    "id_rsa*", "id_ed25519*", "*.keystore",
    "credentials", "credentials.*", ".netrc", ".pgpass",
    "*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.db",
)

#: Directory *names* refused anywhere in a path. These are names that mean the
#: same thing wherever they appear.
#:
#: `private` is deliberately NOT in this set, even though `~/Lexicon/private`
#: is the rule's whole reason for existing. Matching the bare name anywhere
#: matches an incidental fact about the filesystem rather than a policy: on
#: macOS `/tmp` and `/var` are symlinks into `/private/...`, so *every*
#: canonicalised path under them contains a `private` component. The policy is
#: "the Lexicon's private directory", and `never_serve_roots()` expresses that
#: as containment instead.
NEVER_SERVE_DIRS: frozenset[str] = frozenset({
    ".git",         # object store, config with tokens, hooks
    ".ssh",
    "secrets",
    "node_modules",
})

#: Extensions the document view will render as text. Anything else is refused:
#: this server has no business handing out binaries, and the archive is full of
#: them (1,769 ChatGPT `.dat` attachments alone).
TEXT_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".py", ".swift", ".js", ".ts", ".css", ".html",
    ".sh", ".zsh", ".bash", ".c", ".h", ".cpp", ".hpp", ".m", ".mm", ".sql",
    ".plist", ".xml", ".csv", ".tsv", ".log",
})

#: A document view that streamed a gigabyte would hang the browser and the
#: server thread with it. Larger files are truncated with a visible marker.
MAX_FILE_BYTES = 2_000_000


@dataclass(frozen=True)
class Admission:
    """The verdict, plus why -- the reason is for logs, never for the client."""

    ok: bool
    path: Path | None = None
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def allowed_roots(cfg: Config) -> list[Path]:
    """Every directory tree a document may legitimately come from.

    The Lexicon root covers curated notes and the archive; the configured
    source roots cover in-place repo Markdown. Nothing else is servable, which
    means a path outside them fails before any pattern matching happens.
    """
    roots = [cfg.lexicon_root]
    roots += [r.path for r in cfg.source_roots]
    out: list[Path] = []
    for r in roots:
        try:
            out.append(Path(os.path.realpath(r)))
        except OSError:
            continue
    return out


def never_serve_roots(cfg: Config) -> list[Path]:
    """Trees that are refused wholesale, by containment rather than by name.

    `~/Lexicon/private` is named here directly and not merely inherited from
    `config.yaml`: config is editable, and an editing mistake must not be able
    to turn the one directory that exists to hold secrets into a served one.
    """
    roots = [cfg.lexicon_root / "private", *cfg.never_index]
    out: list[Path] = []
    for r in roots:
        try:
            out.append(Path(os.path.realpath(r)))
        except OSError:
            continue
    return out


def _within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def admit(cfg: Config, raw: str, *, require_file: bool = True) -> Admission:
    """Decide whether *raw* may be read, and return its canonical path.

    Order matters. Canonicalisation comes first so that `..` segments and
    symlinks are gone before any rule is applied -- a rule evaluated against an
    uncanonicalised string is a rule that can be walked around.
    """
    if not raw or "\x00" in raw:
        return Admission(False, reason="empty or NUL-bearing path")

    try:
        # expanduser first: `~/Lexicon/private/x` must be caught, not treated
        # as a relative path that lands outside every root and merely 404s for
        # the wrong reason.
        expanded = Path(raw).expanduser()
        canonical = Path(os.path.realpath(expanded))
    except (OSError, RuntimeError, ValueError) as e:
        return Admission(False, reason=f"uncanonicalisable: {e}")

    if not canonical.is_absolute():
        return Admission(False, reason="not absolute after canonicalisation")

    roots = allowed_roots(cfg)
    if not any(_within(canonical, root) for root in roots):
        return Admission(False, reason="outside every configured root")

    # Never-serve, applied to the canonical path so a symlink cannot launder a
    # forbidden location into an allowed-looking one.
    for banned_root in never_serve_roots(cfg):
        if _within(canonical, banned_root):
            return Admission(False, reason=f"inside a never-serve tree: {banned_root}")

    parts = set(canonical.parts)
    banned = parts & NEVER_SERVE_DIRS
    if banned:
        return Admission(False, reason=f"path traverses a never-serve directory: {sorted(banned)}")

    name = canonical.name
    for pat in NEVER_SERVE_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return Admission(False, reason=f"filename matches never-serve pattern {pat!r}")

    # The configured rules get their say too, so an exclusion the operator adds to
    # config.yaml takes effect here without a code change.
    if cfg.is_never_indexed(canonical) or cfg.is_excluded_file(name):
        return Admission(False, reason="excluded by config.yaml")

    if require_file:
        if not canonical.is_file():
            return Admission(False, reason="not a regular file")
        if canonical.suffix.lower() not in TEXT_SUFFIXES:
            return Admission(False, reason=f"non-text suffix {canonical.suffix!r}")

    return Admission(True, path=canonical)


def read_text(path: Path) -> tuple[str, bool]:
    """Read an admitted file. Returns ``(text, truncated)``."""
    data = path.read_bytes()[: MAX_FILE_BYTES + 1]
    truncated = len(data) > MAX_FILE_BYTES
    if truncated:
        data = data[:MAX_FILE_BYTES]
    return data.decode("utf-8", errors="replace"), truncated
