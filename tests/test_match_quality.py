"""Match-quality redesign tests: banding, attribution, prefLabel trust.

Pins the three-part redesign driven by the 'Sari' and 'Dukkehus' field cases:

  * proposal ordering is banded — exact-in-match_langs > exact-outside >
    fuzzy — with reconcile score ranking only WITHIN a band (its absolute
    values are noise at the low end: the exact-en hit for 'Dollhouse' scored
    9.5 below three fuzzy relatives);
  * _refine_match attributes a surface form shared by several languages to
    the caller's preferred languages first ('sari' exists in en/es/it/fr/nl
    and 'Sari' in de — alphabetical tie-break used to pick 'de');
  * an exact match on the TARGET-language PREFERRED label, where the query is
    the term's source-data English, is trusted to auto-accept
    (languages.trusted_target_pref_exact); alt-label exacts and any LLM/edited
    provenance stay review-tier.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.adapters.aat import AatAdapter
from museumvocab_reconcile.tiering import classify


# ---- banding: the Dukkehus regression ----------------------------------------

VISUAL_WORKS = {"id": "300179869", "label": "Visual Works (hierarchy name)"}


def dukkehus_profile():
    return make_profile({
        "languages": {"match_langs": ["nb", "nn", "en"]},
        "facets": {
            "accept_all": True,
            "preferred_hierarchies": {"300179869": "Visual Works (hierarchy name)"},
            "hierarchy_mode": "prefer",
        },
    })


def dukkehus_candidates():
    # Real shape of the field case: three fuzzy relatives outscore the exact
    # descriptor-adjacent hit; two of the four sit in the same anchor.
    return [
        make_candidate(concept_id="300386287", score=17.5, is_exact=False,
                       matched_lang="en", facet=None),
        make_candidate(concept_id="300423586", score=14.8, is_exact=False,
                       matched_lang="en", facet="work_types"),
        make_candidate(concept_id="300457671", score=11.8, is_exact=False,
                       matched_lang="en", facet="work_types",
                       matched_label="pronk poppenhuizen",
                       ancestors=[VISUAL_WORKS]),
        make_candidate(concept_id="300136025", score=9.5, is_exact=True,
                       matched_lang="en", query_lang="en", query_term="Dollhouse",
                       matched_label="dollhouse", pref_label_target="dollhouses",
                       ancestors=[VISUAL_WORKS]),
    ]


def test_exact_match_outranks_fuzzy_regardless_of_score():
    term = make_term(nb="Dukkehus", en="Dollhouse")  # source_data English
    out = classify(term, dukkehus_candidates(), dukkehus_profile())
    assert out.best.concept_id == "300136025"
    # exact on an ALT label ('dollhouse' vs descriptor 'dollhouses') is not the
    # prefLabel rule — and the negative gap blocks score auto-accept: review.
    assert out.tier == "review"


def test_banding_demotion_of_fuzzy_top_is_disclosed():
    term = make_term(nb="Dukkehus", en="Dollhouse")
    out = classify(term, dukkehus_candidates(), dukkehus_profile())
    assert any(
        "300386287" in r and "fuzzy match, outranked by an exact label match" in r
        for r in out.reasons
    )


def test_hierarchy_steering_inherits_band_order():
    # Within the anchor, the exact 300136025 must beat the higher-scored fuzzy
    # 300457671 — the old score-ordered in_hier[0] proposed pronk poppenhuizen.
    term = make_term(nb="Dukkehus", en="Dollhouse", expected_hierarchy="visual works")
    out = classify(term, dukkehus_candidates(), dukkehus_profile())
    assert out.best.concept_id == "300136025"
    assert any("'visual works' agrees" in r for r in out.reasons)


# ---- attribution: the Sari regression -----------------------------------------

SARI_LABELS = {
    "pref": {"en": "saris (garments)"},
    "alt": {"de": ["Sari"], "en": ["sari", "saris"], "es": ["sari"],
            "fr": ["sari"], "it": ["sari"], "nl": ["sari"]},
}


def _refine(prefer_langs):
    c = make_candidate(matched_label="saris (garments)", matched_lang="en",
                       is_exact=False, query_term="Sari", query_lang="nb")
    AatAdapter(cache=None)._refine_match(
        c, {"pref_labels": SARI_LABELS["pref"], "alt_labels": SARI_LABELS["alt"]},
        prefer_langs,
    )
    return c


def test_shared_surface_form_attributed_to_preferred_language():
    c = _refine(["nb", "nn", "en"])
    assert c.is_exact is True
    assert c.matched_lang == "en"        # not 'de' (alphabetical accident)


def test_without_preference_alphabetical_tiebreak_documented():
    # Documents WHY prefer_langs must be passed: bare refine still picks 'de'.
    assert _refine(None).matched_lang == "de"


def test_query_language_still_wins_over_preference():
    c = make_candidate(matched_label="x", matched_lang="en", is_exact=False,
                       query_term="forgylling", query_lang="nb")
    AatAdapter(cache=None)._refine_match(
        c, {"pref_labels": {"en": "gilding"},
            "alt_labels": {"nb": ["forgylling"], "en": ["forgylling"]}},
        ["en"],
    )
    assert c.matched_lang == "nb"


# ---- trusted source-English prefLabel exact ------------------------------------

def pref_exact_candidate(**over):
    kw = dict(concept_id="P", score=20, is_exact=True, matched_lang="en",
              query_lang="en", query_term="gilding",
              matched_label="gilding", pref_label_target="gilding",
              facet="work_types")
    kw.update(over)
    return make_candidate(**kw)


def test_source_english_preflabel_exact_auto_accepts():
    term = make_term(nb="forgylling", en="gilding")     # target_source source_data
    out = classify(term, [pref_exact_candidate()], make_profile())
    assert out.tier == "auto_accept"


def test_llm_english_preflabel_exact_stays_review():
    term = make_term(nb="forgylling", en="gilding", target_source="llm")
    out = classify(term, [pref_exact_candidate()], make_profile())
    assert out.tier == "review"


def test_alt_label_exact_is_not_the_pref_rule():
    term = make_term(nb="dukkehus", en="Dollhouse")
    c = pref_exact_candidate(matched_label="dollhouse",
                             pref_label_target="dollhouses",
                             query_term="Dollhouse")
    out = classify(term, [c], make_profile())
    assert out.tier == "review"


def test_pref_rule_can_be_disabled():
    profile = make_profile({"languages": {"trusted_target_pref_exact": False}})
    term = make_term(nb="forgylling", en="gilding")
    out = classify(term, [pref_exact_candidate()], profile)
    assert out.tier == "review"


def test_pref_rule_never_overridden_by_steering():
    profile = make_profile({"facets": {
        "preferred_hierarchies": {"300179869": "Visual Works (hierarchy name)"},
        "hierarchy_mode": "prefer",
    }})
    trusted = pref_exact_candidate()
    in_hier = make_candidate(concept_id="H", score=90, facet="work_types",
                             ancestors=[VISUAL_WORKS])
    term = make_term(nb="forgylling", en="gilding")
    out = classify(term, [in_hier, trusted], profile)
    assert out.best.concept_id == "P" and out.tier == "auto_accept"
