"""Lookup-stage tests for gather_candidates: LLM-alternatives fallback.

Pins the fallback contract:
  * alternatives are queried when the primary (nb/en) queries yield no
    CONVINCING candidate — none at or above alternatives_trigger_score — and
    they only widen recall, never re-rank (a strong primary is undisturbed);
  * a single weak fuzzy hit must NOT suppress the fallback (regression: 'Sari'
    -> 'Sari (Samanid pottery style)' @33 blocked the 'sari' query that finds
    the correct 'saris (garments)');
  * trigger 0 = strict mode (fallback only when nothing >= min_score);
  * the number of alternative queries is capped (max_alternative_queries,
    0 = disabled);
  * when the same concept is found by both an en and an nb query, the nb
    occurrence wins the dedup, so trusted-query provenance is never masked.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.cli import gather_candidates


class FakeAdapter:
    """Returns canned candidates per query label; records every search call."""

    def __init__(self, by_label):
        self.by_label = by_label
        self.calls: list[tuple[str, str]] = []

    def search(self, label, lang, limit=5):
        self.calls.append((label, lang))
        out = []
        for c in self.by_label.get(label, []):
            c.query_term, c.query_lang = label, lang
            out.append(c)
        return out


LANGS = make_profile().languages


def test_alternatives_not_queried_when_primary_convincing():
    adapter = FakeAdapter({"ting": [make_candidate(concept_id="A", score=90)]})
    term = make_term(nb="ting", en="thing", target_alternatives=["object", "item"])
    cands, used = gather_candidates(term, adapter, LANGS, result_limit=5)
    assert not used
    assert [c.concept_id for c in cands] == ["A"]
    assert ("object", "en") not in adapter.calls


def test_weak_fuzzy_hit_does_not_suppress_fallback():
    # The 'Sari' regression: primaries return only a junk fuzzy hit (33);
    # the alternative query must still run, and it recovers the right concept
    # without displacing anything (ranking stays score-based downstream).
    adapter = FakeAdapter({
        "Sari": [make_candidate(concept_id="POTTERY", score=33)],
        "sari": [make_candidate(concept_id="GARMENT", score=100,
                                matched_lang="en", is_exact=True)],
    })
    term = make_term(nb="Sari", en="saris", target_source="llm",
                     target_alternatives=["sari"])
    cands, used = gather_candidates(term, adapter, LANGS, result_limit=5)
    assert used
    assert {c.concept_id for c in cands} == {"POTTERY", "GARMENT"}


def test_trigger_zero_restores_strict_behaviour():
    adapter = FakeAdapter({"ting": [make_candidate(concept_id="A", score=33)]})
    term = make_term(nb="ting", target_source="llm", target_alternatives=["alt"])
    _, used = gather_candidates(term, adapter, LANGS, result_limit=5,
                                alternatives_trigger_score=0)
    assert not used  # one hit >= min_score(0) suppresses fallback in strict mode


def test_alternatives_queried_when_primary_empty():
    adapter = FakeAdapter({"gilt": [make_candidate(concept_id="B", score=30)]})
    term = make_term(nb="forgylling", en="gold-coating",
                     target_source="llm", target_alternatives=["gilt", "gilding"])
    cands, used = gather_candidates(term, adapter, LANGS, result_limit=5)
    assert used
    assert [c.concept_id for c in cands] == ["B"]
    # primary queries ran first, then the alternatives (target language)
    assert adapter.calls[:2] == [("forgylling", "nb"), ("gold-coating", "en")]
    assert ("gilt", "en") in adapter.calls and ("gilding", "en") in adapter.calls


def test_alternatives_triggered_when_primary_all_below_min_score():
    adapter = FakeAdapter({
        "ting": [make_candidate(concept_id="LOW", score=3)],
        "alt": [make_candidate(concept_id="C", score=30)],
    })
    term = make_term(nb="ting", target_alternatives=["alt"], target_source="llm")
    cands, used = gather_candidates(term, adapter, LANGS, result_limit=5, min_score=9)
    assert used
    assert {c.concept_id for c in cands} == {"LOW", "C"}  # caller re-filters by score


def test_alternative_query_cap_and_disable():
    adapter = FakeAdapter({})
    term = make_term(nb="x", target_alternatives=["a", "b", "c", "d"], target_source="llm")
    gather_candidates(term, adapter, LANGS, result_limit=5, max_alternative_queries=2)
    alt_calls = [c for c in adapter.calls if c[0] in {"a", "b", "c", "d"}]
    assert [c[0] for c in alt_calls] == ["a", "b"]

    adapter2 = FakeAdapter({})
    _, used = gather_candidates(term, adapter2, LANGS, result_limit=5,
                                max_alternative_queries=0)
    assert not used and all(c[0] not in {"a", "b", "c", "d"} for c in adapter2.calls)


def test_dedup_prefers_source_language_query_occurrence():
    # Root term -> query order is [en, nb]; the same concept comes back from
    # both. The nb occurrence must win so its trusted provenance is kept.
    adapter = FakeAdapter({
        "thing": [make_candidate(concept_id="X", score=50)],
        "ting": [make_candidate(concept_id="X", score=48, matched_lang="nb",
                                is_exact=True)],
    })
    term = make_term(nb="ting", en="thing", level=0)  # root
    cands, used = gather_candidates(term, adapter, LANGS, result_limit=5)
    assert not used
    (only,) = cands
    assert only.query_lang == "nb" and only.is_exact
