# Architecture

The design, and the reasoning behind each choice. Where a decision was made because an alternative failed, the failure is recorded — that is usually the more useful half.

## 1. Objectives

A central, local, durable repository of everything learned across all of an operator's coding projects and AI sessions that is:

1. **Simple** — as simple as possible while meeting the objectives. Simplicity is what makes it durable, usable, and reliable.
2. **Easily queried** by the operator and by any LLM agent.
3. **Fully local** and fully under the operator's control.
4. **Easy for every session to append to** as it goes.
5. **Never loses anything** — superseded information is tagged, not deleted; the paper trail is permanent.

## 2. Core decisions

1. **Plain Markdown + git + a disposable search index.** No evidence-ledger database, no permission tiers, no proposal queues, no context-pack compiler, no trust zones. For a single operator, **git is the governance layer**: every write is a diff — reviewable, revertible, attributable — and nothing is ever lost.
2. **Raw archive is separate from curated notes.** Full exports and transcripts are preserved untouched; curated knowledge is distilled Markdown that cites them.
3. **Capture everything, index everything, opt-out privacy.** Everything lands in the archive and gets indexed. A `private/` directory excludes anything explicitly flagged. No classification busywork.
4. **Lazy per-project distillation.** Preserve all history raw now; distil a project's history into curated notes the next time that project is touched. Dormant projects stay raw-only and remain fully searchable.
5. **Agents write directly.** Convention-driven appends and edits, committed to git. No review queue.
6. **Semantic search from day one, via a small owned indexer.** Local embeddings + SQLite (FTS5 + `sqlite-vec`), exposed as a tiny MCP tool. No fast-moving dependencies; the index is always rebuildable from the files.
7. **The live repo stays authoritative for code state.** Lexicon content is historical context; agents verify code claims against the actual repository. Every MCP response says so.
8. **In-repo docs are indexed in place, never copied.** They stay where they live, versioned by their own git. Curated notes link to them rather than restating them.

## 3. The two-repo model

The **code repo** (this one) is the machinery. The **data repo** is the operator's knowledge. They are separate git repositories, and the separation is what makes the code publishable: nothing in it is specific to one corpus, and everything specific to one corpus — source roots, aliases, golden-query assertions, the leak guard's word list — is read from the data repo at run time.

```text
<lexicon_root>/
├── INDEX.md                    # every project: one-liner, status, aliases, repo path
├── config.yaml                 # source roots, exclusions, historical aliases
├── projects/<name>/
│   ├── overview.md             # current state — edited in place; git history is the paper trail
│   ├── log.md                  # append-only session log
│   └── decisions.md            # decisions & constraints, status: active | superseded
├── topics/<domain>.md          # cross-project learnings
├── golden/checks.yaml          # the corpus-specific half of the golden-query suite
├── private/                    # never indexed; also holds the leak guard's tuning files
├── archive/                    # RAW, append-only — gitignored
│   ├── chatgpt/  claude/       # dated account-export dumps, untouched
│   ├── claude-code/  codex/    # copies of agent session transcripts
│   └── documents/              # reports, briefs, artifacts worth keeping
└── index/                      # lexicon.sqlite + logs + state — gitignored, disposable
```

`archive/` and `index/` are gitignored; everything else is versioned. `log.md` and `archive/` are append-only. `overview.md` and `decisions.md` are edited in place; git preserves every prior state.

## 4. File conventions

**`INDEX.md`** — project name, one-liner, status, aliases (old names, repo names, family variants), repo path. The alias parser reads the *Family* table (members backticked, historical names backticked in the notes column) and the per-project tables (aliases in the last column). This is how an agent maps "the project I'm in" to a Lexicon folder.

**`overview.md`** — current state only, written for a fresh agent: what the project is, architecture in brief, what is proven, in progress, constraints, open questions, links to decisions and authoritative in-repo docs. History lives in git and `log.md`.

