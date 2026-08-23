# LexiconLocal

A local, offline knowledge base for coding agents: every session transcript and every repo's Markdown, captured automatically and made searchable, so the next session starts by knowing what came before. **This repo is the code** (indexer, MCP server, capture scripts, web UI). **The data lives in a separate repo** — by default `~/Lexicon` — which this code manages but never owns, and which is never published.

## Read first

1. `README.md` — what it is, install, first run
2. `docs/ARCHITECTURE.md` — the design; do not deviate without flagging it
3. `docs/OPERATIONS.md` — what runs unattended, and what every failure means

## This repository is public. The operator's data is not.

- **Commit messages, comments, tests and docs describe the system, never the operator's corpus.** No project names, no repo names, no sizes, no business context. *"The scoped vector scan is linear in project size"* is fine. *"Project X has 7,722 vectors"* is a leak. Where a measurement justifies a constant, state it as a shape ("the largest project"), not an identity.
- `scripts/leak_guard.py` derives the set of protected names from the operator's live Lexicon and runs on every commit and push (`scripts/install_hooks.sh`). **Do not bypass it.** `LEAK_GUARD=off` exists for a deliberate override with the hits in view; `--no-verify` is never appropriate here.
- Test fixtures are synthetic. **Never copy real transcripts, exports, or note files into this repo**, even as examples.

## Tech stack (fixed — do not substitute)

- Python 3.12+, `uv` for environment and packaging, single package `lexiconlocal`
- SQLite: FTS5 for lexical, `sqlite-vec` for vectors — one DB file at `<lexicon_root>/index/lexicon.sqlite`
- Embeddings: Ollama, `nomic-embed-text`, local only
- MCP: official `mcp` Python SDK, stdio transport
- pytest; small synthetic fixtures only

## Hard rules

- **Everything local.** No network calls except localhost Ollama. No cloud APIs, no telemetry.
- **The data repo is sacred:** `archive/` and every `log.md` are append-only — never edit, rewrite, or delete their content. `overview.md` / `decisions.md` / `INDEX.md` may be edited; superseded content gets status-tagged, never removed.
- **The index is disposable; the files are truth.** Any state that cannot be rebuilt from the data repo's files with `lexicon index --full` is a design violation.
- **Fail loud.** A parser that skips a file must record it. `lexicon report` must always distinguish "nothing new" from "importer broke."
- **Never index `<lexicon_root>/private/`**, `.env` files, or credential-like content (redact on ingest).
- **Any change to parsing, chunking, redaction, or attribution MUST bump `PIPELINE_VERSION` in the same commit** — otherwise the change never reaches already-indexed documents. The incremental path compares mtime+size, so a code change alone is invisible to files that have not been edited.
- **After every `PIPELINE_VERSION` bump, run `uv run python scripts/golden_queries.py`** — it asserts search quality against the live index and exits non-zero on regression. Ranking defects do not show up in the unit tests. The corpus-specific half of that suite is `<lexicon_root>/golden/checks.yaml`; the harness is generic.
- **Never quote a golden-query absent-topic probe phrase in any `.md` file, decision record, or commit message.** The harness generates them fresh from a date-seeded combination each run precisely so a leak cannot compound into tomorrow's check. The word pools it draws from are fine to reference — only a specific generated combination is a probe.
- **Calibrated constants are starting points, not truths.** The static boosts, the L2 confidence bounds, the absent-median threshold and the exact-scan cutoff were measured on one corpus. Changing one needs a measurement, not a hunch, and the golden suite is how it is checked.
- Commit in logical units with clear messages. **Do not commit the data repo's own content from here** — notes a distillation pass produced belong to the session that produced them, so attribution matches what actually happened. Maintenance performed from this session (a repaired `INDEX.md` row, a `config.yaml` entry, a decision record) may be committed here, and the message should say what it was.

## Commands

```bash
uv sync                          # set up env
uv run pytest                    # must pass before any change is declared done
uv run lexicon init [ROOT]       # scaffold a new data repo (never overwrites)
uv run lexicon preflight         # prove Ollama, the model, the DB, the agents, the MCP registration
uv run lexicon index             # incremental index of all configured sources
uv run lexicon index --full      # full rebuild (always safe)
uv run lexicon search "..."      # CLI search (hybrid FTS + vector)
uv run lexicon report            # ingest coverage / health report
uv run lexicon web               # read-only local UI
uv run lexicon distill           # projects with raw material but no curated notes
uv run python scripts/golden_queries.py      # search-quality guard against the live index
uv run python scripts/leak_guard.py --tree   # audit the whole tree for protected names
```
