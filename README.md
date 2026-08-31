# Lexicon Local

**A local, offline second brain for your coding agents.**

Every session you have with a coding AI — Claude Code, Codex, ChatGPT, Claude web — and every Markdown file across every repo you work on, captured automatically, indexed locally, and searchable by the next agent before it starts guessing.

> *"It's a second brain for my AI coding assistants — everything they and I have ever figured out gets captured automatically, and any new session can search all of it before it starts guessing."*

Nothing leaves your machine. The only network call is to Ollama on `localhost`.

---

## What it does

- **Captures** — a Claude Code `SessionEnd` hook archives each transcript the moment a session ends; a nightly job sweeps up Codex sessions; account exports from ChatGPT and Claude web are dropped in and parsed. Your repos' own `.md` files are indexed in place, never copied.
- **Indexes** — one SQLite file holding two search engines: FTS5 for exact words, `sqlite-vec` for meaning. Chunked, deduplicated by content hash, credentials redacted on the way in.
- **Searches** — hybrid lexical + semantic retrieval fused with reciprocal rank fusion, curated notes boosted over raw transcripts, and an absolute **confidence** score so an agent can tell *"we have this"* from *"we have nothing"* instead of bluffing with the least-bad match.
- **Serves** — an MCP server any agent can call (`lexicon_search`, `lexicon_read`), a CLI, and a read-only local web UI for you.
- **Distils** — raw transcripts are searchable from day one; curated `overview.md` / `decisions.md` / `log.md` notes are written per project when it is worth the pass. `lexicon distill` ranks the backlog.
- **Watches itself** — preflight proves the embedder works by actually embedding something; a weekly golden-query suite asserts search quality against the live index; a watchdog notices when the nightly job has stopped.

## The two-repo model

**This repository is the code.** Your knowledge lives in a **separate repo** — `~/Lexicon` by default — that the code manages but never owns, and that you never publish.

```text
~/Lexicon/                     # your data — a git repo with no remote
├── INDEX.md                   # every project: one-liner, aliases, repo path
├── config.yaml                # what gets indexed
├── projects/<name>/           # curated notes: overview.md, decisions.md, log.md
├── topics/                    # cross-project learnings
├── private/                   # never indexed
├── archive/                   # raw transcripts and exports — non-destructive, gitignored
└── index/                     # the SQLite index — disposable, gitignored
```

The index is derived. Delete it and `lexicon index --full` rebuilds it from the files. **The files are the truth.**

## Install

Requirements: macOS or Linux, Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and [Ollama](https://ollama.com) running locally.

```bash
git clone https://github.com/alexeymohr/LexiconLocal.git
cd LexiconLocal
uv sync
ollama pull nomic-embed-text
```

## First run

```bash
uv run lexicon init                       # scaffolds ~/Lexicon; prints the next steps
# edit ~/Lexicon/config.yaml: point source_roots at the directory that CONTAINS your repos
uv run lexicon preflight                  # proves Ollama, the model, the DB, and the registrations
uv run lexicon index --full               # first index; later runs are incremental
uv run lexicon report                     # what got indexed, and whether the importer is healthy
uv run lexicon search "how did we handle X"
```

Then wire up the agents:

```bash
uv run python scripts/register_mcp.py     # Claude Code, Codex, Claude Desktop
./scripts/install_agents.sh               # macOS: nightly capture + weekly quality guard
```

and add the `SessionEnd` hook so transcripts are captured without anyone remembering to — `lexicon init` prints the exact JSON to paste into `~/.claude/settings.json`. Finally, paste the convention block from `~/Lexicon/CONVENTION.md` into each repo's `CLAUDE.md` or `AGENTS.md` so agents know to look before they leap.

## Three doors

| Door | For | Command |
|---|---|---|
| **MCP server** | agents, in any repo | registered once; tools `lexicon_search`, `lexicon_read` |
| **CLI** | you, in a terminal | `lexicon search`, `report`, `distill`, `preflight`, `agents` |
| **Web UI** | you, in a browser | `lexicon web` — read-only, `localhost` only |

## Platform honesty

The indexer, search, MCP server and web UI are portable Python and should run anywhere SQLite and Ollama do. **The unattended automation is macOS-specific:** the nightly and weekly jobs use `launchd`, notifications use `osascript`, and the launch-agent health check reads Background Task Management. On Linux you still get capture through the `SessionEnd` hook; the scheduled jobs need a cron equivalent nobody has written yet.

This has been run on exactly one machine. The first install on a second one will find something. Please open an issue when it does.

## Calibration honesty

Several constants were **measured, not derived** — the static ranking boosts, the L2 confidence bounds, the absent-topic median threshold, the exact-scan cutoff for project-scoped vector search. They are right for the corpus they were measured on and are starting points for yours. `docs/OPERATIONS.md` says which ones and how to re-measure. The golden-query suite (`scripts/golden_queries.py`) is how you find out whether they still hold; its corpus-specific half is a YAML file you own.

## Privacy

- Everything local. No cloud APIs, no telemetry.
- `private/`, `.env` files, keys, and the PII files inside account exports are never indexed. Credential-shaped strings are redacted on ingest.
- Your data repo has no remote. This code repo is public — and ships with a leak guard (`scripts/leak_guard.py`, wired in by `scripts/install_hooks.sh`) that derives the names of *your* projects from *your* Lexicon at commit time and refuses to let them into a commit or a push. If you contribute, install the hooks first.

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the design and the reasoning behind it
- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — every key in `config.yaml`
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — what runs unattended, what each failure means, what was calibrated

## Changes

[`CHANGELOG.md`](CHANGELOG.md) — what changed in each release, and which changes
are breaking.

## License

MIT. See [`LICENSE`](LICENSE).