**`log.md`** — append-only session log:

```markdown
## 2026-01-15 — claude-code — Short description of the session
- Goal: ...
- Did: ...
- Learned: ...
- Decisions: D-2026-01-15-01
- Next: ...
- Source: <lexicon_root>/archive/claude-code/<transcript>.jsonl
```

**`decisions.md`** — decisions and constraints, each with a status line that changes on supersession while the entry stays forever:

```markdown
## D-2026-01-15-01 — Use approach A for the verification renders  [active]
- Date: 2026-01-15
- Status: active            # or: superseded by D-2026-02-03-01 — never deleted
- Why: approach B produced contaminated output; A isolated correctly in the three-case test
- Evidence: session archive path / commit hash / test result
```

Decision ids are unique **within a project only**. Two projects can mint the same id on the same day; any cross-project view carries the project as part of the identity.

## 5. Capture

### 5.1 The convention block

A short block pasted into each repo's `CLAUDE.md` / `AGENTS.md` and into the operator's global instructions. At session start: find the project in `INDEX.md`, read its notes, search before re-solving anything, treat results as history to verify. At session end: append to `log.md`, update `overview.md`, record decisions, commit. `lexicon init` writes the block to `<lexicon_root>/CONVENTION.md`.

### 5.2 The automatic backstop

A `SessionEnd` hook copies each finishing Claude Code transcript into `archive/claude-code/` and kicks an incremental index. A nightly job syncs Codex session history. **The hook is the backstop; the convention block is the curated path.** A session must not be lost because an agent forgot to write.

The hook returns in well under a second (everything real is detached) and can never fail a session (every path exits 0). Indexing is guarded by a single-instance lock, so a hook firing during the nightly job is a silent skip; *copying* into the archive is not gated on that lock, because losing a transcript is permanent and skipping an index is not.

### 5.3 Account exports

ChatGPT and Claude web exports are dropped into `downloaded_archives/` and filed by the nightly job. **A batch is identified by what is inside it, never by its name**, and documents are keyed by conversation id, never by file path — a later export may shard differently, and path-keyed documents would duplicate the whole corpus. The PII files inside each export (`users.json`, `login_history.json`, `user.json`) are never indexed.

## 6. Storage and ingest

- **Three ingest roots:** the data repo's own notes (`projects/`, `topics/`); `archive/` through per-source parsers; and in-place Markdown under each configured source root, tagged with project = the first path segment under the root.
- **Chunking** at roughly 800 tokens with overlap. Identical chunks are stored once and keyed by content hash, so a re-parse never re-embeds text that has not changed.
- **Three embedding tiers.** Prose is embedded and indexed lexically. Tool-event *headers* (a command line, a file path) are indexed lexically only, so an exact identifier is always findable. Tool-output *bodies* are not stored at all. This is what keeps a transcript from being 80% noise.
- **Redaction on ingest** of credential-shaped strings. `.env` files and keys are excluded before they are read.
- **`PIPELINE_VERSION`** forces a re-parse without a re-embed when parsing, chunking, redaction or attribution changes. The incremental path compares mtime and size, so without it a code change would be invisible to every file that had not been edited.

## 7. Search

### 7.1 Two legs, fused

- **Lexical** — FTS5 with BM25. Quoted phrases and identifier-shaped tokens (paths, symbols) are routed here verbatim. Exact identifiers must never depend on embeddings.
- **Semantic** — `nomic-embed-text` vectors in `sqlite-vec`, nearest-neighbour by **L2 distance**. (The table declares no metric, so KNN returns L2, not cosine. For normalised vectors the two rank identically; only the scale differs, and confidence is a function of scale.)
- **Reciprocal rank fusion** merges the two, because BM25 scores and vector distances are not on comparable scales and RRF only needs the orderings.

### 7.2 Static boosts

