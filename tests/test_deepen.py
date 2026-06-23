"""Contract tests for the optional `deepen` stage.

Pins the invariants that keep deepen safe:

  * SELECTION — only review/no-match terms in the hard subset are deepened;
    auto-accepts are never disturbed.
  * DEPTH PROMOTION — a wider, Norwegian-first re-lookup that recovers a trusted
    nb/nn exact promotes the term to auto_accept VIA THE RULE ENGINE (the whole
    point: the primary lookup truncated the nb candidate before its exactness
    was knowable).
  * LLM IS ADVISORY — the recommendation never changes the tier and never
    auto-accepts; a hallucinated / off-list id is rejected and flagged, never
    substituted; agreement with the rule proposal is reported.
  * CACHING — the recommendation is cached under a version + candidate-set
    stamped key, so an unchanged set is not re-queried but a changed one is.
  * SIBLINGS — cross-facet siblings are harvested as extra candidates and
    enriched regardless of their (zero) reconcile score.

All offline: a FakeAdapter subclasses the real AuthorityAdapter so the genuine
enrich / _refine_match / merge code runs; a FakeRecommender returns canned picks.
"""
from __future__ import annotations

from conftest import make_candidate, make_profile, make_term

from museumvocab_reconcile.adapters.base import AuthorityAdapter
from museumvocab_reconcile.cli import gather_candidates
from museumvocab_reconcile.deepen import (
    build_recommend_payload,
    recommend_for_term,
    run_deepen,
    select_for_deepen,
    widen_candidates,
)
from museumvocab_reconcile.model import Candidate
from museumvocab_reconcile.tiering import classify

DEEP_PROFILE = make_profile({
    "deepen": {
        "select_below_score": 25.0,
        "result_limit": 20,
        "enrich_top_n": 20,
        "max_alternative_queries": 3,
        "alternatives_trigger_score": 0.0,
        "include_sibling_candidates": True,
        "max_sibling_candidates": 4,
        "use_llm": True,
    },
})


class FakeAdapter(AuthorityAdapter):
    """Search returns canned hits per query label; fetch returns canned records.

    Subclasses the real base so enrich_candidates / _refine_match (the code that
    recomputes is_exact and matched_lang from the fetched labels) is exercised
    for real.
    """

    name = "aat"

    def __init__(self, by_label, records):
        super().__init__(cache=None)
        self.by_label = by_label
        self.records = records
        self.searches: list[tuple[str, str]] = []

    def search(self, label, lang, limit=5):
        self.searches.append((label, lang))
        out = []
        for c in self.by_label.get(label, [])[:limit]:
            d = Candidate(**{**c.__dict__})
            d.query_term, d.query_lang = label, lang
            out.append(d)
        return out

    def fetch(self, concept_id):
        rec = self.records.get(concept_id, {})
        return {
            "id": concept_id,
            "uri": f"http://vocab.getty.edu/aat/{concept_id}",
            "pref_labels": rec.get("pref_labels", {}),
            "alt_labels": rec.get("alt_labels", {}),
            "scope_note": rec.get("scope_note"),
            "ancestors": rec.get("ancestors", []),
            "cross_refs": rec.get("cross_refs", []),
            "facet": rec.get("facet"),
            "aat_facet": rec.get("aat_facet"),
        }


class FakeRecommender:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def recommend(self, payload, context, cfg):
        self.calls += 1
        return dict(self.reply)


# ---- selection ------------------------------------------------------------

def test_select_skips_auto_accept_and_picks_hard_subset():
    prof = DEEP_PROFILE
    auto = classify(
        make_term(nb="x", en="x"),
        [make_candidate(concept_id="A", score=90, is_exact=True, matched_lang="nb",
                        query_term="x")],
        prof,
    )
    assert auto.tier == "auto_accept"
    assert not select_for_deepen(auto, prof)   # never disturb an auto-accept

    weak = classify(
        make_term(nb="y", en="y"),
        [make_candidate(concept_id="B", score=12, is_exact=False)],
        prof,
    )
    assert weak.tier == "review"
    assert select_for_deepen(weak, prof)       # low score -> selected


# ---- depth promotion ------------------------------------------------------

def test_deep_lookup_recovers_truncated_nb_exact_and_promotes():
    """The primary pass kept only a fuzzy English hit; the wide nb-first re-lookup
    surfaces the nb-exact concept, and the RULE engine auto-accepts it."""
    term = make_term(nb="Prøve", en="Sample", target_source="source_data")
    original = [make_candidate(concept_id="FUZZ", score=16, is_exact=False,
                               matched_label="proving mortars", matched_lang="nb",
                               facet="work_types", query_term="Prøve", query_lang="nb")]
    pre = classify(term, original, DEEP_PROFILE)
    assert pre.tier == "review"

    adapter = FakeAdapter(
        by_label={
            "Prøve": [make_candidate(concept_id="GOOD", score=12, facet=None,
                                     pref_label_target=None)],
            "Sample": [],
        },
        records={
            "GOOD": {"pref_labels": {"en": "samples"}, "alt_labels": {"nb": ["Prøve"]},
                     "facet": "work_types", "ancestors": []},
            "FUZZ": {"pref_labels": {"en": "proving mortars"}, "facet": "work_types"},
        },
    )
    out, stats = run_deepen([pre], adapter, None, DEEP_PROFILE, None,
                            gather_fn=gather_candidates)
    ct = out[0]
    assert ct.deep_used
    assert ct.tier == "auto_accept"
    assert ct.match_type == "nb_exact"
    assert ct.best.concept_id == "GOOD"
    assert stats["promoted_to_auto_accept"] == 1


# ---- LLM is advisory only -------------------------------------------------

