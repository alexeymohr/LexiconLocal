# Changelog

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
  ranked below that cutoff could not be returned at all, and what survived kept a
  ranking computed against material the filter had excluded.
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
