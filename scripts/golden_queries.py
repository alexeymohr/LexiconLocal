#!/usr/bin/env python
"""Asserted golden-query regression guard, run against the LIVE index.

Both ranking defects found in Phase 2 -- static boosts too weak to express the
design's hierarchy, and RRF scoring an absent topic above a present one --
passed every unit test in this repo. They were only visible against 5,685 real
documents. This file is the guard for that class of defect, which is why it
runs against the real index and not a fixture.

Assertions are deliberately expressed as **ratios, shares and thresholds**,
never as absolute ranks or document counts. The corpus grows every day by
design; a check that breaks because the index got bigger would be turned off
within a week, and then it would be guarding nothing.

Exit 0 = search quality intact. Exit 1 = regression. Exit 2 = cannot run.

Run it after every PIPELINE_VERSION bump (see CLAUDE.md), and weekly via
com.lexiconlocal.golden.plist.
"""

from __future__ import annotations

import os
import random
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

import yaml

from lexiconlocal import db as dbmod
from lexiconlocal.config import load_config
from lexiconlocal.embed import EmbedError, Embedder
from lexiconlocal.search import (
    BOOSTS,
    CONFIDENCE_ABSENT_MEDIAN,
    Searcher,
    _lexical_coverage,
    median_confidence,
)

#: Domain vocabularies for runtime-generated absent-topic probes.
#:
#: A fixed probe LIST was the defect, not a missing one. This corpus indexes
#: its own repo and every session transcript, so a literal phrase quoted
#: anywhere -- a doc, a decision record, a completion note -- leaks into the
#: corpus within the hour and stops being absent. Two of the four original
#: probes were caught drifting toward exactly that (lexical coverage 0.60 and
#: climbing toward the 0.80 compromise line). A better fixed list has the same
#: shelf life as the one it replaces.
#:
#: The fix is generation. Each run draws several of these categories and, from
#: each, four words in no fixed order -- a four-word combination this specific
#: is not the kind of thing that turns up by accident in a corpus of software
#: and audio-engineering work, even though an individual word
#: (harpsichord, trilobite) might. A category's word list is not a probe and is
#: safe to keep in this file; only a *generated phrase* is a probe, and
#: CLAUDE.md says never to quote one.
_PROBE_VOCAB: dict[str, list[str]] = {
    "hydrothermal_vents": ["hydrothermal", "tubeworm", "chemosynthesis", "symbiosis",
                           "vent", "siphonophore", "bathypelagic", "hadal",
                           "osedax", "vestimentiferan", "methanotroph", "sulfide",
                           "riftia", "pyrolobus", "thermophile", "anhydrite",
                           "serpentinite", "chemolithotroph"],
    "early_music": ["harpsichord", "meantone", "temperament", "courante",
                    "sackbut", "gambist", "virginal", "mensural",
                    "hemiola", "plainchant", "tablature", "consort",
                    "rebec", "crumhorn", "cornetto", "solmization",
                    "neume", "organum"],
    "ancient_scripts": ["cuneiform", "sumerian", "akkadian", "ziggurat",
                        "ostracon", "hieratic", "demotic", "ledger",
                        "ration", "tablet", "scribe", "barley",
                        "epigraphy", "papyrus", "stele", "cartouche",
                        "hieroglyph", "scribal"],
    "paleontology": ["trilobite", "cambrian", "exoskeleton", "moulting",
                     "brachiopod", "ammonite", "crinoid", "permian",
                     "devonian", "carboniferous", "ediacaran", "stromatolite",
                     "graptolite", "belemnite", "foraminifera", "echinoderm",
                     "ostracoderm", "conodont"],
    "textile_crafts": ["tatting", "naalbinding", "warp", "weft",
                       "shuttle", "distaff", "fulling", "mordant",
                       "selvage", "heddle", "tabby", "twill",
                       "sprang", "backstrap", "retting", "carding",
                       "fustian", "damask"],
    "volcanology": ["laccolith", "phreatomagmatic", "tephra", "lahar",
                    "fumarole", "pahoehoe", "xenolith", "caldera",
                    "scoria", "rhyolite", "andesite", "obsidian",
                    "stratovolcano", "phreatic", "breccia", "ignimbrite",
                    "maar", "tuff"],
    "apiculture": ["apiary", "propolis", "waggle", "brood",
                   "festooning", "queenright", "nectary", "supersedure",
                   "abscond", "pheromone", "comb", "skep",
                   "apiarist", "mellifera", "langstroth", "uncapping",
                   "extractor", "excluder"],
    "heraldry": ["blazon", "tincture", "chevron", "rampant",
                "escutcheon", "gyronny", "tressure", "bordure",
                "fess", "pale", "saltire", "vairy",
                "cadency", "marshalling", "crest", "mantling",
                "torse", "lozenge"],
}