def _two_fuzzy_review_term():
    term = make_term(nb="Plan", en="plan", target_source="llm")
    cands = [
        make_candidate(concept_id="P1", score=14, is_exact=False,
                       matched_label="urban planning projects", facet="work_types",
                       query_term="plan", query_lang="en"),
        make_candidate(concept_id="P2", score=12, is_exact=False,
                       matched_label="floor plans", facet="work_types",
                       query_term="plan", query_lang="en"),
    ]
    return classify(term, cands, DEEP_PROFILE)


def test_llm_recommendation_is_advisory_never_changes_tier():
    ct = _two_fuzzy_review_term()
    assert ct.tier == "review"
    rec = FakeRecommender({"recommended_id": "P2", "confidence": "high",
                           "reason": "floor plans fit a design drawing"})
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen)
    assert ct.tier == "review"                       # unchanged
    assert ct.llm_recommended_id == "P2"
    assert ct.llm_recommendation_confidence == "high"
    assert ct.llm_agrees_with_rule is (ct.best.concept_id == "P2")
    assert ct.llm_recommendation_source.startswith("llm_deep:")


def test_off_list_recommendation_is_rejected_and_flagged():
    ct = _two_fuzzy_review_term()
    rec = FakeRecommender({"recommended_id": "999999", "confidence": "high",
                           "reason": "made-up concept"})
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen)
    assert ct.llm_recommended_id is None             # never substituted
    assert ct.llm_agrees_with_rule is None
    assert "off-list" in ct.llm_recommendation_reason
    assert "999999" in ct.llm_recommendation_reason


def test_empty_recommendation_is_clean_not_an_error():
    ct = _two_fuzzy_review_term()
    rec = FakeRecommender({"recommended_id": "", "confidence": "low",
                           "reason": "no candidate fits"})
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen)
    assert ct.llm_recommended_id is None
    assert "off-list" not in ct.llm_recommendation_reason


def test_llm_recommendation_never_auto_accepts():
    """Even a 'high' confidence pick on a fuzzy, non-trusted term stays review."""
    ct = _two_fuzzy_review_term()
    rec = FakeRecommender({"recommended_id": "P1", "confidence": "high", "reason": "x"})
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen)
    assert ct.tier == "review"


# ---- caching --------------------------------------------------------------

class DictCache:
    def __init__(self):
        self.d = {}
    def has(self, k):
        return k in self.d
    def get(self, k):
        return self.d.get(k)
    def set(self, k, v, *, flush=True):
        self.d[k] = v
    def flush(self):
        pass


def test_recommendation_cached_by_version_and_candset():
    ct = _two_fuzzy_review_term()
    cache = DictCache()
    rec = FakeRecommender({"recommended_id": "P1", "confidence": "low", "reason": "x"})
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen, cache)
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen, cache)
    assert rec.calls == 1                            # second call served from cache

    # a changed candidate set must re-query (different stamp)
    ct.candidates = ct.candidates[:1]
    recommend_for_term(ct, rec, DEEP_PROFILE.deepen, cache)
    assert rec.calls == 2


# ---- sibling harvesting ---------------------------------------------------

def test_siblings_harvested_and_enriched_despite_zero_score():
    term = make_term(nb="Forgylling", en="gilding", target_source="source_data")
    original = [make_candidate(concept_id="TECH", score=18, is_exact=False,
                               facet="techniques", query_term="Forgylling")]
    original[0].cross_refs = [{"id": "MAT", "relation": "related", "label": "gold leaf"}]
    adapter = FakeAdapter(
        by_label={"Forgylling": [], "gilding": []},
        records={
            "TECH": {"pref_labels": {"en": "gilding"}, "facet": "techniques",
                     "cross_refs": [{"id": "MAT", "relation": "related", "label": "gold leaf"}]},
            "MAT": {"pref_labels": {"en": "gold leaf"}, "facet": "materials"},
        },
    )
    widened, n_new = widen_candidates(term, original, adapter, DEEP_PROFILE, gather_candidates)
    ids = {c.concept_id for c in widened}
    assert "MAT" in ids                              # sibling surfaced
    mat = next(c for c in widened if c.concept_id == "MAT")
    assert mat.facet == "materials"                  # and enriched


# ---- nb/nn evidence reaches the recommender -------------------------------

def test_nb_altlabels_survive_enrichment_and_reach_payload():
    """The Norwegian-first instruction is only as good as the nb/nn altLabels that
    actually reach the prompt: enrichment must fold them onto the candidate
    (filtered to prefer_langs), and the payload must surface them."""
    term = make_term(nb="Brodyr", en="embroidery")
    original = [make_candidate(concept_id="EMB", score=14, is_exact=False,
                               facet="techniques", query_term="Brodyr")]
    adapter = FakeAdapter(
        by_label={"Brodyr": [], "embroidery": []},
        records={"EMB": {
            "pref_labels": {"en": "embroidery"},
            # nb/nn are the trusted signal; 'de' must be dropped by the prefer_langs filter
            "alt_labels": {"nb": ["Brodyr"], "nn": ["Broderi"], "de": ["Stickerei"]},
            "facet": "techniques",
        }},
    )
    widened, _ = widen_candidates(term, original, adapter, DEEP_PROFILE, gather_candidates)
    emb = next(c for c in widened if c.concept_id == "EMB")
    assert emb.alt_labels.get("nb") == ["Brodyr"]
    assert emb.alt_labels.get("nn") == ["Broderi"]
    assert "de" not in emb.alt_labels                # filtered to prefer_langs

    ct = classify(term, widened, DEEP_PROFILE)
    payload = build_recommend_payload(ct)
    ev = next(c for c in payload["candidates"] if c["id"] == "EMB")
    assert ev["nb_altLabels"] == ["Brodyr"]
    assert ev["nn_altLabels"] == ["Broderi"]
