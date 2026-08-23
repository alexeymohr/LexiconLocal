"""Scoring behaviour: static boosts and absolute confidence.

Reciprocal rank fusion is ordinal, so on its own it cannot tell "best match in
the corpus" from "least bad of fifty irrelevant chunks". These cover the two
corrections that came out of running the real index.
"""

from __future__ import annotations

from lexiconlocal.search import (
    BOOSTS,
    MIN_CONFIDENCE,
    VEC_CONFIDENT_L2,
    VEC_HOPELESS_L2,
    _confidence,
    _lexical_coverage,
    boost_for,
    is_exact_query,
)


def test_boost_hierarchy_matches_the_design():
    """DESIGN.md 6.2: curated notes > in-repo docs > transcripts > tool events."""
    assert BOOSTS["lexicon"] > BOOSTS["repo-doc:top"]
    assert BOOSTS["repo-doc:top"] > BOOSTS["transcript:prose"]
    assert BOOSTS["transcript:prose"] > BOOSTS["transcript:tool_event"]
    assert BOOSTS["transcript:prose"] > BOOSTS["repo-doc:deep"]
    assert BOOSTS["chatgpt:abandoned"] < BOOSTS["transcript:prose"]


def test_boost_spread_can_outweigh_matching_in_both_legs():
    """A document matching both legs roughly doubles its fused score.

    If the doc-over-transcript boost is smaller than that, the hierarchy
    inverts -- which is exactly what the first real index did.
    """
    assert BOOSTS["repo-doc:top"] / BOOSTS["transcript:prose"] >= 2.0


def test_docs_directory_beats_deep_paths():
    top = boost_for("repo-doc", "prose", "/Users/a/programming/X/docs/mission.md", None)
    deep = boost_for("repo-doc", "prose", "/Users/a/programming/X/src/a/b/c/d/e/notes.md", None)
    assert top > deep


def test_confidence_separates_present_from_absent_topics():
    """Measured on the real corpus: present ~0.76-0.82, absent ~0.96-0.99."""
    present = _confidence(0.78, 0.0)
    absent = _confidence(0.97, 0.0)
    assert present > 0.9
    assert absent <= MIN_CONFIDENCE + 1e-9
    assert present > absent * 2


def test_confidence_never_reaches_zero():
    """A weak hit is still worth showing, just not showing confidently."""
    assert _confidence(1.5, 0.0) == MIN_CONFIDENCE
    assert _confidence(None, 0.0) == MIN_CONFIDENCE


def test_confidence_bounds_are_ordered():
    assert VEC_CONFIDENT_L2 < VEC_HOPELESS_L2


def test_lexical_coverage_rescues_exact_matches_the_vector_leg_misses():
    """An exact identifier can have no semantic neighbours at all."""
    q = '"uv run pytest"'
    text = "[exec] /bin/zsh -lc uv run pytest (exit 2)"
    assert _lexical_coverage(q, text) == 1.0
    # full coverage means full confidence even with no vector hit
    assert _confidence(None, _lexical_coverage(q, text)) == 1.0


def test_lexical_coverage_penalises_single_term_matches():
    q = "photosynthesis chlorophyll absorption spectra"
    text = "the absorption of the audio signal was measured"
    cov = _lexical_coverage(q, text)
    assert cov <= 0.25
    assert _confidence(0.97, cov) < 0.4


def test_exact_query_detection():
    assert is_exact_query('"some exact phrase"')
    assert is_exact_query("src/render/bounce.py")
    assert is_exact_query("AAFParseError.invalid_slot")
    assert not is_exact_query("how do we isolate a track bounce")


# ---------------------------------------------------------------------------
# D4: which distance is the confidence model actually reading?
# ---------------------------------------------------------------------------

