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


# ---- prefer in-match_langs candidates when selecting best -----------------

def test_prefer_match_langs_proposes_real_match_over_higher_scored_und():
    prof = make_profile({"languages": {"match_langs": ["nb", "nn", "en"]}})
    und = make_candidate(concept_id="U", score=80, is_exact=True, matched_lang="und",
                         facet="work_types")
    en = make_candidate(concept_id="E", score=50, is_exact=False, matched_lang="en",
                        facet="work_types")
    out = classify(make_term(), [und, en], prof)
    assert out.best.concept_id == "E"          # the credible match is proposed
    # higher-scored und rival shrinks the gap, so it routes to review (conservative)
    assert out.tier == "review"


def test_prefer_match_langs_auto_accepts_when_real_match_leads():
    prof = make_profile({"languages": {"match_langs": ["nb", "nn", "en"]}})
    en = make_candidate(concept_id="E", score=60, is_exact=False, matched_lang="en",
                        facet="work_types")
    und = make_candidate(concept_id="U", score=30, matched_lang="und", facet="work_types")
    out = classify(make_term(), [en, und], prof)
    assert out.best.concept_id == "E"
    assert out.tier == "auto_accept"


def test_all_und_falls_back_and_gate_routes_to_review():
    prof = make_profile({"languages": {"match_langs": ["nb", "nn", "en"]}})
    u1 = make_candidate(concept_id="U", score=80, matched_lang="und", facet="work_types")
    out = classify(make_term(), [u1], prof)
    assert out.best.concept_id == "U"
    assert out.tier == "review"
    assert any("match_langs" in r for r in out.reasons)


def test_match_langs_warning_when_trusted_langs_omitted():
    prof = make_profile({"languages": {"match_langs": ["en"]}})  # omits nb, nn
    assert any("trusted_exact_match_langs" in w for w in prof.validate())


# ---- _parse_linked_art language attribution -------------------------------

def test_untagged_name_is_english_tagged_unmapped_is_und():
    from museumvocab_reconcile.adapters.aat import _parse_linked_art
    node = {
        "type": "Type",
        "identified_by": [
            # untagged English altLabel -> should be "en", not "und"
            {"type": "Name", "content": "licences"},
            # French prefLabel carrying a (mapped-as-None) language URI -> "und"
            {"type": "Name", "content": "agrément",
             "language": [{"id": "http://vocab.getty.edu/aat/300387000"}],  # not in LANG_URI
             "classified_as": [{"id": "http://vocab.getty.edu/aat/300404670"}]},
            # tagged English prefLabel -> "en"
            {"type": "Name", "content": "registrations (licenses)",
             "language": [{"id": "http://vocab.getty.edu/aat/300388277"}],
             "classified_as": [{"id": "http://vocab.getty.edu/aat/300404670"}]},
        ],
    }
    pref, alt, _scope, _broader = _parse_linked_art(node)
    assert pref.get("en") == "registrations (licenses)"
    assert pref.get("und") == "agrément"           # tagged foreign stays und
    assert "licences" in alt.get("en", [])         # untagged altLabel -> en


def test_matched_lang_on_real_getty_language_uris():
    # End-to-end of the 300027760 fix: an English query coincides with the French
    # altLabel -> matched_lang 'fr' (gated by match_langs); an English altLabel
    # query -> matched_lang 'en' (no longer 'und').
    from museumvocab_reconcile.adapters.aat import _parse_linked_art, AatAdapter
    node = {"type": "Type", "identified_by": [
        {"type": "Name", "content": "registrations",
         "language": [{"id": "http://vocab.getty.edu/language/en"}],
         "alternative": [{"type": "Name", "content": "registrations (licenses)",
                          "language": [{"id": "http://vocab.getty.edu/language/en"}]}],
         "classified_as": [{"id": "http://vocab.getty.edu/aat/300404670"}]},
        {"type": "Name", "content": "agrément",
         "language": [{"id": "http://vocab.getty.edu/language/fr"}],
         "classified_as": [{"id": "http://vocab.getty.edu/term/type/AlternateDescriptor"}]},
        {"type": "Name", "content": "registration",
         "language": [{"id": "http://vocab.getty.edu/language/en"}],
         "alternative": [{"type": "Name", "content": "registration (license)",
                          "language": [{"id": "http://vocab.getty.edu/language/en"}]}]},
    ]}
    pref, alt, *_ = _parse_linked_art(node)
    rec = {"pref_labels": pref, "alt_labels": alt}
    a = AatAdapter(cache=None)

    fr = make_candidate(matched_lang="en", is_exact=False, query_term="agrément")
    a._refine_match(fr, rec)
    assert (fr.matched_lang, fr.is_exact) == ("fr", True)

    en = make_candidate(matched_lang="en", is_exact=False, query_term="registration (license)")
    a._refine_match(en, rec)
    assert (en.matched_lang, en.is_exact) == ("en", True)
