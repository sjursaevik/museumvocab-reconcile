"""Tiering tests for the two LLM-metadata signals.

Tripwire (skill: "LLM English is a lookup query only, never a trust signal"):
a best candidate surfaced via a target-language query whose label came from the
translate step (target_source llm/human) must NEVER auto-accept — not on
score/gap, not even on an exact label hit. nb/nn-query candidates of the same
term keep their normal trust.

Advisory (expected_facet): the LLM's facet prediction may steer WHICH near-tied
candidate is proposed and annotate reasons — it never changes the tier, never
overrides a trusted exact, and never beats a human-curated hierarchy hit.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.tiering import classify


# ---- LLM-English surfaced matches are review-only --------------------------

def test_llm_english_high_score_never_auto_accepts(profile):
    cand = make_candidate(score=90, facet="techniques", matched_lang="en",
                          query_lang="en", query_term="gilding")
    term = make_term(nb="forgylling", en="gilding", target_source="llm")
    out = classify(term, [cand], profile)
    assert out.tier == "review"
    assert any("never auto-accept" in r for r in out.reasons)


def test_llm_english_exact_hit_never_auto_accepts(profile):
    # Even an exact match on an nb altLabel is a coincidence when the QUERY
    # string was LLM-generated (e.g. a loanword): review, not trust.
    cand = make_candidate(score=90, facet="techniques", matched_lang="nb",
                          is_exact=True, query_lang="en", query_term="appliqué")
    term = make_term(nb="applikasjon", en="appliqué", target_source="llm")
    assert classify(term, [cand], profile).tier == "review"


def test_human_edited_english_also_review_only(profile):
    # Cataloguer-edited LLM English is human-authored but NOT source data;
    # only source-data English supports auto-accept.
    cand = make_candidate(score=90, facet="techniques", matched_lang="en",
                          query_lang="en")
    term = make_term(nb="x", en="gilding", target_source="human")
    assert classify(term, [cand], profile).tier == "review"


def test_source_data_english_score_path_still_auto_accepts(profile):
    cand = make_candidate(score=90, facet="techniques", matched_lang="en",
                          query_lang="en")
    term = make_term(nb="x", en="gilding")  # target_source defaults source_data
    assert classify(term, [cand], profile).tier == "auto_accept"


def test_nb_query_candidate_of_translated_term_still_trusted(profile):
    # The guard keys on the QUERY that surfaced the best candidate, not on the
    # term having been translated: an nb exact match still auto-accepts.
    cand = make_candidate(score=40, facet="techniques", matched_lang="nb",
                          is_exact=True, query_lang="nb")
    term = make_term(nb="forgylling", en="gilding", target_source="llm")
    assert classify(term, [cand], profile).tier == "auto_accept"


# ---- expected_facet: advisory steering + annotation -------------------------

def test_expected_facet_breaks_near_tie_within_pool(profile):
    top = make_candidate(concept_id="T", score=30, facet="techniques")
    near = make_candidate(concept_id="M", score=28, facet="materials")
    term = make_term(nb="x", expected_facet="materials")
    out = classify(term, [top, near], profile)
    assert out.best.concept_id == "M"
    assert out.proposed_facet == "materials"
    # near-tied cross-facet candidates still flag ambiguity -> review
    assert out.tier == "review"
    assert any("LLM expected facet" in r and "agrees" in r for r in out.reasons)


def test_expected_facet_does_not_reach_beyond_gap(profile):
    top = make_candidate(concept_id="T", score=40, facet="techniques")
    far = make_candidate(concept_id="M", score=20, facet="materials")
    term = make_term(nb="x", expected_facet="materials")
    out = classify(term, [top, far], profile)
    assert out.best.concept_id == "T"
    assert any("differs" in r for r in out.reasons)


def test_expected_facet_never_overrides_trusted_exact(profile):
    exact = make_candidate(concept_id="T", score=30, facet="techniques",
                           matched_lang="nb", is_exact=True)
    near = make_candidate(concept_id="M", score=29, facet="materials")
    term = make_term(nb="x", expected_facet="materials")
    out = classify(term, [exact, near], profile)
    assert out.best.concept_id == "T"


def test_expected_facet_does_not_beat_hierarchy_hit():
    profile = make_profile({"facets": {
        "preferred_hierarchies": {"300100000": "Gilding hierarchy"},
        "hierarchy_mode": "prefer",
    }})
    in_hier = make_candidate(concept_id="T", score=30, facet="techniques",
                             ancestors=[{"id": "300100000", "label": "anchor"}])
    near = make_candidate(concept_id="M", score=29, facet="materials")
    term = make_term(nb="x", expected_facet="materials")
    out = classify(term, [in_hier, near], profile)
    assert out.best.concept_id == "T"


def test_expected_facet_absent_changes_nothing(profile):
    top = make_candidate(concept_id="T", score=30, facet="techniques")
    near = make_candidate(concept_id="M", score=28, facet="materials")
    out = classify(make_term(nb="x"), [top, near], profile)
    assert out.best.concept_id == "T"
    assert not any("LLM expected facet" in r for r in out.reasons)