ABSENT_PROBE_COUNT = 4


def _golden_seed(offset_days: int = 0) -> str:
    """Today's date, or an explicit override, as the probe-generation seed.

    Same-day reproducibility without storing anything: a failure this
    afternoon draws the same probes as one investigated this evening.
    ``LEXICON_GOLDEN_SEED`` overrides it, to reproduce an older run's probes
    when investigating a stale failure log.
    """
    override = os.environ.get("LEXICON_GOLDEN_SEED")
    if override:
        return override
    return (date.today() - timedelta(days=offset_days)).isoformat()


def generate_absent_probes(
    seed: str | None = None, count: int = ABSENT_PROBE_COUNT
) -> list[str]:
    """Fresh absent-topic probes: deterministic for one seed, disposable across seeds.

    If a phrase generated today does leak into some future document, it costs
    nothing -- tomorrow's seed draws a different combination, and today's leak
    is just noise to it. That is the property a fixed list never had.
    """
    rng = random.Random(seed if seed is not None else _golden_seed())
    categories = rng.sample(sorted(_PROBE_VOCAB), k=min(count, len(_PROBE_VOCAB)))
    probes = []
    for cat in categories:
        words = rng.sample(_PROBE_VOCAB[cat], k=4)
        rng.shuffle(words)
        probes.append(" ".join(words))
    return probes

#: A probe whose terms are this well covered by a real chunk has leaked into the
#: corpus wholesale and can no longer stand in for an absent topic at all.
PROBE_COMPROMISED_COVERAGE = 0.8

#: Below PROBE_COMPROMISED_COVERAGE but at or above this, a probe's score is
#: legitimately lifted by a partial term match rather than by an engine
#: defect, so the score-ratio assertion is skipped for it (the confidence
#: assertion still runs -- it is checked against a present-topic reference
#: near 1.0, a bar partial overlap does not come close to).
#:
#: Found live, not theorised: two adjacent daily seeds redrew the same
#: category (textile_crafts) and shared two of four words ("tabby",
#: "mordant") with a probe deliberately leaked into a scratch doc for the
#: isolation test below. Coverage landed at 0.50 -- nowhere near compromised,
#: but enough to lift the score past the 60%-of-present bar and fail a check
#: that had nothing to do with retrieval quality. A 12-word pool made this
#: likely; the pools were widened to 18 afterward, and this tier is the
#: belt-and-braces for whatever headroom that does not cover.
PROBE_ELEVATED_COVERAGE = 0.4

#: Absent topics are judged **relative to a present-topic reference**, never
#: against an absolute number.
#:
#: The reason is structural, not fussiness. Claude Code sessions are archived
#: and indexed by design, so any phrase typed while maintaining this guard --
#: including choosing the probes -- is in the corpus within the hour. A probe
#: measured at confidence 0.32 when it was picked read 0.66 an hour later
#: purely because the session that picked it had been ingested. An absolute
#: threshold cannot survive that; the separation between answerable and
#: unanswerable can, and it is what the Phase 2 defect actually violated
#: (an absent topic scoring *above* a present one).
ABSENT_SCORE_MAX_FRACTION_OF_PRESENT = 0.6
MIN_USABLE_PROBES = 2


def _probe_coverage(searcher: Searcher, query: str, result) -> float:
    """Lexical coverage of *query* against a result's full chunk text."""
    row = searcher.conn.execute(
        "SELECT text FROM chunks WHERE id=?", (result.chunk_id,)
    ).fetchone()
    return _lexical_coverage(query, row["text"]) if row else 0.0

FAILURES: list[str] = []
CHECKS = 0
SKIPPED = 0

