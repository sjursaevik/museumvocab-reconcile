"""Matched-language correctness and the match_langs gate.

Reproduces the 'agrément' bug: an English query string that coincides with a
record's French prefLabel (tagged 'und' because its language URI isn't mapped).
The reconcile display name is the English label, so the provisional matched_lang
was wrongly 'en'; enrichment must recompute it from the record's real labels.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.adapters.aat import AatAdapter
from museumvocab_reconcile.tiering import classify


def _refine(cand, pref, alt=None):
    AatAdapter(cache=None)._refine_match(cand, {"pref_labels": pref, "alt_labels": alt or {}})
    return cand


def test_cross_language_exact_match_is_attributed_to_und_not_query_lang():
    # English query "agrément" coincides with the French prefLabel (under "und");
    # English prefLabel is the unrelated "registrations (licenses)".
    c = make_candidate(matched_label="registrations (licenses)", matched_lang="en",
                       is_exact=False, query_term="agrément")
    _refine(c, {"en": "registrations (licenses)", "und": "agrément"})
    assert c.is_exact is True
    assert c.matched_lang == "und"
    assert c.matched_label == "agrément"


def test_nb_altlabel_exact_now_recognised_despite_english_display_name():
    # nb query matched via a Norwegian altLabel; display name stays English.
    # Previously is_exact compared to the English name -> False; now -> True/nb.
    c = make_candidate(matched_label="gilding", matched_lang="nb", is_exact=False,
                       query_term="forgylling")
    _refine(c, {"en": "gilding"}, {"nb": ["forgylling"]})
    assert c.is_exact is True
    assert c.matched_lang == "nb"
    assert c.matched_label == "forgylling"


def test_query_lang_preferred_when_label_identical_across_languages():
    c = make_candidate(matched_lang="en", is_exact=False, query_term="red")
    _refine(c, {"en": "red", "und": "red"})
    assert c.matched_lang == "en"


def test_non_exact_match_is_marked_not_exact_and_keeps_display_values():
    c = make_candidate(matched_label="registrations (licenses)", matched_lang="en",
                       is_exact=True, query_term="something else")
    _refine(c, {"en": "registrations (licenses)", "und": "agrément"})
    assert c.is_exact is False
    assert c.matched_lang == "en"            # provisional display retained
    assert c.matched_label == "registrations (licenses)"


def test_refine_is_noop_without_query_term():
    c = make_candidate(matched_label="gilding", matched_lang="en", is_exact=True,
                       query_term="")
    # enrich_candidates only calls _refine_match when query_term is set; calling
    # directly with an empty query is a no-op too.
    _refine(c, {"en": "gilding"})
    assert c.matched_lang == "en" and c.is_exact is True


# ---- the match_langs gate -------------------------------------------------

def test_match_langs_gate_routes_und_match_to_review():
    prof = make_profile({"languages": {"match_langs": ["nb", "nn", "en"]}})
    # strong score that would otherwise auto-accept, but matched via "und"
    c = make_candidate(concept_id="A", score=80, is_exact=True, matched_lang="und",
                       facet="work_types")
    out = classify(make_term(), [c], prof)
    assert out.tier == "review"
    assert any("match_langs" in r for r in out.reasons)


def test_match_langs_empty_is_no_restriction():
    prof = make_profile()  # match_langs defaults to []
    c = make_candidate(concept_id="A", score=80, is_exact=False, matched_lang="und",
                       facet="work_types")
    rival = make_candidate(concept_id="B", score=30, facet="work_types")
    out = classify(make_term(), [c, rival], prof)
    assert out.tier == "auto_accept"          # gap path unaffected by language


def test_match_langs_allows_in_set_language():
    prof = make_profile({"languages": {"match_langs": ["nb", "nn", "en"]}})
    c = make_candidate(concept_id="A", score=80, is_exact=False, matched_lang="en",
                       facet="work_types")
    rival = make_candidate(concept_id="B", score=30, facet="work_types")
    out = classify(make_term(), [c, rival], prof)
    assert out.tier == "auto_accept"
