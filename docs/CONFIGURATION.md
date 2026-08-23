# Configuration

One file: `<lexicon_root>/config.yaml`. It is the only source of truth for what gets indexed. `lexicon init` writes a commented template; this page documents every key.

The config path defaults to `~/Lexicon/config.yaml`. Every command accepts `--config PATH` to use another, and the golden-query suite honours `LEXICON_CONFIG`.

## `schema_version`

```yaml
schema_version: 1
```

The config format version. Currently always `1`.

## `lexicon_root`

```yaml
lexicon_root: ~/Lexicon
```

The data repo. Everything else — `projects/`, `archive/`, `index/`, `private/`, `golden/` — lives under it. `~` and environment variables are expanded.

## `source_roots`

```yaml
source_roots:
  - path: ~/code
    type: repos
  - path: ~/Documents/AgentProjects
    type: repos
```

Directories that **contain** your repositories. For each root:

- Every top-level entry under it becomes a **project**, named after the directory. A session whose working directory is anywhere inside `<root>/<name>/` is attributed to project `<name>`; a Markdown file anywhere inside it is indexed as a `repo-doc` of that project.
- A file sitting directly at the root (not inside a project directory) belongs to the `_loose` pseudo-project.
- Each root gets a short **label** derived from its directory name, used to disambiguate two projects with the same name under different roots. A label that collides, or that is literally `projects`, is prefixed with the parent directory's name.

Two rules that are easy to get wrong:

1. **A root is a container of repos, never a repo itself.** Point it at a single repo and its *subdirectories* become the projects.
2. **Never point a root at a directory that also holds personal files.** Only `.md` and `.markdown` are read, but the whole tree is walked. If your code lives next to your tax records, move the code.

`type` is currently always `repos`.

## `exclude_dirs`

```yaml
exclude_dirs:
  - node_modules
  - .git
  - ".venv*"
  - "user-data-*"
```

Directory **names** to prune at any depth, matched with shell-style globs against the name alone (not the path). A pruned directory is never entered, so this is also how you keep the walker out of symlink loops, build trees and vendored dependencies.

Things worth excluding that are not obvious until they bite: build output that contains symlinks back into the tree; vendored third-party source (it would surface *their* docs in *your* search); generated per-run report directories; browser-profile directories inside a repo; and the **dotted sibling** of anything you already exclude — `scratch` does not match `.scratch`.

## `exclude_files`

```yaml
exclude_files:
  - "*.env"
  - ".env*"
  - "*.pem"
  - "*.key"
```

File-name globs never read, let alone indexed. The template covers environment files and private-key formats. Redaction on ingest is the second line of defence, not a substitute for this one.

## `never_index`

```yaml
never_index:
  - ~/Lexicon/private
```

Paths never indexed under any circumstances, checked by containment (anything beneath the path is excluded). `private/` is the convention: move anything sensitive there at any time and reindex. Containment rather than name-matching matters on macOS, where `/tmp` and `/var` are symlinks into `/private/...` and a name match on `private` would refuse the whole filesystem.

## `historical_aliases`

```yaml
historical_aliases:
  old-spike-name: CurrentProject
  another-old-name: CurrentProject
```

Historical project names mapped to the project they belong to. A repo that was renamed leaves old transcripts recorded under the old name; listing it here means a filter on the new name still finds them, and the distillation backlog does not list the old name as a separate undistilled project. Keys are matched case-insensitively. Default: empty.

`INDEX.md`'s alias columns do the same job and are folded in on top; this key exists so a rename is covered even before anyone writes the `INDEX.md` row.

## What is *not* in this file

- **Ranking and confidence constants** live in `src/lexiconlocal/search.py`, deliberately: they were measured against a corpus and changing one is a calibration task, not a configuration choice. `OPERATIONS.md` lists them.
- **Golden-query assertions** live in `<lexicon_root>/golden/checks.yaml`. See `golden/checks.example.yaml` in this repo.
- **The leak guard's tuning** lives in `<lexicon_root>/private/leak-allow.txt` and `leak-extra.txt`.
- **The embedding model and host** default to `nomic-embed-text` on `http://localhost:11434` and are overridable per command with `--model` and `--host`. The host must be loopback; the web UI refuses to bind anywhere else.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `LEXICON_ROOT` | scripts, leak guard | Override the data repo location |
| `LEXICON_REPO` | shell scripts | Override the code repo location (default: where the script lives) |
| `LEXICON_BIN` | the `SessionEnd` hook | Override the `lexicon` binary path |
| `LEXICON_CONFIG` | golden-query suite | Run against a different config |
| `LEXICON_GOLDEN_SEED` | golden-query suite | Reproduce a past run's generated absent probes |
| `LEXICON_BTM` | launch-agent checks (macOS) | `force` / `skip` the Background Task Management read, which raises a password dialog |
| `LEAK_GUARD` | leak guard | `off` overrides a refusal, with the hits printed anyway |
| `DRY_RUN` | nightly job | `1` logs what it would do and changes nothing |