#: The corpus-specific half of this suite lives in YAML, not here. The harness
#: knows HOW to assert; the checks file says WHAT the operator's corpus
#: answers. The example ships with the repo; the real one lives in the
#: operator's Lexicon root, which is private.
CHECKS_REL = Path("golden/checks.yaml")
EXAMPLE_CHECKS = Path(__file__).resolve().parent.parent / "golden" / "checks.example.yaml"


def load_checks(lexicon_root: Path) -> tuple[dict, bool]:
    """Return (checks, is_example). Falls back to the shipped example, loudly."""
    own = lexicon_root / CHECKS_REL
    if own.is_file():
        return yaml.safe_load(own.read_text(encoding="utf-8")) or {}, False
    return yaml.safe_load(EXAMPLE_CHECKS.read_text(encoding="utf-8")) or {}, True


def skip(name: str, why: str) -> None:
    """A check the corpus cannot exercise. Not a pass, not a fail -- said out loud."""
    global SKIPPED
    SKIPPED += 1
    print(f"  [SKIP] {name} — {why}")


def check(name: str, condition: bool, detail: str = "", *, detail_on_pass: bool = True) -> bool:
    """Record one assertion.

    ``detail_on_pass=False`` is for checks whose detail is an explanation of
    what failure would mean -- printing that next to a PASS reads as if
    something went wrong.
    """
    global CHECKS
    CHECKS += 1
    status = "PASS" if condition else "FAIL"
    show = detail if (detail and (detail_on_pass or not condition)) else ""
    print(f"  [{status}] {name}" + (f" — {show}" if show else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)
    return condition


def heading(text: str) -> None:
    print()
    print(text)
    print("-" * min(len(text), 78))


def brief(results, n=5) -> None:
    for i, r in enumerate(results[:n], 1):
        path = r.path.replace(str(Path.home()), "~")
        print(f"      {i}. [{r.score:.4f}] conf={r.confidence:.2f} {r.source_type:<14} "
              f"{(r.title or '')[:40]:<40} {path[-46:]}")


def main() -> int:
    # LEXICON_CONFIG lets the suite run against a second root -- a fixture, a
    # fresh install -- without touching the operator's real one.
    try:
        cfg = load_config(Path(os.environ["LEXICON_CONFIG"]) if os.environ.get("LEXICON_CONFIG") else None)
    except Exception as e:  # noqa: BLE001
        print(f"cannot load config: {e}", file=sys.stderr)
        return 2
    try:
        emb = Embedder()
        emb.preflight()
    except EmbedError as e:
        # Without the vector leg this suite would be testing something else
        # entirely, so refuse rather than report a misleading pass.
        print(f"cannot run: embeddings unavailable — {e}", file=sys.stderr)
        return 2
    try:
        s = Searcher(cfg, emb)
    except Exception as e:  # noqa: BLE001
        print(f"cannot open the index: {e}", file=sys.stderr)
        return 2

    checks, is_example = load_checks(cfg.lexicon_root)
    print("golden queries — asserted against the live index")
    if is_example:
        print(f"  NOTE: no {cfg.lexicon_root / CHECKS_REL}; using the shipped example checks.")
        print( "        Most corpus-specific checks will SKIP. Copy golden/checks.example.yaml")
        print( "        into your Lexicon root and fill it in with queries your notes answer.")

    # --- static configuration invariants ------------------------------------
    heading("0. Ranking hierarchy (DESIGN.md 6.2)")
    check("curated notes outrank in-repo docs",
          BOOSTS["lexicon"] > BOOSTS["repo-doc:top"])
    check("in-repo docs outrank transcripts",
          BOOSTS["repo-doc:top"] > BOOSTS["transcript:prose"])
    check("transcript prose outranks tool events",
          BOOSTS["transcript:prose"] > BOOSTS["transcript:tool_event"])
    check("abandoned branches rank below real threads",
          BOOSTS["chatgpt:abandoned"] < BOOSTS["transcript:prose"])
    ratio = BOOSTS["repo-doc:top"] / BOOSTS["transcript:prose"]
    check(f"doc-over-transcript ratio >= 2.0 (is {ratio:.2f})", ratio >= 2.0,
          "below 2.0 a doc loses to a transcript that merely matched in both "
          "legs — the Phase 2 defect", detail_on_pass=False)

    # --- GQ1 ----------------------------------------------------------------
    ref = checks.get("present_reference") or {}
    ref_query, ref_projects = ref.get("query", ""), {p.lower() for p in ref.get("projects", [])}
    heading(f"1. {ref_query!r} — curated/doc material must lead")
    r1 = s.search(ref_query, limit=5) if ref_query else []
    brief(r1)
    if not r1:
        skip("present reference returns results", "the corpus does not answer this query")
    else:
        docish = sum(1 for r in r1 if r.source_type in ("repo-doc", "lexicon", "codex-memory"))
        check("majority of top-5 are docs, not transcripts", docish >= 3,
              f"{docish}/5 doc-like")
        if ref_projects:
            inside = sum(1 for r in r1 if (r.project or "").lower() in ref_projects)
            check("top-5 stay within the expected projects", inside >= 3,
                  f"{inside}/5 in {sorted(ref_projects)}")
        check("lead result is confident", r1[0].confidence >= 0.8,
              f"confidence={r1[0].confidence:.2f}")

    # --- GQ2a ---------------------------------------------------------------
    sym = checks.get("exact_symbol") or {}
    heading(f"2a. Exact symbol {sym.get('query', '')} — must hit lexically")
    r2a = s.search(sym["query"], limit=5) if sym.get("query") else []
    brief(r2a, 3)
    if not r2a:
        skip("exact symbol returns results", "the symbol is not in the corpus")
    else:
        check("top hit matched via FTS", "fts" in r2a[0].matched_by,
              f"matched_by={r2a[0].matched_by}")
        want = sym.get("expect_source_type")
        if want:
            check(f"top hit is a {want}", r2a[0].source_type == want,
                  f"got {r2a[0].source_type}")
        check("top hit is confident", r2a[0].confidence >= 0.9,
              f"confidence={r2a[0].confidence:.2f}")

    # --- GQ2b ---------------------------------------------------------------
    tev = (checks.get("tool_event_string") or {}).get("query", "")
    heading("2b. String living only in a tool-event header")
    r2b = s.search(tev, limit=5, kind="tool_event") if tev else []
    brief(r2b, 3)
    if not r2b:
        skip("tool-event string returns results", "no tool-event chunk carries it")
    else:
        check("all results are tool_event chunks",
              all(r.chunk_kind == "tool_event" for r in r2b))
        check("found lexically, not by embedding",
              all("fts" in r.matched_by for r in r2b),
              "exact identifiers must never depend on the vector leg")

    # --- GQ3 ----------------------------------------------------------------
    xp = checks.get("cross_project") or {}
    heading("3. Conceptual cross-project retrieval")
    r3 = s.search(xp["query"], limit=6) if xp.get("query") else []
    brief(r3)
    if not r3:
        skip("cross-project query returns results", "the corpus does not answer it")
    else:
        projects = {(r.project or "").lower() for r in r3}
        want = (xp.get("expect_project") or "").lower()
        if want:
            check(f"{want} material surfaces", want in projects,
                  f"projects={sorted(p for p in projects if p)}")
        # Counted by ORIGIN, not by project label. Web chats carry no project:
        # attribution is derived from a transcript's `cwd`, and a web
        # conversation has none. When an account export lands, genuinely
        # relevant threads take top slots and a label-based count reads as
        # "1 distinct project" while siblings sit just below. Retrieval did not
        # narrow; the label went missing. An unattributed archive is still a
        # boundary crossed, so it counts as one origin. A result set that
        # tunnels into a single project still collapses to one origin and still
        # fails, which is the point.
        #
        # >=2, not >=3: which siblings appear shifts as the corpus grows.
        origins = {
            f"project:{r.project.lower()}" if r.project else f"archive:{r.path.split('#')[0]}"
            for r in r3
        }
        check("retrieval crosses project/source boundaries", len(origins) >= 2,
              f"{len(origins)} distinct origins: {sorted(origins)[:4]}")
        check("lead result is confident", r3[0].confidence >= 0.8,
              f"confidence={r3[0].confidence:.2f}")

    # --- GQ4 ----------------------------------------------------------------
    heading("4. Absent topics must not score like present ones")
    absent_seed = _golden_seed()
    absent_probes = generate_absent_probes(absent_seed)
    print(f"      seed: {absent_seed}  ({len(absent_probes)} probes generated fresh)")
    present = s.search(ref_query, limit=1) if ref_query else []
    present_score = present[0].score if present else 0.0
    present_conf = present[0].confidence if present else 0.0
    if not present:
        # Absent-vs-present is a RELATIVE test. Without a present reference the
        # ratio below is against zero and every probe "fails". Say so instead.
        skip("absent-vs-present separation", "no present reference answered; fill in "
             "present_reference in golden/checks.yaml")

    usable = 0
    for q in (absent_probes if present else []):
        res = s.search(q, limit=3)
        if not res:
            usable += 1
            check(f"absent probe returns nothing: {q[:34]}", True, "no results")
            continue
        coverage = max(_probe_coverage(s, q, r) for r in res)
        top = res[0]
        if coverage >= PROBE_COMPROMISED_COVERAGE:
            # Not a search-quality failure: this specific combination now
            # genuinely exists in the corpus (a freak collision, or a phrase
            # quoted somewhere against the CLAUDE.md rule) and can no longer
            # stand in for an absent topic today. Tomorrow's seed draws a
            # different combination regardless.
            print(f"      COMPROMISED PROBE (coverage {coverage:.2f}) — {q[:46]}")
            continue
        usable += 1
        print(f"      absent  [{top.score:.4f}] conf={top.confidence:.2f} "
              f"cov={coverage:.2f}  {q[:46]}")
        if coverage >= PROBE_ELEVATED_COVERAGE:
            # A partial term match (half or more of the probe's words appear
            # in this chunk) legitimately lifts a fused score -- that is the
            # engine doing its job, not a ranking defect -- so the score-ratio
            # assertion below would be testing the vocabulary, not retrieval.
            # The confidence assertion still runs: it is judged against a
            # present-topic reference near 1.0, which partial overlap alone
            # does not approach.
            print(f"        elevated coverage ({coverage:.2f}) from partial term "
                  f"overlap -- score-ratio assertion skipped for this probe")
        else:
            limit = present_score * ABSENT_SCORE_MAX_FRACTION_OF_PRESENT
            check(f"scores well below the present reference: {q[:30]}",
                  top.score < limit,
                  f"absent={top.score:.4f} must be < {limit:.4f} "
                  f"({ABSENT_SCORE_MAX_FRACTION_OF_PRESENT:.0%} of present {present_score:.4f})")
        check(f"less confident than the present reference: {q[:28]}",
              top.confidence < present_conf,
              f"absent conf={top.confidence:.2f} vs present {present_conf:.2f}")

    if present:
        print(f"      present [{present_score:.4f}] conf={present_conf:.2f}  (reference)")
        check("present-topic reference is itself confident", present_conf >= 0.8,
              f"confidence={present_conf:.2f}")
        check(f"at least {MIN_USABLE_PROBES} absent probes were usable ({usable}/{len(absent_probes)})",
              usable >= MIN_USABLE_PROBES,
              "the absent-topic check has gone vacuous -- belt-and-braces against a "
              "freak collision in generate_absent_probes(); investigate if this fires",
              detail_on_pass=False)

    # --- GQ4b ---------------------------------------------------------------
    # The MCP banner (Phase 5 D3) fires on an absolute number, and this corpus
    # grows every day -- so the number has to be re-measured, not trusted. This
    # check is the calibration, not a formality: if the two bands ever meet,
    # the banner is either crying wolf or has gone silent.
    heading("4b. The absent-corpus banner is still calibrated (Phase 5 D3)")
    absent_medians, present_medians = [], []
    for q in absent_probes:  # same generated set as GQ4 -- one seed, one run
        res = s.search(q, limit=5)
        if not res:
            absent_medians.append(0.0)
            continue
        if max(_probe_coverage(s, q, r) for r in res) >= PROBE_ELEVATED_COVERAGE:
            # Same tier as GQ4: partial term overlap is not a clean absence
            # signal either, and this calibration is only meaningful over
            # probes the corpus genuinely does not cover.
            continue
        absent_medians.append(median_confidence(res))
    for q in checks.get("present_queries") or ([ref_query] if ref_query else []):
        res = s.search(q, limit=5)
        if res:
            present_medians.append(median_confidence(res))

    if absent_medians and present_medians:
        worst_absent, worst_present = max(absent_medians), min(present_medians)
        print(f"      absent medians up to {worst_absent:.2f} | "
              f"present medians down to {worst_present:.2f} | "
              f"banner fires below {CONFIDENCE_ABSENT_MEDIAN:.2f}")
        check("absent topics stay below the banner threshold",
              worst_absent < CONFIDENCE_ABSENT_MEDIAN,
              f"worst absent median {worst_absent:.2f} >= "
              f"{CONFIDENCE_ABSENT_MEDIAN:.2f} — the banner has gone silent",
              detail_on_pass=False)
        check("present topics stay above it",
              worst_present >= CONFIDENCE_ABSENT_MEDIAN,
              f"worst present median {worst_present:.2f} < "
              f"{CONFIDENCE_ABSENT_MEDIAN:.2f} — the banner is crying wolf",
              detail_on_pass=False)
    else:
        # On a fresh or tiny corpus there may be no present query that answers
        # yet. That is not a regression in the banner; it is an empty corpus.
        skip("banner calibration", "no present query answered, or no usable absent probe; "
             "fill in present_queries in golden/checks.yaml")

    # --- GQ5 ----------------------------------------------------------------
    pf = checks.get("project_filter") or {}
    heading("5. Project filter and alias resolution")
    cur, alias = pf.get("current", ""), pf.get("alias", "")
    filtered = s.search(pf.get("query", ""), limit=8, project=cur) if cur and pf.get("query") else []
    if not filtered:
        skip("project filter returns only that project", "no scoped results; set project_filter")
    else:
        projects = {(r.project or "") for r in filtered}
        check("project filter returns only that project",
              projects <= set(s.projects.resolve(cur)), f"got {sorted(projects)}")
    if cur and alias:
        aliased = s.search(pf.get("alias_query") or pf.get("query", ""), limit=8, project=alias)
        alias_projects = {(r.project or "") for r in aliased}
        if not aliased:
            skip(f"historical name {alias} resolves to {cur}", "no results through the alias")
        else:
            check(f"historical name {alias} resolves to {cur}",
                  alias_projects <= set(s.projects.resolve(cur)),
                  f"got {sorted(alias_projects)}")
        resolved = {p.lower() for p in s.projects.resolve(alias)}
        check(f"alias map links {alias} and {cur}",
              {alias.lower(), cur.lower()} <= resolved, f"resolved={sorted(resolved)}")
    else:
        skip("alias resolution", "project_filter.current/alias not set")

    # The checks above can pass on a post-filter, because the project-filter
    # query is one its project already ranks for globally. The scoped-search
    # defect only shows on a *small* project asked a question it does not
    # dominate -- on the calibration corpus two such projects returned [0,0,0]
    # while holding dozens of documents and recorded decisions each.
    heading("5b. A small project answers questions about itself (Phase 5 D1)")
    SMALL_PROJECT_QUESTIONS = [
        "what decisions and constraints govern this project",
        "current state and open questions",
        "what failed and why",
    ]
    small = list(checks.get("small_projects") or [])
    if not small:
        skip("small projects answer self-questions", "small_projects not set")
    for probe in small:
        counts = [len(s.search(q, limit=10, project=probe))
                  for q in SMALL_PROJECT_QUESTIONS]
        check(f"{probe} answers all {len(SMALL_PROJECT_QUESTIONS)} self-questions",
              all(c > 0 for c in counts), f"result counts {counts}")
        leaked = {(r.project or "") for q in SMALL_PROJECT_QUESTIONS
                  for r in s.search(q, limit=10, project=probe)}
        check(f"{probe} scoped results stay inside the project",
              leaked <= set(s.projects.resolve(probe)), f"got {sorted(leaked)}")

    # Latency is part of the contract: the scoped path does more work than the
    # post-filter it replaces, and the Phase 4 bar is 500 ms.
    slowest, slowest_case = 0.0, ""
    latency_cases = [("broad", {"source_type": "lexicon"})]
    if small:
        latency_cases.append((small[0], {"project": small[0]}))
    if checks.get("large_project"):
        latency_cases.append((checks["large_project"], {"project": checks["large_project"]}))
    for probe, kwargs in latency_cases:
        t0 = time.perf_counter()
        s.search("what decisions and constraints govern this", limit=10, **kwargs)
        ms = (time.perf_counter() - t0) * 1000
        if ms > slowest:
            slowest, slowest_case = ms, probe
    check("filtered search stays under the 500 ms bar",
          slowest < 500, f"slowest {slowest:.0f} ms ({slowest_case})")

    # --- GQ6 ----------------------------------------------------------------
    heading("6. ChatGPT export is present, threaded, and retrievable")
    # This source is worth its own check because its whole-corpus failure mode
    # is silent: the pre-dump parser globbed `conversations.json`, the real
    # export ships `conversations-NNN.json`, and every other check in this file
    # passed while 2,089 conversations went unindexed.
    # Not every operator has dropped a ChatGPT export, and a fresh install has
    # none. Absence is the normal state, not a regression -- `lexicon report`
    # is what says whether a configured source is untested.
    chatgpt_dir = cfg.archive_dir / "chatgpt"
    if not chatgpt_dir.is_dir() or not any(chatgpt_dir.iterdir()):
        skip("ChatGPT export checks", "no export under archive/chatgpt/")
    else:
        conn = s.conn
        rows = conn.execute(
            """SELECT json_extract(extra_json, '$.branch') AS branch, COUNT(*) AS n
               FROM documents
               WHERE json_extract(extra_json, '$.tool') = 'chatgpt'
               GROUP BY branch"""
        ).fetchall()
        counts = {r["branch"]: r["n"] for r in rows}
        canonical_n = counts.get("canonical", 0)
        abandoned_n = counts.get("abandoned", 0)
        check("ChatGPT conversations are in the index", canonical_n > 0,
              f"{canonical_n} canonical conversations")
        # Reconstructing children from parent edges is what keeps this ratio sane.
        # Treating every node as a leaf -- which the export's missing `children`
        # key invites -- floods the index with near-duplicate abandoned branches.
        if canonical_n:
            share = abandoned_n / canonical_n
            check(f"abandoned branches stay a minority of threads ({share:.2f} per thread)",
                  share < 0.5,
                  "branch detection has regressed to treating every node as a leaf")

        reasoning_in_prose = conn.execute(
            """SELECT COUNT(*) AS n FROM chunks c
               JOIN occurrences o ON o.chunk_hash = c.content_hash
               JOIN documents d ON d.id = o.doc_id
               WHERE json_extract(d.extra_json, '$.tool') = 'chatgpt'
                 AND c.kind = 'prose' AND c.text LIKE '%[thinking]%'"""
        ).fetchone()["n"]
        check("model reasoning stays off the prose/embedding tier",
              reasoning_in_prose == 0,
              f"{reasoning_in_prose} prose chunks carry reasoning text (D-2026-08-19-01)")

        # End-to-end retrieval, using a title drawn from the index itself so the
        # check does not rot when a particular conversation is superseded.
        row = conn.execute(
            """SELECT title, json_extract(extra_json, '$.conversation_id') AS cid
               FROM documents
               WHERE json_extract(extra_json, '$.tool') = 'chatgpt'
                 AND json_extract(extra_json, '$.branch') = 'canonical'
                 AND title NOT LIKE 'ChatGPT conversation %'
                 AND LENGTH(title) BETWEEN 20 AND 60
               ORDER BY LENGTH(title) DESC, title LIMIT 1"""
        ).fetchone()
        if row:
            r6 = s.search(row["title"], limit=8)
            brief(r6, 3)
            check(f"a ChatGPT thread is retrievable by its own title: {row['title'][:40]!r}",
                  any(f"conversation={row['cid']}" in r.path for r in r6),
                  "the ChatGPT leg is indexed but not reachable through search")

        # --- summary ------------------------------------------------------------
    s.close()
    emb.close()
    print()
    print("=" * 78)
    if FAILURES:
        print(f"GOLDEN QUERIES FAILED — {len(FAILURES)} of {CHECKS} checks regressed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"GOLDEN QUERIES PASSED — {CHECKS}/{CHECKS} checks"
          + (f"  ({SKIPPED} skipped: the corpus could not exercise them)" if SKIPPED else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
