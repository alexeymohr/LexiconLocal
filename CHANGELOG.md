# Changelog

## 0.3.2

Privacy hardening and stronger regression coverage. No reindex; nothing stored
changes.

### Fixed — local-only was not fully enforced

- **Ollama traffic no longer honours environment proxies.** 0.3.1 validated that
  the embedding host was loopback, which established where a request was
  *pointed* and not where it would *travel*. httpx applies `HTTP_PROXY` /
  `ALL_PROXY` to loopback URLs unless `NO_PROXY` excludes them — verified against
  the transport it selects — so an environment variable could have routed corpus
  text through a proxy while every host check passed. All Ollama traffic now goes
  through one client with `trust_env=False`.
- **Every Ollama cloud-tag form is recognised.** The guard matched `model:cloud`
  only, so `gpt-oss:120b-cloud`, `qwen3-coder:480b-cloud` and any uppercase
  variant went through. The tag is now read after the final colon and matched
  case-insensitively against `cloud` and any `-cloud` suffix. `preflight` had a
  second hand-written copy of the rule; there is now one predicate.

### Documentation

- The README states what enforcement proves and what it does not: loopback
  addressing means the request reached Ollama on this machine, not that Ollama
  performed the inference here. Ollama's own `OLLAMA_NO_CLOUD=1` is documented as
  an optional operator measure — verified present in Ollama 0.33.0. LexiconLocal
  does not set it.
- The 0.3.1 note on filtered ranking is qualified where it is made: lexical
  ranking happens within the requested subset, vector ranking only when the
  subset is at or below the exact-scan threshold.

### Investigated, not implemented

`/api/tags` was inspected for a signal that a model has local weights. Entries
carry `size`, `digest`, `details` and `capabilities`, but with only local models
installed there was nothing to compare against, and `size > 0` is a heuristic
rather than a documented guarantee. No check was added; recording the finding is
the honest outcome.

### Tests

Filtered-search regressions now name the document they expect and assert it came
back. The previous ones asserted `all(...)` over the results, which an empty list
satisfies — they proved a filter returned nothing wrong, never that it returned
the right thing. Verified against the defect they guard: with candidate
generation reverted, the date-scoped case returns a distractor rather than the
target, and returns one result rather than none, so a non-empty assertion alone
would not have caught it.

## 0.3.1

Hardening pass. No reindex is required and nothing stored changes: the pipeline
version is untouched, so an existing index carries straight over.

### Breaking

- **Embedding is enforced local-only, not merely defaulted to it.** A non-loopback
  `--host` is now refused before any request is made, and a `:cloud` model is
  refused outright. The docstring had promised this for as long as the file has
  existed while the code accepted any host it was given. If you point Lexicon at
  an Ollama on another machine, it will now stop instead of embedding your corpus
  across the network. There is deliberately no override.
- **`lexicon search` exits 2 on a refused host** rather than falling back to
  lexical-only results. An unreachable Ollama is a degradation; a refused target
  is an operator error, and answering the query anyway hid the fact that the
  `--host` you asked for was ignored.

### Changed behaviour

- **Undated documents satisfy neither `--after` nor `--before`.** They previously
  failed `after` and passed `before`, because a missing date was compared as an
  empty string. That asymmetry was an accident, not a policy.
- **`lexicon_read` refuses to guess.** When a partial path matches more than one
  document it now returns the candidate paths instead of silently reading
  whichever row the database reached first. `%` and `_` in a path are treated as
  literal characters rather than SQL wildcards.
- **Filtered search scopes retrieval instead of trimming its output.**
  `source_type`, `kind` and the date bounds now constrain candidate generation,
  as `project` already did. Previously a filtered search took the best 1,600
  chunks of the whole corpus and discarded the rest, so a well-matching document
  ranked below that cutoff could not be returned at all.

  Precisely: lexical retrieval and ranking happen inside the requested subset,
  and vector ranking does too when the subset is at or below the exact-scan
  threshold. Above that threshold the vector leg keeps its global fallback, so
  its ranking is still computed against the whole corpus. Saying ranking happens
  within the subset, without that qualifier, would overstate it.
- **Human-readable search leads with confidence**, keeping the RRF score as
  secondary diagnostics, and emits the same low-coverage warning the MCP surface
  has always had. JSON output is unchanged.

### Known limitation

Above the exact-scan cutoff the vector leg remains a global KNN. Lexical
starvation is fixed and ranking now happens within the requested subset, but a
match that only vector similarity would find, ranked outside the global
candidate pool, still cannot surface for a broad filter.

### Performance

Measured against the previous release on a ~196,000-chunk corpus, worst single
call 470 ms against the 500 ms bar. Broad filters cost 66–103 ms more; narrow
ones got faster (a narrow source-type filter by 117 ms, `kind=tool_event` by
182 ms, the latter by skipping a vector leg those chunks never had). Unfiltered
and project-only searches are unchanged.

### Fixed

- The MCP server advertised version `0.2.0` while the package was `0.3.0`. It now
  reports the installed version.
- The SessionEnd hook claimed `--ignore-existing` kept the archive append-only. It
  does not use that flag, and adding it would be a bug: a transcript is still
  being written when a session ends. The archive is non-destructive — nothing
  already there is deleted, and a copied session file may be refreshed as its
  source grows. Documentation now says so.

## 0.3.0

Initial public release.