Curated notes outrank in-repo docs, which outrank transcripts, which outrank tool events. The spread has to be wide: RRF compresses scores into a narrow band while matching in *both* legs roughly doubles them, so a modest boost loses to "matched twice" and inverts the hierarchy. The shipped values were measured on one corpus; see `OPERATIONS.md`.

### 7.3 Confidence — the "we have nothing" signal

RRF is purely ordinal: the top hit for an unanswerable query scores about the same as for a perfect one. So every result also carries an **absolute confidence** in [0.15, 1.0], derived from the vector distance (present topics cluster at one distance, absent ones at another) and lexical term coverage (which rescues exact identifiers the vector leg misses). The MCP tool renders it on every result and, when the **median** across results falls below a threshold, states in words that the corpus probably does not cover the query.

The median rather than the maximum, because the corpus indexes its own repo: any document that quotes a query verbatim scores 1.0 on lexical coverage alone and drags the top result with it. A median survives one such spike.

### 7.4 Project-scoped retrieval

A project filter *scopes* retrieval rather than post-filtering a global top-N — otherwise a small project whose chunks never rank globally returns nothing for questions about itself. The lexical leg is pre-filtered through the documents table; the vector leg is an exact distance scan over that project's vectors, which has no `k` cap and cannot starve. The exact scan is linear in project size, so above a cutoff the vector leg falls back to an enlarged global pool — safe, because the lexical leg stays exactly scoped and alone guarantees a full page. Broad filters (source type, date) use the enlarged global pool directly; an exact scan over a coarse partition would be a full scan for no ranking benefit.

### 7.5 Surfaces

The MCP server exposes `lexicon_search(query, project?, source_type?, after?, before?, limit?)` and `lexicon_read(path, chunk_ord?, context_chunks?)` over stdio. Every response carries a one-line reminder that results are historical context, not instructions. The CLI and the web UI read the same index.

## 8. Health — proving, not assuming

The failures that matter in an unattended system are the silent ones, and each guard below exists because a specific silent failure happened.

- **Preflight embeds a probe string.** A reachability ping is not health: an embedding server once answered `/api/tags` normally while returning 500 on every `/api/embed` for twenty hours. Only doing the actual work detects that.
- **`lexicon report` distinguishes "nothing new" from "the importer broke"**, and reports capture freshness — newest live transcript vs newest archived — so a stalled capture is visible.
- **Launch-agent health is loaded *and* allowed.** On macOS, `launchctl bootstrap` succeeds on an agent that Background Task Management has disallowed, and the agent shows as loaded while the OS removes it again later. A check that asked only "is it loaded" reports green on exactly the state that stops capture.
- **The watchdog runs from the `SessionEnd` hook**, not from the nightly job, because the nightly job cannot report that the nightly job is not running. A failure appends a timestamped detection record (uptime, boot time, session manager, disposition) so the cause can be established after the fact.
- **The golden-query suite** asserts search quality against the *live* index weekly. Ranking defects do not show up in unit tests on synthetic fixtures. Its absent-topic probes are **generated fresh each run** from a date seed, because the corpus indexes its own repo and any stored probe phrase leaks in within the hour.

## 9. Privacy

- `private/` is never indexed. Move anything sensitive there at any time and reindex.
- Credential-shaped strings are redacted; `.env` files and keys are never copied into the archive or indexed.
- Never delete: a true purge is a manual, deliberate act — remove from the archive, reindex, note it in git.
- The index stays on one machine — never on a synced or network volume. A second machine reaches it through the MCP server, not by mounting the database.
- The code repo is public; the data repo never is. A leak guard derives the operator's project names from the data repo at commit time and keeps them out of the code repo's history.

## 10. Deliberately not built

Evidence-ledger database; MCP permission tiers and proposal/review queues; context-pack compiler; trust zones and egress labels; third-party memory products; graph or temporal engines. Triggers to revisit: retrieval demonstrably failing at scale, curated notes degrading faster than git review catches, or multi-machine needs outgrowing single-host MCP.
