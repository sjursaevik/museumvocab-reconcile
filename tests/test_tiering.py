"""Confidence-tiering tests — the trust logic that decides auto_accept.

These pin the rules the technique pilot found trustworthy and the tripwires:
  * only a trusted-language (nb/nn) exact match in an accepted facet auto-accepts;
  * the score/gap path is gated by auto_accept.mode (so a profile can refuse to
    auto-accept anything surfaced only by an untrusted-language query);
  * cross-facet ambiguity and out-of-facet bests route to review.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.tiering import classify


def test_no_candidates_is_no_match(profile):
    out = classify(make_term(), [], profile)
    assert out.tier == "no_match"
    assert out.best is None
    assert out.proposed_facet is None
    assert any("no candidate" in r.lower() for r in out.reasons)


def test_nb_exact_in_accepted_facet_auto_accepts(profile):
    cand = make_candidate(matched_lang="nb", is_exact=True, facet="work_types",
                          pref_label_target="bowl")
    out = classify(make_term(), [cand], profile)
    assert out.tier == "auto_accept"
    assert out.proposed_facet == "work_types"
    assert out.proposed_target_term == "bowl"
    assert any("trusted language" in r for r in out.reasons)


def test_nn_exact_also_trusted(profile):
    cand = make_candidate(matched_lang="nn", is_exact=True, facet="materials")
    assert classify(make_term(), [cand], profile).tier == "auto_accept"


def test_exact_match_outside_accepted_facet_goes_to_review(profile):
    # Exact nb match, but the facet is not in the accepted set -> review, never accept.
    cand = make_candidate(matched_lang="nb", is_exact=True, facet="styles_periods")
    out = classify(make_term(), [cand], profile)
    assert out.tier == "review"
    assert any("not in accepted set" in r for r in out.reasons)


def test_score_gap_path_auto_accepts_in_full_mode(profile):
    best = make_candidate(concept_id="A", score=40, facet="work_types", is_exact=False,
                          matched_lang="en")
    rival = make_candidate(concept_id="B", score=30, facet="work_types")
    out = classify(make_term(), [best, rival], profile)
    assert out.tier == "auto_accept"
    assert out.best.concept_id == "A"
    assert any("gap" in r for r in out.reasons)


def test_exact_only_mode_refuses_score_gap_acceptance():
    # The lever protecting against LLM-English (untrusted) score-based auto-accept:
    # in exact_only mode a strong score/gap alone must NOT auto-accept.
    profile = make_profile({"thresholds": {"auto_accept": {"mode": "exact_only"}}})
    best = make_candidate(concept_id="A", score=99, facet="work_types",
                          is_exact=False, matched_lang="en")
    rival = make_candidate(concept_id="B", score=10, facet="work_types")
    out = classify(make_term(), [best, rival], profile)
    assert out.tier == "review"
    assert any("exact_only" in r for r in out.reasons)


def test_off_mode_sends_everything_to_review():
    profile = make_profile({"thresholds": {"auto_accept": {"mode": "off"}}})
    cand = make_candidate(matched_lang="nb", is_exact=True, facet="work_types")
    out = classify(make_term(), [cand], profile)
    assert out.tier == "review"
    assert any("mode=off" in r for r in out.reasons)


def test_cross_facet_ambiguity_forces_review(profile):
    # Two near-tied candidates in different (both accepted) facets -> genuine
    # ambiguity, route to review even though each facet on its own is accepted.
    a = make_candidate(concept_id="A", score=30, facet="work_types", is_exact=False)
    b = make_candidate(concept_id="B", score=28, facet="techniques", is_exact=False)
    out = classify(make_term(), [a, b], profile)
    assert out.tier == "review"
    assert any("cross-facet" in r for r in out.reasons)


def test_prefers_accepted_facet_candidate_over_higher_out_of_facet(profile):
    # Top overall hit is out-of-facet; a lower in-facet nb-exact candidate exists.
    # classify() should propose the in-facet one and auto-accept on the trusted
    # exact match (a negative score gap must not block a trusted exact match).
    out_of_facet = make_candidate(concept_id="HI", score=50, facet="styles_periods",
                                  is_exact=False, matched_lang="en")
    in_facet = make_candidate(concept_id="LO", score=35, facet="work_types",
                              is_exact=True, matched_lang="nb")
    out = classify(make_term(), [out_of_facet, in_facet], profile)
    assert out.best.concept_id == "LO"
    assert out.proposed_facet == "work_types"
    assert out.tier == "auto_accept"


def test_trusted_lang_exact_match_toggle_disables_exact_path():
    # With trusted_lang_exact_match=False, an nb-exact but low-score candidate
    # no longer auto-accepts via the exact path and falls through to review.
    profile = make_profile(
        {"thresholds": {"auto_accept": {"trusted_lang_exact_match": False}}}
    )
    cand = make_candidate(matched_lang="nb", is_exact=True, facet="work_types", score=10)
    out = classify(make_term(), [cand], profile)
    assert out.tier == "review"


def test_accept_all_lets_any_facet_through():
    profile = make_profile({"facets": {"accept_all": True, "accepted": []}})
    cand = make_candidate(matched_lang="nb", is_exact=True, facet="anything_at_all")
    out = classify(make_term(), [cand], profile)
    assert out.tier == "auto_accept"
    assert out.proposed_facet == "anything_at_all"


# --------------------------------------------------------------------------
# preferred_hierarchies (hierarchy_mode: prefer) — finer-grained than the facet
# --------------------------------------------------------------------------

def _anc(*ids):
    return [{"id": i, "label": i} for i in ids]


def _prefer_profile(anchors):
    return make_profile({"facets": {"preferred_hierarchies": anchors}})  # mode defaults to "prefer"


def test_prefer_steers_to_in_hierarchy_candidate_over_higher_score():
    # Without anchors, the score-40 out-of-hierarchy candidate would auto-accept.
    # With H anchored, the score-30 in-hierarchy candidate is proposed instead, and
    # the shrunken gap correctly routes the now-uncertain result to review.
    top = make_candidate(concept_id="A", score=40, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300999000", "300264092"))
    inh = make_candidate(concept_id="B", score=30, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300264551", "300264092"))
    prof = _prefer_profile({"300264551": "Furnishings and Equipment"})
    out = classify(make_term(), [top, inh], prof)
    assert out.best.concept_id == "B"
    assert out.proposed_hierarchy == "Furnishings and Equipment (300264551)"
    assert out.tier == "review"
    assert any("preferred hierarchy" in r for r in out.reasons)


def test_prefer_never_demotes_a_trusted_exact_top_pick():
    # nb-exact top pick sits OUTSIDE the preferred hierarchy; it must still win and
    # auto-accept — the trusted-exact signal is sacrosanct.
    top = make_candidate(concept_id="A", score=40, is_exact=True, matched_lang="nb",
                         facet="work_types", ancestors=_anc("300999000", "300264092"))
    inh = make_candidate(concept_id="B", score=30, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300264551", "300264092"))
    prof = _prefer_profile({"300264551": "Furnishings and Equipment"})
    out = classify(make_term(), [top, inh], prof)
    assert out.best.concept_id == "A"
    assert out.tier == "auto_accept"
    assert out.proposed_hierarchy is None


def test_prefer_keeps_in_hierarchy_winner_and_surfaces_it():
    # Top accepted candidate is already in the preferred hierarchy: unchanged pick,
    # auto-accepts on the score gap, and the hierarchy is surfaced.
    top = make_candidate(concept_id="A", score=40, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300264551", "300264092"))
    rival = make_candidate(concept_id="B", score=30, facet="work_types",
                          ancestors=_anc("300999000", "300264092"))
    prof = _prefer_profile({"300264551": "Furnishings and Equipment"})
    out = classify(make_term(), [top, rival], prof)
    assert out.best.concept_id == "A"
    assert out.tier == "auto_accept"
    assert out.proposed_hierarchy == "Furnishings and Equipment (300264551)"


def test_empty_preferred_hierarchies_is_a_no_op():
    top = make_candidate(concept_id="A", score=40, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300999000", "300264092"))
    inh = make_candidate(concept_id="B", score=30, facet="work_types",
                        ancestors=_anc("300264551", "300264092"))
    out = classify(make_term(), [top, inh], make_profile())  # no anchors
    assert out.best.concept_id == "A"
    assert out.proposed_hierarchy is None


def test_hierarchy_mode_off_ignores_anchors():
    top = make_candidate(concept_id="A", score=40, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300999000", "300264092"))
    inh = make_candidate(concept_id="B", score=30, facet="work_types",
                        ancestors=_anc("300264551", "300264092"))
    prof = make_profile({"facets": {"hierarchy_mode": "off",
                                    "preferred_hierarchies": {"300264551": "x"}}})
    out = classify(make_term(), [top, inh], prof)
    assert out.best.concept_id == "A"
    assert out.proposed_hierarchy is None


def test_unquoted_numeric_anchor_keys_are_coerced_and_match():
    # YAML parses an unquoted `300264551:` key as an int; it must still match the
    # (string) ancestor ids rather than silently missing.
    inh = make_candidate(concept_id="B", score=30, is_exact=False, matched_lang="en",
                         facet="work_types", ancestors=_anc("300264551", "300264092"))
    prof = make_profile({"facets": {"preferred_hierarchies": {300264551: "Furnishings"}}})
    out = classify(make_term(), [inh], prof)
    assert out.proposed_hierarchy == "Furnishings (300264551)"


def test_anchor_matching_concept_id_itself_counts():
    cand = make_candidate(concept_id="300264551", score=30, is_exact=False,
                          matched_lang="en", facet="work_types",
                          ancestors=_anc("300264092"))
    prof = _prefer_profile({"300264551": "Furnishings"})
    out = classify(make_term(), [cand], prof)
    assert out.proposed_hierarchy == "Furnishings (300264551)"


def test_invalid_hierarchy_mode_raises():
    import pytest
    with pytest.raises(ValueError, match="hierarchy_mode"):
        make_profile({"facets": {"hierarchy_mode": "boost"}})
