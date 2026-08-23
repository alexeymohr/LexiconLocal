# Operations

What runs unattended, what every failure means, and which numbers were measured rather than derived.

A healthy system is **silent**. Every alarm below fires only on a problem, because an alarm that fires on good days gets ignored on bad ones.

## What runs, and when

| When | What | Label |
|---|---|---|
| Every Claude Code session end | `scripts/session_end_hook.sh` — archive the transcript, kick an incremental index, run the watchdog at most once a day | (hook in `~/.claude/settings.json`) |
| Daily 03:30 | `scripts/lexicon_daily.sh` — sync Codex sessions, re-snapshot memories on change, file dropped exports, preflight → index → report, regenerate `HOME.md`, safety-net commit of the data repo, export-freshness warning | `com.lexiconlocal.daily` |
| Sunday 04:00 | `scripts/run_golden.sh` — the golden-query suite against the live index | `com.lexiconlocal.golden` |
| 1st of the month 10:00 | a notification reminding you to run the account exports | `com.lexiconlocal.export-reminder` |

macOS only: the three scheduled jobs are `launchd` agents rendered from `scripts/launchd/*.template` by `scripts/install_agents.sh`. The hook is portable. If the Mac is asleep at the scheduled time, `launchd` runs the job on wake.

Logs: `<lexicon_root>/index/logs/` — `daily.log`, `golden.log`, `hook.log`, plus `launchd` stdout/stderr captures. The daily log keeps the last 30 runs.

## Preflight

`lexicon preflight` proves seven things, and the daily job runs it before indexing:

| Check | What "proven" means |
|---|---|
| `ollama` | reachable at the configured host; started via the Ollama app if not |
| `embed model` | the local model is present; a `:cloud` variant is never an acceptable substitute |
| `embedding` | **a probe string was actually embedded and a real vector came back.** Reachability is not health: a server once answered every ping for twenty hours while failing every embed. On failure, one restart is attempted and the probe re-run |
| `database` | the index file opens and its directory is writable |
| `lexicon repo` | the data repo exists and is a git repository |
| `launch agents` | (macOS) each agent is registered with `launchctl` **and** not disallowed by Background Task Management — see below |
| `mcp registration` | each installed client's config carries the `lexicon` server and its command exists on disk |

Exit 2 on any failure. The daily job continues past a preflight failure (parsing needs no embedder; stage one is stored and the report says what is pending), but records it as a problem.

## What each failure means

**`lexicon report` exits non-zero.** Read the verdict line:

- `IMPORTER ERRORS` — a parser failed on a file. The file list is above the verdict. A parser that skips a file must record it; this is that record.
- `DEGRADED` — parsing is fine but prose chunks are unembedded. The embedder was down during the run. `lexicon preflight`, then `lexicon index` to resume.
- `CAPTURE STALLED` — the newest live transcript is more than 26 hours newer than the newest archived one. The capture path has stopped. `lexicon agents` says which job.
- `NEVER ARCHIVED` — a configured live source has never been synced at all.

**`lexicon agents` shows FAIL.** Three distinct causes, reported distinctly:

- *plist missing* — `scripts/install_agents.sh`.
- *not registered with launchd* — same script; it boots out any stale instance and bootstraps fresh.
- *disallowed in Background Task Management* — **a script cannot fix this.** The agent was switched off in System Settings → General → Login Items & Extensions, where unsigned `bash` and `osascript` entries appear under "Unknown Developer" and are easy to switch off by accident. Re-enable it there. Until you do, `launchctl bootstrap` will succeed and `launchctl list` will show the agent, and macOS will remove it again — which is why the check reads both states.

A detection record is appended to `index/state/agent-detections.jsonl` each time the watchdog finds an agent down: wall-clock time, boot time, uptime, GUI session manager, console user, and the disposition of each failing agent. It exists so a recurrence can be diagnosed from evidence rather than re-guessed.

**The golden suite fails.** Read the section. Sections 0, 4 and 4b are corpus-independent and a failure there is a real regression in ranking, probe generation or confidence calibration. Sections 1–3, 5 and 5b assert things *you* wrote in `golden/checks.yaml`; a failure there is either a regression or a stale assertion, and the section's output says which result came back.

