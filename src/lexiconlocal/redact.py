"""Credential redaction, applied before anything reaches the database.

Phase 1 confirmed real credential material in the corpus (a private ``.pem``,
several ``.env`` files, a ``.cer``). File-level exclusion in ``config.yaml``
handles the obvious carriers; this module handles secrets pasted into prose and
transcripts, where no filename hints at them.

Redaction is deliberately eager. A false positive costs a few unsearchable
characters; a false negative puts a live key in a searchable index.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # PEM blocks -- collapse the whole body, not just the header.
    (
        "pem",
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b")),
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("basic-auth-url", re.compile(r"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")),
    ("private-key-assign", re.compile(r"\b(?i:private_key|secret_key|api_key|access_token|client_secret)\s*[:=]\s*['\"]?[A-Za-z0-9/_+=-]{20,}['\"]?")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

#: Long unbroken base64-ish runs that survived the named patterns above.
#: Tuned high enough that git hashes (40) and normal words do not trigger.
#:
#: NB: ``/`` is deliberately NOT in this class. Including it let a match span
#: path separators, so the first real index redacted the middle of ordinary
#: file paths and URLs -- e.g. ``/[REDACTED:high-entropy].swift:37-92``. Paths
#: and symbols are exactly what exact-identifier search depends on, so eating
#: them is worse than missing a base64 blob (which still gets caught segment by
#: segment, since real blobs contain long runs without a slash).
_HIGH_ENTROPY = re.compile(r"\b[A-Za-z0-9+]{60,}={0,2}\b")


def redact(text: str) -> tuple[str, list[str]]:
    """Return ``(clean_text, kinds_found)``.

    Every match becomes ``[REDACTED:<kind>]`` so the surrounding prose stays
    searchable and the redaction itself is visible rather than silent.
    """
    kinds: list[str] = []
    out = text
    for kind, pat in _PATTERNS:
        if pat.search(out):
            kinds.append(kind)
            out = pat.sub(f"[REDACTED:{kind}]", out)

    def _entropy_sub(m: re.Match[str]) -> str:
        s = m.group(0)
        # Require genuine mixed-case-plus-digit shape; long lowercase runs are
        # usually hashes or encoded ids that carry no secret.
        if any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s):
            return "[REDACTED:high-entropy]"
        return s

    new = _HIGH_ENTROPY.sub(_entropy_sub, out)
    if new != out:
        kinds.append("high-entropy")
        out = new

    return out, kinds