def test_knn_distance_is_l2_not_cosine(tmp_path):
    """Pin the metric the confidence thresholds were calibrated against.

    ``chunk_vecs`` declares no distance, so sqlite-vec's KNN returns L2. Every
    comment called it cosine until Phase 5. The thresholds were right all along
    -- they were measured, not derived -- but the wrong name is a live trap: the
    natural way to write a scoped vector query is ``vec_distance_cosine``, which
    returns roughly 0.34 where KNN returns 0.82 for the same pair. Confidence
    would clamp to 1.00 for every result and the "the Lexicon does not cover
    this" signal would vanish without a single test going red.

    If sqlite-vec ever changes its default metric, this test fails instead.
    """
    import math

    from lexiconlocal import db as dbmod

    conn = dbmod.connect(tmp_path / "vec.sqlite")
    dbmod.init_schema(conn, 4)

    # Deliberately not parallel and not unit-length-trivial, so L2 and cosine
    # cannot coincide by accident.
    stored = [0.6, 0.8, 0.0, 0.0]
    query = [0.0, 1.0, 0.0, 0.0]
    conn.execute("INSERT INTO chunk_vecs(chunk_id, embedding) VALUES (1, ?)",
                 (dbmod.serialize_f32(stored),))
    conn.commit()

    q = dbmod.serialize_f32(query)
    knn = conn.execute(
        "SELECT distance FROM chunk_vecs WHERE embedding MATCH ? AND k = 1", (q,)
    ).fetchone()["distance"]
    l2 = conn.execute("SELECT vec_distance_l2(embedding, ?) d FROM chunk_vecs", (q,)
                      ).fetchone()["d"]
    cosine = conn.execute("SELECT vec_distance_cosine(embedding, ?) d FROM chunk_vecs", (q,)
                          ).fetchone()["d"]
    conn.close()

    assert math.isclose(knn, l2, rel_tol=1e-5), "KNN must be L2"
    assert not math.isclose(knn, cosine, rel_tol=1e-3), (
        f"L2 ({l2}) and cosine ({cosine}) must stay distinguishable, or this "
        f"test proves nothing"
    )
    # And the arithmetic that makes them interchangeable for *ranking* only:
    assert math.isclose(l2, math.hypot(0.6 - 0.0, 0.8 - 1.0), rel_tol=1e-5)


# ---------------------------------------------------------------------------
# D3: the corpus-coverage signal agents actually read
# ---------------------------------------------------------------------------

def _r(conf):
    from lexiconlocal.search import Result
    return Result(path="p", project=None, source_type="repo-doc", doc_date=None,
                  title=None, score=0.01, chunk_ord=0, chunk_kind="prose",
                  excerpt="", chunk_id=1, confidence=conf)


def test_median_confidence_survives_a_single_self_quoting_document():
    """The reason the signal is a median and not a maximum.

    This corpus indexes its own repo, so a doc that quotes a query verbatim
    scores 1.00 on lexical coverage alone. Two of the four original absent
    probes were compromised exactly that way -- the top hit read 1.00 while
    every other result sat near the floor.
    """
    from lexiconlocal.search import CONFIDENCE_ABSENT_MEDIAN, median_confidence

    spiked = [_r(1.00), _r(0.32), _r(0.32), _r(0.36), _r(0.32)]
    assert max(r.confidence for r in spiked) == 1.00
    assert median_confidence(spiked) < CONFIDENCE_ABSENT_MEDIAN

    covered = [_r(0.97), _r(1.00), _r(1.00), _r(0.99), _r(0.98)]
    assert median_confidence(covered) >= CONFIDENCE_ABSENT_MEDIAN


def test_median_confidence_handles_empty_and_even_counts():
    from lexiconlocal.search import median_confidence

    assert median_confidence([]) == 0.0
    assert abs(median_confidence([_r(0.4), _r(0.8)]) - 0.6) < 1e-9


def test_confidence_bands_are_ordered_and_within_the_scale():
    from lexiconlocal.search import (
        CONFIDENCE_ABSENT_MEDIAN,
        CONFIDENCE_COVERED,
        MIN_CONFIDENCE,
    )

    assert MIN_CONFIDENCE < CONFIDENCE_ABSENT_MEDIAN < CONFIDENCE_COVERED <= 1.0