**A macOS password prompt appears out of nowhere.** Something unattended invoked `sfltool dumpbtm`, which reads Background Task Management and raises the admin dialog. The code gates that read on a TTY being attached; if you see the prompt, find what called `lexicon agents` or `install_agents.sh` without one. `LEXICON_BTM=skip` silences the read entirely.

## Capture freshness

`lexicon report` compares the newest file under each live transcript source (`~/.claude/projects`, `~/.codex/sessions`) with the newest under its archive directory. `rsync` preserves mtimes, so the difference is literally the capture lag with no clock arithmetic. A full daily cycle of accumulation is normal; past 26 hours it is not accumulation, it is a stall.

## The leak guard

This code repo is public. `scripts/leak_guard.py` keeps your project names out of it:

- The protected set is **derived at run time** from your data repo — every top-level entry under every source root, every curated project, every `INDEX.md` row, every alias, your username — minus short and generic words. Nobody maintains a list.
- `scripts/install_hooks.sh` wires it into `pre-commit` (staged content), `commit-msg` (the message), and `pre-push` (prints every outgoing commit, guards the whole range, waits for a typed `yes`; refuses without a terminal).
- `<lexicon_root>/private/leak-allow.txt` permits names it should not block (this repo's own name lands in the set because it lives under a source root). `leak-extra.txt` adds names no filesystem knows.
- `LEAK_GUARD=off` overrides a refusal, with the hits printed anyway. `--no-verify` bypasses git hooks entirely and is never appropriate here.
- **The guard catches names. It cannot catch meaning.** A transcript phrase in a test, a client in prose, a constant explained by your business — the push-time read is for those.

## Calibrated constants — starting points, not truths

Every number below was **measured on one corpus** and is correct for it. For yours it is where to start. Each lives in `src/lexiconlocal/search.py` with the measurement that produced it in a comment; the golden suite is how you check whether it still holds.

| Constant | Value | What it is | How to re-measure |
|---|---|---|---|
| `BOOSTS` | lexicon 3.0 · memories 2.4 · project briefs 2.0 · top-level repo docs 2.0 · deep repo docs 0.7 · transcripts 1.0 · tool events 0.5 · abandoned branches 0.3 | Static multipliers that make the curated-over-raw hierarchy actually separate under RRF | Golden section 1: does curated material lead for a query a curated note answers? |
| `VEC_CONFIDENT_L2` / `VEC_HOPELESS_L2` | 0.80 / 0.95 | L2 distances at which a vector hit is fully confident / hopeless | Search a handful of topics your notes cover and a handful they do not; present topics should cluster below the first, absent above the second |
| `CONFIDENCE_ABSENT_MEDIAN` | 0.60 | Below this median confidence the MCP tool says the corpus likely does not cover the query | Golden section 4b measures both bands every run and fails if they meet |
| `EXACT_SCAN_MAX_CHUNKS` | 3,000 | Above this many chunks a project-scoped vector search falls back to the global pool | A latency budget as a row count: the exact scan costs ~85 µs per vector. Re-measure if the machine or corpus changes; golden section 5b's 500 ms bar is the tripwire |
| `BROAD_CANDIDATES` | 1,600 | Global pool size for broad filters | KNN cost plateaus around here; it is the first size at which a narrow source type fills a page |
| `RRF_K` | 60 | The standard RRF constant | Leave it |

## Routine maintenance

- **After any change to parsing, chunking, redaction or attribution:** bump `PIPELINE_VERSION` in the same commit, then run the golden suite.
- **After an Ollama upgrade:** re-embed a few stored chunks and compare. A changed embedding space would degrade retrieval without any error anywhere.
- **Monthly:** run the ChatGPT and Claude account exports and drop them into `downloaded_archives/`. The reminder fires on the 1st; the daily freshness check warns if an export goes stale.
- **Whenever you rename a repo:** add the old name to `historical_aliases` in `config.yaml`, and an alias to its `INDEX.md` row.
- **`lexicon distill`** lists projects that have raw material but no curated notes, ranked by volume decayed against recency. There is deliberately no target for how many to distil.
